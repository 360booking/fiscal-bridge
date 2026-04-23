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

from . import __version__, status as status_file
from .config import BridgeConfig, config_dir
from .printers import available_models


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
        f = ttk.Frame(self.root, padding=20)
        f.grid(row=0, column=0, sticky="nsew")

        ttk.Label(f, text="Activare 360booking Fiscal Bridge",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 6))
        ttk.Label(f, text="Generează codul din panoul 360booking → Setări fiscale → Activează",
                  foreground="#666").grid(row=1, column=0, columnspan=2, pady=(0, 16))

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
                from .main import _install_autorun
                _install_autorun()
            except Exception as exc:
                self.root.after(0, lambda e=exc: self.status.set(f"Auto-start a eșuat: {e}"))

        if self.background.get():
            try:
                from .main import _start_hidden_now
                _start_hidden_now()
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
        cfg = BridgeConfig.load()
        f = ttk.Frame(self.root, padding=20)
        f.grid(row=0, column=0, sticky="nsew")

        ttk.Label(f, text="360booking Fiscal Bridge",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 12))

        # Live indicators — three rows, each with a colored dot + label.
        live_box = ttk.LabelFrame(f, text=" Stare ", padding=10)
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

        rows = [
            ("Bridge ID:", (cfg.bridge_id or "")[:24] + "…" if cfg.bridge_id else "(not set)"),
            ("Tenant:", (cfg.tenant_id or "")[:24] + "…" if cfg.tenant_id else "(not set)"),
            ("Imprimantă:", cfg.printer_model or "simulator"),
            ("Port COM:", cfg.serial_port or "(nu e setat)"),
            ("Versiune bridge:", __version__),
            ("Log file:", str(config_dir() / "bridge.log")),
        ]
        for i, (label, value) in enumerate(rows, start=2):
            ttk.Label(f, text=label, foreground="#444").grid(row=i, column=0, sticky="w", pady=2)
            ttk.Label(f, text=value, font=("Consolas", 9)).grid(row=i, column=1, sticky="w", pady=2)

        btn_bar = ttk.Frame(f)
        btn_bar.grid(row=20, column=0, columnspan=2, pady=(16, 0), sticky="e")
        ttk.Button(btn_bar, text="Pornește acum", command=self._start).grid(row=0, column=0, padx=4)
        ttk.Button(btn_bar, text="Oprește", command=self._stop).grid(row=0, column=1, padx=4)
        ttk.Button(btn_bar, text="Deschide log", command=self._open_log).grid(row=0, column=2, padx=4)
        ttk.Button(btn_bar, text="Reactivează", command=self._reenroll).grid(row=0, column=3, padx=4)
        ttk.Button(btn_bar, text="Dezinstalează", command=self._uninstall).grid(row=0, column=4, padx=4)

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

def run_gui() -> int:
    root = tk.Tk()
    root.title(WINDOW_TITLE)
    root.geometry("520x460")
    root.resizable(False, False)

    try:
        ttk.Style().theme_use("vista" if _is_windows() else "clam")
    except tk.TclError:
        pass

    cfg = BridgeConfig.load()
    if cfg.is_claimed():
        StatusPanel(root)
    else:
        EnrollForm(root)

    root.mainloop()
    return 0
