"""WebSocket loop: connect, hello, heartbeat, handle jobs, reconnect
on any disconnect with exponential backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import signal
from typing import Any

import websockets

from . import __version__
from . import printers, status
from .config import BridgeConfig
from .printers import FiscalPrinter, PrintJob

log = logging.getLogger("bridge.ws")


# In-memory cache of the last protocol config the server pushed.
# Merged into _build_printer() on every build. Default is empty so
# the driver falls back to its compiled-in defaults.
_server_protocol_config: dict = {}


def _build_printer(cfg: BridgeConfig) -> FiscalPrinter:
    """Look up the printer via the registry. Per-printer config is
    merged from three sources (later wins):
      1. BridgeConfig (local file — serial_port, baud, operator)
      2. Server-pushed protocol config (_server_protocol_config)
      3. Per-call overrides (none today)
    """
    printer_config = {
        "serial_port": cfg.serial_port,
        "serial_baud": cfg.serial_baud,
        "operator": getattr(cfg, "operator", "1"),
        "operator_password": getattr(cfg, "operator_password", "0000"),
    }
    # Server-pushed knobs override the compiled-in defaults but not
    # the local serial config (port/baud are physical to the tenant
    # machine and not reasonably pushable from the cloud).
    for k, v in _server_protocol_config.items():
        if k in ("serial_port", "serial_baud"):
            continue
        printer_config[k] = v

    try:
        return printers.build(cfg.printer_model, printer_config)
    except KeyError as exc:
        log.error("%s — falling back to simulator", exc)
        return printers.build("simulator", printer_config)


def _probe_printer(cfg: BridgeConfig) -> dict:
    """Quick connectivity check for the GUI status panel. Simulator
    is always OK; serial printers open the port for ~100ms to verify
    the device is reachable (doesn't print anything)."""
    if (cfg.printer_model or "simulator").lower() == "simulator":
        return {"printer_status": "ok", "printer_detail": "simulator"}
    port = (cfg.serial_port or "").strip()
    if not port:
        return {"printer_status": "not_configured", "printer_detail": "serial port missing"}
    try:
        import serial
        s = serial.Serial(port=port, baudrate=cfg.serial_baud or 9600, timeout=0.1)
        s.close()
        return {"printer_status": "ok", "printer_detail": f"{port} reachable"}
    except Exception as exc:
        return {"printer_status": "error", "printer_detail": f"{port}: {exc}"}


async def _heartbeat_loop(ws, interval: float = 30.0):
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send(json.dumps({"type": "heartbeat"}))
            status.write({"ws_connected": True})
        except websockets.ConnectionClosed:
            return


async def _run_once(cfg: BridgeConfig) -> None:
    url = f"{cfg.websocket_url}?token={cfg.device_token}"
    log.info("Connecting to %s", cfg.websocket_url)
    # Status snapshot before connect: process is up, printer probed,
    # WS not yet connected.
    status.write({
        "ws_connected": False,
        "printer_model": cfg.printer_model,
        "bridge_id": cfg.bridge_id,
        "tenant_id": cfg.tenant_id,
        "version": __version__,
        **_probe_printer(cfg),
    })
    printer = _build_printer(cfg)
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": __version__,
            "printer_model": cfg.printer_model,
            "os_info": f"{platform.system()} {platform.release()} {platform.version()}",
        }))
        status.write({"ws_connected": True})
        heartbeat_task = asyncio.create_task(_heartbeat_loop(ws))
        printer_ref = {"p": printer}  # rebuilt whenever server pushes new config
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                mtype = msg.get("type")
                if mtype == "welcome":
                    log.info("Registered as bridge %s", msg.get("bridge_id"))
                elif mtype == "config":
                    # Server pushed new protocol tweaks. Rebuild the
                    # printer so the next job uses the new knobs.
                    global _server_protocol_config
                    _server_protocol_config = msg.get("protocol") or {}
                    log.info("Protocol config from server: %s", _server_protocol_config)
                    try:
                        printer_ref["p"] = _build_printer(cfg)
                    except Exception as exc:
                        log.warning("Rebuild with server config failed: %s", exc)
                elif mtype == "heartbeat_ack":
                    pass
                elif mtype == "job":
                    await _handle_job(ws, printer_ref["p"], msg)
                elif mtype == "error":
                    log.warning("Server error: %s", msg.get("error"))
                else:
                    log.debug("Unknown message type: %s", mtype)
        finally:
            heartbeat_task.cancel()


async def _handle_job(ws, printer: FiscalPrinter, msg: dict) -> None:
    job = PrintJob(
        kind=msg.get("kind") or "",
        job_id=msg.get("job_id") or "",
        payload=msg.get("payload") or {},
    )
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, printer.handle, job,
        )
        await ws.send(json.dumps({
            "type": "job_result",
            "job_id": job.job_id,
            "success": result.success,
            "data": result.data,
            "error": result.error,
        }))
    except Exception as exc:
        log.exception("Job %s crashed", job.job_id)
        await ws.send(json.dumps({
            "type": "job_result",
            "job_id": job.job_id,
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        }))


async def run_forever() -> None:
    """Reconnect loop with exponential backoff (capped at 60s).

    Bails out (instead of retrying forever) on authentication errors —
    if the server returns 403 or closes with code 4001/4403, the
    token was revoked (admin hit "Deconectează" / "Reactivează").
    Retrying in that state just spams the server with doomed attempts
    and keeps the admin panel stuck in "offline".
    """
    cfg = BridgeConfig.load()
    if not cfg.is_claimed():
        raise SystemExit("Bridge not enrolled. Run with --enroll=CODE first.")

    backoff = 1.0
    auth_fail_count = 0
    while True:
        try:
            await _run_once(cfg)
            backoff = 1.0
            auth_fail_count = 0
        except asyncio.CancelledError:
            status.write({"ws_connected": False})
            raise
        except websockets.InvalidStatusCode as exc:
            status_code = getattr(exc, "status_code", 0)
            if status_code in (401, 403):
                auth_fail_count += 1
                log.error(
                    "Auth rejected by server (HTTP %s). The device_token has "
                    "been revoked — probably an admin disconnected or re-enrolled. "
                    "Fail #%d.",
                    status_code, auth_fail_count,
                )
                status.write({
                    "ws_connected": False,
                    "last_error": f"HTTP {status_code} — token revoked. Re-enroll required.",
                })
                if auth_fail_count >= 3:
                    log.error("Giving up — run with --enroll=<new code> to re-authenticate.")
                    return
            else:
                log.warning("Connection lost (HTTP %s) — retrying in %.0fs", status_code, backoff)
                status.write({"ws_connected": False, "last_error": f"HTTP {status_code}"})
        except Exception as exc:
            log.warning("Connection lost: %s — retrying in %.0fs", exc, backoff)
            status.write({"ws_connected": False, "last_error": str(exc)})
        await asyncio.sleep(backoff)
        backoff = min(60.0, backoff * 1.7)
