from time import time, sleep
import ctypes
import ctypes.wintypes as wt
import hashlib
import itertools
import json
import os
import shutil
import threading
import traceback
from queue import PriorityQueue
from pathlib import Path
from engine.cache_replenishment import rephrase_replacement_line, render_replacement_line

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
SWAP_GIVE_UP_S = 16  # per-attempt cap: the game normally frees a played wem in seconds, not minutes

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


# --- Restart Manager plumbing: name the process(es) holding a file open at swap give-up ---
class _RM_FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("pid", wt.DWORD), ("start", _RM_FILETIME)]

class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [("proc", _RM_UNIQUE_PROCESS),
                ("app", wt.WCHAR * 256),
                ("service", wt.WCHAR * 64),
                ("type", wt.DWORD),
                ("status", wt.DWORD),
                ("session", wt.DWORD),
                ("restartable", wt.BOOL)]

_SESSION_KEY_BUF = wt.WCHAR * 33  # CCH_RM_SESSION_KEY (32) + terminator
try:
    _RM_DLL = ctypes.WinDLL("rstrtmgr.dll")
    _RM_DLL.RmStartSession.restype = wt.DWORD
    _RM_DLL.RmStartSession.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, ctypes.POINTER(_SESSION_KEY_BUF)]
    _RM_DLL.RmRegisterResources.restype = wt.DWORD
    _RM_DLL.RmRegisterResources.argtypes = [wt.DWORD, wt.UINT, ctypes.POINTER(wt.LPCWSTR), wt.UINT,
                                            ctypes.POINTER(_RM_UNIQUE_PROCESS), wt.UINT,
                                            ctypes.POINTER(wt.LPCWSTR)]
    _RM_DLL.RmGetList.restype = wt.DWORD
    _RM_DLL.RmGetList.argtypes = [wt.DWORD, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT),
                                  ctypes.POINTER(_RM_PROCESS_INFO), ctypes.POINTER(wt.DWORD)]
    _RM_DLL.RmEndSession.restype = wt.DWORD
    _RM_DLL.RmEndSession.argtypes = [wt.DWORD]
except OSError:
    _RM_DLL = None  # diagnostic only: degrade silently


def _rm_holder_names(file_path: Path) -> tuple:
    """(foreign_names, self_present): open-handle holders per Restart Manager; ([], False) on failure."""
    if _RM_DLL is None:
        return [], False
    session, key = wt.DWORD(0), _SESSION_KEY_BUF()
    if _RM_DLL.RmStartSession(ctypes.byref(session), 0, ctypes.byref(key)) != 0:
        return [], False
    try:
        if _RM_DLL.RmRegisterResources(session, 1, (wt.LPCWSTR * 1)(str(file_path)), 0, None, 0, None) != 0:
            return [], False
        needed, count, reasons = wt.UINT(0), wt.UINT(0), wt.DWORD(0)
        # NULL buffer first: ERROR_MORE_DATA (234) reports the true holder count in `needed`
        rc = _RM_DLL.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        if needed.value == 0 or (rc != 0 and rc != 234):
            return [], False
        count = wt.UINT(min(needed.value, 64))
        buf = (_RM_PROCESS_INFO * count.value)()
        if _RM_DLL.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), buf, ctypes.byref(reasons)) != 0:
            return [], False
        names, self_present, my_pid = [], False, os.getpid()
        for info in buf[:count.value]:
            if info.proc.pid == my_pid:
                self_present = True
                continue  # our own handles share delete, so they are never the replace blocker
            if info.app and info.app not in names:
                names.append(info.app)
        return names, self_present
    finally:
        _RM_DLL.RmEndSession(session)


_holder_report_lock = threading.Lock()
_holder_report_last = {}

def _log_swap_holders(dst_path: Path, wem_id: str):
    """Sole give-up line (one per held wem per 90s): who (or what) keeps the target replace-blocked."""
    with _holder_report_lock:
        now = time()
        if now - _holder_report_last.get(wem_id, 0.0) < 90.0:  # one line per hold incident, not per retry
            return
        _holder_report_last[wem_id] = now
    try:
        names, self_present = _rm_holder_names(dst_path)
    except Exception:
        return  # diagnostic only, never block the serve path
    note = f"retrying every 1s, attempt cap {SWAP_GIVE_UP_S}s"
    if names:
        print(f"[Cache] {dst_path.name} is held open by: {', '.join(names)} - {note}.")
    elif self_present:
        print(f"[Cache] {dst_path.name} replace denied, RM only sees this engine (own handles don't block) "
              f"- blocker is likely a memory-mapped section in the game; {note}.")
    else:
        print(f"[Cache] {dst_path.name} replace denied with no RM-visible holder - blocker is likely a "
              f"memory-mapped section in the game (FMOD); {note}.")


_OVERWRITE_HELD_WEMS = True  # write new variant bytes into a replace-locked wem (needs holder write-share)
_OVERWRITE_DELAY_S = 0.2  # write the moment replace is denied: the NEXT play must see this variant
_OVERWRITE_DENY_TTL_S = 120  # denial expiry: a later serve re-probes in case the holder's share changed
_overwrite_denied_until = {}  # wem_id -> monotonic time until which overwrite skips this line
_posix_denied_ids = set()  # wems whose holder shares no delete: posix rename logs once and is skipped

_FILE_RENAME_INFO_EX_CLASS = 22  # FileRenameInfoEx in FILE_INFO_BY_HANDLE_CLASS
_RENAME_REPLACE_POSIX = 0x3  # FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS


class _RenameInfoEx(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("RootDirectory", wt.HANDLE),
                ("FileNameLength", wt.DWORD), ("FileName", ctypes.c_wchar * 1024)]


def _posix_replace_held(dst_path: Path, src_path: Path) -> int:
    """POSIX-semantics rename of src over dst while any holder keeps it open;
    returns 0 on success or the winerror (32 = a holder shares no delete)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
    k32.SetFileInformationByHandle.restype = wt.BOOL
    k32.SetFileInformationByHandle.argtypes = [wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD]
    # DELETE access on the SOURCE (our cache file) + POSIX flags replace the target entry
    # without delete rights on the target's holders.
    handle = k32.CreateFileW(str(src_path), 0x00010000, 0x7, None, 3, 0x80, None)
    if handle in (None, wt.HANDLE(-1).value):
        return ctypes.get_last_error() or 2
    info = _RenameInfoEx()
    info.Flags = _RENAME_REPLACE_POSIX
    dst_str = str(dst_path)
    info.FileNameLength = len(dst_str) * 2
    info.FileName = dst_str  # type: ignore[misc]
    ok = k32.SetFileInformationByHandle(handle, _FILE_RENAME_INFO_EX_CLASS, ctypes.byref(info),
                                        ctypes.sizeof(info))
    err = 0 if ok else ctypes.get_last_error()
    k32.CloseHandle(handle)
    return err


def _try_posix_swap(config, dst_path: Path, src_path: Path, wem_id: str) -> bool:
    """One POSIX-rename shot per serve; logs the holder's no-delete-share once per line."""
    err = _posix_replace_held(dst_path, src_path)
    if err == 0:
        record_mod_manifest(config, wem_id)
        print(f"[Cache] POSIX-swapped held {dst_path.name} - fresh audio live for the next play.")
        return True
    if err in (2, 3):
        return False  # src already claimed by a concurrent serve of the same line: benign race
    if err != 32 or wem_id not in _posix_denied_ids:
        _posix_denied_ids.add(wem_id)
        print(f"[Cache] POSIX rename on {dst_path.name} denied (winerror {err}) - "
              f"{'holder shares no delete' if err == 32 else 'see winerror'}; overwrite stays the mid-hold route.")
    return False

def _try_overwrite_held(config, dst_path: Path, src_path: Path, wem_id: str) -> bool:
    """Rewrite the held wem's content in place with the new variant (no delete rights needed)."""
    try:
        payload = src_path.read_bytes()
        with open(dst_path, "r+b") as fh:
            fh.write(payload)
            fh.truncate()
    except FileNotFoundError:
        return False  # no held content (transient) - the normal replace path handles it
    except PermissionError as d_e:
        # sharing/access denial = the holder shares no write; re-probe after a TTL, not never
        _overwrite_denied_until[wem_id] = time() + _OVERWRITE_DENY_TTL_S
        code = getattr(d_e, "winerror", None) or d_e.errno
        print(f"[Cache] write-open on {dst_path.name} denied (system error {code}) - "
              f"in-place overwrite paused for this line ({_OVERWRITE_DENY_TTL_S}s).")
        return False
    except OSError:
        return False  # transient I/O issue - the normal replace path continues
    try:
        src_path.unlink()
    except OSError:
        pass  # stray cache copy; the next serve's listing treats it as a normal variant
    record_mod_manifest(config, wem_id)
    print(f"[Cache] overwrote held {dst_path.name} in place - fresh audio live for the next play.")
    return True


def move_cachedfile_to_mod_dir(config, src_path: Path, mod_dir: Path, wem_id: str, on_attempt=None):
    """Moves a cached WEM file into the mod dir and records the voice/settings era in the
    folder manifest. Ladder: replace, POSIX rename every cycle, in-place overwrite (once, TTL-gated)."""
    mod_dir.mkdir(parents=True, exist_ok=True)
    dst_path = mod_dir / f"{wem_id}.wem"

    start_time = time()
    attempt = 0
    overwrite_tried = False
    while True:
        try:
            src_path.replace(dst_path)
            record_mod_manifest(config, wem_id)
            return dst_path
        except FileNotFoundError:
            return None  # variant already claimed by a concurrent serve of the same line
        except (PermissionError, OSError):
            if time() - start_time > SWAP_GIVE_UP_S:
                _log_swap_holders(dst_path, wem_id)
                return None
            attempt += 1
            if time() - start_time >= _OVERWRITE_DELAY_S:
                # POSIX rename is the mid-hold workhorse: verified to succeed over an ARMED
                # target and over delete-sharing holders, so re-fire it every cycle until it lands
                if _try_posix_swap(config, dst_path, src_path, wem_id):
                    return dst_path
                if not overwrite_tried and _OVERWRITE_HELD_WEMS \
                        and _overwrite_denied_until.get(wem_id, 0) < time():
                    if on_attempt:
                        on_attempt()  # this write-open breaks our armed oplock; guard exactly this slice
                    if _try_overwrite_held(config, dst_path, src_path, wem_id):
                        return dst_path
                overwrite_tried = overwrite_tried or time() - start_time >= _OVERWRITE_DELAY_S
            # exponential ramp 0.1s -> 0.5s cap: steady 10 Hz retry bursts can trip AV heuristics
            sleep(min(0.5, 0.05 * (2 ** min(attempt, 4))))

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

def _maybe_start_filler_round(just_served=None):
    """One filler round ('1c..35c'): +1 variant per tracked line under cache depth; chained rounds
    grow depth a level at a time. just_served counts the file moving out of cache as already gone."""
    global _filler_round_pending
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
    depth_sum = depth_count = 0
    for wem_id, info in config.intent_map.items():
        if str(info.get("Used", "")).strip().lower() != "yes":
            continue
        wem_dir = Path(config.quick_cache_dir) / str(wem_id)
        with _jobs_lock:
            pending = _active_jobs_count.get(str(wem_id), 0)
        have = len(list(wem_dir.glob(f"{wem_id}_{prefix_base}*.wem"))) if wem_dir.is_dir() else 0
        if wem_id == just_served and have:
            have -= 1  # the served variant leaves the cache as soon as we return
        under = have + pending < max_depth
        if under:
            to_queue.append((wem_id, wem_dir))
        depth_sum += have + pending + (1 if under else 0)
        depth_count += 1
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
    depth_now = depth_sum / depth_count if depth_count else 0
    print(f"[Cache] Filler round: +1 variant for {len(to_queue)} line(s) - depth now {depth_now:.1f}/{max_depth}.")


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
        _maybe_start_filler_round(just_served=wem_id)


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
            # Top up to the expected net inventory without double-queuing. The served
            # file is about to leave the cache (moved to the mod dir), so count it gone.
            _enqueue_restock(config, id_str, wem_dir, rephrase, current_inventory - 1)
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
                # The served file leaves the cache right after return: count it gone.
                _enqueue_restock(config, id_str, wem_dir, rephrase,
                                 len(cached_files) + latest_pending - 1)
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
