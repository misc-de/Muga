"""Camera window — frameless live preview + still capture using GStreamer."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, GLib, Graphene, Gtk

try:
    gi.require_version("GExiv2", "0.10")
    from gi.repository import GExiv2  # type: ignore
    _HAS_GEXIV2 = True
except (ValueError, ImportError):
    GExiv2 = None  # type: ignore
    _HAS_GEXIV2 = False

from . import camera_controls
from .camera_controls import V4l2Control
from .camera_geo import GeoClient

LOGGER = logging.getLogger(__name__)


_CSS = b"""
.camera-root { background-color: #000; }
.camera-toast {
    background-color: rgba(0, 0, 0, 0.55);
    color: #fff;
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 12px;
}
.camera-countdown {
    color: #fff;
    font-size: 96px;
    font-weight: 200;
    text-shadow: 0 0 24px rgba(0,0,0,0.7);
}
.camera-iconbtn {
    min-width: 44px;
    min-height: 44px;
    padding: 0;
    border-radius: 999px;
    background-color: rgba(0, 0, 0, 0.45);
    color: #fff;
    border: none;
    box-shadow: none;
}
.camera-iconbtn:hover { background-color: rgba(0, 0, 0, 0.65); }
.camera-iconbtn:active { background-color: rgba(255, 255, 255, 0.15); }
.camera-iconbtn:checked {
    background-color: rgba(255, 255, 255, 0.85);
    color: #000;
}
.camera-resbtn {
    min-height: 36px;
    padding: 0 14px;
    border-radius: 999px;
    background-color: rgba(0, 0, 0, 0.45);
    color: #fff;
    border: none;
    box-shadow: none;
    font-size: 12px;
    font-feature-settings: "tnum";
}
.camera-resbtn:hover { background-color: rgba(0, 0, 0, 0.65); }
.shutter-button {
    min-width: 76px;
    min-height: 76px;
    padding: 6px;
    border-radius: 999px;
    background-color: transparent;
    border: 4px solid #fff;
    box-shadow: none;
}
.shutter-button > .shutter-core {
    background-color: #e8443b;
    border-radius: 999px;
    min-width: 56px;
    min-height: 56px;
}
.shutter-button:hover > .shutter-core { background-color: #ff5247; }
.shutter-button:active > .shutter-core { background-color: #c0322a; }
.shutter-button:disabled > .shutter-core { background-color: #6a6a6a; }
"""


_corner_css_installed = False


def _ensure_css() -> None:
    global _corner_css_installed
    if _corner_css_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _corner_css_installed = True


class CameraError(RuntimeError):
    pass


def _gst() -> Any:
    try:
        from gi.repository import Gst as _Gst
    except (ImportError, ValueError) as exc:
        raise CameraError("GStreamer Python bindings not found (python3-gst-1.0)") from exc
    _Gst.init(None)
    return _Gst


def camera_supported() -> bool:
    try:
        gst = _gst()
    except CameraError:
        return False
    return (
        gst.ElementFactory.find("v4l2src") is not None
        or gst.ElementFactory.find("autovideosrc") is not None
        or gst.ElementFactory.find("droidcamsrc") is not None
    )


def _droidcamsrc_available(gst: Any) -> bool:
    """Whether gst-droid's droidcamsrc is installed. Its presence on a
    system is a strong signal we're on Halium/Hybris (FuriOS, Droidian,
    UBports, postmarketOS-on-Halium), where the regular /dev/video*
    nodes don't expose real cameras — the camera HAL goes through
    Android via libhybris instead."""
    return gst.ElementFactory.find("droidcamsrc") is not None


def _droidcam_camera_count(gst: Any) -> int:
    """Return how many camera-device IDs droidcamsrc exposes, derived
    from its GParamSpec range (the property is clamped to [min, max]).
    We avoid the alternative — actually transitioning probe elements to
    READY — because that opens the Android camera HAL once per ID and
    rapid open/close cycles wedge the HAL on some phones (the real
    pipeline opens later but the camera no longer streams)."""
    if not _droidcamsrc_available(gst):
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


def _enumerate_droidcam_devices(gst: Any) -> list[dict[str, Any]]:
    """Return one device dict per droidcamsrc camera-device the driver
    exposes via its property-spec range. Names mirror conventional
    phone labels: camera 0 is 'Back camera', 1 is 'Front camera',
    extras are 'Back camera N'."""
    count = _droidcam_camera_count(gst)
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
# Device enumeration helpers
# ----------------------------------------------------------------------

_IR_HINTS = ("infrared", "ir camera", "rgb-ir", " ir ", "(ir)", "[ir]")


def _is_ir_name(name: str) -> bool:
    """Heuristic: Windows-Hello-style IR cameras shouldn't appear in a normal
    camera picker. UVC drivers expose them as separate /dev/video nodes and
    they only carry monochrome IR streams.

    Reference: Snapshot src/device_provider.rs IR filtering.
    """
    lo = " " + name.lower() + " "
    if lo.lstrip().startswith("ir "):
        return True
    return any(hint in lo for hint in _IR_HINTS)


def _classify_location(props: Any, name: str) -> str:
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


def _device_props(dev: Any) -> Any:
    try:
        return dev.get_properties()
    except Exception:
        return None


def _device_path(props: Any) -> str:
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


def _is_pipewire_device(props: Any) -> bool:
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


def _enumerate_devices(gst: Any) -> list[dict[str, Any]]:
    # On Halium/Hybris (FuriOS, Droidian, UBports), the only real cameras
    # are reachable via droidcamsrc — the /dev/video* nodes there expose
    # ISP / encoder helpers, not capture devices, and v4l2src fails with
    # ENOTTY when it tries to enumerate formats. Skip the v4l2/pipewire
    # path entirely when droidcamsrc is available.
    droidcam_devs = _enumerate_droidcam_devices(gst)
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
            props = _device_props(dev)
            path = _device_path(props)
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
                "location": _classify_location(props, display),
                "caps": caps,
                "pipewire": _is_pipewire_device(props),
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
        if _is_ir_name(d["name"]):
            LOGGER.debug("Filtering IR device: %s", d["name"])
            continue
        kinds = _device_kinds(d.get("caps"))
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
            _modes_from_caps(d.get("caps"))
        ) > len(_modes_from_caps(existing.get("caps"))):
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


def _modes_from_caps(caps: Any) -> list[tuple[int, int, str]]:
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


def _resolutions_from_caps(caps: Any) -> list[tuple[int, int]]:
    """Back-compat shim: just the (w, h) pairs, prefer raw over jpeg when
    the same resolution exists in both."""
    seen: dict[tuple[int, int], str] = {}
    for w, h, kind in _modes_from_caps(caps):
        prev = seen.get((w, h))
        if prev is None or (prev == "jpeg" and kind == "raw"):
            seen[(w, h)] = kind
    return sorted(seen.keys(), key=lambda wh: -(wh[0] * wh[1]))


def _device_kinds(caps: Any) -> set[str]:
    """Which capture formats a device advertises: {'raw', 'jpeg'}."""
    return {k for _w, _h, k in _modes_from_caps(caps)}


# ----------------------------------------------------------------------
# Custom drawing widgets
# ----------------------------------------------------------------------


def _corner_bracket(corner: str) -> Gtk.DrawingArea:
    """A single L-shaped viewfinder corner. corner ∈ {tl, tr, bl, br}."""
    area = Gtk.DrawingArea()
    area.set_content_width(28)
    area.set_content_height(28)

    def draw(_da: Gtk.DrawingArea, cr: Any, w: int, h: int) -> None:
        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.set_line_width(2.0)
        cr.set_line_cap(1)
        pad = 3
        length = min(w, h) - pad - 4
        if corner == "tl":
            cr.move_to(pad, pad + length); cr.line_to(pad, pad); cr.line_to(pad + length, pad)
        elif corner == "tr":
            cr.move_to(w - pad - length, pad); cr.line_to(w - pad, pad); cr.line_to(w - pad, pad + length)
        elif corner == "bl":
            cr.move_to(pad, h - pad - length); cr.line_to(pad, h - pad); cr.line_to(pad + length, h - pad)
        else:
            cr.move_to(w - pad - length, h - pad); cr.line_to(w - pad, h - pad); cr.line_to(w - pad, h - pad - length)
        cr.stroke()

    area.set_draw_func(draw)
    area.set_can_target(False)
    return area


def _grid_overlay() -> Gtk.DrawingArea:
    """Rule-of-thirds grid spanning the viewport."""
    area = Gtk.DrawingArea()
    area.set_hexpand(True)
    area.set_vexpand(True)

    def draw(_da: Gtk.DrawingArea, cr: Any, w: int, h: int) -> None:
        # Faint dark shadow first, then white lines on top — gives the grid
        # legibility on both bright and dark scenes.
        for offset, alpha in ((1.0, 0.35), (0.0, 0.75)):
            cr.set_source_rgba(0 if alpha < 0.5 else 1, 0 if alpha < 0.5 else 1, 0 if alpha < 0.5 else 1, alpha)
            cr.set_line_width(1.0)
            for i in (1, 2):
                x = w * i / 3 + offset
                cr.move_to(x, 0); cr.line_to(x, h)
                y = h * i / 3 + offset
                cr.move_to(0, y); cr.line_to(w, y)
            cr.stroke()

    area.set_draw_func(draw)
    area.set_can_target(False)
    return area


class MirroredPicture(Gtk.Picture):
    """Gtk.Picture that can render its content horizontally flipped and/or
    zoomed about its center.

    Only the on-screen render is transformed — captured frames are
    unaffected, so text in front-cam selfies still reads correctly in
    saved files and zoom is purely a viewfinder affordance. Implementing
    zoom widget-side (rather than via a videocrop pipeline element) keeps
    the GStreamer chain minimal, which avoids negotiation failures on
    cameras whose only modes are MJPG.
    """

    __gtype_name__ = "YagaMirroredPicture"

    def __init__(self) -> None:
        super().__init__()
        self._mirrored = False
        self._zoom = 1.0

    def set_mirrored(self, mirrored: bool) -> None:
        if self._mirrored == mirrored:
            return
        self._mirrored = mirrored
        self.queue_draw()

    def set_zoom(self, zoom: float) -> None:
        zoom = max(1.0, min(8.0, zoom))
        if abs(zoom - self._zoom) < 0.01:
            return
        self._zoom = zoom
        self.queue_draw()

    def get_zoom(self) -> float:
        return self._zoom

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:  # type: ignore[override]
        zoom = self._zoom
        mirror = self._mirrored
        if zoom == 1.0 and not mirror:
            Gtk.Picture.do_snapshot(self, snapshot)
            return
        w = self.get_width()
        h = self.get_height()
        snapshot.save()
        if mirror:
            snapshot.translate(Graphene.Point().init(w, 0))
            snapshot.scale(-1.0, 1.0)
        if zoom != 1.0:
            cx = w / 2 if not mirror else w / 2
            cy = h / 2
            snapshot.translate(Graphene.Point().init(cx, cy))
            snapshot.scale(zoom, zoom)
            snapshot.translate(Graphene.Point().init(-cx, -cy))
        Gtk.Picture.do_snapshot(self, snapshot)
        snapshot.restore()


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------


class CameraWindow(Adw.Window):
    def __init__(
        self,
        parent: Gtk.Window,
        save_dir: Path,
        translator: Callable[[str], str] | None = None,
        on_captured: Callable[[Path], None] | None = None,
    ) -> None:
        super().__init__()
        _ensure_css()
        self._ = translator or (lambda s: s)
        self.set_transient_for(parent)
        self.set_modal(False)
        self.set_decorated(False)
        self.set_default_size(820, 540)
        self.set_title(self._("Camera"))
        self.add_css_class("camera-root")

        self._save_dir = Path(save_dir)
        self._on_captured = on_captured
        self._Gst = _gst()
        self._pipeline: Any = None
        self._bus: Any = None
        self._appsink: Any = None
        self._preview_appsink: Any = None
        self._preview_signal_id: int | None = None
        self._valve: Any = None
        self._capture_signal_id: int | None = None
        self._capture_timeout_id: int | None = None
        self._capsfilter: Any = None
        self._devices: list[dict[str, Any]] = _enumerate_devices(self._Gst)
        self._device_index = 0
        self._busy_capture = False
        self._toast_timer: int | None = None
        self._timer_choices = (0, 3, 10)
        self._timer_idx = 0
        self._countdown_value = 0
        self._countdown_source: int | None = None
        self._grid_on = False
        self._zoom = 1.0
        self._zoom_base = 1.0
        self._zoom_max = 4.0
        self._selected_resolution: tuple[int, int] | None = None
        self._flash_source: int | None = None
        self._controls: dict[str, V4l2Control] = {}
        self._controls_built: bool = False
        # Per-device cache so re-opening the popover after a camera switch
        # doesn't trigger another v4l2-ctl probe.
        self._controls_cache: dict[str, dict[str, V4l2Control]] = {}
        self._focus_point: tuple[float, float] | None = None
        self._focus_hide_source: int | None = None
        self._geo: GeoClient | None = None

        overlay = Gtk.Overlay()
        self.set_content(overlay)

        self._picture = MirroredPicture()
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        overlay.set_child(self._picture)

        # Grid overlay — toggleable, sits beneath every chrome element.
        self._grid = _grid_overlay()
        self._grid.set_visible(False)
        overlay.add_overlay(self._grid)

        # Corner brackets — pure decoration, always present.
        for corner, halign, valign, mt, mb, ms, me in (
            ("tl", Gtk.Align.START,  Gtk.Align.START, 12, 0, 12, 0),
            ("tr", Gtk.Align.END,    Gtk.Align.START, 12, 0, 0, 12),
            ("bl", Gtk.Align.START,  Gtk.Align.END,   0, 12, 12, 0),
            ("br", Gtk.Align.END,    Gtk.Align.END,   0, 12, 0, 12),
        ):
            bracket = _corner_bracket(corner)
            bracket.set_halign(halign)
            bracket.set_valign(valign)
            bracket.set_margin_top(mt); bracket.set_margin_bottom(mb)
            bracket.set_margin_start(ms); bracket.set_margin_end(me)
            overlay.add_overlay(bracket)

        # Screen-flash overlay — white box that briefly fades over the preview
        # right after capture. No CSS needed; we toggle visibility + opacity.
        self._flash = Gtk.Box()
        self._flash.add_css_class("camera-flash")
        self._flash.set_hexpand(True)
        self._flash.set_vexpand(True)
        self._flash.set_can_target(False)
        self._flash.set_opacity(0.0)
        self._flash.set_visible(False)
        # Inline style so we don't depend on theme: a plain white fill.
        try:
            css = Gtk.CssProvider()
            css.load_from_data(b".camera-flash { background-color: #fff; }")
            self._flash.get_style_context().add_provider(
                css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception:
            pass
        overlay.add_overlay(self._flash)

        # Tap-to-focus indicator — full-size invisible drawing area that
        # only paints when self._focus_point is set. Picture click coords
        # map 1:1 to this area's coords because both are children of the
        # same Overlay and span the same allocation.
        self._focus_rect = Gtk.DrawingArea()
        self._focus_rect.set_hexpand(True)
        self._focus_rect.set_vexpand(True)
        self._focus_rect.set_can_target(False)
        self._focus_rect.set_draw_func(self._draw_focus_rect)
        overlay.add_overlay(self._focus_rect)

        # Countdown — large centered number shown only while self-timer runs.
        self._countdown = Gtk.Label(label="")
        self._countdown.add_css_class("camera-countdown")
        self._countdown.set_halign(Gtk.Align.CENTER)
        self._countdown.set_valign(Gtk.Align.CENTER)
        self._countdown.set_visible(False)
        self._countdown.set_can_target(False)
        overlay.add_overlay(self._countdown)

        # Close button — needed because there's no titlebar.
        self._close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        self._close_button.add_css_class("camera-iconbtn")
        self._close_button.set_halign(Gtk.Align.START)
        self._close_button.set_valign(Gtk.Align.START)
        self._close_button.set_margin_top(16)
        self._close_button.set_margin_start(16)
        self._close_button.set_tooltip_text(self._("Close"))
        self._close_button.connect("clicked", lambda _b: self.close())
        overlay.add_overlay(self._close_button)

        # Top-right cluster: grid toggle, self-timer, resolution picker.
        top_right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        top_right.set_halign(Gtk.Align.END)
        top_right.set_valign(Gtk.Align.START)
        top_right.set_margin_top(16)
        top_right.set_margin_end(16)
        overlay.add_overlay(top_right)

        self._grid_button = Gtk.ToggleButton()
        self._grid_button.set_icon_name("view-grid-symbolic")
        self._grid_button.add_css_class("camera-iconbtn")
        self._grid_button.set_tooltip_text(self._("Grid"))
        self._grid_button.connect("toggled", self._on_grid_toggled)
        top_right.append(self._grid_button)

        self._timer_button = Gtk.Button()
        self._timer_button.add_css_class("camera-iconbtn")
        self._timer_button.set_tooltip_text(self._("Self-timer"))
        self._timer_button.connect("clicked", lambda _b: self._cycle_timer())
        self._refresh_timer_button()
        top_right.append(self._timer_button)

        self._res_button = Gtk.MenuButton()
        self._res_button.add_css_class("camera-resbtn")
        self._res_button.set_tooltip_text(self._("Resolution"))
        self._res_button.set_label("—")
        self._res_button.set_visible(False)
        self._res_popover: Gtk.Popover | None = None
        top_right.append(self._res_button)

        # Manual-controls gear — only shown when v4l2-ctl is installed.
        self._gear_button = Gtk.MenuButton()
        self._gear_button.set_icon_name("emblem-system-symbolic")
        self._gear_button.add_css_class("camera-iconbtn")
        self._gear_button.set_tooltip_text(self._("Camera controls"))
        self._gear_button.set_visible(False)
        self._gear_popover: Gtk.Popover | None = None
        self._gear_button.connect("notify::active", self._on_gear_toggled)
        top_right.append(self._gear_button)

        # Geotag opt-in — default OFF; enabling spins up a GeoClue client
        # and embeds GPS into EXIF on subsequent captures. We don't persist
        # this across sessions on purpose; geotagging is intentional state
        # the user re-confirms each time they open the camera.
        self._geo_button = Gtk.ToggleButton()
        self._geo_button.set_icon_name("mark-location-symbolic")
        self._geo_button.add_css_class("camera-iconbtn")
        self._geo_button.set_tooltip_text(self._("Geotag photos"))
        self._geo_button.connect("toggled", self._on_geo_toggled)
        top_right.append(self._geo_button)

        # Shutter — large red circular button, right-centered.
        self._shutter = Gtk.Button()
        self._shutter.add_css_class("shutter-button")
        core = Gtk.Box()
        core.add_css_class("shutter-core")
        self._shutter.set_child(core)
        self._shutter.set_halign(Gtk.Align.END)
        self._shutter.set_valign(Gtk.Align.CENTER)
        self._shutter.set_margin_end(24)
        self._shutter.set_tooltip_text(self._("Capture"))
        self._shutter.connect("clicked", lambda _b: self._on_shutter())
        overlay.add_overlay(self._shutter)

        # Camera-switch — only added when more than one capture device exists.
        self._rotate_button: Gtk.Button | None = None
        if len(self._devices) > 1:
            self._rotate_button = Gtk.Button.new_from_icon_name("camera-switch-symbolic")
            self._rotate_button.add_css_class("camera-iconbtn")
            self._rotate_button.set_halign(Gtk.Align.END)
            self._rotate_button.set_valign(Gtk.Align.END)
            self._rotate_button.set_margin_end(24)
            self._rotate_button.set_margin_bottom(24)
            self._rotate_button.set_tooltip_text(self._("Switch camera"))
            self._rotate_button.connect("clicked", lambda _b: self._switch_camera())
            overlay.add_overlay(self._rotate_button)

        # Toast for status / errors.
        self._toast = Gtk.Label(label="")
        self._toast.add_css_class("camera-toast")
        self._toast.set_halign(Gtk.Align.CENTER)
        self._toast.set_valign(Gtk.Align.END)
        self._toast.set_margin_bottom(28)
        self._toast.set_visible(False)
        overlay.add_overlay(self._toast)

        # Window-level drag (replacement for titlebar grab).
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        self.add_controller(drag)

        # Pinch-to-zoom on the preview.
        zoom_gesture = Gtk.GestureZoom()
        zoom_gesture.connect("begin", self._on_zoom_begin)
        zoom_gesture.connect("scale-changed", self._on_zoom_changed)
        self.add_controller(zoom_gesture)

        # Tap-to-focus — attached to the picture so the chrome buttons,
        # which sit above the picture in the overlay z-order, capture
        # their own clicks first.
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", self._on_tap_to_focus)
        self._picture.add_controller(click)

        # ESC / Space / Return shortcuts.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        # Scroll-to-zoom for desktops without touch.
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        self.connect("close-request", self._on_close)

        if not self._devices:
            self._shutter.set_sensitive(False)
            self._show_toast(self._("No camera detected"))
        else:
            GLib.idle_add(self._start_pipeline)

    # ------------------------------------------------------------------
    # Pipeline lifecycle
    # ------------------------------------------------------------------

    def _current_device(self) -> dict[str, Any] | None:
        if not self._devices:
            return None
        return self._devices[self._device_index % len(self._devices)]

    def _build_downstream_description(self, device: dict[str, Any]) -> str:
        """Build everything from the first videoconvert onwards. The source
        (and optional capsfilter / jpegdec) are added programmatically in
        _start_pipeline so we can use Gst.Device.create_element(), which
        picks pipewiresrc vs v4l2src per the device's provider."""
        gst = self._Gst
        has_gtk_sink = gst.ElementFactory.find("gtk4paintablesink") is not None
        has_jpeg = gst.ElementFactory.find("jpegenc") is not None
        has_appsink = gst.ElementFactory.find("appsink") is not None

        if has_gtk_sink:
            preview_branch = (
                "t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert "
                "   ! gtk4paintablesink name=preview"
            )
        elif has_appsink:
            preview_branch = (
                "t. ! queue leaky=downstream max-size-buffers=2 "
                "   ! videoconvert ! video/x-raw,format=RGBA "
                "   ! appsink name=preview_sink emit-signals=true "
                "             max-buffers=1 drop=true sync=false"
            )
        else:
            preview_branch = (
                "t. ! queue leaky=downstream max-size-buffers=2 ! fakesink sync=false"
            )
        # The snap branch is gated by a valve so jpegenc only runs when the
        # user actually presses the shutter. Running jpegenc on every frame
        # at 30 fps on a Halium phone causes memory pile-up and OOM crash.
        # On capture: open valve, wait for new-sample signal, close valve.
        snapshot_branch = (
            "t. ! queue leaky=downstream max-size-buffers=2 "
            "   ! valve name=shutter drop=true "
            "   ! videoconvert ! jpegenc quality=92 "
            "   ! appsink name=snap emit-signals=true max-buffers=1 drop=true sync=false"
            if (has_jpeg and has_appsink)
            else ""
        )

        parts = ["videoconvert ! tee name=t", preview_branch]
        if snapshot_branch:
            parts.append(snapshot_branch)
        return " ".join(parts)

    def _selected_format_kind(self, device: dict[str, Any]) -> str:
        """For the user-picked resolution, was it advertised as raw or jpeg?"""
        if self._selected_resolution is None:
            return "raw"
        sel_w, sel_h = self._selected_resolution
        for w, h, k in _modes_from_caps(device.get("caps")):
            if w == sel_w and h == sel_h:
                return k
        return "raw"

    def _make_source_element(self, device: dict[str, Any]) -> Any:
        """Pick the correct source element. Falls back through:
          (a) droidcamsrc when the device dict tags itself as such (Halium
              / Hybris phones). Uses the named viewfinder pad downstream.
          (b) Gst.Device.create_element() — picks pipewiresrc for
              PipeWire-managed cameras, v4l2src for raw v4l2 nodes.
              Critical when PipeWire holds an exclusive lock on the
              /dev/videoN node (direct v4l2src would fail with ENOTTY).
          (c) Manual v4l2src device=... — for the /dev backup-scan
              path or when create_element returns nothing.
        """
        gst = self._Gst
        factory = device.get("source_factory") or ""
        if factory == "droidcamsrc":
            src = gst.ElementFactory.make("droidcamsrc", "src")
            if src is not None:
                try:
                    src.set_property("camera-device", device.get("droidcam_id", 0))
                except Exception:
                    LOGGER.debug("droidcamsrc camera-device set failed", exc_info=True)
                # mode-image gives the viewfinder + imgsrc pads (which is
                # what we need for stills). mode-video also exposes vidsrc
                # but we don't record video yet.
                try:
                    src.set_property("mode", 1)  # 1 = image, 2 = video
                except Exception:
                    pass
                return src
        gst_device = device.get("gst_device")
        if gst_device is not None:
            try:
                src = gst_device.create_element("src")
                if src is not None:
                    return src
            except Exception:
                LOGGER.debug("Gst.Device.create_element failed", exc_info=True)
        path = device.get("path") or ""
        if path and gst.ElementFactory.find("v4l2src") is not None:
            src = gst.ElementFactory.make("v4l2src", "src")
            if src is not None:
                src.set_property("device", path)
                return src
        if gst.ElementFactory.find("autovideosrc") is not None:
            return gst.ElementFactory.make("autovideosrc", "src")
        return None

    def _source_output_pad(self, source: Any) -> Any:
        """Get the right output pad. droidcamsrc uses request pads named
        vfsrc / imgsrc / vidsrc; everything else has a static src pad."""
        # Try named viewfinder first (droidcamsrc convention).
        pad = source.get_static_pad("vfsrc")
        if pad is not None:
            return pad
        templates = []
        try:
            templates = [
                t.name_template for t in source.get_pad_template_list() or []
            ]
        except Exception:
            pass
        if "vfsrc" in templates:
            try:
                pad = source.request_pad_simple("vfsrc")
                if pad is not None:
                    return pad
            except Exception:
                LOGGER.debug("request_pad_simple(vfsrc) failed", exc_info=True)
        return source.get_static_pad("src")

    def _start_pipeline(self) -> bool:
        device = self._current_device()
        if device is None:
            return False
        gst = self._Gst
        self._stop_pipeline()

        # v4l2 controls are probed lazily on gear-popover open; doing it
        # before the source opens the device corrupts descriptor state
        # on some UVC kernels (ENUM_FMT -> ENOTTY).
        self._mark_controls_dirty_for_device(device)

        source = self._make_source_element(device)
        if source is None:
            self._fail(self._("No camera source element available"))
            return False

        # Optional capsfilter pinning the user-picked resolution. Format
        # (raw vs jpeg) is also pinned because for MJPG we have to insert
        # jpegdec, and the two need to agree.
        capsfilter = None
        jpegdec = None
        if self._selected_resolution is not None:
            sel_w, sel_h = self._selected_resolution
            kind = self._selected_format_kind(device)
            capsfilter = gst.ElementFactory.make("capsfilter", "resfilter")
            if kind == "jpeg":
                caps_str = f"image/jpeg,width={sel_w},height={sel_h}"
                if gst.ElementFactory.find("jpegdec") is not None:
                    jpegdec = gst.ElementFactory.make("jpegdec", "jpegdec")
            else:
                caps_str = f"video/x-raw,width={sel_w},height={sel_h}"
            capsfilter.set_property("caps", gst.Caps.from_string(caps_str))

        bin_desc = self._build_downstream_description(device)
        LOGGER.debug(
            "Camera pipeline: src=%s caps=%s downstream=%s",
            source.get_factory().get_name() if source.get_factory() else "?",
            (capsfilter.get_property("caps").to_string()
                if capsfilter is not None else "<auto>"),
            bin_desc,
        )
        try:
            downstream = gst.parse_bin_from_description(bin_desc, True)
        except Exception as exc:
            self._fail(f"Pipeline error: {exc}")
            return False

        pipeline = gst.Pipeline.new("yaga-camera")
        pipeline.add(source)
        if capsfilter is not None:
            pipeline.add(capsfilter)
        if jpegdec is not None:
            pipeline.add(jpegdec)
        pipeline.add(downstream)

        # Link the chain. For droidcamsrc the first link has to go through
        # the named viewfinder request-pad (vfsrc); for everything else
        # the source's static src pad is fine and Element.link() handles
        # it transparently.
        src_pad = self._source_output_pad(source)
        if src_pad is None:
            self._fail(self._("Source element has no usable output pad"))
            return False
        chain_tail = [downstream]
        if jpegdec is not None:
            chain_tail.insert(0, jpegdec)
        if capsfilter is not None:
            chain_tail.insert(0, capsfilter)
        # First link: source-pad → first-tail's sink pad.
        first_tail = chain_tail[0]
        first_sink = first_tail.get_static_pad("sink")
        if first_sink is None:
            self._fail(self._("Downstream element has no sink pad"))
            return False
        if src_pad.link(first_sink) != gst.PadLinkReturn.OK:
            fa = source.get_factory().get_name() if source.get_factory() else "?"
            fb = first_tail.get_factory().get_name() if first_tail.get_factory() else "?"
            self._fail(f"Could not link {fa} → {fb}")
            return False
        # Remaining links go via Element.link() since they're all static.
        for a, b in zip(chain_tail, chain_tail[1:]):
            if not a.link(b):
                fa = a.get_factory().get_name() if a.get_factory() else "?"
                fb = b.get_factory().get_name() if b.get_factory() else "?"
                self._fail(f"Could not link {fa} → {fb}")
                return False

        self._pipeline = pipeline
        self._appsink = pipeline.get_by_name("snap")
        self._valve = pipeline.get_by_name("shutter")
        self._capsfilter = capsfilter
        self._zoom = 1.0
        self._picture.set_zoom(1.0)

        self._bus = pipeline.get_bus()
        if self._bus is not None:
            self._bus.add_signal_watch()
            self._bus.connect("message", self._on_bus_message)

        sink = pipeline.get_by_name("preview")
        if sink is not None:
            try:
                paintable = sink.get_property("paintable")
                if paintable is not None:
                    self._picture.set_paintable(paintable)
            except Exception:
                LOGGER.debug("Could not bind preview paintable", exc_info=True)
        else:
            preview_app = pipeline.get_by_name("preview_sink")
            if preview_app is not None:
                self._preview_appsink = preview_app
                self._preview_signal_id = preview_app.connect(
                    "new-sample", self._on_preview_sample
                )

        self._picture.set_mirrored(device.get("location") == "front")

        result = pipeline.set_state(gst.State.PLAYING)
        if result == gst.StateChangeReturn.FAILURE:
            self._fail(self._("Could not start camera"))
            return False

        self._shutter.set_sensitive(self._appsink is not None)
        if self._appsink is None:
            self._show_toast(self._("Capture unavailable"))
        self._populate_resolutions(device)
        return False

    def _stop_pipeline(self) -> None:
        # Tear down any in-flight capture state before disposing the pipeline.
        self._close_valve_and_disconnect()
        self._valve = None
        if self._preview_appsink is not None and self._preview_signal_id is not None:
            try:
                self._preview_appsink.disconnect(self._preview_signal_id)
            except Exception:
                pass
        self._preview_signal_id = None
        self._preview_appsink = None
        if self._bus is not None:
            try:
                self._bus.remove_signal_watch()
            except Exception:
                pass
            self._bus = None
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(self._Gst.State.NULL)
                # Wait for the state-change to complete; the v4l2 device
                # is only released once the transition has actually
                # finished, and a subsequent start would otherwise race
                # against the same descriptor.
                self._pipeline.get_state(2 * self._Gst.SECOND)
            except Exception:
                pass
            self._pipeline = None
        self._appsink = None
        self._capsfilter = None

    def _on_preview_sample(self, sink: Any) -> Any:
        """Fallback preview path — invoked on the streaming thread when
        gtk4paintablesink isn't installed. Pulls one RGBA buffer, wraps
        it in a Gdk.MemoryTexture, then hands the texture to the picture
        widget from the main loop.

        Memory copy budget: one bytes(...) per frame (~1.2 MB at 640x480
        RGBA, ~36 MB/s at 30 fps). Negligible on any modern desktop and
        not worth the complexity of zero-copy buffer-pool tricks.
        """
        gst = self._Gst
        try:
            sample = sink.emit("pull-sample")
        except Exception:
            return gst.FlowReturn.OK
        if sample is None:
            return gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps()
        if buf is None or caps is None or caps.get_size() == 0:
            return gst.FlowReturn.OK
        s = caps.get_structure(0)
        ok_w, w = s.get_int("width")
        ok_h, h = s.get_int("height")
        if not (ok_w and ok_h) or w <= 0 or h <= 0:
            return gst.FlowReturn.OK
        ok, mapinfo = buf.map(gst.MapFlags.READ)
        if not ok:
            return gst.FlowReturn.OK
        try:
            data = GLib.Bytes.new(bytes(mapinfo.data))
        finally:
            buf.unmap(mapinfo)
        try:
            texture = Gdk.MemoryTexture.new(
                w, h, Gdk.MemoryFormat.R8G8B8A8, data, w * 4,
            )
        except Exception:
            LOGGER.debug("MemoryTexture.new failed", exc_info=True)
            return gst.FlowReturn.OK
        # set_paintable must run on the main loop.
        GLib.idle_add(self._picture.set_paintable, texture)
        return gst.FlowReturn.OK

    def _on_bus_message(self, _bus: Any, message: Any) -> None:
        gst = self._Gst
        if message.type == gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            err_msg = err.message if err else "?"
            dbg_text = (dbg or "").strip()
            LOGGER.error(
                "GStreamer pipeline error: %s (debug: %s)",
                err_msg, dbg_text or "<none>",
            )

            # Best-effort interpretation of common v4l2 failure modes so
            # the toast tells the user something they can act on.
            hint = self._interpret_v4l2_error(err_msg, dbg_text)
            self._fail(hint if hint else f"Camera error: {err_msg}")

    def _interpret_v4l2_error(self, message: str, debug: str) -> str | None:
        combined = (message + " " + debug).lower()
        device = self._current_device()
        name = device.get("name") if device else None
        path = device.get("path") if device else ""
        suffix = f" ({path})" if path else ""
        if "inappropriate ioctl" in combined or "enotty" in combined:
            return self._(
                "Camera node%s isn't a v4l2 capture device. Try the "
                "switch-camera button or open a different node."
            ) % suffix
        if "busy" in combined or "ebusy" in combined or "resource busy" in combined:
            return self._(
                "Camera%s is in use by another app — close it and retry."
            ) % suffix
        if "permission" in combined or "eacces" in combined:
            return self._(
                "No permission to open %s. Add yourself to the 'video' "
                "group: sudo usermod -a -G video $USER"
            ) % (path or self._("camera"))
        if "not-negotiated" in combined or "no common" in combined:
            return self._(
                "Camera and preview pipeline couldn't agree on a format. "
                "Pick a lower resolution from the menu."
            )
        return None

    def _fail(self, message: str) -> None:
        LOGGER.warning("Camera pipeline failed: %s", message)
        self._stop_pipeline()
        self._show_toast(message)
        self._shutter.set_sensitive(False)

    # ------------------------------------------------------------------
    # Resolution picker
    # ------------------------------------------------------------------

    def _populate_resolutions(self, device: dict[str, Any]) -> None:
        # Uses raw-or-jpeg union so devices that only expose MJPG (most
        # UVC cams at high resolutions) still get a working picker.
        resolutions = _resolutions_from_caps(device.get("caps"))
        if len(resolutions) < 2:
            self._res_button.set_visible(False)
            return

        # Limit to a reasonable handful so the popover stays compact.
        if len(resolutions) > 8:
            # Always keep max + min, then a spread in between.
            keep = [resolutions[0], resolutions[-1]]
            step = max(1, (len(resolutions) - 2) // 6)
            keep.extend(resolutions[1:-1:step])
            resolutions = sorted(set(keep), key=lambda wh: -(wh[0] * wh[1]))

        popover = Gtk.Popover()
        popover.set_autohide(True)
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")
        popover.set_child(list_box)

        current = self._selected_resolution or resolutions[0]
        for w, h in resolutions:
            ratio = self._aspect_label(w, h)
            row_label = Gtk.Label(
                label=f"{w}×{h}  {ratio}",
                xalign=0.0,
            )
            row_label.set_margin_top(8); row_label.set_margin_bottom(8)
            row_label.set_margin_start(12); row_label.set_margin_end(12)
            row = Gtk.ListBoxRow()
            row.set_child(row_label)
            row.set_activatable(True)
            row._yaga_res = (w, h)  # type: ignore[attr-defined]
            list_box.append(row)

        def on_activated(_lb: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
            wh = getattr(row, "_yaga_res", None)
            popover.popdown()
            if wh is None or wh == self._selected_resolution:
                return
            self._selected_resolution = wh
            self._res_button.set_label(f"{wh[0]}×{wh[1]}")
            GLib.idle_add(self._start_pipeline)

        list_box.connect("row-activated", on_activated)

        self._res_popover = popover
        self._res_button.set_popover(popover)
        self._res_button.set_label(f"{current[0]}×{current[1]}")
        self._res_button.set_visible(True)

    # ------------------------------------------------------------------
    # V4L2 controls panel
    # ------------------------------------------------------------------

    def _mark_controls_dirty_for_device(self, device: dict[str, Any]) -> None:
        """Cheap pre-pipeline-start state reset. Does NOT touch the device
        via v4l2-ctl — that runs only when the user actually opens the
        gear popover. We still show the gear unconditionally when
        v4l2-ctl is installed; if the device turns out to have nothing
        tunable, the post-probe code hides it."""
        path = device.get("path") or ""
        self._controls = {}
        if self._gear_popover is not None:
            self._gear_button.set_popover(None)
            self._gear_popover = None
        self._controls_built = False
        # Show the gear if v4l2-ctl is available and we have a /dev path
        # to probe. The probe itself happens on first popover-open.
        self._gear_button.set_visible(
            bool(path) and camera_controls.controls_supported()
        )

    def _ensure_controls_probed(self) -> None:
        """Run the v4l2-ctl probe for the active device if we haven't yet.
        Called when the gear popover is first opened — never at pipeline
        start, so the probe can't interfere with v4l2src negotiation."""
        device = self._current_device()
        if device is None:
            return
        path = device.get("path") or ""
        if not path or not camera_controls.controls_supported():
            self._controls = {}
            return
        cached = self._controls_cache.get(path)
        if cached is None:
            cached = camera_controls.probe_controls(path)
            self._controls_cache[path] = cached
        self._controls = cached
        # Now that we know what's tunable, hide the gear if it turned
        # out to have nothing useful.
        has_any = any(
            camera_controls.resolve(cached, logical) is not None
            for logical in (
                "auto_exposure", "exposure_absolute",
                "auto_white_balance", "white_balance_temp",
                "auto_focus", "focus_absolute",
                "gain", "brightness", "contrast", "saturation",
            )
        )
        self._gear_button.set_visible(has_any)

    def _on_gear_toggled(self, _btn: Gtk.MenuButton, _pspec: Any) -> None:
        if self._gear_button.get_active() and not self._controls_built:
            self._ensure_controls_probed()
            self._build_controls_popover()

    def _build_controls_popover(self) -> None:
        popover = Gtk.Popover()
        popover.set_autohide(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10); box.set_margin_bottom(10)
        box.set_margin_start(10); box.set_margin_end(10)
        box.set_size_request(280, -1)
        popover.set_child(box)

        # Exposure section.
        exp_auto = camera_controls.resolve(self._controls, "auto_exposure")
        exp_abs = camera_controls.resolve(self._controls, "exposure_absolute")
        exp_slider: Gtk.Scale | None = None
        if exp_auto is not None or exp_abs is not None:
            self._add_section_header(box, self._("Exposure"))
            if exp_auto is not None and exp_auto.type == "menu":
                manual_v, auto_v = self._auto_manual_values(exp_auto.menu)
                if manual_v is not None and auto_v is not None:
                    auto_switch = self._switch_row(
                        box, self._("Auto"), exp_auto.value == auto_v
                    )
                    auto_switch.connect(
                        "notify::active",
                        lambda sw, _p: self._apply_auto(
                            exp_auto, sw.get_active(), auto_v, manual_v, [exp_slider]
                        ),
                    )
            if exp_abs is not None and exp_abs.type == "int":
                exp_slider = self._slider_row(
                    box, self._("Time"), exp_abs,
                    lambda v, ctrl=exp_abs: camera_controls.set_control(
                        self._current_device_path(), ctrl.name, v
                    ),
                )
                exp_slider.set_sensitive(not exp_abs.inactive)

        # White balance section.
        wb_auto = camera_controls.resolve(self._controls, "auto_white_balance")
        wb_temp = camera_controls.resolve(self._controls, "white_balance_temp")
        wb_slider: Gtk.Scale | None = None
        if wb_auto is not None or wb_temp is not None:
            self._add_section_header(box, self._("White balance"))
            if wb_auto is not None:
                if wb_auto.type == "bool":
                    wb_switch = self._switch_row(
                        box, self._("Auto"), bool(wb_auto.value)
                    )
                    wb_switch.connect(
                        "notify::active",
                        lambda sw, _p: self._apply_bool(
                            wb_auto, sw.get_active(), [wb_slider]
                        ),
                    )
                elif wb_auto.type == "menu":
                    m_v, a_v = self._auto_manual_values(wb_auto.menu)
                    if m_v is not None and a_v is not None:
                        wb_switch = self._switch_row(
                            box, self._("Auto"), wb_auto.value == a_v
                        )
                        wb_switch.connect(
                            "notify::active",
                            lambda sw, _p: self._apply_auto(
                                wb_auto, sw.get_active(), a_v, m_v, [wb_slider]
                            ),
                        )
            if wb_temp is not None and wb_temp.type == "int":
                wb_slider = self._slider_row(
                    box, self._("Temperature"), wb_temp,
                    lambda v, ctrl=wb_temp: camera_controls.set_control(
                        self._current_device_path(), ctrl.name, v
                    ),
                )
                wb_slider.set_sensitive(not wb_temp.inactive)

        # Focus section.
        focus_auto = camera_controls.resolve(self._controls, "auto_focus")
        focus_abs = camera_controls.resolve(self._controls, "focus_absolute")
        focus_slider: Gtk.Scale | None = None
        if focus_auto is not None or focus_abs is not None:
            self._add_section_header(box, self._("Focus"))
            if focus_auto is not None and focus_auto.type == "bool":
                fsw = self._switch_row(box, self._("Auto"), bool(focus_auto.value))
                fsw.connect(
                    "notify::active",
                    lambda sw, _p: self._apply_bool(
                        focus_auto, sw.get_active(), [focus_slider]
                    ),
                )
            if focus_abs is not None and focus_abs.type == "int":
                focus_slider = self._slider_row(
                    box, self._("Position"), focus_abs,
                    lambda v, ctrl=focus_abs: camera_controls.set_control(
                        self._current_device_path(), ctrl.name, v
                    ),
                )
                focus_slider.set_sensitive(not focus_abs.inactive)

        # Image section (always present if we have any of these).
        image_controls: list[tuple[str, str]] = [
            ("gain", self._("Gain")),
            ("brightness", self._("Brightness")),
            ("contrast", self._("Contrast")),
            ("saturation", self._("Saturation")),
        ]
        image_added = False
        for logical, label in image_controls:
            ctrl = camera_controls.resolve(self._controls, logical)
            if ctrl is None or ctrl.type != "int":
                continue
            if not image_added:
                self._add_section_header(box, self._("Image"))
                image_added = True
            self._slider_row(
                box, label, ctrl,
                lambda v, c=ctrl: camera_controls.set_control(
                    self._current_device_path(), c.name, v
                ),
            )

        # Reset button at the bottom — restores driver defaults across all
        # exposed controls so the user can recover from a tweak experiment.
        reset = Gtk.Button(label=self._("Reset to defaults"))
        reset.add_css_class("flat")
        reset.set_margin_top(8)
        reset.connect("clicked", lambda _b: self._reset_controls_to_default())
        box.append(reset)

        self._gear_popover = popover
        self._gear_button.set_popover(popover)
        self._controls_built = True

    def _add_section_header(self, parent: Gtk.Box, text: str) -> None:
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class("heading")
        label.set_margin_top(4)
        parent.append(label)

    def _switch_row(self, parent: Gtk.Box, text: str, active: bool) -> Gtk.Switch:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.set_hexpand(True)
        row.append(lbl)
        sw = Gtk.Switch()
        sw.set_active(active)
        sw.set_valign(Gtk.Align.CENTER)
        row.append(sw)
        parent.append(row)
        return sw

    def _slider_row(
        self,
        parent: Gtk.Box,
        text: str,
        ctrl: V4l2Control,
        on_change: Callable[[int], Any],
    ) -> Gtk.Scale:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.set_size_request(80, -1)
        row.append(lbl)
        lo = ctrl.min if ctrl.min is not None else 0
        hi = ctrl.max if ctrl.max is not None else 100
        step = max(1, ctrl.step or 1)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        if ctrl.value is not None:
            scale.set_value(ctrl.value)

        # Debounce writes — dragging a slider fires value-changed dozens of
        # times per second; we don't need to launch a subprocess per tick.
        pending: dict[str, Any] = {"timeout": None, "value": None}

        def fire() -> bool:
            pending["timeout"] = None
            v = pending["value"]
            if v is None:
                return False
            on_change(int(v))
            return False

        def on_value_changed(s: Gtk.Scale) -> None:
            pending["value"] = s.get_value()
            if pending["timeout"] is not None:
                GLib.source_remove(pending["timeout"])
            pending["timeout"] = GLib.timeout_add(80, fire)

        scale.connect("value-changed", on_value_changed)
        row.append(scale)
        parent.append(row)
        return scale

    def _auto_manual_values(self, menu: dict[int, str]) -> tuple[int | None, int | None]:
        """For a v4l2 exposure-style menu, pick the (manual, auto) values.
        'Manual' is the obvious one; 'auto' falls back through "Auto Mode",
        "Aperture Priority Mode", then anything else."""
        manual: int | None = None
        auto: int | None = None
        for v, label in menu.items():
            if "manual" in label.lower():
                manual = v
                break
        for v, label in menu.items():
            if v == manual:
                continue
            if "auto" in label.lower():
                auto = v
                break
        if auto is None:
            for v, label in menu.items():
                if v == manual:
                    continue
                if "aperture" in label.lower():
                    auto = v
                    break
        if auto is None:
            for v in menu:
                if v != manual:
                    auto = v
                    break
        return manual, auto

    def _apply_auto(
        self,
        ctrl: V4l2Control,
        auto_on: bool,
        auto_value: int,
        manual_value: int,
        dependents: list[Gtk.Scale | None],
    ) -> None:
        target = auto_value if auto_on else manual_value
        ok = camera_controls.set_control(
            self._current_device_path(), ctrl.name, target
        )
        if not ok:
            return
        ctrl.value = target
        # When auto is on, manual sliders are masked by the kernel — disable
        # them locally to mirror that without needing a re-probe.
        for dep in dependents:
            if dep is not None:
                dep.set_sensitive(not auto_on)

    def _apply_bool(
        self,
        ctrl: V4l2Control,
        on: bool,
        dependents: list[Gtk.Scale | None],
    ) -> None:
        ok = camera_controls.set_control(
            self._current_device_path(), ctrl.name, 1 if on else 0
        )
        if not ok:
            return
        ctrl.value = 1 if on else 0
        for dep in dependents:
            if dep is not None:
                dep.set_sensitive(not on)

    def _reset_controls_to_default(self) -> None:
        path = self._current_device_path()
        if not path:
            return
        for ctrl in self._controls.values():
            if ctrl.default is None or ctrl.readonly:
                continue
            camera_controls.set_control(path, ctrl.name, ctrl.default)
        # Force a rebuild on next open so the UI reflects the reset values.
        self._controls_cache.pop(path, None)
        self._controls = camera_controls.probe_controls(path)
        self._controls_cache[path] = self._controls
        if self._gear_popover is not None:
            self._gear_button.set_popover(None)
            self._gear_popover = None
        self._controls_built = False
        self._show_toast(self._("Controls reset"))

    def _current_device_path(self) -> str:
        device = self._current_device()
        return device.get("path") or "" if device else ""

    @staticmethod
    def _aspect_label(w: int, h: int) -> str:
        if w == 0 or h == 0:
            return ""
        # Common-case aspects first.
        from math import gcd
        g = gcd(w, h)
        a, b = w // g, h // g
        # Snap to recognized aspects when close.
        candidates = {(16, 9), (4, 3), (3, 2), (21, 9), (1, 1), (5, 4)}
        for ca, cb in candidates:
            if abs(a / b - ca / cb) < 0.02:
                return f"{ca}:{cb}"
        return f"{a}:{b}" if a < 30 else ""

    # ------------------------------------------------------------------
    # Shutter / self-timer
    # ------------------------------------------------------------------

    def _on_shutter(self) -> None:
        # If a countdown is running, treat shutter-press as cancel.
        if self._countdown_source is not None:
            self._cancel_countdown()
            return
        delay = self._timer_choices[self._timer_idx]
        if delay <= 0:
            self._capture()
        else:
            self._start_countdown(delay)

    def _refresh_timer_button(self) -> None:
        value = self._timer_choices[self._timer_idx]
        if value == 0:
            self._timer_button.set_icon_name("alarm-symbolic")
            self._timer_button.set_tooltip_text(self._("Self-timer off"))
        else:
            self._timer_button.set_label(f"{value}s")
            self._timer_button.set_tooltip_text(
                self._("Self-timer: %d seconds") % value
            )

    def _cycle_timer(self) -> None:
        self._timer_idx = (self._timer_idx + 1) % len(self._timer_choices)
        self._refresh_timer_button()
        if self._countdown_source is not None:
            self._cancel_countdown()

    def _start_countdown(self, seconds: int) -> None:
        self._countdown_value = seconds
        self._countdown.set_text(str(seconds))
        self._countdown.set_visible(True)
        self._countdown_source = GLib.timeout_add_seconds(1, self._tick_countdown)

    def _tick_countdown(self) -> bool:
        self._countdown_value -= 1
        if self._countdown_value <= 0:
            self._countdown.set_visible(False)
            self._countdown_source = None
            self._capture()
            return False
        self._countdown.set_text(str(self._countdown_value))
        return True

    def _cancel_countdown(self) -> None:
        if self._countdown_source is not None:
            GLib.source_remove(self._countdown_source)
            self._countdown_source = None
        self._countdown.set_visible(False)

    # ------------------------------------------------------------------
    # Grid toggle
    # ------------------------------------------------------------------

    def _on_grid_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._grid_on = btn.get_active()
        self._grid.set_visible(self._grid_on)

    # ------------------------------------------------------------------
    # Zoom (digital, widget-snapshot only)
    # ------------------------------------------------------------------

    def _apply_zoom(self, zoom: float) -> None:
        zoom = max(1.0, min(self._zoom_max, zoom))
        self._zoom = zoom
        self._picture.set_zoom(zoom)

    def _on_zoom_begin(self, _gesture: Gtk.GestureZoom, _seq: Any) -> None:
        self._zoom_base = self._zoom

    def _on_zoom_changed(self, _gesture: Gtk.GestureZoom, scale: float) -> None:
        self._apply_zoom(self._zoom_base * scale)

    def _on_scroll(self, _ctl: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        if dy == 0:
            return False
        factor = 0.9 if dy > 0 else 1.1
        self._apply_zoom(self._zoom * factor)
        return True

    # ------------------------------------------------------------------
    # Geotagging
    # ------------------------------------------------------------------

    def _on_geo_toggled(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            if not _HAS_GEXIV2:
                btn.set_active(False)
                self._show_toast(self._("GExiv2 missing — geotag unavailable"))
                return
            self._geo = GeoClient(app_id="yaga")
            ok = self._geo.start(
                accuracy=5,
                on_update=self._on_geo_update,
                on_error=lambda msg: GLib.idle_add(self._on_geo_error, msg),
            )
            if not ok:
                btn.set_active(False)
                self._geo = None
                self._show_toast(self._("GeoClue not available"))
                return
            self._show_toast(self._("Locating…"))
        else:
            if self._geo is not None:
                self._geo.stop()
                self._geo = None

    def _on_geo_update(self, location: dict[str, Any] | None) -> None:
        # Signal arrives on the main loop already (GDBus dispatches there),
        # so it's safe to touch widgets without idle_add. Toast only on the
        # first fix to avoid spamming as location refines.
        if location is None:
            return
        if getattr(self, "_geo_toasted", False):
            return
        self._geo_toasted = True
        self._show_toast(self._("Location ready"))

    def _on_geo_error(self, message: str) -> bool:
        self._show_toast(message)
        self._geo_button.set_active(False)
        return False

    # ------------------------------------------------------------------
    # Tap-to-focus
    # ------------------------------------------------------------------

    def _on_tap_to_focus(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
    ) -> None:
        if n_press != 1:
            return
        # Picture coords -> overlay coords. Since the focus_rect drawing
        # area shares the overlay allocation with the picture, no
        # translation is required.
        self._focus_point = (x, y)
        self._focus_rect.queue_draw()
        if self._focus_hide_source is not None:
            GLib.source_remove(self._focus_hide_source)
        self._focus_hide_source = GLib.timeout_add(700, self._hide_focus_rect)
        self._fire_autofocus()

    def _hide_focus_rect(self) -> bool:
        self._focus_point = None
        self._focus_hide_source = None
        self._focus_rect.queue_draw()
        return False

    def _draw_focus_rect(
        self, _da: Gtk.DrawingArea, cr: Any, _w: int, _h: int
    ) -> None:
        if self._focus_point is None:
            return
        fx, fy = self._focus_point
        size = 60
        cr.set_line_width(2.0)
        # Outer shadow for contrast on bright scenes.
        cr.set_source_rgba(0, 0, 0, 0.6)
        cr.rectangle(fx - size / 2 + 1, fy - size / 2 + 1, size, size)
        cr.stroke()
        cr.set_source_rgba(1.0, 0.82, 0.10, 0.95)
        cr.rectangle(fx - size / 2, fy - size / 2, size, size)
        cr.stroke()
        # Tiny corner ticks like classic AF indicators.
        for ox, oy in ((-size / 2, 0), (size / 2, 0), (0, -size / 2), (0, size / 2)):
            cr.move_to(fx + ox * 0.6, fy + oy * 0.6)
            cr.line_to(fx + ox, fy + oy)
        cr.stroke()

    def _fire_autofocus(self) -> None:
        """Trigger one autofocus cycle if the device exposes the V4L2
        button control for it. Visual indicator runs regardless so the
        user gets feedback even when the hardware can't act on it."""
        af = camera_controls.resolve(self._controls, "auto_focus_start")
        if af is None:
            return
        camera_controls.set_control(self._current_device_path(), af.name, 1)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _switch_camera(self) -> None:
        if len(self._devices) <= 1:
            return
        self._device_index = (self._device_index + 1) % len(self._devices)
        self._selected_resolution = None
        GLib.idle_add(self._start_pipeline)

    def _capture(self) -> None:
        if self._busy_capture or self._appsink is None:
            return
        self._busy_capture = True
        self._shutter.set_sensitive(False)
        # Hook a one-shot new-sample listener, then open the valve so a
        # single frame flows down to jpegenc + appsink. The valve gating
        # means jpegenc isn't burning CPU on every preview frame, which is
        # critical on Halium phones (otherwise the preview pipeline OOMs).
        self._capture_signal_id = self._appsink.connect(
            "new-sample", self._on_capture_sample
        )
        if self._valve is not None:
            self._valve.set_property("drop", False)
        # Safety timeout — if no sample reaches us in 2 s, give up cleanly.
        self._capture_timeout_id = GLib.timeout_add_seconds(
            2, self._on_capture_timeout
        )

    def _close_valve_and_disconnect(self) -> None:
        if self._valve is not None:
            try:
                self._valve.set_property("drop", True)
            except Exception:
                LOGGER.debug("valve close failed", exc_info=True)
        if self._capture_signal_id is not None and self._appsink is not None:
            try:
                self._appsink.disconnect(self._capture_signal_id)
            except Exception:
                pass
        self._capture_signal_id = None
        if self._capture_timeout_id is not None:
            GLib.source_remove(self._capture_timeout_id)
            self._capture_timeout_id = None

    def _on_capture_sample(self, sink: Any) -> Any:
        gst = self._Gst
        try:
            sample = sink.emit("pull-sample")
        except Exception:
            sample = None
        # Disconnect + close valve from the main loop, then write the file.
        GLib.idle_add(self._finish_capture, sample)
        return gst.FlowReturn.OK

    def _finish_capture(self, sample: Any) -> bool:
        self._close_valve_and_disconnect()
        if sample is None:
            self._show_toast(self._("No frame available"))
        else:
            self._write_sample(sample)
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        return False

    def _on_capture_timeout(self) -> bool:
        # Reached only when no sample showed up — pipeline stalled or the
        # camera produces no frames. Reset state and tell the user.
        self._capture_timeout_id = None
        self._close_valve_and_disconnect()
        self._show_toast(self._("No frame available"))
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        return False

    def _write_sample(self, sample: Any) -> None:
        buf = sample.get_buffer() if sample is not None else None
        if buf is None:
            self._show_toast(self._("No frame available"))
            return
        success, mapinfo = buf.map(self._Gst.MapFlags.READ)
        if not success:
            self._show_toast(self._("Could not read frame"))
            return
        try:
            data = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)

        self._save_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = self._save_dir / f"yaga_{stamp}.jpg"
        i = 1
        while path.exists():
            path = self._save_dir / f"yaga_{stamp}_{i}.jpg"
            i += 1
        try:
            path.write_bytes(data)
        except OSError as exc:
            self._show_toast(self._("Failed to save: %s") % exc)
            return

        self._write_exif(path)
        self._flash_screen()
        self._show_toast(self._("Saved %s") % path.name)
        if self._on_captured is not None:
            try:
                self._on_captured(path)
            except Exception:
                LOGGER.debug("on_captured callback failed", exc_info=True)

    def _write_exif(self, path: Path) -> None:
        if not _HAS_GEXIV2:
            return
        device = self._current_device()
        model = device.get("name") if device else None
        now = time.strftime("%Y:%m:%d %H:%M:%S")
        try:
            md = GExiv2.Metadata()  # type: ignore[union-attr]
            md.open_path(str(path))
            md.set_tag_string("Exif.Image.Make", "Yaga")
            if model:
                # Strip non-printable bits some PipeWire descriptions carry.
                clean = re.sub(r"[^\x20-\x7e]+", " ", model).strip()
                if clean:
                    md.set_tag_string("Exif.Image.Model", clean[:64])
            md.set_tag_string("Exif.Image.Software", "Yaga")
            md.set_tag_string("Exif.Image.DateTime", now)
            md.set_tag_string("Exif.Photo.DateTimeOriginal", now)
            md.set_tag_string("Exif.Photo.DateTimeDigitized", now)

            # Geotag if the user has opted in and we have a fresh fix.
            if self._geo is not None:
                location = self._geo.latest()
                if location is not None:
                    try:
                        md.set_gps_info(
                            location["lon"], location["lat"], location.get("alt", 0.0)
                        )
                        md.set_tag_string(
                            "Exif.GPSInfo.GPSProcessingMethod", "GeoClue"
                        )
                    except Exception:
                        LOGGER.debug("set_gps_info failed", exc_info=True)

            md.save_file(str(path))
        except Exception:
            LOGGER.debug("Could not write EXIF for %s", path, exc_info=True)

    # ------------------------------------------------------------------
    # Screen-flash
    # ------------------------------------------------------------------

    def _flash_screen(self) -> None:
        if self._flash_source is not None:
            GLib.source_remove(self._flash_source)
            self._flash_source = None
        self._flash.set_opacity(0.85)
        self._flash.set_visible(True)
        # Fade in ~12 steps over ~240ms to keep the flash brief but visible.
        self._flash_step = 0

        def step() -> bool:
            self._flash_step += 1
            opacity = max(0.0, 0.85 - self._flash_step * 0.085)
            self._flash.set_opacity(opacity)
            if opacity <= 0.0:
                self._flash.set_visible(False)
                self._flash_source = None
                return False
            return True

        self._flash_source = GLib.timeout_add(20, step)

    # ------------------------------------------------------------------
    # Window chrome substitutes
    # ------------------------------------------------------------------

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        event = gesture.get_current_event()
        if event is None:
            return
        surface = self.get_surface()
        device = event.get_device()
        if surface is None or device is None:
            return
        try:
            surface.begin_move(device, 1, _x, _y, event.get_time())
        except Exception:
            LOGGER.debug("begin_move not supported", exc_info=True)

    def _on_key(self, _ctl: Gtk.EventControllerKey, keyval: int, _kc: int, _mods: Any) -> bool:
        if keyval == Gdk.KEY_Escape:
            if self._countdown_source is not None:
                self._cancel_countdown()
                return True
            self.close()
            return True
        if keyval in (Gdk.KEY_space, Gdk.KEY_Return):
            self._on_shutter()
            return True
        if keyval in (Gdk.KEY_g, Gdk.KEY_G):
            self._grid_button.set_active(not self._grid_button.get_active())
            return True
        return False

    def _show_toast(self, text: str, sticky: bool = False) -> None:
        self._toast.set_text(text)
        self._toast.set_visible(True)
        if self._toast_timer is not None:
            GLib.source_remove(self._toast_timer)
            self._toast_timer = None
        if not sticky:
            self._toast_timer = GLib.timeout_add_seconds(3, self._hide_toast)

    def _hide_toast(self) -> bool:
        self._toast.set_visible(False)
        self._toast_timer = None
        return False

    def _on_close(self, _win: Any) -> bool:
        self._stop_pipeline()
        if self._toast_timer is not None:
            GLib.source_remove(self._toast_timer)
            self._toast_timer = None
        if self._countdown_source is not None:
            GLib.source_remove(self._countdown_source)
            self._countdown_source = None
        if self._flash_source is not None:
            GLib.source_remove(self._flash_source)
            self._flash_source = None
        if self._focus_hide_source is not None:
            GLib.source_remove(self._focus_hide_source)
            self._focus_hide_source = None
        if self._geo is not None:
            self._geo.stop()
            self._geo = None
        return False
