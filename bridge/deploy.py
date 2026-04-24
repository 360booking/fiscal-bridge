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
from typing import Dict, Optional

log = logging.getLogger("bridge.deploy")

INSTALL_DIR_NAME = "360booking-bridge"
INSTALLED_EXE_NAME = "360booking-bridge.exe"
SHORTCUT_NAME = "360booking Fiscal Bridge"


def _program_files_dir() -> Path:
    base = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
    return Path(base) / INSTALL_DIR_NAME


def installed_exe_path() -> Path:
    return _program_files_dir() / INSTALLED_EXE_NAME


def _start_menu_dir_allusers() -> Path:
    """All-users Start Menu (requires admin) — visible to every account."""
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _start_menu_dir_user() -> Path:
    """Per-user Start Menu — works without admin, only visible to the
    current Windows user. Good fallback when UAC was declined."""
    base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _public_desktop_dir() -> Path:
    base = os.environ.get("PUBLIC") or r"C:\Users\Public"
    return Path(base) / "Desktop"


def _user_desktop_dir() -> Path:
    """Per-user Desktop — always writable by the current user, and on
    most Windows setups it's what the user actually sees. Public
    Desktop is shown too but sometimes intermediate shells hide it."""
    base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
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
    try:
        shortcut.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.warning("deploy: cannot create parent dir %s: %s", shortcut.parent, exc)
        return False
    # Use double quotes inside PS so paths with apostrophes don't break.
    # Escape backticks / double quotes in paths (rare) by doubling them.
    def _ps_q(p: str) -> str:
        return p.replace('"', '`"')
    ps = (
        '$ws = New-Object -ComObject WScript.Shell;'
        f'$s = $ws.CreateShortcut("{_ps_q(str(shortcut))}");'
        f'$s.TargetPath = "{_ps_q(str(target))}";'
        f'$s.WorkingDirectory = "{_ps_q(str(target.parent))}";'
        f'$s.IconLocation = "{_ps_q(str(target))},0";'
        f'$s.Description = "{_ps_q(description)}";'
        '$s.Save()'
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and shortcut.exists():
            log.info("deploy: created shortcut %s → %s", shortcut, target)
            return True
        log.warning(
            "deploy: shortcut %s failed (rc=%s): stderr=%s stdout=%s",
            shortcut, r.returncode,
            (r.stderr or "").strip(), (r.stdout or "").strip(),
        )
        return False
    except FileNotFoundError:
        log.warning("deploy: powershell.exe not on PATH — cannot create shortcut %s", shortcut)
        return False
    except Exception as exc:
        log.warning("deploy: shortcut %s exception: %s", shortcut, exc)
        return False


def create_shortcuts(exe: Path) -> Dict[str, bool]:
    """Create Start Menu + Desktop shortcuts pointing at `exe`. Returns
    a dict with status of each location so callers can report what
    landed and what didn't. Writes to BOTH all-users and per-user
    locations so the icon shows up even when admin is partial.
    """
    from typing import Dict as _D  # keep local import so top-level module is lean
    desc = "360booking Fiscal Bridge — casa de marcat"
    targets = {
        "start_menu_allusers": _start_menu_dir_allusers() / f"{SHORTCUT_NAME}.lnk",
        "start_menu_user": _start_menu_dir_user() / f"{SHORTCUT_NAME}.lnk",
        "desktop_public": _public_desktop_dir() / f"{SHORTCUT_NAME}.lnk",
        "desktop_user": _user_desktop_dir() / f"{SHORTCUT_NAME}.lnk",
    }
    results: Dict[str, bool] = {}
    for key, path in targets.items():
        results[key] = _write_lnk_via_powershell(exe, path, desc)
    return results


def deploy(src_exe: str) -> tuple[Optional[Path], Dict[str, bool]]:
    """Full install step: copy exe to Program Files + create shortcuts.
    Needs admin for the copy. Returns (installed_path_or_None, results)."""
    dst = copy_exe_to_program_files(src_exe)
    if not dst:
        return None, {}
    results = create_shortcuts(dst)
    return dst, results


def create_shortcuts_pointing_to(exe_path: str) -> Dict[str, bool]:
    """Shortcut-only path — no Program Files copy. Used as a fallback
    when admin isn't available (so we at least put an icon on the
    user's desktop/Start Menu pointing at whatever exe they ran). Also
    used by the GUI's "Create shortcuts" button to let the user fix a
    missing shortcut without reinstalling."""
    src = Path(exe_path)
    if not src.exists():
        log.warning("deploy: exe missing at %s — cannot create shortcuts", src)
        return {}
    return create_shortcuts(src)


def uninstall_shortcuts() -> None:
    for path in (
        _start_menu_dir_allusers() / f"{SHORTCUT_NAME}.lnk",
        _start_menu_dir_user() / f"{SHORTCUT_NAME}.lnk",
        _public_desktop_dir() / f"{SHORTCUT_NAME}.lnk",
        _user_desktop_dir() / f"{SHORTCUT_NAME}.lnk",
    ):
        try:
            if path.exists():
                path.unlink()
                log.info("deploy: removed shortcut %s", path)
        except Exception as exc:
            log.warning("deploy: could not remove %s: %s", path, exc)
