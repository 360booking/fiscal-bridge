"""Cross-session single-instance lock via an OS-level file lock.

Earlier versions of this module used a PID file: write our PID to
`bridge.pid`, on startup read it and call OpenProcess to see if the
old PID was still alive. That works most of the time but fails the
exact case we care about — a power cut. When the PC loses power
mid-print, the bridge process dies without running atexit, so the
stale PID stays in the file. Worse, if a *different* program later
reuses that PID, the "is it alive" check passes and the real bridge
refuses to start. After reboot we'd also see two copies race past
the check (read happens before either write) and both write their
own PIDs in turn — both end up running, flapping on the server's
one-slot-per-tenant registry.

This version uses an OS-level byte-range lock held on an open file
descriptor. The lock is released automatically by the kernel when
the process exits for any reason — clean shutdown, crash, kill, or
power loss. No stale state to clean up, no PID reuse ambiguity, no
read-then-write race.

  Windows: msvcrt.locking() with LK_NBLCK (non-blocking exclusive)
  POSIX:   fcntl.flock() with LOCK_EX | LOCK_NB

We stash the open file handle in a module-level global so the
kernel keeps the lock for the life of the process. Do not close it.
"""
from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Optional

log = logging.getLogger("bridge.lock")


class AlreadyRunning(Exception):
    pass


# Kept alive for the life of the process — the lock is tied to this
# file handle. Closing it releases the lock.
_lock_fh = None  # type: Optional["object"]


def _lock_path() -> Path:
    if platform.system() == "Windows":
        programdata = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        d = Path(programdata) / "360booking-bridge"
    else:
        import tempfile
        d = Path(tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d / "bridge.lock"


def acquire() -> None:
    """Claim the single-instance lock. Raises AlreadyRunning if
    another bridge is alive."""
    global _lock_fh
    p = _lock_path()

    try:
        fh = open(p, "a+b")
    except OSError as exc:
        # If the lock file itself can't be opened, don't block legit
        # startup — log and move on. Worst case the old flapping
        # symptom comes back, but we'd rather have a bridge than not.
        log.warning("Could not open lock file %s: %s — skipping lock", p, exc)
        return

    try:
        if platform.system() == "Windows":
            import msvcrt
            fh.seek(0)
            # Lock 1 byte at offset 0. msvcrt.locking ranges can extend
            # past EOF, so the file may be empty — no need to pre-fill.
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                fh.close()
                raise AlreadyRunning(
                    f"Another 360booking bridge is already running "
                    f"(lock held on {p})"
                ) from exc
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                fh.close()
                raise AlreadyRunning(
                    f"Another 360booking bridge is already running "
                    f"(lock held on {p})"
                ) from exc
    except AlreadyRunning:
        raise
    except Exception as exc:
        fh.close()
        log.warning("Lock acquisition failed (%s) — continuing without lock", exc)
        return

    # Write our PID inside the locked file for debugging. The lock,
    # not the PID, is the source of truth for "is a bridge running".
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
    except Exception:
        pass

    _lock_fh = fh
