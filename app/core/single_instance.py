"""
app/core/single_instance.py

Защита от запуска второго экземпляра приложения (подготовка к релизу).

Зачем: автозапуск вместе с Windows + ручной запуск ярлыка дадут два процесса,
и оба начнут обновлять одни и те же файлы по расписанию — обновления
задвоятся. Реализация на QSharedMemory: первый экземпляр создаёт сегмент
общей памяти с уникальным ключом, второй обнаруживает, что сегмент уже
существует, и тихо завершает работу.

Только PyQt6, без сторонних зависимостей, не требует прав администратора.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QSharedMemory

import config

logger = logging.getLogger(__name__)


class SingleInstance:
    """
    Гарантирует, что одновременно работает только один экземпляр.

    Использование:
        guard = SingleInstance()
        if guard.already_running():
            return 0
        # держим ссылку на guard живой до конца работы приложения

    Ссылку на объект нужно сохранять (например, в локальной переменной run())
    на всё время работы приложения: при сборке мусора QSharedMemory
    отсоединяет сегмент, и защита перестаёт действовать.
    """

    def __init__(self, key: str | None = None) -> None:
        self._key = key or f"{config.APP_SLUG}-single-instance"
        self._shared = QSharedMemory(self._key)
        self._is_running = False
        self._attach_or_create()

    def _attach_or_create(self) -> None:
        # Если сегмент уже существует — экземпляр уже запущен.
        if self._shared.attach():
            self._is_running = True
            self._shared.detach()
            return
        # Пытаемся создать сегмент (1 байта достаточно как флаг присутствия).
        if self._shared.create(1):
            self._is_running = False
            return
        # Сегмент мог остаться от аварийно завершённого процесса (актуально
        # для Unix; на Windows ОС чистит сама). Пробуем подцепиться и пересоздать.
        self._shared.attach()
        self._shared.detach()
        self._is_running = not self._shared.create(1)

    def already_running(self) -> bool:
        return self._is_running

    def release(self) -> None:
        """Явно отсоединить сегмент (необязательно — ОС освободит при выходе)."""
        if self._shared.isAttached():
            self._shared.detach()