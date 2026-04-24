"""Deploy the bridge binary to a stable Program Files location and
create Start Menu + Desktop shortcuts. Called from --install while
elevated so the user has a visible "app" to launch/restart.

Without this step, the downloaded setup.exe stays wherever Explorer
dropped it (usually ~/Downloads) and the Windows Service we register
points at that throwaway path — if the user moves or deletes the
download the service breaks. A user also has no obvious way to open
the GUI because nothing shows up in Start Menu.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger("bridge.deploy")

INSTALL_DIR_NAME = "360booking-bridge"
INSTALLED_EXE_NAME = "360booking-bridge.exe"
SHORTCUT_NAME = "360booking Fiscal Bridge"


def _program_files_dir() -> Path:
    base = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
    return Path(base) / INSTALL_DIR_NAME


def installed_exe_path() -> Path:
    return _program_files_dir() / INSTALLED_EXE_NAME


def _start_menu_dir() -> Path:
    """All-users Start Menu (requires admin) so every Windows account on
    the PC sees the shortcut. ProgramData is the canonical path."""
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _public_desktop_dir() -> Path:
    base = os.environ.get("PUBLIC") or r"C:\Users\Public"
    return Path(base) / "Desktop"


def copy_exe_to_program_files(src_exe: str) -> Optional[Path]:
    """Copy the running .exe to C:\\Program Files\\360booking-bridge\\.
    Returns the destination path on success, None if the copy was
    skipped (non-frozen dev mode) or failed.
    """
    src = Path(src_exe).resolve()
    if not src.exists() or src.suffix.lower() != ".exe":
        log.info("deploy: source %s not a .exe, skipping copy", src)
        return None
    dst_dir = _program_files_dir()
    dst = dst_dir / INSTALLED_EXE_NAME
    # If the service is already running from `dst`, we're running the
    # same file — nothing to copy.
    try:
        if dst.exists() and src.samefile(dst):
            log.info("deploy: already running from installed location %s", dst)
            return dst
    except OSError:
        pass
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        # shutil.copy2 replaces the destination atomically enough for
        # our purposes. If the current service is holding the old .exe
        # open (NSSM will for a running service), the caller is
        # expected to stop the service first — install_service() does
        # that already via `nssm stop` before reaching here.
        shutil.copy2(src, dst)
        log.info("deploy: copied %s → %s", src, dst)
        return dst
    except Exception as exc:
        log.warning("deploy: copy failed: %s", exc)
        return None


def _write_lnk_via_powershell(target: Path, shortcut: Path, description: str = "") -> bool:
    """Create a Windows .lnk via PowerShell's WScript.Shell COM. Avoids
    adding pywin32 as a build dependency."""
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$s = $ws.CreateShortcut('{shortcut}');"
        f"$s.TargetPath = '{target}';"
        f"$s.WorkingDirectory = '{target.parent}';"
        f"$s.IconLocation = '{target},0';"
        f"$s.Description = '{description}';"
        "$s.Save()"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and shortcut.exists():
            log.info("deploy: created shortcut %s", shortcut)
            return True
        log.warning("deploy: shortcut %s failed: %s", shortcut, (r.stderr or r.stdout or "").strip())
        return False
    except Exception as exc:
        log.warning("deploy: shortcut %s exception: %s", shortcut, exc)
        return False


def create_shortcuts(exe: Path) -> tuple[bool, bool]:
    """Create Start Menu + Desktop shortcuts pointing at `exe`.
    Returns (start_menu_ok, desktop_ok)."""
    desc = "360booking Fiscal Bridge — casa de marcat"
    start = _start_menu_dir() / f"{SHORTCUT_NAME}.lnk"
    desk = _public_desktop_dir() / f"{SHORTCUT_NAME}.lnk"
    start_ok = _write_lnk_via_powershell(exe, start, desc)
    desk_ok = _write_lnk_via_powershell(exe, desk, desc)
    return start_ok, desk_ok


def deploy(src_exe: str) -> tuple[Optional[Path], bool, bool]:
    """Run the whole deploy step: copy exe + create shortcuts.
    Returns (installed_exe_path_or_None, start_menu_ok, desktop_ok).
    """
    dst = copy_exe_to_program_files(src_exe)
    if not dst:
        return None, False, False
    start_ok, desk_ok = create_shortcuts(dst)
    return dst, start_ok, desk_ok


def uninstall_shortcuts() -> None:
    for path in (
        _start_menu_dir() / f"{SHORTCUT_NAME}.lnk",
        _public_desktop_dir() / f"{SHORTCUT_NAME}.lnk",
    ):
        try:
            if path.exists():
                path.unlink()
                log.info("deploy: removed shortcut %s", path)
        except Exception as exc:
            log.warning("deploy: could not remove %s: %s", path, exc)
