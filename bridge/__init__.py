"""360booking fiscal bridge — Windows agent for the restaurant PC.

Keeps a persistent WebSocket to 360booking.ro/api/fiscal-bridge/agent,
accepts print jobs, and forwards them to a locally-attached fiscal
printer (Datecs DP-25 or compatible).

Entry points:
  python -m bridge --install --enroll=F3KP7XMA  first-run installer
  python -m bridge --run                        normal service loop
"""

__version__ = "0.2.9"
