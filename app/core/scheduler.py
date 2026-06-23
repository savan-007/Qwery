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
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core import excel_refresher
from app.core.storage import RunRecord, Schedule, Storage, WatchedFile

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._storage = storage
        self._refresh_func = refresh_func or excel_refresher.refresh_workbook
        self._on_run_complete = on_run_complete
        self._timeout_sec = timeout_sec
        self._misfire_grace = misfire_grace_sec

        self._scheduler = BackgroundScheduler()

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

            with self._running_guard:
                self._running.add(file_id)
            try:
                result = self._refresh_func(wf.path, timeout_sec=self._timeout_sec)
            finally:
                with self._running_guard:
                    self._running.discard(file_id)

            record = self._storage.log_run(result, file_id=file_id)
            logger.info(
                "Обновление id=%s: %s",
                file_id,
                "OK" if record.success else f"ОШИБКА: {record.error}",
            )

            if self._on_run_complete is not None:
                try:
                    self._on_run_complete(record)
                except Exception:
                    logger.exception("Ошибка в колбэке on_run_complete")
            return record
        finally:
            lock.release()

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