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
    """File logging always; stdout only when a real console is attached.
    The windowed .exe has no console, so stdout handler is a no-op there."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    fh = logging.FileHandler(_log_path(), mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Only add a stdout handler when we actually have a console — the
    # windowed build points stdout at nul and the handler would waste
    # cycles formatting lines for nobody.
    if sys.stdout and sys.stdout.isatty():
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
    # Idempotency guard: if we already have a claimed config, skip the
    # network call. Prevents 410 Gone when a parent invocation already
    # saved the token and we're running in the elevated copy.
    existing = BridgeConfig.load()
    if existing.is_claimed():
        _ok("ENROLL", f"already claimed as bridge {existing.bridge_id[:8]}… (skipping re-claim)")
        return existing
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


def _write_hidden_launcher(exe_path: str) -> Path:
    """Write a tiny VBScript that runs the bridge fully hidden (no
    console window) via WScript.Shell.Run(..., 0). The scheduled task
    targets this .vbs instead of the .exe directly — that way Windows
    doesn't flash a console at every login."""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    vbs = d / "run-hidden.vbs"
    content = (
        'Set shell = CreateObject("WScript.Shell")\r\n'
        'shell.Run """' + exe_path + '"" --run", 0, False\r\n'
    )
    vbs.write_text(content, encoding="utf-8")
    return vbs


def _install_scheduled_task() -> bool:
    """Fallback installer that doesn't need admin — registers a
    per-user scheduled task via schtasks. Runs at logon with a hidden
    VBS launcher so no console window appears."""
    import subprocess
    exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    vbs = _write_hidden_launcher(exe)
    _ok("AUTORUN", f"hidden launcher → {vbs}")
    cmd = [
        "schtasks", "/Create", "/F",
        "/SC", "ONLOGON",
        "/TN", "360bookingFiscalBridge",
        "/TR", f'wscript.exe "{vbs}"',
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        _ok("AUTORUN", "scheduled task '360bookingFiscalBridge' registered (runs at login)")
        return True
    except subprocess.CalledProcessError as exc:
        _fail("AUTORUN", (exc.stderr or exc.stdout or str(exc)).strip())
        return False


def _install_autorun() -> None:
    """Prefer a real Windows service (robust across logout / reboot /
    crash). Fall back to a per-user scheduled task if admin elevation
    is declined or NSSM isn't bundled in this build."""
    if platform.system() != "Windows":
        _info("AUTORUN", "Skipped — Windows-only in this build.")
        _info("HINT", "On Linux/macOS, run `360booking-bridge --run` under systemd/launchd.")
        return

    from . import service

    exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]

    if service.is_admin():
        _info("AUTORUN", "Admin detected — installing as Windows Service (most robust)")
        ok, msg = service.install_service(exe)
        if ok:
            _ok("AUTORUN", f"Windows Service: {msg}")
            _info("AUTORUN", "Starts at BOOT (before login), survives logout, auto-restarts on crash")
            return
        _fail("AUTORUN", f"Service install failed: {msg}")
        _info("AUTORUN", "Falling back to scheduled task (per-user)")
        _install_scheduled_task()
        return

    # Not admin — try to elevate. Strip --enroll from the relaunched
    # command: the config was just saved by this (non-admin) process,
    # so the elevated copy should NOT try to claim the code again (it
    # would fail with HTTP 410 since the code is one-time-use). The
    # elevated copy just needs --install to register the service and
    # --run to start it.
    if os.environ.get("FB_NO_ELEVATE") != "1":
        _info("AUTORUN", "Requesting admin to install as Windows Service…")
        original = list(sys.argv[1:])
        cleaned: list[str] = []
        skip_next = False
        for i, a in enumerate(original):
            if skip_next:
                skip_next = False
                continue
            if a.startswith("--enroll="):
                continue
            if a == "--enroll":
                skip_next = True
                continue
            cleaned.append(a)
        # Always include --install for the elevated copy.
        if "--install" not in cleaned:
            cleaned.append("--install")
        os.environ["FB_NO_ELEVATE"] = "1"
        if service.relaunch_as_admin(cleaned):
            _ok("AUTORUN", "Relaunched with admin rights — this window will close.")
            _info("AUTORUN", "Service installation continues in the elevated window.")
            sys.exit(0)
        _fail("AUTORUN", "UAC elevation declined")

    _info("AUTORUN", "Falling back to scheduled task (per-user, less robust)")
    _install_scheduled_task()


def _start_hidden_now() -> None:
    """Spawn a detached hidden copy of the bridge and exit the current
    process. Lets the user test `--background` without reinstalling."""
    import subprocess
    if platform.system() != "Windows":
        _info("BACKGND", "Non-Windows: launching with nohup-style detach")
        subprocess.Popen(
            [sys.argv[0], "--run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    vbs = _write_hidden_launcher(exe)
    subprocess.Popen(
        ["wscript.exe", str(vbs)],
        creationflags=0x00000008,  # DETACHED_PROCESS
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    _ok("BACKGND", "bridge started in background, no console")
    _info("BACKGND", "to stop: open Task Manager → kill the 360booking-bridge process")
    _info("BACKGND", f"logs: {_log_path()}")


def _uninstall_autorun() -> None:
    import subprocess
    if platform.system() != "Windows":
        _info("AUTORUN", "Skipped (non-Windows).")
        return
    # Try both — service + scheduled task — since we don't know which
    # install path the user took. Service removal needs admin.
    from . import service
    if service.service_state() != "missing":
        if service.is_admin():
            ok, msg = service.uninstall_service()
            (_ok if ok else _fail)("AUTORUN", f"Windows Service: {msg}")
        else:
            _info("AUTORUN", "Windows Service detected — needs admin to uninstall")
            if service.relaunch_as_admin():
                sys.exit(0)
    r = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", "360bookingFiscalBridge"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        _ok("AUTORUN", "scheduled task removed")
    else:
        _info("AUTORUN", "no scheduled task to remove")


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
    """Run the WebSocket loop. In windowed (no-console) builds we route
    through the tray icon so the user sees a visible indicator in the
    notification area; CLI runs (with a real terminal) stick to the
    plain loop so output still shows up in the window."""
    if sys.stdout and sys.stdout.isatty():
        print()
        print(_c("32", f"  {_c('1', 'Bridge is running.')}  Press Ctrl+C to stop."))
        print()
        try:
            asyncio.run(run_forever())
        except KeyboardInterrupt:
            print()
            _info("EXIT", "stopped by user (Ctrl+C)")
        return
    # Windowed / background build: start the tray icon + WS loop.
    try:
        from .tray import run_tray_with_loop
        run_tray_with_loop(lambda: asyncio.run(run_forever()))
    except Exception as exc:
        log = logging.getLogger("bridge.main")
        log.exception("Tray startup failed, falling back to headless loop: %s", exc)
        asyncio.run(run_forever())


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
    p.add_argument("--run", action="store_true", help="Run the WebSocket loop with tray icon (or console if launched from a terminal)")
    p.add_argument("--upgrade", action="store_true",
                   help="Download the latest .exe from GitHub, stop current bridge, relaunch new version")
    p.add_argument("--background", action="store_true",
                   help="Start the bridge in a hidden background process and exit "
                        "the console. Combine with --run. On Windows uses a VBS "
                        "launcher; no console window appears.")
    p.add_argument("--install", action="store_true",
                   help="Register auto-start at user login (Windows scheduled task, "
                        "always hidden — no console window on boot)")
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
        if args.upgrade:
            from .upgrade import run_upgrade
            run_upgrade()
            return 0

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
            if args.run and args.background:
                _start_hidden_now()
            elif args.run:
                _run_loop()
            else:
                _info("DONE", "Enrollment complete. Run again with --run to start the loop.")
            return 0

        if args.install and not args.run:
            _install_autorun()
            return 0

        if args.install and args.run:
            _install_autorun()
            # Intentionally fall through to --run so the bridge starts
            # serving in the current window immediately after the
            # service is registered.

        if args.run:
            cfg = BridgeConfig.load()
            if not cfg.is_claimed():
                _fail("CONFIG", "bridge is not enrolled — run with --enroll=CODE first")
                return 1
            _ok("CONFIG", f"loaded — bridge {cfg.bridge_id[:8]}…, tenant {cfg.tenant_id[:8]}…")
            _info("PRINTER", f"model={cfg.printer_model}, serial={cfg.serial_port or 'none'}")
            if cfg.printer_model != "simulator" and cfg.serial_port:
                _check_serial_port(cfg.serial_port)
            if args.background:
                _start_hidden_now()
            else:
                _run_loop()
            return 0

        # No args → double-click from Explorer.
        #  - If enrolled: drop into tray + WS loop so the bridge keeps
        #    running; user can open the GUI from the tray menu.
        #  - If not enrolled: open the enrollment GUI.
        cfg = BridgeConfig.load()
        if cfg.is_claimed():
            _run_loop()
            return 0
        try:
            from .gui import run_gui
            return run_gui()
        except Exception as exc:
            _fail("GUI", f"could not start: {exc}")
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
