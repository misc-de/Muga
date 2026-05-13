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
from .camera_orientation import (
    OrientationClient,
    ORIENT_NORMAL,
    ORIENT_BOTTOM_UP,
    ORIENT_LEFT_UP,
    ORIENT_RIGHT_UP,
    is_landscape as orientation_is_landscape,
)

LOGGER = logging.getLogger(__name__)

# Set YAGA_CAMERA_DEBUG=1 in the environment to surface the camera's
# pipeline diagnostics on stderr. Off by default — the messages went
# to _dlog() during development and would spam logs in normal use.
import os as _os
_CAMERA_DEBUG = bool(_os.environ.get("YAGA_CAMERA_DEBUG"))


def _dlog(message: str) -> None:
    """Diagnostic log. Goes through LOGGER.info so it lands in journald
    via Adw's logging, and additionally mirrors to stderr when the env
    flag is on (useful when running yaga from a phone terminal where
    logger output may not be visible)."""
    LOGGER.info(message)
    if _CAMERA_DEBUG:
        print(message)


# ---- Layout constants ----------------------------------------------
# Pixel values used by _apply_layout_for / _position_* methods. Pulled
# out so tuning the chrome no longer means hunting through 4 different
# helper functions.
_BRACKET_INSET = 50          # corner-bracket inset from image edge
_OPTIONS_NOTCH_MARGIN = 40   # gap above the options bar (clear of notch)
_OPTIONS_BAR_SIDE = 40       # options-bar edge inset in landscape — matches
                             # the portrait notch margin so the icon row sits
                             # the same distance from the visible frame in
                             # both orientations.
_SHUTTER_SIDE_MARGIN = 24    # shutter inset from screen edge in portrait
                             # handed mode (corner placement).
_SHUTTER_LANDSCAPE_INSET = 90  # shutter "into-the-image" edge inset in
                               # landscape and in portrait NEUTRAL. 70 felt
                               # tight in landscape — only 20 px past the
                               # 50-px bracket inset — so this is bumped to
                               # match portrait's perceived breathing room.
_RECORD_DOT_INSET = 16       # record-dot inset from image rect
_SWIPE_HINT_INSET = 20       # swipe-hint inset from edge
_ICON_PIXEL_SIZE = 26
# Debounce window for settings.save() calls. Rapid slider/button taps
# coalesce into one disk write at the trailing edge; _on_close flushes
# pending writes synchronously.
_PERSIST_DELAY_MS = 500

# --- Halium / gst-droid quirks ------------------------------------
# Numbers carved out of the previously-scattered magic constants in
# pipeline-string builders. The actual caps strings still reference
# them inline (changing those strings means re-validating on a real
# Halium device), but at least the values now have names that explain
# why they are what they are.
#
# - Preview is capped to 720p @ 24fps on Halium because anything higher
#   piles memory in droidcamsrc's pool and OOMs phosh within seconds.
# - The image-capture path through `vfsrc` peaks at 2560 px regardless
#   of the requested cap — a HAL limit on this hardware. Full sensor
#   resolution is only reachable via the `imgsrc` pad in mode=1, which
#   has its own start-capture deadlock issues this build can't navigate.
_HALIUM_PREVIEW_CAP_W = 1280
_HALIUM_PREVIEW_CAP_H = 720
_HALIUM_PREVIEW_FPS = 24
_HALIUM_IMAGE_MAX_VIA_VFSRC = 2560

# Video-record JPEG quality mapped from the user's bitrate preset. We
# can't pass bitrate to jpegenc directly (it's a quality element, not
# a rate-controlled encoder), so we approximate the same perceptual
# ladder. The dict is keyed by the actual preset values exposed in the
# Quality popover; the chooser snaps inputs to one of these.
_VIDEO_BITRATE_TO_QUALITY: dict[int, int] = {
    2000: 70,
    4000: 85,
    8000: 92,
    16000: 98,
}


def _write_exif_app1_inplace(path: Path, exif_tiff: bytes) -> None:
    """Patch a JPEG's APP1 (EXIF) segment in place — no decode / re-
    encode of the pixel data. `exif_tiff` is the raw TIFF blob that
    PIL.Image.Exif().tobytes() returns (no marker, no length, no
    "Exif\\0\\0" prefix — we add those here).

    JPEG layout we care about::

        SOI            FF D8
        APPn segs      FF Em LL LL .. payload (LL = big-endian length
                                     including LL itself but not Em)
        ...
        SOS, data, EOI

    We rewrite the file as:
        SOI + new APP1 (EXIF) + every original segment EXCEPT the
        existing APP1 segments + the rest of the file from SOS onward.

    Atomic via tmp + os.replace so a crash mid-write doesn't leave a
    truncated photo on disk.
    """
    raw = path.read_bytes()
    if len(raw) < 4 or raw[0] != 0xFF or raw[1] != 0xD8:
        # Not a JPEG (or empty) — nothing to patch.
        return
    # Build the new APP1 (EXIF) segment.
    body = b"Exif\x00\x00" + exif_tiff
    if len(body) > 0xFFFD:
        # APP1 length field is uint16 and includes itself (2 bytes).
        # Anything bigger needs the Extended-EXIF spec we don't support.
        LOGGER.debug("EXIF payload too large (%d bytes); skipping", len(body))
        return
    import struct
    new_app1 = b"\xFF\xE1" + struct.pack(">H", len(body) + 2) + body
    # Walk the existing segments, skipping any existing APP1.
    out = bytearray(b"\xFF\xD8")
    out += new_app1
    i = 2
    while i + 1 < len(raw):
        if raw[i] != 0xFF:
            # Hit pixel data without seeing SOS — broken JPEG, bail and
            # write back original bytes (we already inserted APP1 at
            # the start, which is still a structurally valid file).
            out += raw[i:]
            break
        marker = raw[i + 1]
        if marker == 0xDA:  # SOS — everything from here is image data
            out += raw[i:]
            break
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7,
                      0xD8, 0xD9, 0x01):
            # Standalone markers (no length). RST*, SOI, EOI, TEM.
            # Append and advance 2.
            out += raw[i:i + 2]
            i += 2
            continue
        # Length-prefixed segment.
        if i + 4 > len(raw):
            break
        seg_len = (raw[i + 2] << 8) | raw[i + 3]
        seg_end = i + 2 + seg_len
        if seg_end > len(raw):
            break
        if marker == 0xE1:
            # Existing APP1 — could be EXIF or XMP. Skip EXIF, keep
            # XMP (which uses the "http://ns.adobe.com/xap/1.0/\0"
            # signature, distinguishable from "Exif\0\0").
            seg = raw[i + 4:seg_end]
            if seg.startswith(b"Exif\x00\x00"):
                i = seg_end
                continue
        out += raw[i:seg_end]
        i = seg_end
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(out)
            fh.flush()
            try:
                _os.fsync(fh.fileno())
            except OSError:
                pass
        _os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _is_halium_device(device: dict | None) -> bool:
    """True when the active source is gst-droid's droidcamsrc — the
    primary signal for the Halium/Hybris quirks path. Tolerant of None
    so callers can pass a possibly-unset current device."""
    if device is None:
        return False
    return device.get("source_factory") == "droidcamsrc"

# Maps the 4-state device orientation to the GSK rotation (in degrees)
# that puts a glyph/label upright in the user's view. Used by every
# rotatable widget — defined once here, referenced everywhere.
_ICON_ROTATION_DEG = {
    ORIENT_NORMAL:    0,
    ORIENT_BOTTOM_UP: 180,
    ORIENT_LEFT_UP:   270,
    ORIENT_RIGHT_UP:  90,
}


# Shared letterbox-math helper — now lives in camera_widgets next to
# ImageChrome (its primary consumer). Re-imported here so the existing
# _compute_image_rect call sites in CameraWindow keep working.
from .camera_widgets import compute_image_rect as _compute_image_rect


_CSS_PATH = Path(__file__).parent / "data" / "camera.css"

_corner_css_installed = False


def _ensure_css() -> None:
    global _corner_css_installed
    if _corner_css_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_path(str(_CSS_PATH))
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


# Device enumeration + caps helpers live in camera_devices. We import
# under the original underscore-prefixed names so the existing call
# sites in CameraWindow don't need updating.
from .camera_devices import (
    droidcamsrc_available as _droidcamsrc_available,
    droidcam_camera_count as _droidcam_camera_count,
    enumerate_droidcam_devices as _enumerate_droidcam_devices,
    is_ir_name as _is_ir_name,
    classify_location as _classify_location,
    device_props as _device_props,
    device_path as _device_path,
    is_pipewire_device as _is_pipewire_device,
    enumerate_devices as _enumerate_devices,
    modes_from_caps as _modes_from_caps,
    resolutions_from_caps as _resolutions_from_caps,
    device_kinds as _device_kinds,
)


# Custom drawing widgets live in camera_widgets. They were renamed to
# drop the leading underscore (now they're public to the camera_*
# module set); we alias back to the original names so the call sites
# in CameraWindow don't move.
from .camera_widgets import (
    ImageChrome as _ImageChrome,
    MirroredPicture,
    RotatableIcon as _RotatableIcon,
    RotatableLabel as _RotatableLabel,
    RotatableSwitch as _RotatableSwitch,
)


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
        handedness: str = "right",
        video_dir: Path | None = None,
        settings: Any = None,
    ) -> None:
        super().__init__()
        _ensure_css()
        self._ = translator or (lambda s: s)
        self.set_transient_for(parent)
        self.set_modal(False)
        self.set_decorated(False)
        self.set_default_size(820, 540)
        # Fullscreen the camera window. Phosh's top status bar otherwise
        # overlaps the window's top edge and eats clicks on the upper
        # icon row (the user can see the icons but presses go to the
        # system bar). Fullscreen also matches the typical phone-camera
        # experience and means the picture rect uses the entire screen.
        self.fullscreen()
        self.set_title(self._("Camera"))
        self.add_css_class("camera-root")

        self._save_dir = Path(save_dir)
        self._video_dir = Path(video_dir) if video_dir is not None else self._save_dir
        self._on_captured = on_captured
        self._handedness = (
            handedness if handedness in ("left", "right", "neutral") else "right"
        )
        # Seed orientation state up front: the timer button is created
        # in the options bar before the orientation backend starts, and
        # its _RotatableIcon needs to know the current rotation.
        self._device_orientation: str = ORIENT_NORMAL
        self._applied_layout: str | None = None
        self._layout_landscape: bool | None = None
        # List of widgets whose glyph/text needs to rotate with device
        # orientation. Initialised up front because the countdown label
        # and the options-bar icon buttons (all _Rotatable*) register
        # themselves into it as they're built.
        self._rotatable_icons: list = []
        # Settings object (yaga.config.Settings) for persisting camera
        # picks (quality, image size, bitrate) across sessions. None
        # means transient — settings won't be saved but defaults apply.
        self._settings = settings
        self._settings_persist_source: int | None = None
        # Photo quality (jpegenc quality, 0-100) and video bitrate (kbps).
        # Initial values come from persisted settings when available.
        if settings is not None:
            self._jpeg_quality: int = int(
                getattr(settings, "camera_jpeg_quality", 92)
            )
            self._video_bitrate_kbps: int = int(
                getattr(settings, "camera_video_bitrate_kbps", 4000)
            )
        else:
            self._jpeg_quality = 92
            self._video_bitrate_kbps = 4000
        self._Gst = _gst()
        self._pipeline: Any = None
        self._bus: Any = None
        self._appsink: Any = None
        # Halium-only: droidcamsrc's imgsrc pad. When present we route
        # the shutter through this instead of the (capped-resolution)
        # vfsrc+jpegenc path, so photos come out at the sensor's native
        # resolution as a HAL-encoded JPEG.
        self._imgsink: Any = None
        self._capture_signal_sink: Any = None
        # Caps-swap state: when we capture on the vfsrc+jpegenc fallback
        # with the Halium 720p cap in place, we temporarily lift the
        # cap to force droidcamsrc to renegotiate to native resolution,
        # then restore. Low-res frames still in flight from before the
        # renegotiation are filtered out via _capture_min_width. (Used
        # only when the image-mode pipeline path fails / is unavailable.)
        self._capture_saved_caps: Any = None
        self._capture_min_width: int = 0
        # Transient image-mode pipeline state. On Halium, the vfsrc pad
        # caps the resolution at ~2560 px even with the capsfilter
        # lifted; full sensor res (e.g. 3864x5152) only comes through
        # droidcamsrc's imgsrc pad in mode=1. We tear down the preview
        # pipeline, build this image-mode pipeline transiently, emit
        # start-capture, save the HAL JPEG, then restore preview. The
        # ~2-3 s during HAL mode-switch is bridged by a spinner overlay.
        self._image_pipeline: Any = None
        self._image_src: Any = None
        self._image_signal_id: int | None = None
        self._image_bus: Any = None
        self._image_timeout_id: int | None = None
        # Video-record transient pipeline state.
        self._video_pipeline: Any = None
        self._video_src: Any = None
        self._video_bus: Any = None
        self._video_path: Path | None = None
        self._video_finalize_source: int | None = None
        self._preview_appsink: Any = None
        self._preview_signal_id: int | None = None
        self._preview_paintable: Any = None
        self._preview_paintable_signal_id: int | None = None
        self._preview_frame_count = 0
        self._source_frame_count = 0
        self._sink_frame_count = 0
        self._source_probe_pad: Any = None
        self._source_probe_id: int | None = None
        self._sink_probe_pad: Any = None
        self._sink_probe_id: int | None = None
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
        # Halium image-capture target. The transient capture pipeline
        # always asks the HAL for native resolution (HAL's preferred
        # image-resolution is the only one we can reliably get out of
        # gst-droid). If this is non-None, we Pillow-downscale the JPEG
        # to fit inside (w, h) before saving — keeps aspect ratio.
        self._image_resolution: tuple[int, int] | None = None
        if settings is not None:
            stored = getattr(settings, "camera_image_resolution", None)
            if stored and len(stored) == 2:
                try:
                    self._image_resolution = (int(stored[0]), int(stored[1]))
                except Exception:
                    self._image_resolution = None
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

        # Brackets + rule-of-thirds grid in a single overlay that tracks
        # the actual letterboxed image rect (not the widget allocation).
        # On a tall phone window the camera image is letterboxed inside
        # the picture widget, so widget-anchored brackets land on the
        # black bars; this places them on the image instead.
        self._chrome = _ImageChrome(self._picture)
        overlay.add_overlay(self._chrome)

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
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
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
        self._countdown = _RotatableLabel()
        self._countdown.set_label("")
        self._countdown.add_css_class("camera-countdown")
        self._countdown.set_halign(Gtk.Align.CENTER)
        self._countdown.set_valign(Gtk.Align.CENTER)
        self._countdown.set_visible(False)
        self._countdown.set_can_target(False)
        overlay.add_overlay(self._countdown)
        # Register so _apply_layout_for rotates the countdown number
        # along with the other icon glyphs.
        self._register_rotatable(self._countdown)

        # Capturing spinner — shown over the (frozen) preview while the
        # transient image-mode pipeline reconfigures the HAL to capture
        # at native sensor resolution. Vertical stack: spinner + label.
        self._capture_spinner_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
        )
        self._capture_spinner_box.add_css_class("camera-capture-overlay")
        self._capture_spinner_box.set_halign(Gtk.Align.CENTER)
        self._capture_spinner_box.set_valign(Gtk.Align.CENTER)
        self._capture_spinner_box.set_visible(False)
        self._capture_spinner_box.set_can_target(False)
        self._capture_spinner = Gtk.Spinner()
        self._capture_spinner.set_size_request(72, 72)
        self._capture_spinner.set_halign(Gtk.Align.CENTER)
        self._capture_spinner_box.append(self._capture_spinner)
        capture_label = _RotatableLabel()
        capture_label.set_label(self._("Capturing…"))
        capture_label.add_css_class("title-3")
        self._capture_spinner_box.append(capture_label)
        # Register the label so the orientation tick rotates it upright.
        self._register_rotatable(capture_label)
        overlay.add_overlay(self._capture_spinner_box)

        # Recording indicator: red dot that blinks in the user's
        # top-right corner whenever a video recording is in progress.
        # Position adjusts per orientation via _apply_layout_for.
        self._record_dot = Gtk.Box()
        self._record_dot.add_css_class("camera-record-dot")
        self._record_dot.set_visible(False)
        self._record_dot.set_can_target(False)
        self._record_dot.set_halign(Gtk.Align.END)
        self._record_dot.set_valign(Gtk.Align.START)
        self._record_dot.set_margin_top(28)
        self._record_dot.set_margin_end(28)
        overlay.add_overlay(self._record_dot)
        self._record_dot_blink_id: int | None = None

        # Swipe-hint: a small "<- swipe ->" pill that pulses twice when
        # the camera opens, teaching the user that horizontal swipe on
        # the shutter switches photo <-> video. Hides for good after
        # the two pulse cycles.
        self._swipe_hint = _RotatableLabel()
        self._swipe_hint.set_label(self._("←  swipe  →"))
        self._swipe_hint.add_css_class("camera-swipe-hint")
        # Initial alignment; overwritten by _position_swipe_hint as
        # soon as the layout runs for the actual orientation.
        self._swipe_hint.set_halign(Gtk.Align.CENTER)
        self._swipe_hint.set_valign(Gtk.Align.END)
        self._swipe_hint.set_visible(False)
        self._swipe_hint.set_can_target(False)
        self._swipe_hint.set_opacity(0.0)
        overlay.add_overlay(self._swipe_hint)
        self._register_rotatable(self._swipe_hint)
        self._swipe_hint_cycles_left: int = 2
        self._swipe_hint_phase: float = 0.0
        self._swipe_hint_direction: int = 1
        self._swipe_hint_pulse_id: int | None = None

        # Escape closes the window. There's no on-screen close button —
        # on phones the system swipe-from-bottom is used; on desktops the
        # Escape key. (window-close shortcut intentionally omitted from
        # the overlay so it doesn't compete with viewfinder real estate.)
        esc = Gtk.ShortcutController()
        esc.set_scope(Gtk.ShortcutScope.LOCAL)
        esc.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("Escape"),
                Gtk.CallbackAction.new(lambda *_a: (self.close() or True)),
            )
        )
        self.add_controller(esc)

        # Mode-options bar (grid toggle, self-timer, resolution picker,
        # camera gear, geotag). Orientation and anchoring are managed by
        # _apply_layout_for so the bar always sits outside the
        # camera image rect, never on top of it:
        #   portrait  -> horizontal row centred above the picture (fits
        #                in the top letterbox even when it's only ~80 px)
        #   landscape -> vertical column on the side opposite the
        #                shutter (so the user's thumb doesn't shadow it).
        self._options_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
        )
        self._options_bar.set_halign(Gtk.Align.CENTER)
        self._options_bar.set_valign(Gtk.Align.START)
        self._options_bar.set_margin_top(16)
        overlay.add_overlay(self._options_bar)
        top_right = self._options_bar  # alias for the appends below

        # Each icon button gets a _RotatableIcon as its child so the
        # glyph can rotate with the device orientation. The list lets us
        # walk all of them in _apply_layout_for. The Gtk.Label-based
        # buttons (timer, resolution) are intentionally not rotated;
        # rotating Pango text inside a narrow pill clips badly.
        # (self._rotatable_icons was initialised earlier so the
        # countdown label can register before this point.)

        def _icon(name: str) -> _RotatableIcon:
            img = _RotatableIcon()
            img.set_from_icon_name(name)
            # Explicit pixel_size so MenuButton's internal layout (which
            # ignored our CSS -gtk-icon-size in some Adwaita versions)
            # renders at the same size as Button/ToggleButton-hosted
            # icons. Matches the .camera-iconbtn image CSS rule.
            img.set_pixel_size(26)
            self._register_rotatable(img)
            return img

        self._grid_button = Gtk.ToggleButton()
        self._grid_button.set_child(_icon("view-grid-symbolic"))
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
        self._gear_button.set_child(_icon("emblem-system-symbolic"))
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
        # Geotag toggle lives inside the Camera Settings popover as a
        # second-level boolean (Gtk.Switch). The flag below is the
        # user-intent source-of-truth; self._geo is the actual
        # GeoClient handle, which is None when GeoClue is unavailable
        # (silent fail).
        self._geo_enabled: bool = bool(
            getattr(settings, "camera_geo_enabled", False)
        ) if settings is not None else False
        # Auto-attach the GeoClient at startup if the user previously
        # enabled it. Silent if GeoClue isn't on the system.
        if self._geo_enabled:
            self._try_start_geo_silent()

        # Quality picker — popover with photo (jpeg quality) and video
        # (bitrate) presets. Photo quality updates live on the running
        # jpegenc element; video applies once the recording branch is
        # wired up.
        self._quality_button = Gtk.MenuButton()
        self._quality_button.set_child(_icon("applications-graphics-symbolic"))
        self._quality_button.add_css_class("camera-iconbtn")
        self._quality_button.set_tooltip_text(self._("Quality"))
        self._photo_quality_buttons: list[tuple[Gtk.Button, int]] = []
        self._video_quality_buttons: list[tuple[Gtk.Button, int]] = []
        self._image_size_buttons: list[
            tuple[Gtk.Button, tuple[int, int] | None]
        ] = []
        # Capture mode: "photo" or "video". Drives which sections the
        # Quality popover shows. Until a mode-toggle UI lands (when
        # video recording is wired up), this stays at "photo".
        self._capture_mode: str = "photo"
        self._quality_button.set_popover(self._build_quality_popover())
        top_right.append(self._quality_button)

        # Settings popover — quick-access toggles that aren't really
        # "quality" or "camera-control". Currently: handedness
        # (right / left / neutral). Built fresh on every orientation
        # change so the layout transposes like the quality popover.
        self._settings_button = Gtk.MenuButton()
        self._settings_button.set_child(_icon("preferences-system-symbolic"))
        self._settings_button.add_css_class("camera-iconbtn")
        self._settings_button.set_tooltip_text(self._("Settings"))
        self._handedness_buttons: list[tuple[Gtk.Button, str]] = []
        self._settings_button.set_popover(self._build_settings_popover())
        top_right.append(self._settings_button)

        # Camera-switch lives in the same options row as the other
        # icons — only present when more than one capture device exists.
        self._rotate_button: Gtk.Button | None = None
        if len(self._devices) > 1:
            self._rotate_button = Gtk.Button()
            self._rotate_button.set_child(_icon("camera-switch-symbolic"))
            self._rotate_button.add_css_class("camera-iconbtn")
            self._rotate_button.set_tooltip_text(self._("Switch camera"))
            self._rotate_button.connect("clicked", lambda _b: self._switch_camera())
            top_right.append(self._rotate_button)

        # Single capture button. Positioned on the handedness side and
        # repositioned by _on_orientation_tick: lower-third in portrait,
        # vertically centred in landscape.
        primary_align = (
            Gtk.Align.START if self._handedness == "left" else Gtk.Align.END
        )
        # Box (not Button) so our own GestureClick has clean access to
        # press/release without competing with Gtk.Button's internal
        # click handling. The .shutter-button CSS class still gives it
        # the white ring + icon styling.
        self._shutter = Gtk.Box()
        self._shutter.add_css_class("shutter-button")
        self._shutter.set_halign(primary_align)
        self._shutter.set_size_request(76, 76)
        self._shutter_icon = _RotatableIcon()
        self._shutter_icon.set_from_icon_name("camera-photo-symbolic")
        self._shutter_icon.set_halign(Gtk.Align.CENTER)
        self._shutter_icon.set_valign(Gtk.Align.CENTER)
        self._shutter_icon.set_hexpand(True)
        self._shutter_icon.set_vexpand(True)
        self._shutter.append(self._shutter_icon)
        self._register_rotatable(self._shutter_icon)
        self._recording: bool = False
        if self._handedness == "left":
            self._shutter.set_margin_start(24)
        else:
            self._shutter.set_margin_end(24)
        self._shutter.set_tooltip_text(self._("Capture"))
        # Click + horizontal-swipe via a single GestureClick that tracks
        # press/release positions. A tap (small dx) fires the shutter
        # action; a horizontal drag past the swipe threshold flips
        # photo ↔ video mode.
        self._shutter_press_x: float | None = None
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", self._on_shutter_pressed)
        click.connect("released", self._on_shutter_released)
        self._shutter.add_controller(click)
        overlay.add_overlay(self._shutter)
        # Initial valign — overwritten as soon as the sensor (or, on
        # desktops without an accelerometer, the tick fallback) reports a
        # real orientation.
        self._shutter.set_valign(Gtk.Align.CENTER)
        # Prefer the device accelerometer over window dimensions. On
        # phones with Phosh the surface size doesn't change on screen
        # rotation — the compositor rotates the buffer instead — so
        # polling get_width/get_height never sees the transition. The
        # sensor signals it cleanly. If the sensor isn't available the
        # tick callback fills in.
        # Pre-apply the portrait-normal layout BEFORE the window is
        # shown, so the shutter/options-bar don't snap into place on
        # the first sensor callback (which fires asynchronously a few
        # hundred ms after window-show).
        self._apply_layout_for(ORIENT_NORMAL)

        self._orientation = OrientationClient()
        if not self._orientation.start(self._on_orientation_changed):
            self._orientation = None
            self.add_tick_callback(self._on_orientation_tick)

        # Toast for status / errors.
        self._toast = Gtk.Label(label="")
        self._toast.add_css_class("camera-toast")
        self._toast.set_halign(Gtk.Align.CENTER)
        self._toast.set_valign(Gtk.Align.END)
        self._toast.set_margin_bottom(28)
        self._toast.set_visible(False)
        overlay.add_overlay(self._toast)

        # Viewfinder gestures attach to the PICTURE (not the window).
        # On Phosh, window-level gestures coordinate touch sequences
        # before they reach overlay children, so a press on an icon
        # button could get claimed by GestureZoom/GestureDrag and the
        # button's "clicked" never fires. Attaching to the picture means
        # these gestures only see events whose target is in the picture
        # subtree — i.e. taps and pinches on the actual image, not the
        # icons that sit above it.

        # Pinch-to-zoom on the preview.
        zoom_gesture = Gtk.GestureZoom()
        zoom_gesture.connect("begin", self._on_zoom_begin)
        zoom_gesture.connect("scale-changed", self._on_zoom_changed)
        self._picture.add_controller(zoom_gesture)

        # Tap-to-focus — attached to the picture in TARGET phase so it
        # only fires when the picture itself is the actual click target.
        # Default BUBBLE would also fire for clicks consumed by overlay
        # buttons (button is the target, gesture bubbles up through the
        # picture's ancestor chain), making the icons feel non-responsive
        # because the focus pulse paints on top of where the user just
        # pressed.
        click = Gtk.GestureClick()
        click.set_button(1)
        click.set_propagation_phase(Gtk.PropagationPhase.TARGET)
        click.connect("released", self._on_tap_to_focus)
        self._picture.add_controller(click)

        # ESC / Space / Return shortcuts — keys are global; stays on window.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        # Scroll-to-zoom for desktops without touch — only meaningful
        # when the pointer is over the picture, so it also moves there.
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self._picture.add_controller(scroll)

        self.connect("close-request", self._on_close)
        # Trigger the swipe-hint pulse once the window is actually
        # mapped (visible on screen). Doing this before "map" would
        # animate against an off-screen widget.
        self.connect("map", lambda _w: self._start_swipe_hint())
        # Energy gating: when the camera window isn't visible (user
        # switched apps, screen blanked, etc.), pause the GStreamer
        # pipeline and stop the orientation + GeoClue subscriptions.
        # Each is significant battery cost on a phone — 30 Hz video
        # buffer pool churn especially. Resume on map.
        self.connect("map", self._on_window_map)
        self.connect("unmap", self._on_window_unmap)

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

        on_halium = _is_halium_device(device)
        preview_queue = "queue leaky=downstream max-size-buffers=2"
        # sync=false on the preview sink: with sync=true (the default), the
        # sink compares buffer timestamps to the pipeline clock and drops
        # anything it considers "late", emitting a "buffers are being
        # dropped / computer too slow" warning. On phones that's a noisy,
        # CPU-wasting loop. There's no audio to sync to here — we want the
        # newest frame, not "the frame the clock says is due now".
        if has_gtk_sink:
            preview_branch = (
                f"t. ! {preview_queue} ! videoconvert "
                "   ! gtk4paintablesink name=preview sync=false"
            )
        elif has_appsink:
            preview_branch = (
                f"t. ! {preview_queue} "
                "   ! videoconvert "
                "   ! appsink name=preview_sink emit-signals=true "
                "             caps=video/x-raw,format=RGBA "
                "             max-buffers=2 drop=true sync=false"
            )
        else:
            preview_branch = (
                f"t. ! {preview_queue} ! fakesink sync=false"
            )
        # The snap branch is gated by a valve so jpegenc only runs when the
        # user actually presses the shutter. Running jpegenc on every frame
        # at 30 fps on a Halium phone causes memory pile-up and OOM crash.
        # On capture: open valve, wait for new-sample signal, close valve.
        #
        # `async=false` on the snap appsink is critical: by default, an
        # async sink blocks the pipeline's READY→PAUSED→PLAYING transition
        # until it gets a preroll buffer. With the valve closed (drop=true)
        # at startup, no buffer ever reaches the snap appsink, so without
        # async=false the whole pipeline stays stuck at "pending PLAYING".
        # The preview sink prerolls and drives playback by itself.
        #
        # On Halium the valve sits BEFORE the queue — with valve drop=true
        # (the default state), no buffers flow, and the queue stays empty.
        # If the queue were upstream of the valve it would always hold the
        # last 2 source-pool buffers even while idle, just adding pressure
        # on droidcamsrc's pool.
        if has_jpeg and has_appsink:
            # Name the jpegenc element so _set_jpeg_quality can live-
            # update its quality property without rebuilding the pipeline.
            q = max(0, min(100, self._jpeg_quality))
            if on_halium:
                snapshot_branch = (
                    "t. ! valve name=shutter drop=true "
                    "   ! queue leaky=downstream max-size-buffers=2 "
                    f"   ! videoconvert ! jpegenc name=snap_jpeg quality={q} "
                    "   ! appsink name=snap emit-signals=true "
                    "             max-buffers=1 drop=true sync=false async=false"
                )
            else:
                snapshot_branch = (
                    "t. ! queue leaky=downstream max-size-buffers=2 "
                    "   ! valve name=shutter drop=true "
                    f"   ! videoconvert ! jpegenc name=snap_jpeg quality={q} "
                    "   ! appsink name=snap emit-signals=true "
                    "             max-buffers=1 drop=true sync=false async=false"
                )
        else:
            snapshot_branch = ""

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
        """Pick a Gst source element for `device`, falling back through:
          (a) droidcamsrc for Halium/Hybris phones tagged in the device
              dict (`source_factory == "droidcamsrc"`).
          (b) Gst.Device.create_element() — picks pipewiresrc for
              PipeWire-managed cameras, v4l2src for raw v4l2 nodes.
              Critical when PipeWire holds an exclusive lock on the
              /dev/videoN node (direct v4l2src would fail with ENOTTY).
          (c) Manual v4l2src device=… — for the /dev backup-scan path
              or when create_element returns nothing.
          (d) autovideosrc as a last resort.

        Each path lives in its own helper so failures are scoped and
        the dispatcher reads as a straight fallback ladder."""
        if _is_halium_device(device):
            src = self._make_droidcam_source(device)
            if src is not None:
                return src
        src = self._make_gst_device_source(device)
        if src is not None:
            return src
        src = self._make_v4l2_source(device)
        if src is not None:
            return src
        return self._make_autovideo_source()

    def _make_droidcam_source(self, device: dict[str, Any]) -> Any:
        gst = self._Gst
        src = gst.ElementFactory.make("droidcamsrc", "src")
        if src is None:
            return None
        try:
            src.set_property("camera-device", device.get("droidcam_id", 0))
        except Exception:
            LOGGER.debug("droidcamsrc camera-device set failed", exc_info=True)
        # mode=2 (video) keeps the viewfinder rolling continuously
        # without the per-frame Photography reconfiguration that
        # mode=1 (image) does — on the user's FuriOS device that
        # extra reconfigure causes the visible stream of
        # "setting focus-mode 6 / flash-mode 0 not supported"
        # warnings, and after the first frame the preview stops
        # updating. We don't need imgsrc anyway because the snap
        # appsink branch encodes JPEGs from the viewfinder feed.
        try:
            src.set_property("mode", 2)  # 2 = video
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
        return src

    def _make_gst_device_source(self, device: dict[str, Any]) -> Any:
        gst_device = device.get("gst_device")
        if gst_device is None:
            return None
        try:
            return gst_device.create_element("src")
        except Exception:
            LOGGER.debug("Gst.Device.create_element failed", exc_info=True)
            return None

    def _make_v4l2_source(self, device: dict[str, Any]) -> Any:
        gst = self._Gst
        path = device.get("path") or ""
        if not path or gst.ElementFactory.find("v4l2src") is None:
            return None
        src = gst.ElementFactory.make("v4l2src", "src")
        if src is not None:
            src.set_property("device", path)
        return src

    def _make_autovideo_source(self) -> Any:
        gst = self._Gst
        if gst.ElementFactory.find("autovideosrc") is None:
            return None
        return gst.ElementFactory.make("autovideosrc", "src")

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
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
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
        #
        # On Halium without a user pick, cap the source to a 720p/24fps
        # ceiling. droidcamsrc otherwise negotiates the HAL's max (often
        # 1080p@30) and the resulting GPU/memory bandwidth — videoconvert,
        # tee fan-out, gtk4paintablesink GL upload — competes with phosh
        # for the same GPU and the compositor crashes under sustained
        # load. 720p@24 is well below most HAL modes so droidcamsrc still
        # has a valid mode to pick.
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
        elif _is_halium_device(device):
            # Width/height only — no framerate constraint. Many Halium
            # HALs only advertise discrete framerates (commonly just 30/1)
            # and a [1, 24] range gives them no valid value, which
            # silently stalls negotiation at READY->PAUSED.
            capsfilter = gst.ElementFactory.make("capsfilter", "halium_default_cap")
            caps_str = (
                "video/x-raw,"
                "width=(int)[1,1280],"
                "height=(int)[1,720]"
            )
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
        self._imgsink = None
        self._preview_frame_count = 0
        self._source_frame_count = 0
        self._sink_frame_count = 0
        self._zoom = 1.0
        self._picture.set_zoom(1.0)

        # Note: we explicitly do NOT hook the in-pipeline imgsrc branch
        # on this preview pipeline. The main pipeline runs droidcamsrc
        # in mode=2 (video) to avoid mode=1's per-frame Photography
        # reconfigure stall — but gst-droid's `start-capture` action
        # signal is mode-dependent: in mode=2 it starts video recording
        # ("cannot record video in raw mode"), only in mode=1 does it
        # take a picture. Halium captures therefore go through the
        # transient mode=1 pipeline in _capture_via_image_pipeline.
        if _is_halium_device(device):
            try:
                templates = [
                    t.name_template
                    for t in (source.get_pad_template_list() or [])
                ]
            except Exception:
                templates = []
            _dlog(f"[yaga.camera] droidcamsrc pad templates: {templates}")

        # Diagnostic buffer probes. Tells us — without enabling GST_DEBUG —
        # whether droidcamsrc is producing a continuous stream and whether
        # those buffers reach the sink. Crucial for "one frame then freeze"
        # debugging: if source >> sink, the stall is downstream; if both
        # stop at 1, the source itself stops producing.
        if src_pad is not None:
            self._source_probe_pad = src_pad
            self._source_probe_id = src_pad.add_probe(
                gst.PadProbeType.BUFFER, self._on_source_buffer,
            )

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
                    # The paintable's intrinsic size becomes known when the
                    # first frame arrives and changes when the source
                    # renegotiates caps; the chrome needs to redraw at
                    # those points so the brackets snap onto the new image
                    # rect instead of staying around the previous one.
                    # Track the (paintable, signal-id) pair so the next
                    # _stop_pipeline can disconnect — otherwise the
                    # closure-bound `self` keeps the window alive as long
                    # as the paintable does, and a video-mode swap leaks
                    # the previous paintable's handler.
                    try:
                        self._preview_paintable = paintable
                        self._preview_paintable_signal_id = paintable.connect(
                            "invalidate-size",
                            lambda _p: self._chrome.queue_draw(),
                        )
                    except Exception:
                        LOGGER.debug("invalidate-size hookup failed", exc_info=True)
            except Exception:
                LOGGER.debug("Could not bind preview paintable", exc_info=True)
        else:
            preview_app = pipeline.get_by_name("preview_sink")
            if preview_app is not None:
                self._preview_appsink = preview_app
                self._preview_signal_id = preview_app.connect(
                    "new-sample", self._on_preview_sample
                )

        # Probe the preview sink's input so we can compare source-side vs
        # sink-side buffer counts in the logs.
        preview_sink_element = sink or pipeline.get_by_name("preview_sink")
        if preview_sink_element is not None:
            sink_pad = preview_sink_element.get_static_pad("sink")
            if sink_pad is not None:
                self._sink_probe_pad = sink_pad
                self._sink_probe_id = sink_pad.add_probe(
                    gst.PadProbeType.BUFFER, self._on_sink_buffer,
                )

        self._picture.set_mirrored(device.get("location") == "front")

        result = pipeline.set_state(gst.State.PLAYING)
        if result == gst.StateChangeReturn.FAILURE:
            self._fail(self._("Could not start camera"))
            return False

        preview_path = (
            "gtk4paintablesink" if pipeline.get_by_name("preview") is not None
            else "appsink" if pipeline.get_by_name("preview_sink") is not None
            else "fakesink"
        )
        src_factory = source.get_factory().get_name() if source.get_factory() else "?"
        result_nick = {
            gst.StateChangeReturn.SUCCESS: "SUCCESS",
            gst.StateChangeReturn.ASYNC: "ASYNC",
            gst.StateChangeReturn.NO_PREROLL: "NO_PREROLL",
            gst.StateChangeReturn.FAILURE: "FAILURE",
        }.get(result, str(result))
        _dlog(f"[yaga.camera] pipeline PLAYING source={src_factory} "
            f"preview={preview_path} set_state={result_nick}")

        self._shutter.set_sensitive(self._appsink is not None)
        if self._appsink is None:
            self._show_toast(self._("Capture unavailable"))
        self._populate_resolutions(device)
        return False

    def _stop_pipeline(self) -> None:
        # Tear down any in-flight capture state before disposing the pipeline.
        self._close_valve_and_disconnect()
        self._valve = None
        # Clearing the busy flag here is the only place that catches
        # the "user hit camera-switch (or something else that triggers
        # _stop_pipeline) mid-capture" path — otherwise _busy_capture
        # stays True forever and the shutter is locked.
        self._busy_capture = False
        if self._preview_appsink is not None and self._preview_signal_id is not None:
            try:
                self._preview_appsink.disconnect(self._preview_signal_id)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
        self._preview_signal_id = None
        self._preview_appsink = None
        if (
            self._preview_paintable is not None
            and self._preview_paintable_signal_id is not None
        ):
            try:
                self._preview_paintable.disconnect(
                    self._preview_paintable_signal_id
                )
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
        self._preview_paintable_signal_id = None
        self._preview_paintable = None
        if self._source_probe_pad is not None and self._source_probe_id is not None:
            try:
                self._source_probe_pad.remove_probe(self._source_probe_id)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
        self._source_probe_pad = None
        self._source_probe_id = None
        if self._sink_probe_pad is not None and self._sink_probe_id is not None:
            try:
                self._sink_probe_pad.remove_probe(self._sink_probe_id)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
        self._sink_probe_pad = None
        self._sink_probe_id = None
        if self._bus is not None:
            try:
                self._bus.remove_signal_watch()
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
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
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._pipeline = None
        self._appsink = None
        self._imgsink = None
        self._capture_signal_sink = None
        self._capsfilter = None

    def _on_source_buffer(self, _pad: Any, _info: Any) -> Any:
        # First-frames diagnostic so logs show whether the source is
        # actually producing. Silent after the first few; the upstream
        # preview path is the source of truth for "is it running?".
        gst = self._Gst
        self._source_frame_count += 1
        if self._source_frame_count <= 3:
            _dlog(f"[yaga.camera] source buffer #{self._source_frame_count}")
        return gst.PadProbeReturn.OK

    def _on_sink_buffer(self, _pad: Any, _info: Any) -> Any:
        # Same idea as the source probe, on the preview sink's input.
        gst = self._Gst
        self._sink_frame_count += 1
        if self._sink_frame_count <= 3:
            _dlog(f"[yaga.camera] sink buffer #{self._sink_frame_count} "
                f"(source so far: {self._source_frame_count})")
        return gst.PadProbeReturn.OK

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
        # First-frame diagnostic — print to stderr so users on phones can
        # confirm frames are reaching the Python side at all (LOGGER.info
        # wouldn't show without console-handler configuration).
        if self._preview_frame_count == 0:
            _dlog(f"[yaga.camera] first preview frame {w}x{h} "
                f"format={s.get_string('format') or '?'}")
        self._preview_frame_count += 1
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
        t = message.type
        if t == gst.MessageType.ERROR:
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
        elif t == gst.MessageType.WARNING:
            err, dbg = message.parse_warning()
            _dlog(f"[yaga.camera] bus WARNING from {message.src.get_name() if message.src else '?'}: "
                f"{err.message if err else '?'} | {(dbg or '').strip()}")
        elif t == gst.MessageType.EOS:
            _dlog(f"[yaga.camera] bus EOS from {message.src.get_name() if message.src else '?'}")
        elif t == gst.MessageType.STATE_CHANGED:
            if message.src is self._pipeline:
                old, new, pending = message.parse_state_changed()
                if new == gst.State.PLAYING or pending != gst.State.VOID_PENDING:
                    _dlog(f"[yaga.camera] pipeline state {old.value_nick} -> "
                        f"{new.value_nick} (pending {pending.value_nick})")

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
        # On Halium / droidcamsrc, the image-size presets live inside
        # the Quality popover ("Photo size" section), so we hide the
        # standalone resolution chip here.
        if _is_halium_device(device):
            self._res_button.set_visible(False)
            return
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
        # Video mode: shutter toggles recording. Self-timer doesn't apply.
        if self._capture_mode == "video":
            if self._recording:
                self._stop_video_recording()
            else:
                self._start_video_recording()
            return
        delay = self._timer_choices[self._timer_idx]
        if delay <= 0:
            self._capture()
        else:
            self._start_countdown(delay)

    def _on_shutter_pressed(
        self, _gesture: Gtk.GestureClick, _n: int, x: float, _y: float,
    ) -> None:
        self._shutter_press_x = x

    def _on_shutter_released(
        self, _gesture: Gtk.GestureClick, _n: int, x: float, _y: float,
    ) -> None:
        if self._shutter_press_x is None:
            return
        dx = x - self._shutter_press_x
        self._shutter_press_x = None
        # |dx| > 40 px treats the gesture as a horizontal swipe that
        # toggles photo ↔ video mode. Anything smaller fires the
        # normal shutter action.
        if abs(dx) > 40 and not self._recording:
            new_mode = "video" if self._capture_mode == "photo" else "photo"
            self._set_capture_mode(new_mode)
            return
        self._on_shutter()

    def _update_shutter_icon(self) -> None:
        if self._recording:
            # Stop-glyph indicator while a recording is in progress.
            self._shutter_icon.set_from_icon_name("media-playback-stop-symbolic")
            self._shutter.add_css_class("recording")
        elif self._capture_mode == "video":
            self._shutter_icon.set_from_icon_name("camera-video-symbolic")
            self._shutter.remove_css_class("recording")
        else:
            self._shutter_icon.set_from_icon_name("camera-photo-symbolic")
            self._shutter.remove_css_class("recording")

    def _on_orientation_changed(self, orientation: str) -> None:
        # Sensor callback. Stores the full 4-state value (used by the
        # EXIF writer to tag captured photos with the right rotation)
        # and reapplies the shutter / options-bar layout.
        self._device_orientation = orientation
        self._apply_layout_for(orientation)

    def _register_rotatable(self, w: Any) -> None:
        """Register a widget for orientation-driven rotation. Prunes
        orphaned (no-parent) entries first — popover rebuilds add fresh
        _RotatableLabels every cycle, and the old ones must drop out as
        new ones go in or the list grows unbounded. Replaces both the
        ad-hoc filter blocks in _refresh_timer_button and the prune
        step at the top of _apply_layout_for."""
        self._rotatable_icons = [
            x for x in self._rotatable_icons if x.get_parent() is not None
        ]
        self._rotatable_icons.append(w)

    def _apply_layout_for(self, orientation: str) -> None:
        # Position the shutter + options bar per the user's spec.
        # The two flipped lays (`bottom-up`, `right-up`) are the
        # 180-deg-rotated mirrors of `normal` / `left-up` respectively,
        # so when those fire the shutter "wanders" to the corner that
        # ends up under the user's preferred-hand thumb after the flip.
        if orientation == self._applied_layout:
            return
        self._applied_layout = orientation
        self._layout_landscape = orientation_is_landscape(orientation)

        # Rotate every icon glyph so it stays upright relative to the
        # user's view. Empirically the GSK rotate() direction lands on
        # the opposite sign from what I'd guess, so the 90/270 values
        # are swapped compared to the standard "compensate for device
        # orientation" cookbook.
        icon_rotation = _ICON_ROTATION_DEG.get(orientation, 0)
        # The Quality popover lays out its sections + button rows
        # differently in portrait vs. landscape (transposed orientation
        # + rotated text labels), so rebuild it whenever orientation
        # flips. _build_quality_popover reads self._layout_landscape /
        # self._device_orientation directly.
        try:
            self._quality_button.set_popover(self._build_quality_popover())
        except Exception:
            LOGGER.debug("quality popover rebuild failed", exc_info=True)
        try:
            self._settings_button.set_popover(self._build_settings_popover())
        except Exception:
            LOGGER.debug("settings popover rebuild failed", exc_info=True)
        # Drop widgets that have been orphaned. Must run AFTER the
        # popover rebuilds because set_popover() is what orphans the
        # _RotatableLabels from the old popover — pruning before
        # set_popover would leave the just-replaced labels in the list
        # until the next layout cycle.
        self._rotatable_icons = [
            w for w in self._rotatable_icons if w.get_parent() is not None
        ]
        for img in self._rotatable_icons:
            img.set_rotation_deg(icon_rotation)
        # Recording dot follows the orientation too.
        try:
            self._position_record_dot()
        except Exception:
            LOGGER.debug("record-dot reposition failed", exc_info=True)
        try:
            self._position_swipe_hint()
        except Exception:
            LOGGER.debug("swipe-hint reposition failed", exc_info=True)

        neutral = (self._handedness == "neutral")
        right = (self._handedness == "right")

        # Always reset every margin first so we can express each case
        # purely in terms of the margins it needs (avoids stale values
        # from the previous orientation leaking through).
        for w in (self._shutter, self._options_bar):
            w.set_margin_top(0)
            w.set_margin_bottom(0)
            w.set_margin_start(0)
            w.set_margin_end(0)

        h = max(0, self.get_height())
        third = max(48, h // 6)            # lower/upper third offset
        user_vertical = max(120, self.get_width() // 3)

        kw = dict(neutral=neutral, right=right, third=third,
                  user_vertical=user_vertical)
        if orientation == ORIENT_NORMAL:
            self._layout_portrait(flip_180=False, **kw)
        elif orientation == ORIENT_BOTTOM_UP:
            self._layout_portrait(flip_180=True, **kw)
        elif orientation == ORIENT_LEFT_UP:
            self._layout_landscape(is_right_up=False, **kw)
        elif orientation == ORIENT_RIGHT_UP:
            self._layout_landscape(is_right_up=True, **kw)

    # ------------------------------------------------------------------
    # Layout helpers
    #
    # The four orientations come in two mirror-pairs:
    #   - Portrait NORMAL <-> BOTTOM_UP  (180° rotation)
    #   - Landscape LEFT_UP <-> RIGHT_UP (180° rotation)
    # Each pair shares its placement math; only the axis flips differ.
    # The pair-helpers below express their canonical case explicitly
    # and apply a mechanical flip when the mirror flag is set, so a
    # tweak to e.g. landscape-left-up can't drift out of sync with
    # landscape-right-up.
    # ------------------------------------------------------------------

    @staticmethod
    def _flip_align(a: Gtk.Align) -> Gtk.Align:
        if a == Gtk.Align.START:
            return Gtk.Align.END
        if a == Gtk.Align.END:
            return Gtk.Align.START
        return a

    def _apply_placement(
        self,
        widget: Any,
        *,
        halign: Gtk.Align,
        valign: Gtk.Align,
        m_top: int = 0,
        m_bottom: int = 0,
        m_start: int = 0,
        m_end: int = 0,
        flip: bool = False,
    ) -> None:
        """Set halign/valign and the four margins on `widget`. When
        flip=True, mirrors both axes (start<->end on aligns,
        top<->bottom and start<->end on margins). Margins are reset
        upstream in _apply_layout_for, so non-set values stay at 0."""
        if flip:
            halign = self._flip_align(halign)
            valign = self._flip_align(valign)
            m_top, m_bottom = m_bottom, m_top
            m_start, m_end = m_end, m_start
        widget.set_halign(halign)
        widget.set_valign(valign)
        widget.set_margin_top(m_top)
        widget.set_margin_bottom(m_bottom)
        widget.set_margin_start(m_start)
        widget.set_margin_end(m_end)

    def _layout_portrait(
        self,
        *,
        flip_180: bool,
        neutral: bool,
        right: bool,
        third: int,
        user_vertical: int,
    ) -> None:
        # Canonical case (flip_180=False) is ORIENT_NORMAL: phone
        # upright, shutter in the lower third on the handedness side,
        # options bar pinned to the top centre clear of the notch.
        # flip_180=True is ORIENT_BOTTOM_UP — same layout, 180° mirror.
        notch = _OPTIONS_NOTCH_MARGIN
        side = _SHUTTER_SIDE_MARGIN
        end, start, center = Gtk.Align.END, Gtk.Align.START, Gtk.Align.CENTER
        if neutral:
            self._apply_placement(
                self._shutter,
                halign=center, valign=end,
                m_bottom=_SHUTTER_LANDSCAPE_INSET,
                flip=flip_180,
            )
        elif right:
            self._apply_placement(
                self._shutter,
                halign=end, valign=end,
                m_bottom=third, m_end=side,
                flip=flip_180,
            )
        else:
            self._apply_placement(
                self._shutter,
                halign=start, valign=end,
                m_bottom=third, m_start=side,
                flip=flip_180,
            )
        self._options_bar.set_orientation(Gtk.Orientation.HORIZONTAL)
        self._apply_placement(
            self._options_bar,
            halign=center, valign=start,
            m_top=notch,
            flip=flip_180,
        )

    def _layout_landscape(
        self,
        *,
        is_right_up: bool,
        neutral: bool,
        right: bool,
        third: int,
        user_vertical: int,
    ) -> None:
        # Canonical case (is_right_up=False) is ORIENT_LEFT_UP: phone
        # rotated CW 90°, left side physically up. The compositor
        # keeps widget dims in portrait orientation, so the user's
        # "right" lands on the widget's top edge. The options bar
        # stays HORIZONTAL (icons aligned along widget-x) so after the
        # 90° view tilt it reads as a single vertical column on the
        # user's left/right side. is_right_up=True is ORIENT_RIGHT_UP
        # — pure 180° mirror across both axes.
        inset = _SHUTTER_LANDSCAPE_INSET
        bar_inset = _OPTIONS_BAR_SIDE
        end, start, center = Gtk.Align.END, Gtk.Align.START, Gtk.Align.CENTER
        flip = is_right_up
        self._options_bar.set_orientation(Gtk.Orientation.HORIZONTAL)
        if neutral:
            # Shutter on user's right (widget top), icons on user's
            # left (widget bottom).
            self._apply_placement(
                self._shutter,
                halign=center, valign=start, m_top=inset,
                flip=flip,
            )
            self._apply_placement(
                self._options_bar,
                halign=center, valign=end, m_bottom=bar_inset,
                flip=flip,
            )
        elif right:
            # User's bottom-right = widget top-right.
            self._apply_placement(
                self._shutter,
                halign=end, valign=start,
                m_top=inset, m_end=user_vertical,
                flip=flip,
            )
            self._apply_placement(
                self._options_bar,
                halign=center, valign=end, m_bottom=bar_inset,
                flip=flip,
            )
        else:
            # User's bottom-left = widget bottom-right.
            self._apply_placement(
                self._shutter,
                halign=end, valign=end,
                m_bottom=inset, m_end=user_vertical,
                flip=flip,
            )
            self._apply_placement(
                self._options_bar,
                halign=center, valign=start, m_top=bar_inset,
                flip=flip,
            )

    def _on_orientation_tick(self, _widget: Any, _clock: Any) -> bool:
        # Fallback path when the accelerometer isn't available (desktop
        # builds, kiosks, etc.). Without a sensor we can only infer
        # portrait/landscape from the window size, so we collapse the
        # 4-state space to `normal` / `left-up`. Hysteresis avoids
        # flapping near 1:1 aspect ratios.
        w = self.get_width()
        h = self.get_height()
        if w <= 0 or h <= 0:
            return True  # GLib.SOURCE_CONTINUE
        if self._layout_landscape:
            landscape = w > h * 1.05
        else:
            landscape = w > h * 1.25
        new = ORIENT_LEFT_UP if landscape else ORIENT_NORMAL
        if new != self._applied_layout:
            self._device_orientation = new
            self._apply_layout_for(new)
        return True

    def _refresh_timer_button(self) -> None:
        value = self._timer_choices[self._timer_idx]
        if value == 0:
            # Use a _RotatableIcon so the alarm glyph follows device
            # orientation like the other icon buttons. Track it in
            # _rotatable_icons (replacing any previous timer icon there)
            # so the next _apply_layout_for picks it up.
            icon = _RotatableIcon()
            icon.set_from_icon_name("alarm-symbolic")
            icon.set_pixel_size(24)
            # Apply current rotation right away so the freshly created
            # icon doesn't appear upright for a frame after the swap.
            rot = _ICON_ROTATION_DEG.get(self._device_orientation, 0)
            icon.set_rotation_deg(rot)
            # set_child first so the previous timer icon gets orphaned;
            # _register_rotatable then prunes orphans and adds the new
            # one — replaces the ad-hoc "filter by parent" block.
            self._timer_button.set_child(icon)
            self._register_rotatable(icon)
            self._timer_button.set_tooltip_text(self._("Self-timer off"))
        else:
            # Label mode. Use a _RotatableLabel so the "3s" / "10s"
            # text rotates with device orientation just like the icon
            # variants. Bold/large styling comes from .camera-timer-text.
            label = _RotatableLabel()
            label.set_label(f"{value}s")
            label.add_css_class("camera-timer-text")
            rot = _ICON_ROTATION_DEG.get(self._device_orientation, 0)
            label.set_rotation_deg(rot)
            self._timer_button.set_child(label)
            self._register_rotatable(label)
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
        self._chrome.set_grid_visible(self._grid_on)

    # ------------------------------------------------------------------
    # Quality picker (photo: jpegenc quality, video: bitrate)
    # ------------------------------------------------------------------

    def _orient_seq(self, items: list) -> list:
        """Order children so the first item lands at the user's
        top/left regardless of device orientation. BOTTOM_UP and
        RIGHT_UP flip the user-view axis vs the widget axis, so we
        reverse the children for those."""
        if self._device_orientation in (ORIENT_BOTTOM_UP, ORIENT_RIGHT_UP):
            return list(reversed(items))
        return list(items)

    def _build_quality_popover(self) -> Gtk.Popover:
        # Orientation-aware layout: in landscape, the whole popover
        # content is transposed so it reads right in the user's view.
        # Outer box stacks the sections HORIZONTALLY in widget space
        # (which is vertical in the user's view); each inner row stacks
        # the buttons VERTICALLY in widget space (horizontal for user).
        # Button labels use _RotatableLabel so they're upright.
        landscape = bool(self._layout_landscape)
        outer_orient = (
            Gtk.Orientation.HORIZONTAL if landscape else Gtk.Orientation.VERTICAL
        )
        inner_orient = (
            Gtk.Orientation.VERTICAL if landscape else Gtk.Orientation.HORIZONTAL
        )
        label_rot = _ICON_ROTATION_DEG.get(self._device_orientation, 0)

        # Reset per-popover state — we rebuild this whole subtree on
        # every orientation change, so the old entries point at widgets
        # that are about to be unparented.
        self._photo_quality_buttons = []
        self._video_quality_buttons = []
        self._image_size_buttons = []

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=outer_orient, spacing=10)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)

        def _rotated_text(text: str) -> _RotatableLabel:
            lab = _RotatableLabel()
            lab.set_label(text)
            lab.set_rotation_deg(label_rot)
            return lab

        def _rotated_button(text: str, on_click: Callable) -> Gtk.Button:
            btn = Gtk.Button()
            btn.set_child(_rotated_text(text))
            btn.connect("clicked", on_click)
            return btn

        def _section(
            title: str,
            presets: list[tuple[str, int]],
            current: int,
            on_pick: Callable[[int], None],
            store: list[tuple[Gtk.Button, int]],
        ) -> None:
            # Multi-button sections (Photo/Video quality, Photo size,
            # Handedness): header ABOVE the row of buttons. Portrait =
            # VERTICAL (header above buttons), landscape = HORIZONTAL
            # in widget (header LEFT of buttons in widget = ABOVE in
            # user's rotated view).
            section_orient = (
                Gtk.Orientation.HORIZONTAL if landscape
                else Gtk.Orientation.VERTICAL
            )
            sec = Gtk.Box(orientation=section_orient, spacing=6)
            header = _rotated_text(title)
            header.set_xalign(0)
            header.add_css_class("heading")
            row = Gtk.Box(orientation=inner_orient, spacing=6)
            for label, value in presets:
                btn = _rotated_button(label, lambda _b, v=value: on_pick(v))
                if value == current:
                    btn.add_css_class("suggested-action")
                store.append((btn, value))
                row.append(btn)
            # Header above buttons in user's view — _orient_seq flips
            # widget child order for BOTTOM_UP / RIGHT_UP so the visual
            # ends up consistent across all four orientations.
            for w in self._orient_seq([header, row]):
                sec.append(w)
            box.append(sec)

        if self._capture_mode == "photo":
            _section(
                self._("Photo quality"),
                [
                    (self._("Eco"),  60),
                    (self._("Std"),  85),
                    (self._("High"), 92),
                    (self._("Max"),  98),
                ],
                self._jpeg_quality,
                self._set_jpeg_quality,
                self._photo_quality_buttons,
            )
            box.append(Gtk.Separator(orientation=inner_orient))

            # Photo size: string-keyed presets, built manually. Header
            # above the row of buttons, same as the other multi-button
            # sections.
            section_orient = (
                Gtk.Orientation.HORIZONTAL if landscape
                else Gtk.Orientation.VERTICAL
            )
            size_sec = Gtk.Box(orientation=section_orient, spacing=6)
            size_header = _rotated_text(self._("Photo size"))
            size_header.set_xalign(0)
            size_header.add_css_class("heading")
            size_row = Gtk.Box(orientation=inner_orient, spacing=6)
            size_presets: list[tuple[str, tuple[int, int] | None]] = [
                (self._("Max"),  None),
                (self._("2K"),   (2560, 1920)),
                (self._("FHD"),  (1920, 1440)),
                (self._("HD"),   (1280, 960)),
            ]
            for label, wh in size_presets:
                btn = _rotated_button(
                    label, lambda _b, v=wh: self._set_image_resolution(v),
                )
                if wh == self._image_resolution:
                    btn.add_css_class("suggested-action")
                self._image_size_buttons.append((btn, wh))
                size_row.append(btn)
            for w in self._orient_seq([size_header, size_row]):
                size_sec.append(w)
            box.append(size_sec)

        elif self._capture_mode == "video":
            _section(
                self._("Video quality"),
                [
                    (self._("Eco"),   2000),
                    (self._("Std"),   4000),
                    (self._("High"),  8000),
                    (self._("Max"),  16000),
                ],
                self._video_bitrate_kbps,
                self._set_video_bitrate,
                self._video_quality_buttons,
            )

        popover.set_child(box)
        return popover

    def _build_settings_popover(self) -> Gtk.Popover:
        # Same orientation-aware layout pattern as the Quality popover:
        # in landscape, the section + its button row are laid out so the
        # user sees an upright stack and an upright row of buttons.
        landscape = bool(self._layout_landscape)
        outer_orient = (
            Gtk.Orientation.HORIZONTAL if landscape else Gtk.Orientation.VERTICAL
        )
        inner_orient = (
            Gtk.Orientation.VERTICAL if landscape else Gtk.Orientation.HORIZONTAL
        )
        label_rot = _ICON_ROTATION_DEG.get(self._device_orientation, 0)

        self._handedness_buttons = []

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=outer_orient, spacing=10)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)

        def _rotated_text(text: str) -> _RotatableLabel:
            lab = _RotatableLabel()
            lab.set_label(text)
            lab.set_rotation_deg(label_rot)
            return lab

        # Handedness has the multi-button layout (header ABOVE buttons),
        # geotagging is a boolean (header NEXT TO the switch). Two
        # separate orientation values for those two semantics.
        section_orient = (
            Gtk.Orientation.HORIZONTAL if landscape
            else Gtk.Orientation.VERTICAL
        )
        gps_section_orient = (
            Gtk.Orientation.VERTICAL if landscape
            else Gtk.Orientation.HORIZONTAL
        )
        sec = Gtk.Box(orientation=section_orient, spacing=6)
        header = _rotated_text(self._("Handedness"))
        header.set_xalign(0)
        header.add_css_class("heading")
        row = Gtk.Box(orientation=inner_orient, spacing=6)
        presets: list[tuple[str, str]] = [
            (self._("Right"),   "right"),
            (self._("Left"),    "left"),
            (self._("Neutral"), "neutral"),
        ]
        for label, value in presets:
            btn = Gtk.Button()
            btn.set_child(_rotated_text(label))
            if value == self._handedness:
                btn.add_css_class("suggested-action")
            btn.connect("clicked", lambda _b, v=value: self._set_handedness(v))
            self._handedness_buttons.append((btn, value))
            row.append(btn)
        for w in self._orient_seq([header, row]):
            sec.append(w)
        box.append(sec)

        box.append(Gtk.Separator(orientation=inner_orient))

        # Geotagging: real boolean via _RotatableSwitch so it rotates
        # in sync with the rest of the chrome (Gtk.Switch is normally
        # horizontal in widget-space, which appears vertical in
        # landscape without rotation). The switch sits at the user's
        # RIGHT regardless of orientation; achieved by reversing the
        # widget child order on BOTTOM_UP / RIGHT_UP (handled by
        # _orient_seq).
        gps_sec = Gtk.Box(orientation=gps_section_orient, spacing=12)
        gps_header = _rotated_text(self._("Geotagging"))
        gps_header.set_xalign(0)
        gps_header.add_css_class("heading")
        gps_header.set_hexpand(True)
        self._geo_switch = _RotatableSwitch()
        self._geo_switch.set_active(self._geo_enabled)
        self._geo_switch.set_halign(Gtk.Align.END)
        self._geo_switch.set_valign(Gtk.Align.CENTER)
        self._geo_switch.set_rotation_deg(label_rot)
        self._register_rotatable(self._geo_switch)
        self._geo_switch.connect("state-set", self._on_geo_switch_state_set)
        for w in self._orient_seq([gps_header, self._geo_switch]):
            gps_sec.append(w)
        box.append(gps_sec)

        popover.set_child(box)
        return popover

    def _set_handedness(self, value: str) -> None:
        if value not in ("right", "left", "neutral"):
            return
        if value == self._handedness:
            return
        self._handedness = value
        # Persist via the shared debounced path; the flush method writes
        # the `handedness` field on Settings along with the other camera
        # picks, so the standalone save() call here is unnecessary.
        self._persist_settings()
        # Highlight the active button.
        for btn, v in self._handedness_buttons:
            if v == value:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        # Re-run the layout pass for the current orientation so shutter
        # and options bar reposition to the new side.
        if self._applied_layout is not None:
            current = self._applied_layout
            self._applied_layout = None  # force re-apply
            self._apply_layout_for(current)

    def _persist_settings(self) -> None:
        # Debounced — calls accumulate, flushed once after _PERSIST_DELAY_MS.
        # Prevents settings.save() spam when sliders or quality buttons are
        # tapped rapidly. Flushed synchronously on close.
        if self._settings is None:
            return
        if self._settings_persist_source is not None:
            try:
                GLib.source_remove(self._settings_persist_source)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
        self._settings_persist_source = GLib.timeout_add(
            _PERSIST_DELAY_MS, self._persist_settings_flush
        )

    def _persist_settings_flush(self) -> bool:
        self._settings_persist_source = None
        if self._settings is None:
            return False
        try:
            self._settings.camera_jpeg_quality = int(self._jpeg_quality)
            self._settings.camera_video_bitrate_kbps = int(self._video_bitrate_kbps)
            if self._image_resolution is None:
                self._settings.camera_image_resolution = None
            else:
                self._settings.camera_image_resolution = [
                    int(self._image_resolution[0]),
                    int(self._image_resolution[1]),
                ]
            self._settings.handedness = self._handedness
            self._settings.camera_geo_enabled = bool(self._geo_enabled)
            self._settings.save()
        except Exception:
            LOGGER.debug("camera settings persist failed", exc_info=True)
        return False

    def _set_jpeg_quality(self, value: int) -> None:
        self._jpeg_quality = value
        # Live-update the running jpegenc; no pipeline restart needed.
        if self._pipeline is not None:
            jpeg = self._pipeline.get_by_name("snap_jpeg")
            if jpeg is not None:
                try:
                    jpeg.set_property("quality", value)
                except Exception:
                    LOGGER.debug("jpegenc quality update failed", exc_info=True)
        for btn, v in self._photo_quality_buttons:
            if v == value:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        self._persist_settings()

    def _set_video_bitrate(self, value: int) -> None:
        self._video_bitrate_kbps = value
        for btn, v in self._video_quality_buttons:
            if v == value:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        self._persist_settings()

    def _set_image_resolution(
        self, wh: tuple[int, int] | None,
    ) -> None:
        self._image_resolution = wh
        for btn, v in self._image_size_buttons:
            if v == wh:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        self._persist_settings()

    def _set_capture_mode(self, mode: str) -> None:
        if mode == self._capture_mode or mode not in ("photo", "video"):
            return
        self._capture_mode = mode
        # Rebuild the popover so it shows mode-relevant sections.
        self._quality_button.set_popover(self._build_quality_popover())
        self._update_shutter_icon()
        self._apply_mode_visibility()
        self._show_toast(
            self._("Video mode") if mode == "video" else self._("Photo mode")
        )

    def _apply_mode_visibility(self) -> None:
        """Hide options-bar buttons that don't apply to the current
        capture mode. Currently the self-timer is photo-only; everything
        else is useful in both modes."""
        photo_only = (self._capture_mode == "photo")
        try:
            self._timer_button.set_visible(photo_only)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)

    # ------------------------------------------------------------------
    # Zoom (digital, widget-snapshot only)
    # ------------------------------------------------------------------

    def _apply_zoom(self, zoom: float) -> None:
        zoom = max(1.0, min(self._zoom_max, zoom))
        self._zoom = zoom
        self._picture.set_zoom(zoom)
        # Brackets/grid follow the zoomed image rect, so redraw them too.
        self._chrome.queue_draw()

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

    def _try_start_geo_silent(self) -> None:
        """Best-effort GeoClue handshake. Quietly does nothing if
        GeoClue isn't available — the geotag flag stays in the user's
        intended state, captures just won't get GPS embedded."""
        if self._geo is not None:
            return
        self._geo = GeoClient(app_id="yaga")
        ok = self._geo.start(
            accuracy=8,
            on_update=self._on_geo_update,
            on_error=lambda msg: GLib.idle_add(self._on_geo_error, msg),
        )
        if not ok:
            self._geo = None

    def _on_geo_switch_state_set(self, _sw: Gtk.Switch, state: bool) -> bool:
        # Gtk.Switch fires "state-set" with the requested new state.
        # Return False so the Switch accepts the state change visually.
        self._geo_enabled = bool(state)
        self._persist_settings()
        if self._geo_enabled:
            self._try_start_geo_silent()
        else:
            if self._geo is not None:
                try:
                    self._geo.stop()
                except Exception:
                    LOGGER.debug("camera cleanup/op failed", exc_info=True)
                self._geo = None
        return False

    def _on_geo_update(self, location: dict[str, Any] | None) -> None:
        # Silent — no toast on first fix. The capture path reads the
        # GeoClient's `latest()` directly so we don't need to surface
        # progress to the UI.
        pass

    def _on_geo_error(self, message: str) -> bool:
        # Silent fail: log for debugging, stop the GeoClient, leave the
        # user-intent flag (_geo_enabled) alone so the next attempt can
        # succeed if GeoClue becomes available later. No toast.
        LOGGER.debug("GeoClue error: %s", message)
        # Null first so a re-entrant error fired during stop() can't
        # double-stop or read a partially-torn-down client.
        client = self._geo
        self._geo = None
        if client is not None:
            try:
                client.stop()
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
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
        # The GestureClick on the picture is in BUBBLE phase, so it
        # still fires for clicks that the overlay icons consumed first.
        # Drop any click that didn't actually land inside the visible
        # image rect (i.e. clicks on letterbox / icon bar): no focus
        # pulse, no AF call, no flicker over the icon the user pressed.
        rect = self._chrome._image_rect(
            self._picture.get_width(), self._picture.get_height()
        )
        rx, ry, rw, rh = rect
        if rw <= 0 or rh <= 0 or not (rx <= x <= rx + rw and ry <= y <= ry + rh):
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

    def _emit_start_capture_async(
        self,
        *,
        get_pipeline: Callable[[], Any],
        get_src: Callable[[Any], Any],
        delay_ms: int,
        log_prefix: str,
        thread_name: str,
        extra_guard: Callable[[], bool] | None = None,
    ) -> None:
        """Emit gst-droid's `start-capture` signal after `delay_ms`,
        on the GLib main loop.

        Earlier versions ran the emit on a worker thread to avoid
        droid_media_camera_take_picture deadlocking against the
        preview-frame-pull thread. That deadlock is specifically about
        the GStreamer *streaming* thread (where appsink new-sample
        callbacks originate), not the GLib main loop — so a main-loop
        emit is safe AND it serialises with `_stop_pipeline` /
        `_image_teardown` automatically (single-threaded by virtue of
        the GLib loop). The previous threaded version had a window
        between get_state() and emit() in which teardown could drop
        refs, leaving us emitting against an unparented element.

        We keep the `thread_name` arg for call-site compatibility; it
        is unused now."""
        del thread_name  # kept for backwards-compat with callers
        gst = self._Gst

        def _emit() -> bool:
            pipeline = get_pipeline()
            if pipeline is None:
                return False
            if extra_guard is not None and not extra_guard():
                return False
            try:
                _, state, _ = pipeline.get_state(0)
                if state not in (gst.State.PAUSED, gst.State.PLAYING):
                    _dlog(
                        f"[yaga.camera] {log_prefix}: skip start-capture, "
                        f"pipeline in {state}"
                    )
                    return False
            except Exception:
                return False
            src = get_src(pipeline)
            if src is None:
                return False
            try:
                src.emit("start-capture")
                _dlog(
                    f"[yaga.camera] {log_prefix}: start-capture emitted"
                )
            except Exception as exc:
                _dlog(
                    f"[yaga.camera] {log_prefix}: start-capture failed: {exc}"
                )
            return False

        GLib.timeout_add(delay_ms, _emit)

    def _capture(self) -> None:
        if self._busy_capture:
            return
        # Routing:
        #   1. In-pipeline imgsrc (Halium with cooperative gst-droid) —
        #      best case, instant + full res. Currently unavailable on
        #      this user's HAL but kept for other builds.
        #   2. Halium with the 720p preview cap in place — tear down
        #      preview, run a transient mode=1 image pipeline to get
        #      full sensor res via imgsrc, restore preview.
        #   3. Default vfsrc + jpegenc with the valve gating jpegenc.
        is_halium_capped = (
            self._capsfilter is not None
            and self._capsfilter.get_name() == "halium_default_cap"
            and self._imgsink is None
        )
        if is_halium_capped:
            self._capture_via_image_pipeline()
            return

        sink = self._imgsink if self._imgsink is not None else self._appsink
        if sink is None:
            _dlog(f"[yaga.camera] capture: ignored (imgsink={self._imgsink is not None}, "
                f"appsink={self._appsink is not None})")
            return
        path_name = "imgsrc" if sink is self._imgsink else "vfsrc+jpegenc"
        _dlog(f"[yaga.camera] capture: start path={path_name}")
        self._busy_capture = True
        self._shutter.set_sensitive(False)

        # If we're on the vfsrc+jpegenc fallback path and the source is
        # currently 720p-capped (Halium default), temporarily change
        # the capsfilter to forbid the 720p range. That triggers a
        # caps renegotiation through to droidcamsrc, which then
        # reconfigures its HAL session to the sensor-native resolution.
        # The original cap is restored in _close_valve_and_disconnect.
        self._capture_min_width = 0
        self._capture_saved_caps = None
        if (sink is self._appsink
                and self._capsfilter is not None
                and self._capsfilter.get_name() == "halium_default_cap"):
            try:
                self._capture_saved_caps = self._capsfilter.get_property("caps")
                highres = self._Gst.Caps.from_string(
                    "video/x-raw,width=(int)[1281,99999]"
                )
                self._capsfilter.set_property("caps", highres)
                self._capture_min_width = 1281
                _dlog("[yaga.camera] capture: caps swapped to high-res "
                    "(waiting for renegotiated frame)")
            except Exception as exc:
                self._capture_saved_caps = None
                self._capture_min_width = 0
                _dlog(f"[yaga.camera] capture: caps swap failed: {exc}")

        # connect + emit + timeout setup wrapped so a mid-setup failure
        # (e.g. OOM during GLib.timeout_add_seconds) doesn't leak the
        # signal handler — a second _capture() would then connect again
        # and produce duplicate saves.
        try:
            self._capture_signal_id = sink.connect(
                "new-sample", self._on_capture_sample
            )
            self._capture_signal_sink = sink
        except Exception:
            LOGGER.debug("capture: sink connect failed", exc_info=True)
            self._capture_signal_id = None
            self._capture_signal_sink = None
            self._busy_capture = False
            self._shutter.set_sensitive(True)
            return

        if sink is self._imgsink:
            # Trigger the HAL still-capture. There are two issues we
            # have to dodge on gst-droid pre-PR#39:
            #   1. Race between caps-negotiation on the imgsrc branch
            #      and start-capture, which otherwise asserts
            #      `gst_buffer_pool_set_flushing: pool is not a buffer
            #      pool`. We pin caps=image/jpeg on the imgsink to make
            #      negotiation deterministic, plus add a small delay.
            #   2. droid_media_camera_take_picture can deadlock when
            #      called from the same thread that pulls preview
            #      frames. We dispatch the emit from a worker thread so
            #      it doesn't compete with the GLib main loop.
            self._emit_start_capture_async(
                get_pipeline=lambda: self._pipeline,
                get_src=lambda p: p.get_by_name("src"),
                delay_ms=150,
                log_prefix="capture",
                thread_name="yaga-start-capture",
                extra_guard=lambda: self._busy_capture,
            )
        else:
            # vfsrc path: open the valve so jpegenc gets one frame.
            if self._valve is not None:
                self._valve.set_property("drop", False)
                _dlog("[yaga.camera] capture: valve opened")

        # Safety timeout — generous because the HAL may take a moment
        # to capture (AF, exposure, encode) at full resolution, and the
        # caps-swap path also has to wait for HAL reconfigure. Wrapped
        # so a failure here also disconnects the signal handler above
        # rather than leaving it dangling.
        try:
            self._capture_timeout_id = GLib.timeout_add_seconds(
                15, self._on_capture_timeout
            )
        except Exception:
            LOGGER.debug("capture: timeout setup failed", exc_info=True)
            self._close_valve_and_disconnect()
            self._busy_capture = False
            self._shutter.set_sensitive(True)

    def _close_valve_and_disconnect(self) -> None:
        if self._valve is not None:
            try:
                self._valve.set_property("drop", True)
            except Exception:
                LOGGER.debug("valve close failed", exc_info=True)
        if self._capture_signal_id is not None and self._capture_signal_sink is not None:
            try:
                self._capture_signal_sink.disconnect(self._capture_signal_id)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
        self._capture_signal_id = None
        self._capture_signal_sink = None
        if self._capture_timeout_id is not None:
            GLib.source_remove(self._capture_timeout_id)
            self._capture_timeout_id = None
        # Put the Halium 720p cap back if we lifted it for this capture.
        if self._capture_saved_caps is not None and self._capsfilter is not None:
            try:
                self._capsfilter.set_property("caps", self._capture_saved_caps)
                _dlog("[yaga.camera] capture: restored 720p preview cap")
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
        self._capture_saved_caps = None
        self._capture_min_width = 0

    def _on_capture_sample(self, sink: Any) -> Any:
        gst = self._Gst
        # The signal can fire again between the moment we accept a
        # sample and the moment idle_add runs _finish_capture, which
        # would queue a second save. Bail out if we've already started
        # handing off (signal id cleared below).
        if self._capture_signal_id is None:
            return gst.FlowReturn.OK
        try:
            sample = sink.emit("pull-sample")
        except Exception as exc:
            sample = None
            _dlog(f"[yaga.camera] capture: pull-sample failed: {exc}")
        if sample is None:
            return gst.FlowReturn.OK
        # If a caps-swap is in progress, drop in-flight low-res frames
        # so the saved photo is from after the HAL renegotiation.
        if self._capture_min_width > 0:
            caps = sample.get_caps()
            s = caps.get_structure(0) if caps is not None and caps.get_size() > 0 else None
            ok_w, w = (s.get_int("width") if s is not None else (False, 0))
            if ok_w and w < self._capture_min_width:
                _dlog(f"[yaga.camera] capture: skipping low-res frame "
                    f"({w}px, waiting for >={self._capture_min_width}px)")
                return gst.FlowReturn.OK
            # First matching frame — stop filtering.
            self._capture_min_width = 0
            _dlog(f"[yaga.camera] capture: high-res frame received ({w}px)")
        # One-shot: disconnect immediately so the next new-sample doesn't
        # trigger a duplicate save while _finish_capture is still queued.
        sig_id = self._capture_signal_id
        self._capture_signal_id = None
        sig_sink = self._capture_signal_sink
        if sig_sink is not None and sig_id is not None:
            try:
                sig_sink.disconnect(sig_id)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
        _dlog("[yaga.camera] capture: new-sample accepted")
        GLib.idle_add(self._finish_capture, sample)
        return gst.FlowReturn.OK

    def _finish_capture(self, sample: Any) -> bool:
        self._close_valve_and_disconnect()
        if sample is None:
            _dlog("[yaga.camera] capture: finish with no sample")
            self._show_toast(self._("No frame available"))
        else:
            _dlog("[yaga.camera] capture: writing sample")
            self._write_sample(sample)
            self._flash_screen()
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        return False

    def _on_capture_timeout(self) -> bool:
        # Reached only when no sample showed up — pipeline stalled or the
        # camera produces no frames. Reset state and tell the user.
        _dlog("[yaga.camera] capture: TIMEOUT — no sample arrived in 10 s")
        self._capture_timeout_id = None
        self._close_valve_and_disconnect()
        self._show_toast(self._("No frame available"))
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        return False

    # ------------------------------------------------------------------
    # Transient image-mode pipeline for full-resolution Halium captures
    # ------------------------------------------------------------------

    def _show_capture_spinner(self, visible: bool) -> None:
        self._capture_spinner_box.set_visible(visible)
        if visible:
            self._capture_spinner.start()
        else:
            self._capture_spinner.stop()

    def _start_swipe_hint(self) -> None:
        if self._swipe_hint_pulse_id is not None:
            return  # already running
        self._swipe_hint_cycles_left = 2
        self._swipe_hint_phase = 0.0
        self._swipe_hint_direction = 1
        self._swipe_hint.set_visible(True)
        self._swipe_hint.set_opacity(0.0)
        self._swipe_hint_pulse_id = GLib.timeout_add(40, self._swipe_hint_tick)

    def _swipe_hint_tick(self) -> bool:
        # Fade in to 1.0, fade back to 0.0, count one full cycle. After
        # 2 cycles hide the widget permanently.
        self._swipe_hint_phase += 0.035 * self._swipe_hint_direction
        if self._swipe_hint_phase >= 1.0:
            self._swipe_hint_phase = 1.0
            self._swipe_hint_direction = -1
        elif self._swipe_hint_phase <= 0.0:
            self._swipe_hint_phase = 0.0
            self._swipe_hint_direction = 1
            self._swipe_hint_cycles_left -= 1
            if self._swipe_hint_cycles_left <= 0:
                self._swipe_hint.set_visible(False)
                self._swipe_hint_pulse_id = None
                return False
        self._swipe_hint.set_opacity(self._swipe_hint_phase)
        return True

    def _start_record_blink(self) -> None:
        self._position_record_dot()
        self._record_dot.set_visible(True)
        self._record_dot.set_opacity(1.0)
        if self._record_dot_blink_id is None:
            self._record_dot_blink_id = GLib.timeout_add(
                650, self._toggle_record_dot,
            )

    def _toggle_record_dot(self) -> bool:
        opaque = self._record_dot.get_opacity() > 0.5
        self._record_dot.set_opacity(0.0 if opaque else 1.0)
        return True  # keep ticking

    def _stop_record_blink(self) -> None:
        if self._record_dot_blink_id is not None:
            try:
                GLib.source_remove(self._record_dot_blink_id)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._record_dot_blink_id = None
        self._record_dot.set_visible(False)
        self._record_dot.set_opacity(1.0)

    def _position_record_dot(self) -> None:
        """Place the recording indicator in the user's top-right
        corner OF THE VIDEO IMAGE (not the widget bounds). With
        letterboxing, widget bounds extend past the visible image —
        we want the dot sitting inside the live frame so it reads as
        "this image is recording". Falls back to widget corner when
        the paintable hasn't supplied an intrinsic size yet."""
        # Reset all margins so we can additively set just the two
        # margins that pin the dot to the right edge per orientation.
        self._record_dot.set_margin_top(0)
        self._record_dot.set_margin_bottom(0)
        self._record_dot.set_margin_start(0)
        self._record_dot.set_margin_end(0)

        orient = self._device_orientation
        inset = _RECORD_DOT_INSET

        # Image rect (in widget coords) via the shared helper.
        pic_w = max(0, self._picture.get_width())
        pic_h = max(0, self._picture.get_height())
        rect = _compute_image_rect(self._picture, pic_w, pic_h)
        left, top, img_w, img_h = rect

        if img_w <= 0 or img_h <= 0:
            # No paintable yet — fall back to widget corner.
            mapping = {
                ORIENT_NORMAL:    (Gtk.Align.END,   Gtk.Align.START, 28, 28),
                ORIENT_BOTTOM_UP: (Gtk.Align.START, Gtk.Align.END,   28, 28),
                ORIENT_LEFT_UP:   (Gtk.Align.START, Gtk.Align.START, 28, 28),
                ORIENT_RIGHT_UP:  (Gtk.Align.END,   Gtk.Align.END,   28, 28),
            }
            halign, valign, m_h, m_v = mapping.get(
                orient, (Gtk.Align.END, Gtk.Align.START, 28, 28)
            )
            self._record_dot.set_halign(halign)
            self._record_dot.set_valign(valign)
            if halign == Gtk.Align.END:
                self._record_dot.set_margin_end(m_h)
            else:
                self._record_dot.set_margin_start(m_h)
            if valign == Gtk.Align.START:
                self._record_dot.set_margin_top(m_v)
            else:
                self._record_dot.set_margin_bottom(m_v)
            return

        # Letterbox widths (computed from the image rect via the shared
        # helper above) so the dot sits inside the image rect, not at
        # the widget edge.
        right = left + img_w
        bottom = top + img_h
        lb_top = int(top)
        lb_bottom = int(pic_h - bottom)
        lb_left = int(left)
        lb_right = int(pic_w - right)

        if orient == ORIENT_NORMAL:
            # User top-right = image top-right corner of widget.
            self._record_dot.set_halign(Gtk.Align.END)
            self._record_dot.set_valign(Gtk.Align.START)
            self._record_dot.set_margin_end(lb_right + inset)
            self._record_dot.set_margin_top(lb_top + inset)
        elif orient == ORIENT_BOTTOM_UP:
            # User top-right = image bottom-left in widget.
            self._record_dot.set_halign(Gtk.Align.START)
            self._record_dot.set_valign(Gtk.Align.END)
            self._record_dot.set_margin_start(lb_left + inset)
            self._record_dot.set_margin_bottom(lb_bottom + inset)
        elif orient == ORIENT_LEFT_UP:
            # User top-right = image top-left in widget.
            self._record_dot.set_halign(Gtk.Align.START)
            self._record_dot.set_valign(Gtk.Align.START)
            self._record_dot.set_margin_start(lb_left + inset)
            self._record_dot.set_margin_top(lb_top + inset)
        elif orient == ORIENT_RIGHT_UP:
            # User top-right = image bottom-right in widget.
            self._record_dot.set_halign(Gtk.Align.END)
            self._record_dot.set_valign(Gtk.Align.END)
            self._record_dot.set_margin_end(lb_right + inset)
            self._record_dot.set_margin_bottom(lb_bottom + inset)

    def _position_swipe_hint(self) -> None:
        """Place the swipe hint at the user's bottom-centre. In portrait
        that's below the shutter (which lives in the lower third);
        in landscape the same user-bottom-centre lands between the
        shutter (at the corner) and the image rect on the inward side.
        """
        hint = self._swipe_hint
        hint.set_margin_top(0); hint.set_margin_bottom(0)
        hint.set_margin_start(0); hint.set_margin_end(0)
        orient = self._device_orientation
        # (halign, valign, margin-edge) per orientation. The
        # margin-edge name picks which margin we set to push the hint
        # ~20 px inward from the screen edge.
        mapping = {
            ORIENT_NORMAL:    (Gtk.Align.CENTER, Gtk.Align.END,   "bottom"),
            ORIENT_BOTTOM_UP: (Gtk.Align.CENTER, Gtk.Align.START, "top"),
            ORIENT_LEFT_UP:   (Gtk.Align.END,    Gtk.Align.CENTER, "end"),
            ORIENT_RIGHT_UP:  (Gtk.Align.START,  Gtk.Align.CENTER, "start"),
        }
        halign, valign, edge = mapping.get(
            orient, (Gtk.Align.CENTER, Gtk.Align.END, "bottom")
        )
        hint.set_halign(halign)
        hint.set_valign(valign)
        if edge == "bottom":
            hint.set_margin_bottom(20)
        elif edge == "top":
            hint.set_margin_top(20)
        elif edge == "end":
            hint.set_margin_end(20)
        elif edge == "start":
            hint.set_margin_start(20)

    def _capture_via_image_pipeline(self) -> None:
        """Stop the preview pipeline, build a droidcamsrc mode=1 ->
        imgsrc -> appsink pipeline, emit start-capture, save the HAL
        JPEG, then restore the preview pipeline. The frozen-preview
        window during HAL mode switch is bridged by a spinner sitting
        on top of the last live frame (instead of a black background)."""
        self._busy_capture = True
        self._shutter.set_sensitive(False)
        # Snapshot the current preview paintable to a static one so the
        # picture keeps showing the last live frame while the preview
        # pipeline is torn down for the capture. When _start_pipeline
        # runs again afterwards it sets the new live paintable from
        # gtk4paintablesink, replacing this frozen still.
        self._freeze_preview_frame()
        self._show_capture_spinner(True)

        device = self._current_device() or {}
        cam_id = device.get("droidcam_id", 0)
        _dlog(f"[yaga.camera] image-capture: stopping preview for cam {cam_id}")
        self._stop_pipeline()
        # Defer the build by one idle tick so the spinner actually
        # renders before the synchronous parts of the build (state
        # changes, get_state waits) block the main loop.
        GLib.idle_add(self._image_pipeline_build, cam_id)

    def _freeze_preview_frame(self) -> None:
        paintable = self._picture.get_paintable()
        if paintable is None:
            return
        intr_w = paintable.get_intrinsic_width()
        intr_h = paintable.get_intrinsic_height()
        if intr_w <= 0 or intr_h <= 0:
            return
        try:
            snap = Gtk.Snapshot()
            paintable.snapshot(snap, intr_w, intr_h)
            size = Graphene.Size().init(intr_w, intr_h)
            still = snap.to_paintable(size)
            if still is not None:
                self._picture.set_paintable(still)
        except Exception:
            LOGGER.debug("freeze_preview_frame failed", exc_info=True)

    def _image_pipeline_build(self, cam_id: int) -> bool:
        gst = self._Gst

        # Transient image-capture pipeline:
        #   droidcamsrc(mode=1) → vfsrc → fakesink   (HAL needs an
        #                                              active viewfinder
        #                                              to function)
        #                       → imgsrc → queue → appsink (capture)
        # We are in mode=1 here because gst-droid's start-capture
        # semantics are mode-dependent: in mode=2 it tries to start
        # video recording (hence the "cannot record video in raw mode"
        # error we hit when emitting start-capture against the main
        # mode=2 preview pipeline); in mode=1 it triggers a still
        # capture. imgsrc is a static ALWAYS pad on this droidcamsrc
        # build (per gst_droidcamsrc_init), so we fetch it with
        # get_static_pad — request_pad_simple silently returns None
        # for static pads, which was our earlier dead end.
        pipeline = gst.Pipeline.new("yaga-image-capture")
        src = gst.ElementFactory.make("droidcamsrc", "src")
        if src is None:
            return self._image_capture_failed("droidcamsrc element unavailable")
        try:
            src.set_property("camera-device", cam_id)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
        try:
            src.set_property("mode", 1)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
        pipeline.add(src)

        # vfsrc → fakesink. droidcamsrc's HAL setup expects a live
        # viewfinder pad even when we only care about imgsrc.
        vf_fakesink = gst.ElementFactory.make("fakesink", "vf_fakesink")
        if vf_fakesink is not None:
            vf_fakesink.set_property("sync", False)
            vf_fakesink.set_property("async", False)
            pipeline.add(vf_fakesink)
            vf_pad = src.get_static_pad("vfsrc")
            if vf_pad is None:
                try:
                    vf_pad = src.request_pad_simple("vfsrc")
                except Exception:
                    vf_pad = None
            if vf_pad is not None:
                vf_pad.link(vf_fakesink.get_static_pad("sink"))

        # imgsrc → queue → appsink. Pin caps=image/jpeg on the sink so
        # negotiation completes without a downstream buffer query —
        # otherwise gst-droid asserts on a missing buffer pool when
        # start-capture fires before negotiation propagates upstream.
        queue = gst.ElementFactory.make("queue", "img_queue")
        sink = gst.ElementFactory.make("appsink", "img_sink")
        if queue is None or sink is None:
            return self._image_capture_failed("queue/appsink unavailable")
        queue.set_property("leaky", 2)
        queue.set_property("max-size-buffers", 1)
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", True)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        sink.set_property("caps", gst.Caps.from_string("image/jpeg"))
        pipeline.add(queue)
        pipeline.add(sink)
        queue.link(sink)

        imgsrc_pad = src.get_static_pad("imgsrc")
        if imgsrc_pad is None:
            try:
                imgsrc_pad = src.request_pad_simple("imgsrc")
            except Exception:
                imgsrc_pad = None
        if imgsrc_pad is None:
            pipeline.set_state(gst.State.NULL)
            return self._image_capture_failed("imgsrc pad unavailable")
        if imgsrc_pad.link(queue.get_static_pad("sink")) != gst.PadLinkReturn.OK:
            pipeline.set_state(gst.State.NULL)
            return self._image_capture_failed("imgsrc -> queue link failed")

        self._image_pipeline = pipeline
        self._image_src = src
        self._image_signal_id = sink.connect(
            "new-sample", self._on_image_capture_sample
        )

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_image_pipeline_error)
        self._image_bus = bus

        result = pipeline.set_state(gst.State.PLAYING)
        _dlog(f"[yaga.camera] image-capture: pipeline PLAYING -> "
            f"{result.value_nick if result else '?'}")

        # Trigger start-capture from a worker thread (gst-droid pre-PR#39
        # deadlocks when called from the same thread that pulls preview
        # frames). 500 ms delay so the HAL finishes its Photography
        # reconfigure before we ask it to capture.
        self._emit_start_capture_async(
            get_pipeline=lambda: self._image_pipeline,
            get_src=lambda _p: self._image_src,
            delay_ms=500,
            log_prefix="image-capture",
            thread_name="yaga-img-start-capture",
        )

        self._image_timeout_id = GLib.timeout_add_seconds(
            15, self._on_image_capture_timeout,
        )
        return False  # one-shot idle

    def _on_image_capture_sample(self, sink: Any) -> Any:
        gst = self._Gst
        if self._image_signal_id is None:
            return gst.FlowReturn.OK
        try:
            sample = sink.emit("pull-sample")
        except Exception as exc:
            _dlog(f"[yaga.camera] image-capture: pull-sample failed: {exc}")
            sample = None
        if sample is None:
            return gst.FlowReturn.OK
        # One-shot disconnect so we don't queue more saves.
        sid = self._image_signal_id
        self._image_signal_id = None
        try:
            sink.disconnect(sid)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)

        caps = sample.get_caps()
        s = caps.get_structure(0) if caps is not None and caps.get_size() > 0 else None
        if s is not None:
            ok_w, w = s.get_int("width")
            ok_h, h = s.get_int("height")
            _dlog(f"[yaga.camera] image-capture: sample received {w}x{h}")

        GLib.idle_add(self._image_capture_finish, sample)
        return gst.FlowReturn.OK

    def _image_capture_finish(self, sample: Any) -> bool:
        if self._image_timeout_id is not None:
            GLib.source_remove(self._image_timeout_id)
            self._image_timeout_id = None
        self._image_teardown()
        # Kick the preview-restart idle FIRST so the heavy pipeline
        # rebuild (mode=2, HAL reconfigure, gtk4paintablesink wiring)
        # can run in parallel with the JPEG write + EXIF below. Both
        # cost ~hundreds of ms on Halium; serialising them doubles
        # the perceived snap-to-preview latency.
        GLib.idle_add(self._start_pipeline)
        if sample is not None:
            _dlog("[yaga.camera] image-capture: writing sample")
            self._write_sample(sample)
            self._flash_screen()
        else:
            self._show_toast(self._("No frame available"))
        self._show_capture_spinner(False)
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        return False

    def _on_image_capture_timeout(self) -> bool:
        _dlog("[yaga.camera] image-capture: TIMEOUT — no sample arrived in 15 s")
        self._image_timeout_id = None
        self._image_teardown()
        self._show_toast(self._("No frame available"))
        self._show_capture_spinner(False)
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        GLib.idle_add(self._start_pipeline)
        return False

    def _image_capture_failed(self, reason: str) -> bool:
        _dlog(f"[yaga.camera] image-capture: {reason}")
        self._image_teardown()
        # Restore the 720p preview cap on the failure path too — the
        # vfsrc+jpegenc fallback may have swapped it for full-res; if
        # the swap stayed in effect, the next preview pipeline would
        # come up at native sensor resolution and crush phosh.
        if self._capture_saved_caps is not None and self._capsfilter is not None:
            try:
                self._capsfilter.set_property("caps", self._capture_saved_caps)
            except Exception:
                LOGGER.debug("caps restore after failure failed", exc_info=True)
        self._capture_saved_caps = None
        self._capture_min_width = 0
        self._show_toast(self._("Capture failed: %s") % reason)
        self._show_capture_spinner(False)
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        GLib.idle_add(self._start_pipeline)
        return False

    def _image_teardown(self) -> None:
        if self._image_bus is not None:
            try:
                self._image_bus.remove_signal_watch()
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._image_bus = None
        if self._image_pipeline is not None:
            try:
                self._image_pipeline.set_state(self._Gst.State.NULL)
                self._image_pipeline.get_state(2 * self._Gst.SECOND)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._image_pipeline = None
        self._image_src = None
        self._image_signal_id = None

    def _on_image_pipeline_error(self, _bus: Any, msg: Any) -> None:
        # Don't just log — route through _image_capture_failed so the
        # user gets feedback (toast, shutter re-armed, preview restored)
        # instead of staring at the spinner for the full 15 s timeout.
        # Bail if state was already torn down (timeout fired first).
        reason = "image-capture bus error"
        try:
            err, dbg = msg.parse_error()
            _dlog(f"[yaga.camera] image-capture bus error: "
                f"{err.message if err else '?'} | {(dbg or '').strip()}")
            if err is not None and err.message:
                reason = err.message
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
        if self._image_pipeline is None:
            return
        GLib.idle_add(self._image_capture_failed, reason)

    # ------------------------------------------------------------------
    # Video recording (Halium)
    # ------------------------------------------------------------------

    def _build_video_path(self) -> Path:
        self._video_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        # .mkv (Matroska) because we record MJPEG-in-Matroska — the
        # FuriOS-camera pattern that side-steps gst-droid's "Cannot
        # record video in raw mode" entirely (no vidsrc, no
        # start-capture). Matroska accepts MJPEG natively.
        #
        # Reserve the name via O_CREAT|O_EXCL placeholder, then close
        # immediately — filesink reopens-truncates by path. This closes
        # most of the TOCTOU window vs `path.exists()` followed by
        # filesink open; a symlink swap between placeholder-close and
        # filesink-open is still possible on world-writable dirs, but
        # the practical surface is now small.
        path = self._video_dir / f"{stamp}.mkv"
        i = 1
        for _attempt in range(1000):
            try:
                fd = _os.open(
                    path,
                    _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL,
                    0o644,
                )
                _os.close(fd)
                return path
            except FileExistsError:
                path = self._video_dir / f"{stamp}_{i}.mkv"
                i += 1
            except OSError:
                # Permission / disk error — fall through to the caller,
                # which will see filesink fail and surface a toast.
                return path
        return path

    def _start_video_recording(self) -> None:
        device = self._current_device() or {}
        if not _is_halium_device(device):
            self._show_toast(self._(
                "Video recording: only supported on Halium devices for now"
            ))
            return
        if self._recording or self._busy_capture:
            # _recording is set inside _video_pipeline_build (~100 ms
            # idle-deferred from here); _busy_capture catches the
            # in-flight window between this method's entry and the
            # pipeline actually coming up.
            return
        self._busy_capture = True
        self._shutter.set_sensitive(False)
        # Switch to a transient pipeline:
        #   droidcamsrc(mode=2) → vfsrc → gtk4paintablesink   (live preview)
        #                       → vidsrc → queue → h264parse → mp4mux
        #                                          → filesink
        # Same swap-trick as image capture, but here the pipeline keeps
        # running for the duration of the recording.
        cam_id = device.get("droidcam_id", 0)
        self._freeze_preview_frame()
        self._show_capture_spinner(True)
        _dlog(f"[yaga.camera] video-record: stopping preview for cam {cam_id}")
        self._stop_pipeline()
        GLib.idle_add(self._video_pipeline_build, cam_id)

    def _video_pipeline_build(self, cam_id: int) -> bool:
        gst = self._Gst

        # FuriOS-camera pattern: tee from vfsrc (NOT vidsrc) and record
        # MJPEG inside a Matroska container. This sidesteps gst-droid's
        # "Cannot record video in raw mode" error entirely because we
        # never touch vidsrc and never need start-capture. The recording
        # branch is just gst-pipeline elements that run for as long as
        # the pipeline is PLAYING; EOS finalises the MKV moov on stop.
        #
        # Pipeline:
        #   droidcamsrc(mode=2)
        #     ! tee name=t
        #     t. ! queue ! videoconvert ! gtk4paintablesink   (preview)
        #     t. ! queue ! videoconvert ! jpegenc ! mux.
        #     matroskamux name=mux ! filesink location=...
        pipeline = gst.Pipeline.new("yaga-video-record")
        src = gst.ElementFactory.make("droidcamsrc", "src")
        if src is None:
            return self._video_recording_failed("droidcamsrc unavailable")
        try:
            src.set_property("camera-device", cam_id)
            src.set_property("mode", 2)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
        pipeline.add(src)

        # Upstream videoconvert + tee — fan out vfsrc to the preview
        # and recording branches.
        up_convert = gst.ElementFactory.make("videoconvert", "up_convert")
        tee = gst.ElementFactory.make("tee", "t")
        if None in (up_convert, tee):
            return self._video_recording_failed("videoconvert/tee unavailable")
        pipeline.add(up_convert); pipeline.add(tee)
        vf_pad = src.get_static_pad("vfsrc")
        if vf_pad is None:
            try:
                vf_pad = src.request_pad_simple("vfsrc")
            except Exception:
                vf_pad = None
        if vf_pad is None:
            return self._video_recording_failed("vfsrc pad unavailable")
        if vf_pad.link(up_convert.get_static_pad("sink")) != gst.PadLinkReturn.OK:
            return self._video_recording_failed("vfsrc -> videoconvert link failed")
        if not up_convert.link(tee):
            return self._video_recording_failed("videoconvert -> tee link failed")

        # Preview branch: tee -> queue -> videoconvert -> sink.
        prev_queue = gst.ElementFactory.make("queue", "prev_queue")
        prev_convert = gst.ElementFactory.make("videoconvert", "prev_convert")
        prev_sink = gst.ElementFactory.make("gtk4paintablesink", "preview")
        if None in (prev_queue, prev_convert, prev_sink):
            return self._video_recording_failed("preview elements unavailable")
        try:
            prev_sink.set_property("sync", False)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
        prev_queue.set_property("leaky", 2)
        prev_queue.set_property("max-size-buffers", 2)
        pipeline.add(prev_queue); pipeline.add(prev_convert); pipeline.add(prev_sink)
        if not tee.link(prev_queue):
            return self._video_recording_failed("tee -> prev_queue link failed")
        prev_queue.link(prev_convert)
        prev_convert.link(prev_sink)

        # Recording branch: tee -> queue -> videoconvert -> jpegenc -> mux.
        # jpegenc quality is the user's Video-quality preset mapped from
        # kbps via _VIDEO_BITRATE_TO_QUALITY (rough but better than
        # nothing — jpegenc doesn't take a target bitrate).
        path = self._build_video_path()
        self._video_path = path
        kbps = max(2000, min(16000, int(self._video_bitrate_kbps)))
        jpeg_q = _VIDEO_BITRATE_TO_QUALITY.get(kbps, 85)
        rec_queue = gst.ElementFactory.make("queue", "rec_queue")
        rec_convert = gst.ElementFactory.make("videoconvert", "rec_convert")
        jpegenc = gst.ElementFactory.make("jpegenc", "rec_jpegenc")
        mkvmux = gst.ElementFactory.make("matroskamux", "mux")
        filesink = gst.ElementFactory.make("filesink", "filesink")
        if None in (rec_queue, rec_convert, jpegenc, mkvmux, filesink):
            return self._video_recording_failed(
                "video-record elements unavailable "
                "(need jpegenc + matroskamux + filesink)"
            )
        rec_queue.set_property("leaky", 2)
        rec_queue.set_property("max-size-buffers", 4)
        try:
            jpegenc.set_property("quality", jpeg_q)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
        filesink.set_property("location", str(path))
        filesink.set_property("sync", False)
        filesink.set_property("async", False)
        pipeline.add(rec_queue); pipeline.add(rec_convert)
        pipeline.add(jpegenc); pipeline.add(mkvmux); pipeline.add(filesink)
        if not tee.link(rec_queue):
            return self._video_recording_failed("tee -> rec_queue link failed")
        rec_queue.link(rec_convert)
        rec_convert.link(jpegenc)
        jpegenc.link(mkvmux)
        mkvmux.link(filesink)
        _dlog(f"[yaga.camera] video-record: MJPEG-in-Matroska, jpeg quality={jpeg_q}")

        self._video_pipeline = pipeline
        self._video_src = src
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_video_pipeline_error)
        bus.connect("message::eos", self._on_video_pipeline_eos)
        self._video_bus = bus

        # Hook the new gtk4paintablesink into the picture widget.
        try:
            paintable = prev_sink.get_property("paintable")
            if paintable is not None:
                self._picture.set_paintable(paintable)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)

        result = pipeline.set_state(gst.State.PLAYING)
        _dlog(f"[yaga.camera] video-record: pipeline PLAYING -> "
            f"{result.value_nick if result else '?'} (file={path})")

        # No start-capture needed — pipeline is recording the moment
        # it reaches PLAYING because the recording branch is inline.
        self._recording = True
        self._show_capture_spinner(False)
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        self._update_shutter_icon()
        self._start_record_blink()
        self._show_toast(self._("Recording…"))
        return False

    def _stop_video_recording(self) -> None:
        if not self._recording or self._video_pipeline is None:
            return
        if self._video_finalize_source is not None:
            # EOS already sent, finalize timeout armed — second tap is a
            # no-op until _video_finalize runs.
            return
        _dlog("[yaga.camera] video-record: stop requested")
        self._shutter.set_sensitive(False)
        self._show_capture_spinner(True)
        # Send EOS so matroskamux finalises the file (writes seek-
        # cues + closing tags). No stop-capture call: we never used
        # vidsrc/start-capture in the FuriOS pattern.
        try:
            self._video_pipeline.send_event(self._Gst.Event.new_eos())
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
        # Belt-and-braces timeout in case EOS doesn't land. Track the
        # source id so EOS arrival can cancel it — otherwise a second
        # recording started within 5 s would have the old timeout fire
        # against the NEW pipeline, calling _video_finalize on it.
        self._video_finalize_source = GLib.timeout_add_seconds(
            5, self._video_finalize_timeout
        )

    def _on_video_pipeline_eos(self, _bus: Any, _msg: Any) -> None:
        _dlog("[yaga.camera] video-record: EOS received, finalising file")
        self._cancel_video_finalize_timeout()
        self._video_finalize()

    def _cancel_video_finalize_timeout(self) -> None:
        if self._video_finalize_source is not None:
            try:
                GLib.source_remove(self._video_finalize_source)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._video_finalize_source = None

    def _video_finalize_timeout(self) -> bool:
        # The source id is consumed by GLib after this returns False; if
        # we were cancelled by EOS the source attribute is already None
        # and we never enter the body.
        self._video_finalize_source = None
        if self._video_pipeline is not None:
            _dlog("[yaga.camera] video-record: EOS timeout, finalising anyway")
            self._video_finalize()
        return False

    def _video_finalize(self) -> None:
        path = self._video_path
        self._video_teardown()
        self._recording = False
        self._stop_record_blink()
        self._show_capture_spinner(False)
        self._shutter.set_sensitive(True)
        self._update_shutter_icon()
        if path is not None and path.exists():
            self._show_toast(self._("Saved %s") % path.name)
            if self._on_captured is not None:
                try:
                    self._on_captured(path)
                except Exception:
                    LOGGER.debug("on_captured callback failed", exc_info=True)
        else:
            self._show_toast(self._("Recording failed"))
        # Bring the preview pipeline back.
        GLib.idle_add(self._start_pipeline)

    def _video_recording_failed(self, reason: str) -> bool:
        _dlog(f"[yaga.camera] video-record: {reason}")
        self._video_teardown()
        self._recording = False
        self._stop_record_blink()
        self._show_capture_spinner(False)
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        self._update_shutter_icon()
        self._show_toast(self._("Recording failed: %s") % reason)
        GLib.idle_add(self._start_pipeline)
        return False

    def _video_teardown(self) -> None:
        # Cancel any in-flight finalize timeout — without this, a failed
        # recording (or a fast stop→start sequence) would leave a stale
        # timer that fires against an unrelated future pipeline.
        self._cancel_video_finalize_timeout()
        if self._video_bus is not None:
            try:
                self._video_bus.remove_signal_watch()
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._video_bus = None
        if self._video_pipeline is not None:
            try:
                self._video_pipeline.set_state(self._Gst.State.NULL)
                self._video_pipeline.get_state(2 * self._Gst.SECOND)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._video_pipeline = None
        self._video_src = None
        self._video_path = None

    def _on_video_pipeline_error(self, _bus: Any, msg: Any) -> None:
        try:
            err, dbg = msg.parse_error()
            _dlog(f"[yaga.camera] video-record bus error: "
                f"{err.message if err else '?'} | {(dbg or '').strip()}")
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)

    def _write_sample(self, sample: Any) -> None:
        buf = sample.get_buffer() if sample is not None else None
        if buf is None:
            _dlog("[yaga.camera] capture: sample has no buffer")
            self._show_toast(self._("No frame available"))
            return
        success, mapinfo = buf.map(self._Gst.MapFlags.READ)
        if not success:
            _dlog("[yaga.camera] capture: buffer.map failed")
            self._show_toast(self._("Could not read frame"))
            return
        try:
            data = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        _dlog(f"[yaga.camera] capture: jpeg bytes={len(data)} save_dir={self._save_dir}")

        # Optional Pillow downscale to the user-picked target. Image
        # resolution picker on Halium sets _image_resolution; we keep
        # aspect ratio by fitting inside the target box (thumbnail()),
        # only downscaling (never upscaling).
        target = self._image_resolution
        if target is not None:
            try:
                from PIL import Image as PILImage
                import io
                src = PILImage.open(io.BytesIO(data))
                tw, th = target
                if src.width > tw or src.height > th:
                    src.thumbnail((tw, th), PILImage.LANCZOS)
                    buf_out = io.BytesIO()
                    src.save(
                        buf_out, format="JPEG",
                        quality=max(0, min(100, self._jpeg_quality)),
                    )
                    data = buf_out.getvalue()
                    _dlog(f"[yaga.camera] capture: downscaled to "
                        f"{src.width}x{src.height} ({len(data)} bytes)")
            except Exception as exc:
                _dlog(f"[yaga.camera] capture: downscale failed, keeping "
                    f"native ({exc})")

        try:
            self._save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _dlog(f"[yaga.camera] capture: mkdir {self._save_dir} failed: {exc}")
            self._show_toast(self._("Failed to save: %s") % exc)
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        # O_CREAT|O_EXCL atomically creates a new file or fails. Combined
        # with a per-i bump on EEXIST, this closes the TOCTOU window
        # between `path.exists()` and `path.write_bytes()` — on a shared
        # save-dir, another local user could otherwise win the race and
        # have us write into their symlink target.
        path = self._save_dir / f"{stamp}.jpg"
        i = 1
        fd = -1
        for _attempt in range(1000):
            try:
                fd = _os.open(
                    path,
                    _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL,
                    0o644,
                )
                break
            except FileExistsError:
                path = self._save_dir / f"{stamp}_{i}.jpg"
                i += 1
            except OSError as exc:
                _dlog(f"[yaga.camera] capture: open {path} failed: {exc}")
                self._show_toast(self._("Failed to save: %s") % exc)
                return
        if fd < 0:
            self._show_toast(self._("Failed to save: too many collisions"))
            return
        try:
            with _os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            _dlog(f"[yaga.camera] capture: write {path} failed: {exc}")
            self._show_toast(self._("Failed to save: %s") % exc)
            return
        _dlog(f"[yaga.camera] capture: SAVED {path}")

        self._write_exif(path)
        self._show_toast(self._("Saved %s") % path.name)
        if self._on_captured is not None:
            try:
                self._on_captured(path)
            except Exception:
                LOGGER.debug("on_captured callback failed", exc_info=True)

    def _write_exif(self, path: Path) -> None:
        # Prefer GExiv2 when it's available (proper Exiv2 backend, full
        # tag support). Fall back to Pillow when the GExiv2 GIR isn't
        # installed — covers the basic tags + GPS without requiring the
        # gir1.2-gexiv2-0.10 system package.
        if _HAS_GEXIV2:
            self._write_exif_gexiv2(path)
        else:
            self._write_exif_pillow(path)

    def _current_exif_basics(self) -> dict[str, Any]:
        """Common bits used by both EXIF backends."""
        device = self._current_device()
        model = (device.get("name") if device else None) or ""
        if model:
            model = re.sub(r"[^\x20-\x7e]+", " ", model).strip()[:64]
        return {
            "make": "Yaga",
            "model": model,
            "software": "Yaga",
            "now": time.strftime("%Y:%m:%d %H:%M:%S"),
            "orientation": {
                # 1 = top-left, 3 = bottom-right (180), 6 = right-top
                # (90 CW), 8 = left-bottom (90 CCW). Assumes sensor top
                # edge aligns with device top — true for most phone
                # modules.
                ORIENT_NORMAL:    1,
                ORIENT_BOTTOM_UP: 3,
                ORIENT_LEFT_UP:   6,
                ORIENT_RIGHT_UP:  8,
            }.get(self._device_orientation, 1),
        }

    def _write_exif_gexiv2(self, path: Path) -> None:
        basics = self._current_exif_basics()
        try:
            md = GExiv2.Metadata()  # type: ignore[union-attr]
            md.open_path(str(path))
            md.set_tag_string("Exif.Image.Make", basics["make"])
            if basics["model"]:
                md.set_tag_string("Exif.Image.Model", basics["model"])
            md.set_tag_string("Exif.Image.Software", basics["software"])
            md.set_tag_string("Exif.Image.DateTime", basics["now"])
            md.set_tag_string("Exif.Photo.DateTimeOriginal", basics["now"])
            md.set_tag_string("Exif.Photo.DateTimeDigitized", basics["now"])
            md.set_tag_string(
                "Exif.Image.Orientation", str(basics["orientation"])
            )
            if self._geo is not None:
                location = self._geo.latest()
                if location is not None:
                    try:
                        md.set_gps_info(
                            location["lon"], location["lat"],
                            location.get("alt", 0.0),
                        )
                        md.set_tag_string(
                            "Exif.GPSInfo.GPSProcessingMethod", "GeoClue"
                        )
                    except Exception:
                        LOGGER.debug("set_gps_info failed", exc_info=True)
            md.save_file(str(path))
        except Exception:
            LOGGER.debug("Could not write EXIF (GExiv2) for %s", path, exc_info=True)

    def _write_exif_pillow(self, path: Path) -> None:
        """Pillow-based EXIF writer used when GExiv2 isn't installed.
        Covers Make/Model/Software/DateTime/Orientation, plus GPS when
        the user has the geo toggle on and there's a fresh fix.

        Builds the EXIF blob via PIL.Image.Exif() (which doesn't
        require opening the source JPEG) and patches the file's APP1
        segment in place. Avoids the decode-then-re-encode cycle of
        Image.save("JPEG", quality=...), which lost ~5-10 quality
        points per save and stalled the UI 200-600 ms on a phone."""
        try:
            from PIL.Image import Exif
        except ImportError:
            return
        basics = self._current_exif_basics()
        try:
            exif = Exif()
            # 0th IFD (image-level metadata).
            exif[0x010F] = basics["make"]           # Make
            if basics["model"]:
                exif[0x0110] = basics["model"]      # Model
            exif[0x0131] = basics["software"]       # Software
            exif[0x0132] = basics["now"]            # DateTime
            exif[0x0112] = int(basics["orientation"])  # Orientation
            # Exif sub-IFD (Photo.* tags in Exiv2 vocabulary).
            exif_ifd = exif.get_ifd(0x8769)
            exif_ifd[0x9003] = basics["now"]        # DateTimeOriginal
            exif_ifd[0x9004] = basics["now"]        # DateTimeDigitized
            # GPS sub-IFD.
            if self._geo is not None:
                loc = self._geo.latest()
                if loc is not None:
                    gps = exif.get_ifd(0x8825)
                    self._pillow_set_gps(gps, loc)
            _write_exif_app1_inplace(path, exif.tobytes())
        except Exception:
            LOGGER.debug("Could not write EXIF (Pillow) for %s", path, exc_info=True)

    def _pillow_set_gps(self, gps_ifd: dict, location: dict) -> None:
        lat = location.get("lat")
        lon = location.get("lon")
        if lat is None or lon is None:
            return
        alt = location.get("alt", 0.0) or 0.0

        def to_dms(decimal: float) -> tuple:
            d = int(decimal)
            m_full = (decimal - d) * 60
            m = int(m_full)
            s = (m_full - m) * 60
            return (
                (d, 1),
                (m, 1),
                (int(round(s * 10000)), 10000),
            )

        gps_ifd[0x0000] = b"\x02\x02\x00\x00"        # GPSVersionID 2.2.0.0
        gps_ifd[0x0001] = "N" if lat >= 0 else "S"   # LatitudeRef
        gps_ifd[0x0002] = to_dms(abs(lat))           # Latitude
        gps_ifd[0x0003] = "E" if lon >= 0 else "W"   # LongitudeRef
        gps_ifd[0x0004] = to_dms(abs(lon))           # Longitude
        gps_ifd[0x0005] = 0 if alt >= 0 else 1       # AltitudeRef
        gps_ifd[0x0006] = (int(round(abs(alt) * 100)), 100)  # Altitude

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

    def _on_window_unmap(self, _win: Any) -> None:
        # Pipeline → PAUSED stops buffer-pool churn (the biggest single
        # battery cost while the window is hidden). We keep state alive
        # so re-mapping doesn't pay the full PIPELINE_STARTUP_MS again.
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(self._Gst.State.PAUSED)
            except Exception:
                LOGGER.debug("pipeline pause on unmap failed", exc_info=True)
        # Orientation: the sensord 10 Hz stream and D-Bus subscription
        # serve no purpose while we aren't drawing.
        if self._orientation is not None:
            try:
                self._orientation.stop()
            except Exception:
                LOGGER.debug("orientation stop on unmap failed", exc_info=True)
        # GeoClue: stop the location subscription. Resume on map only
        # when the user-intent flag is still set.
        if self._geo is not None:
            try:
                self._geo.stop()
            except Exception:
                LOGGER.debug("geo stop on unmap failed", exc_info=True)
            self._geo = None

    def _on_window_map(self, _win: Any) -> None:
        # Counterpart to _on_window_unmap. Wakes the pipeline + sensors
        # back up. First-map skipped because the constructor already
        # spun everything up.
        if self._pipeline is not None:
            try:
                _, state, _ = self._pipeline.get_state(0)
                if state == self._Gst.State.PAUSED:
                    self._pipeline.set_state(self._Gst.State.PLAYING)
            except Exception:
                LOGGER.debug("pipeline resume on map failed", exc_info=True)
        if self._orientation is not None:
            try:
                self._orientation.start(on_change=self._on_orientation_changed)
            except Exception:
                LOGGER.debug("orientation restart on map failed", exc_info=True)
        if self._geo_enabled and self._geo is None:
            self._try_start_geo_silent()

    def _on_close(self, _win: Any) -> bool:
        # Tear down ALL pipelines: the main preview AND any transient
        # capture/record pipeline that might still be running. Without
        # the latter, closing the window mid-recording leaves a stray
        # droidcamsrc holding the HAL.
        self._stop_pipeline()
        try:
            self._image_teardown()
        except Exception:
            LOGGER.debug("image-pipeline teardown failed", exc_info=True)
        try:
            self._video_teardown()
        except Exception:
            LOGGER.debug("video-pipeline teardown failed", exc_info=True)
        # GLib sources.
        for src_attr in (
            "_toast_timer", "_countdown_source", "_flash_source",
            "_focus_hide_source", "_record_dot_blink_id",
            "_swipe_hint_pulse_id", "_image_timeout_id",
            "_video_finalize_source",
        ):
            src_id = getattr(self, src_attr, None)
            if src_id is not None:
                try:
                    GLib.source_remove(src_id)
                except Exception:
                    LOGGER.debug("camera cleanup/op failed", exc_info=True)
                setattr(self, src_attr, None)
        # Flush any pending debounced settings write so values touched
        # within _PERSIST_DELAY_MS of close still make it to disk.
        if self._settings_persist_source is not None:
            try:
                GLib.source_remove(self._settings_persist_source)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._settings_persist_source = None
            self._persist_settings_flush()
        if self._geo is not None:
            try:
                self._geo.stop()
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._geo = None
        if self._orientation is not None:
            try:
                self._orientation.stop()
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
            self._orientation = None
        return False
