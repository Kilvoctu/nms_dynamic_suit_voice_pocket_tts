# engine/oplock_probe.py
"""Oplock probe: prove that second-process opens (read/mmap) break a level-1 oplock
held by this process, and that attribute-only opens/stat do not. Stdlib only."""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
FSCTL_REQUEST_OPLOCK_LEVEL_1 = 0x00090000
ERROR_IO_PENDING = 997

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [ctypes.c_wchar_p, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                            wt.DWORD, wt.DWORD, ctypes.c_void_p]
k32.DeviceIoControl.restype = wt.BOOL
k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CloseHandle.restype = wt.BOOL
k32.CloseHandle.argtypes = [wt.HANDLE]


class _Overlapped(ctypes.Structure):
    _fields_ = [("internal", ctypes.c_void_p), ("internal_high", ctypes.c_void_p),
                ("offset", wt.DWORD), ("offset_high", wt.DWORD), ("hEvent", wt.HANDLE)]


def open_shared(path):
    """Open path with read access, share-all, overlapped flag for async FSCTLs."""
    h = k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING,
                        FILE_FLAG_OVERLAPPED, None)
    if h in (None, 0xFFFFFFFFFFFFFFFF):
        raise OSError(f"open failed err={ctypes.get_last_error()}")
    return h


def arm_level1(handle):
    """Async-request a level-1 oplock; returns (overlapped, state string)."""
    ov = _Overlapped()
    ov.hEvent = k32.CreateEventW(None, True, False, None)
    got = wt.DWORD(0)
    ok = k32.DeviceIoControl(handle, FSCTL_REQUEST_OPLOCK_LEVEL_1, None, 0, None, 0,
                             ctypes.byref(got), ctypes.byref(ov))
    err = ctypes.get_last_error()
    if ok:
        return ov, "granted-immediately"
    if err == ERROR_IO_PENDING:
        return ov, "armed"
    return ov, f"refused(err {err})"


def wait_break(handle, ov, timeout):
    """True when the pending oplock request completed (a break arrived)."""
    state = k32.WaitForSingleObject(ov.hEvent, int(timeout * 1000))
    if state != 0:
        return False
    got = wt.DWORD(0)
    ok = k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(got), False)
    err = ctypes.get_last_error()
    print(f"  BREAK fired: result_ok={bool(ok)} err={err} bytes={got.value}")
    return True


TRIGGER = r'''
import mmap, os, sys, time
action, path = sys.argv[2], sys.argv[3]
t0 = time.monotonic()
if action == "open_read":
    with open(path, "rb") as f:
        f.read()
elif action == "mmap":
    with open(path, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            _ = m[0:16]
elif action == "open_only":
    fd = os.open(path, os.O_RDONLY)
    os.close(fd)
elif action == "stat":
    os.stat(path)
    os.stat(path)
print(f"{action} done in {(time.monotonic()-t0)*1000:.0f}ms")
'''


def run_case(action, path, timeout=5.0):
    h = open_shared(path)
    ov, state = arm_level1(h)
    print(f"=== {action}: oplock request -> {state}")
    if not state.startswith(("granted", "armed")):
        k32.CloseHandle(h)
        return
    t0 = time.monotonic()
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--trigger", action, path],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    broke = wait_break(h, ov, timeout)
    k32.CloseHandle(h)  # close = acknowledge; unblocks the opener
    out = child.communicate(timeout=15)[0].strip()
    dt = time.monotonic() - t0
    print(f"  child: {out}")
    verdict = "BREAK (detection possible)" if broke else "no break (useless for detection)"
    print(f"  => {verdict}  (observed after {dt:.2f}s)")
    time.sleep(0.3)


def main():
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-") and sys.argv[1] != "--trigger":
        target = sys.argv[1]
        for action in ("open_read", "mmap", "open_only", "stat"):
            run_case(action, target)
        return
    tmp = os.path.join(os.environ.get("TEMP", r"C:\temp\opencode"), "oplock_probe.bin")
    with open(tmp, "wb") as f:
        f.write(b"x" * 4096)
    for action in ("open_read", "mmap", "open_only", "stat"):
        run_case(action, tmp)
    try:
        os.unlink(tmp)
    except OSError:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--trigger":
        exec(TRIGGER)
    else:
        main()
