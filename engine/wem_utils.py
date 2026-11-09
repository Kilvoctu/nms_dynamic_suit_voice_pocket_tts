# wem_utils.py
import struct
import subprocess
from pathlib import Path
import time

_VANILLA_CHANNEL_MASK = 0x00003102


def _patch_wem_fmt(path: Path):
    """Rewrite the wem fmt's channel-layout subtype to vanilla's 0x3102 so the
    game's decoder accepts the file. 0x42-byte wwise fmt: subtype lives at
    payload+20; payload+24 is the sample count - the old GUID write stomped it."""
    data = bytearray(path.read_bytes())
    i = data.find(b"fmt ")
    if i < 0:
        return
    size = struct.unpack_from("<I", data, i + 4)[0]
    fmt = i + 8
    if size != 0x42:
        return
    struct.pack_into("<I", data, fmt + 20, _VANILLA_CHANNEL_MASK)
    path.write_bytes(bytes(data))


_STOMPED_GUID_SIGNATURE = bytes.fromhex("33f40000d9000000b858000000000d03")


def fmt_was_guid_stomped(path: Path) -> bool:
    """True when the file carries the old bad patch (fake GUID over sample_count)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    i = data.find(b"fmt ")
    if i < 0:
        return False
    if struct.unpack_from("<I", data, i + 4)[0] != 0x42:
        return False
    fmt = i + 8
    return bytes(data[fmt + 24:fmt + 40]) == _STOMPED_GUID_SIGNATURE


def wem_duration_seconds(path) -> float | None:
    """Clip length in seconds from the RIFF header alone (data size / avg bytes
    per sec) - no Vorbis decode needed. None when unreadable or implausible."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos = 12
    fmt_avg = None
    data_size = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csize = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if cid == b"fmt " and csize >= 16:
            fmt_avg = struct.unpack_from("<I", data, body + 8)[0]  # nAvgBytesPerSec
        elif cid == b"data":
            data_size = min(csize, len(data) - body)
            break
        pos = body + csize + (csize & 1)
    if not fmt_avg or not data_size:
        return None
    dur = data_size / float(fmt_avg)
    return dur if 0.05 < dur < 60.0 else None


def convert_to_wem(config, temp_wav_path, output_path: Path):
    """Convert WAV to WEM via the standalone wav2wem encoder (retries if in use)."""
    temp_wav_path = Path(temp_wav_path).resolve()
    output_path = Path(output_path).resolve()  # Final WEM location
    wav2wem = config.wav2wem_path.resolve()    # standalone encoder binary
    temp_dir = config.temp_wem_dir.resolve()   # temporary folder for conversion

    # Encode into temp_dir first, then move into place with retries.
    wem_file = temp_dir / temp_wav_path.with_suffix(".wem").name
    wem_file.unlink(missing_ok=True)

    cmd = [
        str(wav2wem),
        str(temp_wav_path),
        "-o", str(wem_file),
        "-q", str(config.wav2wem_quality),
    ]

    try:
        subprocess.run(cmd, check=True, creationflags=config.create_no_window)
    except subprocess.CalledProcessError as e:
        print(f"Subprocess failed during WEM conversion: {e}")
        raise

    _patch_wem_fmt(wem_file)

    # Move/rename to final output_path with retries
    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        try:
            wem_file.replace(output_path)
            break
        except PermissionError:
            time.sleep(0.5 * attempt)
        except Exception:
            time.sleep(0.5 * attempt)
    else:
        raise FileExistsError(f"Could not move WEM to {output_path}")
