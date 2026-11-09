# manager/main.py - PySide6 entry point for the DSV Management Utility.
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.path_utils import assets_root, is_frozen, project_root

# Under pythonw.exe (and the consoleless frozen exe) there is no console:
# sys.stdout/stderr are None. Route them to a file so prints, tracebacks and
# Qt warnings stay recoverable.
if sys.stdout is None or sys.stderr is None:
    try:
        _stdio_log = project_root() / "state" / "cache" / "manager_stdout.log"
        _stdio_log.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = open(_stdio_log, "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    except OSError:
        pass


def _fail(exc: BaseException) -> None:
    """Last-resort crash reporter: traceback to the console, crash log, and a dialog when Qt is importable."""
    detail = traceback.format_exc()
    print(detail, file=sys.stderr)
    try:
        log_dir = project_root() / "state" / "cache"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "manager_crash.log").write_text(detail, encoding="utf-8")
        _boot_log(f"CRASH: {detail}")
    except OSError:
        pass
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            QApplication(sys.argv)
        QMessageBox.critical(
            None, "NMS DSV Manager",
            f"The Management Utility failed to start:\n\n{exc}\n\n"
            f"Full details: cache\\manager_crash.log",
        )
    except Exception:
        pass
    sys.exit(1)


def _boot_log(message: str) -> None:
    """Append a boot milestone so a native crash still shows the last completed stage."""
    try:
        log_dir = project_root() / "state" / "cache"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "manager_boot.log", "a", encoding="utf-8") as f:
            f.write(f"{message}\n")
    except OSError:
        pass


def _acquire_instance_lock():
    """Single-instance lock beside the app; per-user app-data as a fallback."""
    from PySide6.QtCore import QLockFile, QStandardPaths

    lock_dir = project_root()
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        lock_dir = Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
            or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
        )
        lock_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_dir / "nms_dsv_manager.lock"))
    return lock if lock.tryLock(0) else None


def _main() -> None:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ui.main_window import MainWindow
    from ui.style import APP_QSS

    if not is_frozen():  # the frozen exe always runs its bundled interpreter
        expected_dir = (project_root() / "venv" / "Scripts").resolve()
        if Path(sys.executable).resolve().parent != expected_dir:
            _boot_log(f"WRONG INTERPRETER: {sys.executable}")
            _ = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(
                None, "NMS DSV Manager",
                "The Management Utility was launched outside its virtual environment.\n\n"
                "Please start it with the project venv (python main.py - see README).",
            )
            sys.exit(1)

    _boot_log("--- manager session start ---")
    _boot_log("imports ok")

    def _event_loop_hook(exc_type, exc, tb):
        """PySide6 aborts natively on unhandled slot exceptions; this hook still logs the traceback."""
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        print(detail, file=sys.stderr)
        try:
            (project_root() / "state" / "cache" / "manager_crash.log").write_text(
                detail, encoding="utf-8"
            )
            _boot_log(f"EVENT LOOP CRASH: {exc_type.__name__}: {exc}")
        except OSError:
            pass

    sys.excepthook = _event_loop_hook

    app = QApplication(sys.argv)
    app.setApplicationName("NMS Dynamic Suit Voice")
    app.setWindowIcon(QIcon(str(assets_root() / "assets" / "nms_dsv.png")))
    app.setStyleSheet(APP_QSS)  # group-box bevels + slim scrollbars, app-wide
    _boot_log("QApplication created")

    lock = _acquire_instance_lock()
    if lock is None:
        QMessageBox.warning(None, "NMS DSV Manager", "The Management Utility is already running.")
        sys.exit(0)
    _boot_log("instance lock acquired")

    window = MainWindow()
    _boot_log("MainWindow constructed")
    window.show()
    _boot_log("window shown; entering event loop")
    sys.exit(app.exec())


def main() -> None:
    try:
        _main()
    except SystemExit:
        raise
    except BaseException as exc:
        _fail(exc)


if __name__ == "__main__":
    main()
