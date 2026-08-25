"""Tests for the QR scanner and the GeoClue client.

Both talk to hardware — a camera pipeline and the system D-Bus — so both are
driven through doubles here.

The location freshness rules carry a privacy decision worth pinning down:
GeoClue falls back to cell-tower accuracy (often more than a kilometre) when
GPS is unavailable, and writing that into a photo's EXIF shares more location
than someone enabling geotagging meant to.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.camera_fakes import FakeElement, FakeGst

qr = pytest.importorskip("muga.qr")
geo = pytest.importorskip("muga.camera_geo")


# ---------------------------------------------------------------------------
# QR scanner
# ---------------------------------------------------------------------------

class _QrGst(FakeGst):
    """FakeGst plus the parse_launch / message enums the scanner needs."""

    class MessageType:
        ERROR = "ERROR"
        ELEMENT = "ELEMENT"
        EOS = "EOS"

    def __init__(self, *, halium: bool = False, **kw) -> None:
        # Desktops don't have gst-droid; the Halium phones do, and there the
        # scanner takes a different (capped) source path. Default to the
        # desktop shape and let the Halium tests opt in.
        absent = set(kw.pop("absent_factories", ()) or ())
        if not halium:
            absent.add("droidcamsrc")
        super().__init__(absent_factories=absent, **kw)
        self.launched: list[str] = []
        self.launch_error: Exception | None = None
        outer = self

        def _parse_launch(description):
            if outer.launch_error is not None:
                raise outer.launch_error
            outer.launched.append(description)
            pipeline = outer.Pipeline.new("qr")
            preview = FakeElement("gtk4paintablesink", "preview",
                                  preset_props={"paintable": "paintable-object"})
            pipeline.added.append(preview)
            return pipeline

        self.parse_launch = _parse_launch


@pytest.fixture()
def plenty_of_memory(monkeypatch):
    """Pin the memory backstop to "all clear" so a test that starts the
    scanner doesn't depend on how loaded the machine running it happens
    to be."""
    monkeypatch.setattr(qr.memory_guard, "pressure_reason", lambda *_a, **_k: None)


def _scanner(gst, **kw):
    """A WebcamQRScanner without running its GTK-touching __init__."""
    scanner = qr.WebcamQRScanner.__new__(qr.WebcamQRScanner)
    scanner._Gst = gst
    scanner._GLib = MagicMock()
    scanner._Gtk = MagicMock()
    scanner.on_success = kw.get("on_success", MagicMock())
    scanner.on_error = kw.get("on_error", MagicMock())
    scanner.timeout_seconds = kw.get("timeout_seconds", 60)
    scanner._pipeline = None
    scanner._bus = None
    scanner._timeout_id = None
    scanner._mem_guard_id = None
    scanner._mem_baseline_kb = 1000
    scanner._finished = False
    scanner._picture = MagicMock()
    scanner._status = MagicMock()
    return scanner


def test_scanner_builds_a_pipeline_with_a_preview() -> None:
    gst = _QrGst()
    scanner = _scanner(gst)
    scanner._build_pipeline()
    assert "zxing message=true" in gst.launched[0]
    assert "gtk4paintablesink" in gst.launched[0]
    scanner._picture.set_paintable.assert_called_once_with("paintable-object")


def test_scanner_falls_back_without_a_preview_sink() -> None:
    """A headless-ish setup still has to scan, just without showing the feed."""
    gst = _QrGst(absent_factories={"gtk4paintablesink"})
    scanner = _scanner(gst)
    scanner._build_pipeline()
    assert "gtk4paintablesink" not in gst.launched[0]
    assert "zxing" in gst.launched[0]


def test_scanner_reports_a_missing_camera() -> None:
    gst = _QrGst(absent_factories={"autovideosrc"})  # droidcamsrc absent by default
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    scanner._build_pipeline()
    assert "No video device" in on_error.call_args[0][0]


def test_scanner_names_the_package_for_a_missing_zxing() -> None:
    """gstreamer1.0-plugins-bad is not installed by default anywhere."""
    gst = _QrGst(absent_factories={"zxing"})
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    scanner._build_pipeline()
    assert "plugins-bad" in on_error.call_args[0][0]


def test_scanner_reports_an_unparsable_pipeline() -> None:
    gst = _QrGst()
    gst.launch_error = RuntimeError("syntax error")
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    scanner._build_pipeline()
    assert "Pipeline error" in on_error.call_args[0][0]


def test_scanner_delivers_a_decoded_code() -> None:
    gst = _QrGst()
    on_success = MagicMock()
    scanner = _scanner(gst, on_success=on_success)
    structure = MagicMock()
    structure.get_name.return_value = "barcode"
    structure.get_value.return_value = "  nc://login/password:pw  "
    message = SimpleNamespace(type=_QrGst.MessageType.ELEMENT,
                              get_structure=lambda: structure)

    scanner._on_message(None, message)

    on_success.assert_called_once_with("nc://login/password:pw")
    assert scanner._finished is True, "the scanner kept running after a hit"


def test_scanner_ignores_other_element_messages() -> None:
    gst = _QrGst()
    on_success = MagicMock()
    scanner = _scanner(gst, on_success=on_success)
    structure = MagicMock()
    structure.get_name.return_value = "level"
    scanner._on_message(None, SimpleNamespace(type=_QrGst.MessageType.ELEMENT,
                                              get_structure=lambda: structure))
    on_success.assert_not_called()


@pytest.mark.parametrize("symbol", ["", "   ", None])
def test_scanner_ignores_an_empty_symbol(symbol) -> None:
    gst = _QrGst()
    on_success = MagicMock()
    scanner = _scanner(gst, on_success=on_success)
    structure = MagicMock()
    structure.get_name.return_value = "barcode"
    structure.get_value.return_value = symbol
    scanner._on_message(None, SimpleNamespace(type=_QrGst.MessageType.ELEMENT,
                                              get_structure=lambda: structure))
    on_success.assert_not_called()


def test_scanner_reports_a_pipeline_error() -> None:
    gst = _QrGst()
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    message = SimpleNamespace(type=_QrGst.MessageType.ERROR,
                              parse_error=lambda: ("device busy", "debug"))
    scanner._on_message(None, message)
    assert "Camera error" in on_error.call_args[0][0]


def test_scanner_ignores_messages_after_it_finished() -> None:
    """The bus can deliver a queued message after cancel()."""
    gst = _QrGst()
    on_success, on_error = MagicMock(), MagicMock()
    scanner = _scanner(gst, on_success=on_success, on_error=on_error)
    scanner._finished = True
    scanner._on_message(None, SimpleNamespace(type=_QrGst.MessageType.ERROR,
                                              parse_error=lambda: ("x", "y")))
    on_error.assert_not_called()


def test_scanner_times_out() -> None:
    gst = _QrGst()
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    assert scanner._on_timeout() is False
    assert "Timeout" in on_error.call_args[0][0]


def test_scanner_timeout_after_a_hit_is_silent() -> None:
    gst = _QrGst()
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    scanner._finished = True
    scanner._on_timeout()
    on_error.assert_not_called()


def test_scanner_reports_a_failed_start(plenty_of_memory) -> None:
    gst = _QrGst()

    from tests.camera_fakes import FakePipeline

    class _Failing(FakePipeline):
        def set_state(self, state):
            self.states.append(state)
            return FakeGst.StateChangeReturn.FAILURE

    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    scanner._build_pipeline = lambda: setattr(scanner, "_pipeline", _Failing("qr"))
    scanner.start()
    assert "Could not start camera" in on_error.call_args[0][0]


def test_scanner_cancel_releases_the_camera() -> None:
    gst = _QrGst()
    scanner = _scanner(gst)
    scanner._build_pipeline()
    pipeline = scanner._pipeline
    scanner.cancel()
    assert scanner._finished is True
    assert scanner._pipeline is None
    assert FakeGst.State.NULL in pipeline.states


# ---------------------------------------------------------------------------
# Resource ceilings
#
# A QR scan is a camera pipeline, and an uncapped one took the user's phone
# down: memory filled, the app died, and the OOM killer took phosh with it.
# Every ceiling below is load-bearing, so each gets a test.
# ---------------------------------------------------------------------------

def test_source_is_capped_on_halium() -> None:
    """droidcamsrc otherwise negotiates the HAL's largest mode and fills its
    pool faster than zxing drains it."""
    gst = _QrGst(halium=True)
    scanner = _scanner(gst)
    scanner._build_pipeline()
    desc = gst.launched[0]
    assert "droidcamsrc mode=2" in desc
    assert "src.vfsrc" in desc, "must pin the viewfinder pad, not imgsrc/vidsrc"
    assert "width=(int)[1,1280],height=(int)[1,720]" in desc


def test_every_queue_is_bounded() -> None:
    """queue's byte/time limits default to 10 MB / 1 s, and a single
    full-resolution frame blows past the byte one on its own — which would
    leave the buffer count meaningless."""
    gst = _QrGst()
    scanner = _scanner(gst)
    scanner._build_pipeline()
    desc = gst.launched[0]
    queues = [part for part in desc.split("!") if part.strip().startswith("queue")]
    assert queues, "the branches must be decoupled from the source by queues"
    for queue in queues:
        assert "leaky=downstream" in queue
        assert "max-size-buffers=1" in queue
        assert "max-size-bytes=0" in queue
        assert "max-size-time=0" in queue


def test_decode_branch_is_throttled() -> None:
    """zxing reads a QR code fine from a small frame, and a code held in
    front of the lens does not need thirty decode attempts a second."""
    gst = _QrGst()
    scanner = _scanner(gst)
    scanner._build_pipeline()
    decode = gst.launched[0].split("zxing")[0]
    assert "framerate=(fraction)[1/1,8/1]" in decode
    assert "width=(int)[1,640]" in decode
    # Without a pinned par, videoscale squashes the pixels instead of the
    # frame and a 4K feed arrives as 640x2160 — nearly as big, and too
    # distorted for zxing to read.
    assert "pixel-aspect-ratio=(fraction)1/1" in decode
    assert "format=I420" in decode, "ARGB costs zxing 4x the bytes per pixel"


def test_preview_is_capped_and_does_not_sync() -> None:
    """sync=true makes the sink drop 'late' buffers and log a warning for
    every one of them — a CPU-burning, log-filling loop on a phone."""
    gst = _QrGst()
    scanner = _scanner(gst)
    scanner._build_pipeline()
    preview = gst.launched[0].split("t. ! ")[-1]
    assert "gtk4paintablesink name=preview sync=false" in preview
    assert "width=(int)[1,1280]" in preview
    assert "pixel-aspect-ratio=(fraction)1/1" in preview
    assert "framerate=(fraction)[1/1,15/1]" in preview


def test_scanner_survives_without_the_scaling_plugins() -> None:
    """videoscale/videorate are base plugins, but a broken install must
    still scan rather than fail to negotiate."""
    gst = _QrGst(absent_factories={"videoscale", "videorate"})
    scanner = _scanner(gst)
    scanner._build_pipeline()
    desc = gst.launched[0]
    assert "videoscale" not in desc and "videorate" not in desc
    assert "zxing message=true" in desc


def test_stop_waits_for_the_camera_to_be_released() -> None:
    """The HAL only hands back its buffer pool once the NULL transition has
    actually completed; reopening the scanner before then races it."""
    gst = _QrGst()
    scanner = _scanner(gst)
    scanner._build_pipeline()
    pipeline = scanner._pipeline
    scanner.cancel()
    assert pipeline.state_waits, "did not wait for the state change"


# ---------------------------------------------------------------------------
# Memory backstop
# ---------------------------------------------------------------------------

def test_scanner_refuses_to_start_when_memory_is_already_short(monkeypatch) -> None:
    gst = _QrGst()
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    monkeypatch.setattr(qr.memory_guard, "pressure_reason",
                        lambda *_a, **_k: "low system memory (40 MB free)")
    scanner.start()
    assert scanner._pipeline is None, "started a camera on an OOM-bound system"
    assert "Camera not started" in on_error.call_args[0][0]


def test_memory_guard_stops_a_runaway_scan(monkeypatch) -> None:
    gst = _QrGst()
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    scanner._build_pipeline()
    pipeline = scanner._pipeline
    monkeypatch.setattr(qr.memory_guard, "pressure_reason",
                        lambda *_a, **_k: "runaway memory use (+512 MB)")

    assert scanner._on_memory_check() is False, "the guard kept polling"

    assert FakeGst.State.NULL in pipeline.states, "the camera was left running"
    assert "runaway memory use" in on_error.call_args[0][0]


def test_memory_guard_keeps_polling_while_memory_is_fine(plenty_of_memory) -> None:
    gst = _QrGst()
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    scanner._build_pipeline()
    assert scanner._on_memory_check() is True
    on_error.assert_not_called()


def test_memory_guard_stops_polling_once_the_scan_is_done() -> None:
    gst = _QrGst()
    scanner = _scanner(gst)
    scanner._finished = True
    assert scanner._on_memory_check() is False


# ---------------------------------------------------------------------------
# memory_guard probes
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_proc(tmp_path, monkeypatch):
    """Point the /proc probes at files we control."""
    from muga import memory_guard

    def _write(*, available_kb=8_000_000, total_kb=8_000_000, rss_kb=200_000):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            f"MemTotal:       {total_kb} kB\n"
            f"MemFree:         1000000 kB\n"
            f"MemAvailable:   {available_kb} kB\n"
        )
        status = tmp_path / "status"
        status.write_text(f"Name:\tmuga\nVmPeak:\t900000 kB\nVmRSS:\t{rss_kb} kB\n")
        monkeypatch.setattr(memory_guard, "_MEMINFO", meminfo)
        monkeypatch.setattr(memory_guard, "_SELF_STATUS", status)
        return memory_guard

    return _write


def test_memory_probes_read_proc(fake_proc) -> None:
    mg = fake_proc(available_kb=1_234_567, total_kb=4_000_000, rss_kb=98_765)
    assert mg.available_kb() == 1_234_567
    assert mg.total_kb() == 4_000_000
    assert mg.self_rss_kb() == 98_765


def test_no_pressure_when_there_is_headroom(fake_proc) -> None:
    mg = fake_proc(available_kb=3_000_000, total_kb=8_000_000, rss_kb=200_000)
    assert mg.pressure_reason(baseline_rss_kb=150_000) is None


def test_pressure_when_the_system_runs_low(fake_proc) -> None:
    """The threshold scales with RAM: 5% of 8 GB is 400 MB, so 200 MB free
    is already too close to the OOM killer on a phone this size."""
    mg = fake_proc(available_kb=200_000, total_kb=8_000_000)
    assert "low system memory" in mg.pressure_reason()


def test_pressure_when_our_own_process_runs_away(fake_proc) -> None:
    """Caches get reclaimed for a while before MemAvailable moves, so a pool
    filling inside our process has to be caught on its own."""
    mg = fake_proc(available_kb=3_000_000, total_kb=8_000_000, rss_kb=700_000)
    assert "runaway memory use" in mg.pressure_reason(baseline_rss_kb=200_000)
    assert mg.pressure_reason(baseline_rss_kb=690_000) is None


def test_probes_survive_an_unreadable_proc(tmp_path, monkeypatch) -> None:
    from muga import memory_guard
    monkeypatch.setattr(memory_guard, "_MEMINFO", tmp_path / "nope")
    monkeypatch.setattr(memory_guard, "_SELF_STATUS", tmp_path / "nope")
    assert memory_guard.available_kb() is None
    assert memory_guard.self_rss_kb() is None
    assert memory_guard.pressure_reason(baseline_rss_kb=1000) is None


def test_scanner_only_reports_the_first_failure() -> None:
    gst = _QrGst()
    on_error = MagicMock()
    scanner = _scanner(gst, on_error=on_error)
    scanner._fail("first")
    scanner._fail("second")
    on_error.assert_called_once_with("first")


# ---------------------------------------------------------------------------
# GeoClue location freshness
# ---------------------------------------------------------------------------

def _geo_client(location=None):
    client = geo.GeoClient.__new__(geo.GeoClient)
    client._location = location
    return client


def _fix(**over):
    base = {"lat": 52.52, "lon": 13.405, "alt": 34.0,
            "accuracy": 10.0, "timestamp": time.time()}
    base.update(over)
    return base


def test_latest_returns_a_fresh_precise_fix() -> None:
    assert _geo_client(_fix()).latest() is not None


def test_latest_without_a_fix() -> None:
    assert _geo_client(None).latest() is None


def test_latest_drops_a_stale_fix() -> None:
    """A fix from an hour ago tags the photo with where the user was, not
    where they are."""
    old = _fix(timestamp=time.time() - geo.LOCATION_TTL_SECONDS - 1)
    assert _geo_client(old).latest() is None


def test_latest_keeps_a_fix_just_inside_the_ttl() -> None:
    recent = _fix(timestamp=time.time() - geo.LOCATION_TTL_SECONDS + 5)
    assert _geo_client(recent).latest() is not None


def test_latest_drops_a_coarse_fix() -> None:
    """GeoClue falls back to cell-tower accuracy without GPS; baking a
    kilometre-wide fix into EXIF shares more than the user agreed to."""
    coarse = _fix(accuracy=geo.LOCATION_MAX_ACCURACY_M + 1)
    assert _geo_client(coarse).latest() is None


def test_latest_keeps_a_fix_at_the_accuracy_limit() -> None:
    edge = _fix(accuracy=geo.LOCATION_MAX_ACCURACY_M)
    assert _geo_client(edge).latest() is not None


def test_latest_treats_an_unknown_accuracy_as_precise() -> None:
    """Some providers report 0/None; refusing those would disable geotagging
    entirely on that hardware."""
    assert _geo_client(_fix(accuracy=0.0)).latest() is not None
    assert _geo_client(_fix(accuracy=None)).latest() is not None


def test_latest_of_a_fix_with_no_timestamp() -> None:
    """Missing timestamp defaults to 0, i.e. epoch — must read as stale."""
    fix = _fix()
    del fix["timestamp"]
    assert _geo_client(fix).latest() is None


# ---------------------------------------------------------------------------
# GeoClue signal handling
# ---------------------------------------------------------------------------

def _signal_client(**over):
    client = geo.GeoClient.__new__(geo.GeoClient)
    client._location = None
    client._on_update = over.get("on_update")
    client._on_error = over.get("on_error")
    client._read_location = over.get("read_location", MagicMock())
    return client


def test_signal_handler_ignores_other_signals() -> None:
    client = _signal_client()
    client._on_signal(None, None, "SomethingElse", MagicMock())
    client._read_location.assert_not_called()


def test_signal_handler_reads_the_new_location_path() -> None:
    on_update = MagicMock()
    client = _signal_client(on_update=on_update)
    params = MagicMock()
    params.unpack.return_value = ("/old/path", "/new/path")

    client._on_signal(None, None, "LocationUpdated", params)

    client._read_location.assert_called_once_with("/new/path")
    on_update.assert_called_once()


def test_signal_handler_ignores_an_empty_path() -> None:
    client = _signal_client()
    params = MagicMock()
    params.unpack.return_value = ("/old", "")
    client._on_signal(None, None, "LocationUpdated", params)
    client._read_location.assert_not_called()


def test_signal_handler_survives_an_unpackable_payload() -> None:
    client = _signal_client()
    params = MagicMock()
    params.unpack.side_effect = RuntimeError("bad variant")
    client._on_signal(None, None, "LocationUpdated", params)
    client._read_location.assert_not_called()


def test_signal_handler_survives_a_raising_callback() -> None:
    """The callback is the camera window's; a failure there must not kill the
    D-Bus subscription."""
    client = _signal_client(on_update=MagicMock(side_effect=RuntimeError("boom")))
    params = MagicMock()
    params.unpack.return_value = ("/old", "/new")
    client._on_signal(None, None, "LocationUpdated", params)


def test_fail_reports_through_the_error_callback() -> None:
    on_error = MagicMock()
    client = geo.GeoClient.__new__(geo.GeoClient)
    client._on_error = on_error
    client._fail("no GeoClue on this system")
    on_error.assert_called_once_with("no GeoClue on this system")


def test_fail_survives_a_raising_error_callback() -> None:
    client = geo.GeoClient.__new__(geo.GeoClient)
    client._on_error = MagicMock(side_effect=RuntimeError("boom"))
    client._fail("message")


def test_fail_without_a_callback() -> None:
    client = geo.GeoClient.__new__(geo.GeoClient)
    client._on_error = None
    client._fail("message")
