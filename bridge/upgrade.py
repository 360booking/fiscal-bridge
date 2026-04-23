"""Self-upgrade — download the latest release and replace the running
binary. Windows holds the .exe file locked while it's executing, so
the actual swap happens in a small helper batch that waits for us to
exit first.

Flow:
  1. Resolve current .exe path.
  2. Download latest from GitHub releases to %TEMP%.
  3. Write a .bat in %TEMP% that: sleep → taskkill → move → relaunch.
  4. Spawn the batch DETACHED and exit this process so the lock drops.
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

log = logging.getLogger("bridge.upgrade")


LATEST_URL = "https://github.com/360booking/fiscal-bridge/releases/latest/download/360booking-bridge-setup.exe"


def run_upgrade() -> None:
    """Called from the tray / CLI --upgrade. Exits the process at the
    end — if we're the tray host, the host dies; scheduled task /
    service will relaunch us once the new .exe is in place.
    """
    if platform.system() != "Windows":
        raise NotImplementedError("Self-upgrade is Windows-only for now")

    current_exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    current = Path(current_exe).resolve()
    tmp_dir = Path(tempfile.gettempdir())
    new_exe = tmp_dir / "360booking-bridge-new.exe"
    helper_bat = tmp_dir / "360booking-upgrade.bat"

    log.info("Downloading latest from %s", LATEST_URL)
    urllib.request.urlretrieve(LATEST_URL, new_exe)
    log.info("Downloaded %s (%d bytes)", new_exe, new_exe.stat().st_size)

    # Helper batch: wait for our process to exit (file lock drops),
    # replace the .exe, relaunch with --run --background so the user
    # isn't stuck with a foreground CMD.
    content = f"""@echo off
rem 360booking bridge self-upgrade helper
timeout /t 3 /nobreak >nul
taskkill /f /im 360booking-bridge-setup.exe /t >nul 2>&1
taskkill /f /im 360booking-bridge.exe /t >nul 2>&1
timeout /t 2 /nobreak >nul
move /y "{new_exe}" "{current}" >nul
if errorlevel 1 (
    echo Upgrade failed: could not replace {current}
    pause
    exit /b 1
)
start "" "{current}" --run --background
del "%~f0"
"""
    helper_bat.write_text(content, encoding="ascii")
    log.info("Wrote helper batch %s", helper_bat)

    # Spawn the batch in a detached cmd so it survives our exit.
    subprocess.Popen(
        ["cmd", "/c", str(helper_bat)],
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    log.info("Helper spawned. Exiting current process to release exe lock.")
    # Tiny grace period so the helper definitely started
    import time
    time.sleep(0.5)
    os._exit(0)
