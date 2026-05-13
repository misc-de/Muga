"""GeoClue2 client wrapper for opt-in photo geotagging.

Talks to org.freedesktop.GeoClue2 over the system bus via Gio.DBusProxy
so we don't pull in the optional `Geoclue` GIR. Designed to fail soft:
if GeoClue isn't installed, isn't activatable, or refuses our client
(e.g. accuracy policy), `GeoClient.start()` returns False and callers
should treat geotagging as unavailable for this session.

Accuracy levels per the GeoClue spec:
    0 None, 1 Country, 2 City, 3 Neighborhood, 4 Street, 5 Exact
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from gi.repository import Gio, GLib

LOGGER = logging.getLogger(__name__)

GEOCLUE_BUS = "org.freedesktop.GeoClue2"
GEOCLUE_MANAGER_PATH = "/org/freedesktop/GeoClue2/Manager"
GEOCLUE_MANAGER_IFACE = "org.freedesktop.GeoClue2.Manager"
GEOCLUE_CLIENT_IFACE = "org.freedesktop.GeoClue2.Client"
GEOCLUE_LOCATION_IFACE = "org.freedesktop.GeoClue2.Location"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

# Locations older than this are considered stale and not used for geotagging.
LOCATION_TTL_SECONDS = 300


class GeoClient:
    """Wraps a GeoClue2 client object across its lifecycle."""

    def __init__(self, app_id: str = "yaga") -> None:
        self.app_id = app_id
        self._client_proxy: Any = None
        self._client_path: str | None = None
        self._signal_id: int | None = None
        self._location: dict[str, Any] | None = None
        self._on_update: Callable[[dict[str, Any] | None], None] | None = None
        self._on_error: Callable[[str], None] | None = None

    @property
    def running(self) -> bool:
        return self._client_proxy is not None

    def start(
        self,
        accuracy: int = 5,
        on_update: Callable[[dict[str, Any] | None], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> bool:
        self._on_update = on_update
        self._on_error = on_error
        try:
            mgr = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.NONE,
                None,
                GEOCLUE_BUS,
                GEOCLUE_MANAGER_PATH,
                GEOCLUE_MANAGER_IFACE,
                None,
            )
            result = mgr.call_sync(
                "GetClient", None, Gio.DBusCallFlags.NONE, 5000, None
            )
            self._client_path = result.unpack()[0]
        except GLib.Error as exc:
            self._fail(f"GeoClue manager unavailable: {exc.message}")
            return False

        try:
            self._client_proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.NONE,
                None,
                GEOCLUE_BUS,
                self._client_path,
                GEOCLUE_CLIENT_IFACE,
                None,
            )
            self._set_property("DesktopId", GLib.Variant("s", self.app_id))
            # Sensor-Suite uses level 8 (GPS-class precision per newer
            # GeoClue versions; older versions clamp to 5 = EXACT, which
            # is also the highest they expose). DistanceThreshold=0 and
            # TimeThreshold=1 push GeoClue to emit updates as often as
            # the upstream provider can, so a fresh location is available
            # within a second of the geotag toggle being enabled.
            self._set_property(
                "RequestedAccuracyLevel", GLib.Variant("u", max(0, min(8, accuracy)))
            )
            for prop, variant in (
                ("DistanceThreshold", GLib.Variant("u", 0)),
                ("TimeThreshold", GLib.Variant("u", 1)),
            ):
                try:
                    self._set_property(prop, variant)
                except Exception:
                    # Older GeoClue versions don't expose these; fine.
                    pass
            self._signal_id = self._client_proxy.connect("g-signal", self._on_signal)
            self._client_proxy.call_sync(
                "Start", None, Gio.DBusCallFlags.NONE, 5000, None
            )
        except GLib.Error as exc:
            self._fail(f"GeoClue start failed: {exc.message}")
            self.stop()
            return False
        return True

    def stop(self) -> None:
        if self._client_proxy is not None:
            if self._signal_id is not None:
                try:
                    self._client_proxy.disconnect(self._signal_id)
                except Exception:
                    pass
                self._signal_id = None
            try:
                self._client_proxy.call_sync(
                    "Stop", None, Gio.DBusCallFlags.NONE, 2000, None
                )
            except Exception:
                pass
        self._client_proxy = None
        self._client_path = None

    def latest(self) -> dict[str, Any] | None:
        """Return the most recent location dict, or None if missing or
        older than LOCATION_TTL_SECONDS. Keys: lat, lon, alt, accuracy."""
        loc = self._location
        if loc is None:
            return None
        ts = loc.get("timestamp", 0.0)
        if time.time() - ts > LOCATION_TTL_SECONDS:
            return None
        return loc

    # ------------------------------------------------------------------

    def _set_property(self, name: str, value: GLib.Variant) -> None:
        connection = self._client_proxy.get_connection()
        connection.call_sync(
            GEOCLUE_BUS,
            self._client_path,
            DBUS_PROPS_IFACE,
            "Set",
            GLib.Variant("(ssv)", (GEOCLUE_CLIENT_IFACE, name, value)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )

    def _on_signal(
        self, _proxy: Any, _sender: str | None, signal: str, parameters: GLib.Variant
    ) -> None:
        if signal != "LocationUpdated":
            return
        try:
            _old, new = parameters.unpack()
        except Exception:
            return
        if not isinstance(new, str) or not new:
            return
        self._read_location(new)
        if self._on_update is not None:
            try:
                self._on_update(self._location)
            except Exception:
                LOGGER.debug("GeoClient on_update raised", exc_info=True)

    def _read_location(self, path: str) -> None:
        try:
            loc_proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.NONE,
                None,
                GEOCLUE_BUS,
                path,
                GEOCLUE_LOCATION_IFACE,
                None,
            )
        except GLib.Error as exc:
            LOGGER.debug("Could not open Location proxy: %s", exc.message)
            return

        def get(prop: str) -> Any:
            v = loc_proxy.get_cached_property(prop)
            return v.unpack() if v is not None else None

        lat = get("Latitude")
        lon = get("Longitude")
        if lat is None or lon is None:
            return
        self._location = {
            "lat": float(lat),
            "lon": float(lon),
            "alt": float(get("Altitude") or 0.0),
            "accuracy": float(get("Accuracy") or 0.0),
            "speed": float(get("Speed") or 0.0),
            "heading": float(get("Heading") or 0.0),
            "description": get("Description") or "",
            "timestamp": time.time(),
        }

    def _fail(self, message: str) -> None:
        LOGGER.debug("GeoClient: %s", message)
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                LOGGER.debug("GeoClient on_error raised", exc_info=True)
