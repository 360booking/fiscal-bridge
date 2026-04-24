"""Datecs serial protocol probe — sends STATUS (0x4A) in both FP-55 and
FP-700 dialects and reports which (if any) the printer ACKs. Useful when
open_fiscal returns NAK and we can't tell whether it's a framing problem
(wrong dialect) or a logical problem (wrong operator password, printer
state).

Usage from CLI:
    360booking-bridge.exe --probe-printer

Also callable from the GUI via the "Test comunicare" button.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import BridgeConfig
from .printers.datecs_fp import (
    DatecsFPError,
    DatecsFPTransport,
)

log = logging.getLogger("bridge.probe")


@dataclass
class ProbeResult:
    dialect: str                      # "fp55" | "fp700"
    ok: bool
    error: Optional[str] = None
    raw_response: Optional[str] = None  # hex


# Status command code — the same value on both dialects. Sending it is
# safe on any Datecs fiscal printer: it just returns the 6 status bytes,
# nothing is printed or changed.
CMD_STATUS = 0x4A


_DIALECTS = {
    "fp55":  {"encoding_offset": 0x20, "bcc_algo": "sum", "bcc_coverage": "body", "cmd_width": 4},
    "fp700": {"encoding_offset": 0x30, "bcc_algo": "xor", "bcc_coverage": "body", "cmd_width": 1},
}


def _probe_dialect(port: str, baud: int, dialect: str) -> ProbeResult:
    params = _DIALECTS[dialect]
    transport = DatecsFPTransport(port, baud, timeout=2.0, **params)
    try:
        transport.open()
    except Exception as exc:
        return ProbeResult(dialect=dialect, ok=False, error=f"open failed: {exc}")
    try:
        resp = transport.execute(CMD_STATUS, b"")
        return ProbeResult(
            dialect=dialect, ok=True,
            raw_response=resp.raw.hex() if hasattr(resp, "raw") else None,
        )
    except DatecsFPError as exc:
        return ProbeResult(dialect=dialect, ok=False, error=str(exc))
    except Exception as exc:
        return ProbeResult(dialect=dialect, ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        transport.close()


def probe_all(port: Optional[str] = None, baud: Optional[int] = None) -> dict:
    """Probe both dialects and return a structured summary.

    Returns:
        {
            "port": "COM3",
            "baud": 9600,
            "results": [ProbeResult(...), ProbeResult(...)],
            "recommended_variant": "fp55" | "fp700" | None,
        }
    """
    cfg = BridgeConfig.load()
    port = port or cfg.serial_port
    baud = baud or cfg.serial_baud or 9600

    if not port:
        return {
            "port": None, "baud": baud, "results": [],
            "recommended_variant": None,
            "error": "No serial port configured. Set it in Setări imprimantă first.",
        }

    results = []
    for dialect in ("fp55", "fp700"):
        r = _probe_dialect(port, baud, dialect)
        log.info(
            "probe %s on %s@%s: ok=%s error=%s raw=%s",
            dialect, port, baud, r.ok, r.error, r.raw_response,
        )
        results.append(r)

    recommended = next((r.dialect for r in results if r.ok), None)
    return {
        "port": port, "baud": baud,
        "results": results,
        "recommended_variant": recommended,
    }


def format_report(summary: dict) -> str:
    """Render the probe summary as a human-readable multi-line string
    suitable for both CLI output and a GUI messagebox."""
    lines = []
    lines.append(f"Port:  {summary.get('port') or '(none)'}")
    lines.append(f"Baud:  {summary.get('baud')}")
    lines.append("")
    if "error" in summary:
        lines.append(f"Eroare: {summary['error']}")
        return "\n".join(lines)
    for r in summary.get("results", []):
        label = "FP-55 (DP-25 modern)" if r.dialect == "fp55" else "FP-700 (DP-25 vechi)"
        if r.ok:
            lines.append(f"  ✓ {label}: RĂSPUNS OK")
            if r.raw_response:
                lines.append(f"      raw: {r.raw_response}")
        else:
            lines.append(f"  ✗ {label}: {r.error}")
    lines.append("")
    rec = summary.get("recommended_variant")
    if rec == "fp55":
        lines.append("Imprimanta folosește dialect FP-55. Asta e default — nu schimba nimic.")
    elif rec == "fp700":
        lines.append("Imprimanta folosește dialect FP-700.")
        lines.append("În Setări imprimantă setează protocol_variant = fp700 și salvează.")
    else:
        lines.append("Nicio variantă nu a răspuns. Verifică:")
        lines.append("  1. Port COM este corect (altă aplicație îl ocupă?)")
        lines.append("  2. Baud rate corespunde setării de pe imprimantă")
        lines.append("  3. Cablul USB/serial e conectat")
        lines.append("  4. Imprimanta e pornită și afișează 'Conexiune PC'")
    return "\n".join(lines)
