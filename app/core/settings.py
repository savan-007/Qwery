"""
app/core/settings.py

Пользовательские настройки в JSON (config.SETTINGS_PATH).

Значения по умолчанию берутся из config.DEFAULTS:
    start_with_windows, minimize_to_tray, show_notifications, analytics_enabled.

Файл хранится в %LOCALAPPDATA%\\ExcelQueryScheduler и переживает обновление .exe.
При повреждённом/отсутствующем файле молча используются значения по умолчанию.
"""

from __future__ import annotations

import json
import logging
import threading

import config

logger = logging.getLogger(__name__)


class Settings:
    """Простое хранилище настроек «ключ-значение» поверх JSON."""

    def __init__(self, path=None, defaults: dict | None = None) -> None:
        self._path = path if path is not None else config.SETTINGS_PATH
        self._defaults = dict(defaults if defaults is not None else config.DEFAULTS)
        self._lock = threading.Lock()
        self._data = dict(self._defaults)
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data.update(loaded)
                    logger.info("Настройки загружены: %s", self._path)
        except Exception as exc:
            logger.warning("Не удалось прочитать настройки (%s) — значения по умолчанию", exc)

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, self._defaults.get(key, default))

    def set(self, key: str, value, *, save: bool = True) -> None:
        with self._lock:
            self._data[key] = value
            if save:
                self._save_locked()

    def update(self, values: dict, *, save: bool = True) -> None:
        with self._lock:
            self._data.update(values)
            if save:
                self._save_locked()

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("Не удалось сохранить настройки: %s", exc)

    def save(self) -> None:
        with self._lock:
            self._save_locked()