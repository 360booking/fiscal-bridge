"""Abstract printer interface used by the bridge. Specific devices
(Datecs DP-25, simulator, etc.) implement this and get selected at
startup based on `config.printer_model`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class FiscalPrinterError(Exception):
    pass


@dataclass
class PrintJob:
    kind: str                    # "print_receipt" | "test_print" | "x_report" | "z_report"
    job_id: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PrintResult:
    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class FiscalPrinter(ABC):
    model: str = "base"

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}

    @abstractmethod
    def handle(self, job: PrintJob) -> PrintResult:
        """Execute a single job against the physical device."""
