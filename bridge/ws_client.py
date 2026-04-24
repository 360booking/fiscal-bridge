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
    # machine and not reasonably pushable from the cloud) and not
    # operator/operator_password (set per-till in the GUI by the
    # person physically at the restaurant — the server default is
    # 0000 but many tenants run with 0001 or per-cashier passwords).
    _LOCAL_WINS = {"serial_port", "serial_baud", "operator", "operator_password"}
    for k, v in _server_protocol_config.items():
        if k in _LOCAL_WINS:
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


async def _heartbeat_loop(ws, interval: float = 15.0):
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
    # ping_interval 20s keeps us under typical 60s proxy idle timeouts;
    # ping_timeout 60s (up from 20s) tolerates occasional server-side
    # event-loop hiccups without dropping the connection. Combined with
    # the 15s app-level heartbeat, this keeps the WS up through brief
    # DB slowdowns without stacking reconnects that kill in-flight jobs.
    async with websockets.connect(url, ping_interval=20, ping_timeout=60) as ws:
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
    # If the user changed config in the GUI (operator password, serial
    # port, etc.) after we connected, the printer instance still holds
    # the old values. Rebuild it from the current config file so the
    # very next job picks up the change without a process restart.
    try:
        fresh_cfg = BridgeConfig.load()
        printer = _build_printer(fresh_cfg)
    except Exception as exc:
        log.warning("Could not refresh printer config before job: %s — using cached printer", exc)
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


def _close_code(exc: BaseException) -> int | None:
    """Extract the WebSocket close code from a websockets exception.

    `websockets` >= 11 exposes the close frames on `.rcvd` / `.sent`
    (CloseFrame objects with a `.code` int). Older releases stored
    them as attributes on the exception directly. We check both so
    the bridge keeps working across versions."""
    for attr in ("rcvd", "sent"):
        frame = getattr(exc, attr, None)
        code = getattr(frame, "code", None) if frame is not None else None
        if isinstance(code, int):
            return code
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


async def run_forever() -> None:
    """Reconnect loop with exponential backoff (capped at 60s).

    Bails out (instead of retrying forever) on two conditions:
      - Auth failure (HTTP 401/403 or repeated 4001/4403 close) — the
        token was revoked, admin hit "Deconectează"/"Reactivează".
        Retrying just spams the server.
      - Close code 4000 "replaced by new connection" — another
        instance of the bridge is already connected for this tenant.
        Retrying means we'll kick them off and they'll kick us off
        in turn, producing the 8s-apart "4000 (private use)" flap
        seen in field reports after a power cut. Exiting here lets
        the duplicate pair collapse to exactly one survivor: whoever
        wins the server's last-connect-wins race keeps the slot, the
        other dies for good.
    """
    initial = BridgeConfig.load()
    if not initial.is_claimed():
        raise SystemExit("Bridge not enrolled. Run with --enroll=CODE first.")

    backoff = 1.0
    auth_fail_count = 0
    while True:
        # Reload config from disk each iteration so GUI Save edits
        # (operator password, serial port, model, etc.) take effect on
        # the NEXT reconnect — without requiring a full process restart.
        cfg = BridgeConfig.load()
        if not cfg.is_claimed():
            cfg = initial
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
        except websockets.ConnectionClosed as exc:
            code = _close_code(exc)
            if code == 4000:
                log.error(
                    "Server closed our connection with 4000 (replaced by new "
                    "connection). Another 360booking bridge is already "
                    "connected for this tenant. Exiting so the two instances "
                    "stop flapping — restart only one of them."
                )
                status.write({
                    "ws_connected": False,
                    "last_error": "duplicate bridge (close 4000) — exited",
                })
                return
            log.warning("Connection lost (close %s): %s — retrying in %.0fs", code, exc, backoff)
            status.write({"ws_connected": False, "last_error": f"close {code}: {exc}"})
        except Exception as exc:
            log.warning("Connection lost: %s — retrying in %.0fs", exc, backoff)
            status.write({"ws_connected": False, "last_error": str(exc)})
        await asyncio.sleep(backoff)
        backoff = min(60.0, backoff * 1.7)
