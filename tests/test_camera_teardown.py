"""Tests for camera.py's shutdown paths.

Closing the camera window has to release hardware, not just widgets. Several
of these behaviours are there because the failure is invisible until it hurts:
the kernel LED keeps burning until reboot if the torch is not explicitly
turned off, and a transient capture pipeline left running keeps droidcamsrc
holding the HAL so the camera cannot be reopened at all.

Teardown also has to be exhaustive rather than stopping at the first failure —
each step is individually guarded for that reason, and the tests check that a
raising step does not strand the ones after it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.camera_fakes import FakeElement, FakeGst, gst_win

camera = pytest.importorskip("muga.camera")


# ---------------------------------------------------------------------------
# Closing the window
# ---------------------------------------------------------------------------

def _close_win(**extra):
    defaults = dict(
        _closing=False,
        _stop_pipeline=MagicMock(),
        _image_teardown=MagicMock(),
        _video_teardown=MagicMock(),
        _orient_tick_id=None,
        remove_tick_callback=MagicMock(),
        _toast_timer=None, _countdown_source=None, _focus_hide_source=None,
        _record_dot_blink_id=None, _swipe_hint_pulse_id=None,
        _image_timeout_id=None, _video_finalize_source=None,
        _settings_persist_source=None,
        _persist_settings_flush=MagicMock(),
        _geo=None, _orientation=None,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def _close(win):
    with patch.object(camera, "set_torch_sysfs") as torch, \
         patch.object(camera.GLib, "source_remove") as remove:
        result = camera.CameraWindow._on_close(win, None)
    return result, torch, remove


def test_close_flags_teardown_before_touching_anything() -> None:
    """Streaming-thread callbacks and deferred idle_adds check this flag; if
    it is set late they touch destroyed widgets or reopen the HAL."""
    order = []
    win = _close_win(_stop_pipeline=MagicMock(side_effect=lambda: order.append(win._closing)))
    _close(win)
    assert order == [True], "_closing was not set before teardown started"


def test_close_turns_the_torch_off() -> None:
    """The kernel LED stays lit until reboot otherwise."""
    win = _close_win()
    _, torch, _ = _close(win)
    torch.assert_called_once_with(False)


def test_close_tears_down_every_pipeline() -> None:
    """A stray transient pipeline keeps droidcamsrc holding the HAL, and the
    camera cannot be reopened."""
    win = _close_win()
    _close(win)
    win._stop_pipeline.assert_called_once()
    win._image_teardown.assert_called_once()
    win._video_teardown.assert_called_once()


def test_close_continues_after_a_failing_teardown() -> None:
    win = _close_win(_image_teardown=MagicMock(side_effect=RuntimeError("boom")))
    _close(win)
    win._video_teardown.assert_called_once(), "teardown stopped at the first failure"


def test_close_removes_the_orientation_tick() -> None:
    win = _close_win(_orient_tick_id=7)
    _close(win)
    win.remove_tick_callback.assert_called_once_with(7)
    assert win._orient_tick_id is None


@pytest.mark.parametrize(
    "attr",
    ["_toast_timer", "_countdown_source", "_focus_hide_source",
     "_record_dot_blink_id", "_swipe_hint_pulse_id", "_image_timeout_id",
     "_video_finalize_source"],
)
def test_close_removes_every_glib_source(attr) -> None:
    """A surviving timer fires against a destroyed window."""
    win = _close_win(**{attr: 99})
    _, _, remove = _close(win)
    remove.assert_any_call(99)
    assert getattr(win, attr) is None


def test_close_flushes_pending_settings() -> None:
    """Settings touched within the debounce window would otherwise be lost."""
    win = _close_win(_settings_persist_source=5)
    _close(win)
    win._persist_settings_flush.assert_called_once()
    assert win._settings_persist_source is None


def test_close_does_not_flush_when_nothing_is_pending() -> None:
    win = _close_win(_settings_persist_source=None)
    _close(win)
    win._persist_settings_flush.assert_not_called()


def test_close_stops_the_sensor_clients() -> None:
    """GeoClue and the orientation sensor hold D-Bus subscriptions."""
    geo, orientation = MagicMock(), MagicMock()
    win = _close_win(_geo=geo, _orientation=orientation)
    _close(win)
    geo.stop.assert_called_once()
    orientation.stop.assert_called_once()
    assert win._geo is None
    assert win._orientation is None


def test_close_survives_a_raising_sensor_stop() -> None:
    geo = MagicMock(stop=MagicMock(side_effect=RuntimeError("dbus gone")))
    orientation = MagicMock()
    win = _close_win(_geo=geo, _orientation=orientation)
    _close(win)
    orientation.stop.assert_called_once(), "one failing client stranded the other"


def test_close_returns_false_so_the_window_closes() -> None:
    result, _, _ = _close(_close_win())
    assert result is False


# ---------------------------------------------------------------------------
# Transient pipeline teardown
# ---------------------------------------------------------------------------

def test_image_teardown_releases_the_hal() -> None:
    gst = FakeGst()
    pipeline = gst.Pipeline.new("img")
    bus = FakeElement("bus", "bus")
    bus.removed = 0
    bus.remove_signal_watch = lambda: setattr(bus, "removed", bus.removed + 1)
    win = gst_win(gst, _image_bus=bus, _image_pipeline=pipeline,
                  _image_src=object(), _image_signal_id=3)

    camera.CameraWindow._image_teardown(win)

    assert bus.removed == 1
    assert pipeline.states == [FakeGst.State.NULL]
    assert pipeline.state_waits, "did not wait for the HAL to be released"
    assert win._image_pipeline is None
    assert win._image_src is None
    assert win._image_signal_id is None


def test_image_teardown_is_safe_when_nothing_is_running() -> None:
    win = gst_win(FakeGst(), _image_bus=None, _image_pipeline=None,
                  _image_src=None, _image_signal_id=None)
    camera.CameraWindow._image_teardown(win)


def test_video_teardown_cancels_the_finalize_timer() -> None:
    """A stale finalize timer fires against an unrelated future pipeline."""
    gst = FakeGst()
    win = gst_win(gst, _cancel_video_finalize_timeout=MagicMock(),
                  _video_bus=None, _video_pipeline=None,
                  _video_src=None, _video_path="/tmp/x.mkv")
    camera.CameraWindow._video_teardown(win)
    win._cancel_video_finalize_timeout.assert_called_once()
    assert win._video_path is None


def test_video_teardown_stops_the_pipeline() -> None:
    gst = FakeGst()
    pipeline = gst.Pipeline.new("vid")
    win = gst_win(gst, _cancel_video_finalize_timeout=MagicMock(),
                  _video_bus=None, _video_pipeline=pipeline,
                  _video_src=object(), _video_path="/tmp/x.mkv")
    camera.CameraWindow._video_teardown(win)
    assert pipeline.states == [FakeGst.State.NULL]
    assert win._video_pipeline is None
    assert win._video_src is None


# ---------------------------------------------------------------------------
# Capture cleanup
# ---------------------------------------------------------------------------

def _valve_win(gst, **extra):
    defaults = dict(
        _valve=FakeElement("valve", "shutter"),
        _capture_signal_id=None, _capture_signal_sink=None,
        _capture_timeout_id=None, _capture_saved_caps=None,
        _capsfilter=None, _capture_min_width=0,
    )
    defaults.update(extra)
    return gst_win(gst, **defaults)


def test_valve_cleanup_closes_the_valve() -> None:
    """Left open, jpegenc runs on every frame at 30 fps and the phone OOMs."""
    win = _valve_win(FakeGst())
    camera.CameraWindow._close_valve_and_disconnect(win)
    assert win._valve.props["drop"] is True


def test_valve_cleanup_disconnects_the_sample_handler() -> None:
    sink = FakeElement("appsink", "snap")
    win = _valve_win(FakeGst(), _capture_signal_id=8, _capture_signal_sink=sink)
    camera.CameraWindow._close_valve_and_disconnect(win)
    assert sink.disconnected == [8]
    assert win._capture_signal_id is None
    assert win._capture_signal_sink is None


def test_valve_cleanup_cancels_the_capture_timeout() -> None:
    win = _valve_win(FakeGst(), _capture_timeout_id=12)
    with patch.object(camera.GLib, "source_remove") as remove:
        camera.CameraWindow._close_valve_and_disconnect(win)
    remove.assert_called_once_with(12)
    assert win._capture_timeout_id is None


def test_valve_cleanup_restores_the_halium_preview_cap() -> None:
    """The cap was lifted to reach sensor resolution; leaving it lifted puts
    the preview back at full HAL resolution and the compositor suffers."""
    caps_element = FakeElement("capsfilter", "halium_default_cap")
    win = _valve_win(FakeGst(), _capsfilter=caps_element,
                     _capture_saved_caps="caps:720p", _capture_min_width=1281)
    camera.CameraWindow._close_valve_and_disconnect(win)
    assert caps_element.props["caps"] == "caps:720p"
    assert win._capture_saved_caps is None
    assert win._capture_min_width == 0


def test_valve_cleanup_is_safe_without_a_pipeline() -> None:
    win = _valve_win(FakeGst(), _valve=None)
    camera.CameraWindow._close_valve_and_disconnect(win)


def test_valve_cleanup_survives_a_dead_valve() -> None:
    valve = FakeElement("valve", "shutter")
    valve.set_property = MagicMock(side_effect=RuntimeError("element disposed"))
    sink = FakeElement("appsink", "snap")
    win = _valve_win(FakeGst(), _valve=valve, _capture_signal_id=4,
                     _capture_signal_sink=sink)
    camera.CameraWindow._close_valve_and_disconnect(win)
    assert sink.disconnected == [4], "a dead valve stranded the handler cleanup"


# ---------------------------------------------------------------------------
# Deferred start-capture
# ---------------------------------------------------------------------------

class _StatePipeline(FakeElement):
    def __init__(self, state) -> None:
        super().__init__("pipeline", "p")
        self._state = state
        self.src = FakeElement("droidcamsrc", "src")
        self.src.emitted = []
        self.src.emit = lambda signal: self.src.emitted.append(signal)

    def get_state(self, _timeout):
        return (None, self._state, None)

    def get_by_name(self, name):
        return self.src if name == "src" else None


def _run_emit(win, **kwargs):
    """Call _emit_start_capture_async and run the callback it scheduled."""
    captured = {}
    with patch.object(camera.GLib, "timeout_add",
                      side_effect=lambda ms, fn: captured.update(ms=ms, fn=fn)):
        camera.CameraWindow._emit_start_capture_async(win, **kwargs)
    return captured


def test_start_capture_is_deferred_by_the_requested_delay() -> None:
    """The HAL needs time to finish its Photography reconfigure first."""
    gst = FakeGst()
    pipeline = _StatePipeline(FakeGst.State.PLAYING)
    scheduled = _run_emit(
        gst_win(gst),
        get_pipeline=lambda: pipeline, get_src=lambda p: p.src,
        delay_ms=500, log_prefix="test", thread_name="unused",
    )
    assert scheduled["ms"] == 500
    assert scheduled["fn"]() is False, "the emit must be one-shot"
    assert pipeline.src.emitted == ["start-capture"]


@pytest.mark.parametrize("state", [FakeGst.State.PLAYING, FakeGst.State.PAUSED])
def test_start_capture_fires_once_the_pipeline_is_up(state) -> None:
    pipeline = _StatePipeline(state)
    scheduled = _run_emit(
        gst_win(FakeGst()),
        get_pipeline=lambda: pipeline, get_src=lambda p: p.src,
        delay_ms=150, log_prefix="test", thread_name="unused",
    )
    scheduled["fn"]()
    assert pipeline.src.emitted == ["start-capture"]


@pytest.mark.parametrize("state", [FakeGst.State.NULL, FakeGst.State.READY])
def test_start_capture_skips_a_pipeline_that_is_not_up(state) -> None:
    pipeline = _StatePipeline(state)
    scheduled = _run_emit(
        gst_win(FakeGst()),
        get_pipeline=lambda: pipeline, get_src=lambda p: p.src,
        delay_ms=150, log_prefix="test", thread_name="unused",
    )
    scheduled["fn"]()
    assert pipeline.src.emitted == []


def test_start_capture_skips_a_torn_down_pipeline() -> None:
    """Teardown can run between scheduling and firing; emitting against an
    unparented element is what the main-loop dispatch exists to avoid."""
    scheduled = _run_emit(
        gst_win(FakeGst()),
        get_pipeline=lambda: None, get_src=lambda p: None,
        delay_ms=150, log_prefix="test", thread_name="unused",
    )
    assert scheduled["fn"]() is False


def test_start_capture_honours_the_extra_guard() -> None:
    """The vfsrc path passes _busy_capture: if the capture was already
    abandoned, the HAL must not be asked for a picture."""
    pipeline = _StatePipeline(FakeGst.State.PLAYING)
    scheduled = _run_emit(
        gst_win(FakeGst()),
        get_pipeline=lambda: pipeline, get_src=lambda p: p.src,
        delay_ms=150, log_prefix="test", thread_name="unused",
        extra_guard=lambda: False,
    )
    scheduled["fn"]()
    assert pipeline.src.emitted == []


def test_start_capture_survives_a_missing_source() -> None:
    pipeline = _StatePipeline(FakeGst.State.PLAYING)
    scheduled = _run_emit(
        gst_win(FakeGst()),
        get_pipeline=lambda: pipeline, get_src=lambda p: None,
        delay_ms=150, log_prefix="test", thread_name="unused",
    )
    assert scheduled["fn"]() is False


def test_start_capture_survives_a_refusing_element() -> None:
    pipeline = _StatePipeline(FakeGst.State.PLAYING)
    pipeline.src.emit = MagicMock(side_effect=RuntimeError("no such signal"))
    scheduled = _run_emit(
        gst_win(FakeGst()),
        get_pipeline=lambda: pipeline, get_src=lambda p: p.src,
        delay_ms=150, log_prefix="test", thread_name="unused",
    )
    assert scheduled["fn"]() is False
