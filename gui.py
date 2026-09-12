#!/usr/bin/env python3
"""Bare-Bones macOS Audio Output Switcher & Scheduler GUI (Battery-Optimized).

Features:
- Battery-optimized monitor (low-overhead sleeping, zero subprocesses when idle)
- Live device detection via SwitchAudioSource + Virtual sound block device
- Specific volume locking or allowed volume range (min/max clamping)
- Active override of output and volume changes (clamps shortcuts and GUI sliders)
- Dropdown speaker switcher & quick switch button
- Multi-schedule table with Add, Edit, Delete, Toggle
- Dropbox / custom config path selector
- Per-schedule return action: auto-restore last used or switch to specific device
"""

from __future__ import annotations
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.macos_audio_scheduler/schedules.json")
DAYS_OF_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SWITCH_BIN = shutil.which("SwitchAudioSource") or "/opt/homebrew/bin/SwitchAudioSource"

VIRTUAL_NAME = "Virtual"
VIRTUAL_LABEL = "Virtual (Block: Plays no sound)"


# ==============================================================================
# Helper Functions: Audio CLI & Volume Controls
# ==============================================================================
def get_current_device() -> str:
    try:
        res = subprocess.run([SWITCH_BIN, "-c", "-t", "output"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        return "Unknown"


def get_available_devices() -> list[str]:
    devices = [VIRTUAL_LABEL]
    try:
        res = subprocess.run([SWITCH_BIN, "-a", "-t", "output"], capture_output=True, text=True, check=True)
        for line in res.stdout.strip().splitlines():
            line = line.strip()
            if line and line not in devices:
                devices.append(line)
    except Exception:
        pass
    return devices


def get_volume_state() -> tuple[int, bool]:
    """Returns (output_volume_int, is_muted_bool)."""
    try:
        cmd = 'set vol to output volume of (get volume settings)\nset isM to output muted of (get volume settings)\nreturn (vol as text) & "|" & (isM as text)'
        res = subprocess.run(["osascript", "-e", cmd], capture_output=True, text=True, check=True)
        v_str, m_str = res.stdout.strip().split("|")
        return int(v_str), (m_str.lower() == "true")
    except Exception:
        return 50, False


def set_volume_state(vol: int, muted: bool) -> None:
    mute_clause = "with output muted" if muted else "without output muted"
    subprocess.run(["osascript", "-e", f"set volume output volume {vol} {mute_clause}"], capture_output=True)


def mute_block() -> None:
    subprocess.run(["osascript", "-e", "set volume output volume 0 with output muted"], capture_output=True)


def switch_audio_device(name: str) -> bool:
    if name == VIRTUAL_NAME or name == VIRTUAL_LABEL:
        mute_block()
        return True
    try:
        res = subprocess.run([SWITCH_BIN, "-s", name, "-t", "output"], capture_output=True, text=True)
        return res.returncode == 0
    except Exception:
        return False


# ==============================================================================
# Schedule Modal Dialog with Volume Constraints
# ==============================================================================
class ScheduleDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, devices: list[str], data: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.title("Edit Schedule" if data else "Add New Schedule")
        self.geometry("540x640")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.devices = devices
        self.data = data or {}
        self.result: dict[str, Any] | None = None

        self._build_ui()
        self._populate()
        self.wait_window()

    def _build_ui(self) -> None:
        p = {"padx": 14, "pady": 4}
        container = ttk.Frame(self, padding="14")
        container.pack(fill=tk.BOTH, expand=True)

        # 1. Name
        ttk.Label(container, text="Schedule Name:").grid(row=0, column=0, sticky="w", **p)
        self.name_var = tk.StringVar()
        ttk.Entry(container, textvariable=self.name_var, width=28).grid(row=0, column=1, sticky="w", **p)

        # 2. Target Device Dropdown
        ttk.Label(container, text="Target Output:").grid(row=1, column=0, sticky="w", **p)
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(container, textvariable=self.target_var, values=self.devices, state="readonly", width=28)
        self.target_combo.grid(row=1, column=1, sticky="w", **p)
        self.target_combo.bind("<<ComboboxSelected>>", lambda e: self._on_target_changed())

        # 3. Time Window
        time_box = ttk.LabelFrame(container, text="Time Window (24h format HH:MM)", padding="6")
        time_box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)

        ttk.Label(time_box, text="Start:").grid(row=0, column=0, padx=4)
        self.start_var = tk.StringVar(value="14:00")
        ttk.Entry(time_box, textvariable=self.start_var, width=8).grid(row=0, column=1, padx=4)

        ttk.Label(time_box, text="End:").grid(row=0, column=2, padx=6)
        self.end_var = tk.StringVar(value="18:00")
        ttk.Entry(time_box, textvariable=self.end_var, width=8).grid(row=0, column=3, padx=4)

        # 4. Days
        days_box = ttk.LabelFrame(container, text="Active Days", padding="6")
        days_box.grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)

        self.day_vars: dict[str, tk.BooleanVar] = {}
        row1 = ttk.Frame(days_box)
        row1.pack(fill=tk.X, pady=2)
        for d in DAYS_OF_WEEK:
            v = tk.BooleanVar(value=True)
            self.day_vars[d] = v
            ttk.Checkbutton(row1, text=d, variable=v).pack(side=tk.LEFT, padx=3)

        # 5. Volume Constraint Controls
        vol_box = ttk.LabelFrame(container, text="Volume Enforcement & Sound Range", padding="8")
        vol_box.grid(row=4, column=0, columnspan=2, sticky="ew", pady=6)

        self.vol_mode_var = tk.StringVar(value="unlocked")

        r_unlocked = ttk.Radiobutton(vol_box, text="Unlocked (Allow free adjustment by user)", variable=self.vol_mode_var, value="unlocked", command=self._update_vol_mode)
        r_unlocked.grid(row=0, column=0, columnspan=3, sticky="w", pady=2)

        r_fixed = ttk.Radiobutton(vol_box, text="Lock to Fixed Volume (%):", variable=self.vol_mode_var, value="fixed", command=self._update_vol_mode)
        r_fixed.grid(row=1, column=0, sticky="w", pady=2)
        self.fixed_vol_var = tk.StringVar(value="40")
        self.fixed_entry = ttk.Spinbox(vol_box, from_=0, to=100, textvariable=self.fixed_vol_var, width=5)
        self.fixed_entry.grid(row=1, column=1, sticky="w", padx=4)

        r_range = ttk.Radiobutton(vol_box, text="Constrain to Allowed Range:", variable=self.vol_mode_var, value="range", command=self._update_vol_mode)
        r_range.grid(row=2, column=0, sticky="w", pady=2)
        range_frame = ttk.Frame(vol_box)
        range_frame.grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Label(range_frame, text="Min:").pack(side=tk.LEFT)
        self.min_vol_var = tk.StringVar(value="15")
        self.min_entry = ttk.Spinbox(range_frame, from_=0, to=100, textvariable=self.min_vol_var, width=4)
        self.min_entry.pack(side=tk.LEFT, padx=3)
        ttk.Label(range_frame, text="%   Max:").pack(side=tk.LEFT)
        self.max_vol_var = tk.StringVar(value="60")
        self.max_entry = ttk.Spinbox(range_frame, from_=0, to=100, textvariable=self.max_vol_var, width=4)
        self.max_entry.pack(side=tk.LEFT, padx=3)
        ttk.Label(range_frame, text="%").pack(side=tk.LEFT)

        # 6. Return Action
        end_box = ttk.LabelFrame(container, text="On Schedule End", padding="6")
        end_box.grid(row=5, column=0, columnspan=2, sticky="ew", pady=4)

        self.end_act_var = tk.StringVar(value="restore_previous")
        ttk.Radiobutton(end_box, text="Restore last used audio source & volume", variable=self.end_act_var, value="restore_previous", command=self._toggle_return_combo).grid(row=0, column=0, columnspan=2, sticky="w")
        
        ttk.Radiobutton(end_box, text="Switch to specific device:", variable=self.end_act_var, value="specific_device", command=self._toggle_return_combo).grid(row=1, column=0, sticky="w")
        self.return_var = tk.StringVar()
        self.return_combo = ttk.Combobox(end_box, textvariable=self.return_var, values=self.devices, state="readonly", width=22)
        self.return_combo.grid(row=1, column=1, sticky="w", padx=4)

        # 7. Enabled Checkbox
        self.enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(container, text="Enable this schedule (Actively enforces rules during window)", variable=self.enabled_var).grid(row=6, column=0, columnspan=2, sticky="w", **p)

        # 8. Buttons
        btn_box = ttk.Frame(container)
        btn_box.grid(row=7, column=0, columnspan=2, pady=10)
        ttk.Button(btn_box, text="Save Schedule", command=self._save).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_box, text="Cancel", command=self.destroy).pack(side=tk.LEFT, padx=6)

        self._update_vol_mode()
        self._toggle_return_combo()

    def _on_target_changed(self) -> None:
        target = self.target_var.get()
        if target == VIRTUAL_NAME or target == VIRTUAL_LABEL:
            self.vol_mode_var.set("fixed")
            self.fixed_vol_var.set("0")
            self._update_vol_mode()

    def _update_vol_mode(self) -> None:
        mode = self.vol_mode_var.get()
        self.fixed_entry.configure(state="normal" if mode == "fixed" else "disabled")
        self.min_entry.configure(state="normal" if mode == "range" else "disabled")
        self.max_entry.configure(state="normal" if mode == "range" else "disabled")

    def _toggle_return_combo(self) -> None:
        state = "readonly" if self.end_act_var.get() == "specific_device" else "disabled"
        self.return_combo.configure(state=state)

    def _populate(self) -> None:
        if not self.data:
            if self.devices:
                self.target_var.set(self.devices[0])
                self.return_var.set(self.devices[0])
            return
        self.name_var.set(self.data.get("name", ""))
        target = self.data.get("target_device", "")
        if target == VIRTUAL_NAME or target == VIRTUAL_LABEL:
            self.target_var.set(VIRTUAL_LABEL)
        else:
            self.target_var.set(target)

        self.start_var.set(self.data.get("start_time", "14:00"))
        self.end_var.set(self.data.get("end_time", "18:00"))
        self.vol_mode_var.set(self.data.get("volume_mode", "unlocked"))
        self.fixed_vol_var.set(str(self.data.get("fixed_volume", 40)))
        self.min_vol_var.set(str(self.data.get("min_volume", 15)))
        self.max_vol_var.set(str(self.data.get("max_volume", 60)))

        self.end_act_var.set(self.data.get("end_action", "restore_previous"))
        self.return_var.set(self.data.get("return_device", ""))
        self.enabled_var.set(self.data.get("enabled", True))

        active_days = set(self.data.get("days", DAYS_OF_WEEK))
        for d, v in self.day_vars.items():
            v.set(d in active_days)

        self._update_vol_mode()
        self._toggle_return_combo()

    def _save(self) -> None:
        name = self.name_var.get().strip()
        target = self.target_var.get().strip()
        st = self.start_var.get().strip()
        et = self.end_var.get().strip()
        if not name or not target or not st or not et:
            messagebox.showerror("Validation Error", "Please fill in all required fields.", parent=self)
            return

        days = [d for d, v in self.day_vars.items() if v.get()]
        if not days:
            messagebox.showerror("Validation Error", "Select at least one active day.", parent=self)
            return

        clean_target = VIRTUAL_NAME if (target == VIRTUAL_LABEL or target == VIRTUAL_NAME) else target
        clean_return = VIRTUAL_NAME if (self.return_var.get().strip() == VIRTUAL_LABEL) else self.return_var.get().strip()

        vol_mode = self.vol_mode_var.get()
        if clean_target == VIRTUAL_NAME:
            vol_mode = "fixed"
            fixed_v = 0
            min_v = 0
            max_v = 0
        else:
            try:
                fixed_v = max(0, min(100, int(self.fixed_vol_var.get())))
                min_v = max(0, min(100, int(self.min_vol_var.get())))
                max_v = max(0, min(100, int(self.max_vol_var.get())))
                if min_v > max_v:
                    min_v, max_v = max_v, min_v
            except ValueError:
                messagebox.showerror("Error", "Volume values must be numbers between 0 and 100.", parent=self)
                return

        self.result = {
            "id": self.data.get("id") or str(int(time.time() * 1000)),
            "name": name,
            "target_device": clean_target,
            "start_time": st,
            "end_time": et,
            "days": days,
            "volume_mode": vol_mode,
            "fixed_volume": fixed_v,
            "min_volume": min_v,
            "max_volume": max_v,
            "end_action": self.end_act_var.get(),
            "return_device": clean_return,
            "enabled": self.enabled_var.get(),
        }
        self.destroy()


# ==============================================================================
# Main GUI Window
# ==============================================================================
class AudioSchedulerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Audio Output Switcher & Override Scheduler")
        self.root.geometry("860x660")
        self.root.minsize(760, 560)

        self.config_path = DEFAULT_CONFIG_PATH
        self.devices: list[str] = []
        self.schedules: list[dict[str, Any]] = []

        # Daemon state
        self.daemon_running = False
        self.daemon_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.active_sched_id: str | None = None
        self.saved_device: str | None = None
        self.saved_volume_state: tuple[int, bool] | None = None

        self._build_ui()
        self._refresh_devices()
        self._load_config()

    def _log(self, text: str) -> None:
        def append() -> None:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_box.configure(state=tk.NORMAL)
            self.log_box.insert(tk.END, f"[{ts}] {text}\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state=tk.DISABLED)
        self.root.after(0, append)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding="12")
        main.pack(fill=tk.BOTH, expand=True)

        # 1. Header Card: Current Output & Quick Switch
        header = ttk.LabelFrame(main, text="Current Audio Output & Quick Switch", padding="10")
        header.pack(fill=tk.X, pady=(0, 8))

        row1 = ttk.Frame(header)
        row1.pack(fill=tk.X, pady=2)
        self.curr_var = tk.StringVar(value="Detecting audio device...")
        ttk.Label(row1, textvariable=self.curr_var, font=("", 12, "bold"), foreground="#007AFF").pack(side=tk.LEFT)
        ttk.Button(row1, text="🔄 Refresh", command=self._refresh_devices).pack(side=tk.RIGHT)

        row2 = ttk.Frame(header)
        row2.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(row2, text="Quick Switch:").pack(side=tk.LEFT, padx=(0, 6))
        self.quick_var = tk.StringVar()
        self.quick_combo = ttk.Combobox(row2, textvariable=self.quick_var, state="readonly", width=34)
        self.quick_combo.pack(side=tk.LEFT, padx=6)
        ttk.Button(row2, text="⚡ Switch Now", command=self._quick_switch).pack(side=tk.LEFT, padx=6)

        # 2. Config File Path Card
        path_box = ttk.LabelFrame(main, text="Schedule Configuration File Path (Dropbox / Cloud / Local)", padding="8")
        path_box.pack(fill=tk.X, pady=(0, 8))

        self.path_var = tk.StringVar(value=self.config_path)
        ttk.Entry(path_box, textvariable=self.path_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(path_box, text="Browse...", command=self._browse_path).pack(side=tk.LEFT, padx=3)
        ttk.Button(path_box, text="Load", command=self._load_config).pack(side=tk.LEFT, padx=3)
        ttk.Button(path_box, text="Save", command=self._save_config).pack(side=tk.LEFT, padx=3)

        # 3. Schedules Table
        tbl_box = ttk.LabelFrame(main, text="Configured Schedules (Battery-Optimized & Auto-Enforced)", padding="8")
        tbl_box.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        cols = ("enabled", "name", "window", "days", "target", "volume", "on_end")
        self.tree = ttk.Treeview(tbl_box, columns=cols, show="headings", height=6)
        self.tree.heading("enabled", text="Status")
        self.tree.heading("name", text="Name")
        self.tree.heading("window", text="Window")
        self.tree.heading("days", text="Days")
        self.tree.heading("target", text="Target Speaker")
        self.tree.heading("volume", text="Volume Rule")
        self.tree.heading("on_end", text="On End")

        self.tree.column("enabled", width=65, anchor="center")
        self.tree.column("name", width=130)
        self.tree.column("window", width=95, anchor="center")
        self.tree.column("days", width=110)
        self.tree.column("target", width=160)
        self.tree.column("volume", width=125)
        self.tree.column("on_end", width=135)

        scroll = ttk.Scrollbar(tbl_box, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Table buttons
        btn_bar = ttk.Frame(main)
        btn_bar.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(btn_bar, text="➕ Add Schedule", command=self._add_schedule).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_bar, text="✏️ Edit", command=self._edit_schedule).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_bar, text="🗑️ Delete", command=self._delete_schedule).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_bar, text="Toggle On/Off", command=self._toggle_schedule).pack(side=tk.LEFT, padx=4)

        # 4. Service Control Bar
        svc_box = ttk.LabelFrame(main, text="Scheduler & Override Service Control", padding="8")
        svc_box.pack(fill=tk.X, pady=(0, 8))

        self.status_var = tk.StringVar(value="Status: ⏹️ Stopped")
        ttk.Label(svc_box, textvariable=self.status_var, font=("", 11, "bold")).pack(side=tk.LEFT, padx=4)
        self.svc_btn = ttk.Button(svc_box, text="▶️ Start Scheduler", command=self._toggle_service)
        self.svc_btn.pack(side=tk.RIGHT, padx=4)

        # 5. Activity Log
        log_box_frame = ttk.LabelFrame(main, text="Activity Log (Live Overrides & Clamping)", padding="6")
        log_box_frame.pack(fill=tk.BOTH, expand=True)
        self.log_box = tk.Text(log_box_frame, height=4, state=tk.DISABLED, wrap=tk.WORD, font=("Menlo", 10))
        self.log_box.pack(fill=tk.BOTH, expand=True)

    # --------------------------------------------------------------------------
    # Audio Actions
    # --------------------------------------------------------------------------
    def _refresh_devices(self) -> None:
        self.devices = get_available_devices()
        curr = get_current_device()
        vol, muted = get_volume_state()
        mute_text = " [Muted]" if muted else f" [{vol}%]"
        self.curr_var.set(f"🔊 Output: {curr}{mute_text}")
        self.quick_combo["values"] = self.devices
        if curr in self.devices:
            self.quick_var.set(curr)
        elif self.devices:
            self.quick_var.set(self.devices[0])
        self._log(f"Detected {len(self.devices)} output choices.")

    def _quick_switch(self) -> None:
        target = self.quick_var.get().strip()
        if not target:
            return
        if switch_audio_device(target):
            self._log(f"Switched output to: '{target}'")
            self._refresh_devices()
        else:
            messagebox.showerror("Error", f"Could not switch to '{target}'")

    # --------------------------------------------------------------------------
    # Config File
    # --------------------------------------------------------------------------
    def _browse_path(self) -> None:
        chosen = filedialog.asksaveasfilename(
            title="Choose Schedules JSON File (e.g. Dropbox or Local)",
            initialfile="schedules.json",
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
        )
        if chosen:
            self.path_var.set(chosen)
            self._load_config()

    def _load_config(self) -> None:
        p = os.path.expanduser(self.path_var.get().strip())
        self.config_path = p
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    self.schedules = json.load(f)
                self._log(f"Loaded {len(self.schedules)} schedules from {p}")
            except Exception as e:
                self._log(f"Error loading config: {e}")
                self.schedules = []
        else:
            self.schedules = []
        self._refresh_table()

    def _save_config(self) -> None:
        p = os.path.expanduser(self.path_var.get().strip())
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self.schedules, f, indent=2)
            self._log(f"Saved {len(self.schedules)} schedules to {p}")
            messagebox.showinfo("Saved", f"Configuration saved successfully to:\n{p}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save config: {e}")

    # --------------------------------------------------------------------------
    # Table CRUD
    # --------------------------------------------------------------------------
    def _refresh_table(self) -> None:
        for i in self.tree.get_children():
            self.tree.delete(i)
        for s in self.schedules:
            status = "🟢 ON" if s.get("enabled", True) else "⚪ OFF"
            days = "Everyday" if len(s.get("days", [])) == 7 else ", ".join(s.get("days", []))
            window = f"{s.get('start_time')} - {s.get('end_time')}"
            target_display = VIRTUAL_LABEL if s.get("target_device") == VIRTUAL_NAME else s.get("target_device")

            # Format volume display
            v_mode = s.get("volume_mode", "unlocked")
            if s.get("target_device") == VIRTUAL_NAME:
                vol_str = "Muted (0%)"
            elif v_mode == "fixed":
                vol_str = f"Fixed: {s.get('fixed_volume', 40)}%"
            elif v_mode == "range":
                vol_str = f"{s.get('min_volume', 15)}% - {s.get('max_volume', 60)}%"
            else:
                vol_str = "Unlocked"

            on_end = "↺ Restore" if s.get("end_action") == "restore_previous" else f"➜ {s.get('return_device')}"
            self.tree.insert("", tk.END, iid=s["id"], values=(status, s.get("name"), window, days, target_display, vol_str, on_end))

    def _get_selected(self) -> dict[str, Any] | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return next((s for s in self.schedules if s["id"] == sel[0]), None)

    def _add_schedule(self) -> None:
        dlg = ScheduleDialog(self.root, self.devices)
        if dlg.result:
            self.schedules.append(dlg.result)
            self._save_config_silently()
            self._refresh_table()
            self._log(f"Added schedule: '{dlg.result['name']}'")

    def _edit_schedule(self) -> None:
        selected = self._get_selected()
        if not selected:
            messagebox.showinfo("Notice", "Select a schedule to edit.")
            return
        dlg = ScheduleDialog(self.root, self.devices, data=selected)
        if dlg.result:
            idx = next(i for i, s in enumerate(self.schedules) if s["id"] == selected["id"])
            self.schedules[idx] = dlg.result
            self._save_config_silently()
            self._refresh_table()
            self._log(f"Updated schedule: '{dlg.result['name']}'")

    def _delete_schedule(self) -> None:
        selected = self._get_selected()
        if not selected:
            messagebox.showinfo("Notice", "Select a schedule to delete.")
            return
        if messagebox.askyesno("Confirm Delete", f"Delete '{selected.get('name')}'?"):
            self.schedules = [s for s in self.schedules if s["id"] != selected["id"]]
            self._save_config_silently()
            self._refresh_table()
            self._log(f"Deleted schedule: '{selected.get('name')}'")

    def _toggle_schedule(self) -> None:
        selected = self._get_selected()
        if not selected:
            messagebox.showinfo("Notice", "Select a schedule to toggle.")
            return
        selected["enabled"] = not selected.get("enabled", True)
        self._save_config_silently()
        self._refresh_table()
        state = "Enabled" if selected["enabled"] else "Disabled"
        self._log(f"{state} schedule '{selected.get('name')}'.")

    def _save_config_silently(self) -> None:
        p = os.path.expanduser(self.path_var.get().strip())
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self.schedules, f, indent=2)
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # Background Scheduler Service & Battery-Optimized Enforcement
    # --------------------------------------------------------------------------
    def _toggle_service(self) -> None:
        if self.daemon_running:
            self._stop_service()
        else:
            self._start_service()

    def _start_service(self) -> None:
        self.daemon_running = True
        self.stop_event.clear()
        self.svc_btn.configure(text="⏹️ Stop Scheduler")
        self.status_var.set("Status: 🟢 Running (Battery-Optimized)")
        self._log("Scheduler & Override service started.")

        def loop() -> None:
            while not self.stop_event.is_set():
                is_active = self._evaluate_tick()
                # If a schedule is active: check every 2 seconds.
                # If idle: sleep 10 seconds to save battery!
                sleep_dur = 2 if is_active else 10
                for _ in range(sleep_dur):
                    if self.stop_event.is_set():
                        break
                    time.sleep(1)

        self.daemon_thread = threading.Thread(target=loop, daemon=True)
        self.daemon_thread.start()

    def _stop_service(self) -> None:
        self.stop_event.set()
        self.daemon_running = False
        self.svc_btn.configure(text="▶️ Start Scheduler")
        self.status_var.set("Status: ⏹️ Stopped")
        self._log("Scheduler service stopped.")

    def _evaluate_tick(self) -> bool:
        now = datetime.datetime.now()
        now_t = now.time()
        now_d = DAYS_OF_WEEK[now.weekday()]

        matching = None
        for s in self.schedules:
            if not s.get("enabled", True):
                continue
            if now_d not in s.get("days", []):
                continue
            try:
                sh, sm = map(int, s["start_time"].split(":"))
                eh, em = map(int, s["end_time"].split(":"))
                st = datetime.time(sh, sm)
                et = datetime.time(eh, em)
            except Exception:
                continue

            in_win = (st <= now_t < et) if st <= et else (now_t >= st or now_t < et)
            if in_win:
                matching = s
                break

        # Window entered
        if matching:
            mid = matching["id"]
            raw_target = matching["target_device"]
            target = VIRTUAL_NAME if (raw_target == VIRTUAL_LABEL or raw_target == VIRTUAL_NAME) else raw_target
            vol_mode = matching.get("volume_mode", "unlocked")
            fixed_v = matching.get("fixed_volume", 40)
            min_v = matching.get("min_volume", 15)
            max_v = matching.get("max_volume", 60)

            if target == VIRTUAL_NAME:
                vol_mode = "fixed"
                fixed_v = 0

            if self.active_sched_id != mid:
                if self.active_sched_id is None:
                    self.saved_device = get_current_device()
                    self.saved_volume_state = get_volume_state()
                    self._log(f"Window started: '{matching['name']}'. Saved state (device: '{self.saved_device}', vol: {self.saved_volume_state[0]}%)")
                self.active_sched_id = mid
                self._log(f"Engaging target: '{target}'")
                switch_audio_device(target)
                self._apply_volume_rules(vol_mode, fixed_v, min_v, max_v)
                self.root.after(0, self._refresh_devices)
            else:
                # --------------------------------------------------------------
                # ACTIVE ENFORCEMENT: Override GUI sound & volume changes
                # --------------------------------------------------------------
                if target != VIRTUAL_NAME:
                    curr_dev = get_current_device()
                    if curr_dev != target:
                        self._log(f"[OVERRIDE] Output changed to '{curr_dev}'. Forcing back to '{target}'...")
                        switch_audio_device(target)
                        self.root.after(0, self._refresh_devices)

                # Enforce volume constraints (fixed or min/max range)
                clamped = self._apply_volume_rules(vol_mode, fixed_v, min_v, max_v)
                if clamped:
                    self.root.after(0, self._refresh_devices)

            return True

        # Window exited
        elif self.active_sched_id is not None:
            old = next((s for s in self.schedules if s["id"] == self.active_sched_id), None)
            name = old["name"] if old else "Schedule"
            end_act = old.get("end_action", "restore_previous") if old else "restore_previous"
            self._log(f"Window ended: '{name}'. Action: {end_act}")

            # Restore volume if saved
            if self.saved_volume_state:
                orig_vol, orig_muted = self.saved_volume_state
                self._log(f"Restoring original volume to {orig_vol}% (muted: {orig_muted})")
                set_volume_state(orig_vol, orig_muted)

            restore_to = self.saved_device
            if end_act == "specific_device" and old and old.get("return_device"):
                ret = old["return_device"]
                restore_to = VIRTUAL_NAME if (ret == VIRTUAL_LABEL) else ret

            if restore_to and end_act != "do_nothing" and restore_to != VIRTUAL_NAME:
                self._log(f"Restoring audio device to: '{restore_to}'")
                switch_audio_device(restore_to)

            self.root.after(0, self._refresh_devices)
            self.active_sched_id = None
            self.saved_device = None
            self.saved_volume_state = None
            return False

        return False

    def _apply_volume_rules(self, mode: str, fixed_v: int, min_v: int, max_v: int) -> bool:
        """Enforces volume bounds. Returns True if volume was clamped."""
        if mode == "unlocked":
            return False

        curr_vol, curr_muted = get_volume_state()

        if mode == "fixed":
            if fixed_v == 0:
                if curr_vol != 0 or not curr_muted:
                    self._log(f"[OVERRIDE] Sound change detected on Virtual. Re-blocking to 0% muted...")
                    mute_block()
                    return True
            else:
                if curr_vol != fixed_v or curr_muted:
                    self._log(f"[OVERRIDE] Volume changed to {curr_vol}%. Clamping to fixed {fixed_v}%...")
                    set_volume_state(fixed_v, muted=False)
                    return True
        elif mode == "range":
            if curr_muted and min_v > 0:
                self._log(f"[OVERRIDE] Sound un-muting to minimum {min_v}%...")
                set_volume_state(min_v, muted=False)
                return True
            elif curr_vol < min_v:
                self._log(f"[OVERRIDE] Volume {curr_vol}% was below min {min_v}%. Clamping to {min_v}%...")
                set_volume_state(min_v, muted=False)
                return True
            elif curr_vol > max_v:
                self._log(f"[OVERRIDE] Volume {curr_vol}% exceeded max {max_v}%. Clamping to {max_v}%...")
                set_volume_state(max_v, muted=False)
                return True

        return False


def main() -> None:
    root = tk.Tk()
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"860x660+{max(0, (sw-860)//2)}+{max(0, (sh-660)//2)}")

    app = AudioSchedulerApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        if app.daemon_running:
            app._stop_service()


if __name__ == "__main__":
    main()
