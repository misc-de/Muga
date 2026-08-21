"""Recorded protocol data from a FuriOS/Halium phone. See __init__ for how."""

from __future__ import annotations

import struct

# ---------------------------------------------------------------------------
# sensorfwd accelerometer stream (com.nokia.SensorService, /run/sensord.sock)
# ---------------------------------------------------------------------------

# Six consecutive packets read off the socket with the phone lying flat on its
# back. Each is a 4-byte record count followed by that many 24-byte records.
SENSORD_CHUNKS: tuple[bytes, ...] = tuple(
    bytes.fromhex(h) for h in (
        "01000000fab46a7245000000497332c326c2eb424091764400000000",
        "01000000dded6b7245000000f92432c3912aec429163764400000000",
        "01000000c5266d7245000000f92432c332c7ec429e70764400000000",
        "01000000ae5f6e7245000000f92432c30898ed429e70764400000000",
        "010000008a986f7245000000c3f031c332c7ec429e70764400000000",
        "0100000062d1707245000000c3f031c326c2eb424d22764400000000",
    )
)

# The record layout, confirmed against the bytes above: a 64-bit timestamp,
# three little-endian floats, and a flags word. The floats are milli-g — the
# decoded first packet reads x=-178.5, y=117.9, z=986.3, i.e. ~1 g straight
# down and almost nothing sideways, which is what "flat on a table" looks
# like. Reading them as int32 instead yields values around ±1e9, so the two
# interpretations are easy to tell apart.
SENSORD_HEADER = struct.Struct("<I")
SENSORD_RECORD = struct.Struct("<Qfffi")

def _decode(chunk: bytes) -> list[tuple[float, float, float]]:
    """Milli-g triples out of one recorded packet."""
    (count,) = SENSORD_HEADER.unpack_from(chunk)
    out = []
    for i in range(count):
        _t, x, y, z, _flags = SENSORD_RECORD.unpack_from(
            chunk, SENSORD_HEADER.size + i * SENSORD_RECORD.size)
        out.append((x, y, z))
    return out


# Derived from SENSORD_CHUNKS rather than transcribed, so the two can never
# drift apart. The first packet reads (-178.5, 117.9, 986.3).
SENSORD_DECODED_MILLI_G: tuple[tuple[float, float, float], ...] = tuple(
    triple for chunk in SENSORD_CHUNKS for triple in _decode(chunk)
)

# The one-byte acknowledgement the daemon sends after the session id.
SENSORD_ACK = b"\x0a"


def sensord_packet(*samples: tuple[float, float, float], timestamp: int = 1) -> bytes:
    """Build a sensord packet carrying *samples* as milli-g triples.

    Same framing as SENSORD_CHUNKS, with values chosen by the caller — used to
    exercise orientations the recorded capture does not contain (the phone was
    lying still on a table).
    """
    out = SENSORD_HEADER.pack(len(samples))
    for i, (x, y, z) in enumerate(samples):
        out += SENSORD_RECORD.pack(timestamp + i, x, y, z, 0)
    return out


# ---------------------------------------------------------------------------
# droidcamsrc (gst-droid)
# ---------------------------------------------------------------------------

# The camera-device property's GParamSpec range. The element exposes three
# cameras on this phone, which is what the [minimum, maximum] pair encodes —
# the code derives the count from it rather than opening each HAL camera in
# turn, because rapid open/close cycles wedge the HAL on some devices.
DROIDCAM_PSPEC_MINIMUM = 0
DROIDCAM_PSPEC_MAXIMUM = 2
DROIDCAM_EXPECTED_COUNT = 3

# droidcamsrc's pad templates. All three are what the pipeline builders reach
# for: vfsrc for preview and recording, imgsrc for still capture.
DROIDCAM_PAD_TEMPLATES = ("imgsrc", "vfsrc", "vidsrc")


# ---------------------------------------------------------------------------
# Gst.DeviceMonitor (libcamera via PipeWire, also present on this phone)
# ---------------------------------------------------------------------------

# What the monitor reports here. Note the phone offers both paths — libcamera
# through PipeWire *and* droidcamsrc — and Yaga deliberately prefers
# droidcamsrc, because the /dev/video* nodes on Halium are ISP and encoder
# helpers rather than capture devices.
LIBCAMERA_DEVICES = (
    {
        "display_name": "Back Camera 1",
        "device_class": "Video/Source",
        "props": {
            "object.path": "libcamera:camera0",
            "node.name": "libcamera_input.camera0",
            "node.description": "Built-in Back Camera",
            "api.libcamera.location": "back",
            "device.api": "libcamera",
        },
    },
    {
        "display_name": "Front Camera",
        "device_class": "Video/Source",
        "props": {
            "object.path": "libcamera:camera1",
            "node.name": "libcamera_input.camera1",
            "node.description": "Built-in Front Camera",
            "api.libcamera.location": "front",
            "device.api": "libcamera",
        },
    },
    {
        "display_name": "Back Camera 2",
        "device_class": "Video/Source",
        "props": {
            "object.path": "libcamera:camera2",
            "node.name": "libcamera_input.camera2",
            "node.description": "Built-in Back Camera",
            "api.libcamera.location": "back",
            "device.api": "libcamera",
        },
    },
)


# The caps every one of those cameras advertises, verbatim. Note width and
# height are *ranges*, not fixed values — which is what libcamera reports for
# a device that has not been opened yet. Gst.Structure.get_int() answers
# (False, 0) for a range even though the field is present, so anything that
# asks for concrete numbers here comes away empty-handed.
LIBCAMERA_CAPS_STRING = (
    "video/x-raw, format=(string)I420, width=(int)[ 320, 1920 ], "
    "height=(int)[ 240, 1920 ], framerate=(fraction)[ 1/1, 30/1 ]"
)

# For contrast: what a plain UVC webcam advertises — discrete modes.
V4L2_CAPS_STRING = (
    "video/x-raw, format=(string)YUY2, width=(int)1280, height=(int)720; "
    "image/jpeg, width=(int)1920, height=(int)1080"
)


# ---------------------------------------------------------------------------
# GeoClue2
# ---------------------------------------------------------------------------

# Manager.GetClient returns a single object path.
GEOCLUE_GET_CLIENT_SIGNATURE = "(o)"

# The Client interface's properties on this system.
GEOCLUE_CLIENT_PROPERTIES = (
    "Active", "DesktopId", "DistanceThreshold", "Location",
    "RequestedAccuracyLevel", "TimeThreshold",
)

# The two the code writes before starting, both accepted here.
GEOCLUE_SETTABLE_PROPERTIES = ("DesktopId", "RequestedAccuracyLevel")

# With no fix yet, Client.Location is the root path rather than an empty
# string — worth knowing, because "no location" is still a non-empty string.
GEOCLUE_NO_LOCATION_PATH = "/"
