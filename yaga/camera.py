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
    )


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


def _enumerate_devices(gst: Any) -> list[dict[str, Any]]:
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
            })

    # UVC drivers expose multiple /dev/video* nodes per physical camera
    # (capture + metadata + ISP variants). Dedupe by display name and keep
    # the entry with the richest raw-caps surface so the resolution picker
    # has the most modes to offer.
    by_name: dict[str, dict[str, Any]] = {}
    for d in devices:
        if _is_ir_name(d["name"]):
            LOGGER.debug("Filtering IR device: %s", d["name"])
            continue
        # Pure metadata-only nodes have no raw modes — drop them.
        if not _resolutions_from_caps(d.get("caps")):
            LOGGER.debug("Filtering metadata-only device: %s (%s)", d["name"], d["path"])
            continue
        key = d["name"] or d["path"]
        existing = by_name.get(key)
        if existing is None:
            by_name[key] = d
            continue
        # Prefer the device with more raw resolutions.
        if len(_resolutions_from_caps(d.get("caps"))) > len(
            _resolutions_from_caps(existing.get("caps"))
        ):
            by_name[key] = d
    return list(by_name.values())


def _resolutions_from_caps(caps: Any) -> list[tuple[int, int]]:
    """Extract de-duplicated (w, h) pairs from a GstCaps. Only raw video for
    now — MJPEG-only resolutions are skipped to keep pipeline construction
    deterministic."""
    if caps is None:
        return []
    sizes: set[tuple[int, int]] = set()
    try:
        n = caps.get_size()
    except Exception:
        return []
    for i in range(n):
        s = caps.get_structure(i)
        if s is None:
            continue
        if s.get_name() != "video/x-raw":
            continue
        ok_w, w = s.get_int("width")
        ok_h, h = s.get_int("height")
        if ok_w and ok_h and w > 0 and h > 0:
            sizes.add((w, h))
    out = sorted(sizes, key=lambda wh: -(wh[0] * wh[1]))
    return out


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
    """Gtk.Picture that can render its content horizontally flipped.

    Only the on-screen render is flipped — captured frames are unaffected,
    so text in front-cam selfies still reads correctly in saved files.
    """

    __gtype_name__ = "YagaMirroredPicture"

    def __init__(self) -> None:
        super().__init__()
        self._mirrored = False

    def set_mirrored(self, mirrored: bool) -> None:
        if self._mirrored == mirrored:
            return
        self._mirrored = mirrored
        self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:  # type: ignore[override]
        if not self._mirrored:
            Gtk.Picture.do_snapshot(self, snapshot)
            return
        w = self.get_width()
        snapshot.save()
        snapshot.translate(Graphene.Point().init(w, 0))
        snapshot.scale(-1.0, 1.0)
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
        self._videocrop: Any = None
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
        self._frame_size: tuple[int, int] = (0, 0)
        self._selected_resolution: tuple[int, int] | None = None
        self._flash_source: int | None = None

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

    def _build_pipeline_description(self, device: dict[str, Any]) -> str:
        gst = self._Gst
        has_preview = gst.ElementFactory.find("gtk4paintablesink") is not None
        has_jpeg = gst.ElementFactory.find("jpegenc") is not None
        has_appsink = gst.ElementFactory.find("appsink") is not None
        has_videocrop = gst.ElementFactory.find("videocrop") is not None

        path = device.get("path") or ""
        factory = device.get("source_factory") or ""
        if factory == "v4l2src" and path and gst.ElementFactory.find("v4l2src"):
            src = f'v4l2src device="{path}"'
        else:
            src = "autovideosrc"

        # Optional resolution capsfilter right after the source.
        cap_filter = ""
        if self._selected_resolution is not None:
            w, h = self._selected_resolution
            cap_filter = (
                f' ! capsfilter name=resfilter caps="video/x-raw,width={w},height={h}"'
            )

        crop = " ! videocrop name=zoom left=0 right=0 top=0 bottom=0" if has_videocrop else ""

        preview_branch = (
            "t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert "
            "   ! gtk4paintablesink name=preview"
            if has_preview
            else "t. ! queue leaky=downstream max-size-buffers=2 ! fakesink sync=false"
        )
        snapshot_branch = (
            "t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert "
            "   ! jpegenc quality=92 "
            "   ! appsink name=snap emit-signals=false max-buffers=1 drop=true sync=false"
            if (has_jpeg and has_appsink)
            else ""
        )

        parts = [f"{src}{cap_filter}{crop} ! videoconvert ! tee name=t", preview_branch]
        if snapshot_branch:
            parts.append(snapshot_branch)
        return " ".join(parts)

    def _start_pipeline(self) -> bool:
        device = self._current_device()
        if device is None:
            return False
        gst = self._Gst
        self._stop_pipeline()
        desc = self._build_pipeline_description(device)
        try:
            self._pipeline = gst.parse_launch(desc)
        except Exception as exc:
            self._fail(f"Pipeline error: {exc}")
            return False

        self._appsink = self._pipeline.get_by_name("snap")
        self._videocrop = self._pipeline.get_by_name("zoom")
        self._capsfilter = self._pipeline.get_by_name("resfilter")
        self._frame_size = (0, 0)
        # Reset zoom for the new pipeline; otherwise stale crop values from
        # the previous device leak through if it had a different frame size.
        self._zoom = 1.0

        self._bus = self._pipeline.get_bus()
        if self._bus is not None:
            self._bus.add_signal_watch()
            self._bus.connect("message", self._on_bus_message)

        sink = self._pipeline.get_by_name("preview")
        if sink is not None:
            try:
                paintable = sink.get_property("paintable")
                if paintable is not None:
                    self._picture.set_paintable(paintable)
            except Exception:
                LOGGER.debug("Could not bind preview paintable", exc_info=True)

        # Mirror the preview only when the active device looks like a
        # front/user-facing camera. Saved frames stay unflipped — the mirror
        # is widget-side only.
        self._picture.set_mirrored(device.get("location") == "front")

        result = self._pipeline.set_state(gst.State.PLAYING)
        if result == gst.StateChangeReturn.FAILURE:
            self._fail(self._("Could not start camera"))
            return False

        self._shutter.set_sensitive(self._appsink is not None)
        if self._appsink is None:
            self._show_toast(self._("Capture unavailable"))

        # Now that the pipeline has been built, the device's caps are known —
        # populate the resolution picker for this device.
        self._populate_resolutions(device)
        return False

    def _stop_pipeline(self) -> None:
        if self._bus is not None:
            try:
                self._bus.remove_signal_watch()
            except Exception:
                pass
            self._bus = None
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(self._Gst.State.NULL)
            except Exception:
                pass
            self._pipeline = None
        self._appsink = None
        self._videocrop = None
        self._capsfilter = None

    def _on_bus_message(self, _bus: Any, message: Any) -> None:
        gst = self._Gst
        if message.type == gst.MessageType.ERROR:
            err, _dbg = message.parse_error()
            self._fail(f"Camera error: {err}")

    def _fail(self, message: str) -> None:
        LOGGER.warning("Camera pipeline failed: %s", message)
        self._stop_pipeline()
        self._show_toast(message)
        self._shutter.set_sensitive(False)

    # ------------------------------------------------------------------
    # Resolution picker
    # ------------------------------------------------------------------

    def _populate_resolutions(self, device: dict[str, Any]) -> None:
        resolutions = _resolutions_from_caps(device.get("caps"))
        if len(resolutions) < 2:
            # Only one (or zero) raw mode — no menu needed.
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
    # Zoom (digital, via videocrop)
    # ------------------------------------------------------------------

    def _frame_dimensions(self) -> tuple[int, int]:
        if self._frame_size[0] > 0:
            return self._frame_size
        if self._videocrop is None:
            return (0, 0)
        try:
            pad = self._videocrop.get_static_pad("sink")
            if pad is None:
                return (0, 0)
            caps = pad.get_current_caps()
            if caps is None or caps.get_size() == 0:
                return (0, 0)
            s = caps.get_structure(0)
            ok_w, w = s.get_int("width")
            ok_h, h = s.get_int("height")
            if ok_w and ok_h:
                self._frame_size = (w, h)
                return self._frame_size
        except Exception:
            return (0, 0)
        return (0, 0)

    def _apply_zoom(self, zoom: float) -> None:
        zoom = max(1.0, min(self._zoom_max, zoom))
        self._zoom = zoom
        if self._videocrop is None:
            return
        w, h = self._frame_dimensions()
        if w == 0 or h == 0:
            return
        # videocrop edges cannot reach the center pixel; clamp generously.
        max_crop_w = max(0, w // 2 - 2)
        max_crop_h = max(0, h // 2 - 2)
        crop_w = min(max_crop_w, int(w * (1 - 1 / zoom) / 2))
        crop_h = min(max_crop_h, int(h * (1 - 1 / zoom) / 2))
        try:
            self._videocrop.set_property("left", crop_w)
            self._videocrop.set_property("right", crop_w)
            self._videocrop.set_property("top", crop_h)
            self._videocrop.set_property("bottom", crop_h)
        except Exception:
            LOGGER.debug("videocrop set-property failed", exc_info=True)

    def _on_zoom_begin(self, _gesture: Gtk.GestureZoom, _seq: Any) -> None:
        self._zoom_base = self._zoom

    def _on_zoom_changed(self, _gesture: Gtk.GestureZoom, scale: float) -> None:
        self._apply_zoom(self._zoom_base * scale)

    def _on_scroll(self, _ctl: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        # Ctrl-less wheel zooms — matches Snapshot's affordance.
        if dy == 0:
            return False
        factor = 0.9 if dy > 0 else 1.1
        self._apply_zoom(self._zoom * factor)
        return True

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
        sample = self._appsink.get_property("last-sample")
        if sample is None:
            GLib.timeout_add(150, self._capture_retry)
            return
        self._write_sample(sample)
        self._busy_capture = False
        self._shutter.set_sensitive(True)

    def _capture_retry(self) -> bool:
        if self._appsink is None:
            return False
        sample = self._appsink.get_property("last-sample")
        if sample is None:
            self._show_toast(self._("No frame available"))
            self._busy_capture = False
            self._shutter.set_sensitive(True)
            return False
        self._write_sample(sample)
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

    def _show_toast(self, text: str) -> None:
        self._toast.set_text(text)
        self._toast.set_visible(True)
        if self._toast_timer is not None:
            GLib.source_remove(self._toast_timer)
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
        return False
