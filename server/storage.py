"""
server/storage.py

Слой хранилища серверной аналитики (Этап 6). SQLite.

⚠️ Render free tier: диск эфемерный — при редеплое/перезапуске файл базы
сбрасывается, поэтому счётчики приблизительные (достаточно для MVP). Вся
работа с базой изолирована в этом модуле: чтобы перейти на внешний Postgres
(Supabase/Neon), достаточно переписать его, не трогая main.py.

Путь к базе берётся из переменной окружения ANALYTICS_DB (по умолчанию
analytics.db в рабочей папке). Доступ сериализуется через RLock, т.к. uvicorn
может обслуживать запросы из разных потоков.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("analytics.storage")

_DB_PATH = Path(os.environ.get("ANALYTICS_DB", "analytics.db"))
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init() -> None:
    """Открыть соединение и создать схему. Вызывается при старте сервера."""
    global _conn
    with _lock:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS installs (
                install_id TEXT    PRIMARY KEY,
                first_seen TEXT    NOT NULL,
                last_seen  TEXT    NOT NULL,
                version    TEXT,
                os         TEXT,
                ping_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        _conn.commit()
    logger.info("Хранилище аналитики готово: %s", _DB_PATH)


def record_ping(install_id: str, version: str | None, os_name: str | None) -> None:
    """Учесть один ping: новая установка либо обновление существующей."""
    if not install_id:
        return
    now = _now()
    with _lock:
        assert _conn is not None, "storage.init() не вызван"
        exists = _conn.execute(
            "SELECT 1 FROM installs WHERE install_id = ?", (install_id,)
        ).fetchone() is not None
        if exists:
            _conn.execute(
                """
                UPDATE installs
                   SET last_seen = ?, version = ?, os = ?, ping_count = ping_count + 1
                 WHERE install_id = ?
                """,
                (now, version, os_name, install_id),
            )
        else:
            _conn.execute(
                """
                INSERT INTO installs
                    (install_id, first_seen, last_seen, version, os, ping_count)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (install_id, now, now, version, os_name),
            )
        _conn.commit()


def stats() -> dict:
    """Агрегированная анонимная статистика: уникальные установки и всего ping'ов."""
    with _lock:
        assert _conn is not None, "storage.init() не вызван"
        installs = _conn.execute("SELECT COUNT(*) AS n FROM installs").fetchone()["n"]
        pings = _conn.execute(
            "SELECT COALESCE(SUM(ping_count), 0) AS n FROM installs"
        ).fetchone()["n"]
        by_version = _conn.execute(
            """
            SELECT version, COUNT(*) AS n
              FROM installs
             GROUP BY version
             ORDER BY n DESC
            """
        ).fetchall()
    return {
        "installs": int(installs),
        "pings": int(pings),
        "by_version": {row["version"] or "unknown": int(row["n"]) for row in by_version},
    }