"""
app/core/analytics.py

Клиент аналитики и проверки обновлений (Этап 6).

Что делает модуль:
    * при запуске приложения отправляет один анонимный ping на сервер
      (install_id + версия + ОС) — ТОЛЬКО если analytics_enabled=True;
    * по ответу ping (или отдельным запросом) узнаёт последнюю версию и,
      если она новее текущей, ненавязчиво сообщает об этом через колбэк;
    * install_id — случайный UUID, генерируется один раз и хранится локально
      в %LOCALAPPDATA%\\ExcelQueryScheduler\\install_id. Никаких MAC-адресов,
      имён машины и прочих ПД — честная анонимность.

Принципы (по ТЗ):
    * неблокирующе: работа идёт в фоновом daemon-потоке с таймаутом, поэтому
      cold start «спящего» сервера Render пользователь не замечает;
    * тихие ошибки: любая сетевая проблема логируется на уровне debug и не
      влияет на запуск приложения;
    * без новых зависимостей: HTTP через стандартный urllib.request.

Развязка с UI:
    модуль ничего не знает про PyQt. О новой версии он сообщает через колбэк
    on_update_available(latest_version). Главное окно передаёт сюда
    SignalBridge.update_available.emit, и сигнал безопасно доезжает до
    главного потока Qt.
"""

from __future__ import annotations

import json
import logging
import platform
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

import config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Идентификатор установки и сведения об ОС
# --------------------------------------------------------------------------- #
def get_or_create_install_id(path: str | Path | None = None) -> str:
    """
    Прочитать анонимный install_id из файла или создать новый при первом запуске.

    При любой ошибке чтения/записи возвращается валидный UUID (в худшем случае
    он просто не сохранится и в следующий раз сгенерируется заново) — модуль
    не должен мешать запуску приложения.
    """
    p = Path(path) if path is not None else config.INSTALL_ID_PATH
    try:
        if p.exists():
            value = p.read_text(encoding="utf-8").strip()
            if value:
                return value
    except Exception as exc:
        logger.debug("Не удалось прочитать install_id (%s) — создаём новый", exc)

    new_id = str(uuid.uuid4())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_id, encoding="utf-8")
        logger.info("Создан новый install_id: %s", new_id)
    except Exception as exc:
        logger.debug("Не удалось сохранить install_id (%s) — используем в памяти", exc)
    return new_id


def current_os() -> str:
    """Короткая строка с ОС без персональных данных, например 'Windows 11'."""
    try:
        return f"{platform.system()} {platform.release()}".strip() or "unknown"
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- #
# Сравнение версий
# --------------------------------------------------------------------------- #
def _parse_version(v: str) -> tuple[int, ...]:
    """'1.2.10' -> (1, 2, 10). Нечисловые хвосты (rc, beta) отбрасываются."""
    parts: list[int] = []
    for chunk in str(v).strip().split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """True, если latest строго новее current. При ошибке разбора — False."""
    try:
        return _parse_version(latest) > _parse_version(current)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Клиент
# --------------------------------------------------------------------------- #
class AnalyticsClient:
    """Анонимная аналитика и проверка обновлений. Не зависит от PyQt."""

    def __init__(
        self,
        settings,
        *,
        base_url: str | None = None,
        timeout: int | None = None,
        current_version: str | None = None,
        on_update_available: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = (base_url or config.ANALYTICS_URL).rstrip("/")
        self._timeout = timeout if timeout is not None else config.ANALYTICS_TIMEOUT_SEC
        self._current_version = current_version or config.APP_VERSION
        self._on_update_available = on_update_available
        self._install_id = get_or_create_install_id()

    # --- Гейт по настройке ---
    def enabled(self) -> bool:
        return bool(self._settings.get("analytics_enabled"))

    # --- Запуск в фоне ---
    def start(self) -> threading.Thread | None:
        """
        Запустить ping + проверку обновлений в фоновом потоке.

        Если аналитика отключена настройкой — ничего не делаем и не ходим в сеть.
        Возвращает запущенный поток (или None, если отключено).
        """
        if not self.enabled():
            logger.info("Аналитика отключена настройкой — сетевых запросов нет")
            return None
        thread = threading.Thread(target=self._run, daemon=True, name="analytics")
        thread.start()
        return thread

    def _run(self) -> None:
        """Тело фонового потока: ping → определить последнюю версию → колбэк."""
        data = self.ping()

        latest = data.get("latest_version") if data else None
        if not latest:
            # ping не прошёл или сервер не вернул версию — пробуем отдельный запрос
            latest = self.check_update()

        if latest and is_newer(latest, self._current_version):
            logger.info(
                "Доступна новая версия: %s (текущая %s)",
                latest, self._current_version,
            )
            if self._on_update_available is not None:
                try:
                    self._on_update_available(latest)
                except Exception:
                    logger.exception("Ошибка в колбэке on_update_available")

    # --- Низкоуровневые HTTP-помощники ---
    def _post_json(self, path: str, payload: dict) -> dict:
        url = f"{self._base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def _get_json(self, path: str) -> dict:
        url = f"{self._base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    # --- Публичные операции ---
    def ping(self) -> dict | None:
        """
        Отправить анонимный ping. Возвращает разобранный ответ сервера
        (обычно содержит latest_version) или None при любой сетевой ошибке.
        """
        payload = {
            "install_id": self._install_id,
            "version": self._current_version,
            "os": current_os(),
        }
        try:
            data = self._post_json("/api/ping", payload)
            logger.debug("ping ok: %s", data)
            return data
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # URLError — сеть/таймаут/HTTP; ValueError — битый JSON. Это норма.
            logger.debug("ping не удался (это нормально): %s", exc)
            return None

    def check_update(self) -> str | None:
        """Запросить последнюю версию отдельным GET. None при ошибке."""
        try:
            data = self._get_json("/api/version")
            return data.get("latest_version")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.debug("Проверка версии не удалась: %s", exc)
            return None


# --------------------------------------------------------------------------- #
# Ручная проверка из консоли:
#   python -m app.core.analytics
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    class _DummySettings:
        def get(self, key, default=None):
            return True if key == "analytics_enabled" else default

    client = AnalyticsClient(
        _DummySettings(),
        on_update_available=lambda v: print(f"Доступна новая версия: {v}"),
    )
    print("install_id:", client._install_id)
    print("os:", current_os())
    print("ping ->", client.ping())
    print("latest ->", client.check_update())