"""Tests for camera.py's GStreamer pipeline construction.

The pipeline builders carry most of the hard-won device knowledge in this
file — droidcamsrc mode 1 vs 2, why the snap branch sits behind a valve, why
the image-capture appsink needs async=false, which pad name a Halium source
exposes. None of it was covered, and none of it is obvious enough to survive
a refactor unaided.

Every builder reaches GStreamer exclusively through ``self._Gst``, so a fake
module is enough to drive them: elements are recording mocks, so the tests
can assert on the properties and links that were actually requested, and
``FakeGst(missing=...)`` makes any element unavailable to exercise the
failure ladders.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.camera_fakes import FakeElement, FakeGst, FakePad, FakePipeline, bind

camera = pytest.importorskip("yaga.camera")


def _win(gst: FakeGst, **attrs) -> SimpleNamespace:
    attrs.setdefault("_", lambda s: s)
    attrs["_Gst"] = gst
    return SimpleNamespace(**attrs)


# ---------------------------------------------------------------------------
# Source-element fallback ladder
# ---------------------------------------------------------------------------

def test_halium_device_gets_droidcamsrc_in_video_mode() -> None:
    """mode=2 is deliberate: mode=1 makes the HAL reconfigure per frame and
    the preview stops updating after the first one."""
    gst = FakeGst()
    win = _win(gst)
    src = camera.CameraWindow._make_droidcam_source(win, {"droidcam_id": 1})
    assert src.factory == "droidcamsrc"
    assert src.props["mode"] == 2
    assert src.props["camera-device"] == 1


def test_droidcam_source_defaults_the_camera_id() -> None:
    gst = FakeGst()
    src = camera.CameraWindow._make_droidcam_source(_win(gst), {})
    assert src.props["camera-device"] == 0


def test_droidcam_source_is_none_when_the_element_is_missing() -> None:
    gst = FakeGst(missing={"droidcamsrc"})
    assert camera.CameraWindow._make_droidcam_source(_win(gst), {}) is None


def test_gst_device_source_is_preferred_over_raw_v4l2() -> None:
    """PipeWire can hold an exclusive lock on /dev/videoN, so the device's own
    create_element (which yields pipewiresrc) has to win."""
    gst_device = MagicMock()
    gst_device.create_element.return_value = "pipewire-element"
    win = _win(FakeGst())
    out = camera.CameraWindow._make_gst_device_source(win, {"gst_device": gst_device})
    assert out == "pipewire-element"
    gst_device.create_element.assert_called_once_with("src")


def test_gst_device_source_handles_a_raising_device() -> None:
    gst_device = MagicMock()
    gst_device.create_element.side_effect = RuntimeError("no")
    win = _win(FakeGst())
    assert camera.CameraWindow._make_gst_device_source(win, {"gst_device": gst_device}) is None


def test_gst_device_source_without_a_device() -> None:
    assert camera.CameraWindow._make_gst_device_source(_win(FakeGst()), {}) is None


def test_v4l2_source_sets_the_device_path() -> None:
    gst = FakeGst()
    src = camera.CameraWindow._make_v4l2_source(_win(gst), {"path": "/dev/video2"})
    assert src.factory == "v4l2src"
    assert src.props["device"] == "/dev/video2"


def test_v4l2_source_needs_a_path() -> None:
    assert camera.CameraWindow._make_v4l2_source(_win(FakeGst()), {"path": ""}) is None


def test_v4l2_source_needs_the_element_installed() -> None:
    gst = FakeGst(absent_factories={"v4l2src"})
    assert camera.CameraWindow._make_v4l2_source(_win(gst), {"path": "/dev/video0"}) is None


def test_autovideo_source_is_the_last_resort() -> None:
    gst = FakeGst()
    assert camera.CameraWindow._make_autovideo_source(_win(gst)).factory == "autovideosrc"
    gst2 = FakeGst(absent_factories={"autovideosrc"})
    assert camera.CameraWindow._make_autovideo_source(_win(gst2)) is None


def _ladder_win(gst):
    """_make_source_element dispatches to the four helpers on self, so bind
    the real ones — the point of these tests is the ladder, not the rungs."""
    return bind(_win(gst), camera.CameraWindow,
                "_make_droidcam_source", "_make_gst_device_source",
                "_make_v4l2_source", "_make_autovideo_source")


def test_source_ladder_walks_all_the_way_down() -> None:
    """No droidcam tag, no gst_device, no path — must still reach autovideosrc."""
    src = camera.CameraWindow._make_source_element(_ladder_win(FakeGst()), {})
    assert src.factory == "autovideosrc"


def test_source_ladder_stops_at_the_first_success() -> None:
    gst_device = MagicMock()
    gst_device.create_element.return_value = "from-device"
    out = camera.CameraWindow._make_source_element(
        _ladder_win(FakeGst()), {"gst_device": gst_device, "path": "/dev/video0"},
    )
    assert out == "from-device", "the v4l2 fallback ran despite a usable device"


def test_source_ladder_prefers_droidcamsrc_on_halium() -> None:
    gst_device = MagicMock()
    gst_device.create_element.return_value = "from-device"
    out = camera.CameraWindow._make_source_element(
        _ladder_win(FakeGst()),
        {"source_factory": "droidcamsrc", "gst_device": gst_device},
    )
    assert getattr(out, "factory", None) == "droidcamsrc"


def test_source_ladder_falls_past_a_broken_droidcamsrc() -> None:
    """Tagged Halium but the element is not installed — must not dead-end."""
    src = camera.CameraWindow._make_source_element(
        _ladder_win(FakeGst(missing={"droidcamsrc"})),
        {"source_factory": "droidcamsrc", "path": "/dev/video0"},
    )
    assert src.factory == "v4l2src"


# ---------------------------------------------------------------------------
# Output-pad selection
# ---------------------------------------------------------------------------

def test_output_pad_prefers_the_droidcam_viewfinder() -> None:
    vf = FakePad("vfsrc")
    src = FakeElement("droidcamsrc", "src", pads={"vfsrc": vf, "src": FakePad("src")})
    assert camera.CameraWindow._source_output_pad(_win(FakeGst()), src) is vf


def test_output_pad_requests_vfsrc_when_it_is_only_a_template() -> None:
    requested = FakePad("vfsrc")
    src = FakeElement("droidcamsrc", "src",
                      pads={"src": FakePad("src"), "request:vfsrc": requested})
    src.get_pad_template_list = lambda: [SimpleNamespace(name_template="vfsrc")]
    assert camera.CameraWindow._source_output_pad(_win(FakeGst()), src) is requested


def test_output_pad_falls_back_to_the_static_src() -> None:
    static = FakePad("src")
    src = FakeElement("v4l2src", "src", pads={"src": static})
    assert camera.CameraWindow._source_output_pad(_win(FakeGst()), src) is static


# ---------------------------------------------------------------------------
# Preview / snapshot description
# ---------------------------------------------------------------------------

def _downstream(gst, **extra):
    win = _win(gst, _jpeg_quality=extra.pop("quality", 92), **extra)
    return camera.CameraWindow._build_downstream_description(win, extra.pop("device", {}))


def test_downstream_uses_the_gtk_sink_when_available() -> None:
    out = _downstream(FakeGst())
    assert "gtk4paintablesink name=preview" in out
    assert "sync=false" in out, "a syncing preview sink drops frames and spams warnings"


def test_downstream_falls_back_to_appsink_preview() -> None:
    out = _downstream(FakeGst(absent_factories={"gtk4paintablesink"}))
    assert "appsink name=preview_sink" in out
    assert "format=RGBA" in out


def test_downstream_falls_back_to_fakesink() -> None:
    gst = FakeGst(absent_factories={"gtk4paintablesink", "appsink"})
    out = _downstream(gst)
    assert "fakesink" in out
    assert "name=preview" not in out


def test_downstream_gates_the_snapshot_branch_behind_a_valve() -> None:
    """jpegenc on every frame at 30 fps piles up memory and OOMs a phone."""
    out = _downstream(FakeGst())
    assert "valve name=shutter drop=true" in out
    assert "jpegenc name=snap_jpeg" in out
    assert "async=false" in out, "an async snap appsink stalls the state change"


def test_downstream_carries_the_jpeg_quality() -> None:
    assert "quality=61" in _downstream(FakeGst(), quality=61)


@pytest.mark.parametrize(("value", "expected"), [(-5, 0), (0, 0), (100, 100), (250, 100)])
def test_downstream_clamps_the_jpeg_quality(value, expected) -> None:
    assert f"quality={expected}" in _downstream(FakeGst(), quality=value)


def _snap_branch(description: str) -> str:
    """The tee branch that ends at the snap appsink, whitespace-normalised."""
    branch = [b for b in description.split("t. !") if "name=snap" in b]
    assert branch, "no snapshot branch in the description"
    return " ".join(branch[0].split())


def test_downstream_puts_the_valve_before_the_queue_on_halium() -> None:
    """Upstream of the valve the queue would always hold two source-pool
    buffers, adding pressure on droidcamsrc's pool even while idle."""
    branch = _snap_branch(_downstream(FakeGst(), device={"source_factory": "droidcamsrc"}))
    assert branch.index("valve name=shutter") < branch.index("queue leaky=downstream")


def test_downstream_puts_the_queue_before_the_valve_elsewhere() -> None:
    branch = _snap_branch(_downstream(FakeGst(), device={"source_factory": "v4l2src"}))
    assert branch.index("queue leaky=downstream") < branch.index("valve name=shutter")


def test_downstream_omits_the_snapshot_branch_without_an_encoder() -> None:
    out = _downstream(FakeGst(absent_factories={"jpegenc"}))
    assert "valve" not in out
    assert "tee name=t" in out, "the tee must still be there for the preview"


# ---------------------------------------------------------------------------
# Still-capture pipeline (Halium image mode)
# ---------------------------------------------------------------------------

def _image_win(gst, **extra):
    defaults = dict(
        _image_capture_failed=MagicMock(return_value=False),
        _on_image_capture_sample=MagicMock(),
        _on_image_pipeline_error=MagicMock(),
        _emit_start_capture_async=MagicMock(),
        _on_image_capture_timeout=MagicMock(),
        _image_pipeline=None, _image_src=None, _image_signal_id=None,
        _image_bus=None, _image_timeout_id=None,
    )
    defaults.update(extra)
    return _win(gst, **defaults)


def _droid_pads():
    return {"droidcamsrc": {"vfsrc": FakePad("vfsrc"), "imgsrc": FakePad("imgsrc")}}


def test_image_pipeline_uses_still_capture_mode() -> None:
    """mode=1 is the whole point here: mode=2 makes start-capture try to
    record video ("cannot record video in raw mode")."""
    gst = FakeGst(element_pads=_droid_pads())
    win = _image_win(gst)
    camera.CameraWindow._image_pipeline_build(win, 3)

    src = gst.element("droidcamsrc")
    assert src.props["mode"] == 1
    assert src.props["camera-device"] == 3


def test_image_pipeline_keeps_a_viewfinder_alive() -> None:
    """droidcamsrc's HAL setup expects a live vfsrc even when only imgsrc is
    wanted, so a fakesink is wired to it."""
    gst = FakeGst(element_pads=_droid_pads())
    camera.CameraWindow._image_pipeline_build(_image_win(gst), 0)
    assert "fakesink" in gst.made_factories()
    fake = gst.element("fakesink")
    assert fake.props == {"sync": False, "async": False}


def test_image_pipeline_pins_jpeg_caps_on_the_sink() -> None:
    """Without pinned caps gst-droid asserts on a missing buffer pool when
    start-capture fires before negotiation propagates."""
    gst = FakeGst(element_pads=_droid_pads())
    camera.CameraWindow._image_pipeline_build(_image_win(gst), 0)
    sink = gst.element("appsink")
    assert sink.props["caps"] == "caps:image/jpeg"
    assert sink.props["async"] is False
    assert sink.props["drop"] is True


def test_image_pipeline_starts_and_arms_a_timeout() -> None:
    gst = FakeGst(element_pads=_droid_pads())
    win = _image_win(gst)
    assert camera.CameraWindow._image_pipeline_build(win, 0) is False  # one-shot idle
    assert gst.pipelines[0].states == [FakeGst.State.PLAYING]
    win._emit_start_capture_async.assert_called_once()
    assert win._emit_start_capture_async.call_args.kwargs["delay_ms"] == 500
    assert win._image_timeout_id is not None


def test_image_pipeline_reports_a_missing_source() -> None:
    gst = FakeGst(missing={"droidcamsrc"})
    win = _image_win(gst)
    camera.CameraWindow._image_pipeline_build(win, 0)
    win._image_capture_failed.assert_called_once()
    assert "droidcamsrc" in win._image_capture_failed.call_args[0][0]


def test_image_pipeline_reports_a_missing_sink() -> None:
    gst = FakeGst(missing={"appsink"}, element_pads=_droid_pads())
    win = _image_win(gst)
    camera.CameraWindow._image_pipeline_build(win, 0)
    assert "queue/appsink" in win._image_capture_failed.call_args[0][0]


def test_image_pipeline_reports_a_missing_imgsrc_pad() -> None:
    """Some droidcamsrc builds expose no imgsrc at all — that has to surface
    as a capture failure, not a silent no-op."""
    gst = FakeGst(element_pads={"droidcamsrc": {"vfsrc": FakePad("vfsrc")}})
    win = _image_win(gst)
    camera.CameraWindow._image_pipeline_build(win, 0)
    assert "imgsrc pad" in win._image_capture_failed.call_args[0][0]
    assert gst.pipelines[0].states == [FakeGst.State.NULL], "pipeline left running"


def test_image_pipeline_reports_a_refused_link() -> None:
    gst = FakeGst(element_pads={
        "droidcamsrc": {"vfsrc": FakePad("vfsrc"), "imgsrc": FakePad("imgsrc", link_ok=False)},
    })
    win = _image_win(gst)
    camera.CameraWindow._image_pipeline_build(win, 0)
    assert "link failed" in win._image_capture_failed.call_args[0][0]
    assert gst.pipelines[0].states == [FakeGst.State.NULL]


# ---------------------------------------------------------------------------
# Video-recording pipeline (generic MJPEG-in-Matroska)
# ---------------------------------------------------------------------------

def _video_win(gst, tmp_path, **extra):
    defaults = dict(
        _video_recording_failed=MagicMock(return_value=False),
        _make_source_element=lambda dev: gst.ElementFactory.make("v4l2src", "src"),
        _source_output_pad=lambda src: FakePad("src"),
        _build_video_path=lambda: tmp_path / "clip.mkv",
        _selected_format_kind=lambda dev: "raw",
        _selected_resolution=None,
        _video_bitrate_kbps=4000,
        _video_path=None, _video_pipeline=None, _video_src=None, _video_bus=None,
        _picture=MagicMock(), _shutter=MagicMock(),
        _on_video_pipeline_error=MagicMock(), _on_video_pipeline_eos=MagicMock(),
        _show_capture_spinner=MagicMock(), _update_shutter_icon=MagicMock(),
        _start_record_blink=MagicMock(), _apply_flash_to_pipeline=MagicMock(),
        _show_toast=MagicMock(), _recording=False, _busy_capture=True,
    )
    defaults.update(extra)
    return _win(gst, **defaults)


def test_video_pipeline_records_mjpeg_into_matroska(tmp_path) -> None:
    """Matroska accepts MJPEG natively, which side-steps gst-droid's "cannot
    record video in raw mode" entirely."""
    gst = FakeGst()
    win = _video_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build_generic(win, {})

    factories = gst.pipelines[0].factories()
    assert "jpegenc" in factories
    assert "matroskamux" in factories
    assert "filesink" in factories
    assert gst.element("filesink").props["location"] == str(tmp_path / "clip.mkv")


def test_video_pipeline_reaches_playing_and_flips_the_recording_flag(tmp_path) -> None:
    gst = FakeGst()
    win = _video_win(gst, tmp_path)
    assert camera.CameraWindow._video_pipeline_build_generic(win, {}) is False
    assert win._recording is True
    assert win._busy_capture is False
    assert gst.pipelines[0].states == [FakeGst.State.PLAYING]
    win._start_record_blink.assert_called_once()
    win._apply_flash_to_pipeline.assert_called_once()


@pytest.mark.parametrize(
    ("kbps", "expected_quality"),
    list(camera._VIDEO_BITRATE_TO_QUALITY.items()),
)
def test_video_pipeline_maps_bitrate_to_jpeg_quality(tmp_path, kbps, expected_quality) -> None:
    gst = FakeGst()
    win = _video_win(gst, tmp_path, _video_bitrate_kbps=kbps)
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    assert gst.element("jpegenc").props["quality"] == expected_quality


@pytest.mark.parametrize(("given", "clamped"), [(100, 2000), (99999, 16000)])
def test_video_pipeline_clamps_the_bitrate(tmp_path, given, clamped) -> None:
    gst = FakeGst()
    win = _video_win(gst, tmp_path, _video_bitrate_kbps=given)
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    assert gst.element("jpegenc").props["quality"] == camera._VIDEO_BITRATE_TO_QUALITY[clamped]


def test_video_pipeline_adds_a_capsfilter_for_a_chosen_resolution(tmp_path) -> None:
    gst = FakeGst()
    win = _video_win(gst, tmp_path, _selected_resolution=(1280, 720))
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    assert gst.element("capsfilter").props["caps"] == "caps:video/x-raw,width=1280,height=720"


def test_video_pipeline_decodes_a_jpeg_mode_source(tmp_path) -> None:
    """A camera advertising the picked size only as MJPEG needs a jpegdec
    before the record chain."""
    gst = FakeGst()
    win = _video_win(gst, tmp_path, _selected_resolution=(1920, 1080),
                     _selected_format_kind=lambda dev: "jpeg")
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    assert gst.element("capsfilter").props["caps"] == "caps:image/jpeg,width=1920,height=1080"
    assert "jpegdec" in gst.pipelines[0].factories()


def test_video_pipeline_fails_without_jpegdec(tmp_path) -> None:
    gst = FakeGst(missing={"jpegdec"})
    win = _video_win(gst, tmp_path, _selected_resolution=(1920, 1080),
                     _selected_format_kind=lambda dev: "jpeg")
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    assert "jpegdec unavailable" in win._video_recording_failed.call_args[0][0]


def test_video_pipeline_fails_without_a_source(tmp_path) -> None:
    gst = FakeGst()
    win = _video_win(gst, tmp_path, _make_source_element=lambda dev: None)
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    assert "source unavailable" in win._video_recording_failed.call_args[0][0]


@pytest.mark.parametrize("element", ["matroskamux", "filesink", "jpegenc", "tee"])
def test_video_pipeline_names_the_missing_element(tmp_path, element) -> None:
    gst = FakeGst(missing={element})
    win = _video_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    win._video_recording_failed.assert_called_once()
    assert "unavailable" in win._video_recording_failed.call_args[0][0]


def test_video_pipeline_fails_on_an_unlinkable_source(tmp_path) -> None:
    gst = FakeGst()
    win = _video_win(gst, tmp_path, _source_output_pad=lambda src: FakePad("src", link_ok=False))
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    assert "link failed" in win._video_recording_failed.call_args[0][0]


def test_video_pipeline_fails_when_the_source_has_no_pad(tmp_path) -> None:
    gst = FakeGst()
    win = _video_win(gst, tmp_path, _source_output_pad=lambda src: None)
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    assert "pads unavailable" in win._video_recording_failed.call_args[0][0]


def test_video_pipeline_hands_the_paintable_to_the_picture(tmp_path) -> None:
    """Without this the preview freezes on the last still frame while
    recording, because the Picture still points at the old pipeline's sink."""
    gst = FakeGst(preset_props={"gtk4paintablesink": {"paintable": "paintable-object"}})
    win = _video_win(gst, tmp_path)
    camera.CameraWindow._video_pipeline_build_generic(win, {})
    win._picture.set_paintable.assert_called_once_with("paintable-object")
    assert gst.element("gtk4paintablesink").props["sync"] is False


def test_video_pipeline_reports_a_failed_state_change(tmp_path) -> None:
    gst = FakeGst()
    win = _video_win(gst, tmp_path)

    original_new = gst.Pipeline.new

    class _FailingPipeline(FakePipeline):
        def set_state(self, state):
            self.states.append(state)
            return FakeGst.StateChangeReturn.FAILURE

    def failing_new(name):
        p = _FailingPipeline(name)
        gst.pipelines.append(p)
        return p

    gst.Pipeline.new = staticmethod(failing_new)
    try:
        camera.CameraWindow._video_pipeline_build_generic(win, {})
    finally:
        gst.Pipeline.new = staticmethod(original_new)

    assert "could not start" in win._video_recording_failed.call_args[0][0]
    assert win._recording is False
