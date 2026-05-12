"""Camera window — frameless live preview + still capture using GStreamer."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, Gdk, GLib, Gtk

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


def _enumerate_devices(gst: Any) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    try:
        monitor = gst.DeviceMonitor.new()
        monitor.add_filter("Video/Source", None)
        monitor.start()
        for dev in monitor.get_devices() or []:
            props = dev.get_properties()
            path = ""
            if props is not None:
                for key in ("device.path", "api.v4l2.path", "object.path"):
                    val = props.get_string(key) if hasattr(props, "get_string") else None
                    if val:
                        path = val
                        break
            display = dev.get_display_name() or path or "Camera"
            devices.append({
                "name": display,
                "path": path,
                "source_factory": "v4l2src" if path.startswith("/dev/video") else "",
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
            })

    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for d in devices:
        key = d["path"] or d["name"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(d)
    return unique


def _corner_bracket(corner: str) -> Gtk.DrawingArea:
    """A single L-shaped viewfinder corner. corner ∈ {tl, tr, bl, br}."""
    area = Gtk.DrawingArea()
    area.set_content_width(28)
    area.set_content_height(28)

    def draw(_da: Gtk.DrawingArea, cr: Any, w: int, h: int) -> None:
        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.set_line_width(2.0)
        cr.set_line_cap(1)  # round
        pad = 3
        length = min(w, h) - pad - 4
        if corner == "tl":
            cr.move_to(pad, pad + length); cr.line_to(pad, pad); cr.line_to(pad + length, pad)
        elif corner == "tr":
            cr.move_to(w - pad - length, pad); cr.line_to(w - pad, pad); cr.line_to(w - pad, pad + length)
        elif corner == "bl":
            cr.move_to(pad, h - pad - length); cr.line_to(pad, h - pad); cr.line_to(pad + length, h - pad)
        else:  # br
            cr.move_to(w - pad - length, h - pad); cr.line_to(w - pad, h - pad); cr.line_to(w - pad, h - pad - length)
        cr.stroke()

    area.set_draw_func(draw)
    area.set_can_target(False)
    return area


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
        self._devices: list[dict[str, str]] = _enumerate_devices(self._Gst)
        self._device_index = 0
        self._busy_capture = False
        self._toast_timer: int | None = None

        overlay = Gtk.Overlay()
        self.set_content(overlay)

        self._picture = Gtk.Picture()
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        overlay.set_child(self._picture)

        # Viewfinder corner brackets — pure decoration, always available.
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

        # Close — needed because we strip the window frame.
        self._close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        self._close_button.add_css_class("camera-iconbtn")
        self._close_button.set_halign(Gtk.Align.START)
        self._close_button.set_valign(Gtk.Align.START)
        self._close_button.set_margin_top(16)
        self._close_button.set_margin_start(16)
        self._close_button.set_tooltip_text(self._("Close"))
        self._close_button.connect("clicked", lambda _b: self.close())
        overlay.add_overlay(self._close_button)

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
        self._shutter.connect("clicked", lambda _b: self._capture())
        overlay.add_overlay(self._shutter)

        # Camera-switch — only added when there is more than one capture device.
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

        # Transient toast for status / error messages.
        self._toast = Gtk.Label(label="")
        self._toast.add_css_class("camera-toast")
        self._toast.set_halign(Gtk.Align.CENTER)
        self._toast.set_valign(Gtk.Align.END)
        self._toast.set_margin_bottom(28)
        self._toast.set_visible(False)
        overlay.add_overlay(self._toast)

        # Window-level drag (since we have no titlebar) + ESC to close.
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        self.add_controller(drag)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self.connect("close-request", self._on_close)

        if not self._devices:
            self._shutter.set_sensitive(False)
            self._show_toast(self._("No camera detected"))
        else:
            GLib.idle_add(self._start_pipeline)

    # ------------------------------------------------------------------
    # Pipeline lifecycle
    # ------------------------------------------------------------------

    def _current_device(self) -> dict[str, str] | None:
        if not self._devices:
            return None
        return self._devices[self._device_index % len(self._devices)]

    def _build_pipeline_description(self, device: dict[str, str]) -> str:
        gst = self._Gst
        has_preview = gst.ElementFactory.find("gtk4paintablesink") is not None
        has_jpeg = gst.ElementFactory.find("jpegenc") is not None
        has_appsink = gst.ElementFactory.find("appsink") is not None

        path = device.get("path") or ""
        factory = device.get("source_factory") or ""
        if factory == "v4l2src" and path and gst.ElementFactory.find("v4l2src"):
            src = f'v4l2src device="{path}"'
        else:
            src = "autovideosrc"

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

        parts = [f"{src} ! videoconvert ! tee name=t", preview_branch]
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

        result = self._pipeline.set_state(gst.State.PLAYING)
        if result == gst.StateChangeReturn.FAILURE:
            self._fail(self._("Could not start camera"))
            return False

        self._shutter.set_sensitive(self._appsink is not None)
        if self._appsink is None:
            self._show_toast(self._("Capture unavailable"))
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
    # User actions
    # ------------------------------------------------------------------

    def _switch_camera(self) -> None:
        if len(self._devices) <= 1:
            return
        self._device_index = (self._device_index + 1) % len(self._devices)
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

        self._show_toast(self._("Saved %s") % path.name)
        if self._on_captured is not None:
            try:
                self._on_captured(path)
            except Exception:
                LOGGER.debug("on_captured callback failed", exc_info=True)

    # ------------------------------------------------------------------
    # Window chrome substitutes
    # ------------------------------------------------------------------

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        # Allow grab-and-move on the preview area since the OS titlebar is gone.
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
            self.close()
            return True
        if keyval in (Gdk.KEY_space, Gdk.KEY_Return):
            self._capture()
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
        return False
