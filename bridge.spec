# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the 360booking fiscal bridge.

Produces a single-file Windows .exe called `360booking-bridge.exe`.
"""
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# The printer registry loads modules by string path via importlib, so
# PyInstaller can't trace them statically — we have to list the whole
# bridge package (and any other dynamic deps) as hidden imports.
hidden = []
hidden += collect_submodules("bridge")
hidden += collect_submodules("websockets")
hidden += ["serial", "serial.tools", "serial.tools.list_ports"]

a = Analysis(
    ["bridge/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="360booking-bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX compression off — Windows Defender treats UPX-packed
    # binaries as "possible malware" and the PE loader sometimes
    # rejects them with "Unsupported 16-bit application".
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
