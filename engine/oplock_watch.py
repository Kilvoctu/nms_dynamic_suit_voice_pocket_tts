# engine/oplock_watch.py
"""Level-1 oplock play detector: game plays leave no atime, USN-journal or directory-event
traces, but their opens still break our oplocks, which names the exact file that got played."""
import ctypes
import ctypes.wintypes as wt
import threading
import time

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
FSCTL_REQUEST_OPLOCK_LEVEL_1 = 0x00090000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = ctypes.c_void_p
_k32.CreateFileW.argtypes = [ctypes.c_wchar_p, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                             wt.DWORD, wt.DWORD, ctypes.c_void_p]
_k32.DeviceIoControl.restype = wt.BOOL
_k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.CloseHandle.restype = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.WaitForSingleObject.restype = wt.DWORD
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_k32.WaitForMultipleObjects.restype = wt.DWORD
_k32.WaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.POINTER(wt.HANDLE), wt.BOOL, wt.DWORD]


class _Overlapped(ctypes.Structure):
    _fields_ = [("internal", ctypes.c_void_p), ("internal_high", ctypes.c_void_p),
                ("offset", wt.DWORD), ("offset_high", wt.DWORD), ("hEvent", wt.HANDLE)]


class _Entry:
    """One tracked file: an armed oplock (handle + pending request) or a re-arm slot."""
    __slots__ = ("path", "handle", "ov", "broken", "next_try", "fails")

    def __init__(self, path):
        self.path = path
        self.handle = None
        self.ov = None
        self.broken = True  # True = needs (re)arming; new entries start unarmed
        self.next_try = 0.0
        self.fails = 0


class OplockWatch:
    """Arms level-1 oplocks on tracked wem files; poll_ack() reports files whose
    oplock broke (someone opened them). Maintains re-arms; stat/open-by-us is safe."""

    def __init__(self, poll_interval=0.02):
        self._entries = {}
        self._dir = None
        self._poll = poll_interval
        self._suppressed = {}
        self._lock = threading.RLock()
        self._wait_ids = []
        self._wait_chunks = []
        self._wait_dirty = True

    # --- lifecycle -------------------------------------------------------
    def set_target(self, directory, ids):
        """Arm missing ids, drop removed ones; re-homes everything on dir change."""
        with self._lock:
            directory = str(directory)
            if self._dir != directory:
                self._close_all()
                self._dir = directory
            for wem_id in ids:
                if wem_id not in self._entries:
                    self._entries[wem_id] = _Entry(f"{directory}\\{wem_id}.wem")
            for wem_id in [k for k in self._entries if k not in set(ids)]:
                self._discard(wem_id)

    def close(self):
        with self._lock:
            self._close_all()
            self._dir = None

    def _close_all(self):
        for entry in self._entries.values():
            self._shut_handle(entry)
        self._entries.clear()

    def _shut_handle(self, entry):
        # Closing the handle acknowledges any pending break implicitly; the kernel
        # keeps the event object alive until the cancelled I/O derefs it.
        if entry.handle:
            try:
                _k32.CloseHandle(entry.handle)
            except Exception:
                pass
        if entry.ov is not None and entry.ov.hEvent:
            _k32.CloseHandle(entry.ov.hEvent)
        entry.handle, entry.ov = None, None
        self._wait_dirty = True

    def _discard(self, wem_id):
        entry = self._entries.pop(wem_id, None)
        if entry:
            self._shut_handle(entry)

    # --- operations ------------------------------------------------------
    def suppress(self, wem_id, seconds=0.6):
        """Short per-attempt guard: breaks inside the window are our own swap's, not plays."""
        with self._lock:
            self._suppressed[wem_id] = time.monotonic() + seconds

    def rearm(self, wem_id):
        """Immediate re-open + re-request after our own swap of the file."""
        with self._lock:
            entry = self._entries.get(wem_id)
            if entry:
                self._shut_handle(entry)
                entry.broken = True
                entry.next_try = 0.0  # maintain() picks it up on the next call

    def poll_ack(self, timeout=0.02):
        """Kernel-side wait for oplock breaks (WfMO chunks, cached arrays); ack + report ids.
        Idle cost is a handful of kernel calls per cycle no matter how many lines are armed."""
        with self._lock:
            if self._wait_dirty:
                self._rebuild_wait_set()
            if not self._wait_ids:
                return []
            fired = []
            chunk_n = 60
            deadline = time.monotonic() + timeout
            n_chunks = max(1, (len(self._wait_ids) + chunk_n - 1) // chunk_n)
            for chunk_i in range(n_chunks):
                arr, count = self._wait_chunks[chunk_i]
                slice_s = max(0.001, (deadline - time.monotonic()) / (n_chunks - chunk_i))
                state = _k32.WaitForMultipleObjects(
                    count, arr, False, max(1, int(slice_s * 1000)))
                if state < count:  # WAIT_OBJECT_0 + index: something broke in this chunk
                    fired = self._collect_broken()
                    break
            return fired

    def _collect_broken(self):
        """0-timeout scan of all armed entries (only runs when a break was seen)."""
        fired = []
        now = time.monotonic()
        for wem_id, entry in self._entries.items():
            if entry.broken or entry.handle is None or entry.ov is None:
                continue
            if _k32.WaitForSingleObject(entry.ov.hEvent, 0) != WAIT_OBJECT_0:
                continue
            self._shut_handle(entry)  # ack: unblocks the opener right away
            entry.broken = True
            entry.next_try = now + 0.25
            until = self._suppressed.get(wem_id, 0.0)
            if until and until < now:
                self._suppressed.pop(wem_id, None)
            if now < until:
                continue  # our own swap caused this break
            fired.append(wem_id)
        return fired

    def _rebuild_wait_set(self):
        """Rebuild the chunked handle arrays from currently armed entries."""
        self._wait_ids = []
        self._wait_chunks = []
        group = []
        for wem_id, entry in self._entries.items():
            if not entry.broken and entry.handle is not None and entry.ov is not None:
                group.append(entry.ov.hEvent)
                self._wait_ids.append(wem_id)
                if len(group) == 60:
                    self._wait_chunks.append(((wt.HANDLE * 60)(*group), 60))
                    group = []
        if group:
            n = len(group)
            self._wait_chunks.append(((wt.HANDLE * n)(*group), n))
        self._wait_dirty = False
    def maintain(self, tries=12):
        """Re-open and re-request broken/busy entries; returns newly armed count."""
        with self._lock:
            now = time.monotonic()
            armed = 0
            for wem_id, entry in self._entries.items():
                if armed >= tries:
                    break
                if not entry.broken or now < entry.next_try:
                    continue
                entry.next_try = now + 1.0  # rate-limit retry attempts per id
                handle = _k32.CreateFileW(entry.path, GENERIC_READ, FILE_SHARE_ALL,
                                          None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
                if handle in (None, 0xFFFFFFFFFFFFFFFF):
                    continue  # file mid-swap or transiently busy: retry later
                ov = _Overlapped()
                ov.hEvent = _k32.CreateEventW(None, True, False, None)
                got = wt.DWORD(0)
                ok = _k32.DeviceIoControl(handle, FSCTL_REQUEST_OPLOCK_LEVEL_1, None, 0,
                                          None, 0, ctypes.byref(got), ctypes.byref(ov))
                err = ctypes.get_last_error()
                if ok or err == ERROR_IO_PENDING:
                    entry.handle, entry.ov, entry.broken = handle, ov, False
                    entry.fails = 0
                    self._wait_dirty = True
                    armed += 1
                else:
                    _k32.CloseHandle(handle)
                    _k32.CloseHandle(ov.hEvent)
                    entry.fails += 1
                    if entry.fails == 1:  # err 300 = game still holds the file; retries quietly
                        print(f"[Watch] oplock re-arm delayed for {wem_id} (err {err}).")
            return armed

    def armed_count(self):
        with self._lock:
            return sum(1 for e in self._entries.values() if not e.broken and e.handle)
