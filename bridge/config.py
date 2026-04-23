"""Config persistence — ~%LOCALAPPDATA%\\360booking-bridge\\config.json on
Windows; ~/.config/360booking-bridge/config.json on Linux/macOS. The
enrollment step writes the device_token here; the service loop reads
it on start.
"""
from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


def config_dir() -> Path:
    """Return the config directory. On Windows we prefer %PROGRAMDATA%
    (machine-wide, readable by both the current user and LocalSystem —
    the account the Windows service runs under) and fall back to
    %LOCALAPPDATA% only when ProgramData isn't writable (rare)."""
    if platform.system() == "Windows":
        # ProgramData is the right place for service-accessible config.
        # LocalSystem has full access; normal users have read + write
        # via the default ACL on ProgramData itself.
        programdata = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        candidate = Path(programdata) / "360booking-bridge"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            # Quick write probe
            probe = candidate / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate
        except Exception:
            pass
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / "360booking-bridge"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "360booking-bridge"


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class BridgeConfig:
    device_token: Optional[str] = None
    tenant_id: Optional[str] = None
    bridge_id: Optional[str] = None
    websocket_url: Optional[str] = None
    # "simulator" | "datecs_dp25" | ...
    printer_model: str = "simulator"
    # Local port for health/diagnostics (not required for operation).
    health_port: int = 17890
    # Server base URL. Override via env for dev.
    server_base_url: str = "https://360booking.ro"
    # Serial port for real printer (e.g. COM3 / /dev/ttyUSB0)
    serial_port: Optional[str] = None
    serial_baud: int = 115200

    def save(self) -> None:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "BridgeConfig":
        p = config_path()
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception:
            return cls()

    def is_claimed(self) -> bool:
        return bool(self.device_token and self.websocket_url)
