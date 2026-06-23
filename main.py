"""
Точка входа Excel Query Scheduler.

ЭТАП 0 — это «дымовой тест»: окно просто открывается и подтверждает, что
PyQt6 корректно работает на вашей машине (Python 3.13). Реальный интерфейс
со списком файлов и расписанием появится на Этапе 3 — тогда содержимое
этого файла заменим на запуск настоящего главного окна из app.ui.main_window.
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

import config


class SmokeTestWindow(QMainWindow):
    """Временное окно для проверки работоспособности стека."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{config.APP_NAME} v{config.APP_VERSION}")
        self.resize(640, 420)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel(config.APP_NAME)
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        status = QLabel(
            "Этап 0 пройден: PyQt6 работает.\n"
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n"
            f"Данные приложения: {config.DATA_DIR}"
        )
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.setStyleSheet("color: gray; font-size: 13px;")

        layout.addWidget(title)
        layout.addWidget(status)
        self.setCentralWidget(central)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setOrganizationName(config.ORG_NAME)

    window = SmokeTestWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())