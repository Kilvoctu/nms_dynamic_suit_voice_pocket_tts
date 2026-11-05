# manager/ui/pipeline_bar.py - always-visible bottom strip: live pipeline status
# + Start/Stop, present on every tab. Owns the pipeline subprocess it launched.
import json
import os
import subprocess
import sys
import time

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton

from engine import pipeline_status
from engine.path_utils import is_pipeline_running

_STOP_FORCE_AFTER_S = 20  # graceful window before offering a force kill
_CLOSE_FORCE_AFTER_S = 15  # shorter patience when the user is closing the app

_START_TIP = (
    "Start the suit-voice engine. It watches the game's mod folder and swaps\n"
    "in freshly generated voice lines as the game plays them."
)
_STOP_TIP = (
    "Stop the suit-voice engine gracefully (it finishes the current voice\n"
    "line first, then exits). The tray icon's Exit does the same."
)


class PipelineBar(QFrame):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setObjectName("pipelineBar")
        self._process = None  # Popen of a pipeline this manager launched
        self._log_handle = None
        self._stop_deadline = None
        self._stopping = False
        self._force_after_s = _STOP_FORCE_AFTER_S
        self._close_pending = None  # callback to finish app close once stopped
        self._color = "#8a8a8a"
        self._full_text = "Status: checking..."
        self._title_suffix = "Starting"
        self._flash_until = 0.0  # monotonic deadline of a transient status message
        self._flash_text = None
        self._depth_checked = 0.0  # cache-depth readout, refreshed every ~5s
        self._depth_text = ""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 6)
        layout.setSpacing(8)
        self.setFixedHeight(38)

        self.status_dot = QLabel("\u25cf")
        self.status_dot.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(self.status_dot)

        self.status_text = QLabel("Status: checking...")
        self.status_text.setStyleSheet("font-weight: bold; color: #8a8a8a;")
        layout.addWidget(self.status_text, 1)

        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._pipeline_start)
        self.start_btn.setToolTip(_START_TIP)
        layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(lambda: self._pipeline_stop())
        self.stop_btn.setToolTip(_STOP_TIP)
        layout.addWidget(self.stop_btn)

        self._tail_separator = None  # created lazily by add_tail()

    def add_tail(self, widget):
        """Append a window-control button to the same single bottom row,
        after a thin separator (so Apply/Save/Cancel share the strip)."""
        if self._tail_separator is None:
            line = QFrame()
            line.setObjectName("pipelineBarSeparator")
            line.setFrameShape(QFrame.Shape.VLine)
            line.setFixedHeight(20)
            line.setStyleSheet("color: #3a3a3a; background-color: #3a3a3a; max-width: 1px;")
            self.layout().addWidget(line)
            self._tail_separator = line
        self.layout().addWidget(widget)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    # ------------------------------------------------------------- display
    def _set_state(self, text: str, color: str, start_on: bool, stop_on: bool, title_suffix: str):
        self._color = color
        self._full_text = text
        self._title_suffix = title_suffix
        self._render_status_text()
        self.status_dot.setStyleSheet(f"color: {color};")
        self.status_text.setStyleSheet(f"font-weight: bold; color: {color};")
        self.start_btn.setEnabled(start_on)
        self.stop_btn.setEnabled(stop_on)
        self.window.setWindowTitle(f"NMS Dynamic Suit Voice - {title_suffix}")
        if hasattr(self.window, "title_bar") and hasattr(self.window.title_bar, "set_title"):
            self.window.title_bar.set_title(f"NMS Dynamic Suit Voice - {title_suffix}")
        if hasattr(self.window, "set_tray_status"):
            self.window.set_tray_status(title_suffix)

    def flash_message(self, text: str, color: str, seconds: int = 5):
        """Temporarily override the status line (e.g. a purge or easter-egg
        note); the next _refresh tick resumes the live pipeline status."""
        self._flash_until = time.monotonic() + seconds
        self._flash_text = (text, color)
        self._set_state(text, color, self.start_btn.isEnabled(), self.stop_btn.isEnabled(),
                        title_suffix=self._title_suffix)

    def _render_status_text(self):
        """Keep the status to a single line: elide the middle of the detail so
        the leading 'Status:' and any trailing 'n/total' counter stay visible."""
        fm = self.status_text.fontMetrics()
        available = max(40, self.width() - 120)  # minus dot, margins, buttons
        self.status_text.setText(fm.elidedText(self._full_text, Qt.TextElideMode.ElideMiddle, available))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_status_text()

    def _read_generation_queue(self, status: dict) -> int:
        """Pending background generation count from the engine's sidecar (pid-checked:
        a file left by a previous engine process reads as 0, never fake activity)."""
        try:
            path = Path(self.window.project_root) / "state" / "cache" / "pipeline_queue.json"
            entry = json.loads(path.read_text(encoding="utf-8"))
            if int(entry.get("pid", -1)) != int(status.get("pid", -2)):
                return 0
            return max(0, int(entry.get("pending", 0)))
        except Exception:
            return 0

    def _cache_depth_text(self):
        if time.time() - self._depth_checked < 5:
            return self._depth_text
        self._depth_checked = time.time()
        text = ""
        try:
            cache_dir = Path(self.window.project_root) / "state" / "cache" / "tmp_quick_dir"
            counts = [len(list(d.glob("*.wem"))) for d in cache_dir.iterdir() if d.is_dir()]
            target = int(self.window.env_data.get("runtime", {}).get("quick_cache_max", 16))
            if counts:
                text = f" · {sum(counts) / len(counts):.1f}/{target} iterations pre-cached"
        except OSError:
            pass
        self._depth_text = text
        return text

    def _refresh(self):
        if self._flash_text is not None:
            if time.monotonic() < self._flash_until:
                return  # a flashed message owns the line until it expires
            self._flash_text = None
        if self.window.setup_pending:
            self._set_state("Status: Setup required - run Setup in the System Environment tab.",
                            "#8a8a8a", False, False, "Setup Required")
            return

        running = is_pipeline_running()
        status = pipeline_status.read_status(self.window.project_root)
        detail = str(status.get("detail", "")).strip()

        # A pipeline we launched that vanished while the port is still bound
        # belongs to someone else now (adopted) - stop tracking it as ours.
        if self._process is not None and self._process.poll() is not None and running:
            self._process = None

        if running:
            if self._stopping:
                if self._stop_deadline is not None and time.time() > self._stop_deadline:
                    # Stop honored too slowly (or ignored): force the process down via
                    # its recorded PID. No modal prompts - the user already asked.
                    pid = status.get("pid")
                    self._force_kill(pid)
                    self._stop_deadline = time.time() + 5  # re-arm; verify death
                self._set_state("Status: Stopping...", "#FBBF24", False, False, "Stopping...")
            else:
                queued = self._read_generation_queue(status)
                depth = self._cache_depth_text()
                if queued > 0:
                    # The engine is synthesizing in the background - say so
                    # instead of a frozen "Ready" (amber = activity).
                    base = (f"Generating {queued} queued variant(s)" if not detail
                            else f"{detail} - {queued} queued")
                    color = "#FBBF24"
                else:
                    base = detail or "listening for game lines"
                    color = "#4ADE80"
                self._set_state(
                    f"Status: Running - {base}{depth}",
                    color, False, True, f"Running ({detail or 'ready'})",
                )
        else:
            self._stop_deadline = None
            self._stopping = False
            self._set_state("Status: Stopped", "#8a8a8a", True, False, "Stopped")
            if self._close_pending is not None:
                callback, self._close_pending = self._close_pending, None
                callback()

    # ------------------------------------------------------------- control
    def owns_pipeline(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _pipeline_start(self):
        if self.window.setup_pending or is_pipeline_running():
            return
        pipeline_status.clear_stop(self.window.project_root)
        log_path = self.window.project_root / "state" / "cache" / "pipeline.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log_path.exists() and log_path.stat().st_size > 5_000_000:
                log_path.write_bytes(b"")  # keep the log from growing forever
        except OSError:
            pass
        if self._log_handle is None or self._log_handle.closed:
            self._log_handle = open(log_path, "ab")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        env = dict(os.environ, PYTHONUNBUFFERED="1")  # live log lines, not block-buffered
        # Prefer the named engine copy (Task Manager clarity); plain pythonw is the fallback.
        engine_exe = self.window.project_root / "venv" / "Scripts" / "NMS_DSV_Engine.exe"
        interpreter = str(engine_exe if engine_exe.is_file() else sys.executable)
        try:
            self._process = subprocess.Popen(
                [interpreter, str(self.window.project_root / "main.py"), "--managed"],
                cwd=str(self.window.project_root),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                env=env,
            )
        except OSError as exc:
            self._process = None
            QMessageBox.critical(self, "Pipeline Error", f"Could not start the pipeline:\n{exc}")

    def _pipeline_stop(self, on_done=None, force_after_s=_STOP_FORCE_AFTER_S):
        if not is_pipeline_running():
            if on_done is not None:
                on_done()
            return
        pipeline_status.request_stop(self.window.project_root)
        self._stopping = True
        self._force_after_s = force_after_s
        self._stop_deadline = time.time() + force_after_s
        self._close_pending = on_done
        self.stop_btn.setEnabled(False)
        self._refresh()

    def _force_kill(self, pid):
        if not pid:
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True, creationflags=creationflags,
        )
