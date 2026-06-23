"""
app/core/storage.py

Слой хранилища Этапа 2 — SQLite.

Хранит:
    * список отслеживаемых файлов с расписанием (функции ТЗ №1, №2);
    * журнал обновлений: дата, длительность, результат (функция №5);
    * данные для статуса в реальном времени (функция №8).

База — единственный источник правды. Память планировщика (APScheduler)
пересобирается из неё при каждом старте, поэтому список файлов и расписания
переживают перезапуск программы.

Потокобезопасность:
    к базе обращаются и UI-поток, и фоновые потоки планировщика, поэтому
    одно соединение открыто с check_same_thread=False, а все операции
    сериализуются через RLock. Включён WAL — для параллельного чтения.

Путь к базе берётся из config.DB_PATH (%LOCALAPPDATA%\\ExcelQueryScheduler).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# --- Допустимые значения расписания ---
_SCHEDULE_KINDS = ("interval", "daily", "weekly")
_VALID_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RUS_DAYS = {
    "mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
    "fri": "Пт", "sat": "Сб", "sun": "Вс",
}

_ISO = "seconds"  # точность сохраняемых временных меток


# --------------------------------------------------------------------------- #
# Доменные модели
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Schedule:
    """
    Расписание обновления одного файла (функция ТЗ №2).

    Виды:
        interval — каждые N минут          (interval_minutes)
        daily    — ежедневно в ЧЧ:ММ        (at_time)
        weekly   — по дням недели в ЧЧ:ММ   (at_time + days_of_week)
    """

    kind: str
    interval_minutes: int | None = None
    at_time: str | None = None          # "ЧЧ:ММ"
    days_of_week: tuple[str, ...] = ()  # подмножество _VALID_DAYS

    def __post_init__(self) -> None:
        if self.kind not in _SCHEDULE_KINDS:
            raise ValueError(f"неизвестный вид расписания: {self.kind!r}")

        if self.kind == "interval":
            if not self.interval_minutes or self.interval_minutes < 1:
                raise ValueError("interval_minutes должен быть целым >= 1")
            return

        # daily / weekly — нужно валидное время
        self._validate_time()
        if self.kind == "weekly":
            days = tuple(d.lower() for d in self.days_of_week)
            if not days:
                raise ValueError("для weekly нужен хотя бы один день недели")
            bad = [d for d in days if d not in _VALID_DAYS]
            if bad:
                raise ValueError(f"неверные дни недели: {bad}")
            self.days_of_week = days

    def _validate_time(self) -> None:
        if not self.at_time:
            raise ValueError("нужно время at_time в формате ЧЧ:ММ")
        try:
            h_str, m_str = self.at_time.split(":")
            h, m = int(h_str), int(m_str)
        except Exception:
            raise ValueError(f"неверный формат времени: {self.at_time!r}")
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"время вне диапазона: {self.at_time!r}")
        # нормализуем к ЧЧ:ММ
        self.at_time = f"{h:02d}:{m:02d}"

    # --- Удобные конструкторы ---
    @classmethod
    def every_minutes(cls, n: int) -> "Schedule":
        return cls("interval", interval_minutes=int(n))

    @classmethod
    def daily_at(cls, hhmm: str) -> "Schedule":
        return cls("daily", at_time=hhmm)

    @classmethod
    def weekly_at(cls, hhmm: str, days) -> "Schedule":
        return cls("weekly", at_time=hhmm, days_of_week=tuple(days))

    # --- Помощники ---
    def hour_minute(self) -> tuple[int, int]:
        h_str, m_str = (self.at_time or "00:00").split(":")
        return int(h_str), int(m_str)

    def describe(self) -> str:
        """Человекочитаемое описание для интерфейса (Этап 3)."""
        if self.kind == "interval":
            return f"каждые {self.interval_minutes} мин"
        if self.kind == "daily":
            return f"ежедневно в {self.at_time}"
        days = ", ".join(_RUS_DAYS[d] for d in self.days_of_week)
        return f"{days} в {self.at_time}"


@dataclass(slots=True)
class WatchedFile:
    """Отслеживаемый файл со своим расписанием (функция ТЗ №1)."""

    id: int | None
    path: Path
    schedule: Schedule
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class RunRecord:
    """Одна запись журнала обновлений (функция ТЗ №5)."""

    id: int | None
    file_id: int | None
    file_path: Path
    started_at: datetime
    finished_at: datetime
    success: bool
    error: str | None = None

    @property
    def duration_sec(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


# --------------------------------------------------------------------------- #
# Хранилище
# --------------------------------------------------------------------------- #
class Storage:
    """Доступ к SQLite-базе приложения."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else config.DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False — соединение используют разные потоки,
        # доступ сериализуем сами через self._lock.
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._create_schema()
        logger.info("Хранилище открыто: %s", self._db_path)

    def _configure(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA synchronous = NORMAL")

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watched_files (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    path             TEXT    NOT NULL UNIQUE,
                    enabled          INTEGER NOT NULL DEFAULT 1,
                    schedule_kind    TEXT    NOT NULL,
                    interval_minutes INTEGER,
                    at_time          TEXT,
                    days_of_week     TEXT,
                    created_at       TEXT    NOT NULL,
                    updated_at       TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id     INTEGER,
                    file_path   TEXT    NOT NULL,
                    started_at  TEXT    NOT NULL,
                    finished_at TEXT    NOT NULL,
                    success     INTEGER NOT NULL,
                    error       TEXT,
                    FOREIGN KEY (file_id) REFERENCES watched_files(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_run_log_file
                    ON run_log(file_id);
                CREATE INDEX IF NOT EXISTS idx_run_log_started
                    ON run_log(started_at DESC);
                """
            )
            self._conn.commit()

    # --- Преобразование строк БД в модели ---
    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> Schedule:
        kind = row["schedule_kind"]
        days = tuple((row["days_of_week"] or "").split(",")) if row["days_of_week"] else ()
        return Schedule(
            kind=kind,
            interval_minutes=row["interval_minutes"],
            at_time=row["at_time"],
            days_of_week=days,
        )

    @classmethod
    def _row_to_file(cls, row: sqlite3.Row) -> WatchedFile:
        return WatchedFile(
            id=row["id"],
            path=Path(row["path"]),
            schedule=cls._row_to_schedule(row),
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            file_id=row["file_id"],
            file_path=Path(row["file_path"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]),
            success=bool(row["success"]),
            error=row["error"],
        )

    # ----------------------------------------------------------------- #
    # Файлы (функции №1, №2)
    # ----------------------------------------------------------------- #
    def add_file(self, path: str | Path, schedule: Schedule,
                 enabled: bool = True) -> WatchedFile:
        """Добавить файл в список. Бросает ValueError, если путь уже есть."""
        norm = str(Path(path))
        now = datetime.now().isoformat(timespec=_ISO)
        days_csv = ",".join(schedule.days_of_week) if schedule.days_of_week else None
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO watched_files
                        (path, enabled, schedule_kind, interval_minutes,
                         at_time, days_of_week, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (norm, int(enabled), schedule.kind, schedule.interval_minutes,
                     schedule.at_time, days_csv, now, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"файл уже отслеживается: {norm}")
            file_id = cur.lastrowid
        logger.info("Добавлен файл id=%s: %s (%s)", file_id, norm, schedule.describe())
        return self.get_file(file_id)  # type: ignore[return-value]

    def update_schedule(self, file_id: int, schedule: Schedule) -> None:
        """Изменить расписание файла."""
        now = datetime.now().isoformat(timespec=_ISO)
        days_csv = ",".join(schedule.days_of_week) if schedule.days_of_week else None
        with self._lock:
            self._conn.execute(
                """
                UPDATE watched_files
                   SET schedule_kind = ?, interval_minutes = ?, at_time = ?,
                       days_of_week = ?, updated_at = ?
                 WHERE id = ?
                """,
                (schedule.kind, schedule.interval_minutes, schedule.at_time,
                 days_csv, now, file_id),
            )
            self._conn.commit()
        logger.info("Изменено расписание id=%s: %s", file_id, schedule.describe())

    def set_enabled(self, file_id: int, enabled: bool) -> None:
        """Включить/выключить обновление файла (без удаления из списка)."""
        now = datetime.now().isoformat(timespec=_ISO)
        with self._lock:
            self._conn.execute(
                "UPDATE watched_files SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), now, file_id),
            )
            self._conn.commit()
        logger.info("Файл id=%s: enabled=%s", file_id, bool(enabled))

    def remove_file(self, file_id: int) -> None:
        """Убрать файл из списка. Записи журнала сохраняются (file_id → NULL)."""
        with self._lock:
            self._conn.execute("DELETE FROM watched_files WHERE id = ?", (file_id,))
            self._conn.commit()
        logger.info("Удалён файл id=%s", file_id)

    def get_file(self, file_id: int) -> WatchedFile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watched_files WHERE id = ?", (file_id,)
            ).fetchone()
        return self._row_to_file(row) if row else None

    def get_file_by_path(self, path: str | Path) -> WatchedFile | None:
        norm = str(Path(path))
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watched_files WHERE path = ?", (norm,)
            ).fetchone()
        return self._row_to_file(row) if row else None

    def list_files(self) -> list[WatchedFile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM watched_files ORDER BY id"
            ).fetchall()
        return [self._row_to_file(r) for r in rows]

    def count_enabled(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM watched_files WHERE enabled = 1"
            ).fetchone()
        return int(row["n"])

    # ----------------------------------------------------------------- #
    # Журнал обновлений (функция №5)
    # ----------------------------------------------------------------- #
    def log_run(self, result, *, file_id: int | None = None) -> RunRecord:
        """
        Записать результат обновления в журнал.

        `result` — любой объект с атрибутами file_path, started_at,
        finished_at, success, error (например, RefreshResult из Этапа 1).
        Так хранилище не зависит от модуля excel_refresher.
        """
        path = str(Path(result.file_path))
        started = result.started_at.isoformat(timespec=_ISO)
        finished = result.finished_at.isoformat(timespec=_ISO)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO run_log
                    (file_id, file_path, started_at, finished_at, success, error)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (file_id, path, started, finished,
                 int(bool(result.success)), result.error),
            )
            self._conn.commit()
            run_id = cur.lastrowid
            row = self._conn.execute(
                "SELECT * FROM run_log WHERE id = ?", (run_id,)
            ).fetchone()
        return self._row_to_run(row)

    def list_runs(self, file_id: int | None = None, limit: int = 100) -> list[RunRecord]:
        """История обновлений (новые сверху). file_id=None — по всем файлам."""
        with self._lock:
            if file_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM run_log ORDER BY started_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM run_log WHERE file_id = ?
                    ORDER BY started_at DESC, id DESC LIMIT ?
                    """,
                    (file_id, limit),
                ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def last_run(self, file_id: int) -> RunRecord | None:
        runs = self.list_runs(file_id=file_id, limit=1)
        return runs[0] if runs else None

    def clear_log(self, file_id: int | None = None) -> int:
        """Очистить журнал (целиком или по одному файлу). Возвращает число строк."""
        with self._lock:
            if file_id is None:
                cur = self._conn.execute("DELETE FROM run_log")
            else:
                cur = self._conn.execute(
                    "DELETE FROM run_log WHERE file_id = ?", (file_id,)
                )
            self._conn.commit()
            deleted = cur.rowcount
        logger.info("Очистка журнала: удалено %s записей", deleted)
        return deleted

    # ----------------------------------------------------------------- #
    def close(self) -> None:
        with self._lock:
            self._conn.close()
        logger.info("Хранилище закрыто")

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()