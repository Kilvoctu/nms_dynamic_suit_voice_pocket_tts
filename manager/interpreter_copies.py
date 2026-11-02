# manager/interpreter_copies.py
"""Named interpreter copies so Task Manager shows NMS_DSV_Manager/NMS_DSV_Engine
instead of generic Python entries (and drops the venv launcher stub processes)."""
import ctypes
import os
import shutil
import struct
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

COPY_NAMES = ("NMS_DSV_Manager.exe", "NMS_DSV_Engine.exe")
DESCRIPTIONS = {"NMS_DSV_Manager.exe": "NMS DSV Manager", "NMS_DSV_Engine.exe": "NMS DSV Engine"}
_PRODUCT = "NMS Dynamic Suit Voice"
_PROBE_TIMEOUT_S = 30
_RT_ICON, _RT_VERSION = 3, 16
_RES_LANG = 0x0409

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
_k32.BeginUpdateResourceW.restype = wintypes.HANDLE
_k32.UpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID,
                                 wintypes.WORD, ctypes.c_char_p, wintypes.DWORD]
_k32.UpdateResourceW.restype = wintypes.BOOL
_k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
_k32.EndUpdateResourceW.restype = wintypes.BOOL
_ver_dll = None  # lazy: version.dll is only needed for branding verification


def _runtime_dll_names() -> list:
    """DLLs the base interpreter ships beside its exe; extensions resolve these
    from the application dir, so the copies need them in venv/Scripts too."""
    names = ["python3.dll", f"python{sys.version_info[0]}{sys.version_info[1]}.dll",
             "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]
    base = Path(sys.base_prefix)
    return [n for n in names if (base / n).is_file()]


# ------------------------------------------------------------- PE .rsrc walk
def _walk_resources(data: bytes) -> dict:
    """Parse a PE's resource directory: {type_id: [(name_id, lang, data_off, size)]}."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\x00\x00":
        return {}
    coff = pe + 4
    num_sec = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    dd_off = opt + (112 if struct.unpack_from("<H", data, opt)[0] == 0x20B else 96)
    res_rva = struct.unpack_from("<I", data, dd_off + 16)[0]
    sections = []
    for i in range(num_sec):
        e = coff + 20 + opt_size + i * 40
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, e + 8)
        sections.append((va, vsize, raw, rsize))
    base = None
    for va, vsize, raw, rsize in sections:
        if va <= res_rva < va + max(vsize, rsize):
            base = res_rva - va + raw
            break
    if not base:
        return {}

    def entries(off):
        n_named, n_id = struct.unpack_from("<HH", data, base + off + 12)
        out = []
        for i in range(n_named + n_id):
            name_id, sub = struct.unpack_from("<II", data, base + off + 16 + i * 8)
            out.append((name_id, sub & 0x7FFFFFFF, bool(name_id & 0x80000000), bool(sub & 0x80000000)))
        return out

    result = {}
    for rtype, t_off, _n, sub in entries(0):
        if not sub or rtype & 0x80000000:
            continue
        for name_id, n_off, _nn, n_sub in entries(t_off):
            for lang, d_off, _nl, _ns in entries(n_off):
                d_rva, size, _cp = struct.unpack_from("<III", data, base + d_off)
                d_off_f = None
                for va, vsize, raw, rsize in sections:
                    if va <= d_rva < va + max(vsize, rsize):
                        d_off_f = d_rva - va + raw
                        break
                if d_off_f is not None:
                    result.setdefault(rtype, []).append((name_id, lang, d_off_f, size))
    return result


# ------------------------------------------------------- version resource blob
def _aligned(n: int) -> int:
    return (n + 3) & ~3


def _wstring(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


def _pad_key(key: str) -> bytes:
    """UTF-16 key + null, padded so the value that follows is 4-aligned
    (the 6-byte block header counts toward the offset, per the PE layout)."""
    raw = _wstring(key)
    return raw + b"\x00" * (_aligned(6 + len(raw)) - 6 - len(raw))


def _pack_string(key: str, value: str) -> bytes:
    """One String block; wLength excludes the final pad (Windows' convention)."""
    key_p = _pad_key(key)
    val = _wstring(value)
    block = struct.pack("<HHH", 6 + len(key_p) + len(val), len(val) // 2, 1) + key_p + val
    return block + b"\x00" * (_aligned(len(block)) - len(block))


def _pack_block(key: str, children: bytes, wtype: int = 1, value: bytes = b"",
                wvalue_bytes: int = 0) -> bytes:
    """A container block (StringFileInfo / StringTable / VarFileInfo / Var)."""
    key_p = _pad_key(key)
    block = struct.pack("<HHH", 6 + len(key_p) + len(value) + len(children),
                        wvalue_bytes, wtype) + key_p + value + children
    return block + b"\x00" * (_aligned(len(block)) - len(block))


def _build_version_blob(description: str, original_filename: str) -> bytes:
    """Minimal well-formed VS_VERSIONINFO mimicking pythonw.exe's exact layout."""
    vi = sys.version_info
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x10000,
                        (vi.major << 16) | vi.minor, vi.micro << 16,
                        (vi.major << 16) | vi.minor, vi.micro << 16,
                        0x3F, 0, 0x40004, 1, 0, 0, 0)
    strings = [
        ("CompanyName", _PRODUCT), ("FileDescription", description),
        ("FileVersion", f"{vi.major}.{vi.minor}.{vi.micro}"),
        ("InternalName", Path(original_filename).stem),
        ("LegalCopyright", "MIT License"),
        ("OriginalFilename", original_filename),
        ("ProductName", _PRODUCT),
        ("ProductVersion", f"{vi.major}.{vi.minor}.{vi.micro}"),
    ]
    table = b"".join(_pack_string(k, v) for k, v in strings)
    sfi = _pack_block("StringFileInfo", _pack_block("000004b0", table))
    var = _pack_block("Translation", b"", wtype=0, value=struct.pack("<HH", 0, 0x04B0), wvalue_bytes=4)
    vfi = _pack_block("VarFileInfo", var)
    children = sfi + vfi
    head_key = _pad_key("VS_VERSION_INFO")
    total = 6 + len(head_key) + len(fixed) + len(children)
    return struct.pack("<HHH", total, len(fixed), 0) + head_key + fixed + children


# ------------------------------------------------------------- icon rebranding
def _icon_size(data: bytes):
    """(width, height) of one RT_ICON payload (PNG or DIB form)."""
    if data[:4] == b"\x89PNG":
        return struct.unpack_from(">II", data, 16)
    w, h = struct.unpack_from("<ii", data, 4)
    return w, abs(h) // 2


def _brand_icons_payloads(icon_ids, exe_data, res, icon_png: Path):
    """Build (name_id, png_bytes) replacements at each original icon's size."""
    from io import BytesIO
    from PIL import Image
    img = Image.open(icon_png).convert("RGBA")
    payloads = {}
    for name_id, _lang, off, size in icon_ids:
        w, h = _icon_size(exe_data[off:off + size])
        if (w, h) not in payloads:
            buf = BytesIO()
            img.resize((w, h), Image.LANCZOS).save(buf, "PNG")
            payloads[(w, h)] = buf.getvalue()
    return [(nid, payloads[_icon_size(exe_data[off:off + size])]) for nid, _l, off, size in icon_ids]


# -------------------------------------------------------------------- branding
def _file_description(exe: Path):
    """Read FileDescription back with Windows' own parser (None on any failure)."""
    global _ver_dll
    try:
        if _ver_dll is None:
            _ver_dll = ctypes.WinDLL("version")
        size = _ver_dll.GetFileVersionInfoSizeW(str(exe), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not _ver_dll.GetFileVersionInfoW(str(exe), 0, size, buf):
            return None
        ptr, blen = ctypes.c_void_p(), wintypes.UINT()
        if not _ver_dll.VerQueryValueW(buf, "\\VarFileInfo\\Translation",
                                       ctypes.byref(ptr), ctypes.byref(blen)) or blen.value < 4:
            return None
        lang, cp = struct.unpack_from("<HH", ctypes.string_at(ptr.value, 4))
        if not _ver_dll.VerQueryValueW(
                buf, f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription",
                ctypes.byref(ptr), ctypes.byref(blen)):
            return None
        return ctypes.wstring_at(ptr.value)
    except Exception:
        return None


def _brand_file(exe: Path, description: str, icon_png: Path) -> bool:
    """Replace the copy's version resource (and icons when PIL is present).
    Returns True when the FileDescription reads back as intended."""
    exe_data = exe.read_bytes()
    res = _walk_resources(exe_data)
    updates = []
    for name_id, lang, _off, _size in res.get(_RT_VERSION, []):
        updates.append((_RT_VERSION, name_id, lang,
                        _build_version_blob(description, exe.name)))
    if icon_png.is_file():
        try:
            for name_id, payload in _brand_icons_payloads(
                    res.get(_RT_ICON, []), exe_data, res, icon_png):
                updates.append((_RT_ICON, name_id, _RES_LANG, payload))
        except Exception:
            pass  # icons are cosmetic; the description alone already rebrands
    if not updates:
        return False
    handle = _k32.BeginUpdateResourceW(str(exe), False)
    if not handle:
        return False
    try:
        for rtype, name_id, lang, payload in updates:
            if not _k32.UpdateResourceW(handle, ctypes.c_void_p(rtype),
                                        ctypes.c_void_p(name_id), lang, payload, len(payload)):
                raise OSError(ctypes.get_last_error())
    except OSError:
        _k32.EndUpdateResourceW(handle, True)
        return False
    if not _k32.EndUpdateResourceW(handle, False):
        return False
    return _file_description(exe) == description


def _verify(exe: Path, project_root: Path) -> bool:
    """True when the copy self-identifies as this venv's interpreter."""
    try:
        out = subprocess.run(
            [str(exe), "-c", "import sys; print(sys.prefix)"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if out.returncode != 0:
        return False
    got = os.path.normcase(os.path.realpath(out.stdout.strip()))
    want = os.path.normcase(str((Path(project_root) / "venv").resolve()))
    return got == want


def ensure_copies(project_root: Path) -> dict:
    """Create/refresh/brand the named interpreter copies in venv/Scripts. Returns
    the subset that exists and verified (possibly empty - callers must fall back)."""
    project_root = Path(project_root)
    scripts = project_root / "venv" / "Scripts"
    source = Path(sys.base_prefix) / "pythonw.exe"
    if not source.is_file() or not scripts.is_dir():
        return {}
    # The copies need the base interpreter's runtime DLLs findable next to them
    # (the loader's application-dir search does not look into the base prefix).
    try:
        for dll_name in _runtime_dll_names():
            base_dll = Path(sys.base_prefix) / dll_name
            scripts_dll = scripts / dll_name
            if base_dll.is_file() and not scripts_dll.is_file():
                shutil.copy2(base_dll, scripts_dll)
    except OSError:
        return {}
    icon_png = project_root / "assets" / "nms_dsv.png"
    ready = {}
    for name in COPY_NAMES:
        dest = scripts / name
        try:
            stale = not dest.is_file() or dest.stat().st_mtime < source.stat().st_mtime
            if stale:
                shutil.copy2(source, dest)
                if not _verify(dest, project_root):
                    dest.unlink(missing_ok=True)
                    continue
        except OSError:
            continue
        if not dest.is_file():
            continue
        # Rebrand when needed (fresh copy, stale refresh, or a previous skip).
        description = DESCRIPTIONS.get(name, name)
        try:
            if _file_description(dest) != description:
                if _brand_file(dest, description, icon_png) and not _verify(dest, project_root):
                    shutil.copy2(source, dest)  # branding broke it: back to plain
        except Exception:
            pass  # branding is cosmetic; never let it break copy creation
        ready[name] = dest
    return ready


def copy_path(project_root: Path, name: str) -> Path:
    """Where a named copy lives (callers check is_file() as the fallback gate)."""
    return Path(project_root) / "venv" / "Scripts" / name
