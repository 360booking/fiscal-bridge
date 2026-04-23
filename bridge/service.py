"""Windows Service installer via NSSM.

NSSM is a tiny (~330KB) service shim that wraps any .exe as a proper
Windows service. It's bundled alongside the bridge binary by the
GitHub Actions build pipeline — at runtime we extract it from the
PyInstaller _MEIPASS bundle to %LocalAppData% so repeated calls can
find it after the current process exits.

Service-based install is preferred over scheduled task because:
  - Starts at BOOT (before any user logs in)
  - Survives user logout / log-off
  - Windows restarts it automatically on crash (SERVICE_RESTART)
  - Runs as LocalSystem → unrestricted access to serial ports,
    no dependency on a specific user profile
  - No console window ever, no "runtimebroker" flashes at login

The tradeoff: installing/removing a service requires admin rights.
`install_service()` auto-elevates via ShellExecute "runas"; if the
user declines the UAC prompt we fall back to scheduled-task install.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import config_dir


SERVICE_NAME = "360bookingFiscalBridge"
SERVICE_DISPLAY = "360booking Fiscal Bridge"
SERVICE_DESCRIPTION = (
    "Relays print jobs from 360booking cloud to a locally-attached "
    "fiscal printer (casa de marcat)."
)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(extra_args: Optional[list] = None) -> bool:
    """Re-execute the current .exe with "runas" verb — triggers a UAC
    prompt. Returns True if the user accepted (we exit right after,
    the elevated copy continues), False if they declined or we're not
    on Windows."""
    if os.name != "nt":
        return False
    exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    args = (extra_args if extra_args is not None else sys.argv[1:])
    params = " ".join(f'"{a}"' for a in args) if args else ""
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    # SE_ERR_ACCESSDENIED == 5 (user declined). >32 = success.
    return ret > 32


def _nssm_source() -> Optional[Path]:
    """Return the path to nssm.exe bundled by PyInstaller, or None if
    this build doesn't ship it (development from source, say)."""
    # PyInstaller extracts data files to sys._MEIPASS at runtime.
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "nssm.exe"
        if p.exists():
            return p
    # Dev fallback: look next to the script
    script = Path(sys.argv[0]).resolve().parent
    for candidate in (script / "nssm.exe", script.parent / "nssm.exe"):
        if candidate.exists():
            return candidate
    return None


def _nssm_install_path() -> Path:
    """Permanent NSSM location. Lives next to the config so service
    uninstall can find it even after the original .exe was deleted."""
    return config_dir() / "nssm.exe"


def ensure_nssm() -> Optional[Path]:
    """Extract the bundled nssm.exe to %LocalAppData%. Returns its
    path or None if it couldn't be sourced."""
    dst = _nssm_install_path()
    if dst.exists():
        return dst
    src = _nssm_source()
    if not src:
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
    except Exception:
        return None
    return dst


def _wait_for_service_deleted(nssm: Path, timeout_s: float = 30.0) -> bool:
    """Poll until the service name is fully removed from the SCM.

    When a stale handle keeps the old service around (Services.msc
    open, Task Manager's Services tab, another admin console),
    Windows puts the service in "MARKED FOR DELETION" state and the
    next CreateService call fails. Wait it out before trying to
    install a fresh one.
    """
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = subprocess.run(
            ["sc", "query", SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if "does not exist" in (r.stderr or r.stdout or "").lower():
            return True
        time.sleep(1.0)
    return False


def install_service(exe_path: str) -> tuple[bool, str]:
    """Install the bridge as a Windows service via NSSM. Assumes the
    caller already verified admin. Returns (ok, message).
    """
    import time

    nssm = ensure_nssm()
    if not nssm:
        return False, "NSSM binary not found in the bundle."

    log = config_dir() / "service.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    # Clean up any previous install. NSSM is a no-op when the service
    # doesn't exist, so unconditional stop+remove is safe.
    subprocess.run([str(nssm), "stop", SERVICE_NAME], capture_output=True)
    subprocess.run([str(nssm), "remove", SERVICE_NAME, "confirm"], capture_output=True)

    # If the previous service left a "MARKED FOR DELETION" zombie,
    # wait it out (up to 30s) before CreateService would collide.
    if not _wait_for_service_deleted(nssm, timeout_s=30.0):
        return False, (
            "Previous service is still being removed by Windows "
            "(MARKED FOR DELETION). Close Services.msc / Task Manager "
            "(the Services tab specifically), or reboot, and retry."
        )

    steps = [
        ["install", SERVICE_NAME, exe_path, "--run"],
        ["set", SERVICE_NAME, "DisplayName", SERVICE_DISPLAY],
        ["set", SERVICE_NAME, "Description", SERVICE_DESCRIPTION],
        ["set", SERVICE_NAME, "Start", "SERVICE_AUTO_START"],
        ["set", SERVICE_NAME, "AppStdout", str(log)],
        ["set", SERVICE_NAME, "AppStderr", str(log)],
        ["set", SERVICE_NAME, "AppStopMethodSkip", "0"],
        ["set", SERVICE_NAME, "AppStopMethodConsole", "5000"],
        # Auto-restart on crash: 5s delay, reset counter after 1h
        ["set", SERVICE_NAME, "AppExit", "Default", "Restart"],
        ["set", SERVICE_NAME, "AppRestartDelay", "5000"],
        ["set", SERVICE_NAME, "AppThrottle", "5000"],
        # Rotate logs at 10MB
        ["set", SERVICE_NAME, "AppRotateFiles", "1"],
        ["set", SERVICE_NAME, "AppRotateBytes", "10485760"],
    ]
    for step in steps:
        r = subprocess.run([str(nssm), *step], capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"nssm {' '.join(step)} failed: {(r.stderr or r.stdout).strip()}"

    # Start it
    r = subprocess.run([str(nssm), "start", SERVICE_NAME], capture_output=True, text=True)
    if r.returncode != 0 and "ALREADY_RUNNING" not in (r.stderr or r.stdout or ""):
        return False, f"Service installed but failed to start: {(r.stderr or r.stdout).strip()}"

    return True, "Service installed and started"


def uninstall_service() -> tuple[bool, str]:
    """Stop and remove the Windows service. Requires admin."""
    nssm_path = ensure_nssm() or _nssm_install_path()
    if not nssm_path.exists():
        return False, "NSSM binary not available — nothing to uninstall via service path."
    subprocess.run([str(nssm_path), "stop", SERVICE_NAME], capture_output=True)
    r = subprocess.run(
        [str(nssm_path), "remove", SERVICE_NAME, "confirm"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 or "does not exist" in (r.stderr or r.stdout or ""):
        return True, "Service removed"
    return False, (r.stderr or r.stdout or "unknown").strip()


def service_state() -> str:
    """Return 'running' / 'stopped' / 'missing' / 'unknown'."""
    if os.name != "nt":
        return "unknown"
    try:
        r = subprocess.run(
            ["sc", "query", SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return "unknown"
    if "does not exist" in (r.stderr or r.stdout or "").lower():
        return "missing"
    out = r.stdout or ""
    if "RUNNING" in out:
        return "running"
    if "STOPPED" in out:
        return "stopped"
    return "unknown"
