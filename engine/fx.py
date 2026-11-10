from typing import Any, cast

import numpy as np

# Parametric EQ / distance DSP primitives.

_EQ_TYPES = ("lowshelf", "highshelf", "peaking", "highpass", "lowpass", "notch")
_GAIN_TYPES = ("lowshelf", "highshelf", "peaking")

_MAX_EQ_BANDS = 10
MAX_EQ_BANDS = _MAX_EQ_BANDS  # public alias: UI caps must mirror this exactly
_MIN_FREQ = 20.0
_MAX_FREQ = 20000.0
_MIN_GAIN_DB = -18.0
_MAX_GAIN_DB = 18.0
_MIN_Q = 0.1
_MAX_Q = 10.0

_DEFAULT_EQ_TYPE = "peaking"
_DEFAULT_FREQ = 1000.0
_DEFAULT_GAIN_DB = 0.0
_DEFAULT_Q = 0.707

_PROXIMITY_MAX_GAIN_DB = 4.0
_PROXIMITY_DB_PER_UNIT = 8.0


def _to_float(value: object, default: float | None = None) -> float | None:
    """Coerce `value` to a finite float, or return `default` (None means 'invalid')."""
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not np.isfinite(result):
        return default
    return result


def _need_float(value: object, default: float) -> float:
    """_to_float with a guaranteed-finite default: never returns None."""
    result = _to_float(value, default)
    return default if result is None else result


def _biquad_coeffs(filter_type: str, freq: float, gain_db: float, q: float, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """RBJ Audio EQ Cookbook biquad coefficients, normalized so a[0] == 1."""
    if filter_type not in _EQ_TYPES:
        raise ValueError(f"unknown biquad filter type: {filter_type!r}")

    sr_f = _to_float(sr)
    if not sr_f or sr_f <= 0.0:
        raise ValueError(f"invalid sample rate: {sr!r}")

    # Clamp defensively so the filter can never be designed outside the Nyquist range.
    freq_f = min(max(_need_float(freq, _DEFAULT_FREQ), _MIN_FREQ), 0.4999 * sr_f)
    q_f = min(max(_need_float(q, _DEFAULT_Q), _MIN_Q), _MAX_Q)
    gain_f = _need_float(gain_db, _DEFAULT_GAIN_DB)

    w0 = 2.0 * np.pi * freq_f / sr_f
    sn = np.sin(w0)
    cs = np.cos(w0)
    alpha = sn / (2.0 * q_f)

    # Cookbook A is the linear amplitude ratio; amp = sqrt(A) since gains are true dB.
    amp = np.sqrt(10.0 ** (gain_f / 20.0))

    if filter_type == "lowpass":
        b0, b1, b2 = (1.0 - cs) / 2.0, 1.0 - cs, (1.0 - cs) / 2.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cs, 1.0 - alpha
    elif filter_type == "highpass":
        b0, b1, b2 = (1.0 + cs) / 2.0, -(1.0 + cs), (1.0 + cs) / 2.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cs, 1.0 - alpha
    elif filter_type == "notch":
        b0, b1, b2 = 1.0, -2.0 * cs, 1.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cs, 1.0 - alpha
    elif filter_type == "peaking":
        b0, b1, b2 = 1.0 + alpha * amp, -2.0 * cs, 1.0 - alpha * amp
        a0, a1, a2 = 1.0 + alpha / amp, -2.0 * cs, 1.0 - alpha / amp
    elif filter_type == "lowshelf":
        # Shelf slope S = 1 form: 2*sqrt(A)*alpha.
        slope = 2.0 * np.sqrt(amp) * alpha
        b0 = amp * ((amp + 1.0) - (amp - 1.0) * cs + slope)
        b1 = 2.0 * amp * ((amp - 1.0) - (amp + 1.0) * cs)
        b2 = amp * ((amp + 1.0) - (amp - 1.0) * cs - slope)
        a0 = (amp + 1.0) + (amp - 1.0) * cs + slope
        a1 = -2.0 * ((amp - 1.0) + (amp + 1.0) * cs)
        a2 = (amp + 1.0) + (amp - 1.0) * cs - slope
    else:  # "highshelf"
        slope = 2.0 * np.sqrt(amp) * alpha
        b0 = amp * ((amp + 1.0) + (amp - 1.0) * cs + slope)
        b1 = -2.0 * amp * ((amp - 1.0) + (amp + 1.0) * cs)
        b2 = amp * ((amp + 1.0) + (amp - 1.0) * cs - slope)
        a0 = (amp + 1.0) - (amp - 1.0) * cs + slope
        a1 = 2.0 * ((amp - 1.0) - (amp + 1.0) * cs)
        a2 = (amp + 1.0) - (amp - 1.0) * cs - slope

    if not np.isfinite(a0) or abs(a0) < 1e-12:
        raise ValueError(f"degenerate biquad for {filter_type!r} at {freq_f} Hz")

    return (
        np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64),
        np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64),
    )


def apply_biquad(x: np.ndarray, b: np.ndarray, a: np.ndarray) -> np.ndarray:
    """Filter `x` with a biquad. Runs along axis 0, so (N, 2) stereo is filtered per channel.

    Returns a float32 array with the same shape as `x`; the input is never modified.
    """
    data = np.asarray(x, dtype=np.float32)
    if data.size == 0:
        return data
    from scipy import signal

    return np.asarray(signal.lfilter(b, a, data, axis=0), dtype=np.float32)


def parametric_eq(stereo: np.ndarray, sr: int, bands: list[dict]) -> np.ndarray:
    """Apply a list of parametric EQ bands to audio, in list order.

    A 1-D input is treated as a single channel; returns float32, unchanged (zero-copy) if nothing to do.
    """
    arr = np.asarray(stereo)
    if arr.size == 0 or arr.ndim > 2:
        return stereo

    mono_in = arr.ndim == 1
    data = arr.reshape(-1, 1) if mono_in else arr

    active: list[dict] = []
    for band in (bands or []):
        if not isinstance(band, dict) or not band.get("enabled", True):
            continue
        if band.get("type") in _GAIN_TYPES and _to_float(band.get("gain_db"), 0.0) == 0.0:
            continue
        active.append(band)

    if not active:
        return stereo

    processed = np.array(data, dtype=np.float32, copy=True)
    for band in active:
        freq = _to_float(band.get("freq"))
        if freq is None:
            print(f"skipping EQ band {band!r}: invalid frequency")
            continue
        try:
            b, a = _biquad_coeffs(
                str(band.get("type", _DEFAULT_EQ_TYPE)),
                freq,
                _need_float(band.get("gain_db"), _DEFAULT_GAIN_DB),
                _need_float(band.get("q"), _DEFAULT_Q),
                sr,
            )
        except ValueError as e:
            print(f"skipping EQ band {band!r}: {e}")
            continue

        from scipy import signal

        for i in range(processed.shape[1]):
            processed[:, i] = signal.lfilter(b, a, processed[:, i])

    return processed[:, 0] if mono_in else processed


def sanitize_eq_bands(bands: object, nyquist: float | None = None) -> list[dict]:
    """Coerce arbitrary user/prompt-supplied EQ band data into safe, clamped band dicts.

    Never raises: garbage input yields an empty list. Invalid/missing band types fall
    back to "peaking"; frequencies, gains and Qs are clamped to musical ranges.
    """
    result: list[dict] = []
    if bands is None or isinstance(bands, (str, bytes, dict)):
        return result

    nyq = _to_float(nyquist)
    if nyq is not None and nyq <= 0.0:
        nyq = None

    try:
        for entry in bands:  # type: ignore[union-attr]
            if not isinstance(entry, dict):
                continue

            freq = _to_float(entry.get("freq", _DEFAULT_FREQ))
            gain_db = _to_float(entry.get("gain_db", _DEFAULT_GAIN_DB))
            q = _to_float(entry.get("q", _DEFAULT_Q))
            if freq is None or gain_db is None or q is None:
                continue

            band_type = entry.get("type", _DEFAULT_EQ_TYPE)
            if band_type not in _EQ_TYPES:
                band_type = _DEFAULT_EQ_TYPE

            freq = min(max(freq, _MIN_FREQ), _MAX_FREQ)
            if nyq is not None:
                freq = min(freq, 0.98 * nyq)

            result.append({
                "type": band_type,
                "freq": freq,
                "gain_db": min(max(gain_db, _MIN_GAIN_DB), _MAX_GAIN_DB),
                "q": min(max(q, _MIN_Q), _MAX_Q),
            })

            if len(result) >= _MAX_EQ_BANDS:
                break
    except Exception as e:
        print(f"EQ band sanitization aborted: {e}")

    result.sort(key=lambda b: b["freq"])
    return result


def distance_gain(distance: float) -> float:
    """Inverse-ish distance attenuation, clamped so close distances never explode."""
    d = _to_float(distance)
    if d is None:
        return 1.0
    return 1.0 / max(0.5, d)


def proximity_shelf_gain_db(distance: float) -> float:
    """Close-mic bass proximity effect: a low-shelf boost that rises as distance drops."""
    d = _to_float(distance)
    if d is None or d >= 1.0:
        return 0.0
    return float(np.clip((1.0 - d) * _PROXIMITY_DB_PER_UNIT, 0.0, _PROXIMITY_MAX_GAIN_DB))


# pre: doubler -> crusher -> decimate -> ringmod -> radio -> echo -> reverb -> compressor ; post: limiter
_DOUBLER_DEF = {"detune_cents": 25.0, "delay_up_ms": 11.0, "delay_down_ms": 18.0, "level": 0.28}
_CRUSHER_DEF = {"bits": 12.0, "mix": 0.05}
_DECIMATE_DEF = {"enabled": False, "rate_hz": 8000.0, "mix": 0.25}
_RINGMOD_DEF = {"freq_hz": 300.0, "mix": 1.0}  # classic robot voice: sine AM at the carrier
_DECIMATE_MIN_HZ = 1000.0
_DECIMATE_MAX_HZ = 22000.0
_DECIMATE_FILTER_ORDER = 8
_DECIMATE_ZEROPHASE_MIN = 64
_RADIO_DEF = {"hp_hz": 750.0, "lp_hz": 7000.0, "level": 0.22, "bits": 8.0, "mix": 0.6}
_RADIO_MAX_BANDS = 4
_ECHO_DEF_MS = [16.0, 40.0, 82.0]
_ECHO_DEF_GAIN = [0.26, 0.16, 0.08]
_ECHO_MAX_TAPS = 8
_COMP_DEF = {"threshold_db": -20.0, "ratio": 3.0, "attack_ms": 10.0, "release_ms": 120.0, "makeup_db": 0.0}
_LIMIT_DEF = {"ceiling": 0.95, "lookahead_ms": 5.0, "release_ms": 80.0}
_REVERB_DEFAULTS = {"enabled": False, "size": 0.6, "damp": 0.5, "wet": 0.35}
_REVERB_TAPS_MS = [21.0, 29.0, 43.0, 57.0, 71.0, 89.0, 107.0, 127.0]
_REVERB_TAP_GAINS = [0.30, 0.24, 0.20, 0.16, 0.13, 0.10, 0.07, 0.05]
_REVERB_MAX_FEEDBACK = 0.82
_REVERB_TARGET_GAIN = 0.6


def _clip(value: float, lo: float, hi: float) -> float:
    """Clamp an already-coerced finite float into [lo, hi]."""
    return lo if value < lo else min(value, hi)


def _fit_len(y: np.ndarray, n: int) -> np.ndarray:
    """Return a 1-D float32 copy of `y` truncated/padded (with zeros) to length `n`."""
    flat = np.asarray(y, dtype=np.float32).reshape(-1)
    if flat.size == n:
        return flat
    if flat.size > n:
        return flat[:n]
    return np.pad(flat, (0, n - flat.size))


def _delay_forward(y: np.ndarray, d: int) -> np.ndarray:
    """Delay a 1-D signal `y` right by `d` samples (zeros in front), same length."""
    n = y.shape[0]
    if d <= 0:
        return y
    if d >= n:
        return np.zeros_like(y)
    out = np.zeros_like(y)
    out[d:] = y[:n - d]
    return out


def _doubler(x: np.ndarray, sr: int, detune_cents: float, delay_up_ms: float,
             delay_down_ms: float, level: float) -> np.ndarray:
    """AT-style doubler: dry + two detuned, delayed copies, per channel. Never modifies input; failure returns it unchanged."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0 or arr.ndim > 2:
        return x

    sr_f = _to_float(sr)
    cents = _to_float(detune_cents)
    up_ms = _to_float(delay_up_ms)
    dn_ms = _to_float(delay_down_ms)
    lvl = _to_float(level)
    if not sr_f or sr_f <= 0.0 or cents is None or up_ms is None or dn_ms is None or lvl is None:
        return x

    d_up = max(0, int(round(up_ms * sr_f / 1000.0)))
    d_dn = max(0, int(round(dn_ms * sr_f / 1000.0)))

    mono_in = arr.ndim == 1
    data = arr.reshape(-1, 1) if mono_in else arr
    n = data.shape[0]

    try:
        out = np.array(data, dtype=np.float32, copy=True)
        for ch in range(data.shape[1]):
            y = data[:, ch]
            up = _pitch_shift(y, cents / 100.0)
            dn = _pitch_shift(y, -cents / 100.0)
            up = _delay_forward(_fit_len(up, n), d_up)
            dn = _delay_forward(_fit_len(dn, n), d_dn)
            out[:, ch] = y + lvl * up + lvl * dn
    except Exception as e:
        print(f"doubler failed, returning input: {e}")
        return x

    return out[:, 0] if mono_in else out


def _pitch_shift(y: np.ndarray, n_steps: float) -> np.ndarray:
    """Duration-preserving pitch shift: FFT resample to n/factor, stretch back to n.
    Lighter than a phase vocoder - fine for the doubler's low-level detuned layers."""
    from scipy.signal import resample

    factor = 2.0 ** (n_steps / 12.0)
    n = len(y)
    small = resample(y, max(64, int(round(n / factor))))
    idx = np.linspace(0.0, len(small) - 1.0, n)
    return np.interp(idx, np.arange(len(small)), small).astype(np.float32)


def _crusher(x: np.ndarray, bits: float, mix: float) -> np.ndarray:
    """Bit-crush via sample quantization, blended by `mix` (0 = dry, 1 = wet)."""
    data = np.asarray(x, dtype=np.float32)
    if data.size == 0:
        return data

    bits_f = _to_float(bits)
    mix_f = _to_float(mix)
    if bits_f is None or mix_f is None:
        return x

    mix_f = _clip(mix_f, 0.0, 1.0)
    if mix_f <= 0.0:
        return data

    levels = 2.0 ** (_clip(bits_f, 0.1, 24.0) - 1.0)
    wet = np.round(data * levels) / levels
    return (data * (1.0 - mix_f) + wet * mix_f).astype(np.float32, copy=False)


def _sosfilt(data: np.ndarray, sos: np.ndarray) -> np.ndarray:
    """Apply a second-order-sections filter along the time (axis 0 for stereo)."""
    from scipy import signal

    if data.ndim == 2:
        return cast(np.ndarray, signal.sosfilt(sos, data, axis=0))
    return cast(np.ndarray, signal.sosfilt(sos, data))


def _decimate(x: np.ndarray, sr: int, rate_hz: float, mix: float) -> np.ndarray:
    """Sample-rate reduction (decimate -> linear upsample) blended by `mix`; aliasing/foldover grit.
    rate_hz clamps to [_DECIMATE_MIN_HZ, _DECIMATE_MAX_HZ], step = max(1, floor(sr / rate_hz)). Never modifies input; garbage/no-op/mix <= 0 return it unchanged."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0 or arr.ndim > 2:
        return x

    sr_f = _to_float(sr)
    rate_f = _to_float(rate_hz)
    mix_f = _to_float(mix)
    if not sr_f or sr_f <= 0.0 or rate_f is None or mix_f is None:
        return x

    mix_f = _clip(mix_f, 0.0, 1.0)
    if mix_f <= 0.0:
        return arr

    # rate_hz is the intermediate rate; effective rate is sr / step (floored). At-or-above-sr
    # or step <= 1 is a no-op: nothing to fold over; the lowpass alone would just dull the voice.
    rate = _clip(rate_f, _DECIMATE_MIN_HZ, _DECIMATE_MAX_HZ)
    step = max(1, int(np.floor(sr_f / rate)))
    if rate >= sr_f or step <= 1:
        return arr

    mono_in = arr.ndim == 1
    data = arr.reshape(-1, 1) if mono_in else arr
    n = data.shape[0]

    try:
        from scipy import signal

        # Cutoff stays below both Nyquists: rate < sr and rate <= sr / step.
        cutoff = 0.45 * rate
        sos = cast(Any, signal.butter(_DECIMATE_FILTER_ORDER, cutoff, btype="lowpass", fs=sr_f, output="sos"))
        work = data.astype(np.float64)
        if n >= _DECIMATE_ZEROPHASE_MIN:
            try:
                anti = signal.sosfiltfilt(sos, work, axis=0)
            except Exception:
                anti = _sosfilt(work, sos)  # Too short for filtfilt's padlen.
        else:
            anti = _sosfilt(work, sos)

        kept = np.asarray(anti, dtype=np.float64)[::step]
        if kept.shape[0] < 2:
            return x  # Nothing to interpolate between.

        xi = np.arange(n, dtype=np.float64)
        xp = np.arange(kept.shape[0], dtype=np.float64) * float(step)
        wet = np.empty((n, data.shape[1]), dtype=np.float32)
        for ch in range(data.shape[1]):
            # noinspection PyTypeChecker
            wet[:, ch] = np.interp(xi, xp, kept[:, ch])

        out = np.asarray(data * (1.0 - mix_f) + wet * mix_f, dtype=np.float32)
    except Exception as e:
        print(f"decimate failed, returning input: {e}")
        return x

    return out[:, 0] if mono_in else out


def _ringmod(x: np.ndarray, sr: int, freq_hz: float, mix: float) -> np.ndarray:
    """Amplitude-modulate `x` with a sine carrier blended by `mix` (1.0 = pure ring mod,
    the classic robot sideband character). Works on mono or (N, ch) input; never raises."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0 or arr.ndim > 2:
        return x
    sr_f = _to_float(sr)
    freq = _to_float(freq_hz)
    mix_f = _to_float(mix)
    if not sr_f or sr_f <= 0.0 or freq is None or mix_f is None:
        return arr
    try:
        t = np.arange(arr.shape[0], dtype=np.float64) / sr_f
        carrier = np.sin(2.0 * np.pi * _clip(freq, 20.0, 1000.0) * t)
        wet = arr * carrier
        out = arr * (1.0 - _clip(mix_f, 0.0, 1.0)) + wet * _clip(mix_f, 0.0, 1.0)
        return np.asarray(out, dtype=np.float32)
    except Exception as e:
        print(f"ringmod failed, returning input: {e}")
        return x


def _radio_layer(x: np.ndarray, sr: int, hp_hz: float, lp_hz: float, level: float,
                 bits: float, mix: float, bands: list[dict]) -> np.ndarray:
    """Walkie-talkie send: x + level * crusher(EQ(bandpass(x))); band-pass is a 4th-order Butterworth HP+LP cascade, `bands` pre-sanitized."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0 or arr.ndim > 2:
        return x

    sr_f = _to_float(sr)
    hp = _to_float(hp_hz)
    lp = _to_float(lp_hz)
    lvl = _to_float(level)
    if not sr_f or sr_f <= 0.0 or hp is None or lp is None or lvl is None:
        return x

    nyq = 0.499 * sr_f
    hp = _clip(hp, 1.0, nyq)
    lp = _clip(lp, 1.0, nyq)

    try:
        from scipy import signal

        hp_sos = signal.butter(4, hp, btype="highpass", fs=sr_f, output="sos")
        lp_sos = signal.butter(4, lp, btype="lowpass", fs=sr_f, output="sos")
        chain = _sosfilt(arr, hp_sos)  # type: ignore[assignment]
        chain = _sosfilt(chain, lp_sos)  # type: ignore[assignment]
        chain = np.asarray(chain, dtype=np.float32)
    except Exception as e:
        print(f"radio bandpass failed, returning input: {e}")
        return x

    chain = parametric_eq(chain, sr, bands if bands else [])
    chain = _crusher(chain, bits, mix)
    return (arr + lvl * chain).astype(np.float32, copy=False)


def _echo_taps(x: np.ndarray, sr: int, taps_ms: list[float], taps_gain: list[float],
               feedback: float = 0.0, damping_hz: float = 0.0) -> np.ndarray:
    """Parallel echo taps plus, when feedback > 0, ONE damped feedback comb keyed to the longest tap (its early tap is skipped).
    The comb is ring-buffered, one forward pass per channel; feedback <= 0.85 (sanitize_fx) keeps the tail bounded and decaying."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0 or arr.ndim > 2:
        return x

    sr_f = _to_float(sr)
    if not sr_f or sr_f <= 0.0:
        return x

    try:
        ms = list(taps_ms)  # type: ignore[arg-type]
        gains = list(taps_gain)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return x  # Non-iterable tap data -> dry.

    n = len(ms)
    if n == 0:
        return x

    # Resolve the usable taps up front: the comb (when enabled) rides the longest one.
    taps: list[tuple[int, float]] = []
    for i in range(min(n, len(gains), _ECHO_MAX_TAPS)):
        ms_i = _to_float(ms[i])
        g_i = _need_float(gains[i], 0.0)
        if ms_i is None:
            continue
        if g_i == 0.0:
            continue
        d = max(0, int(round(ms_i * sr_f / 1000.0)))
        if d <= 0:
            continue
        taps.append((d, g_i))

    mono_in = arr.ndim == 1
    data = arr.reshape(-1, 1) if mono_in else arr
    out = np.array(data, dtype=np.float32, copy=True)

    fb = _need_float(feedback, 0.0)
    comb_i = -1
    if fb > 0.0 and taps:
        # Clamp feedback below unity so the tail always decays (sanitize_fx caps at 0.85).
        fb = _clip(fb, 0.0, 0.95)
        comb_i = max(range(len(taps)), key=lambda idx: taps[idx][0])

    for k, (d, g_i) in enumerate(taps):
        if k == comb_i:
            continue  # The comb takes over this tap's wet role, recirculation included.
        for ch in range(data.shape[1]):
            out[:, ch] += g_i * _delay_forward(data[:, ch], d)

    if comb_i >= 0:
        d_comb, wet_g = taps[comb_i]
        damp_hz = _need_float(damping_hz, 0.0)
        a = float(np.exp(-2.0 * np.pi * damp_hz / sr_f)) if damp_hz > 0.0 else None
        n_s = data.shape[0]
        for ch in range(data.shape[1]):
            src = data[:, ch].astype(np.float64)
            buf = np.zeros(d_comb, dtype=np.float64)  # past y values, ring-indexed.
            wet = np.zeros(n_s, dtype=np.float64)
            wr = 0
            state = 0.0
            for i in range(n_s):
                read = buf[wr]
                if a is not None:
                    state = a * state + (1.0 - a) * read
                    read = state
                y = (src[i - d_comb] if i >= d_comb else 0.0) + fb * read
                buf[wr] = y
                wet[i] = y
                wr += 1
                if wr == d_comb:
                    wr = 0
            out[:, ch] += wet_g * wet.astype(np.float32)

    return out[:, 0] if mono_in else out


def _reverb(x: np.ndarray, sr: int, size: float, damp: float, wet: float) -> np.ndarray:
    """Deterministic plate reverb reusing the _echo_taps engine: pre-delay taps + one damped comb.
    size scales the tap grid and raises feedback toward _REVERB_MAX_FEEDBACK; wet == 0 is an exact pass-through; input never modified."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0 or arr.ndim > 2:
        return x

    sr_f = _to_float(sr)
    size_f = _to_float(size)
    damp_f = _to_float(damp)
    wet_f = _to_float(wet)
    if not sr_f or sr_f <= 0.0 or size_f is None or damp_f is None or wet_f is None:
        return x

    wet_f = _clip(wet_f, 0.0, 1.0)
    if wet_f <= 0.0:
        return arr

    size_f = _clip(size_f, 0.0, 1.0)
    damp_f = _clip(damp_f, 0.0, 1.0)

    # Bigger room -> later taps and a longer (but still decaying) tail.
    scale = 0.4 + 1.2 * size_f
    taps_ms = [ms * scale for ms in _REVERB_TAPS_MS]
    gain_sum = float(sum(_REVERB_TAP_GAINS)) or 1.0
    gains = [_REVERB_TARGET_GAIN * g / gain_sum for g in _REVERB_TAP_GAINS]
    feedback = _REVERB_MAX_FEEDBACK * size_f
    # damp -> comb cutoff: 0 keeps it bright (~14 kHz), 1 darkens it to ~600 Hz.
    damp_hz = min(600.0 + (1.0 - damp_f) ** 2 * 13400.0, 0.45 * sr_f)

    engine = _echo_taps(arr, sr, taps_ms, gains, feedback, damp_hz)
    out = arr + wet_f * (engine - arr)
    return out.astype(np.float32, copy=False)


def _compressor(x: np.ndarray, sr: int, threshold_db: float, ratio: float,
                attack_ms: float, release_ms: float, makeup_db: float) -> np.ndarray:
    """Feed-forward compressor, stereo-linked: one peak-follower on the mono-max dB envelope
    (attack on rise, release on fall), hard knee; the same gain is applied to every channel so
    interaural cues survive. gr_db = (1/ratio - 1) * (env - threshold) when env > threshold, else 0,
    so with makeup 0 it only ever turns the signal down."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0 or arr.ndim > 2:
        return x

    sr_f = _to_float(sr)
    thr = _to_float(threshold_db)
    ratio_f = _to_float(ratio)
    atk = _to_float(attack_ms)
    rel = _to_float(release_ms)
    makeup = _to_float(makeup_db)
    if not sr_f or sr_f <= 0.0 or thr is None or ratio_f is None or ratio_f <= 0.0 \
            or atk is None or rel is None or makeup is None:
        return x

    a_atk = float(np.exp(-1.0 / max(1e-6, (atk / 1000.0) * sr_f)))
    a_rel = float(np.exp(-1.0 / max(1e-6, (rel / 1000.0) * sr_f)))
    inv_ratio_minus1 = (1.0 / ratio_f) - 1.0

    mono_in = arr.ndim == 1
    data = arr.reshape(-1, 1) if mono_in else arr
    n = data.shape[0]

    # Linked detection: one envelope from the per-sample channel maximum, so the L/R
    # gain moves together and the binaural balance is never dynamically rewritten.
    env_db = 20.0 * np.log10(np.abs(data.astype(np.float64)).max(axis=1) + 1e-9)
    gain_db = np.empty(n, dtype=np.float64)
    e = float(env_db[0])
    for i in range(n):
        v = env_db[i]
        a = a_atk if v >= e else a_rel
        e = a * e + (1.0 - a) * v
        gain_db[i] = (inv_ratio_minus1 * (e - thr)) if e > thr else 0.0
    out = data * (10.0 ** ((gain_db + makeup) / 20.0)).astype(np.float32)[:, None]

    return out[:, 0] if mono_in else out


def _limiter(x: np.ndarray, sr: int, ceiling: float, lookahead_ms: float, release_ms: float) -> np.ndarray:
    """Look-ahead brick-wall limiter: max|x| <= ceiling. demand = forward-window max of |x|, so reduction pre-arms before transients.
    Required gain is release-smoothed by a one-pole that never exceeds it, so the ceiling always holds."""
    from scipy import ndimage  # lazy import: only the limiter needs it

    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0 or arr.ndim > 2:
        return x

    sr_f = _to_float(sr)
    ceil_f = _to_float(ceiling)
    look_ms = _to_float(lookahead_ms)
    rel_ms = _to_float(release_ms)
    if not sr_f or sr_f <= 0.0 or ceil_f is None or look_ms is None or rel_ms is None:
        return x

    mono_in = arr.ndim == 1
    data = arr.reshape(-1, 1) if mono_in else arr
    n = data.shape[0]

    mag = np.max(np.abs(data), axis=1).astype(np.float64)
    win = max(1, int(round(look_ms * sr_f / 1000.0)))
    if win > 1:
        # origin=-(win//2) looks forward [i, i+win-1] so reduction pre-arms before a transient;
        # zero-padding keeps the tail from inflating the demand envelope.
        demand = np.asarray(ndimage.maximum_filter1d(mag, size=win, origin=-(win // 2), mode="constant", cval=0.0), dtype=np.float64)
    else:
        demand = mag

    gain_req = np.minimum(1.0, ceil_f / np.maximum(demand, 1e-9))

    a_rel = float(np.exp(-1.0 / max(1e-6, (rel_ms / 1000.0) * sr_f)))
    gain = np.empty(n, dtype=np.float64)
    prev = 1.0
    for i in range(n):
        target = gain_req[i]
        # Attack applies instantly (pre-armed); release is smoothed; follower never exceeds required gain.
        gain[i] = target if target < prev else (a_rel * prev + (1.0 - a_rel) * target)
        prev = gain[i]

    out = data * gain.reshape(-1, 1).astype(np.float32)
    return out[:, 0] if mono_in else out.astype(np.float32)


def sanitize_fx(obj: object, nyquist: float | None = None) -> dict:
    """Coerce arbitrary user/prompt-supplied FX data into a safe, complete rack.

    Never raises and always returns ALL NINE slots ("doubler", "crusher",
    "decimate", "ringmod", "radio", "echo", "reverb", "compressor", "limiter") with defaults
    applied. Every slot has an "enabled" boolean (default False). Numeric
    parameters are clamped to musical ranges; the radio band list is delegated to
    sanitize_eq_bands (max 4 bands).
    """
    src = obj if isinstance(obj, dict) else {}

    def slot(key: str) -> dict:
        s = src.get(key)
        return s if isinstance(s, dict) else {}

    def num(s: dict, key: str, lo: float, hi: float, default: float) -> float:
        val = _to_float(s.get(key, default))
        return default if val is None else _clip(val, lo, hi)

    nyq = _to_float(nyquist)
    if nyq is not None and nyq <= 0.0:
        nyq = None

    def floats(seq: object, lo: float, hi: float, max_n: int) -> list[float]:
        out: list[float] = []
        if seq is None or isinstance(seq, (str, bytes, dict)):
            return out
        try:
            items = list(seq)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return out
        for item in items:
            f = _to_float(item)
            if f is None:
                continue
            out.append(_clip(f, lo, hi))
            if len(out) >= max_n:
                break
        return out

    # --- doubler ---
    d = slot("doubler")
    doubler = {
        "enabled": bool(d.get("enabled", False)),
        "detune_cents": num(d, "detune_cents", 0.0, 100.0, _DOUBLER_DEF["detune_cents"]),
        "delay_up_ms": num(d, "delay_up_ms", 0.0, 60.0, _DOUBLER_DEF["delay_up_ms"]),
        "delay_down_ms": num(d, "delay_down_ms", 0.0, 60.0, _DOUBLER_DEF["delay_down_ms"]),
        "level": num(d, "level", 0.0, 1.0, _DOUBLER_DEF["level"]),
    }

    # --- crusher ---
    c = slot("crusher")
    crusher = {
        "enabled": bool(c.get("enabled", False)),
        "bits": num(c, "bits", 2.0, 16.0, _CRUSHER_DEF["bits"]),
        "mix": num(c, "mix", 0.0, 1.0, _CRUSHER_DEF["mix"]),
    }

    # --- decimate ---
    dc = slot("decimate")
    decimate = {
        "enabled": bool(dc.get("enabled", False)),
        "rate_hz": num(dc, "rate_hz", _DECIMATE_MIN_HZ, _DECIMATE_MAX_HZ, _DECIMATE_DEF["rate_hz"]),
        "mix": num(dc, "mix", 0.0, 1.0, _DECIMATE_DEF["mix"]),
    }

    # --- ringmod ---
    rm = slot("ringmod")
    ringmod = {
        "enabled": bool(rm.get("enabled", False)),
        "freq_hz": num(rm, "freq_hz", 20.0, 1000.0, _RINGMOD_DEF["freq_hz"]),
        "mix": num(rm, "mix", 0.0, 1.0, _RINGMOD_DEF["mix"]),
    }

    # --- radio ---
    r = slot("radio")
    hp = num(r, "hp_hz", 20.0, 20000.0, _RADIO_DEF["hp_hz"])
    lp = num(r, "lp_hz", 20.0, 20000.0, _RADIO_DEF["lp_hz"])
    if hp >= lp:
        hp, lp = lp, hp
    radio = {
        "enabled": bool(r.get("enabled", False)),
        "hp_hz": hp,
        "lp_hz": lp,
        "level": num(r, "level", 0.0, 1.0, _RADIO_DEF["level"]),
        "bits": num(r, "bits", 2.0, 16.0, _RADIO_DEF["bits"]),
        "mix": num(r, "mix", 0.0, 1.0, _RADIO_DEF["mix"]),
        "bands": sanitize_eq_bands(r.get("bands"), nyq)[:_RADIO_MAX_BANDS],
    }

    # --- echo ---
    e = slot("echo")
    taps_ms = floats(e.get("taps_ms"), 0.0, 500.0, _ECHO_MAX_TAPS)
    taps_gain = floats(e.get("taps_gain"), 0.0, 0.9, _ECHO_MAX_TAPS)
    if not taps_ms:
        taps_ms = list(_ECHO_DEF_MS)
    if not taps_gain:
        taps_gain = list(_ECHO_DEF_GAIN)
    n_taps = min(len(taps_ms), len(taps_gain))
    echo = {
        "enabled": bool(e.get("enabled", False)),
        "taps_ms": taps_ms[:n_taps],
        "taps_gain": taps_gain[:n_taps],
        "feedback": num(e, "feedback", 0.0, 0.85, 0.0),
        "damping_hz": num(e, "damping_hz", 0.0, 16000.0, 0.0),
    }

    # --- reverb ---
    v = slot("reverb")
    reverb = {
        "enabled": bool(v.get("enabled", False)),
        "size": round(num(v, "size", 0.0, 1.0, _REVERB_DEFAULTS["size"]), 2),
        "damp": round(num(v, "damp", 0.0, 1.0, _REVERB_DEFAULTS["damp"]), 2),
        "wet": round(num(v, "wet", 0.0, 1.0, _REVERB_DEFAULTS["wet"]), 2),
    }

    # --- compressor ---
    p = slot("compressor")
    compressor = {
        "enabled": bool(p.get("enabled", False)),
        "threshold_db": num(p, "threshold_db", -60.0, 0.0, _COMP_DEF["threshold_db"]),
        "ratio": num(p, "ratio", 1.0, 20.0, _COMP_DEF["ratio"]),
        "attack_ms": num(p, "attack_ms", 0.1, 100.0, _COMP_DEF["attack_ms"]),
        "release_ms": num(p, "release_ms", 10.0, 1000.0, _COMP_DEF["release_ms"]),
        "makeup_db": num(p, "makeup_db", 0.0, 12.0, _COMP_DEF["makeup_db"]),
    }

    # --- limiter ---
    lim = slot("limiter")
    limiter = {
        "enabled": bool(lim.get("enabled", False)),
        "ceiling": num(lim, "ceiling", 0.5, 0.999, _LIMIT_DEF["ceiling"]),
        "lookahead_ms": num(lim, "lookahead_ms", 0.0, 50.0, _LIMIT_DEF["lookahead_ms"]),
        "release_ms": num(lim, "release_ms", 10.0, 1000.0, _LIMIT_DEF["release_ms"]),
    }

    return {
        "doubler": doubler,
        "crusher": crusher,
        "decimate": decimate,
        "ringmod": ringmod,
        "radio": radio,
        "echo": echo,
        "reverb": reverb,
        "compressor": compressor,
        "limiter": limiter,
    }


def apply_fx_settings(x: np.ndarray, sr: int, fx: dict, stage: str) -> np.ndarray:
    """Apply the sanitized FX rack to `x` for the given stage.

    stage == "pre":  doubler -> crusher -> decimate -> ringmod -> radio -> echo -> reverb -> compressor
    stage == "post": limiter
    Unknown stages return `x` unchanged. Only enabled slots are applied. `fx` is
    passed through sanitize_fx (idempotent) so partially-populated dicts are safe.
    """
    cfg = sanitize_fx(fx, nyquist=(sr / 2.0) if _to_float(sr) else None)

    if stage == "pre":
        d = cfg["doubler"]
        if d["enabled"]:
            x = _doubler(x, sr, d["detune_cents"], d["delay_up_ms"], d["delay_down_ms"], d["level"])
        c = cfg["crusher"]
        if c["enabled"]:
            x = _crusher(x, c["bits"], c["mix"])
        dc = cfg["decimate"]
        if dc["enabled"]:
            x = _decimate(x, sr, dc["rate_hz"], dc["mix"])
        rm = cfg["ringmod"]
        if rm["enabled"]:
            x = _ringmod(x, sr, rm["freq_hz"], rm["mix"])
        r = cfg["radio"]
        if r["enabled"]:
            x = _radio_layer(x, sr, r["hp_hz"], r["lp_hz"], r["level"], r["bits"], r["mix"], r["bands"])
        e = cfg["echo"]
        if e["enabled"]:
            x = _echo_taps(x, sr, e["taps_ms"], e["taps_gain"], e["feedback"], e["damping_hz"])
        v = cfg["reverb"]
        if v["enabled"]:
            x = _reverb(x, sr, v["size"], v["damp"], v["wet"])
        p = cfg["compressor"]
        if p["enabled"]:
            x = _compressor(x, sr, p["threshold_db"], p["ratio"], p["attack_ms"], p["release_ms"], p["makeup_db"])
        return x

    if stage == "post":
        lim = cfg["limiter"]
        if lim["enabled"]:
            x = _limiter(x, sr, lim["ceiling"], lim["lookahead_ms"], lim["release_ms"])
        return x

    return x

