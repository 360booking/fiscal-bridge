"""Simulator printer — returns fake BF numbers so the end-to-end
flow can be tested without physical hardware. Logs each receipt to
~/360booking-bridge-receipts/ for inspection."""
from __future__ import annotations

import json
import time
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Iterator

from ..config import config_dir
from .base import FiscalPrinter, PrintJob, PrintResult


class SimulatorPrinter(FiscalPrinter):
    model = "simulator"

    _counter: Iterator[int] = count(1)

    def handle(self, job: PrintJob) -> PrintResult:
        # Pretend a printer round-trip. Real hardware takes 2–4 seconds.
        time.sleep(0.25)

        if job.kind == "test_print":
            return PrintResult(
                success=True,
                data={"message": job.payload.get("message", "test"), "simulated": True},
            )

        if job.kind == "x_report":
            return PrintResult(success=True, data={"report": "X-REPORT simulated", "total": "0.00"})

        if job.kind == "z_report":
            return PrintResult(success=True, data={"report": "Z-REPORT simulated", "total": "0.00"})

        if job.kind == "print_receipt":
            n = next(self._counter)
            receipt_no = f"BF-sim-{n:06d}"
            fiscal_no = f"F{int(time.time())}{n:04d}"
            self._spool(job.job_id, job.payload, receipt_no)
            return PrintResult(
                success=True,
                data={
                    "receipt_number": receipt_no,
                    "fiscal_number": fiscal_no,
                    "printed_at": datetime.now().isoformat(),
                    "simulated": True,
                },
            )

        return PrintResult(success=False, error=f"Unknown job kind: {job.kind}")

    def _spool(self, job_id: str, payload: dict, receipt_no: str) -> None:
        spool = config_dir() / "receipts"
        spool.mkdir(parents=True, exist_ok=True)
        fn = spool / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{receipt_no}.json"
        fn.write_text(json.dumps({"job_id": job_id, "payload": payload}, indent=2), encoding="utf-8")
