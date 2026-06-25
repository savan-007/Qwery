"""
app/ui/settings_dialog.py

Диалог настроек приложения (Этап 4).

Переключатели:
    * запускать вместе с Windows   (start_with_windows)  — функция №4;
    * сворачивать в трей            (minimize_to_tray)    — функция №4;
    * показывать уведомления        (show_notifications)  — функция №6;
    * анонимная аналитика           (analytics_enabled)   — подключится на Этапе 6.

Диалог только собирает значения (get_values()). Применение автозапуска и
сохранение настроек выполняет главное окно.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

from app.core import autostart


class SettingsDialog(QDialog):
    def __init__(self, parent, settings) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)

        self.cb_autostart = QCheckBox("Запускать вместе с Windows")
        self.cb_tray = QCheckBox("Сворачивать в трей вместо закрытия")
        self.cb_notify = QCheckBox("Показывать уведомления Windows")
        self.cb_analytics = QCheckBox("Анонимная аналитика")

        self.cb_autostart.setChecked(bool(settings.get("start_with_windows")))
        self.cb_tray.setChecked(bool(settings.get("minimize_to_tray")))
        self.cb_notify.setChecked(bool(settings.get("show_notifications")))
        self.cb_analytics.setChecked(bool(settings.get("analytics_enabled")))

        for cb in (self.cb_autostart, self.cb_tray, self.cb_notify, self.cb_analytics):
            root.addWidget(cb)

        if not autostart.is_supported():
            self.cb_autostart.setEnabled(False)
            root.addWidget(self._hint("Автозапуск доступен только на Windows."))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addSpacing(6)
        root.addWidget(buttons)

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#8a8a8a; font-size:11px;")
        return lbl

    def get_values(self) -> dict:
        return {
            "start_with_windows": self.cb_autostart.isChecked(),
            "minimize_to_tray": self.cb_tray.isChecked(),
            "show_notifications": self.cb_notify.isChecked(),
            "analytics_enabled": self.cb_analytics.isChecked(),
        }