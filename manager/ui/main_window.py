# manager/ui/main_window.py - frameless dark main window for the DSV Manager.
import ctypes
import json
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QCursor, QIcon, QKeySequence, QMouseEvent, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from modular.path_utils import is_pipeline_running, resolve_mod_root
from ui.pipeline_bar import _CLOSE_FORCE_AFTER_S, PipelineBar
from ui.tabs_audio import AudioTab
from ui.tabs_profile import ProfileTab
from ui.tabs_system import SystemTab
from ui.tabs_voice import VoiceModelTab
from ui.title_bar import TitleBar

_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2
_RESIZE_MARGIN = 8

_DARK_QSS = """
#dsvTitleBar { background-color: #1c1c1c; }
QMainWindow, QWidget { background-color: #202020; color: #E6E6E6; font-size: 9pt; }
QGroupBox {
    border: 1px solid #3a3a3a; border-radius: 6px; margin-top: 10px;
    font-weight: bold; color: #E6E6E6;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9fc3e8; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: #2b2b2b; color: #E6E6E6; border: 1px solid #3f3f3f;
    border-radius: 4px; padding: 3px;
}
QPushButton {
    background-color: #333333; color: #E6E6E6; border: 1px solid #454545;
    border-radius: 4px; padding: 5px 14px;
}
QPushButton:hover { background-color: #3d3d3d; }
QPushButton:pressed { background-color: #2a2a2a; }
QPushButton:disabled { color: #777777; }
QComboBox QAbstractItemView { background-color: #2b2b2b; color: #E6E6E6; selection-background-color: #0d5c9c; }
QListWidget { background-color: #262626; color: #E6E6E6; border: 1px solid #3a3a3a; border-radius: 4px; }
QTabWidget::pane { border: 1px solid #3a3a3a; border-radius: 4px; }
QTabBar::tab {
    background-color: #2b2b2b; color: #bbbbbb; padding: 6px 16px;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
}
QTabBar::tab:selected { background-color: #202020; color: #ffffff; border-bottom: 2px solid #5fb8ff; }
QToolTip { background-color: #2b2b2b; color: #E6E6E6; border: 1px solid #555555; }
#pipelineBar { background-color: #1c1c1c; border-top: 1px solid #3a3a3a; }
QCheckBox::indicator, QSpinBox::up-button, QSpinBox::down-button { width: 14px; }
"""


class MainWindow(QMainWindow):
    """NMS Dynamic Suit Voice management utility (PySide6 port)."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.project_root = Path(__file__).parent.parent.parent
        self.env_config_file = self.project_root / "config" / "environment_config.json"
        self.user_config_file = self.project_root / "user" / "user_customization.json"
        self._legacy_user_config_file = self.project_root / "config" / "user_customization.json"
        # One-time migration: move the legacy tracked location into user/.
        if not self.user_config_file.exists() and self._legacy_user_config_file.exists():
            self.user_config_file.parent.mkdir(parents=True, exist_ok=True)
            self._legacy_user_config_file.replace(self.user_config_file)
        self._threads = []
        self._resize_drag_edges = None
        self._resize_press_global = None
        self._resize_geometry = None

        self.setWindowTitle("NMS Dynamic Suit Voice - Management Utility")
        self._load_configurations()

        # First-run gate: until setup completes, only the System tab mounts.
        from manager.setup_runner import is_setup_complete

        self.setup_pending = not is_setup_complete(self.project_root)

        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.title_bar = TitleBar(
            self, "NMS Dynamic Suit Voice - Management Utility",
            icon_path=str(self.project_root / "assets" / "nms_dsv.png"),
        )
        root_layout.addWidget(self.title_bar)

        if self.setup_pending:
            banner = QLabel(
                "Setup required - open the System Environment tab, enter your No Man's Sky mod directory and press Setup."
            )
            banner.setStyleSheet("color: #FBBF24; font-weight: bold; padding: 8px;")
            banner.setWordWrap(True)
            root_layout.addWidget(banner)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, 1)

        if self.setup_pending:
            self.tabs.addTab(SystemTab(self), "System Environment")
        else:
            self.tabs.addTab(ProfileTab(self), "User Profile")
            self.tabs.addTab(AudioTab(self), "Audio Modifiers")
            self.tabs.addTab(VoiceModelTab(self), "Voice Model")
            self.tabs.addTab(SystemTab(self), "System Environment")

        self.pipeline_bar = PipelineBar(self)
        root_layout.addWidget(self.pipeline_bar)

        apply_btn = QPushButton("Apply Changes")
        apply_btn.clicked.connect(self.save_and_continue_settings)
        apply_btn.setToolTip("Saves every tab's settings to disk. Closing the window (X) just hides it to the tray.")
        self.pipeline_bar.add_tail(apply_btn)

        self.setCentralWidget(central)
        self.resize(1150, 760)
        self.setMinimumSize(860, 560)
        self._load_window_state()

        # Win11 rounded corners
        try:
            preference = ctypes.c_int(_DWMWCP_ROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()), _DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(preference), ctypes.sizeof(preference),
            )
        except Exception:
            pass

        rescan_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        rescan_shortcut.activated.connect(lambda: None)

        self.installEventFilter(self)
        self.setMouseTracking(True)
        for widget in self.findChildren(QWidget):
            widget.setMouseTracking(True)

        if not self.setup_pending:
            runtime = self.env_data.get("runtime", {})
            if runtime.get("autostart_pipeline", True) and not is_pipeline_running():
                QTimer.singleShot(1200, self.pipeline_bar._pipeline_start)

        self._setup_tray_icon()

    # ------------------------------------------------------- configuration
    def _load_configurations(self):
        try:
            self.env_data = json.loads(self.env_config_file.read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.critical(self, "Configuration Error", f"Could not read environment config:\n{e}")
            sys.exit(1)
        if self.user_config_file.exists():
            self.user_data = json.loads(self.user_config_file.read_text(encoding="utf-8"))
        else:
            self.user_data = {}

        self.available_tones = ["Standard"]
        self.available_wordiness = ["Standard"]
        prompt_file = self.project_root / "data" / "prompt_data.json"
        if prompt_file.exists():
            try:
                pdata = json.loads(prompt_file.read_text(encoding="utf-8"))
                self.available_tones = list(pdata.get("tones", {}).keys())
                self.available_wordiness = list(pdata.get("wordiness", {}).keys())
            except Exception:
                pass

    def refresh_voice_highlight(self):
        """Re-render the Voice Model tab's active-voice highlight (used when a preset switches the voice)."""
        voice_tab = self.tabs.widget(2) if not self.setup_pending else None
        if voice_tab is not None and hasattr(voice_tab, "_refresh_catalog"):
            voice_tab._refresh_catalog()

    def save_and_continue_settings(self):
        self._execute_file_save_matrix()
        self.user_data = json.loads(self.user_config_file.read_text(encoding="utf-8"))
        QMessageBox.information(
            self, "Applied", "Preferences committed safely to disk! The window stays open for further edits."
        )

    def _execute_file_save_matrix(self):
        """Compile the tab widgets back into the JSON config files."""
        system_tab = self.tabs.widget(0) if self.setup_pending else self.tabs.widget(3)
        profile_tab = None if self.setup_pending else self.tabs.widget(0)
        audio_tab = None if self.setup_pending else self.tabs.widget(1)
        voice_tab = None if self.setup_pending else self.tabs.widget(2)

        # environment config: runtime + defaults only. The personal mod dir goes
        # into the UNTRACKED user config (never into a tracked file).
        if system_tab is not None:
            mod_dir = system_tab.mod_dir_edit.text().strip()
            if mod_dir:
                self.user_data.setdefault("paths", {})["mod_dir"] = mod_dir.replace("\\", "/")
            self.env_data.setdefault("runtime", {})["quick_cache_max"] = int(system_tab.cache_max_spin.value())
            self.env_data.setdefault("runtime", {})["check_interval"] = float(system_tab.interval_spin.value())
        self.env_config_file.write_text(json.dumps(self.env_data, indent=4), encoding="utf-8")

        # user customization: paths + identity + preferences + effects + voice
        updated = self.user_data if self.user_config_file.exists() else {}
        updated.setdefault("paths", {})
        if system_tab is not None:
            mod_dir = system_tab.mod_dir_edit.text().strip()
            if mod_dir:
                updated["paths"]["mod_dir"] = mod_dir.replace("\\", "/")
        updated.setdefault("tts_voice", "fantine")
        if profile_tab is not None:
            updated["player_name"] = profile_tab.name_edit.text().strip() or "Interloper"
            updated["phrase_settings"] = {
                "tone": profile_tab.tone_combo.currentText(),
                "wordiness": profile_tab.wordiness_combo.currentText(),
            }
        if audio_tab is not None:
            fx = updated.get("voice_effects", {})
            fx.update(audio_tab._slider_values())
            updated["voice_effects"] = fx
        if voice_tab is not None:
            # the voice tab writes tts_voice itself on Apply; keep current value
            pass
        self.user_config_file.write_text(json.dumps(updated, indent=4), encoding="utf-8")

    # ------------------------------------------------- tray icon
    def _setup_tray_icon(self):
        """Tray presence while the utility is open: live pipeline state, Start/Stop and window restore."""
        icon_path = self.project_root / "assets" / "nms_dsv.png"
        self.tray_icon = QSystemTrayIcon(QIcon(str(icon_path)), self)
        self.tray_icon.setToolTip("NMS Dynamic Suit Voice - Manager")

        menu = QMenu()
        show_action = QAction("Show Manager", self)
        show_action.triggered.connect(self._show_from_tray)
        start_action = QAction("Start Pipeline", self)
        start_action.triggered.connect(self.pipeline_bar._pipeline_start)
        stop_action = QAction("Stop Pipeline", self)
        stop_action.triggered.connect(lambda: self.pipeline_bar._pipeline_stop())
        exit_action = QAction("Exit - stops the voice engine", self)
        exit_action.triggered.connect(self._exit_from_tray)
        for act in (show_action, start_action, stop_action, exit_action):
            menu.addAction(act)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def set_tray_status(self, state_text: str):
        """Called by the pipeline bar on every status change."""
        if getattr(self, "tray_icon", None) is not None:
            self.tray_icon.setToolTip(f"NMS Dynamic Suit Voice - {state_text}")

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_from_tray(self):
        self.show()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------- window state memory
    def _ui_state_file(self) -> Path:
        return self.project_root / "config" / "ui_state.json"

    def _load_window_state(self):
        """Restore the window size/position (and maximized state); default size on first run."""
        try:
            data = json.loads(self._ui_state_file().read_text(encoding="utf-8"))
            geo = QByteArray.fromBase64(data.get("geometry", "").encode("ascii"))
            if geo:
                self.restoreGeometry(geo)
        except Exception:
            pass

    def _save_window_state(self):
        try:
            b64 = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self._ui_state_file().write_text(json.dumps({"geometry": b64}), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------- shutdown behavior
    def closeEvent(self, event):
        self._save_window_state()
        if getattr(self, "_really_quit", False):
            event.accept()
            QApplication.instance().quit()
            return
        bar = getattr(self, "pipeline_bar", None)
        if bar is not None and bar._stopping:
            event.ignore()
            self.hide()
            bar._close_pending = self.close
            return
        if not is_pipeline_running():
            event.accept()
            QApplication.instance().quit()
            return
        event.ignore()
        self.hide()  # engine keeps running; tray icon remains

    def _exit_from_tray(self):
        """Tray Exit: stop the running engine (any engine honors the stop flag), then quit."""
        self._really_quit = True
        bar = getattr(self, "pipeline_bar", None)
        if bar is not None and is_pipeline_running():
            self.hide()
            bar._pipeline_stop(on_done=self.close, force_after_s=_CLOSE_FORCE_AFTER_S)
            return
        self.close()

    # ------------------------------------------------- frameless resizing
    def eventFilter(self, obj, event):
        """Track the mouse for manual edge resizing and mirror the hover cursor."""
        if not isinstance(event, QMouseEvent):
            return False
        pos = event.position().toPoint()
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
                edges = self._resize_edges(pos)
                if edges is not None:
                    self._resize_drag_edges = edges
                    self._resize_press_global = event.globalPosition().toPoint()
                    self._resize_geometry = self.geometry()
                    self.grabMouse(self._resize_cursor(pos))
                    return True
            return False
        if event.type() == QEvent.Type.MouseMove:
            if (
                self._resize_drag_edges is not None
                and self._resize_press_global is not None
                and self._resize_geometry is not None
            ):
                self._apply_resize_drag(event.globalPosition().toPoint())
                return True
            self._update_edge_cursor(self._resize_cursor(pos))
            return False
        if event.type() == QEvent.Type.MouseButtonRelease and self._resize_drag_edges is not None:
            self._finish_resize_drag()
            return True
        return False

    def _resize_edges(self, pos):
        rect = self.rect()
        left = pos.x() <= rect.left() + _RESIZE_MARGIN
        right = pos.x() >= rect.right() - _RESIZE_MARGIN
        top = pos.y() <= rect.top() + _RESIZE_MARGIN
        bottom = pos.y() >= rect.bottom() - _RESIZE_MARGIN
        if not (left or right or top or bottom):
            return None
        edges = Qt.Edge(0)
        if left:
            edges |= Qt.Edge.LeftEdge
        if right:
            edges |= Qt.Edge.RightEdge
        if top:
            edges |= Qt.Edge.TopEdge
        if bottom:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _resize_cursor(self, pos):
        rect = self.rect()
        left = pos.x() <= rect.left() + _RESIZE_MARGIN
        right = pos.x() >= rect.right() - _RESIZE_MARGIN
        top = pos.y() <= rect.top() + _RESIZE_MARGIN
        bottom = pos.y() >= rect.bottom() - _RESIZE_MARGIN
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (left and bottom) or (right and top):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return None

    def _update_edge_cursor(self, shape):
        if shape is None:
            self.unsetCursor()
        else:
            self.setCursor(shape)

    def _apply_resize_drag(self, global_pos):
        edges = self._resize_drag_edges
        press_global = self._resize_press_global
        base = self._resize_geometry
        if edges is None or press_global is None or base is None:
            return
        delta = global_pos - press_global
        min_w = self.minimumWidth()
        min_h = self.minimumHeight()
        x, y = base.x(), base.y()
        width, height = base.width(), base.height()
        if edges & Qt.Edge.LeftEdge:
            x = min(base.x() + delta.x(), base.right() - min_w + 1)
            width = base.right() - x + 1
        if edges & Qt.Edge.TopEdge:
            y = min(base.y() + delta.y(), base.bottom() - min_h + 1)
            height = base.bottom() - y + 1
        if edges & Qt.Edge.RightEdge:
            width = max(base.width() + delta.x(), min_w)
        if edges & Qt.Edge.BottomEdge:
            height = max(base.height() + delta.y(), min_h)
        self.setGeometry(x, y, max(width, min_w), max(height, min_h))

    def _finish_resize_drag(self):
        if self.mouseGrabber() is self:
            self.releaseMouse()
        self.unsetCursor()
        self._resize_drag_edges = None
        self._resize_press_global = None
        self._resize_geometry = None
