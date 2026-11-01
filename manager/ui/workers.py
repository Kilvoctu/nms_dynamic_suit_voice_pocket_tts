# manager/ui/workers.py - background task runners so the GUI never blocks.
from PySide6.QtCore import QThread, Signal


class FunctionThread(QThread):
    """Runs a plain callable on a QThread; streams log lines and reports the
    outcome via signals. Used for setup, warmup, top-up and the sandbox test."""

    finished_ok = Signal(object)
    failed = Signal(str)
    log_line = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def emit_log(self, message: str):
        """Thread-safe logging: emits the signal (queued connection), so the
        GUI is only ever touched on the GUI thread."""
        self.log_line.emit(str(message))

    def run(self):
        try:
            result = self._fn()
            self.finished_ok.emit(result)
        except Exception as err:
            self.failed.emit(f"{type(err).__name__}: {err}")
