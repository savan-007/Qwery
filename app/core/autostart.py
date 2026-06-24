"""
app/core/autostart.py

Автозапуск приложения вместе с Windows (функция ТЗ №4).

Способ — ключ реестра текущего пользователя:
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run

Почему HKCU, а не Startup-папка или HKLM:
    * не требует прав администратора (ТЗ: «не требовать прав администратора»);
    * запись привязана к пользователю и легко включается/выключается.

На не-Windows все функции — безопасные заглушки (is_supported() == False),
чтобы модуль импортировался и приложение работало где угодно при разработке.

Команда запуска подбирается автоматически:
    * собранный .exe (PyInstaller, Этап 7) → запускаем сам .exe;
    * разработка → pythonw.exe main.py (pythonw — чтобы не было окна консоли).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = config.APP_SLUG  # "ExcelQueryScheduler"

try:
    import winreg

    _SUPPORTED = sys.platform == "win32"
except Exception:  # winreg есть только на Windows
    winreg = None
    _SUPPORTED = False


def is_supported() -> bool:
    """Доступен ли автозапуск (мы на Windows и есть доступ к реестру)."""
    return _SUPPORTED


def _run_command() -> str:
    """Команда, которую Windows выполнит при входе пользователя."""
    if getattr(sys, "frozen", False):
        # Собранный .exe — запускаем его напрямую.
        return f'"{sys.executable}"'
    # Режим разработки: pythonw.exe (без консоли) + main.py.
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else exe
    main_py = config.BASE_DIR / "main.py"
    return f'"{runner}" "{main_py}"'


def is_enabled() -> bool:
    if not _SUPPORTED:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable() -> bool:
    """Включить автозапуск (перезаписывает путь — на случай переустановки)."""
    if not _SUPPORTED:
        logger.info("Автозапуск недоступен на этой ОС — пропуск")
        return False
    try:
        command = _run_command()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, command)
        logger.info("Автозапуск включён: %s", command)
        return True
    except OSError as exc:
        logger.error("Не удалось включить автозапуск: %s", exc)
        return False


def disable() -> bool:
    if not _SUPPORTED:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
        logger.info("Автозапуск выключен")
        return True
    except FileNotFoundError:
        return True  # записи и так нет — считаем успехом
    except OSError as exc:
        logger.error("Не удалось выключить автозапуск: %s", exc)
        return False


def apply(enabled: bool) -> bool:
    """Привести реестр в соответствие с настройкой."""
    return enable() if enabled else disable()