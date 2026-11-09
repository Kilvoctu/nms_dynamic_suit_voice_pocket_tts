# -*- mode: python ; coding: utf-8 -*-
# NMS_DSV.spec - PyInstaller onedir build: dist\NMS_DSV\DSV.exe + DSV_runtime\
# Shipped as one zip; the exe creates state\ next to itself on first run.
# Build tools live in state/bin on the build machine (see build_exe.bat).
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

cat = 'assets/data'
datas = [
    (cat, cat),
    ('assets/starter_wems', 'assets/starter_wems'),
    ('assets/nms_dsv.png', 'assets'),
] + collect_data_files('pocket_tts')  # config/*.yaml model definitions must ship
binaries = [
    ('state/bin/ffplay.exe', 'bin'),
    ('state/bin/wav2wem.exe', 'bin'),
] + collect_dynamic_libs('llama_cpp')  # ggml/llama DLLs ship as package data, not extensions
hiddenimports = ['engine.warmup_tts']

a = Analysis(
    ['main.py'],
    pathex=['.', 'manager'],  # manager root lets ui.* resolve statically (frozen runtime finds it in the PYZ)
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DSV',
    contents_directory='DSV_runtime',  # support folder beside the exe (PyInstaller default is _internal)
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/nms_dsv.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='NMS_DSV',
)
