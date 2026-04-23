# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the 360booking fiscal bridge.

Produces a single-file Windows .exe called `360booking-bridge.exe`.
"""
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# The printer registry loads modules by string path via importlib,
# which PyInstaller can't trace. Belt + suspenders: we call
# collect_submodules for coverage AND list every module explicitly
# so a discovery failure in one doesn't silently drop a printer.
hidden = []
hidden += collect_submodules("bridge")
hidden += collect_submodules("websockets")
hidden += [
    # explicit list matches printers/registry.py REGISTRY keys
    "bridge",
    "bridge.config",
    "bridge.main",
    "bridge.gui",
    "bridge.ws_client",
    "bridge.printers",
    "bridge.printers.base",
    "bridge.printers.registry",
    "bridge.printers.simulator",
    "bridge.printers.datecs_dp25",
    "bridge.printers.datecs_fp",
    # pyserial pieces
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "serial.serialwin32",
    "serial.serialposix",
    # tkinter GUI
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.font",
]

a = Analysis(
    ["run_bridge.py"],
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
