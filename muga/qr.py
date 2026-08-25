"""GStreamer-based QR code scanner — no zbar required."""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import memory_guard

LOGGER = logging.getLogger(__name__)

# --- Resource ceilings -------------------------------------------------
# A QR scan is a camera pipeline, and on a phone that is the most
# memory-hungry thing this app ever does. The scanner used to run
# `autovideosrc` with no caps filter at all: on Halium (FuriOS, Droidian,
# UBports) that resolves to droidcamsrc, which then negotiates the camera
# HAL's maximum mode — full sensor resolution at 30 fps. Buffers pile up in
# droidcamsrc's pool faster than zxing drains them, the phone runs out of
# RAM, and the OOM killer takes phosh down along with the app.
#
# So every branch is capped. The source ceiling matches the camera window's
# (_HALIUM_PREVIEW_CAP_* in camera.py, proven on the user's device); the
# decode branch is capped much harder because zxing reads a QR code fine
# from a 640px-wide frame, and a code held in front of the lens does not
# need thirty decode attempts per second.
_SRC_CAP_W = 1280
_SRC_CAP_H = 720
_PREVIEW_CAP_W = 1280
_PREVIEW_FPS = 15
_DECODE_CAP_W = 640
_DECODE_FPS = 8
_DECODE_FORMAT = "I420"

# Bounded to a single buffer, and explicitly *only* by buffer count: queue's
# byte and time limits default to 10 MB / 1 s, and one full-resolution frame
# exceeds the byte limit by itself, which would make the buffer count moot.
# leaky=downstream drops the oldest frame instead of stalling the source when
# zxing is slower than the camera — which on a phone it always is.
_QUEUE = "queue leaky=downstream max-size-buffers=1 max-size-bytes=0 max-size-time=0"

# How often the memory backstop samples /proc while a scan runs.
_MEM_CHECK_SECONDS = 1


class QRScanError(RuntimeError):
    pass


def _gst():
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise QRScanError("GStreamer Python bindings not found (python3-gst-1.0)") from exc
    Gst.init(None)
    return Gst


def _has(gst: Any, factory: str) -> bool:
    return gst.ElementFactory.find(factory) is not None


def scan_supported() -> bool:
    try:
        Gst = _gst()
    except QRScanError:
        return False
    return (
        (_has(Gst, "autovideosrc") or _has(Gst, "droidcamsrc"))
        and _has(Gst, "zxing")
    )


class WebcamQRScanner:
    """
    Scans a QR code from the webcam using GStreamer + the zxing element.
    Call build_widget() to get the GTK widget to embed, then start().
    on_success(text) is called on the GLib main loop when a code is found.
    on_error(message) is called on timeout, pipeline error, or when the
    memory backstop stops the scan.
    """

    # Class-level defaults so a partially-constructed scanner (tests, or a
    # failure inside __init__) still tears down cleanly.
    _mem_guard_id: int | None = None
    _mem_baseline_kb: int | None = None

    def __init__(
        self,
        on_success: Callable[[str], None],
        on_error: Callable[[str], None],
        timeout_seconds: int = 60,
    ) -> None:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk
        self._GLib = GLib
        self._Gtk = Gtk
        self._Gst = _gst()
        self.on_success = on_success
        self.on_error = on_error
        self.timeout_seconds = timeout_seconds
        self._pipeline: Any = None
        self._bus: Any = None
        self._timeout_id: int | None = None
        self._mem_guard_id = None
        self._mem_baseline_kb = None
        self._finished = False

        self._picture = Gtk.Picture()
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_can_shrink(True)

        self._status = Gtk.Label(label="Starting camera…", wrap=True, xalign=0.5)
        self._status.add_css_class("dim-label")

    def build_widget(self) -> Any:
        box = self._Gtk.Box(orientation=self._Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.append(self._picture)
        box.append(self._status)
        return box

    def start(self) -> None:
        self._finished = False
        self._stop_pipeline()
        # Don't add a camera pipeline on top of a system that is already
        # short on memory — that is exactly how the compositor gets killed.
        reason = memory_guard.pressure_reason()
        if reason is not None:
            self._fail(f"Camera not started — {reason}")
            return
        self._mem_baseline_kb = memory_guard.self_rss_kb()
        self._build_pipeline()
        if self._pipeline is None:
            return
        result = self._pipeline.set_state(self._Gst.State.PLAYING)
        if result == self._Gst.StateChangeReturn.FAILURE:
            self._fail("Could not start camera")
            return
        if self._timeout_id is not None:
            self._GLib.source_remove(self._timeout_id)
        self._timeout_id = self._GLib.timeout_add_seconds(
            self.timeout_seconds, self._on_timeout
        )
        self._mem_guard_id = self._GLib.timeout_add_seconds(
            _MEM_CHECK_SECONDS, self._on_memory_check
        )

    def cancel(self) -> None:
        self._finished = True
        self._stop_pipeline()

    # ------------------------------------------------------------------
    # Pipeline description
    # ------------------------------------------------------------------

    def _source_description(self) -> str | None:
        """The source element plus its caps ceiling, or None if there is no
        usable camera source on this system."""
        gst = self._Gst
        if _has(gst, "droidcamsrc"):
            # Halium/Hybris phone: the /dev/video* nodes are ISP and encoder
            # helpers, the real camera sits behind the Android HAL. mode=2
            # (video) keeps the viewfinder rolling without the per-frame
            # Photography reconfigure mode=1 does, and vfsrc is named
            # explicitly because droidcamsrc also exposes imgsrc/vidsrc —
            # leaving the choice to caps matching can land on the wrong pad.
            # Width/height only, no framerate: many Halium HALs advertise a
            # single discrete framerate and constraining it here stalls
            # negotiation at READY->PAUSED (see camera.py).
            return (
                "droidcamsrc mode=2 camera-device=0 name=src "
                f"src.vfsrc ! video/x-raw,width=(int)[1,{_SRC_CAP_W}],"
                f"height=(int)[1,{_SRC_CAP_H}]"
            )
        if not _has(gst, "autovideosrc"):
            return None
        # v4l2/PipeWire cameras negotiate a sane default mode on their own and
        # a caps filter here would break the ones that only offer a single
        # large raw mode, so the ceilings for this path live downstream as
        # videoscale/videorate stages instead.
        return "autovideosrc"

    def _scale_stage(self, width: int, fmt: str | None = None) -> str:
        """videoconvert, plus a videoscale ceiling when the plugin is around."""
        caps = "video/x-raw" + (f",format={fmt}" if fmt else "")
        if not _has(self._Gst, "videoscale"):
            return f"videoconvert ! {caps}" if fmt else "videoconvert"
        # The pixel-aspect-ratio has to be pinned. Left free, videoscale meets
        # a width ceiling by squashing the pixels instead of the frame: a 4K
        # feed comes out 640x2160 at par=6/1 — barely smaller, and distorted
        # past what zxing can read. Pinned to 1/1 the height follows the
        # width and 4K becomes 640x360. The width is a range rather than a
        # fixed value so an already-small feed is left alone rather than
        # upscaled into more pixels than the camera ever produced.
        caps += f",width=(int)[1,{width}],pixel-aspect-ratio=(fraction)1/1"
        return f"videoconvert ! videoscale ! {caps}"

    def _rate_stage(self, fps: int) -> str | None:
        if not _has(self._Gst, "videorate"):
            return None
        # drop-only: never duplicate frames to hit the target rate — a camera
        # feeding fewer frames than we asked for is fine, spending CPU to
        # invent copies of them is not.
        return f"videorate drop-only=true ! video/x-raw,framerate=(fraction)[1/1,{fps}/1]"

    def _preview_chain(self) -> str:
        stages = [_QUEUE]
        rate = self._rate_stage(_PREVIEW_FPS)
        if rate:
            stages.append(rate)
        stages.append(self._scale_stage(_PREVIEW_CAP_W))
        # sync=false: with the default sync=true the sink compares buffer
        # timestamps against the clock, drops whatever it considers late, and
        # logs a "buffers are being dropped / computer too slow" warning for
        # each one. On a phone that is a noisy, CPU-burning loop, and there is
        # no audio to sync against — we want the newest frame, not the one the
        # clock says is due.
        stages.append("gtk4paintablesink name=preview sync=false")
        return " ! ".join(stages)

    def _decode_chain(self) -> str:
        # Rate limiting goes first so the frames we throw away never get
        # converted or scaled.
        stages = [_QUEUE]
        rate = self._rate_stage(_DECODE_FPS)
        if rate:
            stages.append(rate)
        # I420 is the cheapest format zxing accepts (12 bits/pixel against
        # ARGB's 32) and it is what phone cameras hand out anyway, so
        # videoconvert usually gets to pass the buffer straight through.
        stages.append(self._scale_stage(_DECODE_CAP_W, fmt=_DECODE_FORMAT))
        stages.append("zxing message=true ! fakesink sync=false")
        return " ! ".join(stages)

    def _pipeline_description(self) -> str | None:
        source = self._source_description()
        if source is None:
            return None
        if not _has(self._Gst, "gtk4paintablesink"):
            return f"{source} ! {self._decode_chain()}"
        return (
            f"{source} ! tee name=t "
            f"t. ! {self._decode_chain()} "
            f"t. ! {self._preview_chain()}"
        )

    def _build_pipeline(self) -> None:
        Gst = self._Gst
        desc = self._pipeline_description()
        if desc is None:
            self._fail("No video device found (autovideosrc missing)")
            return
        if not _has(Gst, "zxing"):
            self._fail(
                "GStreamer zxing element missing.\n"
                "Install: apt install gstreamer1.0-plugins-bad"
            )
            return

        has_preview = _has(Gst, "gtk4paintablesink")
        LOGGER.debug("QR pipeline: %s", desc)
        try:
            self._pipeline = Gst.parse_launch(desc)
        except Exception as exc:
            self._fail(f"Pipeline error: {exc}")
            return

        self._bus = self._pipeline.get_bus()
        if self._bus is not None:
            self._bus.add_signal_watch()
            self._bus.connect("message", self._on_message)

        if has_preview:
            sink = self._pipeline.get_by_name("preview")
            if sink is not None:
                try:
                    paintable = sink.get_property("paintable")
                    if paintable is not None:
                        self._picture.set_paintable(paintable)
                        self._status.set_text("Hold QR code in front of camera")
                    else:
                        self._status.set_text("Camera preview not available")
                except Exception:
                    self._status.set_text("Could not bind camera preview")
        else:
            self._status.set_text("Camera active (no preview available)")

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------

    def _on_message(self, _bus: Any, message: Any) -> None:
        if self._finished:
            return
        Gst = self._Gst
        if message.type == Gst.MessageType.ERROR:
            err, _dbg = message.parse_error()
            self._fail(f"Camera error: {err}")
            return
        if message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is None or structure.get_name() != "barcode":
                return
            symbol = structure.get_value("symbol")
            if not symbol:
                return
            text = str(symbol).strip()
            if not text:
                return
            self._finished = True
            self._stop_pipeline()
            self.on_success(text)

    def _on_memory_check(self) -> bool:
        """Backstop for the caps ceilings above: if memory runs away anyway,
        tear the pipeline down before the OOM killer picks a victim."""
        if self._finished:
            self._mem_guard_id = None
            return False
        reason = memory_guard.pressure_reason(self._mem_baseline_kb)
        if reason is None:
            return True
        LOGGER.warning("QR scan stopped: %s", reason)
        # Cleared first so _stop_pipeline doesn't remove a source that is
        # about to return False and remove itself.
        self._mem_guard_id = None
        self._fail(f"Camera stopped to protect the phone — {reason}")
        return False

    def _on_timeout(self) -> bool:
        self._timeout_id = None
        if not self._finished:
            self._fail("Timeout — no QR code detected")
        return False

    def _fail(self, message: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop_pipeline()
        try:
            self._status.set_text(message)
        except Exception:
            LOGGER.debug("status update failed", exc_info=True)
        self.on_error(message)

    def _stop_pipeline(self) -> None:
        if self._timeout_id is not None:
            self._GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        if self._mem_guard_id is not None:
            self._GLib.source_remove(self._mem_guard_id)
            self._mem_guard_id = None
        if self._bus is not None:
            try:
                self._bus.remove_signal_watch()
            except Exception:
                LOGGER.debug("_bus.remove_signal_watch failed", exc_info=True)
            self._bus = None
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(self._Gst.State.NULL)
                # Wait for the transition to finish: the camera HAL only
                # releases its buffer pool once it has, and reopening the
                # scanner before then races the same device.
                self._pipeline.get_state(self._Gst.SECOND // 2)
            except Exception:
                LOGGER.debug("_pipeline.set_state failed", exc_info=True)
            self._pipeline = None
