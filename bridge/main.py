"""Entry point for the 360booking fiscal bridge.

Usage:
  bridge --enroll=F3KP7XMA [--printer=datecs_dp25|simulator]
  bridge --run
  bridge --install             # register scheduled-task-at-logon + run
  bridge --uninstall

Design goal: every step prints a clear status line so the user can see
at a glance whether we passed it or failed it. On fatal error, the
window pauses for a keypress so they can actually READ the error even
when the .exe was double-clicked from Explorer.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import BridgeConfig, config_dir
from .ws_client import run_forever


# ANSI colors work in Windows 10+ cmd.exe (virtual terminal). Keep
# them ASCII-safe for Windows 7 / legacy consoles via fallback below.
_USE_COLOR = os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    if not _USE_COLOR:
        return s
    return f"\033[{code}m{s}\033[0m"


def _ok(label: str, msg: str) -> None:
    print(f"  {_c('32', '✓')} [{label:<7}] {msg}")


def _fail(label: str, msg: str) -> None:
    print(f"  {_c('31', '✗')} [{label:<7}] {msg}")


def _info(label: str, msg: str) -> None:
    print(f"    [{label:<7}] {msg}")


def _banner() -> None:
    line = "=" * 60
    print()
    print(line)
    print(f"  360booking Fiscal Bridge  v{__version__}")
    print(line)
    print(f"  Config dir:  {config_dir()}")
    print(f"  Log file:    {_log_path()}")
    print(f"  OS:          {platform.system()} {platform.release()}")
    print(line)
    print()


def _log_path() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "bridge.log"


def _setup_logging(verbose: bool = False) -> None:
    """Console + file logging. File is always written so users can
    attach it to a support request when things break."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Clear any pre-existing handlers (avoids duplicate lines when
    # called twice from --enroll --run flow).
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    fh = logging.FileHandler(_log_path(), mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def _pause_on_error(exc: BaseException) -> None:
    """Print a clean crash report and wait for Enter so the user can
    read it before the console closes. Only does this on Windows and
    only when stdin is interactive (so CI / service runs don't hang)."""
    print()
    print(_c("31", "=" * 60))
    print(_c("31", "  FATAL ERROR"))
    print(_c("31", "=" * 60))
    print(f"  {type(exc).__name__}: {exc}")
    print()
    print("  Log file copy:")
    print(f"    {_log_path()}")
    print()
    print("  Full traceback:")
    for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
        print("    " + line.rstrip())
    print(_c("31", "=" * 60))
    try:
        if sys.stdin and sys.stdin.isatty():
            input("  Press Enter to exit... ")
    except Exception:
        pass


def _claim_code(code: str, printer_model: str, server_base_url: str) -> BridgeConfig:
    _info("ENROLL", f"POST {server_base_url}/api/fiscal-bridge/claim  (code {code[:4]}****)")
    payload = {
        "code": code,
        "printer_model": printer_model,
        "version": __version__,
        "os_info": f"{platform.system()} {platform.release()}",
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
        _fail("ENROLL", f"HTTP {exc.code} — {body}")
        if exc.code == 404:
            _info("HINT", "The code may have expired (10 min TTL) or was typed wrong.")
            _info("HINT", "Generate a fresh one from 360booking → Restaurant → Setări fiscale → Activează.")
        if exc.code == 410:
            _info("HINT", "Code expired. Regenerate from the admin panel.")
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        _fail("ENROLL", f"network — {exc.reason}")
        _info("HINT", "Check the PC's internet connection and that 360booking.ro resolves.")
        raise SystemExit(1)

    cfg = BridgeConfig.load()
    cfg.device_token = data["device_token"]
    cfg.tenant_id = data["tenant_id"]
    cfg.bridge_id = data["bridge_id"]
    cfg.websocket_url = data["websocket_url"]
    cfg.printer_model = printer_model
    cfg.server_base_url = server_base_url
    cfg.save()
    _ok("ENROLL", f"bridge {cfg.bridge_id[:8]}… for tenant {cfg.tenant_id[:8]}…")
    _ok("CONFIG", f"saved → {config_dir() / 'config.json'}")
    return cfg


def _install_autorun() -> None:
    import subprocess
    if platform.system() != "Windows":
        _info("AUTORUN", "Skipped — Windows-only in this build.")
        _info("HINT", "On Linux/macOS, run `360booking-bridge --run` under systemd/launchd.")
        return
    exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    cmd = [
        "schtasks", "/Create", "/F",
        "/SC", "ONLOGON",
        "/TN", "360bookingFiscalBridge",
        "/TR", f'"{exe}" --run',
        "/RL", "HIGHEST",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        _ok("AUTORUN", "scheduled task '360bookingFiscalBridge' registered (runs at login)")
    except subprocess.CalledProcessError as exc:
        _fail("AUTORUN", (exc.stderr or exc.stdout or str(exc)).strip())


def _uninstall_autorun() -> None:
    import subprocess
    if platform.system() != "Windows":
        _info("AUTORUN", "Skipped (non-Windows).")
        return
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", "360bookingFiscalBridge"],
            check=True, capture_output=True, text=True,
        )
        _ok("AUTORUN", "scheduled task removed")
    except subprocess.CalledProcessError as exc:
        _info("AUTORUN", f"nothing to uninstall: {(exc.stderr or exc.stdout or '').strip()}")


def _check_serial_port(port: str) -> None:
    """Best-effort probe — opens the COM port briefly to confirm it
    exists and we can access it. Non-fatal on failure (the user might
    want to enroll before plugging in the printer)."""
    if not port:
        return
    try:
        import serial
        s = serial.Serial(port=port, baudrate=9600, timeout=0.1)
        s.close()
        _ok("SERIAL", f"{port} is reachable")
    except serial.SerialException as exc:
        _fail("SERIAL", f"{port} not reachable: {exc}")
        _info("HINT", "Verify the printer is powered on, cable connected, and no other app holds the port.")
    except Exception as exc:
        _fail("SERIAL", f"probe failed: {exc}")


def _run_loop() -> None:
    """Wrap the asyncio loop with a "running" status line, so the user
    sees confirmation when the WebSocket is up."""
    print()
    print(_c("32", f"  {_c('1', 'Bridge is running.')}  Press Ctrl+C to stop."))
    print()
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        print()
        _info("EXIT", "stopped by user (Ctrl+C)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="360booking-bridge", add_help=True)
    p.add_argument("--enroll", metavar="CODE", help="One-time enrollment code from the admin UI")
    from .printers import available_models
    p.add_argument("--printer", default="simulator",
                   choices=available_models(),
                   help=f"Printer backend (default: simulator). Registered: {', '.join(available_models())}")
    p.add_argument("--server", default="https://360booking.ro",
                   help="Server base URL (default: https://360booking.ro)")
    p.add_argument("--serial-port", default=None,
                   help="Serial port of the fiscal printer (e.g. COM3 on Windows, /dev/ttyUSB0 on Linux).")
    p.add_argument("--serial-baud", type=int, default=115200,
                   help="Serial baud rate (default: 115200)")
    p.add_argument("--run", action="store_true", help="Run the WebSocket loop")
    p.add_argument("--install", action="store_true", help="Register auto-start (Windows)")
    p.add_argument("--uninstall", action="store_true", help="Remove auto-start")
    p.add_argument("--verbose", action="store_true", help="Print debug-level logs")
    args = p.parse_args(argv)

    # Banner + logging go first so even early failures leave a trail.
    _banner()
    try:
        _setup_logging(args.verbose)
    except Exception as exc:
        print(f"(Logging setup failed: {exc}. Continuing in stdout-only mode.)")

    try:
        if args.uninstall:
            _uninstall_autorun()
            return 0

        if args.enroll:
            cfg = _claim_code(args.enroll, args.printer, args.server)
            if args.serial_port or args.serial_baud != 115200:
                cfg.serial_port = args.serial_port
                cfg.serial_baud = args.serial_baud
                cfg.save()
                _ok("SERIAL", f"config → {cfg.serial_port or 'none'} @ {cfg.serial_baud}")
            if args.serial_port:
                _check_serial_port(args.serial_port)
            if args.install:
                _install_autorun()
            if args.run:
                _run_loop()
            else:
                _info("DONE", "Enrollment complete. Run again with --run to start the loop.")
            return 0

        if args.install:
            _install_autorun()
            return 0

        if args.run:
            cfg = BridgeConfig.load()
            if not cfg.is_claimed():
                _fail("CONFIG", "bridge is not enrolled — run with --enroll=CODE first")
                return 1
            _ok("CONFIG", f"loaded — bridge {cfg.bridge_id[:8]}…, tenant {cfg.tenant_id[:8]}…")
            _info("PRINTER", f"model={cfg.printer_model}, serial={cfg.serial_port or 'none'}")
            if cfg.printer_model != "simulator" and cfg.serial_port:
                _check_serial_port(cfg.serial_port)
            _run_loop()
            return 0

        p.print_help()
        return 2
    except SystemExit:
        raise
    except BaseException as exc:
        _pause_on_error(exc)
        return 1


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit as se:
        rc = se.code if isinstance(se.code, int) else 1
    except BaseException as exc:
        _pause_on_error(exc)
        rc = 1
    raise SystemExit(rc)
