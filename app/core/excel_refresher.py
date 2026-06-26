"""
app/core/excel_refresher.py

Ядро Этапа 1 — «тихое» обновление Power Query через COM-автоматизацию Excel.

Что делает модуль:
    скрыто открыть книгу .xlsx → обновить все запросы Power Query →
    дождаться завершения (в т.ч. асинхронных запросов) → сохранить → закрыть.

Платформа:
    только Windows + установленный Microsoft Excel (pywin32 / COM).
    На других ОС модуль ИМПОРТИРУЕТСЯ без ошибок, но обновление недоступно
    (is_excel_available() вернёт False) — это удобно для разработки и тестов.

Потоки и COM:
    refresh_workbook() сам инициализирует COM в текущем потоке
    (CoInitialize / CoUninitialize), поэтому его безопасно вызывать из
    фонового потока планировщика (APScheduler, Этап 2).

Защита от зависания (требование ТЗ, п.7 «таймаут — не зависать»):
    если источник данных недоступен и обновление «висит», по таймауту
    процесс Excel принудительно завершается (taskkill), а зависший
    COM-вызов разблокируется с ошибкой и фиксируется как неудача.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# --- Доступность pywin32 (есть только на Windows) ---
try:
    import pythoncom
    import pywintypes
    import win32com.client as win32
    import win32process

    _PYWIN32_AVAILABLE = True
except Exception:  # ImportError на не-Windows
    _PYWIN32_AVAILABLE = False


# --- Константы Excel COM ---
# Используем целочисленные литералы намеренно: при late binding (DispatchEx)
# win32com.client.constants недоступны без EnsureDispatch/makepy.
_XL_DONE = 0                 # XlCalculationState.xlDone — расчёт/обновление завершены
_XL_UPDATE_LINKS_NEVER = 0   # Workbooks.Open(UpdateLinks=0) — не трогать внешние связи

# Расширения, которые считаем книгами Excel
_EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xlsb", ".xls"}

# Пауза между опросами состояния расчёта
_POLL_INTERVAL_SEC = 0.25


@dataclass(slots=True)
class RefreshResult:
    """Результат одного обновления — то, что позже ляжет в журнал (Этапы 2–3)."""

    file_path: Path
    success: bool
    started_at: datetime
    finished_at: datetime
    error: str | None = None

    @property
    def duration_sec(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def __str__(self) -> str:
        status = "OK" if self.success else "ОШИБКА"
        line = f"[{status}] {self.file_path.name} — {self.duration_sec:.1f} c"
        if self.error:
            line += f" — {self.error}"
        return line


def is_excel_available() -> bool:
    """Проверить, что мы на Windows и Microsoft Excel установлен (COM доступен)."""
    if not _PYWIN32_AVAILABLE or sys.platform != "win32":
        return False

    pythoncom.CoInitialize()
    excel = None
    try:
        excel = win32.DispatchEx("Excel.Application")
        return True
    except Exception as exc:
        logger.warning("Excel недоступен через COM: %s", exc)
        return False
    finally:
        try:
            if excel is not None:
                excel.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()

def is_file_locked(path: str | Path) -> bool:
    """
    Проверить, занят ли файл другим процессом на запись (например, открыт
    сотрудником в Excel).

    Идея: книгу .xlsx, открытую в Excel, ОС держит с запретом записи для
    других процессов. Пробуем открыть файл на дозапись ("r+b") — если Excel
    держит его открытым, получим PermissionError. Содержимое не меняется
    (ничего не пишем).

    Замечания:
        * метод проверяет именно возможность ЗАПИСИ — а нам как раз нужно
          сохранить книгу после обновления, так что предикат совпадает с целью;
        * файл с атрибутом «только чтение» тоже даст PermissionError — это
          ложная «занятость», но и обновить-сохранить такой файл всё равно
          нельзя, так что поведение безопасное;
        * несуществующий файл «занятым» не считаем (это другая ошибка).
    """
    p = Path(path)
    try:
        if not p.exists():
            return False
        with open(p, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        # Прочие ошибки (нет прав на путь и т.п.) — пусть разбирается
        # основной путь обновления, не помечаем как «занято».
        return False


def _excel_pid(excel) -> int | None:
    """PID процесса Excel по его главному окну (для аварийного завершения)."""
    try:
        _, pid = win32process.GetWindowThreadProcessId(excel.Hwnd)
        return int(pid) or None
    except Exception:
        return None


def _kill_pid(pid: int) -> None:
    """Жёстко завершить процесс Excel по PID (вместе с дочерними)."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        logger.warning("Процесс Excel PID=%s принудительно завершён по таймауту", pid)
    except Exception as exc:
        logger.error("Не удалось завершить Excel PID=%s: %s", pid, exc)


def _disable_background_refresh(workbook) -> None:
    """
    Отключить фоновое обновление у всех подключений книги, чтобы RefreshAll
    выполнялся синхронно. Набор свойств у разных типов подключений разный,
    поэтому каждое обращение защищаем try/except — это норма.
    """
    try:
        for conn in workbook.Connections:
            for attr in ("OLEDBConnection", "ODBCConnection"):
                try:
                    getattr(conn, attr).BackgroundQuery = False
                except Exception:
                    pass  # у этого подключения такого свойства нет
    except Exception:
        # У книги может вовсе не быть коллекции Connections — это не ошибка.
        pass


def _wait_until_done(excel, deadline: float) -> None:
    """Дождаться завершения асинхронных запросов и пересчёта (с дедлайном)."""
    try:
        excel.CalculateUntilAsyncQueriesDone()
    except Exception:
        pass  # метода может не быть в старых версиях — опираемся на опрос ниже

    while True:
        try:
            state = excel.CalculationState
        except Exception:
            # Книга/Excel уже недоступны — выходим, ошибку обработает вызывающий код.
            return
        if state == _XL_DONE:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("обновление не завершилось в отведённое время")
        time.sleep(_POLL_INTERVAL_SEC)


def _format_error(exc: Exception) -> str:
    """Привести исключение к короткому понятному тексту для журнала."""
    if isinstance(exc, TimeoutError):
        return f"таймаут: {exc}"
    if _PYWIN32_AVAILABLE and isinstance(exc, pywintypes.com_error):
        # com_error.excepinfo[2] часто содержит человекочитаемое описание
        try:
            desc = exc.excepinfo[2]
            if desc:
                return f"COM: {str(desc).strip()}"
        except Exception:
            pass
        return f"COM-ошибка: {exc.args}"
    return f"{type(exc).__name__}: {exc}"


def refresh_workbook(
    file_path: str | Path,
    *,
    timeout_sec: int = 300,
    save: bool = True,
    keep_visible: bool = False,
) -> RefreshResult:
    """
    Скрыто обновить все запросы Power Query в одной книге Excel.

    Args:
        file_path: путь к книге (желательно абсолютный).
        timeout_sec: предельное время на обновление; по истечении процесс
            Excel принудительно завершается, результат — ошибка таймаута.
        save: сохранять ли книгу после обновления (False — «прогон» без записи).
        keep_visible: показать окно Excel (только для отладки на Этапе 1).

    Returns:
        RefreshResult с флагом успеха, длительностью и текстом ошибки.
    """
    started = datetime.now()
    path = Path(file_path).expanduser()

    # --- Предварительные проверки (без запуска Excel) ---
    if not _PYWIN32_AVAILABLE or sys.platform != "win32":
        return RefreshResult(
            path, False, started, datetime.now(),
            "обновление доступно только на Windows с установленным Excel и pywin32",
        )
    if not path.exists():
        return RefreshResult(path, False, started, datetime.now(), "файл не найден")
    if not path.is_file():
        return RefreshResult(path, False, started, datetime.now(), "путь не является файлом")
    if path.suffix.lower() not in _EXCEL_SUFFIXES:
        logger.warning("Необычное расширение %s — пробуем открыть как книгу Excel", path.suffix)

    abs_path = str(path.resolve())
    logger.info("Старт обновления: %s (таймаут %s c)", abs_path, timeout_sec)

    pythoncom.CoInitialize()
    excel = None
    workbook = None
    watchdog: threading.Timer | None = None
    error: str | None = None

    try:
        excel = win32.DispatchEx("Excel.Application")
        # Тихий режим: ни окон, ни диалогов, ни автозапуска макросов.
        excel.Visible = bool(keep_visible)
        excel.DisplayAlerts = False
        excel.ScreenUpdating = False
        excel.EnableEvents = False
        excel.AskToUpdateLinks = False
        excel.AlertBeforeOverwriting = False

        # Сторож от зависания: делает ТОЛЬКО OS-level taskkill, COM-объекты не
        # трогает → потокобезопасен. При таймауте убивает процесс, и зависший
        # COM-вызов в этом потоке разблокируется с ошибкой.
        pid = _excel_pid(excel)
        if pid is not None:
            watchdog = threading.Timer(timeout_sec, _kill_pid, args=(pid,))
            watchdog.daemon = True
            watchdog.start()

        workbook = excel.Workbooks.Open(
            abs_path,
            UpdateLinks=_XL_UPDATE_LINKS_NEVER,
            ReadOnly=False,
            IgnoreReadOnlyRecommended=True,
        )

        _disable_background_refresh(workbook)
        workbook.RefreshAll()

        deadline = time.monotonic() + timeout_sec
        _wait_until_done(excel, deadline)

        if save:
            workbook.Save()

        logger.info("Обновление завершено: %s", path.name)

    except Exception as exc:
        # Сюда приходят pywintypes.com_error и наш TimeoutError.
        error = _format_error(exc)
        logger.error("Ошибка обновления %s: %s", path.name, error)

    finally:
        if watchdog is not None:
            watchdog.cancel()
        # Аккуратное закрытие. Если Excel уже убит сторожем — обёртки молча
        # проглотят ошибки, процесса всё равно нет.
        try:
            if workbook is not None:
                workbook.Close(SaveChanges=False)
        except Exception:
            pass
        try:
            if excel is not None:
                excel.Quit()
        except Exception:
            pass
        workbook = None
        excel = None
        pythoncom.CoUninitialize()

    return RefreshResult(path, error is None, started, datetime.now(), error)


def refresh_many(
    paths,
    *,
    timeout_sec: int = 300,
    save: bool = True,
) -> list[RefreshResult]:
    """Обновить несколько книг подряд (каждая — в своём чистом процессе Excel)."""
    return [
        refresh_workbook(p, timeout_sec=timeout_sec, save=save)
        for p in paths
    ]


# --------------------------------------------------------------------------- #
# CLI для ручной проверки на Windows (Этап 1):
#   python -m app.core.excel_refresher --check
#   python -m app.core.excel_refresher "D:\reports\sales.xlsx"
#   python -m app.core.excel_refresher "D:\a.xlsx" "D:\b.xlsx" --timeout 120
#   python -m app.core.excel_refresher "D:\a.xlsx" --keep-visible --no-save
# --------------------------------------------------------------------------- #
def _setup_cli_logging() -> None:
    config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
        ],
    )


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Тихое обновление Power Query через COM (Этап 1).",
    )
    parser.add_argument("files", nargs="*", help="пути к файлам .xlsx")
    parser.add_argument("--timeout", type=int, default=300, help="таймаут на файл, сек")
    parser.add_argument("--no-save", action="store_true", help="не сохранять после обновления")
    parser.add_argument("--keep-visible", action="store_true", help="показать окно Excel (отладка)")
    parser.add_argument("--check", action="store_true", help="только проверить доступность Excel")
    args = parser.parse_args(argv)

    _setup_cli_logging()

    if args.check:
        ok = is_excel_available()
        print("Excel доступен через COM." if ok
              else "Excel НЕ доступен (нужен Windows + установленный Excel).")
        return 0 if ok else 1

    if not args.files:
        ok = is_excel_available()
        print("Не указаны файлы. Пример:")
        print(r'  python -m app.core.excel_refresher "D:\reports\sales.xlsx"')
        print("Excel доступен." if ok else "Excel недоступен.")
        return 0 if ok else 1

    if args.keep_visible:
        # В видимом режиме обновляем по одному файлу — иначе несколько окон.
        results = [
            refresh_workbook(p, timeout_sec=args.timeout,
                             save=not args.no_save, keep_visible=True)
            for p in args.files
        ]
    else:
        results = refresh_many(args.files, timeout_sec=args.timeout, save=not args.no_save)

    print("\nИтог:")
    for r in results:
        print(" ", r)

    failed = sum(1 for r in results if not r.success)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))