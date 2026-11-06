# engine/posix_swap_probe.py
"""Probe: can a POSIX-semantics rename (FileRenameInfoEx) replace a file that a holder
process keeps open without share-delete AND mapped in memory (the FMOD simulation)?"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

DELETE = 0x00010000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FileRenameInfoEx = 22
FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x1
FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x2

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID,
                            wt.DWORD, wt.DWORD, wt.HANDLE]
k32.SetFileInformationByHandle.restype = wt.BOOL
k32.SetFileInformationByHandle.argtypes = [wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]


class FILE_RENAME_INFO_EX(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("RootDirectory", wt.HANDLE),
                ("FileNameLength", wt.DWORD), ("FileName", wt.WCHAR * 1024)]


def posix_rename(src: str, dst: str):
    """Rename src over dst with POSIX semantics; returns (ok, err)."""
    h = k32.CreateFileW(src, DELETE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                        None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if h in (None, wt.HANDLE(-1).value):
        return False, ctypes.get_last_error()
    info = FILE_RENAME_INFO_EX()
    info.Flags = FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS
    info.FileNameLength = len(dst) * 2
    info.FileName = dst  # type: ignore[misc]
    ok = k32.SetFileInformationByHandle(h, FileRenameInfoEx, ctypes.byref(info),
                                        ctypes.sizeof(info))
    err = ctypes.get_last_error()
    k32.CloseHandle(h)
    return bool(ok), err


HOLDER_RUN = r'''
import ctypes, mmap, os, sys, time
path, ready, go = sys.argv[2], sys.argv[3], sys.argv[4]
k32 = ctypes.WinDLL("kernel32")
h = k32.CreateFileW(path, 0x80000000, 0x1, None, 3, 0x80, None)
open(ready, "w").write("open")
while not os.path.exists(go):
    time.sleep(0.05)
m = mmap.mmap(int(h), 0, access=mmap.ACCESS_READ)
_ = m[0:8]
time.sleep(120)
'''


def run_test(target, label):
    print(f"=== {label}: {target}")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "wb") as f:
        f.write(b"OLD-CONTENT-AAAA")
    new_file = target + ".new"
    with open(new_file, "wb") as f:
        f.write(b"NEW-CONTENT-BBBB")
    ready, go = target + ".ready", target + ".go"
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--hold", target, ready, go],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        if os.path.exists(ready):
            break
        time.sleep(0.05)
    time.sleep(0.3)
    # sanity: the NORMAL replace path against this holder must fail
    try:
        os.replace(new_file, target)
        print("  normal os.replace: UNEXPECTEDLY SUCCEEDED (holder too weak)")
        os.replace(target, new_file)  # undo for the posix attempt
    except OSError as e:
        print(f"  normal os.replace: denied as expected (winerror {getattr(e, 'winerror', e.errno)})")
    ok, err = posix_rename(new_file, target)
    print(f"  POSIX rename: ok={ok} err={err}")
    if ok:
        with open(target, "rb") as f:
            print(f"  fresh open reads: {f.read().decode(errors='replace')}")
    child.kill()
    child.wait()
    for p in (target, new_file, ready, go):
        try:
            os.unlink(p)
        except OSError:
            pass
    return ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--hold":
        exec(HOLDER_RUN)
    else:
        targets = [r"C:\temp\opencode\posix_probe_t.bin"]
        if len(sys.argv) > 1:
            targets.append(sys.argv[1])
        results = [run_test(t, lbl) for t, lbl in zip(targets, ("C: temp", "game folder (G:)"))]
        print("PASS" if all(results) else "FAIL")
