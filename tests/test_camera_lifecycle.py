"""Tests for camera.py's pipeline lifecycle and capture routing.

Two things here are worth pinning down beyond "it works":

  * _stop_pipeline is the only place several handlers get released. The code
    says so explicitly — a video-mode swap used to leak the previous
    paintable's handler, and its closure keeps the whole window alive. It is
    also the only path that clears _busy_capture, so a camera switch during a
    capture would otherwise lock the shutter permanently.
  * _capture picks between three routes depending on HAL quirks, and the
    Halium ones exist because the obvious route does the wrong thing there
    (start-capture in mode=2 tries to record video).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.camera_fakes import FakeElement, FakeGst, FakePad, gst_win

camera = pytest.importorskip("muga.camera")


# ---------------------------------------------------------------------------
# Starting the preview pipeline
# ---------------------------------------------------------------------------

def _start_win(gst, device=None, **extra):
    device = {"path": "/dev/video0", "name": "Cam"} if device is None else device
    defaults = dict(
        _closing=False,
        _current_device=lambda: device,
        _stop_pipeline=MagicMock(),
        _mark_controls_dirty_for_device=MagicMock(),
        _make_source_element=lambda dev: gst.ElementFactory.make("v4l2src", "src"),
        _source_output_pad=lambda src: FakePad("src"),
        _build_downstream_description=lambda dev: "videoconvert ! tee name=t",
        _selected_resolution=None,
        _selected_format_kind=lambda dev: "raw",
        _fail=MagicMock(),
        _picture=MagicMock(),
        _shutter=MagicMock(),
        _chrome=MagicMock(),
        _flash_button=MagicMock(),
        _show_toast=MagicMock(),
        _populate_resolutions=MagicMock(),
        _apply_flash_to_pipeline=MagicMock(),
        _on_bus_message=MagicMock(),
        _on_source_buffer=MagicMock(),
        _on_sink_buffer=MagicMock(),
        _on_preview_sample=MagicMock(),
        _pipeline=None, _appsink=None, _valve=None, _capsfilter=None, _imgsink=None,
        _bus=None, _preview_appsink=None, _preview_signal_id=None,
        _preview_paintable=None, _preview_paintable_signal_id=None,
        _source_probe_pad=None, _source_probe_id=None,
        _sink_probe_pad=None, _sink_probe_id=None,
        _preview_frame_count=0, _source_frame_count=0, _sink_frame_count=0,
        _zoom=1.0,
    )
    defaults.update(extra)
    return gst_win(gst, **defaults)


def test_start_pipeline_does_nothing_while_closing() -> None:
    gst = FakeGst()
    win = _start_win(gst, _closing=True)
    assert camera.CameraWindow._start_pipeline(win) is False
    assert gst.pipelines == []


def test_start_pipeline_needs_a_device() -> None:
    gst = FakeGst()
    win = _start_win(gst, _current_device=lambda: None)
    camera.CameraWindow._start_pipeline(win)
    assert gst.pipelines == []


def test_start_pipeline_tears_down_the_previous_one_first() -> None:
    """Restarting without stopping leaves the v4l2 descriptor held."""
    gst = FakeGst()
    win = _start_win(gst)
    camera.CameraWindow._start_pipeline(win)
    win._stop_pipeline.assert_called_once()


def test_start_pipeline_reaches_playing() -> None:
    gst = FakeGst()
    win = _start_win(gst)
    assert camera.CameraWindow._start_pipeline(win) is False
    assert gst.pipelines[0].states == [FakeGst.State.PLAYING]
    win._populate_resolutions.assert_called_once()
    win._apply_flash_to_pipeline.assert_called_once()


def test_start_pipeline_reports_a_missing_source() -> None:
    gst = FakeGst()
    win = _start_win(gst, _make_source_element=lambda dev: None)
    camera.CameraWindow._start_pipeline(win)
    win._fail.assert_called_once()
    assert "source element" in win._fail.call_args[0][0]


def test_start_pipeline_reports_an_unparsable_description() -> None:
    gst = FakeGst()
    gst.parse_error = RuntimeError("no element \"gtk4paintablesink\"")
    win = _start_win(gst)
    camera.CameraWindow._start_pipeline(win)
    assert "Pipeline error" in win._fail.call_args[0][0]


def test_start_pipeline_reports_a_source_without_a_pad() -> None:
    gst = FakeGst()
    win = _start_win(gst, _source_output_pad=lambda src: None)
    camera.CameraWindow._start_pipeline(win)
    assert "output pad" in win._fail.call_args[0][0]


def test_start_pipeline_reports_a_refused_link() -> None:
    gst = FakeGst()
    win = _start_win(gst, _source_output_pad=lambda src: FakePad("src", link_ok=False))
    camera.CameraWindow._start_pipeline(win)
    assert "Could not link" in win._fail.call_args[0][0]


def test_start_pipeline_reports_a_failed_state_change() -> None:
    gst = FakeGst()
    win = _start_win(gst)

    from tests.camera_fakes import FakePipeline

    class _Failing(FakePipeline):
        def set_state(self, state):
            self.states.append(state)
            return FakeGst.StateChangeReturn.FAILURE

    gst.Pipeline.new = staticmethod(lambda name: gst.pipelines.append(_Failing(name)) or gst.pipelines[-1])
    camera.CameraWindow._start_pipeline(win)
    assert "Could not start camera" in win._fail.call_args[0][0]


def test_start_pipeline_pins_a_chosen_resolution() -> None:
    gst = FakeGst()
    win = _start_win(gst, _selected_resolution=(1280, 720))
    camera.CameraWindow._start_pipeline(win)
    assert gst.element("capsfilter").props["caps"] == "caps:video/x-raw,width=1280,height=720"


def test_start_pipeline_inserts_jpegdec_for_an_mjpeg_mode() -> None:
    gst = FakeGst()
    win = _start_win(gst, _selected_resolution=(1920, 1080),
                     _selected_format_kind=lambda dev: "jpeg")
    camera.CameraWindow._start_pipeline(win)
    assert gst.element("capsfilter").props["caps"] == "caps:image/jpeg,width=1920,height=1080"
    assert "jpegdec" in gst.made_factories()


def test_start_pipeline_caps_halium_to_720p_by_default() -> None:
    """droidcamsrc otherwise negotiates the HAL's max and the resulting GPU
    bandwidth competes with phosh until the compositor crashes."""
    gst = FakeGst()
    win = _start_win(gst, device={"source_factory": "droidcamsrc", "name": "HAL"})
    camera.CameraWindow._start_pipeline(win)
    cap = gst.element("capsfilter")
    assert cap.name == "halium_default_cap"
    assert "1280" in cap.props["caps"] and "720" in cap.props["caps"]
    assert "framerate" not in cap.props["caps"], (
        "a framerate range gives discrete-only HALs no valid value and "
        "negotiation stalls at READY->PAUSED"
    )


def test_start_pipeline_does_not_cap_a_user_choice_on_halium() -> None:
    gst = FakeGst()
    win = _start_win(gst, device={"source_factory": "droidcamsrc"},
                     _selected_resolution=(4000, 3000))
    camera.CameraWindow._start_pipeline(win)
    assert gst.element("capsfilter").name == "resfilter"


def test_start_pipeline_mirrors_the_front_camera() -> None:
    gst = FakeGst()
    win = _start_win(gst, device={"path": "/dev/video0", "location": "front"})
    camera.CameraWindow._start_pipeline(win)
    win._picture.set_mirrored.assert_called_once_with(True)


def test_start_pipeline_shows_the_flash_button_only_on_halium() -> None:
    gst = FakeGst()
    win = _start_win(gst, device={"source_factory": "droidcamsrc"})
    camera.CameraWindow._start_pipeline(win)
    win._flash_button.set_visible.assert_called_once_with(True)

    gst2 = FakeGst()
    win2 = _start_win(gst2, device={"path": "/dev/video0"})
    camera.CameraWindow._start_pipeline(win2)
    win2._flash_button.set_visible.assert_called_once_with(False)


def test_start_pipeline_warns_when_capture_is_unavailable() -> None:
    """No jpegenc means no snap appsink; the shutter has to say so rather
    than silently doing nothing."""
    gst = FakeGst()
    win = _start_win(gst)
    camera.CameraWindow._start_pipeline(win)
    # The fake bin exposes no named children, so get_by_name("snap") is None.
    win._shutter.set_sensitive.assert_called_once_with(False)
    win._show_toast.assert_called_once_with("Capture unavailable")


def test_start_pipeline_adds_buffer_probes() -> None:
    """The source/sink counters are how "one frame then freeze" gets diagnosed
    without turning on GST_DEBUG."""
    gst = FakeGst()
    pad = FakePad("src")
    win = _start_win(gst, _source_output_pad=lambda src: pad)
    camera.CameraWindow._start_pipeline(win)
    assert pad.probes, "no probe on the source pad"
    assert win._source_probe_id is not None


def test_start_pipeline_watches_the_bus() -> None:
    gst = FakeGst()
    win = _start_win(gst)
    camera.CameraWindow._start_pipeline(win)
    assert gst.pipelines[0].bus.watch_added == 1
    assert "message" in gst.pipelines[0].bus.signals


# ---------------------------------------------------------------------------
# Tearing it down
# ---------------------------------------------------------------------------

def _stop_win(gst, **extra):
    defaults = dict(
        _close_valve_and_disconnect=MagicMock(),
        _valve=FakeElement("valve", "shutter"),
        _busy_capture=True,
        _preview_appsink=None, _preview_signal_id=None,
        _preview_paintable=None, _preview_paintable_signal_id=None,
        _source_probe_pad=None, _source_probe_id=None,
        _sink_probe_pad=None, _sink_probe_id=None,
        _bus=None, _pipeline=None,
        _appsink=FakeElement("appsink", "snap"),
        _imgsink=FakeElement("appsink", "img"),
        _capture_signal_sink=FakeElement("appsink", "snap"),
        _capsfilter=FakeElement("capsfilter", "resfilter"),
    )
    defaults.update(extra)
    return gst_win(gst, **defaults)


def test_stop_pipeline_unlocks_the_shutter() -> None:
    """The only place that clears _busy_capture — a camera switch mid-capture
    would otherwise lock the shutter for the rest of the session."""
    win = _stop_win(FakeGst())
    camera.CameraWindow._stop_pipeline(win)
    assert win._busy_capture is False


def test_stop_pipeline_releases_the_paintable_handler() -> None:
    """Its closure holds `self`, so a leaked handler keeps the whole window
    alive for as long as the paintable does."""
    paintable = FakeElement("paintable", "paintable")
    win = _stop_win(FakeGst(), _preview_paintable=paintable,
                    _preview_paintable_signal_id=17)
    camera.CameraWindow._stop_pipeline(win)
    assert paintable.disconnected == [17]
    assert win._preview_paintable is None
    assert win._preview_paintable_signal_id is None


def test_stop_pipeline_releases_the_preview_appsink_handler() -> None:
    appsink = FakeElement("appsink", "preview_sink")
    win = _stop_win(FakeGst(), _preview_appsink=appsink, _preview_signal_id=5)
    camera.CameraWindow._stop_pipeline(win)
    assert appsink.disconnected == [5]
    assert win._preview_appsink is None


def test_stop_pipeline_removes_both_buffer_probes() -> None:
    src_pad, sink_pad = FakePad("src"), FakePad("sink")
    win = _stop_win(FakeGst(), _source_probe_pad=src_pad, _source_probe_id=1,
                    _sink_probe_pad=sink_pad, _sink_probe_id=2)
    camera.CameraWindow._stop_pipeline(win)
    assert src_pad.removed_probes == [1]
    assert sink_pad.removed_probes == [2]
    assert win._source_probe_pad is None
    assert win._sink_probe_pad is None


def test_stop_pipeline_drops_the_bus_watch() -> None:
    bus = FakeElement("bus", "bus")
    bus.watch_removed = 0
    bus.remove_signal_watch = lambda: setattr(bus, "watch_removed", bus.watch_removed + 1)
    win = _stop_win(FakeGst(), _bus=bus)
    camera.CameraWindow._stop_pipeline(win)
    assert bus.watch_removed == 1
    assert win._bus is None


def test_stop_pipeline_waits_for_the_null_transition() -> None:
    """The v4l2 device is only released once the transition finishes; a
    restart would otherwise race the same descriptor."""
    gst = FakeGst()
    pipeline = gst.Pipeline.new("p")
    win = _stop_win(gst, _pipeline=pipeline)
    camera.CameraWindow._stop_pipeline(win)
    assert pipeline.states == [FakeGst.State.NULL]
    assert pipeline.state_waits, "did not wait for the state change"
    assert pipeline.state_waits[0] <= FakeGst.SECOND, "waiting that long freezes the UI"
    assert win._pipeline is None


def test_stop_pipeline_clears_every_element_reference() -> None:
    win = _stop_win(FakeGst())
    camera.CameraWindow._stop_pipeline(win)
    for attr in ("_appsink", "_imgsink", "_capture_signal_sink", "_capsfilter", "_valve"):
        assert getattr(win, attr) is None, f"{attr} still referenced after teardown"


def test_stop_pipeline_is_safe_on_a_never_started_window() -> None:
    win = _stop_win(FakeGst(), _valve=None, _appsink=None, _imgsink=None,
                    _capture_signal_sink=None, _capsfilter=None)
    camera.CameraWindow._stop_pipeline(win)  # must not raise


def test_stop_pipeline_survives_a_raising_disconnect() -> None:
    paintable = FakeElement("paintable", "p")
    paintable.disconnect = MagicMock(side_effect=RuntimeError("already gone"))
    win = _stop_win(FakeGst(), _preview_paintable=paintable,
                    _preview_paintable_signal_id=3)
    camera.CameraWindow._stop_pipeline(win)
    assert win._preview_paintable is None, "teardown stopped at the first failure"


# ---------------------------------------------------------------------------
# Capture routing
# ---------------------------------------------------------------------------

def _capture_win(gst, **extra):
    defaults = dict(
        _busy_capture=False,
        _device_orientation="normal",
        _capture_orientation=None,
        _capsfilter=None,
        _imgsink=None,
        _appsink=FakeElement("appsink", "snap"),
        _valve=FakeElement("valve", "shutter"),
        _shutter=MagicMock(),
        _capture_via_image_pipeline=MagicMock(),
        _emit_start_capture_async=MagicMock(),
        _on_capture_sample=MagicMock(),
        _on_capture_timeout=MagicMock(),
        _close_valve_and_disconnect=MagicMock(),
        _capture_signal_id=None, _capture_signal_sink=None,
        _capture_timeout_id=None, _capture_min_width=0, _capture_saved_caps=None,
        _pipeline=None,
    )
    defaults.update(extra)
    return gst_win(gst, **defaults)


def test_capture_ignores_a_second_press_while_busy() -> None:
    win = _capture_win(FakeGst(), _busy_capture=True)
    camera.CameraWindow._capture(win)
    assert win._capture_signal_id is None


def test_capture_latches_the_framing_orientation() -> None:
    """Every route reaches _write_sample asynchronously, so the orientation
    has to be taken while it still describes how the shot was framed."""
    win = _capture_win(FakeGst(), _device_orientation="left-up")
    camera.CameraWindow._capture(win)
    assert win._capture_orientation == "left-up"


def test_capture_opens_the_valve_on_the_default_path() -> None:
    win = _capture_win(FakeGst())
    camera.CameraWindow._capture(win)
    assert win._valve.props["drop"] is False
    assert win._busy_capture is True
    win._shutter.set_sensitive.assert_called_once_with(False)
    assert win._capture_timeout_id is not None


def test_capture_routes_halium_through_the_image_pipeline() -> None:
    """In mode=2 start-capture would try to record video, so a capped Halium
    preview has to be torn down for a transient mode=1 pipeline."""
    win = _capture_win(FakeGst(), _capsfilter=FakeElement("capsfilter", "halium_default_cap"))
    camera.CameraWindow._capture(win)
    win._capture_via_image_pipeline.assert_called_once()
    assert win._busy_capture is False, "the transient pipeline owns the busy flag"


def test_capture_uses_imgsrc_when_the_pipeline_offers_it() -> None:
    imgsink = FakeElement("appsink", "img_sink")
    win = _capture_win(FakeGst(), _imgsink=imgsink,
                       _capsfilter=FakeElement("capsfilter", "halium_default_cap"))
    camera.CameraWindow._capture(win)
    win._capture_via_image_pipeline.assert_not_called()
    win._emit_start_capture_async.assert_called_once()
    assert win._emit_start_capture_async.call_args.kwargs["delay_ms"] == 150


def test_capture_does_nothing_without_a_sink() -> None:
    win = _capture_win(FakeGst(), _appsink=None, _imgsink=None)
    camera.CameraWindow._capture(win)
    assert win._busy_capture is False
    win._shutter.set_sensitive.assert_not_called()


def test_capture_swaps_caps_to_reach_sensor_resolution() -> None:
    """The 720p preview cap has to be lifted for the shot, or every Halium
    photo comes out at preview resolution."""
    caps = FakeElement("capsfilter", "halium_default_cap")
    caps.props["caps"] = "caps:video/x-raw,width=(int)[1,1280]"
    win = _capture_win(FakeGst(), _capsfilter=caps, _imgsink=None,
                       _appsink=FakeElement("appsink", "snap"))
    # imgsink is None and the capsfilter is the Halium one → image pipeline.
    camera.CameraWindow._capture(win)
    win._capture_via_image_pipeline.assert_called_once()


def test_capture_releases_the_shutter_when_connect_fails() -> None:
    """A leaked handler means the next capture connects twice and saves the
    photo twice."""
    sink = FakeElement("appsink", "snap")
    sink.connect = MagicMock(side_effect=RuntimeError("no such signal"))
    win = _capture_win(FakeGst(), _appsink=sink)
    camera.CameraWindow._capture(win)
    assert win._busy_capture is False
    assert win._capture_signal_id is None
    assert win._capture_signal_sink is None
    win._shutter.set_sensitive.assert_called_with(True)


def test_capture_cleans_up_when_the_timeout_cannot_be_armed() -> None:
    win = _capture_win(FakeGst())
    with patch.object(camera.GLib, "timeout_add_seconds", side_effect=RuntimeError("oom")):
        camera.CameraWindow._capture(win)
    win._close_valve_and_disconnect.assert_called_once()
    assert win._busy_capture is False
    win._shutter.set_sensitive.assert_called_with(True)
