"""Printer registry — lazy factory keyed by model name.

Adding support for a new cash register =
  1. Drop a file under bridge/printers/<brand>_<model>.py that
     subclasses FiscalPrinter and implements `handle(job)`.
  2. Register it below with a short key.

No other file in the project needs to change. The WebSocket client
looks printers up by the string stored in config (`printer_model`);
the backend and admin UI pass that same string through verbatim.

Lazy import keeps the bundled .exe small-ish even after we add 10
brands — each printer's third-party deps (pyserial, hidapi, etc.)
only load when that model is selected at runtime.
"""
from __future__ import annotations

import importlib
from typing import Dict, Optional

from .base import FiscalPrinter


# (model_key → "module:ClassName"). Lowercase keys; the selector
# normalises user input. Keep entries alphabetical within a brand.
REGISTRY: Dict[str, str] = {
    # Dev / demo
    "simulator": "bridge.printers.simulator:SimulatorPrinter",

    # Datecs
    "datecs_dp25": "bridge.printers.datecs_dp25:DatecsDP25Printer",
    # Future Datecs models (uncomment when implemented):
    # "datecs_dp55":   "bridge.printers.datecs_dp55:DatecsDP55Printer",
    # "datecs_fp550":  "bridge.printers.datecs_fp550:DatecsFP550Printer",
    # "datecs_fmp10":  "bridge.printers.datecs_fmp10:DatecsFMP10Printer",

    # Other Romanian brands (placeholders, see README for how to add):
    # "tremol_zfp1000":"bridge.printers.tremol_zfp1000:TremolZFP1000Printer",
    # "eltrade_b1":    "bridge.printers.eltrade_b1:EltradeB1Printer",
    # "partner_xplorer":"bridge.printers.partner_xplorer:PartnerXplorerPrinter",
    # "activa_jupiter":"bridge.printers.activa_jupiter:ActivaJupiterPrinter",
}


def available_models() -> list[str]:
    return sorted(REGISTRY.keys())


def build(model: Optional[str], config: Optional[dict] = None) -> FiscalPrinter:
    """Resolve the printer class for `model` and instantiate it.

    Raises KeyError for unknown models so the bridge fails loudly at
    startup rather than silently printing to the wrong device.
    """
    key = (model or "simulator").strip().lower()
    if key not in REGISTRY:
        raise KeyError(
            f"Unknown printer model: {key!r}. Available: {', '.join(available_models())}"
        )
    dotted = REGISTRY[key]
    module_path, _, class_name = dotted.partition(":")
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls(config=config or {})
