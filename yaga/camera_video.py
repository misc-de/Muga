"""Video recording: its own GStreamer pipeline, separate from the preview.

Split out of ``CameraWindow``. Recording cannot reuse the preview pipeline —
it needs an encoder, a muxer and a file sink — so it builds a second one and
runs it alongside. Two builders exist because the hardware differs:

* ``_video_pipeline_build`` — the Halium / gst-droid path, where droidcamsrc
  owns the sensor and hands out an encoded stream
* ``_video_pipeline_build_generic`` — v4l2 webcams, encoded in software

Stopping is the delicate half. An MP4 is only playable once the muxer has
written its index, so the pipeline gets an EOS and a bounded window to
finalise; ``_video_finalize_timeout`` is what keeps a wedged encoder from
leaving the UI stuck on "recording" forever.
"""

from __future__ import annotations

import os as _os
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from .camera_debug import LOGGER, dlog as _dlog
from .camera_devices import _is_halium_device

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


class CameraVideoMixin:
    """The recording pipeline and its lifecycle. Mixed into CameraWindow.

    The block below is the contract with the host class: every name is created
    in ``CameraWindow.__init__`` (or defined on it) and only annotated here.
    """

    _Gst: Any
    _video_dir: Path
    _video_path: Path | None
    _video_pipeline: Any
    _video_src: Any
    _video_bus: Any
    _video_bitrate_kbps: int
    _video_finalize_source: int | None
    _recording: bool
    _busy_capture: bool
    _device_orientation: str
    _selected_resolution: tuple[int, int] | None
    _picture: Gtk.Picture
    _shutter: Gtk.Widget
    _record_dot: Gtk.Widget
    _record_dot_blink_id: int | None
    _on_captured: Any
    # Bound in CameraWindow.__init__ as an attribute, not a method.
    _: Any

    if TYPE_CHECKING:
        # Provided by CameraWindow; no runtime definition, so these can never
        # shadow the real methods.
        def _current_device(self) -> dict[str, Any] | None: ...
        def _selected_format_kind(self, device: dict[str, Any]) -> str: ...
        def _make_source_element(self, device: dict[str, Any]) -> Any: ...
        def _source_output_pad(self, source: Any) -> Any: ...
        def _start_pipeline(self) -> bool: ...
        def _stop_pipeline(self) -> None: ...
        def _update_shutter_icon(self) -> None: ...
        def _apply_flash_to_pipeline(self) -> None: ...
        def _show_capture_spinner(self, visible: bool) -> None: ...
        def _start_record_blink(self) -> None: ...
        def _stop_record_blink(self) -> None: ...
        def _freeze_preview_frame(self) -> None: ...
        def _show_toast(self, text: str, sticky: bool = False) -> None: ...

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
        _dlog(f"[yaga.camera] video-record: stopping preview for {device.get('name', cam_id)}")
        self._stop_pipeline()
        if _is_halium_device(device):
            GLib.idle_add(self._video_pipeline_build, cam_id)
        else:
            GLib.idle_add(self._video_pipeline_build_generic, device)

    def _video_pipeline_build_generic(self, device: dict[str, Any]) -> bool:
        gst = self._Gst

        pipeline = gst.Pipeline.new("yaga-video-record-generic")
        src = self._make_source_element(device)
        if src is None:
            return self._video_recording_failed("camera source unavailable")

        path = self._build_video_path()
        self._video_path = path
        kbps = max(2000, min(16000, int(self._video_bitrate_kbps)))
        jpeg_q = _VIDEO_BITRATE_TO_QUALITY.get(kbps, 85)

        capsfilter = None
        jpegdec = None
        if self._selected_resolution is not None:
            sel_w, sel_h = self._selected_resolution
            kind = self._selected_format_kind(device)
            capsfilter = gst.ElementFactory.make("capsfilter", "resfilter")
            if capsfilter is None:
                return self._video_recording_failed("capsfilter unavailable")
            if kind == "jpeg":
                capsfilter.set_property(
                    "caps", gst.Caps.from_string(
                        f"image/jpeg,width={sel_w},height={sel_h}"
                    )
                )
                if gst.ElementFactory.find("jpegdec") is not None:
                    jpegdec = gst.ElementFactory.make("jpegdec", "jpegdec")
                if jpegdec is None:
                    return self._video_recording_failed("jpegdec unavailable")
            else:
                capsfilter.set_property(
                    "caps", gst.Caps.from_string(
                        f"video/x-raw,width={sel_w},height={sel_h}"
                    )
                )

        up_convert = gst.ElementFactory.make("videoconvert", "up_convert")
        tee = gst.ElementFactory.make("tee", "t")
        prev_queue = gst.ElementFactory.make("queue", "prev_queue")
        prev_convert = gst.ElementFactory.make("videoconvert", "prev_convert")
        prev_sink = gst.ElementFactory.make("gtk4paintablesink", "preview")
        rec_queue = gst.ElementFactory.make("queue", "rec_queue")
        rec_convert = gst.ElementFactory.make("videoconvert", "rec_convert")
        jpegenc = gst.ElementFactory.make("jpegenc", "rec_jpegenc")
        mkvmux = gst.ElementFactory.make("matroskamux", "mux")
        filesink = gst.ElementFactory.make("filesink", "filesink")
        if None in (
            up_convert, tee, prev_queue, prev_convert, prev_sink,
            rec_queue, rec_convert, jpegenc, mkvmux, filesink,
        ):
            return self._video_recording_failed(
                "video-record elements unavailable "
                "(need gtk4paintablesink + jpegenc + matroskamux + filesink)"
            )

        for el in (src, capsfilter, jpegdec, up_convert, tee,
                   prev_queue, prev_convert, prev_sink,
                   rec_queue, rec_convert, jpegenc, mkvmux, filesink):
            if el is not None:
                pipeline.add(el)

        first = capsfilter or jpegdec or up_convert
        src_pad = self._source_output_pad(src)
        sink_pad = first.get_static_pad("sink")
        if src_pad is None or sink_pad is None:
            return self._video_recording_failed("source link pads unavailable")
        if src_pad.link(sink_pad) != gst.PadLinkReturn.OK:
            return self._video_recording_failed("source -> record pipeline link failed")

        chain = [el for el in (capsfilter, jpegdec, up_convert, tee) if el is not None]
        for a, b in zip(chain, chain[1:]):
            if not a.link(b):
                return self._video_recording_failed("record pipeline link failed")

        try:
            prev_sink.set_property("sync", False)
            jpegenc.set_property("quality", jpeg_q)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)
        prev_queue.set_property("leaky", 2)
        prev_queue.set_property("max-size-buffers", 2)
        rec_queue.set_property("leaky", 2)
        rec_queue.set_property("max-size-buffers", 4)
        filesink.set_property("location", str(path))
        filesink.set_property("sync", False)
        filesink.set_property("async", False)

        if not tee.link(prev_queue):
            return self._video_recording_failed("tee -> prev_queue link failed")
        prev_queue.link(prev_convert)
        prev_convert.link(prev_sink)
        if not tee.link(rec_queue):
            return self._video_recording_failed("tee -> rec_queue link failed")
        rec_queue.link(rec_convert)
        rec_convert.link(jpegenc)
        jpegenc.link(mkvmux)
        mkvmux.link(filesink)

        self._video_pipeline = pipeline
        self._video_src = src
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_video_pipeline_error)
        bus.connect("message::eos", self._on_video_pipeline_eos)
        self._video_bus = bus

        try:
            paintable = prev_sink.get_property("paintable")
            if paintable is not None:
                self._picture.set_paintable(paintable)
        except Exception:
            LOGGER.debug("camera cleanup/op failed", exc_info=True)

        result = pipeline.set_state(gst.State.PLAYING)
        if result == gst.StateChangeReturn.FAILURE:
            return self._video_recording_failed("could not start video pipeline")
        _dlog(f"[yaga.camera] video-record: generic MJPEG-in-Matroska, "
              f"jpeg quality={jpeg_q}, file={path}")

        self._recording = True
        self._show_capture_spinner(False)
        self._busy_capture = False
        self._shutter.set_sensitive(True)
        self._update_shutter_icon()
        self._start_record_blink()
        self._apply_flash_to_pipeline()
        self._show_toast(self._("Recording…"))
        return False

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
        # Re-apply the video light toggle after swapping pipelines so
        # the sysfs torch remains in the requested state.
        self._apply_flash_to_pipeline()
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
        # Re-apply the user's video-light setting after the transient
        # recording pipeline is torn down; the preview rebuild that
        # follows may not happen instantly.
        self._apply_flash_to_pipeline()
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
        self._apply_flash_to_pipeline()
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
                self._video_pipeline.get_state(self._Gst.SECOND // 2)  # 500ms cap: HAL release is normally ms; don't freeze the UI for 2s
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
