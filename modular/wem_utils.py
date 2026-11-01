# wem_utils.py
import subprocess
from pathlib import Path
import time


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

    # Move/rename to final output_path with retries
    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        try:
            wem_file.replace(output_path)
            break
        except PermissionError:
            time.sleep(0.5 * attempt)
        except Exception as e:
            time.sleep(0.5 * attempt)
    else:
        raise FileExistsError(f"Could not move WEM to {output_path}")
