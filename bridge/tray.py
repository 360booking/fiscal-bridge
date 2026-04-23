"""System tray icon — visible indicator that the bridge is running.

Right-click → Open GUI / Upgrade / Stop. Icon colour encodes status:
  green  = connected to 360booking + printer reachable
  yellow = connected to server but printer not reachable / not configured
  red    = server unreachable
  gray   = starting up / status unknown

Depends on `pystray` + `Pillow`. Both are pure-Python enough to bundle
into the PyInstaller build cleanly.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import webbrowser
from typing import Optional

from . import status as status_file

log = logging.getLogger("bridge.tray")


def _make_icon_image(color: tuple[int, int, int]):
    """Build a tiny solid-color circle icon on a transparent square."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=color + (255,), outline=(40, 40, 40, 255))
    return img


def _state_color(stat: Optional[dict]) -> tuple[int, int, int]:
    if not stat:
        return (150, 150, 150)  # gray
    if stat.get("stale"):
        return (150, 150, 150)
    ws = stat.get("ws_connected")
    printer = stat.get("printer_status")
    if not ws:
        return (200, 60, 60)  # red
    if printer == "ok":
        return (60, 180, 75)  # green
    return (230, 180, 40)  # yellow


def _state_tooltip(stat: Optional[dict]) -> str:
    if not stat or stat.get("stale"):
        return "360booking Bridge — neinițializat"
    ws = "connected" if stat.get("ws_connected") else "OFFLINE"
    printer_kind = stat.get("printer_model") or "simulator"
    printer_state = stat.get("printer_status") or "unknown"
    return f"360booking Bridge\nServer: {ws}\nPrinter: {printer_kind} ({printer_state})"


def run_tray_with_loop(ws_loop) -> None:
    """Run the WebSocket loop in the background and block on the
    pystray main loop in the current thread (foreground). The tray
    icon redraws itself every 2s with the latest status.

    Windows needs the pystray event loop on the main thread or the
    tray icon won't appear in the notification area.
    """
    import pystray

    # Start the WS loop in a daemon thread so Ctrl-C / tray Quit kills
    # the whole process cleanly.
    t = threading.Thread(target=ws_loop, daemon=True)
    t.start()

    # Build icon (initial color)
    img = _make_icon_image((150, 150, 150))

    def _open_gui(icon, item):
        try:
            from . import gui
            # Launch the GUI in a detached thread
            threading.Thread(target=gui.run_gui, daemon=True).start()
        except Exception as exc:
            log.exception("Failed to open GUI: %s", exc)

    def _open_admin(icon, item):
        webbrowser.open("https://360booking.ro/admin/restaurant/fiscal")

    def _open_log(icon, item):
        from .config import config_dir
        log_path = config_dir() / "bridge.log"
        if log_path.exists():
            subprocess.Popen(["notepad.exe", str(log_path)])

    def _upgrade(icon, item):
        try:
            from .upgrade import run_upgrade
            threading.Thread(target=run_upgrade, daemon=True).start()
        except Exception as exc:
            log.exception("Upgrade failed to start: %s", exc)

    def _quit(icon, item):
        icon.stop()
        # kill the whole process; ws loop is a daemon so it dies too
        import os
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Stare curentă", lambda icon, item: None, enabled=False),
        pystray.MenuItem("Deschide panoul 360booking", _open_admin),
        pystray.MenuItem("Deschide setări bridge…", _open_gui),
        pystray.MenuItem("Deschide log", _open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Actualizează la ultima versiune", _upgrade),
        pystray.MenuItem("Oprește bridge-ul", _quit),
    )
    icon = pystray.Icon("360booking-bridge", img, "360booking Bridge", menu)

    def _redraw_forever(icon_ref):
        import time
        while True:
            time.sleep(2.0)
            stat = status_file.read()
            try:
                icon_ref.icon = _make_icon_image(_state_color(stat))
                icon_ref.title = _state_tooltip(stat)
            except Exception:
                pass

    threading.Thread(target=_redraw_forever, args=(icon,), daemon=True).start()
    icon.run()
