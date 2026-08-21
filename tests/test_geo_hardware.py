"""GeoClue tests against the D-Bus shapes recorded from a real phone.

Nothing here needs a location fix — the interesting parts are the handshake
(which properties exist and may be written) and what happens when there is no
fix yet, which is the state the recording caught.

One detail the recording settles: with no fix, Client.Location is the root
path "/" rather than an empty string. "No location" is still a non-empty
string, so a truthiness check on it would read as a valid path.

See tests/fixtures/hardware.py for what was recorded and how.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures.hardware import (
    GEOCLUE_CLIENT_PROPERTIES,
    GEOCLUE_GET_CLIENT_SIGNATURE,
    GEOCLUE_NO_LOCATION_PATH,
    GEOCLUE_SETTABLE_PROPERTIES,
)

geo = pytest.importorskip("yaga.camera_geo")

from gi.repository import GLib  # noqa: E402


def _client(**extra):
    client = geo.GeoClient.__new__(geo.GeoClient)
    client.app_id = "yaga"
    client._client_proxy = None
    client._client_path = None
    client._signal_id = None
    client._location = None
    client._on_update = None
    client._on_error = None
    for key, value in extra.items():
        setattr(client, key, value)
    return client


def _proxy(*, location_path=GEOCLUE_NO_LOCATION_PATH):
    proxy = MagicMock()
    proxy.get_connection.return_value = MagicMock()
    proxy.get_cached_property.side_effect = lambda name: (
        GLib.Variant("s", location_path) if name == "Location" else None
    )
    return proxy


# ---------------------------------------------------------------------------
# The recorded interface
# ---------------------------------------------------------------------------

def test_the_properties_the_code_writes_exist_on_the_device() -> None:
    """RequestedAccuracyLevel and DesktopId are written unconditionally; the
    rest are optional tuning the code already treats as best-effort."""
    for name in GEOCLUE_SETTABLE_PROPERTIES:
        assert name in GEOCLUE_CLIENT_PROPERTIES


def test_the_optional_tuning_properties_exist_too() -> None:
    """They are wrapped in try/except for older GeoClue, but on this device
    they are there — so the fast-update path is actually available."""
    for name in ("DistanceThreshold", "TimeThreshold"):
        assert name in GEOCLUE_CLIENT_PROPERTIES


def test_get_client_returns_a_single_object_path() -> None:
    """The code unpacks result[0] as a path; the signature has to match."""
    assert GEOCLUE_GET_CLIENT_SIGNATURE == "(o)"


def test_no_fix_is_a_path_not_an_empty_string() -> None:
    """A truthiness check on Client.Location would read "/" as a valid path."""
    assert GEOCLUE_NO_LOCATION_PATH
    assert GEOCLUE_NO_LOCATION_PATH == "/"


# ---------------------------------------------------------------------------
# Reading a location
# ---------------------------------------------------------------------------

def test_reading_a_location_fills_every_field() -> None:
    values = {"Latitude": 52.52, "Longitude": 13.405, "Altitude": 34.0,
              "Accuracy": 12.5, "Speed": 1.2, "Heading": 90.0,
              "Description": "Berlin"}
    loc_proxy = MagicMock()
    loc_proxy.get_cached_property.side_effect = lambda n: (
        GLib.Variant("d", values[n]) if isinstance(values.get(n), float)
        else GLib.Variant("s", values[n]) if n in values else None
    )
    client = _client()
    with patch.object(geo.Gio.DBusProxy, "new_for_bus_sync", return_value=loc_proxy):
        client._read_location("/org/freedesktop/GeoClue2/Location/0")

    assert client._location["lat"] == pytest.approx(52.52)
    assert client._location["lon"] == pytest.approx(13.405)
    assert client._location["accuracy"] == pytest.approx(12.5)
    assert client._location["description"] == "Berlin"
    assert client._location["timestamp"] > time.time() - 5


def test_reading_a_location_without_coordinates_is_ignored() -> None:
    """A Location object can exist before it carries a position."""
    loc_proxy = MagicMock()
    loc_proxy.get_cached_property.return_value = None
    client = _client()
    with patch.object(geo.Gio.DBusProxy, "new_for_bus_sync", return_value=loc_proxy):
        client._read_location("/org/freedesktop/GeoClue2/Location/0")
    assert client._location is None


def test_reading_a_location_defaults_the_optional_fields() -> None:
    """Altitude and Heading are frequently absent on a Wi-Fi fix."""
    def prop(name):
        if name == "Latitude":
            return GLib.Variant("d", 1.0)
        if name == "Longitude":
            return GLib.Variant("d", 2.0)
        return None

    loc_proxy = MagicMock()
    loc_proxy.get_cached_property.side_effect = prop
    client = _client()
    with patch.object(geo.Gio.DBusProxy, "new_for_bus_sync", return_value=loc_proxy):
        client._read_location("/x")
    assert client._location["alt"] == 0.0
    assert client._location["accuracy"] == 0.0


def test_an_unreachable_location_object_is_survivable() -> None:
    """It can vanish between the signal and the read."""
    client = _client()
    with patch.object(geo.Gio.DBusProxy, "new_for_bus_sync",
                      side_effect=GLib.Error("no such object")):
        client._read_location("/gone")
    assert client._location is None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_a_fresh_client_is_not_running() -> None:
    client = geo.GeoClient()
    assert client.running is False
    assert client.latest() is None


def test_start_reports_a_missing_manager() -> None:
    """GeoClue is absent on plenty of desktops; geotagging then just stays
    off rather than erroring at the user."""
    on_error = MagicMock()
    client = _client()
    with patch.object(geo.Gio.DBusProxy, "new_for_bus_sync",
                      side_effect=GLib.Error("service not found")):
        assert client.start(on_error=on_error) is False
    on_error.assert_called_once()
    assert client.running is False


def test_start_writes_the_recorded_properties() -> None:
    proxy = _proxy()
    connection = proxy.get_connection.return_value
    manager = MagicMock()
    manager.call_sync.return_value = GLib.Variant("(o)", ("/client/1",))

    client = _client()
    with patch.object(geo.Gio.DBusProxy, "new_for_bus_sync",
                      side_effect=[manager, proxy]):
        assert client.start(accuracy=5) is True

    written = [c[0][4].get_child_value(1).get_string()
               for c in connection.call_sync.call_args_list]
    assert "RequestedAccuracyLevel" in written
    for name in written:
        assert name in GEOCLUE_CLIENT_PROPERTIES, f"{name} is not on this device"


def test_start_clamps_the_accuracy_level() -> None:
    """GeoClue's levels run 0-8; anything else is a protocol error."""
    proxy = _proxy()
    connection = proxy.get_connection.return_value
    manager = MagicMock()
    manager.call_sync.return_value = GLib.Variant("(o)", ("/client/1",))

    client = _client()
    with patch.object(geo.Gio.DBusProxy, "new_for_bus_sync",
                      side_effect=[manager, proxy]):
        client.start(accuracy=99)

    levels = [c[0][4].get_child_value(2).get_variant().get_uint32()
              for c in connection.call_sync.call_args_list
              if c[0][4].get_child_value(1).get_string() == "RequestedAccuracyLevel"]
    assert levels == [8]


def test_start_tolerates_missing_tuning_properties() -> None:
    """Older GeoClue has no DistanceThreshold/TimeThreshold — optional."""
    proxy = _proxy()
    connection = proxy.get_connection.return_value

    def selective(bus, path, iface, method, args, *rest):
        name = args.get_child_value(1).get_string()
        if name in ("DistanceThreshold", "TimeThreshold"):
            raise GLib.Error("no such property")
        return None

    connection.call_sync.side_effect = selective
    manager = MagicMock()
    manager.call_sync.return_value = GLib.Variant("(o)", ("/client/1",))

    client = _client()
    with patch.object(geo.Gio.DBusProxy, "new_for_bus_sync",
                      side_effect=[manager, proxy]):
        assert client.start() is True


def test_start_reports_a_refused_start() -> None:
    """Location access can be denied by policy."""
    proxy = _proxy()
    proxy.call_sync.side_effect = GLib.Error("access denied")
    manager = MagicMock()
    manager.call_sync.return_value = GLib.Variant("(o)", ("/client/1",))

    on_error = MagicMock()
    client = _client()
    with patch.object(geo.Gio.DBusProxy, "new_for_bus_sync",
                      side_effect=[manager, proxy]):
        assert client.start(on_error=on_error) is False
    on_error.assert_called_once()
    assert client.running is False, "a failed start left the client half-up"


def test_stop_releases_the_client() -> None:
    proxy = _proxy()
    client = _client(_client_proxy=proxy, _client_path="/client/1", _signal_id=5)
    client.stop()
    proxy.disconnect.assert_called_once_with(5)
    assert client._client_proxy is None
    assert client._client_path is None
    assert client.running is False


def test_stop_survives_a_wedged_bus() -> None:
    """Teardown must not block window-hide on an unresponsive daemon."""
    proxy = _proxy()
    proxy.call_sync.side_effect = GLib.Error("timeout")
    client = _client(_client_proxy=proxy, _signal_id=5)
    client.stop()
    assert client._client_proxy is None


def test_stop_before_start_is_safe() -> None:
    _client().stop()


def test_stop_is_idempotent() -> None:
    client = _client(_client_proxy=_proxy(), _signal_id=1)
    client.stop()
    client.stop()
    assert client.running is False
