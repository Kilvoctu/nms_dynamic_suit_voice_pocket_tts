# engine/pipeline_status.py
"""Status/stop channel between the pipeline process and the manager UI (stdlib-only)."""
import json
import os
import time
from pathlib import Path

from engine.path_utils import project_root as PROJECT_ROOT  # write-able anchor (exe dir when frozen)

_TITLE_PREFIX = "Dynamic Suit Voice - "


def status_path(project_root=PROJECT_ROOT) -> Path:
    return Path(project_root) / "state" / "cache" / "pipeline_status.json"


def stop_flag_path(project_root=PROJECT_ROOT) -> Path:
    return Path(project_root) / "state" / "cache" / "pipeline_stop.flag"


def write_status(project_root, state: str, detail: str = "") -> None:
    """Atomically publish the pipeline state. Failures are non-fatal: the
    status file is a convenience channel, never a correctness dependency."""
    path = status_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({
                "state": state,
                "detail": detail,
                "pid": os.getpid(),
                "updated": time.time(),
            }),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


def read_status(project_root=PROJECT_ROOT) -> dict:
    try:
        return json.loads(status_path(project_root).read_text(encoding="utf-8"))
    except Exception:
        return {}


def sync_title(project_root, title: str) -> None:
    """Map a tray-style status title ('Dynamic Suit Voice - X') to a status entry;
    shared by the tray proxy and the managed handle so every mode reports alike."""
    detail = title
    state = "running"
    if title.startswith(_TITLE_PREFIX):
        detail = title[len(_TITLE_PREFIX):]
    if detail.startswith("Error"):
        state = "error"
        detail = detail.split(":", 1)[1].strip() if ":" in detail else detail
    write_status(project_root, state, detail)


def request_stop(project_root=PROJECT_ROOT) -> None:
    """Ask the pipeline to exit gracefully (checked each watch-loop pass)."""
    try:
        stop_flag_path(project_root).parent.mkdir(parents=True, exist_ok=True)
        stop_flag_path(project_root).write_text("", encoding="utf-8")
    except OSError:
        pass


def is_stop_requested(project_root=PROJECT_ROOT) -> bool:
    return stop_flag_path(project_root).exists()


def clear_stop(project_root=PROJECT_ROOT) -> None:
    try:
        stop_flag_path(project_root).unlink()
    except OSError:
        pass
