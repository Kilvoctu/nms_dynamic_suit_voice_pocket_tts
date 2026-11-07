# engine/audio_chain.py
"""In-memory numpy render chain: float32 array-in/array-out, no disk I/O, no subprocesses.
Sculpt -> gain -> tempo (Praat) -> rate shift -> BS.1770 loudness -> 22050 Hz container."""
import numpy as np

WEM_ENCODER_COMPENSATION_DB = 0.9  # wav2wem loses ~0.9 LU in encode; pre-empt it
MIN_SAMPLES = 100
CONTAINER_SR = 22050  # the game/wem contract: final renders land at this rate
CHAIN_VERSION = 2  # bump to force a re-record sweep when the render chain's sound changes


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
    """Tile short audio so the LUFS meter gets a valid measurement window."""
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


def apply_sculpt(y, sr, fx):
    """The suit-voice sculpt: pitch/formant resamples and the ring-mod overlay.
    No-op stages (pitch 0, formant 100%, ring mix 0) are skipped entirely."""
    pitch = float(fx.get("pitch_semitones", 0) or 0)
    if abs(pitch) > 1e-9:
        y = _resample(y, len(y) / (2 ** (pitch / 12)))
    formant = float(fx.get("formant_percent", 100) or 100)
    if abs(formant - 100) > 1e-9:
        y = _resample(y, len(y) / (formant / 100))
    ring_mix = float(fx.get("ring_mix", 0) or 0)
    if ring_mix > 0:
        t = np.arange(len(y), dtype=np.float64) / sr
        wet = y * np.sin(2 * np.pi * float(fx.get("ring_freq", 300)) * t)
        y = y * (1 - ring_mix / 100) + wet * (ring_mix / 100)
    return np.asarray(y, dtype=np.float32)


def apply_speed_rate(y, sr, fx):
    """Tempo via Praat lengthen (pitch-preserving). Rate slider sets the
    effective stream rate to 44100*rate (24 kHz source is pitch-neutral at ~0.544)."""
    tempo = float(fx.get("tempo", 1.0) or 1.0)
    if abs(tempo - 1.0) > 1e-9:
        try:
            import parselmouth

            sound = parselmouth.Sound(np.asarray(y, dtype=np.float64), sampling_frequency=sr)
            stretched = sound.lengthen(75.0, 600.0, 1.0 / tempo)
            y = np.asarray(stretched.values, dtype=np.float32).flatten()
        except Exception as exc:
            print(f"[Chain] Tempo stretch skipped ({exc}); continuing unstretched.")
    rate = float(fx.get("rate", 1.0) or 1.0)
    if abs(rate - 1.0) > 1e-9 and rate > 0:
        target_sr = int(44100 * rate)
        if abs(target_sr - sr) > 1e-6 * sr:
            y = _resample(y, len(y) * CONTAINER_SR / target_sr)
        sr = CONTAINER_SR
    return np.asarray(y, dtype=np.float32), sr


def normalize_loudness(y, sr, fx):
    """Iterative gain toward the LUFS target (+encoder compensation) with the
    2 dB-headroom brickwall; limiting steals loudness, so re-measure up to 5 passes."""
    target = float(fx.get("loudness_target", -5.0))
    sample_ceiling = max(abs(float(fx.get("loudness_true_peak", 5.0))) - 2.0, 0.0)
    down = 10 ** (-sample_ceiling / 20)
    up = 10 ** (sample_ceiling / 20)
    for _ in range(5):
        measured = integrated_loudness(y, sr)
        gain_db = target - measured + WEM_ENCODER_COMPENSATION_DB
        if abs(gain_db) <= 0.4:
            break
        gain_db = float(np.clip(gain_db, -12.0, 12.0))
        y = soft_clip(y * down * (10 ** (gain_db / 20))) * up
    return np.asarray(y, dtype=np.float32)


def render_chain(audio, sr, fx):
    """The full render chain on one float32 array: sculpt -> gain -> tempo ->
    rate shift -> loudness -> 22050 Hz container. Returns (y, sr)."""
    y = np.asarray(audio, dtype=np.float32).reshape(-1)
    if len(y) < MIN_SAMPLES:
        y = np.pad(y, (0, MIN_SAMPLES - len(y)))
    y = apply_sculpt(y, sr, fx)
    y = y * (10 ** (float(fx.get("gain_db", 5) or 0) / 20))
    y, sr = apply_speed_rate(y, sr, fx)
    y = normalize_loudness(y, sr, fx)
    if sr != CONTAINER_SR:
        y = _resample(y, len(y) * CONTAINER_SR / sr)
        sr = CONTAINER_SR
    return np.asarray(y, dtype=np.float32), sr  # float32 container: >1.0 peaks are legal


def write_wav_float(path, y, sr):
    """Write the chain output as the production container: IEEE float32 wav."""
    from scipy.io import wavfile

    wavfile.write(str(path), int(sr), np.asarray(y, dtype=np.float32))
