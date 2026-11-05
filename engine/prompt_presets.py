# engine/prompt_presets.py
"""User-defined prompt presets (tones, verbiage, base prompt). Stdlib-only so
both the manager UI and the pipeline config share one source of truth."""
import json
import os
from pathlib import Path

PRESETS_FILE = Path("state") / "user" / "prompt_presets.json"
SECTIONS = ("tones", "wordiness")  # sections merged into the prompt engine
BASE_SECTION = "base"  # {name: {text, description}} - full base prompt rewrites
BASE_ACTIVE_KEY = "base_active"  # name of the active base variant ("Default" = built-in)
ACTIVE_DEFAULT = "Default"


def presets_path(root_dir) -> Path:
    """Absolute path of the user preset file for the given project root."""
    return Path(root_dir) / PRESETS_FILE


def _clean_entry(entry, text_key: str):
    """Normalize one preset entry to {text_key, description} ('' values kept)."""
    return {
        text_key: str(entry.get(text_key, "")),
        "description": str(entry.get("description", "")),
    }


def load_presets(root_dir) -> dict:
    """Read user presets; returns tones/wordiness plus the base section and the
    active base name. Missing file or bad JSON yields the all-default shape."""
    data = {"tones": {}, "wordiness": {}, BASE_SECTION: {}, BASE_ACTIVE_KEY: ACTIVE_DEFAULT}
    path = presets_path(root_dir)
    if not path.is_file():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Corrupt file: keep a recovery copy so a later save cannot destroy it.
        try:
            backup = path.with_suffix(path.suffix + ".corrupt")
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
        except OSError:
            pass
        return data
    if not isinstance(raw, dict):
        return data
    for section in SECTIONS:
        entries = raw.get(section, {})
        if isinstance(entries, dict):
            for name, entry in entries.items():
                if isinstance(entry, dict) and entry.get("prompt", "").strip():
                    data[section][str(name)] = _clean_entry(entry, "prompt")
    base = raw.get(BASE_SECTION, {})
    if isinstance(base, dict):
        for name, entry in base.items():
            if isinstance(entry, dict) and entry.get("text", "").strip():
                data[BASE_SECTION][str(name)] = _clean_entry(entry, "text")
    active = raw.get(BASE_ACTIVE_KEY, ACTIVE_DEFAULT)
    if isinstance(active, str) and (active == ACTIVE_DEFAULT or active in data[BASE_SECTION]):
        data[BASE_ACTIVE_KEY] = active
    return data


def save_presets(root_dir, data: dict) -> None:
    """Atomically write the full preset dict (all sections), creating user/ as
    needed - a crash mid-write can never leave a half-written file behind."""
    path = presets_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {section: dict(data.get(section) or {}) for section in SECTIONS}
    clean[BASE_SECTION] = dict(data.get(BASE_SECTION) or {})
    clean[BASE_ACTIVE_KEY] = str(data.get(BASE_ACTIVE_KEY) or ACTIVE_DEFAULT)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(clean, indent=4), encoding="utf-8")
    os.replace(tmp_path, path)


def merge_promptdata(builtin: dict, presets: dict) -> dict:
    """Combine built-in and user preset sections; user entries never override
    built-ins. Non-section keys (per-category context) are carried through."""
    merged = {k: v for k, v in (builtin or {}).items() if k not in (SECTIONS + (BASE_SECTION, BASE_ACTIVE_KEY))}
    for section in SECTIONS:
        base = dict((builtin or {}).get(section, {}))
        for name, entry in (presets.get(section) or {}).items():
            if name in base:
                continue
            base[name] = entry["prompt"] if isinstance(entry, dict) else entry
        merged[section] = base
    return merged


def resolve_base(builtin_text: str, presets: dict) -> str:
    """Return the active base prompt text: the selected user variant, or the built-in."""
    presets = presets or {}
    active = presets.get(BASE_ACTIVE_KEY, ACTIVE_DEFAULT)
    entry = presets.get(BASE_SECTION, {}).get(active)
    if active != ACTIVE_DEFAULT and isinstance(entry, dict):
        return entry.get("text", builtin_text)
    return builtin_text
