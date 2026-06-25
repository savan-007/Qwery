"""
Глобальная конфигурация Excel Query Scheduler.

Здесь хранятся константы приложения и пути к пользовательским данным.
Пользовательские данные (база, логи, настройки) кладём в %LOCALAPPDATA%,
а НЕ в папку программы — так они переживают переустановку .exe и не требуют
прав администратора.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Идентификация приложения ---
APP_NAME = "Excel Query Scheduler"
APP_SLUG = "ExcelQueryScheduler"        # имя без пробелов: для путей и автозапуска
APP_VERSION = "0.1.0"                    # MVP-разработка
ORG_NAME = "savan-007"


def _user_data_dir() -> Path:
    """Папка для пользовательских данных в зависимости от ОС."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_SLUG
    # запасной вариант для разработки на других ОС
    return Path.home() / f".{APP_SLUG.lower()}"


# --- Пути ---
BASE_DIR = Path(__file__).resolve().parent          # корень проекта
ASSETS_DIR = BASE_DIR / "assets"                     # иконки и ресурсы
DATA_DIR = _user_data_dir()                          # пользовательские данные
DB_PATH = DATA_DIR / "scheduler.db"                  # SQLite: файлы и расписания
SETTINGS_PATH = DATA_DIR / "settings.json"           # пользовательские настройки
LOG_PATH = DATA_DIR / "app.log"                      # журнал обновлений

# Создаём папку данных при первом импорте, чтобы остальные модули не дублировали проверку.
DATA_DIR.mkdir(parents=True, exist_ok=True)


# --- Аналитика и проверка обновлений (Этап 6) ---
# Базовый URL сервера аналитики (FastAPI на Render). Без завершающего слэша.
# В разработке можно временно указать "http://127.0.0.1:8000".
ANALYTICS_URL = "https://qwery-analytics-onrender-com.onrender.com"
ANALYTICS_TIMEOUT_SEC = 5                            # таймаут сетевых запросов, сек
INSTALL_ID_PATH = DATA_DIR / "install_id"            # анонимный UUID установки (один раз)

# --- Настройки по умолчанию ---
DEFAULTS = {
    "start_with_windows": True,    # автозапуск с системой
    "minimize_to_tray": True,      # сворачивать в трей вместо закрытия
    "show_notifications": True,    # уведомления Windows
    "analytics_enabled": True,     # анонимная аналитика (можно отключить)
}