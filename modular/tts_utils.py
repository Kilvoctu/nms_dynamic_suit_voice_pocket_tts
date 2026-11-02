# modular/tts_utils.py
import json
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

# Measured: wav2wem's oggenc2 stage costs ~0.9 dB integrated LUFS on float input.
WEM_ENCODER_COMPENSATION_DB = 0.9

def _repair_flattened_hf_links(project_root):
    """Rebuild HF cache symlinks that copy tools flatten into path-text files;
    unresolvable ones are dropped so the online fallback re-downloads them."""
    hub = Path(project_root) / "cache" / "hub"
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
    hub_dir = project_root / "cache" / "hub"
    hub_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(project_root / "cache"))
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


def _offline_fallback_notice():
    global _hub_offline_warned
    if not _hub_offline_warned:
        _hub_offline_warned = True
        print("[TTS] Offline load failed - allowing one online attempt "
              "(run Setup to restore full offline mode).")


def pocket_custom_voice_state_path(project_root, voice: str, model_name: str):
    """Saved state file of a custom cloned voice, or None when absent."""
    folder = Path(project_root) / "user" / "pocket_voices" / voice
    state = folder / POCKET_STATE_DIRNAME / f"{model_name}.safetensors"
    if folder.is_dir() and state.is_file():
        return state
    return None


def list_pocket_voices(project_root, model_name: str = POCKET_DEFAULT_MODEL) -> list:
    """Predefined voices plus custom cloned voices that have a usable saved state."""
    _ensure_pocket_cache_env(project_root)
    voices = list(POCKET_VOICE_GENDERS["Male"] + POCKET_VOICE_GENDERS["Female"])
    voices_dir = Path(project_root) / "user" / "pocket_voices"
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
    temp_wav = final_wav.with_suffix(".temp.wav")

    audio, sample_rate = pocket_generate(
        config.pocket_model_name, config.root_dir, text, config.tts_voice,
        max_tokens=getattr(config, "pocket_tts_max_tokens", 100)
    )
    _write_wav(final_wav, audio, sample_rate)

    if postprocess:
        # Pull your customized voice options directly out of the verified config layer!
        fx = config.voice_effects  # Native user configuration dictionary

        apply_vocal_dsp_chain(
            input_wav=final_wav,
            output_wav=temp_wav,
            gain_db=fx.get("gain_db", 5),
            pitch_semitones=fx.get("pitch_semitones", 4),
            tempo=fx.get("tempo", 1.02),
            rate=fx.get("rate", 0.51),
            ring_freq=fx.get("ring_freq", 300),
            ring_mix=fx.get("ring_mix", 100),
            formant_percent=fx.get("formant_percent", 80),
            loudness_target=fx.get("loudness_target", -5.0),
            loudness_true_peak=fx.get("loudness_true_peak", 5.0)
        )
        temp_wav.replace(final_wav)

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


def _resolve_ffmpeg() -> str:
    """Locate an ffmpeg binary without relying on pre-seeded PATH state."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = Path(__file__).parent.parent / "tools" / "ffmpeg.exe"
    if local.is_file():
        return str(local)
    return "ffmpeg"  # fall through: subprocess will raise a clear error


def _measure_loudness(ffmpeg_bin: str, wav_path: Path, create_no_window: int, pre_filter: str = ""):
    """First loudnorm pass: return the measured JSON stats, or None on failure.
    pre_filter filters are applied before analysis (measures the post-chain audio)."""
    import re
    analysis_chain = f"{pre_filter},loudnorm=print_format=json" if pre_filter else "loudnorm=print_format=json"
    result = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-i", str(wav_path),
         "-af", analysis_chain, "-f", "null", "-"],
        capture_output=True, text=True, creationflags=create_no_window,
    )
    match = re.search(r'\{[^{}]*"input_i"[^{}]*\}', result.stderr or "")
    if not match:
        return None
    try:
        stats = json.loads(match.group(0))
    except Exception:
        return None
    if not stats.get("input_i") or stats["input_i"] in ("-inf",):
        return None
    return stats


def apply_loudness_normalization(ffmpeg_bin: str, wav_path: Path, loudness_target: float,
                                 create_no_window: int = 0, true_peak: float = 5.0,
                                 initial_stats=None, pre_chain: str = "") -> bool:
    """Gain toward the loudness target with a true-peak limiter cap; iterates
    until the target holds (limiting steals loudness, so it re-measures).
    initial_stats: pre-measured stats for the audio pre_chain transforms, saving
    one analysis spawn; pre_chain rides the first apply pass (creative filters)."""
    create_no_window = create_no_window or (0x08000000 if sys.platform == "win32" else 0)
    stats = initial_stats
    if stats is not None:
        try:
            float(stats["input_i"])
        except (KeyError, TypeError, ValueError):
            stats = None
    tmp_path = wav_path.with_suffix(".loudnorm.wav")
    try:
        for _ in range(5):
            if stats is None:
                stats = _measure_loudness(ffmpeg_bin, wav_path, create_no_window)
                if not stats:
                    return False
            try:
                measured_i = float(stats["input_i"])
            except (KeyError, TypeError, ValueError):
                return False
            # Iterate: gain toward the target (+0.9 compensation pre-empts wav2wem's
            # oggenc2 LUFS loss), then a lookahead limiter caps true peaks at the ceiling.
            # Limiting steals some loudness, so measure again and correct, up to 5 passes.
            # alimiter's limit tops out at 1.0, so the signal is scaled down by the
            # ceiling before it and restored after.
            gain_db = loudness_target - measured_i + WEM_ENCODER_COMPENSATION_DB
            if abs(gain_db) <= 0.4 and not pre_chain:
                return True
            gain_db = max(min(gain_db, 12.0), -12.0)
            # Keep 2 dB of sample-peak headroom below the ceiling: hard-clipped
            # speech carries ~1-3 dB of intersample overshoot past sample peaks.
            sample_ceiling = max(true_peak - 2.0, 0.0)
            limiter_chain = (
                f"volume={gain_db:.2f}dB,"
                f"volume={10 ** (-sample_ceiling / 20):.6f},"
                f"alimiter=limit=1:attack=5:release=50,"
                f"volume={10 ** (sample_ceiling / 20):.6f}"
            )
            chain = f"{pre_chain},{limiter_chain}" if pre_chain else limiter_chain
            pre_chain = ""  # creative filters only ride the first apply pass
            result = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-y",
                 "-i", str(wav_path),
                 "-af", chain,
                 "-ar", "22050",
                 "-c:a", "pcm_f32le",  # float output: allows true peak above 0 dBFS
                 str(tmp_path)],
                capture_output=True, text=True, creationflags=create_no_window,
            )
            if result.returncode != 0 or not tmp_path.is_file():
                if tmp_path.exists():
                    tmp_path.unlink()
                return False
            tmp_path.replace(wav_path)
            stats = None  # the audio changed: re-measure next iteration
        return True
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def apply_vocal_dsp_chain(input_wav: Path, output_wav: Path,
                          gain_db, pitch_semitones, tempo, rate,
                          ring_freq, ring_mix, formant_percent,
                          loudness_target=-5.0, loudness_true_peak=5.0,
                          target_sr=22050):

    # 1. LOAD RAW BYTES NATIVELY INTO NUMPY ARRAY
    with wave.open(str(input_wav), "rb") as wf:
        sr = wf.getframerate()
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32)
        if wf.getnchannels() > 1:
            audio = audio[::wf.getnchannels()]  # Enforce structural mono

    if len(audio) < MIN_SAMPLES:
        audio = np.pad(audio, (0, MIN_SAMPLES - len(audio)))

    # 2. RUN MATRIX RESAMPLING IN THE CORRECT PASSED SANDBOX SEQUENCE
    # Imported lazily: scipy.signal costs ~1s at import time and must not slow app startup.
    from scipy.signal import resample

    # A. Pitch Frequency Shift
    factor_pitch = 2 ** (pitch_semitones / 12)
    n_samples_pitch = max(int(len(audio) / factor_pitch), MIN_SAMPLES)
    audio = resample(audio, n_samples_pitch)

    # B. Throat Resonator Formant Shift
    factor_formant = formant_percent / 100
    n_samples_formant = max(int(len(audio) / factor_formant), MIN_SAMPLES)
    audio = resample(audio, n_samples_formant)

    # C. Cybernetic Overlay Blending (Dry/Wet Matrix)
    wet_factor = ring_mix / 100.0
    dry_factor = 1.0 - wet_factor

    dry_signal = audio.copy()
    t = np.arange(len(audio)) / sr
    wet_signal = audio * np.sin(2 * np.pi * ring_freq * t)

    audio = (dry_signal * dry_factor) + (wet_signal * wet_factor)

    # Write processed array steps back to a temporary disk target
    if np.max(np.abs(audio)) > 0:
        audio = audio / np.max(np.abs(audio)) * 32767
    audio_bytes = audio.astype(np.int16).tobytes()

    with wave.open(str(output_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(audio_bytes)

    # 3. One ffmpeg pass: the creative chain (volume/tempo/rate) rides into the
    #    first loudnorm apply instead of its own spawn; loudness is measured with
    #    the chain pre-applied so the linear gain math stays exact. The apply's
    #    -ar 22050 locks the game-engine sample rate (replaces the old resample).
    CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
    setrate_val = int(44100 * rate)
    ffmpeg_bin = _resolve_ffmpeg()
    pre_chain = f"volume={gain_db}dB,atempo={tempo},asetrate={setrate_val}"
    stats = _measure_loudness(ffmpeg_bin, output_wav, CREATE_NO_WINDOW, pre_filter=pre_chain)
    apply_loudness_normalization(
        ffmpeg_bin, output_wav, loudness_target, CREATE_NO_WINDOW,
        true_peak=loudness_true_peak, initial_stats=stats, pre_chain=pre_chain
    )
