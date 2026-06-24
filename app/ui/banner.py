"""
app/ui/banner.py

Рекламный баннер (функция ТЗ №7, раздел 3 «Монетизация»).

По ТЗ:
    * нижняя панель главного окна, высота config.BANNER_HEIGHT (90px), во всю ширину;
    * без кнопки «закрыть» (бесплатная версия);
    * движок — PyQt6-WebEngine (встроенный Chromium);
    * HTML-баннер обновляется каждые config.BANNER_REFRESH_SEC (30–60 сек);
    * без видео — статика или HTML5.

Отказоустойчивость:
    если PyQt6-WebEngine не установлен или Chromium не поднялся, баннер не
    роняет приложение — показывается запасная панель. Это удобно и для
    разработки, и для машин без поддержки WebEngine.

Подключение реальной сети (РСЯ / AdSense):
    рекламные скрипты не работают с локального origin (about:blank/file://) —
    сети требуют реальный http(s)-домен. Поэтому в проде размещаете маленькую
    banner.html на своём домене или GitHub Pages и передаёте её URL:
        BannerWidget(url="https://ваш-домен/banner.html")
    Код блока РСЯ или AdSense вставляется внутрь этой страницы.
    В разработке (url не задан) показывается локальная HTML-заглушка.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

import config

logger = logging.getLogger(__name__)

# --- Доступность движка WebEngine ---
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except Exception as _exc:  # модуль не установлен
    QWebEngineView = None  # type: ignore[assignment]
    WEBENGINE_AVAILABLE = False
    logger.warning("PyQt6-WebEngine недоступен (%s) — баннер в режиме заглушки", _exc)


# Локальная HTML-заглушка для разработки.
# В проде вместо неё грузится banner.html с реального домена, куда вставлен
# блок Яндекс РСЯ или Google AdSense.
_PLACEHOLDER_HTML = """
<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;overflow:hidden;font-family:Segoe UI,Arial,sans-serif;}
  .b{height:100%;display:flex;align-items:center;justify-content:center;
     background:linear-gradient(90deg,#2d6cdf,#5a8cf0);color:#fff;}
  .t{font-size:15px;font-weight:600;letter-spacing:.2px;}
  .s{font-size:11px;opacity:.85;margin-top:2px;}
  .w{text-align:center;}
  /* ВСТАВЬТЕ СЮДА БЛОК РСЯ / ADSENSE НА РЕАЛЬНОЙ СТРАНИЦЕ banner.html */
</style></head><body>
  <div class="b"><div class="w">
    <div class="t">Здесь будет рекламный баннер</div>
    <div class="s">Яндекс РСЯ / Google AdSense • банер 90px</div>
  </div></div>
</body></html>
"""


class BannerWidget(QWidget):
    """Нижняя рекламная панель фиксированной высоты."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        url: str | None = None,
        html: str | None = None,
        refresh_sec: int = config.BANNER_REFRESH_SEC,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._html = html if html is not None else _PLACEHOLDER_HTML
        self._view = None

        self.setFixedHeight(config.BANNER_HEIGHT)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Пытаемся построить WebEngine; при любой ошибке — запасная панель.
        if WEBENGINE_AVAILABLE:
            try:
                self._view = QWebEngineView(self)
                self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
                layout.addWidget(self._view)
                self._load()
            except Exception:
                logger.exception("Не удалось создать QWebEngineView — заглушка")
                self._view = None

        if self._view is None:
            layout.addWidget(self._fallback())

        # Периодическое обновление баннера (ротация рекламы).
        self._timer = QTimer(self)
        self._timer.setInterval(max(5, refresh_sec) * 1000)
        self._timer.timeout.connect(self._refresh)
        if self._view is not None:
            self._timer.start()

    # ----------------------------------------------------------------- #
    def _load(self) -> None:
        if self._view is None:
            return
        if self._url:
            self._view.load(QUrl(self._url))
        else:
            self._view.setHtml(self._html)

    def _refresh(self) -> None:
        if self._view is None:
            return
        # Для реального URL перезагружаем страницу (новый показ);
        # для локального HTML просто перерисовываем.
        if self._url:
            self._view.reload()
        else:
            self._view.setHtml(self._html)

    def set_source(self, *, url: str | None = None, html: str | None = None) -> None:
        """Сменить источник баннера на лету."""
        self._url = url
        if html is not None:
            self._html = html
        self._load()

    def _fallback(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("bannerFallback")
        frame.setStyleSheet(
            "#bannerFallback{background:#eef1f6;border-top:1px solid #d9dee5;}"
            " QLabel{color:#8a8a8a;font-size:12px;}"
        )
        inner = QVBoxLayout(frame)
        label = QLabel("Рекламный баннер (WebEngine недоступен)")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inner.addWidget(label)
        return frame

    def stop(self) -> None:
        """Остановить таймер обновления (при закрытии)."""
        self._timer.stop()