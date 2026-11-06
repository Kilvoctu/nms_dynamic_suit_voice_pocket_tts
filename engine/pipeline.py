# engine/pipeline.py (the core watch/render loop)
import json
import time
from pathlib import Path
from engine.quick_cache import (begin_sweep_wave, end_sweep_wave, get_cached_wem,
                                move_cachedfile_to_mod_dir, _settings_fingerprint)
from engine.oplock_watch import OplockWatch
from PIL import Image
# NOTE: No global config or TTS instantiations live at the top of this file anymore!

def watch_wems(wtray_ui, config):
    """Main watchdog thread invoked after the launcher completes fast boot."""

    # --- PHASE 1: Initialize Pocket TTS Engine Weights ---
    wtray_ui.icon.title = "Dynamic Suit Voice - Loading TTS Voice Models..."

    from engine.tts_utils import warm_pocket_engine, set_torch_threads
    set_torch_threads(config.gen_threads)  # before model load: torch picks the cap up at import
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

    # Re-attach previously auto-learned lines so they are managed from boot. This must
    # happen BEFORE the janitor starts, or the race lets it sweep learned caches as
    # "untracked dirs" and silently erase a line's whole variant stack.
    learned_path = config.root_dir / "state" / "user" / "learned_transcripts.json"
    if learned_path.exists():
        try:
            learned = json.loads(learned_path.read_text(encoding="utf-8"))
            merged = {k: v for k, v in learned.items()
                      if k not in config.intent_map and str(v.get("Used", "")).strip().lower() == "yes"}
            if merged:
                config.intent_map.update(merged)
                print(f"[Watch] Managing {len(merged)} auto-learned line(s) from "
                      f"state/user/learned_transcripts.json.")
        except Exception:
            print("[Watch] learned_transcripts.json unreadable; previously learned lines will re-learn on play.")
    else:
        print("[Watch] No learned-lines file yet; untracked lines will auto-learn as they are first played.")

    # Cache janitor: untracked dirs, stale-era variants and old temps, in the
    # background so it never delays the boot.
    import threading
    from engine.quick_cache import janitor_pass
    threading.Thread(target=janitor_pass, args=(config,), daemon=True, name="cache-janitor").start()

    # Non-blocking watch: play fires go to the serve queue, coalesced by the in-flight guard,
    # so a slow render never delays detecting the next play.
    import threading
    import queue as _queue

    _watch_lock = threading.Lock()
    _in_flight = set()
    _burst_drops = {}  # plays coalesced mid-serve, requeued when the serve lands (no silent loss)
    _last_play_seen = {}  # gap bookkeeping so fired lines tell how far apart the plays were
    _retry_src = {}  # picked variant stashed across a give-up chase so the refire swaps it directly
    _fire_queue = _queue.Queue()

    # Oplock play detector: game plays leave no atime, USN or directory-event traces; their
    # opens still break our level-1 oplocks, naming the exact played file.
    _oplock = OplockWatch()

    # CSV rows for logging untracked plays and auto-learning (the intent map may not know the line).
    _csv_rows = {}
    try:
        import csv as _csv
        with open(config.transcripts_path.with_suffix(".csv"), newline="", encoding="utf-8") as _fh:
            _csv_rows = {r["WEM_number"]: r for r in _csv.DictReader(_fh) if r.get("WEM_number")}
    except Exception:
        pass  # logging/learning convenience

    def _learn_line(wem_id: str, row: dict):
        """Add a heard line to the user's learned-transcripts file and the live intent map."""
        entry = {
            "Filename": row.get("Filename", f"{wem_id}.wem"),
            "Transcription": row.get("Transcription", ""),
            "Category": row.get("Category", ""),
            "Intent": row.get("Intent", ""),
            "Used": "Yes",
        }
        learned_path = config.root_dir / "state" / "user" / "learned_transcripts.json"
        learned = {}
        try:
            learned = json.loads(learned_path.read_text(encoding="utf-8"))
        except Exception:
            learned = {}
        learned[wem_id] = entry
        try:
            learned_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = learned_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(learned, indent=4, ensure_ascii=False), encoding="utf-8")
            tmp.replace(learned_path)
        except OSError as l_e:
            print(f"[Watch] Could not save the learned line {wem_id}: {l_e}")
        config.intent_map[wem_id] = entry

    def _variant_fp(src: Path) -> str:
        """Settings fingerprint from a cache variant stem (id_voice_fp_date_time naming)."""
        head = src.stem.rsplit("_", 2)[0]  # date and time are the last two underscore segments
        return head.rsplit("_", 1)[-1]

    _learn_window = []  # auto-learn timestamps: new learns cap at 3 per minute (load-wave guard)
    _learn_note = [0.0]  # last overflow-verdict time, so a wave prints one line per minute

    def _learn_allow(wem_id: str) -> bool:
        """True when a fresh untracked line may still auto-learn inside the per-minute cap."""
        now = time.monotonic()
        while _learn_window and now - _learn_window[0] > 60.0:
            _learn_window.pop(0)
        if len(_learn_window) >= 3:
            if now - _learn_note[0] > 60.0:
                _learn_note[0] = now
                print(f"[Watch] Holding auto-learn of {wem_id} - 3 new line(s) in the last minute "
                      f"looks like a load wave, not plays; ok lines will learn on their next play.")
            return False
        _learn_window.append(now)
        return True

    def _serve_worker():
        while True:
            wem_id, fire_dir, is_retry = _fire_queue.get()
            try:
                cached_file = None
                if is_retry:  # silent refire: reuse the picked variant instead of re-racing the cache
                    stash = _retry_src.pop(wem_id, None)
                    if stash and stash.exists() and _variant_fp(stash) == _settings_fingerprint(config):
                        cached_file = stash
                if not is_retry:  # requeued held-file swaps carry True and print nothing
                    if wem_id not in config.intent_map:
                        row = _csv_rows.get(wem_id, {})
                        spoken = str(row.get("Transcription", "")).strip()
                        if row:
                            if not _learn_allow(wem_id):
                                continue  # load-wave guard holds the learn; the play keeps stock audio
                            _learn_line(wem_id, row)
                            print(f"[Watch] Auto-learned line {wem_id} - \"{spoken}\" - "
                                  f"it is now managed and revoiced.")
                        else:
                            print(f"[Watch] Untracked line played: {wem_id} - \"{spoken}\" - "
                                  f"not present in the transcript CSV; cannot learn it.")
                            continue
                    spoken = str(config.intent_map.get(wem_id, {}).get("Transcription", "")).strip()
                    spoken_part = f' - speaking: "{spoken}"' if spoken else ""
                    last_seen = _last_play_seen.get(wem_id)
                    gap_part = f" (again after {time.monotonic() - last_seen:.1f}s)" if last_seen else ""
                    _last_play_seen[wem_id] = time.monotonic()
                    print(f"[Watch] {wem_id} fired{gap_part}{spoken_part} - fetching a fresh line...")
                if cached_file is None:  # a refire re-fetches only when its reused pickup failed checks
                    cached_file = get_cached_wem(config, wem_id)
                if cached_file is None:
                    if not is_retry:  # a dry cache ends the refire silently (verdict line already spoke)
                        print(f"[Watch] {wem_id} - no variant available; leaving the current file in place.")
                else:
                    moved_file = move_cachedfile_to_mod_dir(
                        config, cached_file, fire_dir, wem_id,
                        on_attempt=lambda: _oplock.suppress(wem_id, seconds=0.6))
                    if moved_file is None:
                        # silent: _log_swap_holders prints the one verdict line per 90s gate
                        _retry_src[wem_id] = cached_file  # refire swaps this same unconsumed variant
                        retry = threading.Timer(1.0, _fire_queue.put, args=((wem_id, fire_dir, True),))
                        retry.daemon = True
                        retry.start()
                    else:
                        _oplock.rearm(wem_id)
                        print(f"[Watch] {wem_id} - fresh line swapped in.")
            except Exception as s_e:
                print(f"[Watch] serve error for {wem_id}: {s_e}")
            finally:
                with _watch_lock:
                    _in_flight.discard(wem_id)
                    drops = _burst_drops.pop(wem_id, 0)
                if drops:
                    print(f"[Watch] {wem_id} - burst: {drops} play(s) landed mid-serve; requeued.")
                    for _ in range(drops):
                        _fire_queue.put((wem_id, fire_dir, False))
                _fire_queue.task_done()

    for i in range(max(1, int(getattr(config, "serve_workers", 3)))):
        threading.Thread(target=_serve_worker, daemon=True, name=f"watch-serve-{i + 1}").start()

    # --- PHASE 3.5: Mod folder refresh (manifest-gated; only other-era lines re-record).
    # Mid-sweep settings changes bump the generation: the running sweep aborts and restarts.
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
                from engine.quick_cache import _settings_fingerprint, load_mod_manifest

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
                        moved = move_cachedfile_to_mod_dir(
                            config, cached_file, target_dir, wem_id,
                            on_attempt=lambda: _oplock.suppress(wem_id, seconds=0.6))
                        if moved is not None:
                            _oplock.rearm(wem_id)
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
    if wtray_ui.ready_icon_path.exists():
        # Open a fresh copy and preserve the full RGBA alpha transparency channel
        wtray_ui.icon.icon = Image.open(wtray_ui.ready_icon_path).convert("RGBA")

    wtray_ui.icon.title = "Dynamic Suit Voice - Ready"
    last_settings_mtime = config.settings_mtime()
    current_mod_dir = config.mod_dir

    def _oplock_worker():
        """Owns the oplock watcher: arms tracked lines, acks breaks fast, feeds plays."""
        last_target, announced, recent = 0.0, False, []
        disarmed_since = None
        while wtray_ui.running:
            now = time.monotonic()
            if now - last_target >= 10.0:
                last_target = now
                if current_mod_dir and current_mod_dir.exists():
                    # arm every wem in the folder: untracked lines must be detected to auto-learn
                    ids = set(config.intent_map.keys())
                    ids.update(p.stem for p in current_mod_dir.glob("*.wem"))
                    _oplock.set_target(current_mod_dir, tuple(ids))
                    n_armed = _oplock.armed_count()
                    if n_armed and not announced:
                        announced = True
                        print(f"[Watch] Oplock play detection armed on {n_armed} line(s).")
            fired = _oplock.poll_ack(0.02)  # kernel-side wait: near-zero idle cpu
            _oplock.maintain(tries=8)
            if _oplock.armed_count() == 0:
                if disarmed_since is None:
                    disarmed_since = now
                elif now - disarmed_since >= 60.0:
                    disarmed_since = now
                    print("[Watch] Warning: no oplocks armed for a minute - play detection is down.")
            else:
                disarmed_since = None
            if fired:
                recent = [t for t in recent if now - t <= 2.0] + [now] * len(fired)
                if len(recent) > 25:
                    print(f"[Watch] {len(fired)} opens within 2s - game-boot wave, not plays.")
                else:
                    for wem_id in fired:
                        if _consider(wem_id):
                            _fire_queue.put((wem_id, current_mod_dir, False))
        _oplock.close()

    threading.Thread(target=_oplock_worker, daemon=True, name="oplock-watch").start()

    def _consider(wem_id):
        """True when this play must be served; mid-serve plays are counted for requeue (burst drops)."""
        with _watch_lock:
            if wem_id in _in_flight:
                _burst_drops[wem_id] = min(_burst_drops.get(wem_id, 0) + 1, 8)
                return False
            _in_flight.add(wem_id)
            return True

    while wtray_ui.running:
        # Live settings: manager Apply (or hand edits) land within one watch interval, no restart needed.
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
                    if "generation threads" in changed:
                        set_torch_threads(config.gen_threads)  # torch cap applies live; LLM needs a restart
                    threading.Thread(target=_refresh_mod_folder, daemon=True, name="mod-folder-refresh").start()
        if config.mod_dir != current_mod_dir:
            current_mod_dir = config.mod_dir
            print(f"[Config] Watch folder is now: {current_mod_dir}")
        if not current_mod_dir or not current_mod_dir.exists():
            time.sleep(0.5)
            continue

        # Play detection lives on the oplock thread; this loop only keeps settings/dir state fresh.
        time.sleep(0.5)
