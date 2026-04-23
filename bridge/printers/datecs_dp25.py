"""Datecs DP-25 real driver (FP-700 dialect).

Handles the main fiscal operations needed from 360booking:

  - print_receipt  — opens a fiscal receipt, registers items (with VAT
                     group), records a payment, closes with BF number
  - test_print     — prints a non-fiscal "Hello from 360booking" ticket
  - x_report       — X report (readout, doesn't zero the counters)
  - z_report       — Z report (end-of-day, zeros the counters)

Command codes below follow the FP-700 integrator manual. Some DP-25
firmwares renumber them — the constants below are overridable via
config so we can tweak without rebuilding the .exe when we iterate on
real hardware.

Limits of this first pass:
  - Romanian VAT groups A..F are hardcoded in _VAT_GROUP_MAP; a tenant
    with a non-default mapping can override via `config.vat_map`.
  - Payment method map covers cash/card — extend as needed.
  - Line-item description is truncated to 36 characters (DP-25 print
    width). Longer descriptions are wrapped by the device.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import FiscalPrinter, FiscalPrinterError, PrintJob, PrintResult
from .datecs_fp import DatecsFPError, DatecsFPTransport

log = logging.getLogger("bridge.dp25")


# ---- Command codes (FP-700 default) ------------------------------------

CMD_STATUS = 0x4A
CMD_OPEN_FISCAL = 0x30       # open fiscal receipt
CMD_REGISTER_ITEM = 0x31     # register item / plu
CMD_SUBTOTAL = 0x33          # subtotal
CMD_PAYMENT = 0x35           # payment
CMD_CLOSE_FISCAL = 0x38      # close fiscal receipt
CMD_OPEN_NON_FISCAL = 0x26   # open non-fiscal
CMD_PRINT_TEXT = 0x2A        # print text line
CMD_CLOSE_NON_FISCAL = 0x27  # close non-fiscal
CMD_X_REPORT = 0x45          # X report (arg "0")
CMD_Z_REPORT = 0x45          # Z report (arg "1"); same cmd, different data


# Romanian VAT groups — DP-25 firmware assigns these letters
# by default. `config.vat_map` can override.
_VAT_GROUP_MAP = {
    0.19: "A",
    0.09: "B",
    0.05: "C",
    0.00: "D",
}

# Payment method → DP-25 payment type code
# 0 = cash, 1 = check, 2 = card, 3 = voucher (firmware default)
_PAYMENT_MAP = {
    "cash": "0",
    "card": "2",
    "card_pos_manual": "2",
    "stripe": "2",
    "stripe_online": "2",
    "voucher": "3",
    "other": "0",
}


def _fmt_amount(value: float) -> str:
    """DP-25 wants amounts with 2 decimals, dot separator, no sign."""
    return f"{abs(float(value)):.2f}"


def _truncate(text: str, max_len: int = 36) -> str:
    t = (text or "").strip()
    return (t[: max_len - 1] + "…") if len(t) > max_len else t


class DatecsDP25Printer(FiscalPrinter):
    model = "datecs_dp25"

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self.serial_port = (self.config or {}).get("serial_port")
        self.serial_baud = int((self.config or {}).get("serial_baud", 9600))
        # Operator credentials (default 1/0000 on a virgin device)
        self.operator = str((self.config or {}).get("operator") or "1")
        self.operator_password = str((self.config or {}).get("operator_password") or "0000")
        self.vat_map: Dict[float, str] = (
            (self.config or {}).get("vat_map") or _VAT_GROUP_MAP
        )
        if not self.serial_port:
            raise FiscalPrinterError(
                "datecs_dp25: serial_port missing in config (e.g. 'COM3' or '/dev/ttyUSB0')"
            )
        self._transport = DatecsFPTransport(self.serial_port, self.serial_baud)

    # -- dispatch --

    def handle(self, job: PrintJob) -> PrintResult:
        try:
            self._transport.open()
            try:
                if job.kind == "test_print":
                    return self._test_print(job)
                if job.kind == "print_receipt":
                    return self._print_receipt(job)
                if job.kind == "x_report":
                    return self._x_report()
                if job.kind == "z_report":
                    return self._z_report()
                return PrintResult(success=False, error=f"Unknown job kind: {job.kind}")
            finally:
                self._transport.close()
        except DatecsFPError as exc:
            log.exception("Datecs communication error")
            return PrintResult(success=False, error=f"Datecs: {exc}")
        except FiscalPrinterError as exc:
            return PrintResult(success=False, error=str(exc))
        except Exception as exc:
            log.exception("DP-25 handler crashed")
            return PrintResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # -- test print (non-fiscal) --

    def _test_print(self, job: PrintJob) -> PrintResult:
        msg = job.payload.get("message") or "360booking test print"
        self._transport.execute(CMD_OPEN_NON_FISCAL)
        for line in (
            "=== 360booking ===",
            _truncate(msg),
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            "Bridge v0.1 — DP-25",
        ):
            self._transport.execute(CMD_PRINT_TEXT, line.encode("cp1250", errors="replace"))
        self._transport.execute(CMD_CLOSE_NON_FISCAL)
        return PrintResult(success=True, data={"kind": "test_print", "printed": True})

    # -- fiscal receipt --

    def _print_receipt(self, job: PrintJob) -> PrintResult:
        p = job.payload or {}
        items: List[Dict[str, Any]] = p.get("items") or []
        payments: List[Dict[str, Any]] = p.get("payments") or []
        if not items:
            return PrintResult(success=False, error="No items on the receipt")

        # Open fiscal: <op>,<pwd>,<till>
        open_data = f"{self.operator},{self.operator_password},1".encode("ascii")
        self._transport.execute(CMD_OPEN_FISCAL, open_data)

        # Register items.
        # Data format (FP-700): <name>\t<Tx>\t<price>\t<qty>[\t<discount>]
        for item in items:
            name = _truncate(str(item.get("name") or "Produs"))
            vat_rate = float(item.get("vat_rate", 0.19))
            vat_group = self.vat_map.get(round(vat_rate, 4), "A")
            price = _fmt_amount(item.get("unit_price") or item.get("line_total") or 0)
            qty = _fmt_amount(item.get("quantity") or 1)
            data = f"{name}\tT{vat_group}\t{price}\t{qty}".encode("cp1250", errors="replace")
            self._transport.execute(CMD_REGISTER_ITEM, data)

        # Subtotal (optional; helps printing)
        self._transport.execute(CMD_SUBTOTAL, b"")

        # Register payments. If none provided, default to one cash
        # payment for the total.
        if not payments:
            total = float(p.get("total") or sum(
                float(i.get("line_total") or 0) for i in items
            ))
            payments = [{"method": "cash", "amount": total}]

        for pay in payments:
            method = str(pay.get("method") or "cash").lower()
            code = _PAYMENT_MAP.get(method, "0")
            amount = _fmt_amount(pay.get("amount") or 0)
            data = f"{code}\t{amount}".encode("ascii")
            self._transport.execute(CMD_PAYMENT, data)

        # Close fiscal → device returns BF number in the data bytes.
        reply = self._transport.execute(CMD_CLOSE_FISCAL)
        bf_number = reply.data.decode("ascii", errors="replace").strip()

        return PrintResult(
            success=True,
            data={
                "receipt_number": bf_number,
                "fiscal_number": bf_number,
                "printed_at": datetime.now().isoformat(),
                "printer": "datecs_dp25",
                "simulated": False,
            },
        )

    def _x_report(self) -> PrintResult:
        self._transport.execute(CMD_X_REPORT, b"0")
        return PrintResult(success=True, data={"report": "X", "printed": True})

    def _z_report(self) -> PrintResult:
        self._transport.execute(CMD_Z_REPORT, b"1")
        return PrintResult(success=True, data={"report": "Z", "printed": True})
