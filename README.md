# macOS Audio Output Source Manager & Scheduler

[![Platform: macOS](https://img.shields.io/badge/PLATFORM-MACOS-black.svg?style=for-the-badge&logo=apple)](#)
[![Status: Active](https://img.shields.io/badge/STATUS-OPERATIONAL-success.svg?style=for-the-badge)](#)
[![Engine: CoreAudio C Native](https://img.shields.io/badge/ENGINE-COREAUDIO_CTYPES-blue.svg?style=for-the-badge)](#)

A high-performance macOS audio manager, sound-blocking enforcer, and time-based output scheduler. Built with native CoreAudio C bindings, event-driven hardware listeners, and Quartz event taps.

---

## Key Features

- 🚫 **Genuine Virtual Sound Block (`Virtual`)**:
  - Automatically creates a genuine CoreAudio aggregate device named **`Virtual`** that appears directly in **macOS System Settings** and **Control Center**.
  - Routes audio to an internal silent HAL loopback sink (`BlackHole 2ch` or `Steam Streaming Speakers`).
  - Completely stops sound playback through physical speakers or headphones.
- ⚡ **Instant Event-Driven Override (<5ms latency)**:
  - Listens to hardware changes via `AudioObjectAddPropertyListener` on `kAudioHardwarePropertyDefaultOutputDevice`.
  - When a schedule is active, any attempt to switch output devices in macOS Control Center or System Settings is **snapped back within 1–5 milliseconds**.
- ⌨️ **Hardware Volume Key Interception (`CGEventTap`)**:
  - Intercepts physical media keys (`F11`, `F12`, `Mute`) via Quartz Event Services before macOS processes them.
  - Suppresses volume increases during sound block periods or fixed-volume schedules.
- 🔒 **Tamper Lock & Admin PIN Protection**:
  - Password-protect scheduler settings and device controls with an Admin PIN.
  - Displays a native macOS modal password prompt (`osascript display dialog with hidden answer`) over Control Center if unauthorized device changes occur.
- 🎚️ **Volume Bounds & Range Clamping**:
  - **Unlocked**: Normal volume control.
  - **Fixed Volume**: Enforces a strict percentage (e.g. `40%`), clamping any manual changes.
  - **Allowed Range**: Allows free adjustment within a safe bracket (e.g. `15% – 50%`), clamping if exceeded.
- 🔊 **Auto-Restore Previous Device & Volume**:
  - Remembers your original speaker/headphones and volume level before a schedule started, restoring them when the schedule ends.
- 🔋 **Zero Battery Drain When Idle**:
  - Uses event callbacks instead of busy-polling loops. Spawns zero subprocesses during idle states.
- ☁️ **Cloud Config Sync**:
  - Store schedule files in Dropbox, iCloud Drive, or local storage.

---

## Prerequisites & Installation

### 1. Automated Setup (Recommended)
Run the built-in setup command to install `switchaudio-osx` and `blackhole-2ch` via Homebrew:
```bash
./switch_audio.sh setup
```

### 2. Check System Dependencies
Verify that all components are detected and ready:
```bash
./switch_audio.sh check-deps
```

Expected output:
```text
Dependency Check:
  • SwitchAudioSource CLI : Available
  • BlackHole 2ch driver  : Available
  • PyObjC framework      : Available
  • Accessibility access  : Granted (or Not Granted)
```

---

## How to Grant macOS Accessibility Permission (Volume Key Interception)

> [!IMPORTANT]
> ### Crucial Note on macOS Security & Host Applications
> macOS attributes Accessibility permissions to the **host application** (the parent program) running the Python process:
> - **Running inside an IDE?** (e.g., **Antigravity IDE**, **Visual Studio Code**, **Cursor**, **PyCharm**):
>   You must grant Accessibility permission to the **IDE itself** (e.g., toggle ON **Antigravity IDE** or **Visual Studio Code**).
> - **Running inside a Terminal?** (e.g., **Terminal.app**, **iTerm2**, **Warp**, **Alacritty**):
>   You must grant Accessibility permission to your **terminal application** (e.g., toggle ON **Terminal** or **iTerm**).
> - **Running Python directly or as a standalone app?**
>   You must add the **Python application bundle** directly via the `+` button in Accessibility.
>
> **Quick Check:** Run `./switch_audio.sh check-deps` in your terminal anytime to immediately verify whether your current environment has Accessibility granted.

macOS requires **Accessibility** permission to intercept keyboard media keys (`F11`, `F12`, `Mute`) via `CGEventTap`.

> [!NOTE]
> **Is Accessibility mandatory?**
> **No.** If Accessibility is not granted, sound blocking to `Virtual` and instant audio device snapback still function 100%. The app will simply use continuous software volume clamping back to 0% rather than suppressing the physical keypress at the Quartz driver layer.

---

### Step-by-Step Setup Guide

#### Method 1: Enable Your Terminal or IDE (Recommended & Quickest)

1. Open **System Settings** → **Privacy & Security** → **Accessibility** (or run this shortcut in your terminal):
   ```bash
   open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
   ```
2. Locate the app you are using in the list:
   - **Antigravity IDE**
   - **Visual Studio Code**
   - **Terminal**
   - **iTerm**
3. Toggle the switch next to it **ON** (blue).
4. Restart the terminal or app if prompted.

---

#### Method 2: Add the Python App Directly

If macOS explicitly prompts for Python, or you launch the script outside a recognized terminal:

1. Open **System Settings** → **Privacy & Security** → **Accessibility**.
2. Click the **`+`** (Add) button below the application list (enter your Mac administrator password).
3. In the Finder file selector, press **`Cmd + Shift + G`** (*Go to Folder*).
4. Paste the path to your Python installation:
   - **Official Python 3.14 (macOS framework):**
     ```text
     /Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app
     ```
   - **Homebrew Python:**
     ```text
     /opt/homebrew/bin/python3
     ```
5. Click **Open**, and make sure the toggle switch next to **Python** is **ON**.

---

## How to Start & Use

### Option 1: Desktop GUI Application

Launch the GUI:
```bash
python3 gui.py
```

#### Inside the GUI:
1. **Current Output & Quick Switch**: View your active audio device and switch output sources instantly.
2. **Security & PIN Lock**: Click **🔑 Set / Change PIN** to protect your settings with an admin password. Click **🔒 Enable PIN Lock** to activate tamper protection.
3. **Configure Schedules**:
   - Click **➕ Add Schedule**.
   - Enter a name (e.g., *Deep Work* or *Night Block*).
   - Select the target output (choose **`Virtual (Block: Plays no sound)`** for complete silence).
   - Set start time, end time, and days of the week.
   - Configure volume enforcement (Unlocked, Fixed Volume, or Min/Max Range).
   - Choose on-end action (*Restore last used audio source & volume* or switch to a specific device).
4. **Start Scheduler**: Click **▶️ Start Scheduler**. The live CoreAudio hardware listener and override enforcer will run in the background.

---

### Option 2: Command Line Interface (`switch_audio.sh`)

Make the script executable:
```bash
chmod +x switch_audio.sh
```

#### Common Commands:

| Action | Command |
| :--- | :--- |
| **Check dependencies** | `./switch_audio.sh check-deps` |
| **List audio devices** | `./switch_audio.sh list` |
| **Check current device & volume** | `./switch_audio.sh current` |
| **Switch output immediately** | `./switch_audio.sh switch "Virtual"`<br>`./switch_audio.sh switch "MacBook Pro Speakers"` |
| **Run a single schedule** | `./switch_audio.sh schedule --target "Virtual" --start 14:00 --end 18:00` |
| **Schedule with volume clamp** | `./switch_audio.sh schedule --target "MacBook Pro Speakers" --start 14:00 --end 18:00 --min-vol 20 --max-vol 50` |
| **Run daemon on config file** | `./switch_audio.sh daemon --config ~/.macos_audio_scheduler/schedules.json` |

---

## Schedule Configuration Example (`schedules.json`)

Schedules are saved in JSON format:

```json
[
  {
    "id": "silent-focus",
    "name": "Silent Focus Block",
    "target_device": "Virtual",
    "start_time": "14:00",
    "end_time": "17:00",
    "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    "volume_mode": "fixed",
    "fixed_volume": 0,
    "min_volume": 0,
    "max_volume": 0,
    "end_action": "restore_previous",
    "return_device": "",
    "enabled": true
  },
  {
    "id": "safe-volume-study",
    "name": "Study Window (Volume Cap)",
    "target_device": "MacBook Pro Speakers",
    "start_time": "19:00",
    "end_time": "21:00",
    "days": ["Mon", "Wed", "Sat"],
    "volume_mode": "range",
    "fixed_volume": 35,
    "min_volume": 15,
    "max_volume": 45,
    "end_action": "restore_previous",
    "return_device": "",
    "enabled": true
  }
]
```

---

## Project Structure

```text
.
├── coreaudio_backend.py   # Native CoreAudio ctypes bindings, listeners & CGEventTap
├── gui.py                 # Desktop Tkinter GUI with instant event-driven enforcer
├── switch_audio.sh        # Standalone CLI switcher, runner & background daemon
├── schedules.json         # Schedule configuration file
├── LICENSE                # Non-Commercial License
└── README.md              # Project documentation and usage guide
```

---

## License

This project is licensed under a **Non-Commercial License** (Personal & Educational Use Only). Commercial use, resale, or monetization is strictly prohibited without prior written permission. See the [LICENSE](LICENSE) file for details.
