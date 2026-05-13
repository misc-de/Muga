"""Camera-device enumeration and GstCaps inspection helpers.

Extracted from camera.py so the main module focuses on the
CameraWindow class and its pipeline. Everything here is pure
GStreamer + filesystem; no GTK dependencies.

Two enumeration paths:
  1. **droidcamsrc** (gst-droid) — on Halium/Hybris phones the
     /dev/video* nodes don't expose real cameras. We derive the
     droidcamsrc device count from its `camera-device` property's
     pspec range and synthesize labelled entries.
  2. **Gst.DeviceMonitor** — on desktop Linux, the monitor aggregates
     PipeWire and v4l2 providers. We prefer PipeWire entries when both
     report the same /dev path, fall back to a /dev/video* scan if the
     monitor returns nothing usable.

The output of `enumerate_devices()` is a list of dicts with the same
shape regardless of which path produced them; the pipeline builder
in camera.py reads the `source_factory`, `path`, `gst_device`, etc.
fields to construct the right GStreamer element.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# droidcamsrc / Halium
# ----------------------------------------------------------------------


def droidcamsrc_available(gst: Any) -> bool:
    """Whether gst-droid's droidcamsrc is installed. Its presence on a
    system is a strong signal we're on Halium/Hybris (FuriOS, Droidian,
    UBports, postmarketOS-on-Halium), where the regular /dev/video*
    nodes don't expose real cameras — the camera HAL goes through
    Android via libhybris instead."""
    return gst.ElementFactory.find("droidcamsrc") is not None


def droidcam_camera_count(gst: Any) -> int:
    """Return how many camera-device IDs droidcamsrc exposes, derived
    from its GParamSpec range (the property is clamped to [min, max]).
    We avoid the alternative — actually transitioning probe elements to
    READY — because that opens the Android camera HAL once per ID and
    rapid open/close cycles wedge the HAL on some phones (the real
    pipeline opens later but the camera no longer streams)."""
    if not droidcamsrc_available(gst):
        return 0
    el = gst.ElementFactory.make("droidcamsrc", "_introspect")
    if el is None:
        return 0
    try:
        pspec = el.find_property("camera-device")
        if pspec is None:
            return 2
        max_id = getattr(pspec, "maximum", 1)
        # Some drivers expose pspec.maximum as INT32_MAX rather than the
        # real ceiling. Cap at a conservative 4 to avoid offering 2-billion
        # phantom cameras.
        if max_id > 8:
            max_id = 3
        return max(1, int(max_id) + 1)
    finally:
        del el


def enumerate_droidcam_devices(gst: Any) -> list[dict[str, Any]]:
    """Return one device dict per droidcamsrc camera-device the driver
    exposes via its property-spec range. Names mirror conventional
    phone labels: camera 0 is 'Back camera', 1 is 'Front camera',
    extras are 'Back camera N'."""
    count = droidcam_camera_count(gst)
    if count == 0:
        return []
    out: list[dict[str, Any]] = []
    for cam_id in range(count):
        if cam_id == 0:
            name = "Back camera"
            location = "back"
        elif cam_id == 1:
            name = "Front camera"
            location = "front"
        else:
            name = f"Back camera {cam_id}"
            location = "back"
        out.append({
            "name": name,
            "path": "",
            "source_factory": "droidcamsrc",
            "location": location,
            "caps": None,
            "pipewire": False,
            "kinds": {"raw"},
            "gst_device": None,
            "droidcam_id": cam_id,
        })
    return out


# ----------------------------------------------------------------------
# Generic v4l2 / PipeWire enumeration
# ----------------------------------------------------------------------

_IR_HINTS = ("infrared", "ir camera", "rgb-ir", " ir ", "(ir)", "[ir]")


def is_ir_name(name: str) -> bool:
    """Heuristic: Windows-Hello-style IR cameras shouldn't appear in a normal
    camera picker. UVC drivers expose them as separate /dev/video nodes and
    they only carry monochrome IR streams.

    Reference: Snapshot src/device_provider.rs IR filtering.
    """
    lo = " " + name.lower() + " "
    if lo.lstrip().startswith("ir "):
        return True
    return any(hint in lo for hint in _IR_HINTS)


def classify_location(props: Any, name: str) -> str:
    """Return one of 'front', 'back', 'external', 'unknown'.

    Prefers PipeWire/libcamera's authoritative api.libcamera.location prop.
    Falls back to name heuristics for plain v4l2.
    """
    if props is not None and hasattr(props, "get_string"):
        for key in ("api.libcamera.location", "api.libcamera.facing"):
            try:
                val = props.get_string(key)
            except Exception:
                val = None
            if val:
                v = val.lower()
                if "front" in v:
                    return "front"
                if "back" in v or "rear" in v:
                    return "back"
                if "external" in v:
                    return "external"
    lo = name.lower()
    if "front" in lo or "facing" in lo or "user" in lo:
        return "front"
    if "rear" in lo or "back" in lo:
        return "back"
    return "unknown"


def device_props(dev: Any) -> Any:
    try:
        return dev.get_properties()
    except Exception:
        return None


def device_path(props: Any) -> str:
    if props is None or not hasattr(props, "get_string"):
        return ""
    for key in ("device.path", "api.v4l2.path", "object.path"):
        try:
            val = props.get_string(key)
        except Exception:
            val = None
        if val:
            return val
    return ""


def is_pipewire_device(props: Any) -> bool:
    if props is None or not hasattr(props, "get_string"):
        return False
    # Pipewire-provided devices carry these node.* keys; v4l2deviceprovider
    # entries do not. Either is a reliable discriminator.
    try:
        return bool(props.get_string("node.description")) or bool(
            props.get_string("node.name")
        )
    except Exception:
        return False


def enumerate_devices(gst: Any) -> list[dict[str, Any]]:
    # On Halium/Hybris (FuriOS, Droidian, UBports), the only real cameras
    # are reachable via droidcamsrc — the /dev/video* nodes there expose
    # ISP / encoder helpers, not capture devices, and v4l2src fails with
    # ENOTTY when it tries to enumerate formats. Skip the v4l2/pipewire
    # path entirely when droidcamsrc is available.
    droidcam_devs = enumerate_droidcam_devices(gst)
    if droidcam_devs:
        LOGGER.debug(
            "Halium environment detected — using %d droidcamsrc devices",
            len(droidcam_devs),
        )
        return droidcam_devs

    devices: list[dict[str, Any]] = []
    try:
        monitor = gst.DeviceMonitor.new()
        monitor.add_filter("Video/Source", None)
        monitor.start()
        for dev in monitor.get_devices() or []:
            props = device_props(dev)
            path = device_path(props)
            display = dev.get_display_name() or path or "Camera"
            caps = None
            try:
                caps = dev.get_caps()
            except Exception:
                pass
            devices.append({
                "name": display,
                "path": path,
                "source_factory": "v4l2src" if path.startswith("/dev/video") else "",
                "location": classify_location(props, display),
                "caps": caps,
                "pipewire": is_pipewire_device(props),
                # Stash the Gst.Device so we can build the correct source
                # element later via create_element(). For PipeWire-managed
                # cameras that gives us a pipewiresrc rather than a raw
                # v4l2src, which is essential when PipeWire holds an
                # exclusive lock on /dev/videoN.
                "gst_device": dev,
            })
        monitor.stop()
    except Exception:
        LOGGER.debug("DeviceMonitor failed, falling back to /dev scan", exc_info=True)

    if not devices:
        for path in sorted(Path("/dev").glob("video*")):
            devices.append({
                "name": path.name,
                "path": str(path),
                "source_factory": "v4l2src",
                "location": "unknown",
                "caps": None,
                "pipewire": False,
                "gst_device": None,
            })

    # The DeviceMonitor aggregates *all* registered providers, so a single
    # physical camera typically appears twice (once from pipewiredevice-
    # provider, once from v4l2deviceprovider) — but only when both report
    # the same non-empty /dev path. We deliberately do NOT dedupe by name
    # because phones expose multiple physical sensors (front + 2x back)
    # under identical "Integrated Camera" display names, and libcamera-
    # abstracted devices may have empty paths altogether.
    by_path: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    for d in devices:
        if is_ir_name(d["name"]):
            LOGGER.debug("Filtering IR device: %s", d["name"])
            continue
        kinds = device_kinds(d.get("caps"))
        if not kinds:
            LOGGER.debug(
                "Filtering metadata-only device: %s (%s)", d["name"], d["path"]
            )
            continue
        d["kinds"] = kinds
        path = d["path"]
        if not path:
            # No /dev path — can't be safely merged with any other entry.
            unmatched.append(d)
            continue
        existing = by_path.get(path)
        if existing is None:
            by_path[path] = d
            continue
        if d["pipewire"] and not existing["pipewire"]:
            by_path[path] = d
        elif d["pipewire"] == existing["pipewire"] and len(
            modes_from_caps(d.get("caps"))
        ) > len(modes_from_caps(existing.get("caps"))):
            by_path[path] = d

    result = list(by_path.values()) + unmatched

    # Last-resort backup: if the monitor surfaced nothing usable (which can
    # happen when pipewire is in a transient state or only advertises odd
    # caps), scan /dev directly and assume each video node is openable.
    # The pipeline-builder will fail visibly if a given node isn't a
    # capture device.
    if not result:
        LOGGER.debug("No usable devices from monitor — scanning /dev")
        for path in sorted(Path("/dev").glob("video*")):
            result.append({
                "name": path.name,
                "path": str(path),
                "source_factory": "v4l2src",
                "location": "unknown",
                "caps": None,
                "pipewire": False,
                "kinds": set(),
                "gst_device": None,
            })
    return result


# ----------------------------------------------------------------------
# Caps inspection
# ----------------------------------------------------------------------


def modes_from_caps(caps: Any) -> list[tuple[int, int, str]]:
    """Extract (w, h, kind) tuples from a GstCaps where kind is 'raw' for
    video/x-raw structures and 'jpeg' for image/jpeg. UVC cameras typically
    advertise their highest resolutions only via MJPG, so dropping those
    would either leave us with tiny modes or — when raw isn't advertised
    at all — make the camera look like it doesn't exist."""
    if caps is None:
        return []
    out: dict[tuple[int, int, str], None] = {}
    try:
        n = caps.get_size()
    except Exception:
        return []
    for i in range(n):
        s = caps.get_structure(i)
        if s is None:
            continue
        name = s.get_name()
        if name == "video/x-raw":
            kind = "raw"
        elif name == "image/jpeg":
            kind = "jpeg"
        else:
            continue
        ok_w, w = s.get_int("width")
        ok_h, h = s.get_int("height")
        if ok_w and ok_h and w > 0 and h > 0:
            out[(w, h, kind)] = None
    return sorted(out.keys(), key=lambda whk: -(whk[0] * whk[1]))


def resolutions_from_caps(caps: Any) -> list[tuple[int, int]]:
    """Back-compat shim: just the (w, h) pairs, prefer raw over jpeg when
    the same resolution exists in both."""
    seen: dict[tuple[int, int], str] = {}
    for w, h, kind in modes_from_caps(caps):
        prev = seen.get((w, h))
        if prev is None or (prev == "jpeg" and kind == "raw"):
            seen[(w, h)] = kind
    return sorted(seen.keys(), key=lambda wh: -(wh[0] * wh[1]))


def device_kinds(caps: Any) -> set[str]:
    """Which capture formats a device advertises: {'raw', 'jpeg'}."""
    return {k for _w, _h, k in modes_from_caps(caps)}
