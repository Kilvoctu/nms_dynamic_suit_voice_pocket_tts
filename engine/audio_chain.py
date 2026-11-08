# engine/audio_chain.py
"""In-memory numpy render chain: float32 array-in/array-out, no disk I/O, no subprocesses.
Order: 40 kHz FX canvas -> speed/pitch (Praat) -> EQ -> FX pre -> loudness -> FX post -> 48k stereo wem (wem_utils fmt-patches it)."""
import numpy as np

from engine import fx

WEM_ENCODER_COMPENSATION_DB = 0.4  # wav2wem encode cost ~0.3-0.4 LU by content; lands encoded within ~0.1 dB of target
MIN_SAMPLES = 100
CONTAINER_SR = 48000
_FX_SR = 40000
CHAIN_VERSION = 8  # bump to force a re-record sweep when the render chain's sound changes
_FX_TAIL_LEAD_SEC = 0.25
_FX_TAIL_REAR_SEC = 0.5


def _resample(y, n):
    from scipy.signal import resample

    return resample(y, max(int(n), MIN_SAMPLES))


def soft_clip(x):
    """Tanh soft-knee above 0.7: squashes peaks without changing quiet-part timbre."""
    x = np.asarray(x, dtype=np.float32)
    out = np.copy(x)
    mask = np.abs(out) > 0.7
    out[mask] = np.sign(out[mask]) * (0.7 + 0.3 * np.tanh((np.abs(out[mask]) - 0.7) / 0.3))
    return out


def _prepare_lufs_input(y, sr, min_duration_sec=0.4):
    """Mono-downmix multichannel input (one loudness convention everywhere), and tile
    short audio so the LUFS meter gets a valid measurement window."""
    y = np.asarray(y)
    if y.ndim > 1:
        y = y.mean(axis=1)
    y = np.asarray(y, dtype=np.float32)
    min_samples = max(1, int(min_duration_sec * sr))
    if 0 < len(y) < min_samples:
        return np.tile(y, int(np.ceil(min_samples / len(y))))[:min_samples]
    return y


def integrated_loudness(y, sr) -> float:
    """BS.1770 integrated loudness (LUFS); -70.0 for silence/unmeasurable audio."""
    try:
        import pyloudnorm as pyln

        val = pyln.Meter(sr).integrated_loudness(_prepare_lufs_input(np.asarray(y, dtype=np.float32), sr))
        return float(val) if np.isfinite(val) else -70.0
    except Exception as exc:
        print(f"[Chain] LUFS measurement failed: {exc}")
        return -70.0


def _fx_has_tail(rack: dict) -> bool:
    """True if the sanitized rack has a slot producing sound beyond the input length."""
    if rack["reverb"]["enabled"]:
        return True
    e = rack["echo"]
    return bool(e["enabled"] and (float(e["feedback"]) > 0.0 or e["taps_ms"]))


def _pad_for_fx_tail(y: np.ndarray, sr: int, rack: dict) -> np.ndarray:
    """Prepend/append silence headroom so reverb/echo tails ring out unabridged."""
    if not _fx_has_tail(rack) or y.size == 0 or sr <= 0:
        return y
    if float(np.max(np.abs(y))) < 1e-5:
        return y
    lead = int(round(_FX_TAIL_LEAD_SEC * sr))
    rear = int(round(_FX_TAIL_REAR_SEC * sr))
    return np.pad(y, (lead, rear), mode="constant")


def _speed_pitch(y, sr, speed: float, pitch_st: float):
    """Praat-based pitch shift (duration restored) and tempo stretch; degrades
    gracefully to unmodified audio if the engine fails."""
    try:
        import parselmouth

        if abs(pitch_st) > 1e-9:
            factor = 2 ** (pitch_st / 12)
            y = _resample(y, len(y) / factor)  # pitch up, duration down
            sound = parselmouth.Sound(np.asarray(y, dtype=np.float64), sampling_frequency=sr)
            y = np.asarray(sound.lengthen(75.0, 600.0, factor).values, dtype=np.float32).flatten()
        if abs(speed - 1.0) > 1e-9:
            sound = parselmouth.Sound(np.asarray(y, dtype=np.float64), sampling_frequency=sr)
            y = np.asarray(sound.lengthen(75.0, 600.0, 1.0 / speed).values, dtype=np.float32).flatten()
    except Exception as exc:
        print(f"[Chain] Speed/pitch skipped ({exc}); continuing unmodified.")
    return np.asarray(y, dtype=np.float32), sr


def _normalize_loudness(y, sr, target: float, true_peak: float):
    """Iterative gain toward the LUFS target (+encoder compensation) with the
    2 dB-headroom soft-clip ceiling; limiting steals loudness, so re-measure."""
    sample_ceiling = max(abs(true_peak) - 2.0, 0.0)
    down = 10 ** (-sample_ceiling / 20)
    up = 10 ** (sample_ceiling / 20)
    for _ in range(5):
        gain_db = target - integrated_loudness(y, sr) + WEM_ENCODER_COMPENSATION_DB
        if abs(gain_db) <= 0.4:
            break
        gain_db = float(np.clip(gain_db, -12.0, 12.0))
        y = soft_clip(y * down * (10 ** (gain_db / 20))) * up
    return np.asarray(y, dtype=np.float32)


def render_chain(audio, sr, preset, loudness_target=None, true_peak=5.0):
    """Full render chain: canvas -> speed/pitch (Praat) -> EQ -> FX pre -> loudness -> FX post -> 48k stereo.
    preset: {"speed", "pitch_st", "volume", "eq_bands", "fx"}; "volume" = per-preset dB trim on `loudness_target`."""
    if loudness_target is None:
        from engine.config import CUSTOMIZATION_DEFAULTS

        loudness_target = float(CUSTOMIZATION_DEFAULTS["voice_effects"]["loudness_target"])
    y = np.asarray(audio, dtype=np.float32).reshape(-1)
    if len(y) < MIN_SAMPLES:
        y = np.pad(y, (0, MIN_SAMPLES - len(y)))
    preset = preset if isinstance(preset, dict) else {}

    if sr != _FX_SR:  # synth arrives at 24 kHz; the rack was tuned against a 40 kHz canvas
        y = _resample(y, len(y) * _FX_SR / sr)
        sr = _FX_SR

    y, sr = _speed_pitch(y, sr, float(preset.get("speed", 1.0) or 1.0),
                         float(preset.get("pitch_st", 0.0) or 0.0))
    eq_bands = fx.sanitize_eq_bands(preset.get("eq_bands"), nyquist=sr / 2.0)
    if eq_bands:
        y = fx.parametric_eq(y, sr, eq_bands)
    rack = fx.sanitize_fx(preset.get("fx"), nyquist=sr / 2.0)
    y = _pad_for_fx_tail(y, sr, rack)
    y = fx.apply_fx_settings(y, sr, rack, "pre")
    try:
        trim = float(preset.get("volume", 0.0) or 0.0)
    except (TypeError, ValueError):
        trim = 0.0
    y = _normalize_loudness(y, sr, float(loudness_target) + float(np.clip(trim, -10.0, 10.0)),
                            float(true_peak))
    y = fx.apply_fx_settings(y, sr, rack, "post")
    if sr != CONTAINER_SR:
        y = _resample(y, len(y) * CONTAINER_SR / sr)
        sr = CONTAINER_SR
    y = np.ascontiguousarray(np.stack([y, y], axis=1))  # vanilla wems are stereo
    return np.asarray(y, dtype=np.float32), sr


def preset_from_legacy(fx_dict) -> dict:
    """Map the old slider block onto a preset: the resample cascade's net pitch
    and pace, EQ/FX retired. Keeps legacy configs sounding the same."""
    fx_dict = fx_dict if isinstance(fx_dict, dict) else {}
    pitch = float(fx_dict.get("pitch_semitones", 0) or 0)
    formant = float(fx_dict.get("formant_percent", 100) or 100)
    rate = float(fx_dict.get("rate", 1.0) or 1.0)
    tempo = float(fx_dict.get("tempo", 1.0) or 1.0)
    pitch_st = pitch + 12 * np.log2(max(formant, 1e-9) / 100) + 12 * np.log2(max(1.8375 * rate, 1e-9))
    duration = (2 ** (-pitch / 12)) * (100 / max(formant, 1e-9)) * (24000 / (44100 * max(rate, 1e-9))) / max(tempo, 1e-9)
    return {"speed": round(1.0 / duration, 4) if duration > 0 else 1.0,
            "pitch_st": round(float(pitch_st), 3), "eq_bands": [], "fx": {}}


def write_wav_float(path, y, sr):
    """Write the chain output as the production container: IEEE float32 wav."""
    from scipy.io import wavfile

    wavfile.write(str(path), int(sr), np.asarray(y, dtype=np.float32))
