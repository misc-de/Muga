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

qr = pytest.importorskip("yaga.qr")
geo = pytest.importorskip("yaga.camera_geo")


# ---------------------------------------------------------------------------
# QR scanner
# ---------------------------------------------------------------------------

class _QrGst(FakeGst):
    """FakeGst plus the parse_launch / message enums the scanner needs."""

    class MessageType:
        ERROR = "ERROR"
        ELEMENT = "ELEMENT"
        EOS = "EOS"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
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
    gst = _QrGst(absent_factories={"autovideosrc"})
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


def test_scanner_reports_a_failed_start() -> None:
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
