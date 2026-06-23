"""
app/ui/schedule_dialog.py

Диалог настройки расписания для файла (функция ТЗ №2).

Три режима:
    * каждые N минут        → Schedule.every_minutes(n)
    * ежедневно в ЧЧ:ММ     → Schedule.daily_at("ЧЧ:ММ")
    * по дням недели в ЧЧ:ММ → Schedule.weekly_at("ЧЧ:ММ", [...])

Диалог умеет открываться как для нового файла, так и для редактирования
существующего расписания (параметр `schedule`).
"""

from __future__ import annotations

from PyQt6.QtCore import QTime, Qt
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QRadioButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.storage import Schedule

# Порядок дней недели: подпись для UI → токен для Schedule/APScheduler
_DAYS = [
    ("Пн", "mon"), ("Вт", "tue"), ("Ср", "wed"), ("Чт", "thu"),
    ("Пт", "fri"), ("Сб", "sat"), ("Вс", "sun"),
]


class ScheduleDialog(QDialog):
    """Модальный диалог выбора расписания. Результат — get_schedule()."""

    def __init__(self, parent=None, schedule: Schedule | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Расписание обновления")
        self.setMinimumWidth(380)
        self._result: Schedule | None = None

        root = QVBoxLayout(self)

        # --- Режим 1: интервал ---
        self.rb_interval = QRadioButton("Каждые")
        self.spin_minutes = QSpinBox()
        self.spin_minutes.setRange(1, 1440)
        self.spin_minutes.setValue(60)
        self.spin_minutes.setSuffix(" мин")
        row_interval = QHBoxLayout()
        row_interval.addWidget(self.rb_interval)
        row_interval.addWidget(self.spin_minutes)
        row_interval.addStretch(1)
        root.addLayout(row_interval)

        # --- Режим 2: ежедневно ---
        self.rb_daily = QRadioButton("Ежедневно в")
        self.time_daily = QTimeEdit()
        self.time_daily.setDisplayFormat("HH:mm")
        self.time_daily.setTime(QTime(9, 0))
        row_daily = QHBoxLayout()
        row_daily.addWidget(self.rb_daily)
        row_daily.addWidget(self.time_daily)
        row_daily.addStretch(1)
        root.addLayout(row_daily)

        # --- Режим 3: по дням недели ---
        self.rb_weekly = QRadioButton("По дням недели в")
        self.time_weekly = QTimeEdit()
        self.time_weekly.setDisplayFormat("HH:mm")
        self.time_weekly.setTime(QTime(18, 0))
        row_weekly = QHBoxLayout()
        row_weekly.addWidget(self.rb_weekly)
        row_weekly.addWidget(self.time_weekly)
        row_weekly.addStretch(1)
        root.addLayout(row_weekly)

        # чекбоксы дней
        self.day_checks: dict[str, QCheckBox] = {}
        days_grid = QGridLayout()
        days_grid.setContentsMargins(24, 0, 0, 0)
        for col, (label, token) in enumerate(_DAYS):
            cb = QCheckBox(label)
            self.day_checks[token] = cb
            days_grid.addWidget(cb, 0, col)
        self._days_widget = QWidget()
        self._days_widget.setLayout(days_grid)
        root.addWidget(self._days_widget)

        # группа радиокнопок
        self._group = QButtonGroup(self)
        self._group.addButton(self.rb_interval)
        self._group.addButton(self.rb_daily)
        self._group.addButton(self.rb_weekly)
        self._group.buttonToggled.connect(self._update_enabled)

        # кнопки OK/Отмена
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addSpacing(6)
        root.addWidget(buttons)

        # начальное состояние / предзаполнение
        self._prefill(schedule)
        self._update_enabled()

    # --------------------------------------------------------------- #
    def _prefill(self, schedule: Schedule | None) -> None:
        if schedule is None:
            self.rb_interval.setChecked(True)
            return
        if schedule.kind == "interval":
            self.rb_interval.setChecked(True)
            self.spin_minutes.setValue(schedule.interval_minutes or 60)
        elif schedule.kind == "daily":
            self.rb_daily.setChecked(True)
            h, m = schedule.hour_minute()
            self.time_daily.setTime(QTime(h, m))
        elif schedule.kind == "weekly":
            self.rb_weekly.setChecked(True)
            h, m = schedule.hour_minute()
            self.time_weekly.setTime(QTime(h, m))
            for token in schedule.days_of_week:
                if token in self.day_checks:
                    self.day_checks[token].setChecked(True)

    def _update_enabled(self, *args) -> None:
        """Активны только поля выбранного режима."""
        self.spin_minutes.setEnabled(self.rb_interval.isChecked())
        self.time_daily.setEnabled(self.rb_daily.isChecked())
        weekly = self.rb_weekly.isChecked()
        self.time_weekly.setEnabled(weekly)
        self._days_widget.setEnabled(weekly)

    def _build(self) -> Schedule:
        """Собрать Schedule из полей (бросает ValueError при ошибке)."""
        if self.rb_interval.isChecked():
            return Schedule.every_minutes(self.spin_minutes.value())
        if self.rb_daily.isChecked():
            return Schedule.daily_at(self.time_daily.time().toString("HH:mm"))
        # weekly
        days = [t for t, cb in self.day_checks.items() if cb.isChecked()]
        return Schedule.weekly_at(self.time_weekly.time().toString("HH:mm"), days)

    # --------------------------------------------------------------- #
    def accept(self) -> None:  # noqa: D401 — переопределяем для валидации
        try:
            self._result = self._build()
        except ValueError as exc:
            QMessageBox.warning(self, "Расписание", str(exc))
            return
        super().accept()

    def get_schedule(self) -> Schedule | None:
        """Выбранное расписание после exec() с результатом Accepted."""
        return self._result