"""Camera window — live preview + still capture using GStreamer."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, GLib, Gtk

LOGGER = logging.getLogger(__name__)


class CameraError(RuntimeError):
    pass


def _gst() -> Any:
    try:
        gi.require_version("Gst", "1.0")
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
    """Return a list of {name, path, source_factory} dicts for video sources.

    Prefers GStreamer's device monitor so we get real capture-capable nodes
    (skipping metadata-only /dev/videoN entries some kernels expose), and
    falls back to a /dev/video* scan only if the monitor returns nothing.
    """
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

    # De-duplicate by /dev path while preserving order.
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for d in devices:
        key = d["path"] or d["name"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(d)
    return unique


class CameraWindow(Adw.Window):
    """Standalone window with live preview, capture and device rotation."""

    def __init__(
        self,
        parent: Gtk.Window,
        save_dir: Path,
        translator: Callable[[str], str] | None = None,
        on_captured: Callable[[Path], None] | None = None,
    ) -> None:
        super().__init__()
        self._ = translator or (lambda s: s)
        self.set_transient_for(parent)
        self.set_modal(False)
        self.set_default_size(720, 540)
        self.set_title(self._("Camera"))

        self._save_dir = Path(save_dir)
        self._on_captured = on_captured
        self._Gst = _gst()
        self._pipeline: Any = None
        self._bus: Any = None
        self._appsink: Any = None
        self._devices: list[dict[str, str]] = _enumerate_devices(self._Gst)
        self._device_index = 0
        self._busy_capture = False

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        self._title = Adw.WindowTitle(title=self._("Camera"), subtitle="")
        header.set_title_widget(self._title)

        self._rotate_button = Gtk.Button.new_from_icon_name("object-rotate-right-symbolic")
        self._rotate_button.set_tooltip_text(self._("Switch camera"))
        self._rotate_button.connect("clicked", lambda _b: self._switch_camera())
        header.pack_end(self._rotate_button)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar.set_content(body)

        self._picture = Gtk.Picture()
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_can_shrink(True)
        body.append(self._picture)

        self._status = Gtk.Label(label=self._("Starting camera…"), wrap=True, xalign=0.5)
        self._status.add_css_class("dim-label")
        self._status.set_margin_top(6)
        self._status.set_margin_bottom(6)
        body.append(self._status)

        action_bar = Gtk.ActionBar()
        action_bar.set_hexpand(True)
        body.append(action_bar)

        self._capture_button = Gtk.Button(label=self._("Capture"))
        self._capture_button.add_css_class("suggested-action")
        self._capture_button.add_css_class("pill")
        self._capture_button.connect("clicked", lambda _b: self._capture())
        action_bar.set_center_widget(self._capture_button)

        self.connect("close-request", self._on_close)

        self._update_rotate_visibility()
        if not self._devices:
            self._status.set_text(self._("No camera detected"))
            self._capture_button.set_sensitive(False)
            self._rotate_button.set_sensitive(False)
        else:
            GLib.idle_add(self._start_pipeline)

    # ------------------------------------------------------------------
    # Pipeline lifecycle
    # ------------------------------------------------------------------

    def _update_rotate_visibility(self) -> None:
        self._rotate_button.set_visible(len(self._devices) > 1)

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

        name = device.get("name") or self._("Camera")
        self._title.set_subtitle(name)
        self._status.set_text(self._("Ready"))
        self._capture_button.set_sensitive(self._appsink is not None)
        if self._appsink is None:
            self._status.set_text(self._("Capture not available (jpegenc/appsink missing)"))
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
        self._status.set_text(message)
        self._capture_button.set_sensitive(False)

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------

    def _switch_camera(self) -> None:
        if len(self._devices) <= 1:
            return
        self._device_index = (self._device_index + 1) % len(self._devices)
        self._status.set_text(self._("Switching camera…"))
        GLib.idle_add(self._start_pipeline)

    def _capture(self) -> None:
        if self._busy_capture or self._appsink is None:
            return
        self._busy_capture = True
        self._capture_button.set_sensitive(False)
        try:
            sample = self._appsink.get_property("last-sample")
            if sample is None:
                # Frame may not be available yet — appsink emits the first
                # sample only once a buffer has flowed through. Retry once
                # on the next idle tick before giving up.
                self._status.set_text(self._("Waiting for first frame…"))
                GLib.timeout_add(150, self._capture_retry)
                return
            self._write_sample(sample)
        finally:
            self._busy_capture = False
            self._capture_button.set_sensitive(True)

    def _capture_retry(self) -> bool:
        if self._appsink is None:
            return False
        sample = self._appsink.get_property("last-sample")
        if sample is None:
            self._status.set_text(self._("No frame available — try again"))
            self._busy_capture = False
            self._capture_button.set_sensitive(True)
            return False
        self._write_sample(sample)
        self._busy_capture = False
        self._capture_button.set_sensitive(True)
        return False

    def _write_sample(self, sample: Any) -> None:
        buf = sample.get_buffer() if sample is not None else None
        if buf is None:
            self._status.set_text(self._("No frame available — try again"))
            return
        success, mapinfo = buf.map(self._Gst.MapFlags.READ)
        if not success:
            self._status.set_text(self._("Could not read frame"))
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
            self._status.set_text(self._("Failed to save: %s") % exc)
            return

        self._status.set_text(self._("Saved %s") % path.name)
        if self._on_captured is not None:
            try:
                self._on_captured(path)
            except Exception:
                LOGGER.debug("on_captured callback failed", exc_info=True)

    def _on_close(self, _win: Any) -> bool:
        self._stop_pipeline()
        return False
