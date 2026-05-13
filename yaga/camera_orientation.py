"""iio-sensor-proxy client for device orientation (Wasserwaage).

Talks to net.hadess.SensorProxy over the system bus via Gio.DBusProxy so
the optional `Iio` GIR isn't required. On phones (Phosh / Mobian /
FuriOS) this is the reliable way to know whether the user is holding the
device in portrait or landscape — relying on the window allocation
doesn't work when the compositor handles rotation by re-transforming the
output rather than reallocating the surface.

Fails soft: if iio-sensor-proxy is missing, refuses our claim, or
reports no accelerometer, `OrientationClient.start()` returns False and
callers should treat orientation as unavailable.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from gi.repository import Gio, GLib

LOGGER = logging.getLogger(__name__)

SENSOR_BUS = "net.hadess.SensorProxy"
SENSOR_PATH = "/net/hadess/SensorProxy"
SENSOR_IFACE = "net.hadess.SensorProxy"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"


def _orientation_is_landscape(value: str) -> bool:
    # net.hadess.SensorProxy reports "normal", "bottom-up", "left-up",
    # "right-up", or "undefined". The first two are portrait on a phone
    # (device top toward / away from the user); the side-up variants are
    # landscape. "undefined" yields False here; callers can fall back to
    # other signals when current() returns None.
    return value in ("left-up", "right-up")


class OrientationClient:
    """Wraps a net.hadess.SensorProxy accelerometer claim and surfaces
    landscape/portrait transitions via a callback."""

    def __init__(self) -> None:
        self._proxy: Any = None
        self._signal_id: int | None = None
        self._current_landscape: bool | None = None
        self._on_change: Callable[[bool], None] | None = None

    @property
    def running(self) -> bool:
        return self._proxy is not None

    @property
    def current_landscape(self) -> bool | None:
        return self._current_landscape

    def start(self, on_change: Callable[[bool], None] | None = None) -> bool:
        self._on_change = on_change
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.NONE,
                None,
                SENSOR_BUS,
                SENSOR_PATH,
                SENSOR_IFACE,
                None,
            )
        except GLib.Error as exc:
            LOGGER.debug("iio-sensor-proxy unavailable: %s", exc.message)
            self._proxy = None
            return False

        has_accel = self._get_property("HasAccelerometer")
        if not has_accel:
            LOGGER.debug("iio-sensor-proxy: no accelerometer")
            self._proxy = None
            return False

        try:
            self._proxy.call_sync(
                "ClaimAccelerometer", None, Gio.DBusCallFlags.NONE, 5000, None
            )
        except GLib.Error as exc:
            LOGGER.debug("ClaimAccelerometer failed: %s", exc.message)
            self._proxy = None
            return False

        self._signal_id = self._proxy.connect(
            "g-properties-changed", self._on_props_changed
        )
        # Seed initial value so callers don't have to wait for the first
        # change signal to know the orientation.
        initial = self._get_property("AccelerometerOrientation")
        if isinstance(initial, str) and initial != "undefined":
            self._current_landscape = _orientation_is_landscape(initial)
            if on_change is not None:
                on_change(self._current_landscape)
        return True

    def stop(self) -> None:
        if self._proxy is None:
            return
        if self._signal_id is not None:
            try:
                self._proxy.disconnect(self._signal_id)
            except Exception:
                pass
            self._signal_id = None
        try:
            self._proxy.call_sync(
                "ReleaseAccelerometer", None, Gio.DBusCallFlags.NONE, 5000, None
            )
        except GLib.Error as exc:
            LOGGER.debug("ReleaseAccelerometer failed: %s", exc.message)
        self._proxy = None
        self._current_landscape = None

    # ----- internals -------------------------------------------------

    def _get_property(self, name: str) -> Any:
        if self._proxy is None:
            return None
        try:
            value = self._proxy.get_cached_property(name)
        except Exception:
            value = None
        if value is None:
            # Fall back to a Properties.Get if the proxy's local cache
            # hasn't populated yet — happens immediately after creation.
            try:
                props = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SYSTEM,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    SENSOR_BUS,
                    SENSOR_PATH,
                    DBUS_PROPS_IFACE,
                    None,
                )
                result = props.call_sync(
                    "Get",
                    GLib.Variant("(ss)", (SENSOR_IFACE, name)),
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                )
                return result.unpack()[0]
            except GLib.Error:
                return None
        return value.unpack()

    def _on_props_changed(
        self, _proxy: Any, changed: Any, _invalidated: Any
    ) -> None:
        try:
            payload = changed.unpack()
        except Exception:
            return
        if "AccelerometerOrientation" not in payload:
            return
        value = payload["AccelerometerOrientation"]
        if not isinstance(value, str) or value == "undefined":
            return
        landscape = _orientation_is_landscape(value)
        if landscape == self._current_landscape:
            return
        self._current_landscape = landscape
        if self._on_change is not None:
            self._on_change(landscape)
