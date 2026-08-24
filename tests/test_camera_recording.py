"""Tests for camera.py's Halium video path and its sample handlers.

The Halium recorder exists because the obvious approach does not work there:
gst-droid refuses ``start-capture`` on vidsrc with "Cannot record video in raw
mode", so Muga tees off the *viewfinder* pad instead and records MJPEG into
Matroska, which needs no start-capture at all.

The sample handlers are one-shot by construction. Both disconnect themselves
before handing off, because new-sample can fire again between accepting a
frame and the idle callback running — that produced duplicate saves.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.camera_fakes import FakeElement, FakeGst, FakePad, gst_win

camera = pytest.importorskip("muga.camera")
# The recording pipeline lives in its own module now.
camera_video = pytest.importorskip("muga.camera_video")


def _droid_pads(with_vfsrc=True):
    pads = {"src": FakePad("src")}
    if with_vfsrc:
        pads["vfsrc"] = FakePad("vfsrc")
    return {"droidcamsrc": pads}


def _rec_win(gst, tmp_path, **extra):
    defaults = dict(
        _video_recording_failed=MagicMock(return_value=False),
        _build_video_path=lambda: tmp_path / "clip.mkv",
        _video_bitrate_kbps=4000,
        _video_path=None, _video_pipeline=None, _video_src=None, _video_bus=None,
        _picture=MagicMock(), _shutter=MagicMock(),
        _on_video_pipeline_error=MagicMock(), _on_video_pipeline_eos=MagicMock(),
        _show_capture_spinner=MagicMock(), _update_shutter_icon=MagicMock(),
        _start_record_blink=MagicMock(), _apply_flash_to_pipeline=MagicMock(),
        _show_toast=MagicMock(), _recording=False, _busy_capture=True,
    )
    defaults.update(extra)
    return gst_win(gst, **defaults)


# ---------------------------------------------------------------------------
# Halium recorder
# ---------------------------------------------------------------------------

def test_halium_recorder_tees_off_the_viewfinder(tmp_path) -> None:
    """vfsrc, not vidsrc — touching vidsrc is what triggers gst-droid's
    "Cannot record video in raw mode"."""
    gst = FakeGst(element_pads=_droid_pads())
    win = _rec_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build(win, 0)

    vf = gst.element("droidcamsrc").get_static_pad("vfsrc")
    assert vf.linked_to is not None, "the viewfinder pad was never linked"
    assert "vidsrc" not in str(gst.element("droidcamsrc")._pads)


def test_halium_recorder_stays_in_video_mode(tmp_path) -> None:
    gst = FakeGst(element_pads=_droid_pads())
    camera.CameraWindow._video_pipeline_build(_rec_win(gst, tmp_path), 2)
    src = gst.element("droidcamsrc")
    assert src.props["mode"] == 2
    assert src.props["camera-device"] == 2


def test_halium_recorder_writes_mjpeg_into_matroska(tmp_path) -> None:
    gst = FakeGst(element_pads=_droid_pads())
    camera.CameraWindow._video_pipeline_build(_rec_win(gst, tmp_path), 0)
    factories = gst.pipelines[0].factories()
    assert "jpegenc" in factories
    assert "matroskamux" in factories
    assert gst.element("filesink").props["location"] == str(tmp_path / "clip.mkv")


def test_halium_recorder_records_as_soon_as_it_plays(tmp_path) -> None:
    """The record branch is inline, so PLAYING is the whole trigger — there is
    no start-capture to emit."""
    gst = FakeGst(element_pads=_droid_pads())
    win = _rec_win(gst, tmp_path)
    assert camera.CameraWindow._video_pipeline_build(win, 0) is False
    assert gst.pipelines[0].states == [FakeGst.State.PLAYING]
    assert win._recording is True
    assert win._busy_capture is False
    win._shutter.set_sensitive.assert_called_once_with(True)
    win._start_record_blink.assert_called_once()


def test_halium_recorder_reapplies_the_video_light(tmp_path) -> None:
    """Swapping pipelines drops the sysfs torch state unless it is re-applied."""
    gst = FakeGst(element_pads=_droid_pads())
    win = _rec_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build(win, 0)
    win._apply_flash_to_pipeline.assert_called_once()


def test_halium_recorder_hooks_eos_for_the_moov_atom(tmp_path) -> None:
    """EOS is what finalises the Matroska header; without the watch the file
    stays unplayable."""
    gst = FakeGst(element_pads=_droid_pads())
    win = _rec_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build(win, 0)
    assert "message::eos" in gst.pipelines[0].bus.signals
    assert "message::error" in gst.pipelines[0].bus.signals


def test_halium_recorder_requests_vfsrc_when_it_is_not_static(tmp_path) -> None:
    pads = {"droidcamsrc": {"src": FakePad("src"), "request:vfsrc": FakePad("vfsrc")}}
    gst = FakeGst(element_pads=pads)
    win = _rec_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build(win, 0)
    win._video_recording_failed.assert_not_called()


def test_halium_recorder_reports_a_missing_viewfinder(tmp_path) -> None:
    gst = FakeGst(element_pads={"droidcamsrc": {"src": FakePad("src")}})
    win = _rec_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build(win, 0)
    assert "vfsrc pad unavailable" in win._video_recording_failed.call_args[0][0]


def test_halium_recorder_reports_a_missing_source(tmp_path) -> None:
    gst = FakeGst(missing={"droidcamsrc"})
    win = _rec_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build(win, 0)
    assert "droidcamsrc unavailable" in win._video_recording_failed.call_args[0][0]


@pytest.mark.parametrize(
    ("missing", "fragment"),
    [
        ("tee", "videoconvert/tee"),
        ("gtk4paintablesink", "preview elements"),
        ("matroskamux", "video-record elements"),
        ("filesink", "video-record elements"),
    ],
)
def test_halium_recorder_names_what_is_missing(tmp_path, missing, fragment) -> None:
    gst = FakeGst(missing={missing}, element_pads=_droid_pads())
    win = _rec_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build(win, 0)
    assert fragment in win._video_recording_failed.call_args[0][0]


def test_halium_recorder_reports_a_refused_viewfinder_link(tmp_path) -> None:
    pads = {"droidcamsrc": {"vfsrc": FakePad("vfsrc", link_ok=False), "src": FakePad("src")}}
    gst = FakeGst(element_pads=pads)
    win = _rec_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build(win, 0)
    assert "link failed" in win._video_recording_failed.call_args[0][0]


def test_halium_recorder_maps_bitrate_to_quality(tmp_path) -> None:
    gst = FakeGst(element_pads=_droid_pads())
    kbps, quality = next(iter(camera_video._VIDEO_BITRATE_TO_QUALITY.items()))
    win = _rec_win(gst, tmp_path, _video_bitrate_kbps=kbps)
    camera.CameraWindow._video_pipeline_build(win, 0)
    assert gst.element("jpegenc").props["quality"] == quality


def test_halium_recorder_swaps_the_preview_paintable(tmp_path) -> None:
    gst = FakeGst(element_pads=_droid_pads(),
                  preset_props={"gtk4paintablesink": {"paintable": "new-paintable"}})
    win = _rec_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build(win, 0)
    win._picture.set_paintable.assert_called_once_with("new-paintable")


# ---------------------------------------------------------------------------
# Capture sample handler
# ---------------------------------------------------------------------------

class _Structure:
    def __init__(self, width: int) -> None:
        self._width = width

    def get_int(self, name):
        return (True, self._width) if name == "width" else (False, 0)


class _Caps:
    def __init__(self, width: int) -> None:
        self._width = width

    def get_size(self):
        return 1

    def get_structure(self, _i):
        return _Structure(self._width)


class _Sample:
    def __init__(self, width: int = 4000) -> None:
        self._caps = _Caps(width)

    def get_caps(self):
        return self._caps


class _Sink(FakeElement):
    def __init__(self, sample=None, raises=False) -> None:
        super().__init__("appsink", "snap")
        self._sample = sample
        self._raises = raises

    def emit(self, signal):
        if self._raises:
            raise RuntimeError("pull-sample failed")
        return self._sample


def _sample_win(gst, **extra):
    defaults = dict(
        _capture_signal_id=11,
        _capture_signal_sink=None,
        _capture_min_width=0,
        _finish_capture=MagicMock(),
    )
    defaults.update(extra)
    return gst_win(gst, **defaults)


def test_capture_sample_hands_the_frame_off_once() -> None:
    gst = FakeGst()
    sample = _Sample()
    sink = _Sink(sample)
    win = _sample_win(gst, _capture_signal_sink=sink)

    with patch.object(camera.GLib, "idle_add") as idle:
        assert camera.CameraWindow._on_capture_sample(win, sink) == FakeGst.FlowReturn.OK

    idle.assert_called_once_with(win._finish_capture, sample)
    assert win._capture_signal_id is None, "handler not cleared"
    assert sink.disconnected == [11], "handler not disconnected"


def test_capture_sample_ignores_a_second_frame() -> None:
    """new-sample can fire again before the idle callback runs; without this
    guard the photo is saved twice."""
    gst = FakeGst()
    win = _sample_win(gst, _capture_signal_id=None)
    with patch.object(camera.GLib, "idle_add") as idle:
        assert camera.CameraWindow._on_capture_sample(win, _Sink(_Sample())) == FakeGst.FlowReturn.OK
    idle.assert_not_called()


def test_capture_sample_tolerates_a_failing_pull() -> None:
    gst = FakeGst()
    win = _sample_win(gst)
    with patch.object(camera.GLib, "idle_add") as idle:
        camera.CameraWindow._on_capture_sample(win, _Sink(raises=True))
    idle.assert_not_called()
    assert win._capture_signal_id == 11, "the handler must stay armed for a retry"


def test_capture_sample_skips_frames_from_before_the_caps_swap() -> None:
    """The 720p cap is lifted for the shot; in-flight low-res frames must not
    become the saved photo."""
    gst = FakeGst()
    sink = _Sink(_Sample(width=1280))
    win = _sample_win(gst, _capture_signal_sink=sink, _capture_min_width=1281)

    with patch.object(camera.GLib, "idle_add") as idle:
        camera.CameraWindow._on_capture_sample(win, sink)

    idle.assert_not_called()
    assert win._capture_min_width == 1281, "still waiting for a high-res frame"
    assert win._capture_signal_id == 11


def test_capture_sample_accepts_the_first_high_res_frame() -> None:
    gst = FakeGst()
    sink = _Sink(_Sample(width=4000))
    win = _sample_win(gst, _capture_signal_sink=sink, _capture_min_width=1281)

    with patch.object(camera.GLib, "idle_add") as idle:
        camera.CameraWindow._on_capture_sample(win, sink)

    idle.assert_called_once()
    assert win._capture_min_width == 0, "the filter must switch off after a hit"


def test_capture_sample_survives_a_raising_disconnect() -> None:
    gst = FakeGst()
    sink = _Sink(_Sample())
    sink.disconnect = MagicMock(side_effect=RuntimeError("gone"))
    win = _sample_win(gst, _capture_signal_sink=sink)
    with patch.object(camera.GLib, "idle_add") as idle:
        camera.CameraWindow._on_capture_sample(win, sink)
    idle.assert_called_once(), "the frame was dropped because cleanup failed"


# ---------------------------------------------------------------------------
# Finishing and timing out
# ---------------------------------------------------------------------------

def _finish_win(**extra):
    defaults = dict(
        _=lambda s: s,
        _close_valve_and_disconnect=MagicMock(),
        _write_sample=MagicMock(),
        _busy_capture=True,
        _closing=False,
        _show_toast=MagicMock(),
        _shutter=MagicMock(),
        _capture_timeout_id=42,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def test_finish_capture_writes_and_unlocks() -> None:
    win = _finish_win()
    sample = object()
    assert camera.CameraWindow._finish_capture(win, sample) is False
    win._write_sample.assert_called_once_with(sample)
    assert win._busy_capture is False
    win._shutter.set_sensitive.assert_called_once_with(True)


def test_finish_capture_saves_a_shot_taken_while_closing() -> None:
    """A photo already in flight when the window closes is still the user's
    photo; only the widget updates are skipped."""
    win = _finish_win(_closing=True)
    sample = object()
    camera.CameraWindow._finish_capture(win, sample)
    win._write_sample.assert_called_once_with(sample)
    win._shutter.set_sensitive.assert_not_called()


def test_finish_capture_reports_an_empty_result() -> None:
    win = _finish_win()
    camera.CameraWindow._finish_capture(win, None)
    win._write_sample.assert_not_called()
    win._show_toast.assert_called_once_with("No frame available")
    assert win._busy_capture is False


def test_capture_timeout_resets_everything() -> None:
    win = _finish_win()
    assert camera.CameraWindow._on_capture_timeout(win) is False
    assert win._capture_timeout_id is None
    win._close_valve_and_disconnect.assert_called_once()
    assert win._busy_capture is False
    win._shutter.set_sensitive.assert_called_once_with(True)
    win._show_toast.assert_called_once_with("No frame available")
