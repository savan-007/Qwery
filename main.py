"""
Точка входа Excel Query Scheduler.

С Этапа 3 запускает настоящее главное окно (список файлов, расписание,
статус, журнал). Вся сборка приложения — в app.ui.main_window.run().
"""

from __future__ import annotations

import sys

from app.ui.main_window import run

if __name__ == "__main__":
    sys.exit(run())