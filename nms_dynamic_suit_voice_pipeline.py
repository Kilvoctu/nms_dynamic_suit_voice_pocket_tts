# nms_dynamic_suit_voice_pipeline.py (Streamlined Version)
import os
import time
from pathlib import Path
from modular.quick_cache import get_cached_wem, move_cachedfile_to_mod_dir, update_access_time_to_match_newfile
from PIL import Image
# NOTE: No global config or TTS instantiations live at the top of this file anymore!

def watch_wems(wtray_ui, config):
    """Main watchdog thread invoked after the launcher completes fast boot."""

    # --- PHASE 1: Initialize Pocket TTS Engine Weights ---
    wtray_ui.icon.title = "Dynamic Suit Voice - Loading TTS Voice Models..."

    from modular.tts_utils import warm_pocket_engine
    config.tts_model = warm_pocket_engine(config.pocket_model_name, config.root_dir, config.tts_voice)

    # --- PHASE 2: Initialize Llama-cpp Core Engine weights ---
    wtray_ui.icon.title = "Dynamic Suit Voice - Loading Local Llama AI..."

    from llama_cpp import Llama
    config.llm = Llama(
        model_path=config.llm_model,
        n_ctx=40960,
        n_batch=1024,
        n_threads=min(os.cpu_count() or 4, 8),  # guard the hardware footprint
        verbose=False
    )

    # --- PHASE 3: Verify Filesystem Watches ---
    wtray_ui.icon.title = "Dynamic Suit Voice - Verifying Directories..."
    mod_dir = config.mod_dir
    if not mod_dir or not mod_dir.exists():
        wtray_ui.icon.title = "Dynamic Suit Voice - Error: Missing Dirs"
        return

    access_times = {f3.stem: f3.stat().st_atime for f3 in mod_dir.glob("*.wem")}

    # --- PHASE 3.5: Mod folder voice-file refresh (manifest-gated) ---------------
    # The manifest records each file's voice/settings era; only other-era lines re-record.
    # A settings change mid-sweep bumps the generation: the running sweep aborts at
    # its next file and a fresh sweep restarts, so no mixed-era files survive.
    import threading

    _refresh_lock = threading.Lock()
    _refresh_generation = [0]

    def _refresh_mod_folder(rephrase=False):
        """Bump the generation and (re)start the sweep worker; rephrase=True for
        style changes (tone/wordiness/base), False for cheap voice-era swaps."""
        superseded = _refresh_lock.locked()
        _refresh_generation[0] += 1
        if superseded:
            print("[VoiceFiles] Restarting the refresh sweep with the new settings...")
        threading.Thread(target=_refresh_worker, args=(rephrase,), daemon=True, name="mod-folder-refresh").start()

    def _refresh_worker(rephrase=False):
        gen = _refresh_generation[0]
        with _refresh_lock:
            if gen != _refresh_generation[0]:
                return  # superseded while waiting for the previous sweep
            try:
                from modular.quick_cache import _settings_fingerprint, load_mod_manifest

                fingerprint = _settings_fingerprint(config)
                manifest = load_mod_manifest(config)
                target_dir = config.mod_dir
                if not target_dir or not target_dir.exists():
                    print("[VoiceFiles] Mod folder unavailable; nothing to refresh.")
                    return
                all_ids = [f3.stem for f3 in target_dir.glob("*.wem")]
                # Only lines the transcript map tracks are ours to manage; other
                # stubs in the folder keep their stock game audio as a fallback.
                all_ids = [i for i in all_ids if i in config.intent_map]
                stale = [
                    i for i in all_ids
                    if manifest.get(i, {}).get("fingerprint") != fingerprint
                    or manifest.get(i, {}).get("voice") != config.tts_voice
                ]
                if not stale:
                    print("[VoiceFiles] Mod folder already matches the current voice; nothing to do.")
                    return
                used = [i for i in stale if i in config.intent_map and str(config.intent_map[i].get("Used", "")).strip().lower() == "yes"]
                ordered = used + [i for i in stale if i not in used]
                total = len(ordered)
                mode = "restyled voice files" if rephrase else "voice files"
                print(f"[VoiceFiles] Refreshing {total} of {len(all_ids)} tracked {mode} to '{config.tts_voice}' (gameplay lines render first)...")
                for index, wem_id in enumerate(ordered, 1):
                    if not wtray_ui.running or gen != _refresh_generation[0]:
                        return  # engine stopping or a newer settings change took over
                    wtray_ui.icon.title = f"Dynamic Suit Voice - Refreshing voice files {index}/{total}..."
                    cached_file = get_cached_wem(config, wem_id, top_up=False, rephrase=rephrase, priority=10)
                    if cached_file is None:
                        continue
                    moved = move_cachedfile_to_mod_dir(config, cached_file, target_dir, wem_id)
                    if moved is not None:
                        update_access_time_to_match_newfile(moved, access_times)
                        print(f"[VoiceFiles] {index}/{total} -> {wem_id} ({config.tts_voice})")
                wtray_ui.icon.title = "Dynamic Suit Voice - Ready"
                print("[VoiceFiles] Mod folder fully refreshed to the current voice.")
            except Exception as exc:
                print(f"[VoiceFiles] refresh aborted: {exc}")

    _refresh_mod_folder()

    # --- PHASE 4: Initialization Complete, Launch Active Watch ---
    # Restore native transparency by swapping the icon out directly
    root_dir = Path(__file__).parent
    if wtray_ui.ready_icon_path.exists():
        # Open a fresh copy and preserve the full RGBA alpha transparency channel
        wtray_ui.icon.icon = Image.open(wtray_ui.ready_icon_path).convert("RGBA")

    wtray_ui.icon.title = "Dynamic Suit Voice - Ready"
    last_settings_mtime = config.settings_mtime()
    current_mod_dir = config.mod_dir

    while wtray_ui.running:
        # Live settings: manager Apply Changes (or hand edits) land within one watch interval, no engine restart required.
        mtime = config.settings_mtime()
        if mtime != last_settings_mtime:
            last_settings_mtime = mtime
            try:
                changed = config.reload_user_settings()
            except Exception as r_e:
                print(f"[Config] live reload failed: {r_e}")
            else:
                if changed:
                    print(f"[Config] Applied live changes: {', '.join(changed)}")
                    # Style changes (tone/wordiness/base/player name) restyle the
                    # lines; a bare voice/effects swap just re-speaks the canned
                    # transcriptions.
                    style_changed = any(
                        c in ("tone", "wordiness", "base prompt", "prompt presets", "player name")
                        for c in changed)
                    threading.Thread(target=_refresh_mod_folder, args=(style_changed,), daemon=True, name="mod-folder-refresh").start()
        if config.mod_dir != current_mod_dir:
            current_mod_dir = config.mod_dir
            print(f"[Config] Watch folder is now: {current_mod_dir}")
            if current_mod_dir and current_mod_dir.exists():
                access_times.clear()
                access_times.update({f3.stem: f3.stat().st_atime for f3 in current_mod_dir.glob("*.wem")})
        if not current_mod_dir or not current_mod_dir.exists():
            time.sleep(config.check_interval)
            continue

        for f3 in current_mod_dir.glob("*.wem"):
            try:
                current_atime = f3.stat().st_atime
                wem_id = f3.stem
                if current_atime != access_times.get(wem_id, 0):

                    print(f"[Watch] {wem_id} fired - fetching a fresh line...")
                    cached_file = get_cached_wem(config, wem_id)
                    if cached_file is None:
                        print(f"[Watch] {wem_id} - no variant available; leaving the current file in place.")
                        continue

                    moved_file = move_cachedfile_to_mod_dir(config, cached_file, current_mod_dir, wem_id)
                    if moved_file is None:
                        print(f"[Watch] {wem_id} - swap failed (file locked by the game?).")
                        continue

                    update_access_time_to_match_newfile(moved_file, access_times)
                    print(f"[Watch] {wem_id} - fresh line swapped in.")

            except Exception as w_e:
                print(f"Error handling {f3.name}: {w_e}")

        time.sleep(config.check_interval)
