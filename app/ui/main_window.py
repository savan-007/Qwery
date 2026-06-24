"""
app/ui/main_window.py

Главное окно приложения (Этапы 3–4).

Этап 3:
    список файлов с расписанием (№1, №2), статус (№8), журнал (№5).
Этап 4:
    системный трей (№4), уведомления Windows (№6), автозапуск, настройки.

Трей реализован на QSystemTrayIcon (встроен в PyQt6) — в общем event-loop Qt,
без отдельного потока pystray. Закрытие окна по настройке сворачивает программу
в трей; реальный выход — пункт «Выход» в меню трея.

Потоки:
    планировщик зовёт on_run_complete из фонового потока, поэтому результат
    идёт в интерфейс через сигнал Qt (SignalBridge) — слот в главном потоке.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import config
from app.core import autostart
from app.core.scheduler import RefreshScheduler
from app.core.settings import Settings
from app.core.storage import RunRecord, Storage
from app.ui.banner import WEBENGINE_AVAILABLE, BannerWidget
from app.ui.schedule_dialog import ScheduleDialog
from app.ui.settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)

_OK_COLOR = QColor("#1a7f37")
_ERR_COLOR = QColor("#cf222e")
_FILE_FILTER = "Книги Excel (*.xlsx *.xlsm *.xlsb);;Все файлы (*)"

_C_NAME, _C_SCHED, _C_STATE, _C_LAST, _C_NEXT = range(5)
_J_TIME, _J_FILE, _J_DUR, _J_RESULT = range(4)
_ID_ROLE = Qt.ItemDataRole.UserRole


class SignalBridge(QObject):
    """Колбэк планировщика → сигнал Qt (потокобезопасно)."""

    run_completed = pyqtSignal(object)  # RunRecord


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m %H:%M:%S")


def _fmt_next(dt: datetime) -> str:
    return dt.strftime("%d.%m %H:%M")


def _fmt_dur(sec: float) -> str:
    return f"{sec:.0f} c"


def _make_app_icon() -> QIcon:
    """Программная иконка (до появления .ico-ресурса на Этапе 7)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#2d6cdf"))
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPointSize(30)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "Q")
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, storage: Storage) -> None:
        super().__init__()
        self._storage = storage
        self._settings = Settings()
        self._quitting = False
        self._shut_done = False
        self._tray_hint_shown = False
        self._row_by_id: dict[int, int] = {}

        self._app_icon = _make_app_icon()
        self.setWindowIcon(self._app_icon)

        self._bridge = SignalBridge()
        self._bridge.run_completed.connect(self._on_run_completed)
        self._scheduler = RefreshScheduler(
            storage,
            on_run_complete=self._bridge.run_completed.emit,
        )

        self.setWindowTitle(f"{config.APP_NAME} v{config.APP_VERSION}")
        self.resize(900, 600)
        self._build_menu()
        self._build_ui()
        self._build_tray()

        # синхронизируем автозапуск с настройкой при старте
        try:
            autostart.apply(bool(self._settings.get("start_with_windows")))
        except Exception:
            logger.exception("Не удалось применить автозапуск при старте")

        self.rebuild_files_table()
        self.reload_journal()
        self._scheduler.start()
        self.update_status_strip()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ----------------------------------------------------------------- #
    # Построение интерфейса
    # ----------------------------------------------------------------- #
    def _icon(self, pixmap: QStyle.StandardPixmap) -> QIcon:
        return self.style().standardIcon(pixmap)

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("Программа")
        menu.addAction("Настройки…", self.on_settings)
        menu.addSeparator()
        menu.addAction("Свернуть в трей", self.hide)
        menu.addAction("Выход", self._quit)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        self.status_strip = QLabel()
        self.status_strip.setObjectName("statusStrip")
        self.status_strip.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self.status_strip)

        bar = QHBoxLayout()
        self.btn_add = QPushButton(" Добавить файл")
        self.btn_add.setIcon(self._icon(QStyle.StandardPixmap.SP_FileIcon))
        self.btn_edit = QPushButton(" Расписание")
        self.btn_edit.setIcon(self._icon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.btn_toggle = QPushButton(" Вкл/Выкл")
        self.btn_toggle.setIcon(self._icon(QStyle.StandardPixmap.SP_DialogYesButton))
        self.btn_run = QPushButton(" Обновить сейчас")
        self.btn_run.setIcon(self._icon(QStyle.StandardPixmap.SP_BrowserReload))
        self.btn_remove = QPushButton(" Удалить")
        self.btn_remove.setIcon(self._icon(QStyle.StandardPixmap.SP_TrashIcon))

        self.btn_add.clicked.connect(self.on_add_file)
        self.btn_edit.clicked.connect(self.on_edit_schedule)
        self.btn_toggle.clicked.connect(self.on_toggle_enabled)
        self.btn_run.clicked.connect(self.on_run_now)
        self.btn_remove.clicked.connect(self.on_remove_file)

        for b in (self.btn_add, self.btn_edit, self.btn_toggle, self.btn_run, self.btn_remove):
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.files_table = self._make_table(
            ["Файл", "Расписание", "Состояние", "Последнее обновление", "Следующее"]
        )
        self.files_table.itemSelectionChanged.connect(self._update_buttons)
        self.files_table.doubleClicked.connect(self.on_edit_schedule)
        splitter.addWidget(self._titled("Файлы", self.files_table))

        self.journal_table = self._make_table(
            ["Время", "Файл", "Длительность", "Результат"]
        )
        journal_box, journal_layout = self._titled_with_layout("Журнал обновлений")
        self.btn_clear_log = QPushButton("Очистить журнал")
        self.btn_clear_log.clicked.connect(self.on_clear_log)
        hdr = QHBoxLayout()
        hdr.addStretch(1)
        hdr.addWidget(self.btn_clear_log)
        journal_layout.addLayout(hdr)
        journal_layout.addWidget(self.journal_table)
        splitter.addWidget(journal_box)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        # Рекламный баннер снизу (функция №7): фиксированная высота, во всю ширину
        self.banner = BannerWidget(self)
        root.addWidget(self.banner)

        self.setCentralWidget(central)
        self.setStyleSheet(_STYLE)
        self._update_buttons()

    def _build_tray(self) -> None:
        self._tray = QSystemTrayIcon(self._app_icon, self)
        self._tray.setToolTip(config.APP_NAME)

        menu = QMenu()
        menu.addAction("Открыть", self._restore)
        menu.addAction("Обновить все сейчас", self.on_run_all)
        menu.addSeparator()
        menu.addAction("Настройки…", self.on_settings)
        menu.addSeparator()
        menu.addAction("Выход", self._quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)

        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()
        else:
            logger.warning("Системный трей недоступен — закрытие будет завершать программу")

    def _make_table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(headers)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        return table

    @staticmethod
    def _titled(title: str, inner: QWidget) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(title)
        lbl.setObjectName("sectionTitle")
        lay.addWidget(lbl)
        lay.addWidget(inner)
        return box

    @staticmethod
    def _titled_with_layout(title: str):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(title)
        lbl.setObjectName("sectionTitle")
        lay.addWidget(lbl)
        return box, lay

    # ----------------------------------------------------------------- #
    # Таблица файлов
    # ----------------------------------------------------------------- #
    def rebuild_files_table(self) -> None:
        statuses = self._scheduler.get_status()
        self.files_table.setRowCount(len(statuses))
        self._row_by_id.clear()
        for row, st in enumerate(statuses):
            fid = st.file.id
            self._row_by_id[fid] = row
            name_item = QTableWidgetItem(st.file.path.name)
            name_item.setData(_ID_ROLE, fid)
            name_item.setToolTip(str(st.file.path))
            self.files_table.setItem(row, _C_NAME, name_item)
            self.files_table.setItem(row, _C_SCHED, QTableWidgetItem(st.file.schedule.describe()))
            self._fill_dynamic(row, st)
        self._update_buttons()

    def _fill_dynamic(self, row: int, st) -> None:
        if st.running:
            state_text = "обновляется…"
        elif st.file.enabled:
            state_text = "включён"
        else:
            state_text = "выключен"
        state_item = QTableWidgetItem(state_text)
        if not st.file.enabled:
            state_item.setForeground(QBrush(QColor("#8a8a8a")))
        self.files_table.setItem(row, _C_STATE, state_item)

        last = st.last_run
        if last is None:
            last_item = QTableWidgetItem("—")
        else:
            mark = "✓" if last.success else "✗"
            last_item = QTableWidgetItem(f"{mark} {_fmt_dt(last.finished_at)}")
            last_item.setForeground(QBrush(_OK_COLOR if last.success else _ERR_COLOR))
            if last.error:
                last_item.setToolTip(last.error)
        self.files_table.setItem(row, _C_LAST, last_item)

        nxt = st.next_run
        self.files_table.setItem(
            row, _C_NEXT, QTableWidgetItem(_fmt_next(nxt) if nxt else "—")
        )

    def update_files_dynamic(self) -> None:
        statuses = self._scheduler.get_status()
        if {st.file.id for st in statuses} != set(self._row_by_id.keys()):
            self.rebuild_files_table()
            return
        for st in statuses:
            row = self._row_by_id.get(st.file.id)
            if row is not None:
                self._fill_dynamic(row, st)

    def _selected_file_id(self) -> int | None:
        row = self.files_table.currentRow()
        if row < 0:
            return None
        item = self.files_table.item(row, _C_NAME)
        return item.data(_ID_ROLE) if item else None

    def _update_buttons(self) -> None:
        has = self._selected_file_id() is not None
        for b in (self.btn_edit, self.btn_toggle, self.btn_run, self.btn_remove):
            b.setEnabled(has)

    # ----------------------------------------------------------------- #
    # Журнал
    # ----------------------------------------------------------------- #
    def reload_journal(self) -> None:
        runs = self._storage.list_runs(limit=200)
        self.journal_table.setRowCount(len(runs))
        for row, rec in enumerate(runs):
            self._set_journal_row(row, rec)

    def _set_journal_row(self, row: int, rec: RunRecord) -> None:
        self.journal_table.setItem(row, _J_TIME, QTableWidgetItem(_fmt_dt(rec.started_at)))
        self.journal_table.setItem(row, _J_FILE, QTableWidgetItem(rec.file_path.name))
        self.journal_table.setItem(row, _J_DUR, QTableWidgetItem(_fmt_dur(rec.duration_sec)))
        result = "✓ Успех" if rec.success else f"✗ {rec.error or 'ошибка'}"
        item = QTableWidgetItem(result)
        item.setForeground(QBrush(_OK_COLOR if rec.success else _ERR_COLOR))
        if rec.error:
            item.setToolTip(rec.error)
        self.journal_table.setItem(row, _J_RESULT, item)

    # ----------------------------------------------------------------- #
    # Статус (функция №8) + подсказка трея
    # ----------------------------------------------------------------- #
    def update_status_strip(self) -> None:
        statuses = self._scheduler.get_status()
        active = sum(1 for s in statuses if s.file.enabled)
        total = len(statuses)

        last_runs = [s.last_run for s in statuses if s.last_run]
        if last_runs:
            last = max(last_runs, key=lambda r: r.finished_at)
            mark = "✓" if last.success else "✗"
            last_txt = f"{_fmt_dt(last.finished_at)} ({last.file_path.name} {mark})"
        else:
            last_txt = "—"

        next_candidates = [(s.next_run, s.file.path.name) for s in statuses if s.next_run]
        if next_candidates:
            nxt, name = min(next_candidates, key=lambda t: t[0])
            next_txt = f"{_fmt_next(nxt)} ({name})"
        else:
            next_txt = "—"

        self.status_strip.setText(
            f"<b>Активно:</b> {active} из {total} &nbsp;•&nbsp; "
            f"<b>Последнее:</b> {last_txt} &nbsp;•&nbsp; "
            f"<b>Ближайшее:</b> {next_txt}"
        )
        if hasattr(self, "_tray"):
            self._tray.setToolTip(f"{config.APP_NAME} — активно {active} из {total}")

    def _tick(self) -> None:
        self.update_files_dynamic()
        self.update_status_strip()

    # ----------------------------------------------------------------- #
    # Действия пользователя
    # ----------------------------------------------------------------- #
    def on_add_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Выберите файлы Excel", "", _FILE_FILTER)
        if not paths:
            return
        dialog = ScheduleDialog(self)
        if dialog.exec() != int(ScheduleDialog.DialogCode.Accepted):
            return
        schedule = dialog.get_schedule()
        if schedule is None:
            return
        added, skipped = 0, 0
        for path in paths:
            try:
                wf = self._storage.add_file(path, schedule)
                self._scheduler.sync_file(wf.id)
                added += 1
            except ValueError:
                skipped += 1
        self.rebuild_files_table()
        self.update_status_strip()
        if skipped:
            QMessageBox.information(
                self, "Добавление файлов",
                f"Добавлено: {added}. Уже в списке (пропущено): {skipped}.",
            )

    def on_edit_schedule(self) -> None:
        fid = self._selected_file_id()
        if fid is None:
            return
        wf = self._storage.get_file(fid)
        if wf is None:
            return
        dialog = ScheduleDialog(self, schedule=wf.schedule)
        if dialog.exec() != int(ScheduleDialog.DialogCode.Accepted):
            return
        schedule = dialog.get_schedule()
        if schedule is None:
            return
        self._storage.update_schedule(fid, schedule)
        self._scheduler.sync_file(fid)
        self.rebuild_files_table()
        self.update_status_strip()

    def on_toggle_enabled(self) -> None:
        fid = self._selected_file_id()
        if fid is None:
            return
        wf = self._storage.get_file(fid)
        if wf is None:
            return
        self._storage.set_enabled(fid, not wf.enabled)
        self._scheduler.sync_file(fid)
        self.rebuild_files_table()
        self.update_status_strip()

    def on_run_now(self) -> None:
        fid = self._selected_file_id()
        if fid is None:
            return
        self._scheduler.run_now(fid)
        self.statusBar().showMessage("Обновление запущено…", 3000)
        self.update_files_dynamic()

    def on_run_all(self) -> None:
        started = 0
        for st in self._scheduler.get_status():
            if st.file.enabled and st.file.id is not None:
                self._scheduler.run_now(st.file.id)
                started += 1
        self.statusBar().showMessage(f"Запущено обновлений: {started}", 3000)

    def on_remove_file(self) -> None:
        fid = self._selected_file_id()
        if fid is None:
            return
        wf = self._storage.get_file(fid)
        name = wf.path.name if wf else "файл"
        reply = QMessageBox.question(
            self, "Удаление",
            f"Убрать «{name}» из списка?\nЗаписи журнала сохранятся.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._scheduler.remove_job(fid)
        self._storage.remove_file(fid)
        self.rebuild_files_table()
        self.update_status_strip()

    def on_clear_log(self) -> None:
        reply = QMessageBox.question(
            self, "Очистка журнала", "Удалить все записи журнала обновлений?"
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._storage.clear_log()
        self.reload_journal()

    def on_settings(self) -> None:
        dialog = SettingsDialog(self, self._settings)
        if dialog.exec() != int(SettingsDialog.DialogCode.Accepted):
            return
        values = dialog.get_values()
        self._settings.update(values)
        try:
            autostart.apply(values["start_with_windows"])
        except Exception:
            logger.exception("Не удалось применить автозапуск")

    # ----------------------------------------------------------------- #
    # Трей
    # ----------------------------------------------------------------- #
    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:  # одиночный клик
            if self.isVisible():
                self.hide()
            else:
                self._restore()

    def _restore(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _notify(self, record: RunRecord) -> None:
        if not self._settings.get("show_notifications"):
            return
        if not (hasattr(self, "_tray") and self._tray.isVisible()):
            return
        if not QSystemTrayIcon.supportsMessages():
            return
        name = record.file_path.name
        if record.success:
            self._tray.showMessage(
                "Обновление выполнено",
                f"{name} — успешно ({record.duration_sec:.0f} c)",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
        else:
            self._tray.showMessage(
                "Ошибка обновления",
                f"{name}: {record.error or 'ошибка'}",
                QSystemTrayIcon.MessageIcon.Critical,
                6000,
            )

    # ----------------------------------------------------------------- #
    # Сигнал из планировщика (главный поток)
    # ----------------------------------------------------------------- #
    def _on_run_completed(self, record: RunRecord) -> None:
        self.journal_table.insertRow(0)
        self._set_journal_row(0, record)
        while self.journal_table.rowCount() > 200:
            self.journal_table.removeRow(self.journal_table.rowCount() - 1)
        self.update_files_dynamic()
        self.update_status_strip()
        self._notify(record)

    # ----------------------------------------------------------------- #
    # Завершение / трей
    # ----------------------------------------------------------------- #
    def _shutdown(self) -> None:
        if self._shut_done:
            return
        self._shut_done = True
        if hasattr(self, "_timer"):
            self._timer.stop()
        if hasattr(self, "banner"):
            self.banner.stop()
        self._scheduler.shutdown(wait=False)
        self._storage.close()

    def _quit(self) -> None:
        self._quitting = True
        self._shutdown()
        if hasattr(self, "_tray"):
            self._tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event) -> None:
        # Сворачивание в трей вместо закрытия (функция №4)
        to_tray = (
            not self._quitting
            and bool(self._settings.get("minimize_to_tray"))
            and hasattr(self, "_tray")
            and self._tray.isVisible()
        )
        if to_tray:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown and QSystemTrayIcon.supportsMessages():
                self._tray.showMessage(
                    config.APP_NAME,
                    "Программа свёрнута в трей и продолжает работать.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
                self._tray_hint_shown = True
            return
        self._shutdown()
        super().closeEvent(event)


_STYLE = """
#statusStrip {
    background: #eef3fb;
    border: 1px solid #d4def0;
    border-radius: 6px;
    padding: 8px 12px;
    color: #1c2733;
}
#sectionTitle {
    font-weight: bold;
    color: #44505c;
    margin-top: 2px;
}
QPushButton { padding: 6px 12px; }
QTableWidget { gridline-color: #e3e7ed; }
QHeaderView::section {
    background: #f4f6f9;
    padding: 5px;
    border: none;
    border-bottom: 1px solid #d9dee5;
    font-weight: bold;
}
"""


def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
        ],
    )
    # WebEngine рекомендует общий контекст OpenGL — выставить до QApplication.
    if WEBENGINE_AVAILABLE:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setOrganizationName(config.ORG_NAME)
    # Закрытие окна не должно завершать приложение — мы управляем выходом сами
    # (окно может быть скрыто в трее).
    app.setQuitOnLastWindowClosed(False)

    storage = Storage()
    window = MainWindow(storage)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())