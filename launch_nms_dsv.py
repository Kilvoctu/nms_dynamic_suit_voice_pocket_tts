# launch_nms_dsv.py
import os
import sys
import socket

# ============ IMMEDIATE SINGLE-INSTANCE GUARD ============
try:
    _instance_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _instance_lock.bind(("127.0.0.1", 49152))
    _instance_lock.listen(1)  # bind alone doesn't hold the port on Windows
except socket.error:
    os._exit(0)  # sys.exit can hang in pythonw; hard-exit keeps zombies away

import threading
import time
from pathlib import Path
from PIL import Image
import pystray

import modular.pipeline_status as pipeline_status


class _LineStamper:
    """Prefix every complete stdout/stderr line with a local timestamp so the
    pipeline log shows when each event happened (writes may arrive in pieces)."""

    def __init__(self, stream):
        self._stream = stream
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._stream.write(time.strftime("[%H:%M:%S] ") + line + "\n")

    def flush(self):
        if self._buf:
            line, self._buf = self._buf, ""
            self._stream.write(time.strftime("[%H:%M:%S] ") + line + "\n")
        self._stream.flush()

    def isatty(self):
        return False


if sys.stdout is not None:
    sys.stdout = _LineStamper(sys.stdout)
if sys.stderr is not None:
    sys.stderr = _LineStamper(sys.stderr)


ROOT = Path(__file__).parent
MANAGED = "--managed" in sys.argv

pipeline_status.clear_stop(ROOT)


def _sync_title(title: str) -> None:
    """Mirror a tray-style title into the manager-visible status file."""
    pipeline_status.sync_title(ROOT, title)


class _TitleSyncIconProxy:
    """Forwards attribute access to the pystray icon, mirroring title writes
    into the status file so the manager can display a tray-launched pipeline."""

    def __init__(self, icon):
        object.__setattr__(self, "_icon", icon)

    def __getattr__(self, name):
        return getattr(self._icon, name)

    def __setattr__(self, name, value):
        if name == "title":
            setattr(self._icon, name, value)
            _sync_title(value)
        else:
            setattr(self._icon, name, value)


class _ManagedIconHandle:
    """Headless pystray stand-in: title writes publish to the status file
    (until shutdown); image swaps are accepted and ignored."""

    def __init__(self, owner):
        self._owner = owner
        self._title = "Dynamic Suit Voice - Starting Core Pipeline..."
        self.icon = None  # receives the ready RGBA image in pipeline Phase 4

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value
        if not self._owner._shutdown:
            _sync_title(value)


class ManagedPipeline:
    """--managed mode: no tray, no console. Runs the same boot thread, publishes
    state to the status file, and exits gracefully on a manager stop request."""

    def __init__(self):
        self.ready_icon_path = ROOT / "assets" / "nms_dsv.png"
        self.icon = _ManagedIconHandle(self)
        self._stopped = False
        self._shutdown = False
        self._boot_thread = None

    @property
    def running(self):
        if not self._stopped and pipeline_status.is_stop_requested(ROOT):
            self._stopped = True
        return not self._stopped

    def boot_pipeline_thread(self):
        print("Launcher: Managed mode active. Invoking heavy engine components...")

        from modular.config import SuitVoiceConfig
        from nms_dynamic_suit_voice_pipeline import watch_wems

        config = SuitVoiceConfig(init_llm=False)
        watch_wems(self, config)

    def run(self):
        pipeline_status.write_status(ROOT, "running", "Starting core pipeline...")
        self._boot_thread = threading.Thread(target=self.boot_pipeline_thread, daemon=True)
        self._boot_thread.start()
        # Wait for the boot thread to wind down fully (a stop request makes the
        # watch loop exit) so no late title write can clobber the final status.
        while self._boot_thread.is_alive():
            time.sleep(0.25)
        self._shutdown = True
        stopped_by_request = pipeline_status.is_stop_requested(ROOT)
        if stopped_by_request:
            pipeline_status.clear_stop(ROOT)
            pipeline_status.write_status(ROOT, "stopped", "Stopped from the manager.")
        elif pipeline_status.read_status(ROOT).get("state") != "error":
            pipeline_status.write_status(ROOT, "stopped", "Pipeline exited.")
        print("Launcher: Pipeline stopped.")


class FastBootTray:
    def __init__(self):
        self.running = True

        # Single image source target
        self.ready_icon_path = ROOT / "assets" / "nms_dsv.png"

        try:
            if self.ready_icon_path.exists():
                base_img = Image.open(self.ready_icon_path).convert("RGBA")
            else:
                raise FileNotFoundError()
        except Exception:
            base_img = Image.new('RGBA', (64, 64), color=(0, 0, 0, 0))

        # Paint the dynamic startup background beneath the transparent icon
        w, h = base_img.size
        bg = Image.new("RGBA", (w, h), color=(220, 20, 60, 255))  # Solid colour block

        # Merge the transparent art cleanly on top of the shape
        init_img = Image.alpha_composite(bg, base_img).convert("RGB")

        icon = pystray.Icon(
            "NMS_DynamicSuitVoice",
            init_img,
            title="Dynamic Suit Voice - Starting Core Pipeline...",
            menu=pystray.Menu(pystray.MenuItem('Quit', self.on_quit))
        )
        # Proxy so every title write also lands in the manager status file
        self.icon = _TitleSyncIconProxy(icon)

    def on_quit(self, _icon, _item):
        self.running = False
        self.icon.stop()
        pipeline_status.write_status(ROOT, "stopped", "Stopped from the tray.")

    def boot_pipeline_thread(self):
        """Runs inside a background worker thread where heavy 20-second imports are safe."""
        print("Launcher: Fast boot active. Invoking heavy engine components...")

        # Delayed import: keeps startup fast so the icon appears immediately.
        from modular.config import SuitVoiceConfig
        from nms_dynamic_suit_voice_pipeline import watch_wems

        # Instantiate the configuration class now that the tray is already visible
        config = SuitVoiceConfig(init_llm=False)

        # Hand off execution control smoothly to the existing core loop
        watch_wems(self, config)

    def run(self):
        # Spin up the background compiler/initialization thread
        threading.Thread(target=self.boot_pipeline_thread, daemon=True).start()
        # Open the system tray icon instantly (0-second delay)
        self.icon.run()


if __name__ == "__main__":
    app = ManagedPipeline() if MANAGED else FastBootTray()
    app.run()
