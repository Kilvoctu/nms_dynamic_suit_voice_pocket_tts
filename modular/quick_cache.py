from time import time, sleep
import hashlib
import itertools
import json
import os
import shutil
import threading
import traceback
from queue import PriorityQueue
from pathlib import Path
from modular.cache_replenishment import rephrase_replacement_line, render_replacement_line

# Priority queues: stage 1 rephrases with the LLM, stage 2 renders TTS+WEM, on separate workers.
# Tiers: 0 = lines the player waits on, 10 = the voice-file sweep, 20 = restock core, 30 = filler.
_rephrase_queue = PriorityQueue()
_render_queue = PriorityQueue()
_job_seq = itertools.count()
_render_seq = itertools.count()
_llm_worker_thread: threading.Thread | None = None
_tts_worker_thread: threading.Thread | None = None
_MANIFEST_FILENAME = "mod_folder_manifest.json"
_FALLBACK_WAIT_TIMEOUT_S = 120  # max wait for an on-demand generation before skipping
_RESTOCK_CORE = 2  # variants per line topped up at serve time; deeper filler runs in rounds

# Track how many active background tasks are currently scheduled per WEM ID
_active_jobs_count = {}
_gameplay_pending = {}
_jobs_lock = threading.Lock()

# Wave discipline: filler rounds wait until no sweep wave is mid-pass, then grow depth
# one level at a time in line order, each round chained off the previous round's completion.
_sweep_wave_depth = 0
_filler_round_pending = 0
_restock_config = None
_queue_status_config = None  # most recent config seen, for queue-depth publishing
_last_published_pending = -1
_publish_lock = threading.Lock()  # serialize sidecar writes so they can't go stale


def _settings_fingerprint(config) -> str:
    """Short fingerprint of the current voice, effects, and phrasing-style era."""
    try:
        payload = json.dumps(getattr(config, "voice_effects", {}), sort_keys=True, default=str)
    except Exception:
        payload = "unknown"
    era = "|".join([
        str(getattr(config, "tts_voice", "")),
        payload,
        str(getattr(config, "current_tone", "")),
        str(getattr(config, "current_wordiness", "")),
        str(getattr(config, "player_name", "")),
        str(getattr(config, "suit_voice_base", "")),
    ])
    return hashlib.sha1(era.encode("utf-8")).hexdigest()[:12]


def _manifest_path(config) -> Path:
    return Path(config.quick_cache_dir).parent / _MANIFEST_FILENAME


def load_mod_manifest(config) -> dict:
    try:
        return json.loads(_manifest_path(config).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_mod_manifest(config, manifest: dict) -> None:
    try:
        _manifest_path(config).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[Manifest] Could not save mod folder manifest: {exc}")


def record_mod_manifest(config, wem_id) -> None:
    """Record which voice/settings era produced the mod-folder file for wem_id."""
    manifest = load_mod_manifest(config)
    manifest[str(wem_id)] = {
        "voice": str(getattr(config, "tts_voice", "")),
        "fingerprint": _settings_fingerprint(config),
    }
    save_mod_manifest(config, manifest)

def pending_job_count() -> int:
    """Total background jobs currently queued or rendering, across all tiers."""
    with _jobs_lock:
        return sum(_active_jobs_count.values())

def _publish_queue_depth(config=None):
    """Mirror the pending job count to cache/pipeline_queue.json (manager status bar).
    Count is read under the publish lock so concurrent writes can't go stale. Non-fatal."""
    global _queue_status_config, _restock_config, _last_published_pending
    if config is not None:
        _queue_status_config = config
        _restock_config = config
    cfg = config or _queue_status_config
    if cfg is None:
        return
    with _publish_lock:
        total = pending_job_count()
        if total == _last_published_pending:
            return
        _last_published_pending = total
        path = _manifest_path(cfg).parent / "pipeline_queue.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"pending": total, "pid": os.getpid(), "updated": time()}),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            pass

def enqueue_job(func, *args, priority=10, **kwargs):
    """Add a job to the rephrase stage and track its allocation. Tiers: 0 = gameplay
    lines the player waits on, 10 = the voice-file sweep, 20 = restock core, 30 = filler."""
    start_worker()  # ensure worker is running

    # If it's a replacement job, increment the tracking counter for this WEM ID
    if func == _replacement_wrapper:
        wem_id = args[1]
        kwargs = dict(kwargs)
        kwargs.setdefault("priority", priority)
        with _jobs_lock:
            _active_jobs_count[wem_id] = _active_jobs_count.get(wem_id, 0) + 1
            if priority == 0:
                _gameplay_pending[wem_id] = _gameplay_pending.get(wem_id, 0) + 1
        _publish_queue_depth(args[0] if args else None)

    _rephrase_queue.put((priority, next(_job_seq), (func, args, kwargs)))

def _decrement_job(wem_id: str, priority: int):
    """Job lifecycle end (render done or rephrase failed): free the wem's slot.
    Finishing the last filler job of a round chains the next depth round."""
    chain_round = False
    with _jobs_lock:
        if wem_id in _active_jobs_count:
            _active_jobs_count[wem_id] = max(0, _active_jobs_count[wem_id] - 1)
        if priority == 0 and wem_id in _gameplay_pending:
            _gameplay_pending[wem_id] = max(0, _gameplay_pending[wem_id] - 1)
        global _filler_round_pending
        if priority == 30 and _filler_round_pending > 0:
            _filler_round_pending -= 1
            chain_round = _filler_round_pending == 0
    if chain_round:
        _maybe_start_filler_round()
    _publish_queue_depth()


def _llm_worker_loop():
    """Stage 1: rephrase with the LLM, then hand the text to the render stage."""
    while True:
        _priority, _seq, (func, args, kwargs) = _rephrase_queue.get()
        try:
            func(*args, **kwargs)
        except Exception:
            print(f"[Worker] Rephrase failed: {func.__name__} {args!r}")
            traceback.print_exc()
            if func == _replacement_wrapper:
                _decrement_job(args[1], kwargs.get("priority", 10))
        finally:
            _rephrase_queue.task_done()

def _render_worker_loop():
    """Stage 2: TTS render + encode. Owes the job-slot decrement when done."""
    while True:
        _priority, _seq, (config, wem_id, wem_dir, reworded) = _render_queue.get()
        try:
            render_replacement_line(config, wem_id, wem_dir, reworded)
        except Exception:
            print(f"[Worker] Render failed: {wem_id}")
            traceback.print_exc()
        finally:
            _decrement_job(wem_id, _priority)
            _render_queue.task_done()

def start_worker():
    global _llm_worker_thread, _tts_worker_thread
    if _llm_worker_thread is None or not _llm_worker_thread.is_alive():
        _llm_worker_thread = threading.Thread(target=_llm_worker_loop, daemon=True, name="llm-stage")
        _llm_worker_thread.start()
    if _tts_worker_thread is None or not _tts_worker_thread.is_alive():
        _tts_worker_thread = threading.Thread(target=_render_worker_loop, daemon=True, name="tts-stage")
        _tts_worker_thread.start()

def _replacement_wrapper(config, wem_id: str, wem_dir: Path, rephrase: bool = True, priority: int = 10):
    """Stage-1 job body: reword via LLM, then hand off to the TTS render stage."""
    reworded = rephrase_replacement_line(config, wem_id, rephrase=rephrase)
    if reworded is not None:
        _render_queue.put((priority, next(_render_seq), (config, wem_id, wem_dir, reworded)))


def move_cachedfile_to_mod_dir(config, src_path: Path, mod_dir: Path, wem_id: str):
    """Moves a cached WEM file to the mod_dir, renames it, and records which
    voice/settings era produced it in the mod folder manifest."""
    mod_dir.mkdir(parents=True, exist_ok=True)
    dst_path = mod_dir / f"{wem_id}.wem"

    start_time = time()
    while True:
        try:
            src_path.replace(dst_path)
            record_mod_manifest(config, wem_id)
            return dst_path
        except (PermissionError, OSError):
            if time() - start_time > 120:
                return None
            sleep(0.1)

def update_access_time_to_match_newfile(file_path: Path, access_times: dict):
    """Updates the access_times dict with the atime of the given file."""
    access_times[file_path.stem] = file_path.stat().st_atime


def janitor_pass(config):
    """Boot-time cache cleanup (background thread): untracked wem dirs, stale-era
    variants in tracked dirs, and temp wavs older than a day. Logs one summary."""
    removed_variants = removed_dirs = removed_temps = 0
    try:
        cache_root = Path(config.quick_cache_dir)
        if cache_root.is_dir():
            prefix_base = f"{_voice_tag(getattr(config, 'tts_voice', '') or 'unknown')}_{_settings_fingerprint(config)}"
            for entry in list(cache_root.iterdir()):
                if not entry.is_dir() or entry.name in config.intent_map:
                    continue
                # Untracked id (pruned from the transcript map): wholly stale.
                removed_variants += sum(1 for f in entry.glob("*.wem") if f.is_file())
                shutil.rmtree(entry, ignore_errors=True)
                removed_dirs += 1
            for entry in cache_root.iterdir():
                if not entry.is_dir():
                    continue
                prefix = f"{entry.name}_{prefix_base}_"
                for f in entry.glob("*.wem"):
                    if f.name.startswith(prefix):
                        continue
                    try:
                        f.unlink()
                        removed_variants += 1
                    except OSError:
                        pass
        temp_dir = Path(config.temp_wav_dir)
        if temp_dir.is_dir():
            cutoff = time() - 86400  # a day: never touch a render's live temp
            for f in temp_dir.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    try:
                        f.unlink()
                        removed_temps += 1
                    except OSError:
                        pass
    except Exception as j_e:
        print(f"[Cache] Janitor skipped: {j_e}")
        return
    if removed_variants or removed_dirs or removed_temps:
        print(f"[Cache] Janitor: removed {removed_variants} stale variants, "
              f"{removed_dirs} untracked dirs, {removed_temps} old temp files.")
    else:
        print("[Cache] Janitor: cache already clean.")


def _voice_tag(voice: str) -> str:
    """Filesystem-safe cache filename tag for a voice name."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(voice)) or "unknown"


def begin_sweep_wave():
    """Mark a voice-file sweep as mid-pass: filler rounds hold until it ends."""
    global _sweep_wave_depth
    with _jobs_lock:
        _sweep_wave_depth += 1

def end_sweep_wave():
    """Sweep pass over (or aborted): drop the hold and kick the first filler round."""
    global _sweep_wave_depth
    with _jobs_lock:
        _sweep_wave_depth = max(0, _sweep_wave_depth - 1)
    _maybe_start_filler_round()

def _maybe_start_filler_round():
    """One filler round ('1c..35c'): a single extra variant per tracked line under
    cache depth, enqueued in line order; chained rounds grow depth one level at a time."""
    global _filler_round_pending, _restock_config
    config = _restock_config
    if config is None:
        return
    with _jobs_lock:
        if _sweep_wave_depth > 0 or _filler_round_pending > 0:
            return
    max_depth = max(0, int(getattr(config, "quick_cache_max", 16) or 0))
    if max_depth <= _RESTOCK_CORE:
        return
    prefix_base = f"{_voice_tag(getattr(config, 'tts_voice', '') or 'unknown')}_{_settings_fingerprint(config)}_"
    to_queue = []
    for wem_id, info in config.intent_map.items():
        if str(info.get("Used", "")).strip().lower() != "yes":
            continue
        wem_dir = Path(config.quick_cache_dir) / str(wem_id)
        with _jobs_lock:
            pending = _active_jobs_count.get(str(wem_id), 0)
        have = len(list(wem_dir.glob(f"{wem_id}_{prefix_base}*.wem"))) if wem_dir.is_dir() else 0
        if have + pending < max_depth:
            to_queue.append((wem_id, wem_dir))
    if not to_queue:
        return
    # Reserve the round atomically so a chained call can't double-start, and so
    # the counter is already valid if a job somehow completes instantly.
    with _jobs_lock:
        if _sweep_wave_depth > 0 or _filler_round_pending > 0:
            return
        _filler_round_pending += len(to_queue)
    for wem_id, wem_dir in to_queue:
        enqueue_job(_replacement_wrapper, config, wem_id, wem_dir, True, priority=30)
    print(f"[Cache] Filler round: +1 variant for {len(to_queue)} line(s) toward depth {max_depth}.")


def _enqueue_restock(config, wem_id, wem_dir, rephrase, inventory):
    """Serve-time top-up: first _RESTOCK_CORE variants at restock priority now;
    deeper filler is never enqueued here (it runs in rounds after the sweep wave)."""
    global _restock_config
    _restock_config = config
    max_depth = max(0, int(getattr(config, "quick_cache_max", 16) or 0))
    core_missing = max(0, min(_RESTOCK_CORE, max_depth) - inventory)
    if core_missing:
        print(f"[Cache] Restocking {core_missing} variant(s) for WEM {wem_id} (background)...")
        for _ in range(core_missing):
            enqueue_job(_replacement_wrapper, config, wem_id, wem_dir, rephrase, priority=20)
    if max_depth > min(_RESTOCK_CORE, max_depth):
        _maybe_start_filler_round()


def get_cached_wem(config, wem_id, top_up=True, rephrase=True, priority=0):
    """Serve a voice-tagged cache variant, generating one on demand (single-use;
    stale eras purged on sight). Caller priority: watcher 0, sweep 10, core 20, filler 30."""
    wem_dir = config.quick_cache_dir / str(wem_id)
    wem_dir.mkdir(parents=True, exist_ok=True)

    # Clean target lookup ID
    id_str = str(wem_id).strip()
    prefix = f"{id_str}_{_voice_tag(getattr(config, 'tts_voice', '') or 'unknown')}_{_settings_fingerprint(config)}_"

    # Purge variants generated under a different voice/settings era
    for stale in wem_dir.glob("*.wem"):
        if not stale.name.startswith(prefix):
            try:
                stale.unlink()
            except OSError:
                pass

    # Check how many files are currently sitting on the hard drive
    cached_files = sorted(wem_dir.glob(f"{prefix}*.wem"))

    # Extract cache status natively from the pre-loaded configuration layout instance
    should_pre_cache = False
    if id_str in config.intent_map:
        status_flag = config.intent_map[id_str].get("Used", "").strip().lower()
        should_pre_cache = (status_flag == "yes")

    # Calculate how many generation tasks are already pending in the queue pipeline
    with _jobs_lock:
        pending_jobs = _active_jobs_count.get(id_str, 0)

    # Net inventory represents physical files PLUS files currently processing in the background
    current_inventory = len(cached_files) + pending_jobs

    if cached_files:
        if should_pre_cache and top_up:
            # Top up to the expected net inventory without double-queuing.
            _enqueue_restock(config, id_str, wem_dir, rephrase, current_inventory)
        return cached_files[0]

    # FALLBACK GATE: If an inactive file is missing, enqueue ONE immediately if none are pending
    with _jobs_lock:
        gameplay_pending = _gameplay_pending.get(id_str, 0)
    if gameplay_pending == 0:
        if id_str not in config.intent_map:
            # Unknown line: nothing to generate. Returning None lets the watcher
            # leave the existing file alone instead of waiting forever.
            return None
        enqueue_job(_replacement_wrapper, config, wem_id, wem_dir, rephrase, priority=priority)

    # Wait for the fallback render; bail out if the job dies or stalls so the
    # watcher/refresh skips this line instead of waiting forever.
    wait_started = time()
    while True:
        sleep(0.1)  # polling interval
        cached_files = sorted(wem_dir.glob(f"{prefix}*.wem"))
        if cached_files:
            if should_pre_cache and top_up:
                with _jobs_lock:
                    latest_pending = _active_jobs_count.get(id_str, 0)
                _enqueue_restock(config, id_str, wem_dir, rephrase, len(cached_files) + latest_pending)
            return cached_files[0]
        with _jobs_lock:
            still_pending = _active_jobs_count.get(id_str, 0)
        if still_pending == 0:
            if time() - wait_started < 1.0:
                continue  # grace: let the freshly enqueued job register itself
            print(f"[Cache] Generation for WEM {id_str} finished without producing a file; skipping.")
            return None
        if time() - wait_started > _FALLBACK_WAIT_TIMEOUT_S:
            with _jobs_lock:
                still_running = _active_jobs_count.get(id_str, 0)
            print(f"[Cache] Gave up on WEM {id_str} after {_FALLBACK_WAIT_TIMEOUT_S}s "
                  f"(generation still running: {still_running}). The current file stays in place.")
            return None
