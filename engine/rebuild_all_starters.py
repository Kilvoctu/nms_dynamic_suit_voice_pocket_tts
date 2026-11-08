# rebuild_all_starters.py
"""Rebuild every starter wem in the shipping voice (eponine + Factory Standard), no LLM.
Renders stock transcriptions through the production chain over assets/starter_wems; run with the engine stopped."""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.config import SuitVoiceConfig
from engine.tts_utils import pocket_generate, warm_pocket_engine
from engine.audio_chain import render_chain, write_wav_float
from engine.wem_utils import convert_to_wem

VOICE = "eponine"
PRESET_NAME = "Factory Standard Exosuit Core"


def main():
    ap = argparse.ArgumentParser(description="Re-render all starter wems in the shipping voice.")
    ap.add_argument("--resume", action="store_true", help="skip ids already rebuilt this run")
    args = ap.parse_args()

    config = SuitVoiceConfig(init_llm=False)
    presets = json.loads((ROOT / "assets" / "data" / "eqfx_presets.json").read_text(encoding="utf-8"))
    preset = presets[PRESET_NAME]
    rack = {k: preset[k] for k in ("speed", "pitch_st", "volume", "eq_bands", "fx")}

    with open(ROOT / "assets" / "data" / "nms_suit_voice_transcripts.csv", encoding="utf-8") as f:
        texts = {r["WEM_number"]: r["Transcription"] for r in csv.DictReader(f)}

    starters = ROOT / "assets" / "starter_wems"
    ids = sorted((f.stem for f in starters.glob("*.wem")), key=int)
    started = time.time()

    print(f"[Rebuild] {len(ids)} starter lines | voice {VOICE} | preset {PRESET_NAME}")
    warm_pocket_engine(config.pocket_model_name, config.root_dir, VOICE)

    temp_dir = config.temp_wem_dir
    temp_dir.mkdir(parents=True, exist_ok=True)

    done = failed = skipped = 0
    for idx, wem_id in enumerate(ids, 1):
        out = starters / f"{wem_id}.wem"
        if args.resume and out.exists() and out.stat().st_mtime > started:
            skipped += 1
            continue
        text = texts.get(wem_id)
        if not text:
            print(f"[Rebuild] {idx}/{len(ids)} {wem_id}: no transcription, skipped")
            skipped += 1
            continue
        t0 = time.monotonic()
        try:
            audio, sr = pocket_generate(config.pocket_model_name, config.root_dir, text, VOICE,
                                        max_tokens=config.pocket_tts_max_tokens)
            audio, sr = render_chain(audio, sr, rack)  # anchor resolves the config default
            temp_wav = temp_dir / f"rebuild_{wem_id}.wav"
            write_wav_float(temp_wav, audio, sr)
            convert_to_wem(config, temp_wav, out)
            temp_wav.unlink(missing_ok=True)
            done += 1
            print(f'[Rebuild] {idx}/{len(ids)} {wem_id}: "{text}" ({time.monotonic() - t0:.1f}s)')
        except Exception as e:
            failed += 1
            print(f"[Rebuild] {idx}/{len(ids)} {wem_id}: FAILED {e}")
    print(f"[Rebuild] done: {done} rebuilt, {failed} failed, {skipped} skipped")


if __name__ == "__main__":
    main()
