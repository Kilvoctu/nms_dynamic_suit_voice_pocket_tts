# modular/path_utils.py
"""Path resolution helpers. Stdlib-only so pre-setup tooling (manager gating,
setup runner) can import this without pulling numpy/torch."""
from pathlib import Path

# The game's sub-path (game truth - never changes) and the repo's starter-wem copy.
WEM_SUBPATH = Path("DYNAMIC_SUIT_VOICE/AUDIO/WINDOWS/MEDIA/ENGLISH(US)")
STARTER_WEMS_SUBPATH = Path("assets/starter_wems")

PIPELINE_SINGLE_INSTANCE_PORT = 49152


def resolve_mod_wem_dir(user_dir) -> Path:
    """Accepts the MODS root, a DYNAMIC_SUIT_VOICE mid-path, or the full
    ENGLISH(US) folder, and returns the flat folder the pipeline watches."""
    p = Path(user_dir)
    if p.name == WEM_SUBPATH.parts[-1] and WEM_SUBPATH.parts[0] in p.parts:
        return p
    if WEM_SUBPATH.parts[0] in p.parts:
        idx = p.parts.index(WEM_SUBPATH.parts[0])
        return Path(*p.parts[:idx]) / WEM_SUBPATH
    return p / WEM_SUBPATH


def resolve_mod_root(user_dir) -> Path:
    """Inverse of resolve_mod_wem_dir: strips the DYNAMIC_SUIT_VOICE tail and
    returns the simple mod root the user should see and edit in the UI."""
    p = Path(user_dir)
    if WEM_SUBPATH.parts[0] in p.parts:
        idx = p.parts.index(WEM_SUBPATH.parts[0])
        return Path(*p.parts[:idx])
    return p


def is_pipeline_running() -> bool:
    """True when the pipeline's single-instance guard socket (see launch_nms_dsv.py)
    is bound - i.e. a pipeline instance is already running."""
    import socket
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", PIPELINE_SINGLE_INSTANCE_PORT))
        probe.close()
        return False
    except OSError:
        return True


def purge_cache_audio(project_root, env_data: dict) -> int:
    """Delete generated audio from the cache dirs (model weights untouched); returns the file count."""
    paths = env_data.get("paths", {})
    project_root = Path(project_root)
    purged = 0
    for key in ("temp_wav_dir", "temp_wem_dir", "quick_cache_dir"):
        rel = paths.get(key)
        if not rel:
            continue
        target = project_root / str(rel).strip('"')
        if not target.exists():
            continue
        for file in target.rglob("*"):
            if file.is_file():
                file.unlink()
                purged += 1
        for sub in sorted(target.rglob("*"), reverse=True):
            if sub.is_dir():
                try:
                    sub.rmdir()
                except OSError:
                    pass
    return purged
