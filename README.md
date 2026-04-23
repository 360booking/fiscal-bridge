# 360booking Fiscal Bridge

Windows agent that keeps a fiscal printer connected to the 360booking
cloud. Runs on the restaurant PC, speaks to the printer over USB-
serial, and forwards print jobs received via WebSocket from
`360booking.ro`.

## How it works

```
Windows PC                               Hetzner cloud
───────────────────────                  ────────────────────
360booking-bridge.exe                    FastAPI
  ├─ outbound WSS ──────wss:──────────► /api/fiscal-bridge/agent
  │  heartbeat + job receive                │
  └─ pyserial → fiscal printer              │
                                            ▼
                                        job queue (per tenant)
```

Agent pattern: the bridge connects out to 360booking; the server never
needs to reach the restaurant's LAN. Works behind any NAT / firewall
that allows outbound HTTPS.

## Install (Windows)

1. In 360booking admin → Restaurant → Casa de marcat → **"Activează"**.
2. Note the enrollment code (format `ABCD-1234`, valid 10 minutes).
3. Download `360booking-bridge-setup.exe` from the same panel.
4. Open a terminal on the restaurant PC and run:

```
360booking-bridge-setup.exe --enroll=ABCD1234 --install --run
```

That single command:
- Claims the enrollment code and stores a permanent device token.
- Registers a Windows scheduled task so the bridge starts at user login.
- Starts the WebSocket loop immediately.

The admin panel shows `Connected ✓` within a few seconds.

## Configure the printer

First release ships the **simulator** printer — generates fake bon
fiscal numbers, useful for end-to-end testing before hardware arrives.
Switch to the real driver with:

```
360booking-bridge-setup.exe --enroll=<code> --printer=datecs_dp25
```

(Datecs DP-25 driver lands in Phase 2.)

## Development

```bash
pip install -r requirements.txt
python -m bridge --enroll=<code> --printer=simulator --run
```

## Release

Push a tag `v0.1.0`; GitHub Actions builds the .exe and publishes it
as a release asset. The 360booking server redirects
`/api/fiscal-bridge/download` to the latest release automatically.
