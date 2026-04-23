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
from .config import BridgeConfig
from .printers import FiscalPrinter, PrintJob, SimulatorPrinter
from .printers.datecs_dp25 import DatecsDP25Printer

log = logging.getLogger("bridge.ws")


def _build_printer(cfg: BridgeConfig) -> FiscalPrinter:
    model = (cfg.printer_model or "simulator").lower()
    if model == "datecs_dp25":
        return DatecsDP25Printer({"serial_port": cfg.serial_port, "serial_baud": cfg.serial_baud})
    return SimulatorPrinter()


async def _heartbeat_loop(ws, interval: float = 30.0):
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send(json.dumps({"type": "heartbeat"}))
        except websockets.ConnectionClosed:
            return


async def _run_once(cfg: BridgeConfig) -> None:
    url = f"{cfg.websocket_url}?token={cfg.device_token}"
    log.info("Connecting to %s", cfg.websocket_url)
    printer = _build_printer(cfg)
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": __version__,
            "printer_model": cfg.printer_model,
            "os_info": f"{platform.system()} {platform.release()} {platform.version()}",
        }))
        heartbeat_task = asyncio.create_task(_heartbeat_loop(ws))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                mtype = msg.get("type")
                if mtype == "welcome":
                    log.info("Registered as bridge %s", msg.get("bridge_id"))
                elif mtype == "heartbeat_ack":
                    pass
                elif mtype == "job":
                    await _handle_job(ws, printer, msg)
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
    """Reconnect loop with exponential backoff (capped at 60s)."""
    cfg = BridgeConfig.load()
    if not cfg.is_claimed():
        raise SystemExit("Bridge not enrolled. Run with --enroll=CODE first.")

    backoff = 1.0
    while True:
        try:
            await _run_once(cfg)
            # Clean disconnect = server revoked us or closed socket.
            # Reset backoff on clean disconnect, we'll try to reconnect.
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Connection lost: %s — retrying in %.0fs", exc, backoff)
        await asyncio.sleep(backoff)
        backoff = min(60.0, backoff * 1.7)
