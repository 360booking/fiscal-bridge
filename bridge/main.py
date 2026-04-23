"""Entry point for the 360booking fiscal bridge.

Usage:
  bridge --enroll=F3KP7XMA [--printer=datecs_dp25|simulator]
  bridge --run
  bridge --install             # register scheduled-task-at-logon + run
  bridge --uninstall
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request

from .config import BridgeConfig
from .ws_client import run_forever


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s — %(message)s",
    )


def _claim_code(code: str, printer_model: str, server_base_url: str) -> BridgeConfig:
    import platform as _p
    payload = {
        "code": code,
        "printer_model": printer_model,
        "version": "0.1.0",
        "os_info": f"{_p.system()} {_p.release()}",
    }
    req = urllib.request.Request(
        f"{server_base_url.rstrip('/')}/api/fiscal-bridge/claim",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Enrollment failed: HTTP {exc.code} — {body}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Enrollment failed: network — {exc}")

    cfg = BridgeConfig.load()
    cfg.device_token = data["device_token"]
    cfg.tenant_id = data["tenant_id"]
    cfg.bridge_id = data["bridge_id"]
    cfg.websocket_url = data["websocket_url"]
    cfg.printer_model = printer_model
    cfg.server_base_url = server_base_url
    cfg.save()
    print(f"✔ Enrolled as bridge {cfg.bridge_id} for tenant {cfg.tenant_id}")
    print(f"  Config saved to: {cfg.__class__.__name__} → {cfg.device_token[:12]}…")
    return cfg


def _install_autorun() -> None:
    """Register the bridge to run at user login via Windows scheduled
    task. On Linux/macOS, print an instruction (systemd/launchd setup
    left manual for non-Windows targets)."""
    import platform as _p
    import subprocess, sys as _sys
    if _p.system() != "Windows":
        print("Auto-run registration is Windows-only in this build.")
        print("On Linux/macOS, run `360booking-bridge --run` under systemd/launchd.")
        return
    exe = _sys.executable if getattr(_sys, "frozen", False) else _sys.argv[0]
    cmd = [
        "schtasks", "/Create", "/F",
        "/SC", "ONLOGON",
        "/TN", "360bookingFiscalBridge",
        "/TR", f'"{exe}" --run',
        "/RL", "HIGHEST",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("✔ Scheduled task 360bookingFiscalBridge registered (runs at login).")
    except subprocess.CalledProcessError as exc:
        print(f"Could not register scheduled task: {exc.stderr or exc.stdout}")


def _uninstall_autorun() -> None:
    import platform as _p
    import subprocess
    if _p.system() != "Windows":
        print("Skipped (non-Windows).")
        return
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", "360bookingFiscalBridge"],
            check=True, capture_output=True, text=True,
        )
        print("✔ Scheduled task removed.")
    except subprocess.CalledProcessError as exc:
        print(f"Nothing to uninstall: {exc.stderr or exc.stdout}")


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    p = argparse.ArgumentParser(prog="360booking-bridge")
    p.add_argument("--enroll", metavar="CODE", help="One-time enrollment code from the admin UI")
    p.add_argument("--printer", default="simulator",
                   choices=["simulator", "datecs_dp25"],
                   help="Printer backend (default: simulator)")
    p.add_argument("--server", default="https://360booking.ro",
                   help="Server base URL (default: https://360booking.ro)")
    p.add_argument("--run", action="store_true", help="Run the WebSocket loop")
    p.add_argument("--install", action="store_true", help="Register auto-start (Windows)")
    p.add_argument("--uninstall", action="store_true", help="Remove auto-start")
    args = p.parse_args(argv)

    if args.uninstall:
        _uninstall_autorun()
        return 0

    if args.enroll:
        _claim_code(args.enroll, args.printer, args.server)
        if args.install:
            _install_autorun()
        if args.run:
            asyncio.run(run_forever())
        return 0

    if args.install:
        _install_autorun()
        return 0

    if args.run:
        asyncio.run(run_forever())
        return 0

    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
