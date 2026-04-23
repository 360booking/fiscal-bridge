"""Live status file written by the WebSocket loop, read by the GUI.

Path: %LOCALAPPDATA%\\360booking-bridge\\status.json

The bridge updates this file on every significant state change (connect,
disconnect, heartbeat, job completed). The GUI reads it on a timer and
translates into the three colored indicators — no IPC, no listeners,
no extra process. Stale files (mtime > 60s ago) are treated as "not
running".
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import config_dir


def _status_path() -> Path:
    return config_dir() / "status.json"


def write(patch: Dict[str, Any]) -> None:
    """Merge `patch` into the current status file. Creates it if
    missing. Never raises — status reporting must never break the
    main loop."""
    try:
        p = _status_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        current: Dict[str, Any] = {}
        if p.exists():
            try:
                current = json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                current = {}
        current.update(patch)
        current["updated_at"] = time.time()
        p.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception:
        pass


def read() -> Optional[Dict[str, Any]]:
    """Return the current status dict, or None if missing / stale."""
    p = _status_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # "Stale" = last update more than 60s ago. The WS loop writes
        # on every heartbeat (30s) so fresher than that means we're
        # running. Older means the bridge crashed or is unreachable.
        age = time.time() - float(data.get("updated_at") or 0)
        data["stale"] = age > 60
        data["age_s"] = int(age)
        return data
    except Exception:
        return None


def clear() -> None:
    """Remove the status file — called at clean shutdown / disconnect."""
    try:
        _status_path().unlink(missing_ok=True)
    except Exception:
        pass
