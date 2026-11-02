import time
from pathlib import Path
from datetime import datetime
from modular.llm_utils import reword_phrase
from modular.prompt_utils import build_suit_prompt
from modular.tts_utils import run_tts
from modular.wem_utils import convert_to_wem


def generate_replacement_voice_line(config, wem_id, output_path: Path, rephrase: bool = True):
    """Generate one WEM variant; rephrase=False speaks the original transcription (background refresh path)."""

    if wem_id not in config.intent_map:
        print(f"[cache_replacement.py] No intent entry for WEM ID {wem_id}")
        return None

    intent_entry = config.intent_map[wem_id]
    original_phrase = intent_entry["Transcription"]
    category = intent_entry["Category"]
    intent = intent_entry["Intent"]

    if rephrase:
        final_prompt = build_suit_prompt(config, category, intent, original_phrase, wem_id)
        stage_start = time.monotonic()
        reworded = reword_phrase(config, wem_id, category, original_phrase, final_prompt)
        llm_seconds = time.monotonic() - stage_start
    else:
        reworded = original_phrase
        llm_seconds = 0.0

    print(f'[Gen] {wem_id}: "{original_phrase}" -> "{reworded}" (llm {llm_seconds:.1f}s)')

    stage_start = time.monotonic()
    temp_wav_path = run_tts(config, reworded, wem_id)
    render_seconds = time.monotonic() - stage_start

    # Ensure output path exists
    output_path.mkdir(parents=True, exist_ok=True)

    # Determine final WEM filename
    if output_path == config.mod_dir:
        final_wem_path = output_path / f"{wem_id}.wem"
    else:
        # Voice+settings-tagged: lets quick_cache serve only variants from the
        # current voice/phrasing era and purge stale ones automatically.
        from modular.quick_cache import _settings_fingerprint, _voice_tag
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag = f"{_voice_tag(config.tts_voice)}_{_settings_fingerprint(config)}"
        final_wem_path = output_path / f"{wem_id}_{tag}_{timestamp}.wem"

    # Convert WAV to WEM
    stage_start = time.monotonic()
    convert_to_wem(config, temp_wav_path, output_path=final_wem_path)
    encode_seconds = time.monotonic() - stage_start
    print(f"[Gen] {wem_id}: wem ready (render {render_seconds:.1f}s, encode {encode_seconds:.1f}s) -> {final_wem_path.name}")
    return final_wem_path