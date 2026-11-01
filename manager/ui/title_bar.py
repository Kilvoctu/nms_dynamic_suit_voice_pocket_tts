# manager/ui/title_bar.py - frameless dark title bar with Fluent window controls.
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

_TITLE_BAR_HEIGHT = 36


class TitleBar(QWidget):
    """Frameless title bar: drag to move, double-click toggles maximize."""

    def __init__(self, window, title: str, icon_path: str | None = None):
        super().__init__(window)
        self._window = window
        self.setObjectName("dsvTitleBar")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFixedHeight(_TITLE_BAR_HEIGHT)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(6)

        if icon_path:
            icon_label = QLabel(self)
            icon_label.setPixmap(
                QPixmap(icon_path).scaled(
                    20, 20,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            layout.addWidget(icon_label)

        title_label = QLabel(title, self)
        title_font = title_label.font()
        title_font.setPointSize(10)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        layout.addStretch(1)
        self._title_label = title_label

        glyph_font = QFont()
        glyph_font.setFamilies(["Segoe Fluent Icons", "Segoe MDL2 Assets"])
        glyph_font.setPointSize(9)

        self._min_button = QToolButton(self)
        self._min_button.setObjectName("minButton")
        self._min_button.setAutoRaise(True)
        self._min_button.setFixedSize(28, 28)
        self._min_button.setFont(glyph_font)
        self._min_button.setText("\uE921")  # ChromeMinimize
        self._min_button.clicked.connect(window.showMinimized)
        layout.addWidget(self._min_button)

        self._max_button = QToolButton(self)
        self._max_button.setObjectName("maxButton")
        self._max_button.setAutoRaise(True)
        self._max_button.setFixedSize(28, 28)
        self._max_button.setFont(glyph_font)
        self._max_button.setText("\uE922")  # ChromeMaximize
        self._max_button.clicked.connect(self._toggle_maximized)
        layout.addWidget(self._max_button)

        self._close_button = QToolButton(self)
        self._close_button.setObjectName("closeButton")
        self._close_button.setAutoRaise(True)
        self._close_button.setFixedSize(28, 28)
        self._close_button.setFont(glyph_font)
        self._close_button.setText("\uE8BB")  # ChromeClose
        self._close_button.clicked.connect(window.close)
        layout.addWidget(self._close_button)

    def set_title(self, text: str) -> None:
        """Live title update (used by the pipeline status bar)."""
        self._title_label.setText(text)

    def _toggle_maximized(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def mousePressEvent(self, event) -> None:
        """Drag the window by any empty part of the title bar."""
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
