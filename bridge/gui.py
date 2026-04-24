"""Tkinter GUI — appears when the .exe is double-clicked without args.

Two states:
  1. Not enrolled → form with code, printer model, optional COM port
  2. Enrolled → status + start/stop/uninstall/re-enroll buttons

Uses only stdlib (tkinter). PyInstaller bundles it by default on
Windows runners.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from . import __version__
from . import status as status_file
from .config import BridgeConfig, config_dir
from .printers import available_models, implemented_models, planned_models


TASK_NAME = "360bookingFiscalBridge"
WINDOW_TITLE = "360booking Fiscal Bridge"


# ------------------------------ helpers ----------------------------------

def _is_windows() -> bool:
    return platform.system() == "Windows"


def _task_state() -> str:
    """Return 'running' / 'ready' / 'missing' for the scheduled task."""
    if not _is_windows():
        return "unknown"
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return "missing"
        # Output CSV: "task","next_run","status"
        parts = r.stdout.strip().strip('"').split('","')
        status = parts[-1] if parts else ""
        return "running" if status == "Running" else "ready"
    except Exception:
        return "unknown"


def _bridge_process_running() -> bool:
    """Quick check via tasklist for any 360booking-bridge.exe process."""
    if not _is_windows():
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq 360booking-bridge-setup.exe"],
            capture_output=True, text=True, timeout=5,
        )
        return "360booking-bridge-setup.exe" in (r.stdout or "")
    except Exception:
        return False


def _claim(code: str, printer_model: str, server: str) -> dict:
    payload = {
        "code": code.strip().upper().replace("-", ""),
        "printer_model": printer_model,
        "version": __version__,
        "os_info": f"{platform.system()} {platform.release()}",
    }
    req = urlrequest.Request(
        f"{server.rstrip('/')}/api/fiscal-bridge/claim",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------ enrollment form -------------------------

class EnrollForm:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._build()

    def _build(self) -> None:
        f = ttk.Frame(self.root, padding=24)
        f.grid(row=0, column=0, sticky="nsew")

        ttk.Label(f, text="Activare 360booking Fiscal Bridge",
                  style="Header.TLabel").grid(row=0, column=0, columnspan=2, pady=(0, 4))
        ttk.Label(f, text=f"v{__version__}  ·  Bridge Windows pentru casa de marcat fiscală",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=2, pady=(0, 12))
        ttk.Label(f, text="Generează codul din 360booking → Restaurant → Setări fiscale → Activează",
                  foreground="#444").grid(row=2, column=0, columnspan=2, pady=(0, 20))

        ttk.Label(f, text="Cod de activare:").grid(row=2, column=0, sticky="w", pady=4)
        self.code = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.code, font=("Consolas", 12), width=28)
        e.grid(row=2, column=1, sticky="we", pady=4)
        e.focus_set()

        ttk.Label(f, text="Imprimantă fiscală:").grid(row=3, column=0, sticky="w", pady=4)
        self.printer = tk.StringVar(value="simulator")
        ttk.Combobox(f, textvariable=self.printer, values=available_models(),
                     state="readonly", width=26).grid(row=3, column=1, sticky="we", pady=4)

        ttk.Label(f, text="Port COM (opțional):").grid(row=4, column=0, sticky="w", pady=4)
        self.com = tk.StringVar()
        ttk.Entry(f, textvariable=self.com, width=28,
                  font=("Consolas", 11)).grid(row=4, column=1, sticky="we", pady=4)
        ttk.Label(f, text="ex. COM3 (doar pentru Datecs DP-25)",
                  foreground="#666", font=("Segoe UI", 8)).grid(row=5, column=1, sticky="w")

        self.autorun = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, variable=self.autorun,
                        text="Pornește automat la login Windows").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(16, 4))

        self.background = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, variable=self.background,
                        text="Pornește imediat în background (fără fereastră)").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(0, 16))

        btn_bar = ttk.Frame(f)
        btn_bar.grid(row=8, column=0, columnspan=2, sticky="e")
        ttk.Button(btn_bar, text="Anulează", command=self.root.quit).grid(row=0, column=0, padx=4)
        self.install_btn = ttk.Button(btn_bar, text="Activează", command=self._submit)
        self.install_btn.grid(row=0, column=1, padx=4)

        self.status = tk.StringVar(value="Aștept codul…")
        ttk.Label(f, textvariable=self.status, foreground="#444").grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(12, 0))

        f.columnconfigure(1, weight=1)

    def _submit(self) -> None:
        code = self.code.get().strip().upper().replace("-", "")
        if len(code) != 8:
            messagebox.showerror(WINDOW_TITLE, "Codul trebuie să aibă 8 caractere (ex. F3KP7XMA).")
            return
        self.install_btn.config(state="disabled")
        self.status.set("Se validează codul cu 360booking.ro…")
        threading.Thread(target=self._do_enroll, daemon=True).start()

    def _do_enroll(self) -> None:
        try:
            data = _claim(self.code.get(), self.printer.get(), "https://360booking.ro")
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            err = f"HTTP {exc.code}: {body[:200]}"
            self.root.after(0, lambda: self._fail(err))
            return
        except Exception as exc:
            self.root.after(0, lambda: self._fail(str(exc)))
            return

        cfg = BridgeConfig.load()
        cfg.device_token = data["device_token"]
        cfg.tenant_id = data["tenant_id"]
        cfg.bridge_id = data["bridge_id"]
        cfg.websocket_url = data["websocket_url"]
        cfg.printer_model = self.printer.get()
        if self.com.get().strip():
            cfg.serial_port = self.com.get().strip()
        cfg.save()

        self.root.after(0, lambda: self.status.set("Cod validat. Se configurează auto-start…"))

        if self.autorun.get():
            try:
                # Mark this call path so _install_autorun() doesn't
                # sys.exit on UAC elevation — that would silently kill
                # the GUI thread and leave the form stuck.
                import os as _os
                _os.environ["FB_FROM_GUI"] = "1"
                from .main import _install_autorun
                try:
                    _install_autorun()
                except SystemExit:
                    pass
                finally:
                    _os.environ.pop("FB_FROM_GUI", None)
            except Exception as exc:
                self.root.after(0, lambda e=exc: self.status.set(f"Auto-start a eșuat: {e}"))

        if self.background.get():
            try:
                from .main import _start_hidden_now
                try:
                    _start_hidden_now()
                except SystemExit:
                    pass
            except Exception as exc:
                self.root.after(0, lambda e=exc: self.status.set(f"Pornire background a eșuat: {e}"))

        self.root.after(0, lambda: self._done(data))

    def _fail(self, msg: str) -> None:
        self.status.set("")
        self.install_btn.config(state="normal")
        if "404" in msg or "410" in msg:
            messagebox.showerror(
                WINDOW_TITLE,
                "Codul este invalid sau a expirat (valabil 10 minute).\n\n"
                "Generează unul nou din 360booking → Setări fiscale → Activează.",
            )
        else:
            messagebox.showerror(WINDOW_TITLE, f"Enrolment eșuat:\n\n{msg}")

    def _done(self, data: dict) -> None:
        messagebox.showinfo(
            WINDOW_TITLE,
            "Bridge-ul a fost activat cu succes.\n\n"
            f"Bridge ID: {data['bridge_id'][:12]}…\n"
            f"Tenant:    {data['tenant_id'][:12]}…\n\n"
            'Panoul 360booking va afisa starea "Conectat" in cateva secunde.',
        )
        self.root.after(100, self._switch_to_status)

    def _switch_to_status(self) -> None:
        for w in self.root.winfo_children():
            w.destroy()
        StatusPanel(self.root)


# ------------------------------ status panel ----------------------------

class StatusPanel:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._build()
        self.root.after(2000, self._refresh)

    def _build(self) -> None:
        self.cfg = BridgeConfig.load()
        cfg = self.cfg
        f = ttk.Frame(self.root, padding=20)
        f.grid(row=0, column=0, sticky="nsew")

        # --- Header: app name + version on right ---
        hdr = ttk.Frame(f)
        hdr.grid(row=0, column=0, columnspan=2, sticky="we", pady=(0, 14))
        ttk.Label(hdr, text="360booking Fiscal Bridge",
                  style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(hdr, text=f"v{__version__}",
                  style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=(12, 0))
        hdr.columnconfigure(0, weight=1)

        # Live indicators — three rows, each with a colored dot + label.
        live_box = ttk.LabelFrame(f, text=" Stare ", padding=12)
        live_box.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 12))

        self.dot_process = tk.StringVar(value="●")
        self.dot_ws = tk.StringVar(value="●")
        self.dot_printer = tk.StringVar(value="●")
        self.lbl_process = tk.StringVar(value="Proces: verific…")
        self.lbl_ws = tk.StringVar(value="Conexiune 360booking: verific…")
        self.lbl_printer = tk.StringVar(value="Casa de marcat: verific…")

        def _dot_line(row: int, dot_var: tk.StringVar, lbl_var: tk.StringVar):
            ttk.Label(live_box, textvariable=dot_var, font=("Segoe UI", 12, "bold"),
                      foreground="#999", width=2).grid(row=row, column=0, sticky="w")
            ttk.Label(live_box, textvariable=lbl_var).grid(row=row, column=1, sticky="w")

        _dot_line(0, self.dot_process, self.lbl_process)
        _dot_line(1, self.dot_ws, self.lbl_ws)
        _dot_line(2, self.dot_printer, self.lbl_printer)
        self._dots = {
            "process": (self.dot_process, None),
            "ws": (self.dot_ws, None),
            "printer": (self.dot_printer, None),
        }

        # Detail box grouping tenant + printer info
        info_box = ttk.LabelFrame(f, text=" Detalii conexiune ", padding=12)
        info_box.grid(row=2, column=0, columnspan=2, sticky="we", pady=(0, 8))

        rows = [
            ("Bridge ID", (cfg.bridge_id or "")[:24] + "…" if cfg.bridge_id else "(not set)"),
            ("Tenant", (cfg.tenant_id or "")[:24] + "…" if cfg.tenant_id else "(not set)"),
            ("Imprimantă", cfg.printer_model or "simulator"),
            ("Port COM", cfg.serial_port or "(nu e setat)"),
            ("Baud rate", str(cfg.serial_baud or 9600)),
        ]
        for i, (label, value) in enumerate(rows):
            ttk.Label(info_box, text=label + ":", foreground="#555").grid(row=i, column=0, sticky="w", pady=2, padx=(0, 12))
            ttk.Label(info_box, text=value, font=("Consolas", 9)).grid(row=i, column=1, sticky="w", pady=2)
        info_box.columnconfigure(1, weight=1)

        # Log file location (for support)
        log_box = ttk.Frame(f)
        log_box.grid(row=3, column=0, columnspan=2, sticky="we", pady=(4, 0))
        ttk.Label(log_box, text="Log:", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(log_box, text=str(config_dir() / "bridge.log"),
                  font=("Consolas", 8), foreground="#666").grid(row=0, column=1, sticky="w", padx=(4, 0))

        # --- Main actions (primary buttons) ---
        actions = ttk.LabelFrame(f, text=" Acțiuni ", padding=12)
        actions.grid(row=4, column=0, columnspan=2, sticky="we", pady=(12, 0))

        ttk.Button(actions, text="🖨  Setări imprimantă",
                   command=self._edit_printer_config, style="Accent.TButton",
                   width=22).grid(row=0, column=0, padx=4, pady=4, sticky="we")
        ttk.Button(actions, text="▶  Pornește",
                   command=self._start, width=22).grid(row=0, column=1, padx=4, pady=4, sticky="we")
        ttk.Button(actions, text="■  Oprește",
                   command=self._stop, width=22).grid(row=0, column=2, padx=4, pady=4, sticky="we")

        ttk.Button(actions, text="📄  Deschide log",
                   command=self._open_log, width=22).grid(row=1, column=0, padx=4, pady=4, sticky="we")
        ttk.Button(actions, text="⟳  Reactivează (re-enroll)",
                   command=self._reenroll, width=22).grid(row=1, column=1, padx=4, pady=4, sticky="we")
        ttk.Button(actions, text="🗑  Dezinstalează",
                   command=self._uninstall, width=22).grid(row=1, column=2, padx=4, pady=4, sticky="we")

        ttk.Button(actions, text="ℹ  Despre",
                   command=self._show_about, width=22).grid(row=2, column=1, padx=4, pady=4, sticky="we")

        for col in range(3):
            actions.columnconfigure(col, weight=1)

    def _edit_printer_config(self) -> None:
        """Dialog to edit printer_model / serial_port / serial_baud
        without touching config.json by hand. Writes JSON without a
        BOM so Python's json.loads doesn't choke on reload."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Setări imprimantă")
        dlg.geometry("460x360")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        cfg = self.cfg

        # --- Current configuration status banner (above the form) ---
        is_configured = bool(cfg.printer_model and cfg.printer_model != "simulator" and cfg.serial_port)
        is_simulator = cfg.printer_model == "simulator"
        banner = ttk.Frame(dlg, padding=(16, 12, 16, 4))
        banner.grid(row=0, column=0, sticky="we")
        if is_configured:
            ttk.Label(banner,
                      text=f"✓ Casa de marcat: configurată — {cfg.printer_model} pe {cfg.serial_port} @ {cfg.serial_baud}",
                      foreground="#1f883d", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        elif is_simulator:
            ttk.Label(banner,
                      text="⚠ Rulează în simulator — selectează modelul real + port COM mai jos.",
                      foreground="#9a6700", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        else:
            ttk.Label(banner,
                      text="✗ Casa de marcat: NU este configurată — completează câmpurile de mai jos.",
                      foreground="#cf222e", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        f = ttk.Frame(dlg, padding=16)
        f.grid(row=1, column=0, sticky="nsew")

        # --- Model ---
        ttk.Label(f, text="Model imprimantă:").grid(row=0, column=0, sticky="w", pady=4)
        model_var = tk.StringVar(value=cfg.printer_model or "simulator")
        ttk.Combobox(f, textvariable=model_var, values=available_models(),
                     state="readonly", width=28).grid(row=0, column=1, sticky="we", pady=4)

        # --- COM port with scan button ---
        ttk.Label(f, text="Port COM:").grid(row=1, column=0, sticky="w", pady=4)
        com_var = tk.StringVar(value=cfg.serial_port or "")
        com_combo = ttk.Combobox(f, textvariable=com_var, width=26)
        com_combo.grid(row=1, column=1, sticky="we", pady=4)

        def _scan_ports():
            try:
                import serial.tools.list_ports
                ports = [p.device for p in serial.tools.list_ports.comports()]
                com_combo["values"] = ports
                if ports and not com_var.get():
                    com_var.set(ports[0])
                messagebox.showinfo(WINDOW_TITLE,
                                    f"Porturi detectate: {', '.join(ports) if ports else '(niciunul)'}")
            except Exception as exc:
                messagebox.showerror(WINDOW_TITLE, f"Scanare eșuată: {exc}")

        ttk.Button(f, text="↻ Scanează", command=_scan_ports, width=12).grid(row=1, column=2, padx=4, pady=4)

        # Pre-populate the dropdown with whatever is currently attached
        try:
            import serial.tools.list_ports
            com_combo["values"] = [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            pass

        # --- Baud ---
        ttk.Label(f, text="Baud rate:").grid(row=2, column=0, sticky="w", pady=4)
        baud_var = tk.StringVar(value=str(cfg.serial_baud or 9600))
        ttk.Combobox(f, textvariable=baud_var,
                     values=["4800", "9600", "19200", "38400", "57600", "115200"],
                     state="readonly", width=28).grid(row=2, column=1, sticky="we", pady=4)

        ttk.Label(f, text="DP-25 folosește 9600 implicit.",
                  foreground="#666", font=("Segoe UI", 8)).grid(row=3, column=1, sticky="w")

        # --- Separator ---
        ttk.Separator(f, orient="horizontal").grid(row=4, column=0, columnspan=3, sticky="we", pady=12)
        ttk.Label(f, text="Setări avansate (Datecs)",
                  font=("Segoe UI", 9, "bold")).grid(row=5, column=0, columnspan=3, sticky="w")

        # --- Operator credentials ---
        ttk.Label(f, text="Operator ID:").grid(row=6, column=0, sticky="w", pady=4)
        op_var = tk.StringVar(value=str(getattr(cfg, "operator", "1") or "1"))
        ttk.Entry(f, textvariable=op_var, width=28).grid(row=6, column=1, sticky="we", pady=4)

        ttk.Label(f, text="Operator parolă:").grid(row=7, column=0, sticky="w", pady=4)
        pw_var = tk.StringVar(value=str(getattr(cfg, "operator_password", "0000") or "0000"))
        ttk.Entry(f, textvariable=pw_var, width=28).grid(row=7, column=1, sticky="we", pady=4)

        # --- Save / Cancel ---
        status_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=status_var, foreground="#444").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(12, 0))

        btns = ttk.Frame(f)
        btns.grid(row=9, column=0, columnspan=3, sticky="e", pady=(12, 0))

        def _save():
            try:
                # Write config without BOM.
                import json
                data = {
                    "device_token": cfg.device_token,
                    "tenant_id": cfg.tenant_id,
                    "bridge_id": cfg.bridge_id,
                    "websocket_url": cfg.websocket_url,
                    "printer_model": model_var.get().strip() or "simulator",
                    "health_port": getattr(cfg, "health_port", 17890),
                    "server_base_url": cfg.server_base_url,
                    "serial_port": com_var.get().strip() or None,
                    "serial_baud": int(baud_var.get() or 9600),
                }
                # Keep extra fields on BridgeConfig if any were set before.
                for extra in ("operator", "operator_password"):
                    val = {"operator": op_var.get(), "operator_password": pw_var.get()}[extra]
                    if val:
                        data[extra] = val

                from .config import config_path
                p = config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                # Explicit utf-8 (no BOM) + deterministic key order
                p.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")
                dlg.destroy()
                messagebox.showinfo(
                    WINDOW_TITLE,
                    "Setările au fost salvate.\n\n"
                    "Bridge-ul se va reconecta automat cu noile setări "
                    "(opreste-l și pornește-l din tray dacă nu se actualizează).",
                )
                # Refresh the cfg we hold so the status panel shows the new values
                self.cfg = BridgeConfig.load()
            except Exception as exc:
                status_var.set(f"Eroare: {exc}")

        ttk.Button(btns, text="Anulează", command=dlg.destroy).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="Salvează", command=_save).grid(row=0, column=1, padx=4)

        f.columnconfigure(1, weight=1)

    @staticmethod
    def _set_dot(var: tk.StringVar, lbl_var: tk.StringVar,
                 ok: Optional[bool], text: str,
                 dot_label: Optional[ttk.Label] = None) -> None:
        # ok=True → green, False → red, None → gray (unknown)
        color = "#2e7d32" if ok else ("#c62828" if ok is False else "#999")
        mark = "●"
        var.set(mark)
        lbl_var.set(text)
        # Color is set by re-styling the label — tkinter doesn't let us
        # change Label foreground via StringVar, so we keep the label
        # gray and encode state in unicode: green ●, red ✗, gray ○.
        if ok is True:
            var.set("●")
        elif ok is False:
            var.set("✗")
        else:
            var.set("○")

    def _refresh(self) -> None:
        running_proc = _bridge_process_running()
        task = _task_state()
        stat = status_file.read()

        # --- Process indicator ---
        if running_proc:
            self._set_dot(self.dot_process, self.lbl_process, True,
                          "Proces: rulează")
        elif task == "ready":
            self._set_dot(self.dot_process, self.lbl_process, None,
                          'Proces: oprit (instalat, pornește la login)')
        else:
            self._set_dot(self.dot_process, self.lbl_process, False,
                          "Proces: oprit")

        # --- WebSocket / server indicator ---
        if not running_proc:
            self._set_dot(self.dot_ws, self.lbl_ws, None,
                          "Conexiune 360booking: — (proces oprit)")
        elif stat and not stat.get("stale") and stat.get("ws_connected"):
            self._set_dot(self.dot_ws, self.lbl_ws, True,
                          "Conexiune 360booking: activă")
        else:
            detail = stat.get("last_error") if stat else "fără răspuns"
            self._set_dot(self.dot_ws, self.lbl_ws, False,
                          f"Conexiune 360booking: picată ({detail or 'timeout'})")

        # --- Printer indicator ---
        if not running_proc:
            self._set_dot(self.dot_printer, self.lbl_printer, None,
                          "Casa de marcat: — (proces oprit)")
        elif stat and not stat.get("stale"):
            ps = stat.get("printer_status")
            pd = stat.get("printer_detail") or ""
            if ps == "ok":
                self._set_dot(self.dot_printer, self.lbl_printer, True,
                              f"Casa de marcat: conectată ({pd})")
            elif ps == "not_configured":
                self._set_dot(self.dot_printer, self.lbl_printer, None,
                              "Casa de marcat: neconfigurată (adaugă port COM)")
            else:
                self._set_dot(self.dot_printer, self.lbl_printer, False,
                              f"Casa de marcat: eroare ({pd})")
        else:
            self._set_dot(self.dot_printer, self.lbl_printer, None,
                          "Casa de marcat: status indisponibil")

        self.root.after(2000, self._refresh)

    def _start(self) -> None:
        try:
            from .main import _start_hidden_now
            _start_hidden_now()
            messagebox.showinfo(WINDOW_TITLE, "Bridge-ul pornit în background.")
        except Exception as exc:
            messagebox.showerror(WINDOW_TITLE, f"Nu s-a putut porni: {exc}")

    def _stop(self) -> None:
        if not _is_windows():
            return
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "360booking-bridge-setup.exe", "/T"],
                capture_output=True,
            )
            subprocess.run(
                ["taskkill", "/F", "/IM", "360booking-bridge.exe", "/T"],
                capture_output=True,
            )
            messagebox.showinfo(WINDOW_TITLE, "Bridge-ul a fost oprit.")
        except Exception as exc:
            messagebox.showerror(WINDOW_TITLE, f"Nu s-a putut opri: {exc}")

    def _open_log(self) -> None:
        log = config_dir() / "bridge.log"
        if not log.exists():
            messagebox.showinfo(WINDOW_TITLE, "Log-ul nu există încă — pornește bridge-ul o dată.")
            return
        if _is_windows():
            subprocess.Popen(["notepad", str(log)])
        else:
            subprocess.Popen(["xdg-open", str(log)])

    def _fetch_latest_version(self, latest_var: tk.StringVar, status_var: tk.StringVar) -> None:
        """Background probe to GitHub Releases — fills latest_var with the
        tag name or an error note. Runs in a thread so the About dialog
        isn't blocked by network latency."""
        import json as _json
        try:
            req = urlrequest.Request(
                "https://api.github.com/repos/360booking/fiscal-bridge/releases/latest",
                headers={"Accept": "application/json"},
            )
            with urlrequest.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
            tag = (data.get("tag_name") or "").lstrip("v")
            if not tag:
                self.root.after(0, lambda: latest_var.set("(necunoscut)"))
                return
            # Simple version comparison — tuples of ints for stable sort.
            def _parse(v: str) -> tuple:
                try:
                    return tuple(int(p) for p in v.split("."))
                except Exception:
                    return (0,)
            here = _parse(__version__)
            remote = _parse(tag)
            if remote > here:
                msg = f"v{tag}   ⚠ actualizare disponibilă"
                self.root.after(0, lambda m=msg: latest_var.set(m))
                self.root.after(0, lambda: status_var.set("update-available"))
            elif remote == here:
                self.root.after(0, lambda t=tag: latest_var.set(f"v{t}   ✓ la zi"))
                self.root.after(0, lambda: status_var.set("up-to-date"))
            else:
                self.root.after(0, lambda t=tag: latest_var.set(f"v{t} (ai o versiune mai nouă)"))
                self.root.after(0, lambda: status_var.set("ahead"))
        except Exception as exc:
            self.root.after(0, lambda e=exc: latest_var.set(f"(nu s-a putut verifica: {e})"))

    def _show_about(self) -> None:
        """Modal dialog with app info, version (current + latest available),
        configuration status, and the list of supported cash registers."""
        cfg = self.cfg
        dlg = tk.Toplevel(self.root)
        dlg.title("Despre — " + WINDOW_TITLE)
        dlg.geometry("620x620")
        dlg.resizable(False, True)
        dlg.transient(self.root)
        dlg.grab_set()

        f = ttk.Frame(dlg, padding=18)
        f.pack(fill="both", expand=True)

        # --- Header ---
        ttk.Label(f, text="360booking Fiscal Bridge",
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(f, text="Agent Windows pentru casa de marcat fiscală",
                  foreground="#555").pack(anchor="w", pady=(0, 10))

        # --- Version block ---
        ver_box = ttk.LabelFrame(f, text=" Versiune ", padding=10)
        ver_box.pack(fill="x", pady=(0, 10))
        ttk.Label(ver_box, text="Instalată:", foreground="#555",
                  width=16).grid(row=0, column=0, sticky="w")
        ttk.Label(ver_box, text=f"v{__version__}",
                  font=("Consolas", 10, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(ver_box, text="Ultima disponibilă:", foreground="#555",
                  width=16).grid(row=1, column=0, sticky="w", pady=(4, 0))
        latest_var = tk.StringVar(value="verific…")
        latest_status = tk.StringVar(value="checking")
        ttk.Label(ver_box, textvariable=latest_var,
                  font=("Consolas", 10)).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ver_box.columnconfigure(1, weight=1)

        # Kick off the GitHub check in a background thread
        threading.Thread(
            target=self._fetch_latest_version,
            args=(latest_var, latest_status),
            daemon=True,
        ).start()

        # --- Configuration status ---
        cfg_box = ttk.LabelFrame(f, text=" Configurare ", padding=10)
        cfg_box.pack(fill="x", pady=(0, 10))

        def _row(r, label, value, ok):
            color = "#1f883d" if ok is True else ("#cf222e" if ok is False else "#888")
            symbol = "✓" if ok is True else ("✗" if ok is False else "—")
            ttk.Label(cfg_box, text=label + ":", foreground="#555", width=22
                      ).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Label(cfg_box, text=f"{symbol}  {value}", foreground=color
                      ).grid(row=r, column=1, sticky="w", pady=2)

        enrolled = bool(cfg.device_token and cfg.bridge_id)
        _row(0, "Activat (enroll)", "da" if enrolled else "NU — rulează enrollment", enrolled)

        model_ok = bool(cfg.printer_model) and cfg.printer_model != "simulator"
        _row(1, "Model imprimantă",
             cfg.printer_model or "(nu e setat)",
             True if model_ok else (None if cfg.printer_model == "simulator" else False))

        port_ok = bool(cfg.serial_port)
        _row(2, "Port COM",
             cfg.serial_port or "(nu e setat)",
             True if port_ok else (None if cfg.printer_model == "simulator" else False))

        op_ok = bool(getattr(cfg, "operator", None)) and bool(getattr(cfg, "operator_password", None))
        _row(3, "Operator + parolă",
             f"id={getattr(cfg, 'operator', '—')}  pw={'*' * len(getattr(cfg, 'operator_password', '') or '')}"
                 if op_ok else "(nu e setat)",
             op_ok)

        # Overall status line
        overall_ok = enrolled and model_ok and port_ok
        sim_mode = cfg.printer_model == "simulator"
        summary_color = "#1f883d" if overall_ok else ("#9a6700" if sim_mode else "#cf222e")
        summary_text = (
            "✓ Casa de marcat este configurată" if overall_ok
            else ("⚠ Rulez în simulator — nu se printează real"
                  if sim_mode else "✗ Casa de marcat NU este complet configurată")
        )
        ttk.Label(cfg_box, text=summary_text, foreground=summary_color,
                  font=("Segoe UI", 10, "bold")).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        cfg_box.columnconfigure(1, weight=1)

        # --- Supported printers ---
        pr_box = ttk.LabelFrame(f, text=" Case de marcat suportate ", padding=6)
        pr_box.pack(fill="both", expand=True, pady=(0, 10))

        cols = ("brand", "model", "status", "note")
        tree = ttk.Treeview(pr_box, columns=cols, show="headings", height=9)
        tree.heading("brand", text="Brand")
        tree.heading("model", text="Model")
        tree.heading("status", text="Stare")
        tree.heading("note", text="Note")
        tree.column("brand", width=80, anchor="w")
        tree.column("model", width=110, anchor="w")
        tree.column("status", width=90, anchor="w")
        tree.column("note", width=280, anchor="w")

        for m in implemented_models():
            tree.insert("", "end", values=(
                m["brand"], m["model"], "✓ suportată", m["note"],
            ))
        for m in planned_models():
            tree.insert("", "end", values=(
                m["brand"], m["model"], "planificată", m["note"],
            ))

        sb = ttk.Scrollbar(pr_box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        ttk.Label(f, text="Dacă ai o casă de marcat care nu apare aici, contactează 360booking.",
                  foreground="#666", font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 4))

        # --- Footer ---
        foot = ttk.Frame(f)
        foot.pack(fill="x")
        ttk.Label(foot, text="© 360booking.ro",
                  foreground="#666").pack(side="left")
        ttk.Button(foot, text="Închide", command=dlg.destroy, width=14
                   ).pack(side="right")

    def _uninstall(self) -> None:
        if not messagebox.askyesno(
            WINDOW_TITLE,
            "Ești sigur? Această acțiune:\n"
            "  • Oprește bridge-ul\n"
            "  • Elimină auto-start la login\n"
            "  • Șterge config-ul (token, tenant, bridge ID)\n\n"
            "Vei avea nevoie de un cod nou de activare după.",
        ):
            return
        self._stop()
        if _is_windows():
            subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                capture_output=True,
            )
        try:
            (config_dir() / "config.json").unlink(missing_ok=True)
        except Exception:
            pass
        messagebox.showinfo(WINDOW_TITLE, "Dezinstalare completă.")
        self._switch_to_enroll()

    def _reenroll(self) -> None:
        if not messagebox.askyesno(
            WINDOW_TITLE,
            "Asta va șterge configurația curentă și va cere un cod nou.\n\nContinui?",
        ):
            return
        try:
            (config_dir() / "config.json").unlink(missing_ok=True)
        except Exception:
            pass
        self._switch_to_enroll()

    def _switch_to_enroll(self) -> None:
        for w in self.root.winfo_children():
            w.destroy()
        EnrollForm(self.root)


# ------------------------------ entry point -----------------------------

def _style_setup(root: tk.Tk) -> None:
    """Global ttk styling — pick a modern theme + beef up spacing so
    the GUI doesn't look like a Windows 95 tool."""
    style = ttk.Style()
    try:
        style.theme_use("vista" if _is_windows() else "clam")
    except tk.TclError:
        pass
    # Bigger default padding on buttons, consistent fonts.
    style.configure("TButton", padding=(10, 6))
    style.configure("Accent.TButton", padding=(12, 6), font=("Segoe UI", 9, "bold"))
    style.configure("TLabel", font=("Segoe UI", 9))
    style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"))
    style.configure("Muted.TLabel", foreground="#666", font=("Segoe UI", 8))
    style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
    style.configure("TLabelFrame.Label", font=("Segoe UI", 9, "bold"))


def run_gui() -> int:
    root = tk.Tk()
    root.title(WINDOW_TITLE)
    root.geometry("620x560")
    root.resizable(False, False)

    _style_setup(root)

    cfg = BridgeConfig.load()
    if cfg.is_claimed():
        StatusPanel(root)
    else:
        EnrollForm(root)

    root.mainloop()
    return 0
