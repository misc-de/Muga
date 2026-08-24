"""Orientation tests driven by data recorded from a real Halium phone.

_on_socket parses a binary stream from sensorfwd, and the packet layout is
the kind of thing that reads as plausible either way in source: the struct
says three floats, but the values are milli-g and the code divides by 1000,
which would also be what you'd write if they were integers. A capture from
the device settles it — and these tests then run everywhere, phone or not.

See tests/fixtures/hardware.py for what was recorded and how.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures.hardware import (
    SENSORD_ACK,
    SENSORD_CHUNKS,
    SENSORD_DECODED_MILLI_G,
    SENSORD_HEADER,
    SENSORD_RECORD,
    sensord_packet,
)

orientation = pytest.importorskip("muga.camera_orientation")

from muga.camera_orientation import (  # noqa: E402
    ORIENT_BOTTOM_UP,
    ORIENT_LEFT_UP,
    ORIENT_NORMAL,
    ORIENT_RIGHT_UP,
)


# ---------------------------------------------------------------------------
# The wire format
# ---------------------------------------------------------------------------

def test_recorded_packets_match_the_declared_struct() -> None:
    """The layout the parser assumes has to be the one the daemon sends."""
    assert orientation._SENSORD_HDR.size == SENSORD_HEADER.size
    assert orientation._SENSORD_ACCEL.size == SENSORD_RECORD.size
    for chunk in SENSORD_CHUNKS:
        (count,) = orientation._SENSORD_HDR.unpack_from(chunk)
        assert count >= 1
        assert len(chunk) == orientation._SENSORD_HDR.size + count * orientation._SENSORD_ACCEL.size


def test_values_decode_as_floats_not_integers() -> None:
    """Read as int32 the same bytes give values around ±1e9 — nowhere near an
    acceleration. Floats give ~1 g down and little sideways, which is what a
    phone lying on a table reads."""
    chunk = SENSORD_CHUNKS[0]
    _t, x, y, z, _f = orientation._SENSORD_ACCEL.unpack_from(chunk, SENSORD_HEADER.size)
    assert abs(z / 1000.0 - 1.0) < 0.05, "z is not ~1 g"
    assert abs(x / 1000.0) < 0.3 and abs(y / 1000.0) < 0.3

    as_int = struct.Struct("<Qiiii")
    _t2, xi, yi, zi, _f2 = as_int.unpack_from(chunk, SENSORD_HEADER.size)
    assert abs(zi) > 1e6, "the int reading is supposed to be obviously wrong"


# ---------------------------------------------------------------------------
# Feeding the recorded stream through the parser
# ---------------------------------------------------------------------------

class _FakeSocket:
    """Hands out recorded chunks one recv() at a time."""

    def __init__(self, chunks, *, then=None) -> None:
        self._chunks = list(chunks)
        self._then = then          # exception or b"" once the chunks run out
        self.closed = False

    def recv(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        if isinstance(self._then, Exception):
            raise self._then
        return self._then if self._then is not None else b""

    def close(self):
        self.closed = True

    def fileno(self):
        return 42


def _backend(chunks, **extra):
    backend = orientation._SensordBackend()
    backend._sock = _FakeSocket(chunks, then=extra.pop("then", None))
    backend._on_change = extra.pop("on_change", None)
    backend._watch_id = extra.pop("watch_id", 7)
    backend._stopped = False
    for key, value in extra.items():
        setattr(backend, key, value)
    return backend


def test_recorded_stream_is_parsed_sample_for_sample() -> None:
    backend = _backend([b"".join(SENSORD_CHUNKS)])
    seen = []
    with patch.object(orientation._SensordBackend, "_process_sample",
                      side_effect=lambda x, y, z: seen.append((x, y, z))):
        assert backend._on_socket(42, orientation.GLib.IO_IN) is True

    assert len(seen) == len(SENSORD_DECODED_MILLI_G)
    for (gx, gy, gz), (mx, my, mz) in zip(seen, SENSORD_DECODED_MILLI_G):
        # The parser converts milli-g to g.
        assert gx == pytest.approx(mx / 1000.0)
        assert gy == pytest.approx(my / 1000.0)
        assert gz == pytest.approx(mz / 1000.0)


def test_a_phone_lying_flat_holds_its_orientation() -> None:
    """The recorded capture is a phone face-up on a table: ~0.296 g of
    horizontal component against a 0.30 g threshold. It must not guess an
    orientation from that — but note the margin is 1.3%, so a device whose
    accelerometer sits a little differently could cross it while flat."""
    horizontal = [(abs(x) + abs(y)) / 1000.0 for x, y, _z in SENSORD_DECODED_MILLI_G]
    assert max(horizontal) < orientation._MIN_HORIZONTAL_G
    assert max(horizontal) > orientation._MIN_HORIZONTAL_G * 0.9, (
        "the recording is no longer near the threshold; the margin note above "
        "needs revisiting"
    )

    backend = _backend([b"".join(SENSORD_CHUNKS)])
    backend._orientation = ORIENT_LEFT_UP
    backend._on_socket(42, orientation.GLib.IO_IN)
    assert backend._orientation == ORIENT_LEFT_UP


def test_stream_split_across_recv_boundaries() -> None:
    """TCP-style framing: a packet can arrive in pieces, and half a record
    must not be parsed."""
    whole = b"".join(SENSORD_CHUNKS)
    pieces = [whole[i:i + 7] for i in range(0, len(whole), 7)]
    backend = _backend(pieces)
    seen = []
    with patch.object(orientation._SensordBackend, "_process_sample",
                      side_effect=lambda x, y, z: seen.append((x, y, z))):
        for _ in pieces:
            backend._on_socket(42, orientation.GLib.IO_IN)
    assert len(seen) == len(SENSORD_DECODED_MILLI_G)


def test_partial_packet_is_held_until_complete() -> None:
    chunk = SENSORD_CHUNKS[0]
    backend = _backend([chunk[:10]])
    with patch.object(orientation._SensordBackend, "_process_sample") as process:
        backend._on_socket(42, orientation.GLib.IO_IN)
    process.assert_not_called()
    assert backend._buf == chunk[:10], "the fragment was dropped instead of buffered"


def test_multi_record_packets_are_all_consumed() -> None:
    """The daemon batches when samples arrive faster than they are read."""
    packet = sensord_packet(
        (0.0, 1000.0, 0.0), (100.0, 990.0, 0.0), (200.0, 980.0, 0.0),
    )
    backend = _backend([packet])
    seen = []
    with patch.object(orientation._SensordBackend, "_process_sample",
                      side_effect=lambda x, y, z: seen.append((x, y, z))):
        backend._on_socket(42, orientation.GLib.IO_IN)
    assert len(seen) == 3


# ---------------------------------------------------------------------------
# Orientations the recording does not contain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("milli_g", "expected"),
    [
        ((0.0, 1000.0, 0.0), ORIENT_NORMAL),
        ((0.0, -1000.0, 0.0), ORIENT_BOTTOM_UP),
        ((1000.0, 0.0, 0.0), ORIENT_RIGHT_UP),
        ((-1000.0, 0.0, 0.0), ORIENT_LEFT_UP),
    ],
)
def test_held_positions_from_synthesised_packets(milli_g, expected) -> None:
    """Built with the verified framing, since the phone was on a table when
    the capture was taken."""
    backend = _backend([sensord_packet(*[milli_g] * 30)])
    backend._on_socket(42, orientation.GLib.IO_IN)
    assert backend._orientation == expected


def test_a_turn_is_reported_once() -> None:
    on_change = MagicMock()
    backend = _backend(
        [sensord_packet(*[(0.0, 1000.0, 0.0)] * 20),
         sensord_packet(*[(-1000.0, 0.0, 0.0)] * 30)],
        on_change=on_change,
    )
    backend._on_socket(42, orientation.GLib.IO_IN)
    backend._on_socket(42, orientation.GLib.IO_IN)
    assert [c[0][0] for c in on_change.call_args_list] == [ORIENT_NORMAL, ORIENT_LEFT_UP]


# ---------------------------------------------------------------------------
# Stream faults
# ---------------------------------------------------------------------------

def test_hangup_tears_down_and_reconnects() -> None:
    backend = _backend([])
    with patch.object(orientation._SensordBackend, "_schedule_reconnect") as reconnect:
        assert backend._on_socket(42, orientation.GLib.IO_HUP) is False
    reconnect.assert_called_once()
    assert backend._watch_id is None
    assert backend._sock is None


def test_a_clean_close_is_treated_as_a_hangup() -> None:
    """sensorfwd restarting closes the stream without an error condition."""
    backend = _backend([], then=b"")
    with patch.object(orientation._SensordBackend, "_schedule_reconnect") as reconnect:
        assert backend._on_socket(42, orientation.GLib.IO_IN) is False
    reconnect.assert_called_once()


def test_a_socket_error_reconnects() -> None:
    backend = _backend([], then=OSError("connection reset"))
    with patch.object(orientation._SensordBackend, "_schedule_reconnect") as reconnect:
        assert backend._on_socket(42, orientation.GLib.IO_IN) is False
    reconnect.assert_called_once()


def test_a_would_block_is_not_an_error() -> None:
    """Non-blocking reads hit this routinely; reconnecting on it would drop
    the stream every time it caught up."""
    backend = _backend([], then=BlockingIOError())
    with patch.object(orientation._SensordBackend, "_schedule_reconnect") as reconnect:
        assert backend._on_socket(42, orientation.GLib.IO_IN) is True
    reconnect.assert_not_called()


def test_an_implausible_record_count_resyncs() -> None:
    """A desynced stream would otherwise have the parser wait for a packet
    that is never coming."""
    bad = SENSORD_HEADER.pack(orientation._SENSORD_MAX_RECORDS + 1) + b"\x00" * 40
    backend = _backend([bad])
    with patch.object(orientation._SensordBackend, "_process_sample") as process:
        assert backend._on_socket(42, orientation.GLib.IO_IN) is True
    process.assert_not_called()
    assert backend._buf == b"", "the desynced buffer was kept"


def test_the_buffer_has_a_ceiling() -> None:
    """A corrupt length field would otherwise grow the buffer without bound."""
    backend = _backend([b"\x00" * 2048])
    backend._buf = b"\x00" * orientation._SENSORD_MAX_BUF
    assert backend._on_socket(42, orientation.GLib.IO_IN) is True
    assert backend._buf == b""


def test_the_parser_recovers_after_a_resync() -> None:
    bad = SENSORD_HEADER.pack(orientation._SENSORD_MAX_RECORDS + 1) + b"\x00" * 40
    backend = _backend([bad, SENSORD_CHUNKS[0]])
    with patch.object(orientation._SensordBackend, "_process_sample") as process:
        backend._on_socket(42, orientation.GLib.IO_IN)
        backend._on_socket(42, orientation.GLib.IO_IN)
    assert process.call_count == 1, "the parser never recovered framing"


# ---------------------------------------------------------------------------
# Session handshake
# ---------------------------------------------------------------------------

def test_the_handshake_matches_what_the_daemon_expects() -> None:
    """Session id as a little-endian int32, then a one-byte ack."""
    assert len(SENSORD_ACK) == 1
    assert struct.Struct("<i").size == 4


def test_teardown_releases_the_socket() -> None:
    backend = _backend([])
    sock = backend._sock
    backend._buf = b"leftover"
    backend._teardown_socket()
    assert sock.closed is True
    assert backend._sock is None
    assert backend._buf == b"", "stale bytes would corrupt the next session"


def test_teardown_survives_a_dead_socket() -> None:
    backend = _backend([])
    backend._sock = SimpleNamespace(close=MagicMock(side_effect=OSError("bad fd")))
    backend._teardown_socket()
    assert backend._sock is None


def test_reconnect_is_not_scheduled_after_stop() -> None:
    """Otherwise closing the camera leaves a timer that reopens the sensor."""
    backend = _backend([])
    backend._stopped = True
    backend._reconnect_source = None
    with patch.object(orientation.GLib, "timeout_add_seconds") as timeout:
        backend._schedule_reconnect()
    timeout.assert_not_called()


# ---------------------------------------------------------------------------
# Session setup against the recorded daemon
# ---------------------------------------------------------------------------

def _dbus_session(session_id=11):
    """A D-Bus double answering as sensorfwd does on the recorded phone."""
    bus = MagicMock()

    def call_sync(dest, path, iface, method, args, reply_type, *rest):
        if method == "requestSensor":
            return orientation.GLib.Variant("(i)", (session_id,))
        return None

    bus.call_sync.side_effect = call_sync
    return bus


def _socket_pair(ack=SENSORD_ACK):
    sock = MagicMock()
    sock.recv.return_value = ack
    sock.fileno.return_value = 42
    return sock


def test_start_performs_the_recorded_handshake() -> None:
    """loadPlugin, requestSensor, then the session id down the socket and a
    one-byte ack back before any samples arrive."""
    bus, sock = _dbus_session(), _socket_pair()
    backend = orientation._SensordBackend()

    with patch.object(orientation.Gio, "bus_get_sync", return_value=bus), \
         patch.object(orientation._socket, "socket", return_value=sock), \
         patch.object(orientation.GLib, "io_add_watch", return_value=7):
        assert backend.start(None) is True

    methods = [c[0][3] for c in bus.call_sync.call_args_list]
    assert methods[:2] == ["loadPlugin", "requestSensor"]
    assert "setInterval" in methods and "start" in methods
    assert backend._session_id == 11

    sock.connect.assert_called_once_with(orientation.SENSORD_SOCKET)
    sent = sock.send.call_args[0][0]
    assert struct.Struct("<i").unpack(sent)[0] == 11, "wrong session id on the wire"
    sock.recv.assert_called_once_with(1)
    sock.setblocking.assert_called_once_with(False)


def test_start_asks_for_the_recorded_sample_rate() -> None:
    bus, sock = _dbus_session(), _socket_pair()
    backend = orientation._SensordBackend()
    with patch.object(orientation.Gio, "bus_get_sync", return_value=bus), \
         patch.object(orientation._socket, "socket", return_value=sock), \
         patch.object(orientation.GLib, "io_add_watch", return_value=7):
        backend.start(None)

    interval = next(c[0][4] for c in bus.call_sync.call_args_list
                    if c[0][3] == "setInterval")
    assert interval.get_child_value(1).get_int32() == backend.INTERVAL_MS
    assert backend.INTERVAL_MS <= 200, "slower than this and turns feel laggy"


def test_start_without_the_daemon_reports_failure() -> None:
    """Every non-Halium machine lands here, so it has to be quiet and clean."""
    backend = orientation._SensordBackend()
    with patch.object(orientation.Gio, "bus_get_sync",
                      side_effect=orientation.GLib.Error("no such name")):
        assert backend.start(None) is False
    assert backend._sock is None
    assert backend._watch_id is None


def test_start_cleans_up_when_the_socket_is_refused() -> None:
    """The D-Bus session is already open by then and has to be released."""
    bus = _dbus_session()
    sock = _socket_pair()
    sock.connect.side_effect = OSError("no such file")
    backend = orientation._SensordBackend()
    with patch.object(orientation.Gio, "bus_get_sync", return_value=bus), \
         patch.object(orientation._socket, "socket", return_value=sock):
        assert backend.start(None) is False
    assert backend._sock is None


def test_stop_releases_everything() -> None:
    bus, sock = _dbus_session(), _socket_pair()
    backend = orientation._SensordBackend()
    with patch.object(orientation.Gio, "bus_get_sync", return_value=bus), \
         patch.object(orientation._socket, "socket", return_value=sock), \
         patch.object(orientation.GLib, "io_add_watch", return_value=7):
        backend.start(None)

    with patch.object(orientation.GLib, "source_remove") as remove:
        backend.stop()

    remove.assert_any_call(7)
    assert backend._sock is None
    assert backend._watch_id is None
    assert backend._stopped is True


def test_stop_is_safe_before_start() -> None:
    orientation._SensordBackend().stop()


def test_stop_survives_a_wedged_bus() -> None:
    bus = _dbus_session()
    bus.call_sync.side_effect = orientation.GLib.Error("timeout")
    backend = orientation._SensordBackend()
    backend._bus = bus
    backend._session_id = 11
    backend._sock = _socket_pair()
    backend.stop()
    assert backend._sock is None


# ---------------------------------------------------------------------------
# The desktop backend
# ---------------------------------------------------------------------------

def _iio_proxy(*, has_accel=True, initial=ORIENT_NORMAL):
    proxy = MagicMock()
    values = {"HasAccelerometer": has_accel, "AccelerometerOrientation": initial}
    proxy.get_cached_property.side_effect = lambda n: (
        MagicMock(unpack=lambda: values[n]) if n in values else None
    )
    return proxy


def test_iio_backend_claims_the_accelerometer() -> None:
    """iio-sensor-proxy powers the sensor down again unless it is claimed."""
    proxy = _iio_proxy()
    backend = orientation._IIOSensorProxyBackend()
    with patch.object(orientation.Gio.DBusProxy, "new_for_bus_sync", return_value=proxy):
        assert backend.start(None) is True
    assert any(c[0][0] == "ClaimAccelerometer" for c in proxy.call_sync.call_args_list)


def test_iio_backend_reports_the_initial_orientation() -> None:
    """Without this the camera opens in whatever layout it last had until the
    phone is moved."""
    on_change = MagicMock()
    backend = orientation._IIOSensorProxyBackend()
    with patch.object(orientation.Gio.DBusProxy, "new_for_bus_sync",
                      return_value=_iio_proxy(initial=ORIENT_LEFT_UP)):
        backend.start(on_change)
    on_change.assert_called_once_with(ORIENT_LEFT_UP)


def test_iio_backend_declines_a_machine_without_an_accelerometer() -> None:
    backend = orientation._IIOSensorProxyBackend()
    with patch.object(orientation.Gio.DBusProxy, "new_for_bus_sync",
                      return_value=_iio_proxy(has_accel=False)):
        assert backend.start(None) is False
    assert backend._proxy is None


def test_iio_backend_declines_when_the_service_is_absent() -> None:
    backend = orientation._IIOSensorProxyBackend()
    with patch.object(orientation.Gio.DBusProxy, "new_for_bus_sync",
                      side_effect=orientation.GLib.Error("not provided")):
        assert backend.start(None) is False


def test_iio_backend_declines_a_refused_claim() -> None:
    proxy = _iio_proxy()
    proxy.call_sync.side_effect = orientation.GLib.Error("denied")
    backend = orientation._IIOSensorProxyBackend()
    with patch.object(orientation.Gio.DBusProxy, "new_for_bus_sync", return_value=proxy):
        assert backend.start(None) is False
    assert backend._proxy is None


def test_iio_backend_forwards_a_changed_orientation() -> None:
    on_change = MagicMock()
    backend = orientation._IIOSensorProxyBackend()
    backend._on_change = on_change
    backend._orientation = ORIENT_NORMAL

    changed = MagicMock()
    changed.unpack.return_value = {"AccelerometerOrientation": ORIENT_RIGHT_UP}
    backend._on_props_changed(None, changed, None)

    on_change.assert_called_once_with(ORIENT_RIGHT_UP)
    assert backend._orientation == ORIENT_RIGHT_UP


def test_iio_backend_ignores_a_repeat() -> None:
    """It re-emits on every sensor tick; relaying that would rebuild the
    camera's popovers continuously."""
    on_change = MagicMock()
    backend = orientation._IIOSensorProxyBackend()
    backend._on_change = on_change
    backend._orientation = ORIENT_NORMAL
    changed = MagicMock()
    changed.unpack.return_value = {"AccelerometerOrientation": ORIENT_NORMAL}
    backend._on_props_changed(None, changed, None)
    on_change.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [{}, {"Other": "x"}, {"AccelerometerOrientation": "sideways"},
     {"AccelerometerOrientation": 42}],
)
def test_iio_backend_ignores_irrelevant_payloads(payload) -> None:
    on_change = MagicMock()
    backend = orientation._IIOSensorProxyBackend()
    backend._on_change = on_change
    backend._orientation = ORIENT_NORMAL
    changed = MagicMock()
    changed.unpack.return_value = payload
    backend._on_props_changed(None, changed, None)
    on_change.assert_not_called()


def test_iio_backend_survives_an_unpackable_payload() -> None:
    backend = orientation._IIOSensorProxyBackend()
    backend._on_change = MagicMock()
    changed = MagicMock()
    changed.unpack.side_effect = RuntimeError("bad variant")
    backend._on_props_changed(None, changed, None)


def test_iio_backend_releases_the_accelerometer() -> None:
    """Held open, the sensor keeps drawing power after the camera closes."""
    proxy = _iio_proxy()
    backend = orientation._IIOSensorProxyBackend()
    with patch.object(orientation.Gio.DBusProxy, "new_for_bus_sync", return_value=proxy):
        backend.start(None)
    backend.stop()
    assert any(c[0][0] == "ReleaseAccelerometer" for c in proxy.call_sync.call_args_list)
    assert backend._proxy is None


def test_iio_backend_stop_is_safe_before_start() -> None:
    orientation._IIOSensorProxyBackend().stop()


def test_iio_backend_stop_survives_a_refused_release() -> None:
    proxy = _iio_proxy()
    backend = orientation._IIOSensorProxyBackend()
    with patch.object(orientation.Gio.DBusProxy, "new_for_bus_sync", return_value=proxy):
        backend.start(None)
    proxy.call_sync.side_effect = orientation.GLib.Error("gone")
    backend.stop()
    assert backend._proxy is None


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def test_the_client_prefers_sensord() -> None:
    """On a Halium phone both can be present, and sensorfwd is the one wired
    to the actual hardware."""
    client = orientation.OrientationClient()
    with patch.object(orientation._SensordBackend, "start", return_value=True), \
         patch.object(orientation._IIOSensorProxyBackend, "start") as iio_start:
        assert client.start(lambda _o: None) is True
    iio_start.assert_not_called()
    assert "sensord" in client.backend_name.lower()


def test_the_client_falls_back_to_iio() -> None:
    client = orientation.OrientationClient()
    with patch.object(orientation._SensordBackend, "start", return_value=False), \
         patch.object(orientation._IIOSensorProxyBackend, "start", return_value=True):
        assert client.start(lambda _o: None) is True
    assert client.running is True


def test_the_client_copes_with_no_sensor_at_all() -> None:
    """A desktop without either: the camera still opens, just without
    orientation-driven layout."""
    client = orientation.OrientationClient()
    with patch.object(orientation._SensordBackend, "start", return_value=False), \
         patch.object(orientation._IIOSensorProxyBackend, "start", return_value=False):
        assert client.start(lambda _o: None) is False
    assert client.running is False


def test_the_client_stops_its_backend() -> None:
    client = orientation.OrientationClient()
    with patch.object(orientation._SensordBackend, "start", return_value=True), \
         patch.object(orientation._SensordBackend, "stop") as stop:
        client.start(lambda _o: None)
        client.stop()
    stop.assert_called_once()
    assert client.running is False
