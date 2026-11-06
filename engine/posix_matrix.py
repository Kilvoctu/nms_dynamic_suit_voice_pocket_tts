# engine/posix_matrix.py
"""Matrix: which holder share modes let (a) normal replace, (b) POSIX rename,
(c) in-place write-open succeed? Holder can also map a section like FMOD."""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from posix_swap_probe import posix_rename

TMP = os.path.join(os.environ.get("TEMP", r"C:\temp"), "pmatrix")

HOLDER_RUN = r'''
import ctypes, ctypes.wintypes as wt, os, sys, time
path, share, mapped, ready = sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1", sys.argv[5]
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, ctypes.c_void_p]
k32.CreateFileMappingW.restype = ctypes.c_void_p
k32.CreateFileMappingW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
h = k32.CreateFileW(path, 0x80000000, share, None, 3, 0x80, None)
ok = bool(h)
open(ready, "w").write("open" if ok else "fail")
if ok:
    if mapped:
        # FMOD-style: a real data section on the held handle (Python mmap can't take raw handles)
        sec = k32.CreateFileMappingW(h, None, 0x2, 0, 0, None)  # PAGE_READONLY
        view = k32.MapViewOfFile(sec, 0x4, 0, 0, 0)  # FILE_MAP_READ
        if not view:
            open(ready, "w").write(f"map-fail {ctypes.get_last_error()}")
        else:
            _ = ctypes.c_char.from_address(view).value  # touch the mapping
    time.sleep(60)
'''


def one(share, mapped):
    cell = f"t_{share}_{int(mapped)}"
    target = os.path.join(TMP, cell + ".bin")
    new_file = target + ".new"
    ready = target + ".ready"
    for p in (target, new_file, ready):
        if os.path.exists(p):
            os.unlink(p)
    with open(target, "wb") as f:
        f.write(b"OLD-CONTENT")
    with open(new_file, "wb") as f:
        f.write(b"NEW-CONTENT")
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--hold",
                              target, str(share), "1" if mapped else "0", ready],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    status = "start"
    for _ in range(100):
        if os.path.exists(ready):
            status = open(ready).read().strip()
            break
        time.sleep(0.05)
    else:
        status = "no-ready"
    time.sleep(0.3)
    if child.poll() is not None:
        status = "child-died: " + (child.stdout.read() or "").strip().splitlines()[-1] if child.stdout else "child-died"
    res = {"holder": status}
    if status == "open":
        # order matters: write-open first (it does not disturb the file), then replace,
        # then posix; re-create new_file if replace consumed it.
        try:
            open(target, "r+b").close()
            res["write_open"] = True
        except OSError as e:
            code = getattr(e, "winerror", None)
            res["write_open"] = f"err {code if code is not None else e.errno}"
        try:
            os.replace(new_file, target)
            res["replace"] = True
            with open(new_file, "wb") as f:
                f.write(b"NEW-CONTENT")
        except OSError as e:
            res["replace"] = f"err {getattr(e, 'winerror', e.errno)}"
        ok, err = posix_rename(new_file, target)
        res["posix"] = "ok" if ok else f"err {err}"
    child.kill()
    child.wait()
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    return res


def main():
    modes = [(0x1, "R"), (0x5, "R|D"), (0x3, "R|W"), (0x7, "R|W|D")]
    os.makedirs(TMP, exist_ok=True)
    print(f"{'holder share':<12} {'mapped':<7} {'replace':<9} {'posix':<9} {'write-open':<10} holder")
    for share, name in modes:
        for mapped in (False, True):
            r = one(share, mapped)
            print(f"{name:<12} {str(mapped):<7} {str(r.get('replace', '-')):<9} "
                  f"{str(r.get('posix', '-')):<9} {str(r.get('write_open', '-')):<10} {r['holder']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--hold":
        exec(HOLDER_RUN)
    else:
        main()
