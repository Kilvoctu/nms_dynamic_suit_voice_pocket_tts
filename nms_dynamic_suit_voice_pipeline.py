# nms_dynamic_suit_voice_pipeline.py (Streamlined Version)
import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path
from modular.quick_cache import (begin_sweep_wave, end_sweep_wave, get_cached_wem,
                                 move_cachedfile_to_mod_dir, update_access_time_to_match_newfile)
from PIL import Image
# NOTE: No global config or TTS instantiations live at the top of this file anymore!

# --- Windows change-notification watch: the OS reports file access, so the watch
# --- loop idles at ~0 cpu instead of stat-polling every wem file each tick.
_KERNEL32 = ctypes.windll.kernel32
_NOTIFY_BUFFER_SIZE = 64 * 1024
_NOTIFY_FILTER_LAST_ACCESS = 0x0020
_NOTIFY_BUFFER_OVERFLOW = 234
class _Overlapped(ctypes.Structure):
    _fields_ = [("internal", ctypes.c_void_p), ("internal_high", ctypes.c_void_p),
                ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]
_CreateFileW = _KERNEL32.CreateFileW
_CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_CreateFileW.restype = wintypes.HANDLE
_ReadDirectoryChangesW = _KERNEL32.ReadDirectoryChangesW
_ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                   wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                   ctypes.POINTER(_Overlapped), wintypes.LPVOID]
_GetOverlappedResult = _KERNEL32.GetOverlappedResult
_GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped),
                                 ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
_WaitForSingleObject = _KERNEL32.WaitForSingleObject


class _NotifyInfo(ctypes.Structure):
    _fields_ = [("next_entry", wintypes.DWORD), ("action", wintypes.DWORD),
                ("name_length", wintypes.DWORD), ("name", wintypes.WCHAR * 1)]

class _AtimeWatch:
    """Arms ReadDirectoryChangesW (LastAccess) on one directory; wait() blocks up to
    the given timeout and returns touched wem names ([] on quiet, None on re-open)."""

    def __init__(self):
        self._handle = None
        self._overlapped = None
        self._buffer = ctypes.create_string_buffer(_NOTIFY_BUFFER_SIZE)
        self._dir = None

    def open(self, directory) -> bool:
        """(Re)arm the watch on directory; False means events are unavailable."""
        self.close()
        try:
            handle = _CreateFileW(str(directory), 0x0001, 1 | 2 | 4, None, 3,
                                  0x02000000 | 0x40000000, None)
            if not handle or handle == wintypes.HANDLE(-1).value:
                return False
            overlapped = _Overlapped()
            overlapped.hEvent = _KERNEL32.CreateEventW(None, False, False, None)
            read = wintypes.DWORD(0)
            ok = _ReadDirectoryChangesW(handle, self._buffer, _NOTIFY_BUFFER_SIZE, False,
                                        _NOTIFY_FILTER_LAST_ACCESS, ctypes.byref(read),
                                        ctypes.byref(overlapped), None)
            if not ok:
                _KERNEL32.CloseHandle(handle)
                return False
            self._handle, self._overlapped, self._dir = handle, overlapped, directory
            return True
        except Exception:
            return False

    def close(self):
        if self._handle:
            try:
                _KERNEL32.CloseHandle(self._handle)
            except Exception:
                pass
        self._handle, self._overlapped, self._dir = None, None, None

    def wait(self, timeout_s: float):
        """Block up to timeout_s; returns touched wem names ([] = quiet, None = rescan)."""
        if not self._handle:
            return None
        wait = _WaitForSingleObject(self._overlapped.hEvent, int(timeout_s * 1000))
        if wait != 0:  # timeout or failure: the read stays armed for the next call
            return []
        read = wintypes.DWORD(0)
        ok = _GetOverlappedResult(self._handle, ctypes.byref(self._overlapped),
                                  ctypes.byref(read), False)
        data_copy = self._buffer.raw[:read.value] if ok and read.value else b""
        if not ok:
            self.open(self._dir)  # error or overflow: re-arm, caller rescans
            return None
        self.open(self._dir)  # re-arm immediately so no window is left unwatched
        names, pos = [], 0
        while pos + 12 <= len(data_copy):
            info = _NotifyInfo.from_buffer_copy(data_copy, pos)
            if info.name_length:
                name = ctypes.wstring_at(ctypes.addressof(info) + 12, info.name_length // 2)
                if name.lower().endswith(".wem"):
                    names.append(name[:-4])
            if not info.next_entry:
                break
            pos += info.next_entry
        return names


def _scan_atimes(directory):
    """True per-file stats: scandir's enumerated atime lags minutes behind on NTFS
    (System-Managed LastAccess updates the MFT but not the directory index)."""
    found = {}
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.endswith(".wem"):
                    try:
                        found[entry.name[:-4]] = os.stat(entry.path).st_atime
                    except OSError:
                        pass
    except OSError:
        pass  # transient (folder busy): skip this pass, the next one catches up
    return found

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

    access_times = _scan_atimes(mod_dir)

    # Cache janitor: untracked dirs, stale-era variants and old temps, in the
    # background so it never delays the boot.
    import threading
    from modular.quick_cache import janitor_pass
    threading.Thread(target=janitor_pass, args=(config,), daemon=True, name="cache-janitor").start()

    # Non-blocking watch: the scanner only detects plays and hands each fire to the serve
    # thread (same-wem fires coalesced), so a slow render never delays detecting the next play.
    import threading
    import queue as _queue

    _watch_lock = threading.Lock()
    _in_flight = set()
    _fire_queue = _queue.Queue()
    _access = access_times  # shared, always touch under _watch_lock

    # CSV rows for logging untracked plays and auto-learning (the intent map may not know the line).
    _csv_rows = {}
    try:
        import csv as _csv
        with open(config.transcripts_path.with_suffix(".csv"), newline="", encoding="utf-8") as _fh:
            _csv_rows = {r["WEM_number"]: r for r in _csv.DictReader(_fh) if r.get("WEM_number")}
    except Exception:
        pass  # logging/learning convenience

    # Re-attach previously auto-learned lines so they are managed from boot.
    try:
        learned = json.loads((config.root_dir / "user" / "learned_transcripts.json").read_text(encoding="utf-8"))
        merged = {k: v for k, v in learned.items()
                  if k not in config.intent_map and str(v.get("Used", "")).strip().lower() == "yes"}
        if merged:
            config.intent_map.update(merged)
            print(f"[Watch] Managing {len(merged)} auto-learned line(s) from user/learned_transcripts.json.")
    except Exception:
        pass  # first run or unreadable file: nothing to merge

    def _learn_line(wem_id: str, row: dict):
        """Add a heard line to the user's learned-transcripts file and the live intent map."""
        entry = {
            "Filename": row.get("Filename", f"{wem_id}.wem"),
            "Transcription": row.get("Transcription", ""),
            "Category": row.get("Category", ""),
            "Intent": row.get("Intent", ""),
            "Used": "Yes",
        }
        learned_path = config.root_dir / "user" / "learned_transcripts.json"
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

    def _serve_worker():
        while True:
            wem_id, fire_dir = _fire_queue.get()
            try:
                if wem_id not in config.intent_map:
                    row = _csv_rows.get(wem_id, {})
                    spoken = str(row.get("Transcription", "")).strip()
                    if row:
                        _learn_line(wem_id, row)
                        print(f"[Watch] Auto-learned line {wem_id} - \"{spoken}\" - it is now managed and revoiced.")
                    else:
                        print(f"[Watch] Untracked line played: {wem_id} - \"{spoken}\" - "
                              f"not present in the transcript CSV; cannot learn it.")
                        with _watch_lock:  # absorb the play's new atime so the scanner doesn't refire
                            stale_file = fire_dir / f"{wem_id}.wem"
                            if stale_file.exists():
                                _access[wem_id] = stale_file.stat().st_atime
                        continue
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

    _atime_watch = _AtimeWatch()
    _atime_watch_open = False
    _last_safety_scan = 0.0

    def _consider(wem_id, fresh_atime):
        """Coalesced dispatch check: True when this play must be served."""
        with _watch_lock:
            if fresh_atime == _access.get(wem_id, 0) or wem_id in _in_flight:
                return False
            _in_flight.add(wem_id)  # one serve per line at a time
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
                    threading.Thread(target=_refresh_mod_folder, daemon=True, name="mod-folder-refresh").start()
        if config.mod_dir != current_mod_dir:
            current_mod_dir = config.mod_dir
            print(f"[Config] Watch folder is now: {current_mod_dir}")
            if current_mod_dir and current_mod_dir.exists():
                with _watch_lock:
                    _access.clear()
                    _access.update(_scan_atimes(current_mod_dir))
            _atime_watch_open = _atime_watch.open(current_mod_dir) if current_mod_dir else False
        if not current_mod_dir or not current_mod_dir.exists():
            time.sleep(config.check_interval)
            continue
        if not _atime_watch_open:
            _atime_watch_open = _atime_watch.open(current_mod_dir)

        # Event-driven detection: wait() blocks (near-zero idle cpu) and returns the names the OS saw being read.
        touched = _atime_watch.wait(max(config.check_interval, 0.25))
        now = time.time()
        candidates = []
        if touched is None or now - _last_safety_scan >= 5.0:
            candidates = list(_scan_atimes(current_mod_dir).items())
            _last_safety_scan = now
        for wem_id in (touched or []):
            try:
                candidates.append((wem_id, os.stat(current_mod_dir / f"{wem_id}.wem").st_atime))
            except OSError:
                pass
        for wem_id, fresh_atime in candidates:
            if _consider(wem_id, fresh_atime):
                _fire_queue.put((wem_id, current_mod_dir))
