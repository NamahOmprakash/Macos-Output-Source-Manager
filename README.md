# macOS Audio Output Source Manager & Scheduler

[![Status: Do Not Use](https://img.shields.io/badge/STATUS-DO_NOT_USE-red.svg?style=for-the-badge)](#)
[![Status: Broken](https://img.shields.io/badge/FUNCTIONALITY-DOES_NOT_WORK-critical.svg?style=for-the-badge)](#)

> [!CAUTION]
> ### ⚠️ DO NOT USE — DOES NOT WORK
> **This project is currently broken / experimental and does not work as intended. DO NOT USE.**

---

## Key Features

- 🔊 **Auto-Restore Last Used Device & Volume**: Automatically remembers whatever speaker/headphones you were using before the schedule started, and restores both the output source and volume when the schedule completes.
- 🚫 **Virtual Sound Block (`Virtual`)**:
  - A dedicated virtual device named `Virtual` (`Block - plays no sound`).
  - Acts as a complete sound barrier by locking volume to 0% and engaging system output mute.
  - Actively prevents sound playback even if someone attempts to raise the volume.
- 🎚️ **Volume Rules & Range Clamping**:
  - **Unlocked**: User can freely adjust sound volume.
  - **Fixed Volume**: Locks volume to a specific percentage (e.g. `40%`), reverting any manual tampering.
  - **Allowed Volume Range (Min % – Max %)**: Allows user freedom to adjust volume within a safe window (e.g. `15% – 50%`), but automatically clamps volume if it exceeds the maximum or drops below the minimum.
- ⚡ **Active Override Enforcement**:
  - Overrides manual macOS Control Center or Sound menu bar changes during active schedules.
  - Overrides keyboard shortcuts (F11/F12 / Volume Up keys) or Touch Bar volume changes if they violate the schedule's volume rules.
- 🔋 **Battery-Optimized Engine**:
  - Avoids tight polling loops and constant process fork/exec wakeups.
  - In idle state (when no schedule is active), sleeps in a low-power state with **zero subprocesses spawned** (near 0.0% CPU usage).
- ☁️ **Dropbox & Cloud Config Sync**:
  - Store your schedule configuration file anywhere (e.g., `~/Dropbox/audio_schedules.json`) to sync schedules across multiple Macs.
- 🖥️ **Dual Interface**:
  - **GUI (`gui.py`)**: Clean, native Tkinter desktop interface with device dropdowns, schedule table, and real-time activity log.
  - **CLI (`switch_audio.sh`)**: Fast, standalone Bash script for terminal power users, scripts, and cron/launchd integration.

---

## Prerequisites & 1-Command Setup

The manager requires **`switchaudio-osx`** (for CLI device switching) and **`blackhole-2ch`** (the open-source HAL driver that enables the genuine silent `Virtual` device in macOS System Settings & Control Center).

### Automated Setup (Recommended)
Run the built-in setup command to install all dependencies automatically via Homebrew:
```bash
./switch_audio.sh setup
```

### Manual Installation
If you prefer installing manually via Homebrew:
```bash
brew install switchaudio-osx blackhole-2ch
```

> [!NOTE]
> **Why is BlackHole required for the Virtual device?**
> macOS CoreAudio security strictly requires any audio output device appearing in System Settings & Control Center to be an authenticated HAL plug-in driver in `/Library/Audio/Plug-Ins/HAL/`. `BlackHole 2ch` acts as the silent loopback sink. If BlackHole is not installed on a machine, the app automatically falls back to `Steam Streaming Speakers` (if Valve Steam is installed) or software-level volume locking.

---

## Quick Start

### 1. Graphical User Interface (GUI)

Run:
```bash
python3 gui.py
```

#### Inside the GUI:
- **Quick Switch**: Select any detected speaker or `Virtual (Block: Plays no sound)` from the dropdown and click **⚡ Switch Now**.
- **Config Path**: Set or browse to your config file (supports local or Dropbox folder paths) and click **Save** or **Load**.
- **Add / Edit Schedule**:
  - Set schedule name and target speaker (or `Virtual`).
  - Configure 24h start and end times (`HH:MM`) and select active days of the week.
  - Set **Volume Enforcement**:
    - *Unlocked*: Free adjustment.
    - *Lock to Fixed Volume*: Keeps volume at exact level.
    - *Constrain to Allowed Range*: Sets `Min %` and `Max %` thresholds.
  - Select **On Schedule End** behavior:
    - *Restore last used audio source & volume*.
    - *Switch to specific device*.
- **Scheduler Control**: Click **▶️ Start Scheduler** to run background monitoring with real-time logs in the Activity Log pane.

---

### 2. Standalone CLI Script (`switch_audio.sh`)

Make sure the script is executable:
```bash
chmod +x switch_audio.sh
```

#### List Available Audio Devices
```bash
./switch_audio.sh list
```

#### Display Current Output Device & Volume
```bash
./switch_audio.sh current
```

#### Immediate Switch
```bash
# Switch to physical device
./switch_audio.sh switch "MacBook Pro Speakers"

# Switch to Virtual Sound Block (mutes sound and sets volume to 0)
./switch_audio.sh switch "Virtual"
```

#### Run a Single Schedule (Auto-Restores on End)
```bash
# Basic schedule (14:00 to 18:00)
./switch_audio.sh schedule --target "MacBook Pro Speakers" --start 14:00 --end 18:00

# With specific active days and explicit return device:
./switch_audio.sh schedule --target "MacBook Pro Speakers" --start 14:00 --end 16:30 --days Mon,Thu --return "JBL Tune 770NC-LE"

# With locked fixed volume (e.g. 40%):
./switch_audio.sh schedule --target "MacBook Pro Speakers" --start 14:00 --end 18:00 --volume 40

# With allowed volume range (min 20%, max 55%):
./switch_audio.sh schedule --target "MacBook Pro Speakers" --start 14:00 --end 18:00 --min-vol 20 --max-vol 55

# With Virtual Sound Block:
./switch_audio.sh schedule --target "Virtual" --start 14:00 --end 18:00
```

#### Run Background Daemon on a Config File
```bash
# Monitors config file (e.g. Dropbox synced schedules)
./switch_audio.sh daemon --config ~/Dropbox/audio_schedules.json
```

---

## Schedule Configuration Example (`schedules.json`)

The schedule configurations are stored in human-readable JSON:

```json
[
  {
    "id": "evening-classes",
    "name": "Evening Classes",
    "target_device": "MacBook Pro Speakers",
    "start_time": "14:00",
    "end_time": "18:00",
    "days": ["Mon", "Wed", "Fri"],
    "volume_mode": "range",
    "fixed_volume": 40,
    "min_volume": 15,
    "max_volume": 50,
    "end_action": "restore_previous",
    "return_device": "",
    "enabled": true
  },
  {
    "id": "focus-block",
    "name": "Silent Focus Block",
    "target_device": "Virtual",
    "start_time": "22:00",
    "end_time": "23:30",
    "days": ["Everyday"],
    "volume_mode": "fixed",
    "fixed_volume": 0,
    "min_volume": 0,
    "max_volume": 0,
    "end_action": "restore_previous",
    "return_device": "",
    "enabled": true
  }
]
```

---

## File Structure

```text
.
├── gui.py              # Native macOS Tkinter GUI application
├── switch_audio.sh     # Standalone CLI engine and background daemon
└── README.md           # Documentation and usage guide
```

---

## License

This project is licensed under a **Non-Commercial License** (Personal & Educational Use Only). Commercial use, resale, or monetization is strictly prohibited without prior written permission. See the [LICENSE](LICENSE) file for the full legal terms.
