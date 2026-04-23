"""Datecs DP-25 driver placeholder.

Phase 1 ships the simulator; this file exists so the selector can
resolve `printer_model=datecs_dp25` without crashing — it raises a
clear NotImplementedError that the bridge logs to the backend. The
real FP-700 ASCII protocol lands in Phase 2 once we can iterate
against a physical device.

References for when we implement it:
  - Datecs FP-700/FP-2000/DP-25 protocol manual (ships with the
    device; NDA PDF from Datecs sales).
  - Romanian ANAF OPANAF 2338/2019 (format bon fiscal / JE).
  - pyserial 3.5+ for the USB-serial transport.
"""
from __future__ import annotations

from .base import FiscalPrinter, FiscalPrinterError, PrintJob, PrintResult


class DatecsDP25Printer(FiscalPrinter):
    model = "datecs_dp25"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.serial_port = (self.config or {}).get("serial_port")
        self.serial_baud = int((self.config or {}).get("serial_baud", 115200))

    def handle(self, job: PrintJob) -> PrintResult:
        raise FiscalPrinterError(
            "Datecs DP-25 protocol not implemented yet — Phase 2. "
            "Switch printer_model back to 'simulator' to continue testing."
        )
