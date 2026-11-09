# engine/warmup_tts.py
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from engine import path_utils, tts_utils


def main():
    print("Pipeline: Starting Pocket TTS warmup (first run downloads model weights)...")
    project_root = path_utils.project_root()

    # Defaults live in code; the untracked user config is the only override source.
    pocket_model = tts_utils.POCKET_DEFAULT_MODEL
    voice = tts_utils.POCKET_DEFAULT_VOICE
    user_file = project_root / "state" / "user" / "user_customization.json"
    if user_file.exists():
        try:
            user_data = json.loads(user_file.read_text(encoding="utf-8"))
            pocket_model = user_data.get("models", {}).get("pocket_model") or pocket_model
            voice = user_data.get("tts_voice") or voice
        except Exception as e:
            print(f"Warning: could not read {user_file.name}: {e}")

    # Fire up the synthesis warmup routine (downloads weights into cache/hub if missing)
    try:
        started = time.monotonic()
        tts_utils.warm_all_pocket_voices(pocket_model, project_root)
        print(f"[TTS] All voices warm (model '{pocket_model}') in {time.monotonic() - started:.1f}s")

        test_text = (
            "The required models and files for voice synthesis are now installed. "
            "If you are hearing this message, the models are downloaded and everything worked correctly."
        )
        audio, sr = tts_utils.pocket_generate(pocket_model, project_root, test_text, voice)
        wav_path = project_root / "state" / "cache" / "tmp_wav_dir" / "warmup.wav"
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        tts_utils._write_wav(wav_path, audio, sr)
        print(f"TTS warmup complete. Verification test file generated safely: {wav_path}")

        # Best-effort end-to-end check through the wem encoder (state copy, else bundled one)
        wav2wem = project_root / "state" / "bin" / "wav2wem.exe"
        if not wav2wem.is_file():
            wav2wem = path_utils.assets_root() / "bin" / "wav2wem.exe"
        if wav2wem.is_file():
            import subprocess
            wem_path = project_root / "state" / "cache" / "tmp_wem_dir" / "warmup.wem"
            wem_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [str(wav2wem), str(wav_path), "-o", str(wem_path)],
                capture_output=True, creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            if result.returncode == 0 and wem_path.exists():
                print(f"WEM conversion verified: {wem_path}")
            else:
                print("Warning: WEM conversion failed (pipeline will retry during play).")

    except Exception as e:
        print(f"ERROR: Local TTS compilation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
