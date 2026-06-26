"""
app/core/scheduler.py

Планировщик Этапа 2 — APScheduler поверх хранилища.

Задача:
    по расписанию каждого файла (функция ТЗ №2) запускать тихое обновление
    из Этапа 1 (excel_refresher) и складывать результат в журнал (функция №5).

Модель работы:
    * SQLite — источник правды; задачи планировщика пересобираются из базы
      при старте, поэтому персистентный jobstore и pickling не нужны;
    * обновления выполняются в фоновом пуле потоков APScheduler; каждый вызов
      excel_refresher сам инициализирует COM в своём потоке (см. Этап 1);
    * на каждый файл — свой Lock с неблокирующим захватом: плановое обновление
      и ручное «Обновить сейчас» не запустят второй Excel на тот же файл;
    * на каждый файл max_instances=1 + coalesce — обновления не накладываются,
      даже если предыдущее ещё идёт.

Развязка с Этапом 1:
    refresh_func передаётся параметром (по умолчанию
    excel_refresher.refresh_workbook), поэтому планировщик тестируется и без
    Windows/Excel — достаточно подставить заглушку.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core import excel_refresher
from app.core.storage import RunRecord, Schedule, Storage, WatchedFile

logger = logging.getLogger(__name__)


def _local_timezone():
    """Возвращает zoneinfo-зону ОС, совместимую с APScheduler.

    Используем ZoneInfo с именем из tzlocal, а при недоступности —
    фиксированное смещение. Это гарантирует, что next_run_time
    хранится в реальной именованной зоне, а не в fixed-offset UTC+N,
    из-за которой APScheduler мог показывать время со сдвигом.
    """
    try:
        from tzlocal import get_localzone
        return get_localzone()
    except Exception:
        return datetime.now().astimezone().tzinfo


@dataclass(slots=True)
class FileStatus:
    """Снимок состояния файла для статуса в реальном времени (функция ТЗ №8)."""

    file: WatchedFile
    last_run: RunRecord | None
    next_run: datetime | None
    running: bool


def _build_trigger(schedule: Schedule):
    """Преобразовать наше расписание в триггер APScheduler."""
    if schedule.kind == "interval":
        return IntervalTrigger(minutes=schedule.interval_minutes)

    hour, minute = schedule.hour_minute()
    if schedule.kind == "daily":
        return CronTrigger(hour=hour, minute=minute)
    if schedule.kind == "weekly":
        return CronTrigger(
            day_of_week=",".join(schedule.days_of_week),
            hour=hour,
            minute=minute,
        )
    raise ValueError(f"неизвестный вид расписания: {schedule.kind!r}")


class RefreshScheduler:
    """Планирует и выполняет тихие обновления файлов."""

    def __init__(
        self,
        storage: Storage,
        *,
        refresh_func: Callable | None = None,
        on_run_complete: Callable[[RunRecord], None] | None = None,
        timeout_sec: int = 300,
        misfire_grace_sec: int = 3600,
        timezone=None,
        max_concurrent: int = 2,
        defer_minutes: int = 10,
        max_deferrals: int = 3,
    ) -> None:
        self._storage = storage
        self._refresh_func = refresh_func or excel_refresher.refresh_workbook
        self._on_run_complete = on_run_complete
        self._timeout_sec = timeout_sec
        self._misfire_grace = misfire_grace_sec

        # Задача 1: жёсткий потолок одновременных обновлений — действует и на
        # плановые задачи, и на ручные run_now (оба пути проходят через _execute).
        self._max_concurrent = max(1, int(max_concurrent))
        self._slots = threading.BoundedSemaphore(self._max_concurrent)

        # Задача 2: параметры откладывания «файл занят сотрудником».
        self._defer_minutes = int(defer_minutes)
        self._max_deferrals = int(max_deferrals)
        self._deferrals: dict[int, int] = {}
        self._deferrals_guard = threading.Lock()

        # Пул APScheduler ограничиваем тем же числом, что и слоты, — чтобы
        # планировщик не запускал больше плановых задач, чем мы готовы выполнять.
        self._scheduler = BackgroundScheduler(
            timezone=timezone or _local_timezone(),
            executors={"default": ThreadPoolExecutor(max_workers=self._max_concurrent)},
        )

        # Защита от одновременного обновления одного файла.
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Множество выполняющихся прямо сейчас файлов (для статуса).
        self._running: set[int] = set()
        self._running_guard = threading.Lock()

    # --- Идентификатор задачи в планировщике ---
    @staticmethod
    def _job_id(file_id: int) -> str:
        return f"file:{file_id}"

    def _file_lock(self, file_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(file_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[file_id] = lock
            return lock

    # ----------------------------------------------------------------- #
    # Жизненный цикл
    # ----------------------------------------------------------------- #
    def start(self) -> None:
        """Запустить планировщик и пересобрать задачи из базы."""
        if not self._scheduler.running:
            self._scheduler.start()
        for wf in self._storage.list_files():
            if wf.enabled and wf.id is not None:
                self._add_or_update_job(wf)
        logger.info("Планировщик запущен; активных файлов: %s",
                    self._storage.count_enabled())

    def shutdown(self, wait: bool = True) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
        logger.info("Планировщик остановлен")

    # ----------------------------------------------------------------- #
    # Синхронизация задач с базой (вызывает UI на Этапе 3)
    # ----------------------------------------------------------------- #
    def _add_or_update_job(self, wf: WatchedFile) -> None:
        self._scheduler.add_job(
            self._execute,
            trigger=_build_trigger(wf.schedule),
            args=[wf.id],
            id=self._job_id(wf.id),
            name=str(wf.path),
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=self._misfire_grace,
        )
        logger.info("Задача обновлена id=%s: %s", wf.id, wf.schedule.describe())

    def sync_file(self, file_id: int) -> None:
        """
        Привести задачу планировщика в соответствие с базой: добавить/обновить,
        если файл включён, или снять задачу, если выключен либо удалён.
        Вызывается после любых изменений файла из интерфейса.
        """
        wf = self._storage.get_file(file_id)
        if wf is None or not wf.enabled:
            self.remove_job(file_id)
            return
        self._add_or_update_job(wf)

    def remove_job(self, file_id: int) -> None:
        try:
            self._scheduler.remove_job(self._job_id(file_id))
            logger.info("Задача снята id=%s", file_id)
        except Exception:
            pass  # задачи не было — это нормально
        # Снимаем и отложенный повтор, и счётчик попыток откладывания.
        self.remove_retry(file_id)
        self._reset_deferrals(file_id)

    def run_now(self, file_id: int) -> threading.Thread:
        """Запустить обновление файла немедленно (в отдельном потоке)."""
        thread = threading.Thread(
            target=self._execute, args=(file_id,), daemon=True,
            name=f"refresh-now-{file_id}",
        )
        thread.start()
        return thread

    # ----------------------------------------------------------------- #
    # Тело задачи
    # ----------------------------------------------------------------- #
    def _execute(self, file_id: int) -> RunRecord | None:
        lock = self._file_lock(file_id)
        if not lock.acquire(blocking=False):
            logger.info("Файл id=%s уже обновляется — пропуск", file_id)
            return None
        try:
            wf = self._storage.get_file(file_id)
            if wf is None:
                logger.warning("Файл id=%s не найден в базе — пропуск", file_id)
                return None
            if not wf.enabled:
                return None

            # --- Задача 2: файл открыт сотрудником → отложить ---
            # Проверка дешёвая и идёт ДО захвата слота и запуска Excel,
            # чтобы не занимать ресурс впустую.
            if excel_refresher.is_file_locked(wf.path):
                return self._handle_locked(wf)

            # Файл свободен — сбрасываем счётчик откладываний.
            self._reset_deferrals(file_id)

            # --- Задача 1: не более N одновременных обновлений ---
            # Блокирующий захват слота = очередь: лишние обновления ждут здесь,
            # пока не освободится один из N слотов.
            self._slots.acquire()
            try:
                with self._running_guard:
                    self._running.add(file_id)
                try:
                    result = self._refresh_func(wf.path, timeout_sec=self._timeout_sec)
                finally:
                    with self._running_guard:
                        self._running.discard(file_id)
            finally:
                self._slots.release()

            record = self._storage.log_run(result, file_id=file_id)
            logger.info(
                "Обновление id=%s: %s",
                file_id,
                "OK" if record.success else f"ОШИБКА: {record.error}",
            )
            self._publish(record)
            return record
        finally:
            lock.release()

    # ----------------------------------------------------------------- #
    # Откладывание занятого файла (Задача 2) и публикация результата
    # ----------------------------------------------------------------- #
    @staticmethod
    def _retry_job_id(file_id: int) -> str:
        return f"file:{file_id}:retry"

    def _publish(self, record: RunRecord) -> None:
        """Отдать запись в UI через колбэк (обычные и «искусственные» события)."""
        if self._on_run_complete is not None:
            try:
                self._on_run_complete(record)
            except Exception:
                logger.exception("Ошибка в колбэке on_run_complete")

    def _reset_deferrals(self, file_id: int) -> None:
        with self._deferrals_guard:
            self._deferrals.pop(file_id, None)

    def remove_retry(self, file_id: int) -> None:
        """Снять отложенную разовую задачу повтора, если она есть."""
        try:
            self._scheduler.remove_job(self._retry_job_id(file_id))
        except Exception:
            pass

    def _log_synthetic(self, file_id: int, path, error: str) -> RunRecord:
        """
        Записать в журнал «искусственное» событие (отложено / исчерпано).

        Лёгкий путь без изменения схемы БД: обычная запись run_log с
        success=0 и понятным текстом; started_at == finished_at (0 c).
        Результат уходит в UI тем же колбэком, что и обычные обновления.
        """
        now = datetime.now()
        synthetic = excel_refresher.RefreshResult(Path(path), False, now, now, error)
        record = self._storage.log_run(synthetic, file_id=file_id)
        self._publish(record)
        return record

    def _schedule_retry(self, file_id: int, run_at: datetime) -> None:
        """Поставить разовую задачу APScheduler на повтор (DateTrigger)."""
        self._scheduler.add_job(
            self._execute,
            trigger=DateTrigger(run_date=run_at),
            args=[file_id],
            id=self._retry_job_id(file_id),
            name=f"retry:{file_id}",
            replace_existing=True,  # один отложенный повтор на файл, не плодим
            max_instances=1,
            coalesce=True,
            misfire_grace_time=self._misfire_grace,
        )

    def _handle_locked(self, wf: WatchedFile) -> RunRecord | None:
        """
        Файл занят сотрудником: отложить на defer_minutes, максимум
        max_deferrals раз; после исчерпания — записать ошибку.
        """
        file_id = wf.id
        with self._deferrals_guard:
            attempts = self._deferrals.get(file_id, 0) + 1
            self._deferrals[file_id] = attempts

        if attempts > self._max_deferrals:
            # Попытки исчерпаны — фиксируем ошибку, чистим счётчик и повтор.
            self._reset_deferrals(file_id)
            self.remove_retry(file_id)
            logger.warning("Файл id=%s занят, попытки исчерпаны (%s)",
                           file_id, self._max_deferrals)
            return self._log_synthetic(
                file_id, wf.path,
                f"файл занят сотрудником — попытки исчерпаны "
                f"({self._max_deferrals})",
            )

        run_at = datetime.now() + timedelta(minutes=self._defer_minutes)
        self._schedule_retry(file_id, run_at)
        logger.info("Файл id=%s занят — отложен до %s (попытка %s из %s)",
                    file_id, run_at.strftime("%H:%M"), attempts, self._max_deferrals)
        return self._log_synthetic(
            file_id, wf.path,
            f"файл занят — отложено до {run_at.strftime('%H:%M')} "
            f"(попытка {attempts} из {self._max_deferrals})",
        )

    # ----------------------------------------------------------------- #
    # Статус (функция №8)
    # ----------------------------------------------------------------- #
    def next_run_time(self, file_id: int) -> datetime | None:
        job = self._scheduler.get_job(self._job_id(file_id))
        return getattr(job, "next_run_time", None) if job else None

    def is_running(self, file_id: int) -> bool:
        with self._running_guard:
            return file_id in self._running

    def get_status(self) -> list[FileStatus]:
        """Снимок по всем файлам: последнее обновление, следующее, идёт ли сейчас."""
        out: list[FileStatus] = []
        for wf in self._storage.list_files():
            out.append(
                FileStatus(
                    file=wf,
                    last_run=self._storage.last_run(wf.id) if wf.id is not None else None,
                    next_run=self.next_run_time(wf.id) if (wf.enabled and wf.id) else None,
                    running=self.is_running(wf.id) if wf.id is not None else False,
                )
            )
        return out