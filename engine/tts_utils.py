# engine/tts_utils.py
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

MIN_SAMPLES = 1024  # absolute minimum number of samples per channel

# ====================================================
# POCKET TTS ENGINE
POCKET_VOICE_GENDERS = {
    "Male": ["alba", "marius", "javert", "jean"],
    "Female": ["fantine", "cosette", "eponine", "azelma"],
}
PREDEFINED_POCKET_VOICES = frozenset(
    POCKET_VOICE_GENDERS["Male"] + POCKET_VOICE_GENDERS["Female"]
)
POCKET_STATE_DIRNAME = "_model_states"
POCKET_DEFAULT_MODEL = "english_2026-10"
POCKET_DEFAULT_VOICE = "fantine"

_torch_threads = 0  # generation CPU cap for torch renders; 0 = torch default (all cores)


def set_torch_threads(n):
    """Cap torch CPU threads used by TTS renders; applies live if torch is already loaded."""
    global _torch_threads
    _torch_threads = max(1, int(n))
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            torch.set_num_threads(_torch_threads)
        except Exception:
            print("[TTS] torch thread cap not applied this moment.")


def _repair_flattened_hf_links(project_root):
    """Rebuild HF cache symlinks that copy tools flatten into path-text files;
    unresolvable ones are dropped so the online fallback re-downloads them."""
    hub = Path(project_root) / "state" / "cache" / "hub"
    if not hub.is_dir():
        return
    for path in list(hub.rglob("*")):
        if path.is_dir() or path.is_symlink():
            continue
        try:
            if path.stat().st_size > 400:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if not text or "\n" in text or not text.startswith(".."):
            continue
        target = (path.parent / text).resolve()
        if not target.exists():
            path.unlink(missing_ok=True)
            print(f"[TTS] Removed broken cache link {path} (flattened); it will re-download.")
            continue
        if target.is_file():
            shutil.copy2(target, path)
        else:
            path.unlink(missing_ok=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)],
                           capture_output=True, creationflags=flags)
        print(f"[TTS] Repaired flattened cache link: {path.name}")


_pocket_model = None
_pocket_voice_states = {}
_pocket_model_lock = threading.Lock()
_pocket_gen_lock = threading.Lock()


def _ensure_pocket_cache_env(project_root):
    """Route HuggingFace downloads into the repo's gitignored cache dir and
    quiet the noisy unauthenticated-request warning (logged and printed)."""
    import logging

    project_root = Path(project_root)
    hub_dir = project_root / "state" / "cache" / "hub"
    hub_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(project_root / "state" / "cache"))
    os.environ.setdefault("HF_HUB_CACHE", str(hub_dir))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(hub_dir))
    logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)


_hub_offline_warned = False


def _set_hub_offline(offline: bool) -> None:
    """Strict runtime-offline mode: HF hub otherwise phones home for an etag
    check on every load; a failed offline load gets one online fallback."""
    flag = "1" if offline else "0"
    os.environ["HF_HUB_OFFLINE"] = flag
    os.environ["TRANSFORMERS_OFFLINE"] = flag


class _CloneLogHandler(logging.Handler):
    """Routes pocket_tts progress logs to a callback (the clone UI's signal)."""

    def __init__(self, cb):
        super().__init__()
        self._cb = cb

    def emit(self, record):
        try:
            self._cb(record.getMessage())
        except Exception:
            pass


def clone_pocket_voice(sample_path, dest_path, model_name, project_root, log_cb=None):
    """Build a cloned-voice state from any pocket-tts-readable audio (wav, mp3,
    flac, ogg) - mirrors the pocket-tts export-voice CLI, in-process. Offline
    first (the cloning model usually sits in the shared HF cache); one online
    fallback fetch only when the cache misses."""
    _ensure_pocket_cache_env(project_root)
    token_file = Path.home() / ".cache" / "huggingface" / "token"
    if not os.environ.get("HF_TOKEN") and token_file.is_file():
        os.environ["HF_TOKEN"] = token_file.read_text(encoding="utf-8").strip()

    from pocket_tts import TTSModel
    from pocket_tts.models.model_state import export_model_state

    def _export() -> None:
        model = TTSModel.load_model(language=model_name)
        state = model.get_state_for_audio_prompt(audio_conditioning=str(sample_path), truncate=True)
        export_model_state(state, str(dest_path))

    logger = logging.getLogger("pocket_tts")
    handler = _CloneLogHandler(log_cb) if log_cb else None
    old_level = logger.level
    if handler is not None:
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    try:
        _set_hub_offline(True)  # cache-only first: offline clones are the norm
        try:
            _export()
        except Exception:
            _set_hub_offline(False)  # cloning model not cached - one online fetch
            if log_cb:
                log_cb("Cloning model not found in the local cache - fetching it once...")
            _export()
    finally:
        _set_hub_offline(True)
        if handler is not None:
            logger.removeHandler(handler)
            logger.setLevel(old_level)


def _offline_fallback_notice():
    global _hub_offline_warned
    if not _hub_offline_warned:
        _hub_offline_warned = True
        print("[TTS] Offline load failed - allowing one online attempt "
              "(run Setup to restore full offline mode).")


def pocket_custom_voice_state_path(project_root, voice: str, model_name: str):
    """Saved state file of a custom cloned voice, or None when absent."""
    folder = Path(project_root) / "state" / "user" / "pocket_voices" / voice
    state = folder / POCKET_STATE_DIRNAME / f"{model_name}.safetensors"
    if folder.is_dir() and state.is_file():
        return state
    return None


def list_pocket_voices(project_root, model_name: str = POCKET_DEFAULT_MODEL) -> list:
    """Predefined voices plus custom cloned voices that have a usable saved state."""
    _ensure_pocket_cache_env(project_root)
    voices = list(POCKET_VOICE_GENDERS["Male"] + POCKET_VOICE_GENDERS["Female"])
    voices_dir = Path(project_root) / "state" / "user" / "pocket_voices"
    if voices_dir.is_dir():
        for subfolder in sorted(voices_dir.iterdir()):
            if subfolder.is_dir() and subfolder.name not in voices:
                if pocket_custom_voice_state_path(project_root, subfolder.name, model_name):
                    voices.append(subfolder.name)
    return voices


def get_pocket_model(model_name: str = POCKET_DEFAULT_MODEL, project_root="."):
    """Lazy-load the shared Pocket TTS model, strictly offline. A failed offline
    load (cache wiped, Setup skipped) falls back to one online self-heal attempt."""
    global _pocket_model
    if _pocket_model is not None:
        return _pocket_model
    with _pocket_model_lock:
        if _pocket_model is None:
            _ensure_pocket_cache_env(project_root)
            try:
                _repair_flattened_hf_links(project_root)
            except Exception as r_e:
                print(f"[TTS] Cache link check skipped: {r_e}")
            # Imported here so importing this module never pulls in torch.
            from pocket_tts import TTSModel
            if _torch_threads:
                import torch
                torch.set_num_threads(_torch_threads)

            print(f"[TTS] Loading Pocket TTS model '{model_name}' (offline)...")
            started = time.monotonic()
            _set_hub_offline(True)
            try:
                _pocket_model = TTSModel.load_model(language=model_name)
            except Exception:
                _offline_fallback_notice()
                _set_hub_offline(False)
                _pocket_model = TTSModel.load_model(language=model_name)
            print(f"[TTS] Pocket TTS model ready in {time.monotonic() - started:.1f}s")
    return _pocket_model


def _resolve_state_with_fallback(model, source):
    """Resolve one voice state offline-first; one online retry if missing."""
    _set_hub_offline(True)
    try:
        try:
            return model.get_state_for_audio_prompt(source)
        except Exception:
            _offline_fallback_notice()
            _set_hub_offline(False)
            return model.get_state_for_audio_prompt(source)
    finally:
        _set_hub_offline(True)


def resolve_pocket_voice_state(model, voice: str, model_name: str = POCKET_DEFAULT_MODEL, project_root="."):
    """Resolve and cache the conditioning state for one voice (predefined name or cloned folder)."""
    with _pocket_model_lock:
        state = _pocket_voice_states.get(voice)
        if state is None:
            custom_state = pocket_custom_voice_state_path(project_root, voice, model_name)
            if custom_state is not None:
                print(f"[TTS] Resolving cloned voice '{voice}' state...")
                state = _resolve_state_with_fallback(model, str(custom_state))
            elif voice in PREDEFINED_POCKET_VOICES:
                state = _resolve_state_with_fallback(model, voice)
            else:
                known = ", ".join(list_pocket_voices(project_root, model_name))
                raise ValueError(
                    f"Unknown Pocket voice '{voice}'. Available voices: {known}. "
                    "Cloned voices need a saved state under user/pocket_voices/<voice>/."
                )
            _pocket_voice_states[voice] = state
    return state


def pocket_generate(model_name: str, project_root, text: str, voice: str, max_tokens: int = 100):
    """Synthesize one line as (float32 mono audio, sample_rate); max_tokens caps the audio-frame budget."""
    model = get_pocket_model(model_name, project_root)
    state = resolve_pocket_voice_state(model, voice, model_name, project_root)
    with _pocket_gen_lock:
        audio = model.generate_audio(state, text, max_tokens=max_tokens, copy_state=True)
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    return trim_pocket_silence(audio, int(model.sample_rate)), int(model.sample_rate)


def warm_pocket_engine(model_name: str, project_root, voice: str = POCKET_DEFAULT_VOICE):
    """Pre-load the model and one voice state so the first real line skips warmup latency."""
    model = get_pocket_model(model_name, project_root)
    resolve_pocket_voice_state(model, voice, model_name, project_root)
    return model


def warm_all_pocket_voices(model_name: str, project_root):
    """Resolve the conditioning state for every built-in and cloned voice up front
    (setup time), so the pipeline never touches the network mid-game."""
    model = get_pocket_model(model_name, project_root)
    for voice in list_pocket_voices(project_root, model_name):
        try:
            resolve_pocket_voice_state(model, voice, model_name, project_root)
            print(f"[TTS] Voice '{voice}' ready.")
        except Exception as exc:
            print(f"[TTS] Voice '{voice}' failed to warm: {exc}")
    return model


def trim_pocket_silence(audio, sr, lead_ms: float = 80.0, tail_ms: float = 200.0):
    """Trim the silence Pocket TTS pads around a generation, keeping small margins."""
    y = np.asarray(audio, dtype=np.float32)
    if y.ndim != 1 or y.size == 0:
        return audio
    threshold = max(0.004, 0.01 * float(np.max(np.abs(y))))
    nz = np.where(np.abs(y) > threshold)[0]
    if nz.size == 0:
        return audio
    start = max(0, int(nz[0]) - int(lead_ms / 1000.0 * sr))
    end = min(y.size, int(nz[-1]) + 1 + int(tail_ms / 1000.0 * sr))
    if start <= 0 and end >= y.size:
        return audio
    return y[start:end].copy()


def run_tts(config, text: str, wem_num: str, postprocess: bool = True) -> Path:
    final_wav = config.temp_wav_dir / f"{wem_num}.wav"

    audio, sample_rate = pocket_generate(
        config.pocket_model_name, config.root_dir, text, config.tts_voice,
        max_tokens=getattr(config, "pocket_tts_max_tokens", 100)
    )

    if postprocess:
        from engine.audio_chain import render_chain, write_wav_float

        audio, sample_rate = render_chain(
            audio, sample_rate, getattr(config, "eqfx_active", None) or {},
            loudness_target=config.voice_effects.get("loudness_target"),
            true_peak=float(config.voice_effects.get("loudness_true_peak", 5.0)))
        write_wav_float(final_wav, audio, sample_rate)
    else:
        _write_wav(final_wav, audio, sample_rate)

    return final_wav


def _write_wav(path: Path, audio, sample_rate: int):
    """Write float32 mono audio as a 16-bit PCM wav."""
    audio = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    if len(audio) < MIN_SAMPLES:
        audio = np.pad(audio, (0, MIN_SAMPLES - len(audio)))
    samples = (audio * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(samples.tobytes())

