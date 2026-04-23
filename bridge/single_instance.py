"""Single-instance lock so we never run two WebSocket loops in
parallel. Two copies would keep booting each other off the server
and the admin panel would flap between connected/offline forever.

On Windows we use a global named mutex (CreateMutex) — fast, safe
across sessions, auto-released when the holder process exits.
On other platforms we fall back to an advisory lock on a tmp file.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path

log = logging.getLogger("bridge.lock")

_MUTEX_NAME = r"Global\360bookingFiscalBridge"


class AlreadyRunning(Exception):
    """Raised from acquire() if another bridge is already running."""


_kept_handle = None  # keep a reference so the mutex stays alive


def acquire() -> None:
    """Raise AlreadyRunning if another instance holds the lock.
    Otherwise claim it for the lifetime of this process."""
    global _kept_handle
    if platform.system() == "Windows":
        _acquire_windows()
    else:
        _acquire_posix()


def _acquire_windows() -> None:
    global _kept_handle
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    # CreateMutexW returns a handle and sets last error = 183
    # (ERROR_ALREADY_EXISTS) if the mutex already exists.
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    last_err = ctypes.GetLastError()
    if not handle:
        raise RuntimeError(f"CreateMutex failed: {last_err}")
    if last_err == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise AlreadyRunning("Another 360booking bridge is already running")
    _kept_handle = handle


def _acquire_posix() -> None:
    global _kept_handle
    import fcntl  # type: ignore
    import tempfile
    p = Path(tempfile.gettempdir()) / "360booking-bridge.lock"
    f = open(p, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise AlreadyRunning("Another 360booking bridge is already running")
    _kept_handle = f
