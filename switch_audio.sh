#!/usr/bin/env bash
# ==============================================================================
# switch_audio.sh — macOS Audio Output Switcher & Scheduler Engine (Optimized)
# ==============================================================================
# Features:
#   - Battery-optimized execution (event-friendly, low-overhead sleeping)
#   - Virtual sound device (Block: plays no sound)
#   - Specific volume enforcement or allowed volume range (min/max clamping)
#   - Standalone CLI execution & config-based background daemon
# ==============================================================================

set -eo pipefail

DEFAULT_CONFIG="$HOME/.macos_audio_scheduler/schedules.json"

# Find SwitchAudioSource binary
SWITCH_BIN=$(command -v SwitchAudioSource || echo "/opt/homebrew/bin/SwitchAudioSource")
if [[ ! -x "$SWITCH_BIN" ]]; then
    echo "Error: SwitchAudioSource not found. Please install: brew install switchaudio-osx" >&2
    exit 1
fi

VIRTUAL_NAME="Virtual"
VIRTUAL_LABEL="Virtual (Block - plays no sound)"

get_current() {
    "$SWITCH_BIN" -c -t output
}

list_devices() {
    echo "$VIRTUAL_LABEL"
    "$SWITCH_BIN" -a -t output
}

# Query volume and mute state via AppleScript
# Output: "VOL|MUTED" e.g. "50|false"
get_volume_state() {
    osascript -e '
        set vol to output volume of (get volume settings)
        set isMuted to output muted of (get volume settings)
        return (vol as text) & "|" & (isMuted as text)
    ' 2>/dev/null || echo "50|false"
}

set_volume_state() {
    local vol="$1"
    local muted="$2"
    if [[ "$muted" == "true" ]]; then
        osascript -e "set volume output volume $vol with output muted" 2>/dev/null || true
    else
        osascript -e "set volume output volume $vol without output muted" 2>/dev/null || true
    fi
}

mute_block() {
    osascript -e "set volume output volume 0 with output muted" 2>/dev/null || true
}

switch_device() {
    local target="$1"
    if [[ -z "$target" ]]; then
        return 1
    fi
    if [[ "$target" == "$VIRTUAL_NAME" || "$target" == "$VIRTUAL_LABEL" ]]; then
        echo "[$(date +'%T')] Activating Virtual Sound Block (plays no sound)..."
        mute_block
    else
        echo "[$(date +'%T')] Switching audio output to: '$target'"
        "$SWITCH_BIN" -s "$target" -t output
    fi
}

# Enforce volume constraints (fixed level or min/max range)
# Returns 0 if within bounds, 1 if clamped
enforce_volume_constraints() {
    local mode="$1"       # "unlocked", "fixed", or "range"
    local fixed_vol="$2"  # number 0-100
    local min_vol="$3"    # number 0-100
    local max_vol="$4"    # number 0-100

    if [[ "$mode" == "unlocked" || -z "$mode" ]]; then
        return 0
    fi

    local vol_state
    vol_state=$(get_volume_state)
    IFS='|' read -r curr_vol curr_muted <<< "$vol_state"

    if [[ "$mode" == "fixed" ]]; then
        if [[ "$fixed_vol" == "0" ]]; then
            if [[ "$curr_vol" != "0" || "$curr_muted" != "true" ]]; then
                echo "[$(date +'%T')] [OVERRIDE] Sound change detected on Virtual. Re-blocking to 0% muted..."
                mute_block
                return 1
            fi
        else
            if [[ "$curr_vol" != "$fixed_vol" || "$curr_muted" == "true" ]]; then
                echo "[$(date +'%T')] [OVERRIDE] Volume changed to $curr_vol%. Forcing fixed volume: $fixed_vol%..."
                set_volume_state "$fixed_vol" "false"
                return 1
            fi
        fi
    elif [[ "$mode" == "range" ]]; then
        if [[ "$curr_muted" == "true" && "$min_vol" -gt 0 ]]; then
            echo "[$(date +'%T')] [OVERRIDE] Sound was muted. Clamping to minimum volume: $min_vol%..."
            set_volume_state "$min_vol" "false"
            return 1
        elif [[ "$curr_vol" -lt "$min_vol" ]]; then
            echo "[$(date +'%T')] [OVERRIDE] Volume ($curr_vol%) below minimum ($min_vol%). Clamping to $min_vol%..."
            set_volume_state "$min_vol" "false"
            return 1
        elif [[ "$curr_vol" -gt "$max_vol" ]]; then
            echo "[$(date +'%T')] [OVERRIDE] Volume ($curr_vol%) exceeded maximum ($max_vol%). Clamping to $max_vol%..."
            set_volume_state "$max_vol" "false"
            return 1
        fi
    fi
    return 0
}

# ------------------------------------------------------------------------------
# Single schedule command
# ------------------------------------------------------------------------------
run_single_schedule() {
    local target=""
    local start_time=""
    local end_time=""
    local days=""
    local return_target=""
    local volume_mode="unlocked"
    local fixed_vol="50"
    local min_vol="0"
    local max_vol="100"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target|-t)   target="$2"; shift 2 ;;
            --start|-s)    start_time="$2"; shift 2 ;;
            --end|-e)      end_time="$2"; shift 2 ;;
            --days|-d)     days="$2"; shift 2 ;;
            --return|-r)   return_target="$2"; shift 2 ;;
            --volume|-v)   volume_mode="fixed"; fixed_vol="$2"; shift 2 ;;
            --min-vol)     volume_mode="range"; min_vol="$2"; shift 2 ;;
            --max-vol)     volume_mode="range"; max_vol="$2"; shift 2 ;;
            *) echo "Unknown option: $1" >&2; exit 1 ;;
        esac
    done

    if [[ -z "$target" || -z "$start_time" || -z "$end_time" ]]; then
        echo "Usage: $0 schedule --target <device> --start <HH:MM> --end <HH:MM> [options]"
        echo "Options:"
        echo "  --days Mon,Wed         Filter active days"
        echo "  --return <device>      Specific device to restore"
        echo "  --volume <0-100>       Lock to a specific fixed volume"
        echo "  --min-vol <0-100>      Set minimum allowed volume"
        echo "  --max-vol <0-100>      Set maximum allowed volume"
        exit 1
    fi

    if [[ "$target" == "$VIRTUAL_LABEL" || "$target" == "$VIRTUAL_NAME" ]]; then
        target="$VIRTUAL_NAME"
        volume_mode="fixed"
        fixed_vol="0"
    fi

    echo "=========================================================="
    echo " Audio Schedule Monitor (Battery-Optimized)"
    echo " Target: '$target'"
    if [[ "$target" == "$VIRTUAL_NAME" ]]; then
        echo " Mode:   Virtual Sound Block (0% muted, sound blocked)"
    elif [[ "$volume_mode" == "fixed" ]]; then
        echo " Volume: Fixed at $fixed_vol%"
    elif [[ "$volume_mode" == "range" ]]; then
        echo " Volume: Range allowed: $min_vol% - $max_vol%"
    fi
    echo " Window: $start_time -> $end_time (24h)"
    echo " Days:   ${days:-Everyday}"
    echo " Return: ${return_target:-[Auto: restore last used device & volume]}"
    echo "=========================================================="

    local previous_device=""
    local previous_vol_state=""
    local active=false

    cleanup() {
        if $active; then
            echo -e "\nInterrupted. Restoring sound state..."
            if [[ -n "$previous_vol_state" ]]; then
                IFS='|' read -r orig_vol orig_muted <<< "$previous_vol_state"
                set_volume_state "$orig_vol" "$orig_muted"
            fi
            local restore_to="${return_target:-$previous_device}"
            if [[ -n "$restore_to" && "$restore_to" != "$VIRTUAL_NAME" ]]; then
                switch_device "$restore_to" || true
            fi
        fi
        exit 0
    }
    trap cleanup INT TERM

    while true; do
        current_time=$(date +"%H:%M")
        current_day=$(date +"%a")

        day_match=true
        if [[ -n "$days" && ",$days," != *",$current_day,"* ]]; then
            day_match=false
        fi

        in_window=false
        if $day_match; then
            if [[ "$start_time" < "$end_time" || "$start_time" == "$end_time" ]]; then
                if [[ "$current_time" > "$start_time" || "$current_time" == "$start_time" ]] && [[ "$current_time" < "$end_time" ]]; then
                    in_window=true
                fi
            else
                if [[ "$current_time" > "$start_time" || "$current_time" == "$start_time" ]] || [[ "$current_time" < "$end_time" ]]; then
                    in_window=true
                fi
            fi
        fi

        if $in_window; then
            if ! $active; then
                previous_device=$(get_current)
                previous_vol_state=$(get_volume_state)
                echo "[$(date +'%T')] Window started. Saved device='$previous_device', vol='$previous_vol_state'"
                switch_device "$target"
                enforce_volume_constraints "$volume_mode" "$fixed_vol" "$min_vol" "$max_vol" || true
                active=true
            else
                # Active enforcement: check output device & volume
                if [[ "$target" != "$VIRTUAL_NAME" ]]; then
                    curr_dev=$(get_current)
                    if [[ "$curr_dev" != "$target" ]]; then
                        echo "[$(date +'%T')] [OVERRIDE] Output changed to '$curr_dev'. Forcing back to '$target'..."
                        "$SWITCH_BIN" -s "$target" -t output || true
                    fi
                fi
                enforce_volume_constraints "$volume_mode" "$fixed_vol" "$min_vol" "$max_vol" || true
            fi
            sleep 2
        else
            if $active; then
                echo "[$(date +'%T')] Window ended. Restoring original audio state..."
                if [[ -n "$previous_vol_state" ]]; then
                    IFS='|' read -r orig_vol orig_muted <<< "$previous_vol_state"
                    echo "[$(date +'%T')] Restoring volume to $orig_vol% (muted: $orig_muted)..."
                    set_volume_state "$orig_vol" "$orig_muted"
                fi
                local restore_to="${return_target:-$previous_device}"
                if [[ -n "$restore_to" && "$restore_to" != "$VIRTUAL_NAME" ]]; then
                    switch_device "$restore_to"
                fi
                active=false
                echo "Schedule completed."
                break
            fi
            # Battery optimization: sleep 10s when idle
            sleep 10
        fi
    done
}

# ------------------------------------------------------------------------------
# Config file daemon (Battery Optimized)
# ------------------------------------------------------------------------------
run_daemon() {
    local config_file="$DEFAULT_CONFIG"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --config|-c) config_file="$2"; shift 2 ;;
            *) echo "Unknown option: $1" >&2; exit 1 ;;
        esac
    done

    config_file="${config_file/#\~/$HOME}"

    echo "=========================================================="
    echo " macOS Audio Output Scheduler Daemon (Battery-Optimized)"
    echo " Config Path: '$config_file'"
    echo " (Press Ctrl+C to stop)"
    echo "=========================================================="

    local active_sched_id=""
    local previous_device=""
    local previous_vol_state=""

    cleanup() {
        if [[ -n "$active_sched_id" ]]; then
            echo -e "\nStopping daemon. Restoring sound state..."
            if [[ -n "$previous_vol_state" ]]; then
                IFS='|' read -r orig_vol orig_muted <<< "$previous_vol_state"
                set_volume_state "$orig_vol" "$orig_muted"
            fi
            if [[ -n "$previous_device" && "$previous_device" != "$VIRTUAL_NAME" ]]; then
                switch_device "$previous_device" || true
            fi
        fi
        exit 0
    }
    trap cleanup INT TERM

    while true; do
        if [[ -f "$config_file" ]]; then
            current_time=$(date +"%H:%M")
            current_day=$(date +"%a")

            # Parse schedules from config
            eval_result=$(python3 -c "
import json, sys

try:
    with open('$config_file', 'r') as f:
        schedules = json.load(f)
except Exception:
    sys.exit(0)

now_t = '$current_time'
now_d = '$current_day'

matching = None
for s in schedules:
    if not s.get('enabled', True):
        continue
    days = s.get('days', [])
    if days and now_d not in days:
        continue
    st = s.get('start_time', '')
    et = s.get('end_time', '')
    if not st or not et:
        continue
    if st <= et:
        in_win = (st <= now_t < et)
    else:
        in_win = (now_t >= st or now_t < et)
    if in_win:
        matching = s
        break

if matching:
    t = matching.get('target_device', '')
    if 'Virtual' in t:
        t = 'Virtual'
    v_mode = matching.get('volume_mode', 'unlocked')
    f_vol = matching.get('fixed_volume', 50)
    min_v = matching.get('min_volume', 0)
    max_v = matching.get('max_volume', 100)
    if t == 'Virtual':
        v_mode = 'fixed'
        f_vol = 0
    print(f'MATCH|{matching.get(\"id\",\"default\")}|{t}|{matching.get(\"end_action\",\"restore_previous\")}|{matching.get(\"return_device\",\"\")}|{v_mode}|{f_vol}|{min_v}|{max_v}')
else:
    print('NONE')
" 2>/dev/null || echo "NONE")

            if [[ "$eval_result" == MATCH* ]]; then
                IFS='|' read -r _ sched_id target_dev end_action return_dev v_mode f_vol min_v max_v <<< "$eval_result"

                if [[ "$active_sched_id" != "$sched_id" ]]; then
                    if [[ -z "$active_sched_id" ]]; then
                        previous_device=$(get_current)
                        previous_vol_state=$(get_volume_state)
                        echo "[$(date +'%T')] Schedule started: '$sched_id'. Saved device='$previous_device', vol='$previous_vol_state'"
                    fi
                    active_sched_id="$sched_id"
                    switch_device "$target_dev"
                    enforce_volume_constraints "$v_mode" "$f_vol" "$min_v" "$max_v" || true
                else
                    # Active enforcement during schedule
                    if [[ "$target_dev" != "$VIRTUAL_NAME" ]]; then
                        curr_dev=$(get_current)
                        if [[ "$curr_dev" != "$target_dev" ]]; then
                            echo "[$(date +'%T')] [OVERRIDE] Output changed to '$curr_dev'. Forcing back to '$target_dev'..."
                            "$SWITCH_BIN" -s "$target_dev" -t output || true
                        fi
                    fi
                    enforce_volume_constraints "$v_mode" "$f_vol" "$min_v" "$max_v" || true
                fi
                sleep 2
                continue
            elif [[ -n "$active_sched_id" ]]; then
                echo "[$(date +'%T')] Active schedule window ended."
                if [[ -n "$previous_vol_state" ]]; then
                    IFS='|' read -r orig_vol orig_muted <<< "$previous_vol_state"
                    echo "[$(date +'%T')] Restoring volume to $orig_vol% (muted: $orig_muted)..."
                    set_volume_state "$orig_vol" "$orig_muted"
                fi
                local restore_to="$previous_device"
                if [[ -n "$return_dev" && "$end_action" == "specific_device" ]]; then
                    restore_to="$return_dev"
                fi
                if [[ -n "$restore_to" && "$end_action" != "do_nothing" && "$restore_to" != "$VIRTUAL_NAME" ]]; then
                    echo "[$(date +'%T')] Restoring output device to: '$restore_to'"
                    switch_device "$restore_to"
                fi
                active_sched_id=""
                previous_device=""
                previous_vol_state=""
            fi
        fi

        # Battery-friendly: sleep 10s when no schedule is active
        sleep 10
    done
}

# ------------------------------------------------------------------------------
# Main Dispatcher
# ------------------------------------------------------------------------------
case "${1:-}" in
    list)
        echo "Available Audio Output Devices:"
        list_devices | sed 's/^/  • /'
        ;;
    current)
        echo "Current Output Device: $(get_current)"
        vol_info=$(get_volume_state)
        IFS='|' read -r v m <<< "$vol_info"
        echo "Current Volume: ${v}% (Muted: ${m})"
        ;;
    switch)
        shift
        switch_device "$1"
        ;;
    schedule)
        shift
        run_single_schedule "$@"
        ;;
    daemon)
        shift
        run_daemon "$@"
        ;;
    *)
        echo "macOS Audio Output Switcher & Scheduler (Battery-Optimized)"
        echo ""
        echo "Commands:"
        echo "  $0 list                                            List detected audio output devices (incl. Virtual)"
        echo "  $0 current                                         Display current output device and volume"
        echo "  $0 switch \"<Device>\"                               Switch to device immediately"
        echo "  $0 schedule --target \"<Dev>\" --start 14:00 --end 18:00 [options]"
        echo "  $0 daemon [--config \"/path/to/schedules.json\"]    Run background monitor with active override"
        echo ""
        echo "Volume Control Options for 'schedule':"
        echo "  --volume <0-100>       Lock to a specific fixed volume level"
        echo "  --min-vol <0-100>      Set a minimum volume threshold (clamps if reduced below)"
        echo "  --max-vol <0-100>      Set a maximum volume threshold (clamps if increased above)"
        echo ""
        echo "Examples:"
        echo "  $0 schedule --target \"MacBook Pro Speakers\" --start 14:00 --end 18:00 --min-vol 20 --max-vol 50"
        echo "  $0 schedule --target \"Virtual\" --start 14:00 --end 18:00"
        ;;
esac
