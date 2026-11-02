# nms_dynamic_suit_voice_pipeline.py (Streamlined Version)
import os
import time
from pathlib import Path
from modular.quick_cache import (begin_sweep_wave, end_sweep_wave, get_cached_wem,
                                 move_cachedfile_to_mod_dir, update_access_time_to_match_newfile)
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

    config.load_llm()  # single seam: keeps FA and tuning identical to the manager

    # --- PHASE 3: Verify Filesystem Watches ---
    wtray_ui.icon.title = "Dynamic Suit Voice - Verifying Directories..."
    mod_dir = config.mod_dir
    if not mod_dir or not mod_dir.exists():
        wtray_ui.icon.title = "Dynamic Suit Voice - Error: Missing Dirs"
        return

    access_times = {f3.stem: f3.stat().st_atime for f3 in mod_dir.glob("*.wem")}

    # Cache janitor: untracked dirs, stale-era variants and old temps, in the
    # background so it never delays the boot.
    import threading
    from modular.quick_cache import janitor_pass
    threading.Thread(target=janitor_pass, args=(config,), daemon=True, name="cache-janitor").start()

    # Non-blocking watch: the scanner only detects plays and hands each fire to
    # the serve thread, so a slow render never delays detecting the next play.
    # Same-wem fires are coalesced while a serve is in flight - the fresh line
    # that lands covers the next play of that line anyway.
    import threading
    import queue as _queue

    _watch_lock = threading.Lock()
    _in_flight = set()
    _fire_queue = _queue.Queue()
    _access = access_times  # shared, always touch under _watch_lock

    def _serve_worker():
        while True:
            wem_id, fire_dir = _fire_queue.get()
            try:
                spoken = str(config.intent_map.get(wem_id, {}).get("Transcription", "")).strip()
                spoken_part = f' - speaking: "{spoken}"' if spoken else ""
                print(f"[Watch] {wem_id} fired{spoken_part} - fetching a fresh line...")
                cached_file = get_cached_wem(config, wem_id)
                if cached_file is None:
                    with _watch_lock:
                        stale_file = fire_dir / f"{wem_id}.wem"
                        if stale_file.exists():
                            _access[wem_id] = stale_file.stat().st_atime
                    print(f"[Watch] {wem_id} - no variant available; leaving the current file in place.")
                else:
                    moved_file = move_cachedfile_to_mod_dir(config, cached_file, fire_dir, wem_id)
                    if moved_file is None:
                        # Atime stays changed: the next scan retries until the file frees up.
                        print(f"[Watch] {wem_id} - swap failed (file locked by the game?).")
                    else:
                        with _watch_lock:
                            update_access_time_to_match_newfile(moved_file, _access)
                        print(f"[Watch] {wem_id} - fresh line swapped in.")
            except Exception as s_e:
                print(f"[Watch] serve error for {wem_id}: {s_e}")
            finally:
                with _watch_lock:
                    _in_flight.discard(wem_id)
                _fire_queue.task_done()

    threading.Thread(target=_serve_worker, daemon=True, name="watch-serve-1").start()
    threading.Thread(target=_serve_worker, daemon=True, name="watch-serve-2").start()

    # --- PHASE 3.5: Mod folder voice-file refresh (manifest-gated) ---------------
    # The manifest records each file's voice/settings era; only other-era lines re-record.
    # A settings change mid-sweep bumps the generation: the running sweep aborts at
    # its next file and a fresh sweep restarts, so no mixed-era files survive.
    _refresh_lock = threading.Lock()
    _refresh_generation = [0]

    def _refresh_mod_folder():
        """Bump the generation and (re)start the sweep worker; sweeps always
        render in-flavor lines so the mod folder never rests on generic audio."""
        superseded = _refresh_lock.locked()
        _refresh_generation[0] += 1
        if superseded:
            print("[VoiceFiles] Restarting the refresh sweep with the new settings...")
        threading.Thread(target=_refresh_worker, daemon=True, name="mod-folder-refresh").start()

    def _refresh_worker():
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
                print(f"[VoiceFiles] Refreshing {total} of {len(all_ids)} tracked restyled voice files to '{config.tts_voice}' (gameplay lines render first)...")
                begin_sweep_wave()  # hold filler rounds until the pass completes
                try:
                    for index, wem_id in enumerate(ordered, 1):
                        if not wtray_ui.running or gen != _refresh_generation[0]:
                            return  # engine stopping or a newer settings change took over
                        wtray_ui.icon.title = f"Dynamic Suit Voice - Refreshing voice files {index}/{total}..."
                        cached_file = get_cached_wem(config, wem_id, top_up=True, rephrase=True, priority=10)
                        if cached_file is None:
                            continue
                        moved = move_cachedfile_to_mod_dir(config, cached_file, target_dir, wem_id)
                        if moved is not None:
                            with _watch_lock:
                                update_access_time_to_match_newfile(moved, _access)
                            print(f"[VoiceFiles] {index}/{total} -> {wem_id} ({config.tts_voice})")
                finally:
                    end_sweep_wave()
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
                    threading.Thread(target=_refresh_mod_folder, daemon=True, name="mod-folder-refresh").start()
        if config.mod_dir != current_mod_dir:
            current_mod_dir = config.mod_dir
            print(f"[Config] Watch folder is now: {current_mod_dir}")
            if current_mod_dir and current_mod_dir.exists():
                with _watch_lock:
                    _access.clear()
                    _access.update({f3.stem: f3.stat().st_atime for f3 in current_mod_dir.glob("*.wem")})
        if not current_mod_dir or not current_mod_dir.exists():
            time.sleep(config.check_interval)
            continue

        for f3 in current_mod_dir.glob("*.wem"):
            try:
                current_atime = f3.stat().st_atime
                wem_id = f3.stem
                with _watch_lock:
                    changed = current_atime != _access.get(wem_id, 0)
                    if changed and wem_id not in _in_flight:
                        _in_flight.add(wem_id)  # coalesce: one serve per line at a time
                        dispatch = True
                    else:
                        dispatch = False
                if dispatch:
                    _fire_queue.put((wem_id, current_mod_dir))
            except Exception as w_e:
                print(f"Error handling {f3.name}: {w_e}")

        time.sleep(config.check_interval)
