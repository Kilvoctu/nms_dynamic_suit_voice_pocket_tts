# manager/ui/main_window.py - frameless dark main window for the DSV Manager.
import copy
import ctypes
import json
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QMouseEvent, QShortcut
from PySide6.QtWidgets import (
    QApplication,
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

from engine.config import ENVIRONMENT_DEFAULTS
from engine.path_utils import is_pipeline_running
from ui.style import WARN
from ui.pipeline_bar import _CLOSE_FORCE_AFTER_S, PipelineBar
from ui.tabs_audio import AudioTab
from ui.tabs_profile import ProfileTab
from ui.tabs_system import SystemTab
from ui.title_bar import TitleBar

_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2
_RESIZE_MARGIN = 8


class MainWindow(QMainWindow):
    """NMS Dynamic Suit Voice management utility (PySide6 port)."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.project_root = Path(__file__).parent.parent.parent
        self.user_config_file = self.project_root / "state" / "user" / "user_customization.json"
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
                "Setup required - in System Environment tab, enter your No Man's Sky mod directory and press Setup."
            )
            banner.setStyleSheet(f"color: {WARN}; font-weight: bold; padding: 8px;")
            banner.setWordWrap(True)
            root_layout.addWidget(banner)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, 1)

        if self.setup_pending:
            self.tabs.addTab(SystemTab(self), "System Environment")
        else:
            self.tabs.addTab(ProfileTab(self), "User Profile")
            self.tabs.addTab(AudioTab(self), "Voice Modifier")
            self.tabs.addTab(SystemTab(self), "System Environment")

        self.pipeline_bar = PipelineBar(self)
        root_layout.addWidget(self.pipeline_bar)

        apply_btn = QPushButton("Apply Changes")
        apply_btn.clicked.connect(self.save_and_continue_settings)
        apply_btn.setToolTip("Saves every tab's settings to disk. Closing the window (X) just hides it to the tray.")
        self.pipeline_bar.add_tail(apply_btn)

        self.setCentralWidget(central)
        self.resize(860, 660)
        self.setMinimumSize(860, 660)
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
            runtime = {**self.env_data.get("runtime", {}), **self.user_data.get("runtime", {})}
            if runtime.get("autostart_pipeline", True) and not is_pipeline_running():
                QTimer.singleShot(1200, self.pipeline_bar._pipeline_start)

        self._setup_tray_icon()

    # ------------------------------------------------------- configuration
    def _load_configurations(self):
        self.env_data = copy.deepcopy(ENVIRONMENT_DEFAULTS)  # shipped defaults, never written back
        if self.user_config_file.exists():
            self.user_data = json.loads(self.user_config_file.read_text(encoding="utf-8"))
        else:
            self.user_data = {}

        self.available_tones = ["Standard"]
        self.available_wordiness = ["Standard"]
        prompt_file = self.project_root / "assets" / "data" / "prompt_data.json"
        if prompt_file.exists():
            try:
                pdata = json.loads(prompt_file.read_text(encoding="utf-8"))
                self.available_tones = list(pdata.get("tones", {}).keys())
                self.available_wordiness = list(pdata.get("wordiness", {}).keys())
            except Exception:
                pass

    def refresh_voices(self):
        """Re-sync the Voice Modifier tab's voice dropdown (clones added or removed elsewhere)."""
        audio_tab = self.tabs.widget(1) if not self.setup_pending else None
        if audio_tab is not None and hasattr(audio_tab, "refresh_voices"):
            audio_tab.refresh_voices()

    def save_and_continue_settings(self):
        old_variant = str(self.user_data.get("models", {}).get("llm_variant", "e2b")).lower()
        self._execute_file_save_matrix()
        self.user_data = json.loads(self.user_config_file.read_text(encoding="utf-8"))
        note = ""
        if str(self.user_data.get("models", {}).get("llm_variant", "e2b")).lower() != old_variant:
            if is_pipeline_running():
                self.pipeline_bar._pipeline_stop(on_done=self.pipeline_bar._pipeline_start)
                note = " The voice engine is restarting to load the new rephrasing model."
            else:
                note = " The new rephrasing model loads the next time the engine starts."
        QMessageBox.information(
            self, "Applied", "Preferences committed safely to disk!" + note + " The window stays open for further edits."
        )

    def _execute_file_save_matrix(self):
        """Compile the tab widgets back into the JSON config files."""
        system_tab = self.tabs.widget(0) if self.setup_pending else self.tabs.widget(2)
        profile_tab = None if self.setup_pending else self.tabs.widget(0)
        audio_tab = None if self.setup_pending else self.tabs.widget(1)

        # environment config: tracked defaults only. Personal mod dir + runtime
        # overrides go into the UNTRACKED user config (never into a tracked file).
        if system_tab is not None:
            mod_dir = system_tab.mod_dir_edit.text().strip()
            if mod_dir:
                self.user_data.setdefault("paths", {})["mod_dir"] = mod_dir.replace("\\", "/")

        # user customization: paths + identity + preferences + effects + voice
        updated = self.user_data if self.user_config_file.exists() else {}
        updated.setdefault("paths", {})
        if system_tab is not None:
            mod_dir = system_tab.mod_dir_edit.text().strip()
            if mod_dir:
                updated["paths"]["mod_dir"] = mod_dir.replace("\\", "/")
            updated.setdefault("runtime", {})
            updated["runtime"]["quick_cache_max"] = int(system_tab.cache_max_spin.value())
            updated["runtime"]["serve_workers"] = int(system_tab.serve_spin.value())
            updated["runtime"]["gen_threads"] = int(system_tab.threads_spin.value())
            if hasattr(system_tab, "model_combo"):
                updated.setdefault("models", {})["llm_variant"] = system_tab.model_combo.currentData() or "e2b"
        updated.setdefault("tts_voice", "fantine")
        if profile_tab is not None:
            updated["player_name"] = profile_tab.name_edit.text().strip() or "Interloper"
            updated["phrase_settings"] = {
                "tone": profile_tab.tone_combo.currentText(),
                "wordiness": profile_tab.wordiness_combo.currentText(),
            }
            from engine import prompt_presets

            presets = prompt_presets.load_presets(self.project_root)
            presets[prompt_presets.BASE_ACTIVE_KEY] = (
                profile_tab.base_combo.currentText() or prompt_presets.ACTIVE_DEFAULT)
            prompt_presets.save_presets(self.project_root, presets)
        if audio_tab is not None:
            # Commit the voice dropdown (a fresh pick or a preset-switched one) and
            # the active render preset bound to it. The engine live-reloads the
            # preset file (settings_mtime) and the era sweep re-records.
            updated["tts_voice"] = audio_tab.current_voice() or updated.get("tts_voice", "fantine")
            # The volume trim rides inside the committed rack (commit_active_preset), not here.
            audio_tab.commit_active_preset(updated.get("tts_voice", ""))
        self.user_config_file.write_text(json.dumps(updated, indent=4), encoding="utf-8")
        if audio_tab is not None:
            self.refresh_voices()

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
        """Window state lives with the rest of the user's untracked settings."""
        return self.project_root / "state" / "user" / "ui_state.json"

    def _load_window_state(self):
        """Restore the window size/position (and maximized state); default size on first run."""
        try:
            data = json.loads(self._ui_state_file().read_text(encoding="utf-8"))
            geo = QByteArray.fromBase64(data.get("geometry", "").encode("ascii"))
            if geo:
                self.restoreGeometry(geo)
        except Exception:
            pass

    def last_browse_dir(self, key: str) -> str:
        """Last folder the user picked a file in (per dialog key), for reopen-here dialogs."""
        try:
            data = json.loads(self._ui_state_file().read_text(encoding="utf-8"))
            return data.get("last_dirs", {}).get(key, "")
        except Exception:
            return ""

    def remember_browse_dir(self, key: str, path: str):
        """Persist the folder of a just-picked file so its dialog reopens there."""
        try:
            state_file = self._ui_state_file()
            data = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
            data.setdefault("last_dirs", {})[key] = str(Path(path).parent)
            state_file.write_text(json.dumps(data, indent=4), encoding="utf-8")
        except Exception:
            pass

    def _save_window_state(self):
        try:
            b64 = bytes(self.saveGeometry().toBase64()).decode("ascii")
            state_file = self._ui_state_file()
            data = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
            data["geometry"] = b64
            state_file.write_text(json.dumps(data, indent=4), encoding="utf-8")
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
