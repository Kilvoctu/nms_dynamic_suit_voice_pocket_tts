# engine/path_utils.py
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
    """True when the pipeline's single-instance guard socket (see engine/launcher.py)
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


# Gemma 4 rephrasing model variants (System Environment tab selection).
# Values: dir/file = GGUF location under the repo, url = Setup download source,
# label = Advanced Settings display text.
LLM_VARIANTS = {
    "e2b": {
        "dir": "assets/gemma4_e2b",
        "file": "gemma-4-E2B-it-Q4_K_M.gguf",
        "url": "https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/main/gemma-4-E2B-it-Q4_K_M.gguf",
        "label": "E2B (fast baseline)",
    },
    "e4b": {
        "dir": "assets/gemma4_e4b",
        "file": "gemma-4-E4B-it-Q4_K_M.gguf",
        "url": "https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_K_M.gguf",
        "label": "E4B (richer lines, 3x slower)",
    },
}
DEFAULT_LLM_VARIANT = "e2b"


def active_llm_variant(environment: dict, user_cfg: dict) -> str:
    """Merged llm_variant choice (untracked user override over the tracked default)."""
    models = {**environment.get("models", {}), **user_cfg.get("models", {})}
    variant = str(models.get("llm_variant", DEFAULT_LLM_VARIANT)).strip().lower()
    return variant if variant in LLM_VARIANTS else DEFAULT_LLM_VARIANT


def llm_variant_gguf(project_root, variant: str) -> Path:
    """Absolute GGUF path for a known variant id."""
    meta = LLM_VARIANTS[variant]
    return Path(project_root) / meta["dir"] / meta["file"]


def unused_llm_variant_file(project_root, environment: dict, user_cfg: dict) -> Path:
    """GGUF of the variant that is NOT selected (purge target); caller checks existence."""
    other = next(k for k in LLM_VARIANTS if k != active_llm_variant(environment, user_cfg))
    return llm_variant_gguf(project_root, other)
