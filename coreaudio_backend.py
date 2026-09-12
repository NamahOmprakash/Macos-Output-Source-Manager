#!/usr/bin/env python3
"""CoreAudio Native Backend — Direct ctypes bindings for macOS audio management.

Provides:
- Device enumeration (name, UID, output channel count)
- Get/set default output device via CoreAudio API
- Aggregate device create/destroy (visible in System Settings)
- Property listener for instant default-output-device change detection
- CGEventTap for intercepting volume key events (requires Accessibility permission)
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
import threading
import time
from typing import Callable, NamedTuple

# ==============================================================================
# Framework Handles
# ==============================================================================
_coreaudio = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
)
_cf = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)

# Optional: HIServices for AXIsProcessTrusted
try:
    _hiservices = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/"
        "Frameworks/HIServices.framework/HIServices"
    )
except OSError:
    _hiservices = None

# Optional: ApplicationServices for CGEventTap
try:
    _appservices = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
except OSError:
    _appservices = None


# ==============================================================================
# CoreAudio Constants (from AudioHardware.h)
# ==============================================================================
kAudioObjectSystemObject: int = 1
kAudioObjectPropertyScopeGlobal: int = int.from_bytes(b"glob", "big")
kAudioObjectPropertyScopeOutput: int = int.from_bytes(b"outp", "big")
kAudioObjectPropertyElementMain: int = 0

# Hardware properties
kAudioHardwarePropertyDevices: int = int.from_bytes(b"dev#", "big")
kAudioHardwarePropertyDefaultOutputDevice: int = int.from_bytes(b"dOut", "big")
kAudioHardwarePropertyDefaultSystemOutputDevice: int = int.from_bytes(b"sOut", "big")

# Device properties
kAudioDevicePropertyDeviceUID: int = int.from_bytes(b"uid ", "big")
kAudioDevicePropertyDeviceNameCFString: int = int.from_bytes(b"lnam", "big")
kAudioDevicePropertyStreamConfiguration: int = int.from_bytes(b"slay", "big")
kAudioDevicePropertyStreams: int = int.from_bytes(b"stm#", "big")

# CGEventTap constants
kCGSessionEventTap: int = 1
kCGHeadInsertEventTap: int = 0
kCGEventTapOptionDefault: int = 0  # active tap (can modify/suppress events)
NX_SYSDEFINED: int = 14
NX_KEYTYPE_SOUND_UP: int = 0
NX_KEYTYPE_SOUND_DOWN: int = 1
NX_KEYTYPE_MUTE: int = 7

# CFRunLoop
kCFRunLoopCommonModes = ctypes.c_void_p.in_dll(_cf, "kCFRunLoopCommonModes")


# ==============================================================================
# CoreAudio Structures
# ==============================================================================
class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


class AudioBufferList(ctypes.Structure):
    """Simplified — we only need mNumberBuffers to count output channels."""

    class AudioBuffer(ctypes.Structure):
        _fields_ = [
            ("mNumberChannels", ctypes.c_uint32),
            ("mDataByteSize", ctypes.c_uint32),
            ("mData", ctypes.c_void_p),
        ]

    _fields_ = [
        ("mNumberBuffers", ctypes.c_uint32),
        ("mBuffers", AudioBuffer * 1),  # variable length, we'll read dynamically
    ]


# ==============================================================================
# Callback Types
# ==============================================================================
# AudioObjectPropertyListenerProc:
#   OSStatus (*)(AudioObjectID, UInt32, const AudioObjectPropertyAddress*, void*)
LISTENER_PROC_TYPE = ctypes.CFUNCTYPE(
    ctypes.c_int32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.POINTER(AudioObjectPropertyAddress),
    ctypes.c_void_p,
)

# CGEventTapCallBack:
#   CGEventRef (*)(CGEventTapProxy, CGEventType, CGEventRef, void*)
CGEVENT_TAP_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_void_p,  # return CGEventRef (or NULL to suppress)
    ctypes.c_void_p,  # proxy
    ctypes.c_uint32,  # type
    ctypes.c_void_p,  # event
    ctypes.c_void_p,  # userInfo
)


# ==============================================================================
# CoreFoundation Helpers
# ==============================================================================
def _setup_cf_functions() -> None:
    """Configure CoreFoundation function signatures."""
    _cf.CFStringGetLength.restype = ctypes.c_long
    _cf.CFStringGetLength.argtypes = [ctypes.c_void_p]

    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]

    _cf.CFRelease.restype = None
    _cf.CFRelease.argtypes = [ctypes.c_void_p]

    _cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    _cf.CFRunLoopGetCurrent.argtypes = []

    _cf.CFRunLoopAddSource.restype = None
    _cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

    _cf.CFRunLoopRemoveSource.restype = None
    _cf.CFRunLoopRemoveSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]

    _cf.CFRunLoopRun.restype = None
    _cf.CFRunLoopRun.argtypes = []

    _cf.CFRunLoopStop.restype = None
    _cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]

    _cf.CFMachPortInvalidate.restype = None
    _cf.CFMachPortInvalidate.argtypes = [ctypes.c_void_p]

    _cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
    _cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]


_setup_cf_functions()

kCFStringEncodingUTF8: int = 0x08000100


def _cfstring_to_str(cf_str: ctypes.c_void_p | int) -> str:
    """Convert a CFStringRef to a Python string."""
    ptr = cf_str if isinstance(cf_str, int) else (cf_str.value if hasattr(cf_str, "value") else cf_str)
    if not ptr:
        return ""
    buf = ctypes.create_string_buffer(512)
    ok = _cf.CFStringGetCString(ptr, buf, 512, kCFStringEncodingUTF8)
    if ok:
        return buf.value.decode("utf-8")
    return ""


# ==============================================================================
# CoreAudio Function Signatures
# ==============================================================================
def _setup_coreaudio_functions() -> None:
    """Configure CoreAudio function signatures."""
    _coreaudio.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    _coreaudio.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]

    _coreaudio.AudioObjectGetPropertyData.restype = ctypes.c_int32
    _coreaudio.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]

    _coreaudio.AudioObjectSetPropertyData.restype = ctypes.c_int32
    _coreaudio.AudioObjectSetPropertyData.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]

    _coreaudio.AudioObjectAddPropertyListener.restype = ctypes.c_int32
    _coreaudio.AudioObjectAddPropertyListener.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        LISTENER_PROC_TYPE,
        ctypes.c_void_p,
    ]

    _coreaudio.AudioObjectRemovePropertyListener.restype = ctypes.c_int32
    _coreaudio.AudioObjectRemovePropertyListener.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        LISTENER_PROC_TYPE,
        ctypes.c_void_p,
    ]

    _coreaudio.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
    _coreaudio.AudioHardwareCreateAggregateDevice.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]

    _coreaudio.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32
    _coreaudio.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]


_setup_coreaudio_functions()


# ==============================================================================
# Data Types
# ==============================================================================
class AudioDevice(NamedTuple):
    """Represents a macOS audio output device."""

    device_id: int
    name: str
    uid: str
    output_channels: int


# ==============================================================================
# Device Enumeration
# ==============================================================================
def get_all_devices() -> list[AudioDevice]:
    """Returns all audio devices registered in CoreAudio."""
    addr = AudioObjectPropertyAddress(
        kAudioHardwarePropertyDevices,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    size = ctypes.c_uint32(0)
    err = _coreaudio.AudioObjectGetPropertyDataSize(
        kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size)
    )
    if err != 0:
        return []

    count = size.value // ctypes.sizeof(ctypes.c_uint32)
    if count == 0:
        return []

    device_ids = (ctypes.c_uint32 * count)()
    err = _coreaudio.AudioObjectGetPropertyData(
        kAudioObjectSystemObject,
        ctypes.byref(addr),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(device_ids),
    )
    if err != 0:
        return []

    devices: list[AudioDevice] = []
    for dev_id in device_ids:
        name = _get_device_string_property(dev_id, kAudioDevicePropertyDeviceNameCFString)
        uid = _get_device_string_property(dev_id, kAudioDevicePropertyDeviceUID)
        out_ch = _get_device_output_channels(dev_id)
        devices.append(AudioDevice(device_id=dev_id, name=name, uid=uid, output_channels=out_ch))

    return devices


def get_output_devices() -> list[AudioDevice]:
    """Returns only devices with at least 1 output channel."""
    return [d for d in get_all_devices() if d.output_channels > 0]


def find_device_by_name(name: str) -> AudioDevice | None:
    """Find a device by its exact name."""
    for d in get_all_devices():
        if d.name == name:
            return d
    return None


def find_device_by_uid(uid: str) -> AudioDevice | None:
    """Find a device by its UID."""
    for d in get_all_devices():
        if d.uid == uid:
            return d
    return None


def find_blackhole_device() -> AudioDevice | None:
    """Find the BlackHole 2ch device by name pattern."""
    for d in get_all_devices():
        if "BlackHole" in d.name and d.output_channels > 0:
            return d
    return None


def find_virtual_sink() -> AudioDevice | None:
    """Find a suitable silent virtual sink device (BlackHole or Steam Streaming Speakers)."""
    candidates = ["BlackHole 2ch", "BlackHole 16ch", "Steam Streaming Speakers"]
    all_devs = get_all_devices()
    for candidate_name in candidates:
        for d in all_devs:
            if d.name == candidate_name and d.output_channels > 0:
                return d
    return None


def _get_device_string_property(device_id: int, selector: int) -> str:
    """Get a CFString property from a device."""
    addr = AudioObjectPropertyAddress(
        selector, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain
    )
    cf_str = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    err = _coreaudio.AudioObjectGetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(cf_str)
    )
    if err != 0 or not cf_str.value:
        return ""
    result = _cfstring_to_str(cf_str.value)
    _cf.CFRelease(cf_str.value)
    return result


def _get_device_output_channels(device_id: int) -> int:
    """Count the number of output channels for a device."""
    addr = AudioObjectPropertyAddress(
        kAudioDevicePropertyStreamConfiguration,
        kAudioObjectPropertyScopeOutput,
        kAudioObjectPropertyElementMain,
    )
    size = ctypes.c_uint32(0)
    err = _coreaudio.AudioObjectGetPropertyDataSize(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size)
    )
    if err != 0 or size.value == 0:
        return 0

    buf = (ctypes.c_byte * size.value)()
    err = _coreaudio.AudioObjectGetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(buf)
    )
    if err != 0:
        return 0

    # Parse AudioBufferList on ARM64:
    #   struct AudioBufferList {
    #       UInt32 mNumberBuffers;       // 4 bytes at offset 0
    #       // 4 bytes padding (alignment for pointer in AudioBuffer)
    #       AudioBuffer mBuffers[];      // starts at offset 8
    #   };
    #   struct AudioBuffer {
    #       UInt32 mNumberChannels;      // 4 bytes
    #       UInt32 mDataByteSize;        // 4 bytes
    #       void*  mData;               // 8 bytes (ARM64)
    #   };  // total = 16 bytes per buffer
    raw = bytes(buf)
    num_buffers = struct.unpack_from("I", raw, 0)[0]
    total_channels = 0
    ptr_size = ctypes.sizeof(ctypes.c_void_p)
    audio_buffer_size = 4 + 4 + ptr_size  # 16 on ARM64, 12 on x86_64
    # Padding after mNumberBuffers: aligns to pointer size
    header_size = 4 + (ptr_size - 4) if ptr_size > 4 else 4  # 8 on ARM64, 4 on x86_64
    offset = header_size
    for _ in range(num_buffers):
        if offset + audio_buffer_size > len(raw):
            break
        channels = struct.unpack_from("I", raw, offset)[0]
        total_channels += channels
        offset += audio_buffer_size

    return total_channels


# ==============================================================================
# Default Output Device Get/Set
# ==============================================================================
def get_default_output_device_id() -> int:
    """Returns the AudioObjectID of the current default output device."""
    addr = AudioObjectPropertyAddress(
        kAudioHardwarePropertyDefaultOutputDevice,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    dev_id = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
    err = _coreaudio.AudioObjectGetPropertyData(
        kAudioObjectSystemObject,
        ctypes.byref(addr),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(dev_id),
    )
    if err != 0:
        return 0
    return dev_id.value


def get_default_output_device() -> AudioDevice | None:
    """Returns the AudioDevice for the current default output."""
    dev_id = get_default_output_device_id()
    if dev_id == 0:
        return None
    name = _get_device_string_property(dev_id, kAudioDevicePropertyDeviceNameCFString)
    uid = _get_device_string_property(dev_id, kAudioDevicePropertyDeviceUID)
    out_ch = _get_device_output_channels(dev_id)
    return AudioDevice(device_id=dev_id, name=name, uid=uid, output_channels=out_ch)


def set_default_output_device(device_id: int) -> bool:
    """Sets the default output device by AudioObjectID. Returns True on success."""
    addr = AudioObjectPropertyAddress(
        kAudioHardwarePropertyDefaultOutputDevice,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    dev = ctypes.c_uint32(device_id)
    err = _coreaudio.AudioObjectSetPropertyData(
        kAudioObjectSystemObject,
        ctypes.byref(addr),
        0,
        None,
        ctypes.sizeof(ctypes.c_uint32),
        ctypes.byref(dev),
    )
    return err == 0


def set_default_output_by_name(name: str) -> bool:
    """Sets the default output device by name. Returns True on success."""
    dev = find_device_by_name(name)
    if dev is None:
        return False
    return set_default_output_device(dev.device_id)


# ==============================================================================
# Aggregate Device Management
# ==============================================================================
_aggregate_device_id: int | None = None
_AGGREGATE_UID = "org.soundblock.virtual.v3"
_AGGREGATE_NAME = "Virtual"


def create_virtual_device(blackhole_uid: str | None = None) -> int | None:
    """Creates a visible aggregate device named 'Virtual' backed by BlackHole.

    Args:
        blackhole_uid: UID of the BlackHole device. If None, auto-discovers it.

    Returns:
        The AudioObjectID of the created device, or None on failure.
    """
    global _aggregate_device_id

    # Check if already exists
    existing = find_device_by_name(_AGGREGATE_NAME)
    if existing and existing.output_channels > 0:
        _aggregate_device_id = existing.device_id
        return existing.device_id

    # Discover BlackHole UID
    if blackhole_uid is None:
        bh = find_blackhole_device()
        if bh is None:
            return None
        blackhole_uid = bh.uid

    if not blackhole_uid:
        return None

    try:
        import Foundation
        import objc

        desc = Foundation.NSDictionary.dictionaryWithDictionary_(
            {
                "uid": _AGGREGATE_UID,
                "name": _AGGREGATE_NAME,
                "private": 0,  # Visible in System Settings & Control Center
                "stacked": 0,
                "subdevices": [{"uid": blackhole_uid}],
                "master": blackhole_uid,
            }
        )

        out_id = ctypes.c_uint32(0)
        err = _coreaudio.AudioHardwareCreateAggregateDevice(
            objc.pyobjc_id(desc), ctypes.byref(out_id)
        )
        if err == 0 and out_id.value != 0:
            _aggregate_device_id = out_id.value
            # Wait for CoreAudio to finish publishing the aggregate device
            for _ in range(10):
                dev = find_device_by_name(_AGGREGATE_NAME)
                if dev and dev.output_channels > 0:
                    break
                time.sleep(0.05)
            return out_id.value
    except ImportError:
        pass
    except Exception:
        pass

    return None


def destroy_virtual_device() -> bool:
    """Destroys the Virtual aggregate device if it was created by this session."""
    global _aggregate_device_id

    if _aggregate_device_id is None:
        # Try to find it by name
        dev = find_device_by_name(_AGGREGATE_NAME)
        if dev is None:
            return True  # Already gone
        _aggregate_device_id = dev.device_id

    err = _coreaudio.AudioHardwareDestroyAggregateDevice(_aggregate_device_id)
    if err == 0:
        _aggregate_device_id = None
        return True
    return False


def get_virtual_device_id() -> int | None:
    """Returns the AudioObjectID of the Virtual device, or None if not created."""
    return _aggregate_device_id


# ==============================================================================
# Property Listener — Instant Device Change Detection
# ==============================================================================
class DeviceChangeListener:
    """Listens for default output device changes via CoreAudio property listener.

    The callback fires within ~1-5ms of any device change (Control Center,
    System Settings, SwitchAudioSource CLI, or any other source).
    """

    def __init__(self, on_change: Callable[[int], None]) -> None:
        """
        Args:
            on_change: Called with the new device_id whenever the default output changes.
        """
        self._on_change = on_change
        self._active = False
        self._addr = AudioObjectPropertyAddress(
            kAudioHardwarePropertyDefaultOutputDevice,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        )

        # Must keep a reference to prevent garbage collection
        self._callback = LISTENER_PROC_TYPE(self._listener_callback)

    def _listener_callback(
        self,
        obj_id: int,
        num_addresses: int,
        addresses,
        client_data,
    ) -> int:
        """Called by CoreAudio on the audio thread when default output changes."""
        try:
            new_dev_id = get_default_output_device_id()
            self._on_change(new_dev_id)
        except Exception:
            pass
        return 0

    def start(self) -> bool:
        """Register the property listener. Returns True on success."""
        if self._active:
            return True
        err = _coreaudio.AudioObjectAddPropertyListener(
            kAudioObjectSystemObject,
            ctypes.byref(self._addr),
            self._callback,
            None,
        )
        if err == 0:
            self._active = True
            return True
        return False

    def stop(self) -> None:
        """Remove the property listener."""
        if not self._active:
            return
        _coreaudio.AudioObjectRemovePropertyListener(
            kAudioObjectSystemObject,
            ctypes.byref(self._addr),
            self._callback,
            None,
        )
        self._active = False

    @property
    def active(self) -> bool:
        return self._active


# ==============================================================================
# CGEventTap — Volume Key Interception
# ==============================================================================
def is_accessibility_trusted() -> bool:
    """Check if this process has Accessibility permission (needed for CGEventTap)."""
    if _hiservices is None:
        return False
    try:
        _hiservices.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(_hiservices.AXIsProcessTrusted())
    except Exception:
        return False


class VolumeKeyInterceptor:
    """Intercepts and optionally suppresses volume key events (F11/F12/Mute).

    Requires Accessibility permission. When active, volume key presses are
    intercepted before macOS processes them. The provided callback can decide
    whether to suppress each event.
    """

    def __init__(self, should_suppress: Callable[[], bool]) -> None:
        """
        Args:
            should_suppress: Called on each volume key event. Return True to
                             suppress the key press, False to let it through.
        """
        self._should_suppress = should_suppress
        self._tap: ctypes.c_void_p | None = None
        self._runloop_source: ctypes.c_void_p | None = None
        self._thread: threading.Thread | None = None
        self._runloop_ref: ctypes.c_void_p | None = None
        self._active = False
        self._lock = threading.Lock()

        # Must keep reference to prevent GC
        self._callback = CGEVENT_TAP_CALLBACK(self._event_tap_callback)

    def _event_tap_callback(
        self,
        proxy: ctypes.c_void_p,
        event_type: int,
        event: ctypes.c_void_p,
        user_info: ctypes.c_void_p,
    ) -> ctypes.c_void_p:
        """Called by the CGEventTap on every system-defined event."""
        if event_type == NX_SYSDEFINED and event:
            try:
                # Get the data1 field to identify volume keys
                _appservices.CGEventGetIntegerValueField.restype = ctypes.c_int64
                _appservices.CGEventGetIntegerValueField.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                ]
                # Field 87 = kCGEventSourceUnixProcessID equivalent for data1
                data1 = _appservices.CGEventGetIntegerValueField(event, 87)

                # data1 layout for media keys:
                # Bits 16-23: key code (0=sound up, 1=sound down, 7=mute)
                # Bits 8-15: flags
                key_code = (data1 >> 16) & 0xFF

                is_volume_key = key_code in (
                    NX_KEYTYPE_SOUND_UP,
                    NX_KEYTYPE_SOUND_DOWN,
                    NX_KEYTYPE_MUTE,
                )

                if is_volume_key and self._should_suppress():
                    # Return NULL to suppress the event
                    return None
            except Exception:
                pass

        # Return the event unmodified
        return event

    def start(self) -> bool:
        """Start intercepting volume key events. Returns True on success."""
        if self._active:
            return True

        if not is_accessibility_trusted():
            return False

        if _appservices is None:
            return False

        with self._lock:
            try:
                # CGEventTapCreate
                _appservices.CGEventTapCreate.restype = ctypes.c_void_p
                _appservices.CGEventTapCreate.argtypes = [
                    ctypes.c_uint32,  # tap location
                    ctypes.c_uint32,  # place
                    ctypes.c_uint32,  # options
                    ctypes.c_uint64,  # events of interest mask
                    CGEVENT_TAP_CALLBACK,  # callback
                    ctypes.c_void_p,  # user info
                ]

                # Event mask: bit 14 = NX_SYSDEFINED
                event_mask = 1 << NX_SYSDEFINED

                self._tap = _appservices.CGEventTapCreate(
                    kCGSessionEventTap,
                    kCGHeadInsertEventTap,
                    kCGEventTapOptionDefault,
                    event_mask,
                    self._callback,
                    None,
                )

                if not self._tap:
                    return False

                self._runloop_source = _cf.CFMachPortCreateRunLoopSource(
                    None, self._tap, 0
                )

                if not self._runloop_source:
                    _cf.CFMachPortInvalidate(self._tap)
                    self._tap = None
                    return False

                # Run the event tap on a dedicated thread with its own run loop
                self._active = True
                self._thread = threading.Thread(
                    target=self._run_loop_thread, daemon=True
                )
                self._thread.start()
                return True

            except Exception:
                self._active = False
                return False

    def _run_loop_thread(self) -> None:
        """Background thread that runs the CFRunLoop for the event tap."""
        try:
            self._runloop_ref = _cf.CFRunLoopGetCurrent()
            _cf.CFRunLoopAddSource(
                self._runloop_ref, self._runloop_source, kCFRunLoopCommonModes
            )

            # Enable the tap
            _appservices.CGEventTapEnable.restype = None
            _appservices.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            _appservices.CGEventTapEnable(self._tap, True)

            _cf.CFRunLoopRun()
        except Exception:
            pass
        finally:
            self._active = False

    def stop(self) -> None:
        """Stop intercepting volume key events."""
        with self._lock:
            if not self._active:
                return

            self._active = False

            if self._tap:
                try:
                    _appservices.CGEventTapEnable(self._tap, False)
                except Exception:
                    pass

            if self._runloop_ref:
                try:
                    _cf.CFRunLoopStop(self._runloop_ref)
                except Exception:
                    pass

            if self._runloop_source and self._runloop_ref:
                try:
                    _cf.CFRunLoopRemoveSource(
                        self._runloop_ref, self._runloop_source, kCFRunLoopCommonModes
                    )
                except Exception:
                    pass

            if self._tap:
                try:
                    _cf.CFMachPortInvalidate(self._tap)
                except Exception:
                    pass

            self._tap = None
            self._runloop_source = None
            self._runloop_ref = None

    @property
    def active(self) -> bool:
        return self._active


# ==============================================================================
# Dependency Checks
# ==============================================================================
def check_dependencies() -> dict[str, bool]:
    """Check for required dependencies.

    Returns:
        Dict with keys 'blackhole', 'switchaudio', 'pyobjc', 'accessibility'
        and boolean values indicating availability.
    """
    import shutil

    results: dict[str, bool] = {}

    # BlackHole
    bh = find_blackhole_device()
    results["blackhole"] = bh is not None

    # SwitchAudioSource
    results["switchaudio"] = shutil.which("SwitchAudioSource") is not None

    # PyObjC (needed for NSDictionary in aggregate device creation)
    try:
        import Foundation  # noqa: F401
        import objc  # noqa: F401

        results["pyobjc"] = True
    except ImportError:
        results["pyobjc"] = False

    # Accessibility (needed for CGEventTap)
    results["accessibility"] = is_accessibility_trusted()

    return results
