#!/usr/bin/env python3
"""macOS Audio Output Switcher & Scheduler GUI.

Features:
- Genuine Virtual Audio Device: Visible in macOS System Settings & Control Center
- Instant Active Override: CoreAudio property listener fires within ~1-5ms of any device change
- Double-Layer Silence Block: Hardware routing to silent HAL sink + continuous 0% volume clamping
- Volume Key Interception: CGEventTap suppresses F11/F12/Mute when volume is locked
- PIN / Tamper Lock: Password-protects scheduler controls & displays macOS authorization dialog
- Multi-schedule table with Add, Edit, Delete, Toggle
- Dropbox / custom cloud config path selector
- Per-schedule return action: auto-restore last used or switch to specific device
"""

from __future__ import annotations
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

import coreaudio_backend as cab

VIRTUAL_NAME = "Virtual"
VIRTUAL_LABEL = "Virtual (Block: Plays no sound)"
DEFAULT_CONFIG_PATH = str(Path.home() / ".macos_audio_scheduler" / "schedules.json")
DAYS_OF_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ==============================================================================
# Startup Dependency Validation
# ==============================================================================
def validate_dependencies() -> dict[str, bool]:
    """Check all dependencies and return status dict."""
    return cab.check_dependencies()


def show_dependency_errors(deps: dict[str, bool]) -> bool:
    """Show error dialogs for missing dependencies. Returns True if critical deps are met."""
    missing: list[str] = []

    if not deps.get("blackhole"):
        missing.append(
            "• BlackHole 2ch (silent virtual audio driver)\n"
            "  Install: brew install blackhole-2ch"
        )
    if not deps.get("pyobjc"):
        missing.append(
            "• PyObjC (Python-ObjC bridge for aggregate device creation)\n"
            "  Install: pip3 install pyobjc-framework-CoreAudio"
        )

    if missing:
        msg = (
            "Missing required dependencies:\n\n"
            + "\n\n".join(missing)
            + "\n\nThe Virtual sound-blocking device will not be available.\n"
            "Audio switching and scheduling will still work with real devices.\n\n"
            "Run the setup command to install everything:\n"
            "  ./switch_audio.sh setup"
        )
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning("Missing Dependencies", msg)
            root.destroy()
        except Exception:
            print(f"WARNING: {msg}", file=sys.stderr)

    if not deps.get("accessibility"):
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(
                "Accessibility Permission",
                "Volume key interception requires Accessibility permission.\n\n"
                "To enable:\n"
                "System Settings → Privacy & Security → Accessibility\n"
                "→ Add Python / Terminal to the list.\n\n"
                "Without this, volume keys (F11/F12) cannot be blocked during schedules.\n"
                "All other features will work normally.",
            )
            root.destroy()
        except Exception:
            pass

    # Critical: at least the CoreAudio backend must work (it always does on macOS)
    return True


# ==============================================================================
# Helper Functions: Virtual Sink & Volume Controls
# ==============================================================================
def find_virtual_sink_device() -> str | None:
    """Finds a genuine HAL virtual silent output device via CoreAudio enumeration."""
    dev = cab.find_virtual_sink()
    return dev.name if dev else None


def ensure_virtual_device_exists() -> str:
    """Creates a visible aggregate device named 'Virtual' in macOS System Settings."""
    dev_id = cab.create_virtual_device()
    if dev_id is not None:
        return VIRTUAL_NAME
    # Fallback: try to use BlackHole directly
    sink = find_virtual_sink_device()
    return sink if sink else VIRTUAL_NAME


def get_virtual_label() -> str:
    return VIRTUAL_LABEL


def is_virtual_target(name: str) -> bool:
    if not name:
        return False
    return name.startswith("Virtual") or name == VIRTUAL_NAME


def resolve_hw_target(name: str) -> str:
    """Resolves virtual label to actual underlying macOS output device name."""
    if is_virtual_target(name):
        return ensure_virtual_device_exists()
    return name


def get_current_device() -> str:
    """Gets the current default output device name via CoreAudio API."""
    dev = cab.get_default_output_device()
    return dev.name if dev else "Unknown"


def get_available_devices() -> list[str]:
    """Lists all output devices, with Virtual label first."""
    ensure_virtual_device_exists()
    v_label = get_virtual_label()
    devices = [v_label]
    for d in cab.get_output_devices():
        # Hide raw BlackHole and the aggregate "Virtual" from the user-facing list
        if d.name not in (VIRTUAL_NAME, "BlackHole 2ch") and d.name not in devices:
            devices.append(d.name)
    return devices


def get_volume_state() -> tuple[int, bool]:
    """Returns (output_volume_int, is_muted_bool)."""
    try:
        cmd = 'set vol to output volume of (get volume settings)\nset isM to output muted of (get volume settings)\nreturn (vol as text) & "|" & (isM as text)'
        res = subprocess.run(["osascript", "-e", cmd], capture_output=True, text=True, check=True)
        v_str, m_str = res.stdout.strip().split("|")
        if "missing" in v_str.lower() or not v_str.strip().isdigit():
            return 0, True
        return int(v_str), (m_str.lower() == "true")
    except Exception:
        return 0, True


def set_volume_state(vol: int, muted: bool) -> None:
    mute_clause = "with output muted" if muted else "without output muted"
    subprocess.run(["osascript", "-e", f"set volume output volume {vol} {mute_clause}"], capture_output=True)


def mute_block() -> None:
    subprocess.run(["osascript", "-e", "set volume output volume 0 with output muted"], capture_output=True)


def switch_audio_device(name: str) -> bool:
    """Switches macOS audio output. If target is Virtual, routes to silent HAL driver and sets volume 0 muted."""
    if is_virtual_target(name):
        target_dev = ensure_virtual_device_exists()
        ok = cab.set_default_output_by_name(target_dev)
        mute_block()
        return ok
    return cab.set_default_output_by_name(name)


# ==============================================================================
# Security & PIN Management
# ==============================================================================
def hash_pin(pin: str, salt: str | None = None) -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(8)
    h = hashlib.sha256((salt + pin).encode("utf-8")).hexdigest()
    return h, salt


def verify_pin(pin: str, pin_hash: str, salt: str) -> bool:
    if not pin_hash or not salt:
        return True
    h = hashlib.sha256((salt + pin).encode("utf-8")).hexdigest()
    return h == pin_hash


def prompt_macos_pin_dialog(prompt: str) -> str | None:
    """Displays a native macOS modal password dialog over Control Center / Settings."""
    escaped_prompt = prompt.replace('"', '\\"')
    apple_script = f'''
    try
        set res to display dialog "{escaped_prompt}" with title "Audio Output Security" default answer "" with hidden answer buttons {{"Cancel", "Unlock"}} default button "Unlock"
        return text returned of res
    on error
        return ""
    end try
    '''
    try:
        res = subprocess.run(["osascript", "-e", apple_script], capture_output=True, text=True)
        ans = res.stdout.strip()
        return ans if ans else None
    except Exception:
        return None


# ==============================================================================
# Schedule Modal Dialog with Volume Constraints
# ==============================================================================
class ScheduleDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, devices: list[str], data: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.title("Edit Schedule" if data else "Add New Schedule")
        self.geometry("540x650")
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
        if is_virtual_target(target):
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
        v_label = get_virtual_label()
        if not self.data:
            if self.devices:
                self.target_var.set(self.devices[0])
                self.return_var.set(self.devices[0])
            return
        self.name_var.set(self.data.get("name", ""))
        target = self.data.get("target_device", "")
        if is_virtual_target(target):
            self.target_var.set(v_label)
        else:
            self.target_var.set(target)

        self.start_var.set(self.data.get("start_time", "14:00"))
        self.end_var.set(self.data.get("end_time", "18:00"))
        self.vol_mode_var.set(self.data.get("volume_mode", "unlocked"))
        self.fixed_vol_var.set(str(self.data.get("fixed_volume", 40)))
        self.min_vol_var.set(str(self.data.get("min_volume", 15)))
        self.max_vol_var.set(str(self.data.get("max_volume", 60)))

        self.end_act_var.set(self.data.get("end_action", "restore_previous"))
        ret_dev = self.data.get("return_device", "")
        if is_virtual_target(ret_dev):
            self.return_var.set(v_label)
        else:
            self.return_var.set(ret_dev)
        self.enabled_var.set(self.data.get("enabled", True))

        active_days = set(self.data.get("days", DAYS_OF_WEEK))
        for d, v in self.day_vars.items():
            v.set(d in active_days)

        self._on_target_changed()

    def _save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Validation Error", "Please provide a schedule name.", parent=self)
            return

        def parse_hm(s: str) -> tuple[int, int] | None:
            parts = s.strip().split(":")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    return h, m
            return None

        if not parse_hm(self.start_var.get()) or not parse_hm(self.end_var.get()):
            messagebox.showerror("Validation Error", "Times must be in HH:MM 24-hour format.", parent=self)
            return

        days = [d for d, v in self.day_vars.items() if v.get()]
        if not days:
            messagebox.showerror("Validation Error", "Select at least one active day of the week.", parent=self)
            return

        target = self.target_var.get().strip()
        if not target:
            messagebox.showerror("Validation Error", "Select a target output device.", parent=self)
            return

        clean_target = VIRTUAL_NAME if is_virtual_target(target) else target
        clean_return = VIRTUAL_NAME if is_virtual_target(self.return_var.get().strip()) else self.return_var.get().strip()

        # Parse volume settings
        vol_mode = self.vol_mode_var.get()
        fixed_v = 40
        min_v = 15
        max_v = 60

        if clean_target == VIRTUAL_NAME:
            vol_mode = "fixed"
            fixed_v = 0
        elif vol_mode == "fixed":
            try:
                fixed_v = max(0, min(100, int(self.fixed_vol_var.get())))
            except ValueError:
                messagebox.showerror("Validation Error", "Fixed volume must be an integer between 0 and 100.", parent=self)
                return
        elif vol_mode == "range":
            try:
                min_v = max(0, min(100, int(self.min_vol_var.get())))
                max_v = max(0, min(100, int(self.max_vol_var.get())))
                if min_v > max_v:
                    min_v, max_v = max_v, min_v
            except ValueError:
                messagebox.showerror("Validation Error", "Volume range must be integers between 0 and 100.", parent=self)
                return

        self.result = {
            "id": self.data.get("id", str(int(time.time() * 1000))),
            "name": name,
            "target_device": clean_target,
            "start_time": self.start_var.get().strip(),
            "end_time": self.end_var.get().strip(),
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
        self.root.geometry("880x700")
        self.root.minsize(780, 580)

        self.config_path = DEFAULT_CONFIG_PATH
        self.devices: list[str] = []
        self.schedules: list[dict[str, Any]] = []

        # Security state
        self.pin_hash: str | None = None
        self.pin_salt: str | None = None
        self.is_locked = False
        self._prompting_pin = False

        # Daemon state
        self.daemon_running = False
        self.daemon_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.active_sched_id: str | None = None
        self.saved_device: str | None = None
        self.saved_volume_state: tuple[int, bool] | None = None

        # CoreAudio event-driven override: instant device change detection
        self._device_listener = cab.DeviceChangeListener(self._on_device_changed)
        self._target_device_id: int | None = None  # The device ID we're enforcing
        self._target_device_name: str | None = None

        # CGEventTap volume key interceptor
        self._volume_locked = False  # Whether volume keys should be suppressed
        self._volume_interceptor = cab.VolumeKeyInterceptor(self._should_suppress_volume_key)

        # Active schedule state for the listener callback
        self._active_schedule: dict[str, Any] | None = None
        self._active_vol_mode: str = "unlocked"
        self._active_fixed_v: int = 0
        self._active_min_v: int = 0
        self._active_max_v: int = 100
        self._active_is_virtual: bool = False

        self._build_ui()
        self._refresh_devices()
        self._load_config()

        # Register clean teardown
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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
        self.quick_combo = ttk.Combobox(row2, textvariable=self.quick_var, state="readonly", width=36)
        self.quick_combo.pack(side=tk.LEFT, padx=6)
        ttk.Button(row2, text="⚡ Switch Now", command=self._quick_switch).pack(side=tk.LEFT, padx=6)

        # 2. Security & PIN Tamper Lock Card
        sec_box = ttk.LabelFrame(main, text="Security & Tamper Lock (Password Protection)", padding="8")
        sec_box.pack(fill=tk.X, pady=(0, 8))

        self.lock_status_var = tk.StringVar(value="Security: 🔓 Unlocked (No PIN active)")
        ttk.Label(sec_box, textvariable=self.lock_status_var, font=("", 10, "bold")).pack(side=tk.LEFT, padx=4)

        self.lock_toggle_btn = ttk.Button(sec_box, text="🔒 Enable PIN Lock", command=self._toggle_lock)
        self.lock_toggle_btn.pack(side=tk.RIGHT, padx=4)
        ttk.Button(sec_box, text="🔑 Set / Change PIN", command=self._set_or_change_pin).pack(side=tk.RIGHT, padx=4)

        # 3. Config File Path Card
        path_box = ttk.LabelFrame(main, text="Schedule Configuration File Path (Dropbox / Cloud / Local)", padding="8")
        path_box.pack(fill=tk.X, pady=(0, 8))

        self.path_var = tk.StringVar(value=self.config_path)
        ttk.Entry(path_box, textvariable=self.path_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(path_box, text="Browse...", command=self._browse_path).pack(side=tk.LEFT, padx=3)
        ttk.Button(path_box, text="Load", command=self._load_config).pack(side=tk.LEFT, padx=3)
        ttk.Button(path_box, text="Save", command=self._save_config).pack(side=tk.LEFT, padx=3)

        # 4. Schedules Table
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
        self.tree.column("name", width=125)
        self.tree.column("window", width=95, anchor="center")
        self.tree.column("days", width=110)
        self.tree.column("target", width=170)
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

        # 5. Service Control Bar
        svc_box = ttk.LabelFrame(main, text="Scheduler & Override Service Control", padding="8")
        svc_box.pack(fill=tk.X, pady=(0, 8))

        self.status_var = tk.StringVar(value="Status: ⏹️ Stopped")
        ttk.Label(svc_box, textvariable=self.status_var, font=("", 11, "bold")).pack(side=tk.LEFT, padx=4)
        self.svc_btn = ttk.Button(svc_box, text="▶️ Start Scheduler", command=self._toggle_service)
        self.svc_btn.pack(side=tk.RIGHT, padx=4)

        # 6. Activity Log
        log_box_frame = ttk.LabelFrame(main, text="Activity Log (Live Overrides, Clamping & Security)", padding="6")
        log_box_frame.pack(fill=tk.BOTH, expand=True)
        self.log_box = tk.Text(log_box_frame, height=4, state=tk.DISABLED, wrap=tk.WORD, font=("Menlo", 10))
        self.log_box.pack(fill=tk.BOTH, expand=True)

    # --------------------------------------------------------------------------
    # PIN & Security Actions
    # --------------------------------------------------------------------------
    def _require_unlock(self, action_name: str = "This action") -> bool:
        if not self.pin_hash or not self.is_locked:
            return True
        pin = simpledialog.askstring("PIN Verification", f"{action_name} requires administrator PIN.\nEnter PIN:", show="*", parent=self.root)
        if pin is None:
            return False
        if verify_pin(pin, self.pin_hash, self.pin_salt or ""):
            self._log(f"[AUTH] PIN verified for '{action_name}'.")
            return True
        messagebox.showerror("Error", "Incorrect PIN. Action denied.", parent=self.root)
        self._log(f"[AUTH] Failed PIN attempt for '{action_name}'.")
        return False

    def _set_or_change_pin(self) -> None:
        if self.pin_hash:
            if not self._require_unlock("Changing PIN"):
                return
        new_pin = simpledialog.askstring("Set Admin PIN", "Enter new Admin PIN / Password (leave blank to remove PIN):", show="*", parent=self.root)
        if new_pin is None:
            return
        new_pin = new_pin.strip()
        if not new_pin:
            self.pin_hash = None
            self.pin_salt = None
            self.is_locked = False
            self._update_lock_ui()
            self._save_config_silently()
            self._log("[AUTH] Admin PIN removed.")
            messagebox.showinfo("PIN Cleared", "PIN protection disabled.", parent=self.root)
            return

        confirm_pin = simpledialog.askstring("Confirm PIN", "Re-enter new Admin PIN / Password to confirm:", show="*", parent=self.root)
        if confirm_pin != new_pin:
            messagebox.showerror("Mismatch", "PINs did not match. PIN not changed.", parent=self.root)
            return

        h, s = hash_pin(new_pin)
        self.pin_hash = h
        self.pin_salt = s
        self.is_locked = True
        self._update_lock_ui()
        self._save_config_silently()
        self._log("[AUTH] Admin PIN set successfully. Tamper Lock enabled.")
        messagebox.showinfo("Success", "Admin PIN set. Tamper Lock is now ACTIVE.", parent=self.root)

    def _toggle_lock(self) -> None:
        if not self.pin_hash:
            self._set_or_change_pin()
            return

        if self.is_locked:
            if self._require_unlock("Unlocking Scheduler"):
                self.is_locked = False
                self._update_lock_ui()
                self._log("[AUTH] Tamper Lock manually unlocked.")
        else:
            self.is_locked = True
            self._update_lock_ui()
            self._log("[AUTH] Tamper Lock engaged.")

    def _update_lock_ui(self) -> None:
        if not self.pin_hash:
            self.lock_status_var.set("Security: 🔓 Unlocked (No PIN configured)")
            self.lock_toggle_btn.configure(text="🔒 Enable PIN Lock")
        elif self.is_locked:
            self.lock_status_var.set("Security: 🔒 LOCKED (Tamper Lock Active)")
            self.lock_toggle_btn.configure(text="🔓 Unlock")
        else:
            self.lock_status_var.set("Security: 🔓 UNLOCKED (PIN configured)")
            self.lock_toggle_btn.configure(text="🔒 Lock Now")

    # --------------------------------------------------------------------------
    # Audio Actions
    # --------------------------------------------------------------------------
    def _refresh_devices(self) -> None:
        self.devices = get_available_devices()
        curr = get_current_device()
        vol, muted = get_volume_state()
        mute_text = " [Muted]" if muted else f" [{vol}%]"
        v_sink = find_virtual_sink_device()
        v_info = f" (Virtual HAL: {v_sink})" if v_sink else " (No Virtual HAL driver)"
        self.curr_var.set(f"🔊 Output: {curr}{mute_text}{v_info}")
        self.quick_combo["values"] = self.devices
        v_label = get_virtual_label()

        if is_virtual_target(curr):
            self.quick_var.set(v_label)
        elif curr in self.devices:
            self.quick_var.set(curr)
        elif self.devices:
            self.quick_var.set(self.devices[0])
        self._log(f"Detected {len(self.devices)} audio outputs (Virtual HAL Sink: {v_sink}).")

    def _quick_switch(self) -> None:
        if not self._require_unlock("Manual Output Switch"):
            return
        target = self.quick_var.get().strip()
        if not target:
            return
        if switch_audio_device(target):
            clean_target = resolve_hw_target(target)
            self._log(f"Switched output to: '{clean_target}'")
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
                    data = json.load(f)
                if isinstance(data, list):
                    self.schedules = data
                elif isinstance(data, dict):
                    self.schedules = data.get("schedules", [])
                    self.pin_hash = data.get("pin_hash")
                    self.pin_salt = data.get("pin_salt")
                    self.is_locked = bool(data.get("is_locked", bool(self.pin_hash)))
                self._log(f"Loaded {len(self.schedules)} schedules from {p}")
            except Exception as e:
                self._log(f"Error loading config: {e}")
                self.schedules = []
        else:
            self.schedules = []
        self._update_lock_ui()
        self._refresh_table()

    def _save_config(self) -> None:
        if not self._require_unlock("Saving Configuration"):
            return
        p = os.path.expanduser(self.path_var.get().strip())
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            payload = {
                "pin_hash": self.pin_hash,
                "pin_salt": self.pin_salt,
                "is_locked": self.is_locked,
                "schedules": self.schedules,
            }
            with open(p, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            self._log(f"Saved {len(self.schedules)} schedules to {p}")
            messagebox.showinfo("Saved", f"Configuration saved successfully to:\n{p}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save config: {e}")

    def _save_config_silently(self) -> None:
        p = os.path.expanduser(self.path_var.get().strip())
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            payload = {
                "pin_hash": self.pin_hash,
                "pin_salt": self.pin_salt,
                "is_locked": self.is_locked,
                "schedules": self.schedules,
            }
            with open(p, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # Table CRUD
    # --------------------------------------------------------------------------
    def _refresh_table(self) -> None:
        for i in self.tree.get_children():
            self.tree.delete(i)
        v_label = get_virtual_label()
        for s in self.schedules:
            status = "🟢 ON" if s.get("enabled", True) else "⚪ OFF"
            days = "Everyday" if len(s.get("days", [])) == 7 else ", ".join(s.get("days", []))
            window = f"{s.get('start_time')} - {s.get('end_time')}"
            target_display = v_label if is_virtual_target(s.get("target_device", "")) else s.get("target_device")

            v_mode = s.get("volume_mode", "unlocked")
            if is_virtual_target(s.get("target_device", "")):
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
        if not self._require_unlock("Adding Schedule"):
            return
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
        if not self._require_unlock("Editing Schedule"):
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
        if not self._require_unlock("Deleting Schedule"):
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
        if not self._require_unlock("Toggling Schedule"):
            return
        selected["enabled"] = not selected.get("enabled", True)
        self._save_config_silently()
        self._refresh_table()
        state = "Enabled" if selected["enabled"] else "Disabled"
        self._log(f"{state} schedule '{selected.get('name')}'.")

    # --------------------------------------------------------------------------
    # Background Scheduler Service & Instant Active Enforcement
    # --------------------------------------------------------------------------
    def _should_suppress_volume_key(self) -> bool:
        """Called by VolumeKeyInterceptor (CGEventTap) on F11/F12/Mute keypresses."""
        return self._volume_locked

    def _on_device_changed(self, new_dev_id: int) -> None:
        """Called immediately by CoreAudio property listener when default output device changes."""
        if not self.daemon_running or self.active_sched_id is None:
            return
        if self._target_device_id is None:
            return
        if new_dev_id != self._target_device_id:
            # Snap back instantly within CoreAudio
            target_name = self._target_device_name or f"Device #{self._target_device_id}"
            self._log(f"[INSTANT OVERRIDE] Audio changed in macOS. Snapping back to '{target_name}' within CoreAudio...")
            cab.set_default_output_device(self._target_device_id)
            if self._active_is_virtual:
                mute_block()
            self.root.after(0, self._refresh_devices)

            # If Tamper Lock is active, display native macOS authorization prompt
            if self.is_locked and self.pin_hash and not self._prompting_pin:
                self._prompting_pin = True

                def auth_thread() -> None:
                    try:
                        ans = prompt_macos_pin_dialog(
                            "Audio Output is locked by Audio Scheduler.\nEnter Admin PIN to authorize device changes:"
                        )
                        if ans and verify_pin(ans, self.pin_hash or "", self.pin_salt or ""):
                            self.is_locked = False
                            self.root.after(0, self._update_lock_ui)
                            self._log("[AUTH] Correct PIN entered in macOS prompt. Tamper Lock disengaged.")
                        elif ans:
                            self._log("[AUTH] Incorrect PIN entered in macOS prompt. Keeping device locked.")
                    finally:
                        self._prompting_pin = False

                threading.Thread(target=auth_thread, daemon=True).start()

    def _toggle_service(self) -> None:
        if self.daemon_running:
            if not self._require_unlock("Stopping Scheduler"):
                return
            self._stop_service()
        else:
            self._start_service()

    def _start_service(self) -> None:
        self.daemon_running = True
        self.stop_event.clear()
        if self._device_listener:
            self._device_listener.start()
        self.svc_btn.configure(text="⏹️ Stop Scheduler")
        self.status_var.set("Status: 🟢 Running (CoreAudio Listener Active)")
        self._log("Scheduler & Override service started with CoreAudio hardware event listener.")

        def loop() -> None:
            while not self.stop_event.is_set():
                is_active = self._evaluate_tick()
                # When active, device changes are handled instantly (1-5ms) by the CoreAudio listener callback.
                # Polling interval is only needed for checking schedule boundaries and volume slider clamping.
                sleep_secs = 0.5 if is_active else 2.0
                slices = int(sleep_secs / 0.1)
                for _ in range(slices):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)

        self.daemon_thread = threading.Thread(target=loop, daemon=True)
        self.daemon_thread.start()

    def _stop_service(self) -> None:
        self.stop_event.set()
        self.daemon_running = False
        if self._device_listener:
            self._device_listener.stop()
        if self._volume_interceptor:
            self._volume_interceptor.stop()
        self._volume_locked = False
        self._target_device_id = None
        self._target_device_name = None
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
            is_virtual = is_virtual_target(raw_target)
            target_hw = resolve_hw_target(raw_target)

            vol_mode = matching.get("volume_mode", "unlocked")
            fixed_v = matching.get("fixed_volume", 40)
            min_v = matching.get("min_volume", 15)
            max_v = matching.get("max_volume", 60)

            if is_virtual:
                vol_mode = "fixed"
                fixed_v = 0

            self._active_is_virtual = is_virtual
            self._active_vol_mode = vol_mode
            self._active_fixed_v = fixed_v
            self._active_min_v = min_v
            self._active_max_v = max_v
            self._target_device_name = target_hw

            target_dev = cab.find_device_by_name(target_hw)
            if target_dev:
                self._target_device_id = target_dev.device_id
            else:
                self._target_device_id = None

            if self.active_sched_id != mid:
                if self.active_sched_id is None:
                    self.saved_device = get_current_device()
                    self.saved_volume_state = get_volume_state()
                    self._log(f"Window started: '{matching['name']}'. Saved state (device: '{self.saved_device}', vol: {self.saved_volume_state[0]}%)")
                self.active_sched_id = mid
                self._log(f"Engaging target: '{raw_target}' (HW: '{target_hw}')")
                switch_audio_device(raw_target)

                # Hardware volume key interception
                if is_virtual or vol_mode == "fixed":
                    self._volume_locked = True
                    if self._volume_interceptor and self._volume_interceptor.start():
                        self._log("[VOLUME] Hardware volume keys (F11/F12/Mute) locked via CGEventTap.")
                else:
                    self._volume_locked = False
                    if self._volume_interceptor:
                        self._volume_interceptor.stop()

                self._apply_volume_rules(vol_mode, fixed_v, min_v, max_v, is_virtual)
                self.root.after(0, self._refresh_devices)
            else:
                # Active window ongoing: backup check
                curr_dev = get_current_device()
                if curr_dev != target_hw:
                    self._log(f"[OVERRIDE] Output changed to '{curr_dev}'. Snapping back to '{target_hw}'...")
                    switch_audio_device(raw_target)
                    self.root.after(0, self._refresh_devices)

                # Enforce volume constraints
                clamped = self._apply_volume_rules(vol_mode, fixed_v, min_v, max_v, is_virtual)
                if clamped:
                    self.root.after(0, self._refresh_devices)

            return True

        # Window exited
        elif self.active_sched_id is not None:
            old = next((s for s in self.schedules if s["id"] == self.active_sched_id), None)
            name = old["name"] if old else "Schedule"
            end_act = old.get("end_action", "restore_previous") if old else "restore_previous"
            self._log(f"Window ended: '{name}'. Action: {end_act}")

            # Stop volume key interceptor
            self._volume_locked = False
            if self._volume_interceptor:
                self._volume_interceptor.stop()
            self._target_device_id = None
            self._target_device_name = None

            # Restore volume if saved
            if self.saved_volume_state:
                orig_vol, orig_muted = self.saved_volume_state
                self._log(f"Restoring original volume to {orig_vol}% (muted: {orig_muted})")
                set_volume_state(orig_vol, orig_muted)

            restore_to = self.saved_device
            if end_act == "specific_device" and old and old.get("return_device"):
                ret = old["return_device"]
                restore_to = resolve_hw_target(ret)

            if restore_to and end_act != "do_nothing" and not is_virtual_target(restore_to):
                self._log(f"Restoring audio device to: '{restore_to}'")
                switch_audio_device(restore_to)

            self.root.after(0, self._refresh_devices)
            self.active_sched_id = None
            self.saved_device = None
            self.saved_volume_state = None
            return False

        return False

    def _apply_volume_rules(self, mode: str, fixed_v: int, min_v: int, max_v: int, is_virtual: bool) -> bool:
        """Enforces volume bounds. Returns True if volume was clamped."""
        curr_vol, curr_muted = get_volume_state()

        if is_virtual:
            if curr_vol != 0 or not curr_muted:
                self._log("[OVERRIDE] Sound change detected on Virtual. Re-blocking to 0% muted...")
                mute_block()
                return True
            return False

        if mode == "unlocked":
            return False

        if mode == "fixed":
            if fixed_v == 0:
                if curr_vol != 0 or not curr_muted:
                    self._log("[OVERRIDE] Volume changed on muted target. Re-blocking to 0% muted...")
                    mute_block()
                    return True
            else:
                if curr_vol != fixed_v or curr_muted:
                    self._log(f"[OVERRIDE] Volume changed to {curr_vol}%. Clamping to fixed {fixed_v}%...")
                    set_volume_state(fixed_v, muted=False)
                    return True
        elif mode == "range":
            if curr_muted and min_v > 0:
                self._log(f"[OVERRIDE] Sound was muted. Clamping to minimum {min_v}%...")
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

    def _on_close(self) -> None:
        """Clean teardown on app exit: stop services, listeners, and destroy aggregate device."""
        try:
            if self.daemon_running:
                self._stop_service()
            if self._device_listener:
                self._device_listener.stop()
            if self._volume_interceptor:
                self._volume_interceptor.stop()
            # Clean up the virtual aggregate device from CoreAudio
            cab.destroy_virtual_device()
        except Exception:
            pass
        finally:
            self.root.destroy()


def main() -> None:
    deps = validate_dependencies()
    show_dependency_errors(deps)

    root = tk.Tk()
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"880x700+{max(0, (sw-880)//2)}+{max(0, (sh-700)//2)}")

    app = AudioSchedulerApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app._on_close()


if __name__ == "__main__":
    main()
