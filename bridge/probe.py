"""Datecs serial protocol probe — sweeps dialects + common bauds and
reports which combination (if any) the printer ACKs. Useful when
open_fiscal returns NAK and we can't tell whether it's a framing
problem (wrong dialect), a baud mismatch, or a wrong COM port.

Usage from CLI:
    360booking-bridge.exe --probe-printer

Also callable from the GUI via the "Test comunicare" button.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, List

from .config import BridgeConfig
from .printers.datecs_fp import (
    DatecsFPError,
    DatecsFPTransport,
)

log = logging.getLogger("bridge.probe")


@dataclass
class ProbeResult:
    dialect: str                      # "fp55" | "fp700"
    baud: int
    ok: bool
    error: Optional[str] = None
    raw_response: Optional[str] = None  # hex


CMD_STATUS = 0x4A

# Baud rates commonly seen on Datecs fiscal printers. 9600 is the
# factory default; 115200 is typical for modern USB installs;
# 19200/38400 appear on older serial setups.
_COMMON_BAUDS = [9600, 115200, 19200, 38400, 57600, 4800]

_DIALECTS = {
    "fp55":  {"encoding_offset": 0x20, "bcc_algo": "sum", "bcc_coverage": "body", "cmd_width": 4},
    "fp700": {"encoding_offset": 0x30, "bcc_algo": "xor", "bcc_coverage": "body", "cmd_width": 1},
}


def _probe_one(port: str, baud: int, dialect: str) -> ProbeResult:
    params = _DIALECTS[dialect]
    transport = DatecsFPTransport(port, baud, timeout=1.2, **params)
    try:
        transport.open()
    except Exception as exc:
        return ProbeResult(dialect=dialect, baud=baud, ok=False, error=f"open failed: {exc}")
    try:
        resp = transport.execute(CMD_STATUS, b"")
        return ProbeResult(
            dialect=dialect, baud=baud, ok=True,
            raw_response=resp.raw.hex() if hasattr(resp, "raw") else None,
        )
    except DatecsFPError as exc:
        return ProbeResult(dialect=dialect, baud=baud, ok=False, error=str(exc))
    except Exception as exc:
        return ProbeResult(dialect=dialect, baud=baud, ok=False,
                           error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            transport.close()
        except Exception:
            pass


def list_serial_ports() -> List[dict]:
    """List every COM port visible on this machine, with description,
    manufacturer and VID/PID when available. Uses pyserial's
    serial.tools.list_ports — no hardware I/O."""
    try:
        import serial.tools.list_ports as lp
    except Exception as exc:
        log.warning("list_ports not available: %s", exc)
        return []
    out = []
    for p in lp.comports():
        out.append({
            "device": p.device,
            "name": getattr(p, "name", "") or "",
            "description": p.description or "",
            "hwid": p.hwid or "",
            "manufacturer": getattr(p, "manufacturer", None) or "",
            "product": getattr(p, "product", None) or "",
        })
    return out


def probe_all(port: Optional[str] = None, baud: Optional[int] = None,
              sweep_bauds: bool = True) -> dict:
    """Probe the configured port across dialects and (optionally) bauds.
    Tries the user's configured baud first, then common alternatives so
    the right combo is found quickly even when the user has the wrong
    baud in Setări.

    Returns:
        {
            "port": "COM4", "baud": <configured>, "ports": [...],
            "results": [ProbeResult(...), ...],
            "recommended": {"dialect": "fp55", "baud": 9600} | None,
        }
    """
    cfg = BridgeConfig.load()
    port = port or cfg.serial_port
    cfg_baud = baud or cfg.serial_baud or 9600

    ports = list_serial_ports()

    if not port:
        return {
            "port": None, "baud": cfg_baud, "ports": ports,
            "results": [], "recommended": None,
            "error": "No serial port configured. Set it in Setări imprimantă first.",
        }

    # Put the configured baud first so the success case is fast.
    baud_order = [cfg_baud] + [b for b in _COMMON_BAUDS if b != cfg_baud] if sweep_bauds else [cfg_baud]

    results: List[ProbeResult] = []
    recommended = None
    for b in baud_order:
        for dialect in ("fp55", "fp700"):
            r = _probe_one(port, b, dialect)
            log.info(
                "probe %s on %s@%s: ok=%s error=%s raw=%s",
                dialect, port, b, r.ok, r.error, r.raw_response,
            )
            results.append(r)
            if r.ok:
                recommended = {"dialect": dialect, "baud": b}
                # Found a working combo — stop sweeping to save the
                # user time (each probe ~2s of serial I/O).
                return {
                    "port": port, "baud": cfg_baud, "ports": ports,
                    "results": results, "recommended": recommended,
                }
            # Some Datecs firmwares NAK back-to-back frames at the same
            # baud; short pause before the next attempt.
            time.sleep(0.2)

    return {
        "port": port, "baud": cfg_baud, "ports": ports,
        "results": results, "recommended": None,
    }


def format_report(summary: dict) -> str:
    """Render the probe summary as a human-readable multi-line string
    suitable for both CLI output and a GUI messagebox."""
    lines = []
    lines.append(f"Port configurat:  {summary.get('port') or '(none)'}")
    lines.append(f"Baud configurat:  {summary.get('baud')}")

    # List available COM ports so the user can eyeball whether the
    # configured port even exists / looks like a Datecs.
    ports = summary.get("ports") or []
    if ports:
        lines.append("")
        lines.append("Porturi COM detectate pe PC:")
        for p in ports:
            desc = p.get("description") or p.get("product") or ""
            mfg = p.get("manufacturer") or ""
            extras = " · ".join(x for x in (desc, mfg) if x)
            lines.append(f"  • {p['device']}  {extras}")
    else:
        lines.append("  (nu s-au detectat porturi COM)")

    lines.append("")
    if "error" in summary:
        lines.append(f"Eroare: {summary['error']}")
        return "\n".join(lines)

    # Group results by baud for readable output.
    by_baud: dict = {}
    for r in summary.get("results", []):
        by_baud.setdefault(r.baud, []).append(r)

    lines.append("Rezultate probe (STATUS 0x4A):")
    for baud in by_baud:
        fp55 = next((r for r in by_baud[baud] if r.dialect == "fp55"), None)
        fp700 = next((r for r in by_baud[baud] if r.dialect == "fp700"), None)
        def mark(r):
            return "✓ OK" if r and r.ok else ("NAK" if r and r.error == "Device NAK"
                                              else (r.error if r else "—"))
        lines.append(f"  baud={baud:>6}  fp55: {mark(fp55)}   fp700: {mark(fp700)}")

    lines.append("")
    rec = summary.get("recommended")
    if rec:
        lines.append(f"✓ IMPRIMANTA RĂSPUNDE pe:  dialect={rec['dialect']}  baud={rec['baud']}")
        lines.append("")
        lines.append("Setează în 'Setări imprimantă':")
        lines.append(f"  • Baud rate      = {rec['baud']}")
        lines.append(f"  • Protocol dialect = {rec['dialect']}")
        lines.append("Apoi Save. Următorul print ar trebui să meargă.")
    else:
        # Detect "instant NAK on every combo" — a specific pattern that
        # points to an unverified/service-locked fiscal printer rather
        # than a protocol problem. If ALL probes failed with the NAK
        # error specifically (not timeout, not open failed), it's almost
        # never a baud/dialect issue — the printer is rejecting at
        # firmware level because it hasn't been activated by a
        # certified technician (ANAF fiscalization not complete).
        all_nak = bool(summary.get("results")) and all(
            (not r.ok) and r.error == "Device NAK"
            for r in summary["results"]
        )
        lines.append("✗ Nicio combinație (dialect × baud) nu a răspuns.")
        lines.append("")

        if all_nak:
            lines.append("⚠ ATENȚIE: Imprimanta răspunde cu NAK IMEDIAT la orice baud și dialect.")
            lines.append("")
            lines.append("Cauza cea mai probabilă (în experiența noastră):")
            lines.append("")
            lines.append("  ➤ CASA DE MARCAT NU E VERIFICATĂ / ACTIVATĂ")
            lines.append("     Un aparat fiscal nou sau resetat nu acceptă NICIO")
            lines.append("     comandă de la PC până când un tehnician Datecs")
            lines.append("     autorizat nu face activarea (fiscalizarea la ANAF).")
            lines.append("     Până atunci, firmware-ul respinge totul cu NAK.")
            lines.append("")
            lines.append("     Acțiune: sună tehnicianul care ți-a livrat casa")
            lines.append("     și cere VERIFICARE / ACTIVARE / FISCALIZARE.")
            lines.append("     După verificare, rulează din nou Test comunicare.")
            lines.append("")
            lines.append("Dacă ești sigur că e verificată:")
        else:
            lines.append("Verifică pe rând:")
        lines.append("  1. Stare imprimantă — ce afișează pe ecranul fizic?")
        lines.append("     • 'Conexiune PC' / 'Ready'   → stare normală, verifică restul")
        lines.append("     • 'Eroare' / 'Service'       → eroare internă, restart imprimantă")
        lines.append("     • Ecran gol / mesaje blocate → casă neverificată (vezi mai sus)")
        lines.append("  2. Port COM — e cel corect? Vezi lista de mai sus și")
        lines.append("     compară cu Device Manager → Porturi (COM & LPT).")
        lines.append("     Datecs apare de obicei ca 'Datecs Fiscal Printer'.")
        lines.append("  3. Cablul — USB/serial e conectat ferm în ambele capete?")
        lines.append("     Încearcă alt cablu USB dacă ai. Unele cabluri doar")
        lines.append("     încărcare (fără date) fac exact acest simptom.")
        lines.append("  4. Alt software blochează portul — închide:")
        lines.append("     • Datecs PrintProxy (services.msc → stop)")
        lines.append("     • Alt POS sau soft fiscal deschis în paralel")
        lines.append("     • Orice Terminal/HyperTerminal ieșit brusc")
        lines.append("  5. Bon fiscal stuck — apasă 'C' sau 'CE' pe imprimantă")
        lines.append("     de 2-3 ori, apoi oprește și pornește imprimanta.")
        lines.append("  6. Setări serial pe imprimantă (meniu fizic) —")
        lines.append("     bytesize 8, parity None, stop 1 (default peste tot).")
        lines.append("  7. Dacă ai un soft propriu Datecs (PrintView, Console,")
        lines.append("     FP Test), rulează-l — dacă NICI AL LOR nu merge,")
        lines.append("     imprimanta e în stare eronată sau neverificată.")
    return "\n".join(lines)
