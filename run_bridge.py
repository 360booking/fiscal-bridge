"""Top-level entry point for PyInstaller.

Kept outside the `bridge` package so PyInstaller doesn't hit the
"attempted relative import with no known parent package" error that
strikes when `bridge/__main__.py` is used as the frozen entry script.
Everything real lives in bridge.main.main().
"""
from __future__ import annotations

import sys

from bridge.main import main


if __name__ == "__main__":
    sys.exit(main())
