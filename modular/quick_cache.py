from time import time, sleep
import hashlib
import itertools
import json
import threading
import traceback
from queue import PriorityQueue
from pathlib import Path
from modular.cache_replenishment import generate_replacement_voice_line

# Priority queue: lines the game just fired (high) render before background
# voice-file refreshes (low), so gameplay never waits behind housekeeping.
_job_queue = PriorityQueue()
_job_seq = itertools.count()
_worker_thread: threading.Thread | None = None
_MANIFEST_FILENAME = "mod_folder_manifest.json"
_FALLBACK_WAIT_TIMEOUT_S = 120  # max wait for an on-demand generation before skipping

# Track how many active background tasks are currently scheduled per WEM ID
_active_jobs_count = {}
_jobs_lock = threading.Lock()


def _effects_fingerprint(config) -> str:
    """Short fingerprint of the current voice + effects era."""
    try:
        payload = json.dumps(getattr(config, "voice_effects", {}), sort_keys=True, default=str)
    except Exception:
        payload = "unknown"
    era = f"{getattr(config, 'tts_voice', '')}|{payload}"
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
        "fingerprint": _effects_fingerprint(config),
    }
    save_mod_manifest(config, manifest)

def enqueue_job(func, *args, priority=10, **kwargs):
    """Add a job to the queue and track its allocation. Tiers: 0 = gameplay
    lines the player waits on, 10 = the voice-file sweep, 20 = restock prefetch."""
    start_worker()  # ensure worker is running

    # If it's a replacement job, increment the tracking counter for this WEM ID
    if func == _replacement_wrapper:
        wem_id = args[1]
        with _jobs_lock:
            _active_jobs_count[wem_id] = _active_jobs_count.get(wem_id, 0) + 1

    _job_queue.put((priority, next(_job_seq), (func, args, kwargs)))

def _worker_loop():
    """Simple worker loop for queued tasks. A failed job is logged and the
    thread keeps serving the queue instead of dying silently."""
    while True:
        _priority, _seq, (func, args, kwargs) = _job_queue.get()
        try:
            func(*args, **kwargs)
        except Exception:
            print(f"[Worker] Generation job failed: {func.__name__} {args!r}")
            traceback.print_exc()
        finally:
            # Decrement the tracking counter when the background generation pass completes
            if func == _replacement_wrapper:
                wem_id = args[1]
                with _jobs_lock:
                    if wem_id in _active_jobs_count:
                        _active_jobs_count[wem_id] = max(0, _active_jobs_count[wem_id] - 1)
            _job_queue.task_done()

def start_worker():
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
        _worker_thread.start()

def _replacement_wrapper(config, wem_id: str, wem_dir: Path, rephrase: bool = True):
    """Generates a replacement WEM."""
    generate_replacement_voice_line(config, wem_id, wem_dir, rephrase=rephrase)


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


def _voice_tag(voice: str) -> str:
    """Filesystem-safe cache filename tag for a voice name."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(voice)) or "unknown"


def get_cached_wem(config, wem_id, top_up=True, rephrase=True, priority=0):
    """Serve a voice-tagged cache variant, generating one on demand (single-use;
    stale-voice eras purged on sight). The caller's priority tier applies to the
    on-demand fallback render (watcher = 0, sweep = 10); restock is always 20."""
    wem_dir = config.quick_cache_dir / str(wem_id)
    wem_dir.mkdir(parents=True, exist_ok=True)

    # Clean target lookup ID
    id_str = str(wem_id).strip()
    prefix = f"{id_str}_{_voice_tag(getattr(config, 'tts_voice', '') or 'unknown')}_"

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
            # Top up to the expected net inventory without double-queuing. Restock
            # is prefetch (priority 20): it must yield to the sweep and to gameplay.
            missing_count = max(0, config.quick_cache_max - current_inventory)
            if missing_count:
                print(f"[Cache] Restocking {missing_count} variant(s) for WEM {id_str} (background)...")
                for _ in range(missing_count):
                    enqueue_job(_replacement_wrapper, config, wem_id, wem_dir, rephrase, priority=20)
        return cached_files[0]

    # FALLBACK GATE: If an inactive file is missing, enqueue ONE immediately if none are pending
    if pending_jobs == 0:
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
                net_total = len(cached_files) + latest_pending
                missing_count = max(0, config.quick_cache_max - net_total)
                if missing_count:
                    print(f"[Cache] Restocking {missing_count} variant(s) for WEM {id_str} (background)...")
                    for _ in range(missing_count):
                        enqueue_job(_replacement_wrapper, config, wem_id, wem_dir, rephrase, priority=20)
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
