"""Cross-session single-instance lock via a PID file.

We can't reliably use a Windows named mutex here: `Global\\...` needs
SeCreateGlobalPrivilege (normal users lack it → silent fallback to
session-local), so a bridge launched by the user in their session and
another launched by LocalSystem from a scheduled task / service would
BOTH acquire a Local\\... mutex and the OS wouldn't stop them fighting.

PID file in C:\\ProgramData\\... is readable+writable from every
session. On startup we read the PID, check if that process is still
alive (and is one of our .exe names). If yes → bail. Otherwise claim
the file for our own PID and register an atexit cleanup.

Race at startup is benign: two processes racing through this function
will read the file at the same instant. The one that writes last wins
the lock; the other gets its own PID in the file and will lose on the
next acquire if it checks again. In practice we only call acquire
once per process, so worst case we get two bridges for a very short
window — the server's own registry will keep just one connected at a
time and the duplicate's WebSocket will die on the next reconnect.
"""
from __future__ import annotations

import atexit
import logging
import os
import platform
from pathlib import Path

log = logging.getLogger("bridge.lock")


class AlreadyRunning(Exception):
    pass


def _pid_file() -> Path:
    if platform.system() == "Windows":
        programdata = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        return Path(programdata) / "360booking-bridge" / "bridge.pid"
    import tempfile
    return Path(tempfile.gettempdir()) / "360booking-bridge.pid"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        # Check if still running (exit code STILL_ACTIVE=259 means running)
        exit_code = wintypes.DWORD()
        got = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        if not got:
            return False
        return exit_code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _is_our_exe(pid: int) -> bool:
    """Best-effort check that the PID belongs to a 360booking-bridge
    process — avoids false positives if a different program happens
    to have reused the PID after our old bridge crashed."""
    if platform.system() != "Windows":
        # Posix fallback: trust PID match alone. /proc check would be
        # nicer but this code path runs on CI test boxes only.
        return True
    try:
        import subprocess
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        out = (r.stdout or "").lower()
        return "360booking-bridge" in out
    except Exception:
        # If tasklist fails, err on the side of "yes it's ours" so we
        # refuse to start a duplicate. Better UX: bridge silently
        # exits rather than joining a scrum.
        return True


def acquire() -> None:
    """Claim the single-instance lock. Raises AlreadyRunning if
    another bridge is alive."""
    pf = _pid_file()
    pf.parent.mkdir(parents=True, exist_ok=True)

    if pf.exists():
        try:
            old_pid_str = pf.read_text(encoding="utf-8").strip()
            old_pid = int(old_pid_str)
        except (OSError, ValueError):
            old_pid = 0
        if old_pid and old_pid != os.getpid() and _pid_alive(old_pid) and _is_our_exe(old_pid):
            raise AlreadyRunning(
                f"Another 360booking bridge is already running (PID {old_pid})"
            )

    # Claim for our PID
    try:
        pf.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as exc:
        # If we can't even write the PID file, skip the lock rather
        # than block legitimate startup.
        log.warning("Could not write PID file %s: %s", pf, exc)
        return

    def _cleanup() -> None:
        try:
            # Only remove if the PID in the file is still ours (in
            # case another bridge started after us and took over).
            if pf.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pf.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_cleanup)
