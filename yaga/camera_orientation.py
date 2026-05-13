"""Device orientation backend for the camera viewfinder.

Tries two transports, in order:

  1. **sensord (com.nokia.SensorService).** This is what the Halium /
     gst-droid stack provides on FuriOS, Droidian, etc. — phones whose
     accelerometer is exposed via the Nokia SensorService daemon (`sensord`).
     We follow the same pattern Sensor-Suite uses
     (https://github.com/misc-de/Sensor-Suite):
       - load + request the accelerometer plugin over D-Bus on the
         system bus,
       - then read raw samples from /run/sensord.sock (binary protocol),
       - and stop/release on close.

  2. **iio-sensor-proxy (net.hadess.SensorProxy).** Standard on most
     desktop Linux distros. Reports an `AccelerometerOrientation` string
     directly, so we don't have to interpret raw axes.

Whichever connects, the client exposes the same surface:

    client = OrientationClient()
    if client.start(on_change=lambda landscape: ...):
        # transitions fire on_change(True/False) after hysteresis
    client.stop()

If neither backend is available `start()` returns False and the caller
should fall back to a window-size heuristic.
"""
from __future__ import annotations

import logging
import os
import socket as _socket
import struct
from typing import Any, Callable

from gi.repository import Gio, GLib

LOGGER = logging.getLogger(__name__)

# --- sensord (Nokia SensorService) ----------------------------------

SENSORD_BUS = "com.nokia.SensorService"
SENSORD_MANAGER_PATH = "/SensorManager"
SENSORD_MANAGER_IFACE = "local.SensorManager"
SENSORD_ACCEL_PATH = "/SensorManager/accelerometersensor"
SENSORD_ACCEL_IFACE = "local.AccelerometerSensor"
SENSORD_SOCKET = "/run/sensord.sock"

# Binary stream format on /run/sensord.sock:
#   header: uint32 little-endian = number of records that follow
#   record: uint64 timestamp, float32 x, float32 y, float32 z, int32 pad
# Axis values are in mG (divide by 1000 for G).
_SENSORD_HDR = struct.Struct("<I")
_SENSORD_ACCEL = struct.Struct("<Qfffi")

# --- iio-sensor-proxy -----------------------------------------------

IIO_BUS = "net.hadess.SensorProxy"
IIO_PATH = "/net/hadess/SensorProxy"
IIO_IFACE = "net.hadess.SensorProxy"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

# --- orientation classification -------------------------------------

# Hysteresis thresholds for |x|/(|x|+|y|). Enter landscape only when
# x dominates clearly (>0.62), exit landscape only when y dominates
# clearly (<0.38). Avoids flapping when the phone is held near 45 deg.
_LANDSCAPE_ENTER = 0.62
_LANDSCAPE_EXIT = 0.38
# Minimum horizontal-axis magnitude to even classify. If the phone is
# face-up or face-down, gravity is along z and x/y are both near zero;
# keep whatever orientation was last in effect.
_MIN_HORIZONTAL_G = 0.30
# Exponential smoothing factor on the (x, y) samples. Higher = snappier
# but jitterier; 0.25 reaches ~63% of a step input in 4 samples.
_EWMA_ALPHA = 0.25


def _classify_landscape(
    smoothed_x: float, smoothed_y: float, current_landscape: bool
) -> bool:
    ax, ay = abs(smoothed_x), abs(smoothed_y)
    if ax + ay < _MIN_HORIZONTAL_G:
        return current_landscape
    ratio = ax / (ax + ay + 1e-9)
    if current_landscape:
        return ratio > _LANDSCAPE_EXIT
    return ratio > _LANDSCAPE_ENTER


# --------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------


class _SensordBackend:
    """Talks to com.nokia.SensorService for accelerometer samples and
    decides portrait/landscape from the X/Y axes."""

    INTERVAL_MS = 100  # 10 Hz is enough for an orientation flip.

    def __init__(self) -> None:
        self._bus: Any = None
        self._sock: _socket.socket | None = None
        self._watch_id: int | None = None
        self._session_id: int | None = None
        self._buf = b""
        self._smoothed_x = 0.0
        self._smoothed_y = 0.0
        self._smoothed_seeded = False
        self._landscape: bool | None = None
        self._on_change: Callable[[bool], None] | None = None

    def start(self, on_change: Callable[[bool], None] | None) -> bool:
        self._on_change = on_change
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            pid = os.getpid()
            self._call(
                SENSORD_MANAGER_PATH, SENSORD_MANAGER_IFACE, "loadPlugin",
                GLib.Variant("(s)", ("accelerometersensor",)),
            )
            res = self._call(
                SENSORD_MANAGER_PATH, SENSORD_MANAGER_IFACE, "requestSensor",
                GLib.Variant("(sx)", ("accelerometersensor", pid)),
                GLib.VariantType.new("(i)"),
            )
            self._session_id = res.get_child_value(0).get_int32()

            self._sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            self._sock.connect(SENSORD_SOCKET)
            # The protocol requires us to identify by session id then
            # read a 1-byte ack before the actual stream starts.
            self._sock.send(struct.pack("<i", self._session_id))
            self._sock.recv(1)
            self._sock.setblocking(False)
            self._watch_id = GLib.io_add_watch(
                self._sock.fileno(),
                GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                self._on_socket,
            )

            self._call(
                SENSORD_ACCEL_PATH, SENSORD_ACCEL_IFACE, "setInterval",
                GLib.Variant("(ii)", (self._session_id, self.INTERVAL_MS)),
            )
            self._call(
                SENSORD_ACCEL_PATH, SENSORD_ACCEL_IFACE, "start",
                GLib.Variant("(i)", (self._session_id,)),
            )
            return True
        except Exception as exc:
            LOGGER.debug("sensord backend not available: %s", exc)
            self.stop()
            return False

    def stop(self) -> None:
        if self._watch_id is not None:
            try:
                GLib.source_remove(self._watch_id)
            except Exception:
                pass
            self._watch_id = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._bus is not None and self._session_id is not None:
            pid = os.getpid()
            for path, iface, method, args in (
                (
                    SENSORD_ACCEL_PATH, SENSORD_ACCEL_IFACE, "stop",
                    GLib.Variant("(i)", (self._session_id,)),
                ),
                (
                    SENSORD_MANAGER_PATH, SENSORD_MANAGER_IFACE, "releaseSensor",
                    GLib.Variant(
                        "(six)", ("accelerometersensor", self._session_id, pid)
                    ),
                ),
            ):
                try:
                    self._call(path, iface, method, args)
                except Exception:
                    pass
        self._bus = None
        self._session_id = None
        self._buf = b""
        self._landscape = None
        self._smoothed_seeded = False

    # --- internals ----------------------------------------------------

    def _call(
        self,
        path: str,
        iface: str,
        method: str,
        args: Any = None,
        reply_type: Any = None,
    ) -> Any:
        return self._bus.call_sync(
            SENSORD_BUS, path, iface, method, args,
            reply_type, Gio.DBusCallFlags.NONE, 3000, None,
        )

    def _on_socket(self, _fd: int, condition: int) -> bool:
        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            self._watch_id = None
            return False
        assert self._sock is not None
        try:
            chunk = self._sock.recv(4096)
            if not chunk:
                return True
            self._buf += chunk
            while len(self._buf) >= _SENSORD_HDR.size:
                (count,) = _SENSORD_HDR.unpack_from(self._buf)
                need = _SENSORD_HDR.size + count * _SENSORD_ACCEL.size
                if len(self._buf) < need:
                    break
                for i in range(count):
                    _, x, y, z, _flags = _SENSORD_ACCEL.unpack_from(
                        self._buf, _SENSORD_HDR.size + i * _SENSORD_ACCEL.size
                    )
                    self._process_sample(x / 1000.0, y / 1000.0, z / 1000.0)
                self._buf = self._buf[need:]
        except BlockingIOError:
            pass
        except Exception as exc:
            LOGGER.debug("sensord socket error: %s", exc)
            return False
        return True

    def _process_sample(self, x: float, y: float, _z: float) -> None:
        if not self._smoothed_seeded:
            self._smoothed_x = x
            self._smoothed_y = y
            self._smoothed_seeded = True
        else:
            self._smoothed_x += _EWMA_ALPHA * (x - self._smoothed_x)
            self._smoothed_y += _EWMA_ALPHA * (y - self._smoothed_y)
        current = self._landscape if self._landscape is not None else False
        landscape = _classify_landscape(
            self._smoothed_x, self._smoothed_y, current
        )
        if landscape != self._landscape:
            self._landscape = landscape
            if self._on_change is not None:
                self._on_change(landscape)


class _IIOSensorProxyBackend:
    """Fallback for desktop Linux — reads the high-level
    AccelerometerOrientation string from iio-sensor-proxy."""

    def __init__(self) -> None:
        self._proxy: Any = None
        self._signal_id: int | None = None
        self._on_change: Callable[[bool], None] | None = None
        self._landscape: bool | None = None

    def start(self, on_change: Callable[[bool], None] | None) -> bool:
        self._on_change = on_change
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.NONE,
                None,
                IIO_BUS, IIO_PATH, IIO_IFACE, None,
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
                "ClaimAccelerometer", None,
                Gio.DBusCallFlags.NONE, 5000, None,
            )
        except GLib.Error as exc:
            LOGGER.debug("ClaimAccelerometer failed: %s", exc.message)
            self._proxy = None
            return False
        self._signal_id = self._proxy.connect(
            "g-properties-changed", self._on_props_changed
        )
        initial = self._get_property("AccelerometerOrientation")
        if isinstance(initial, str) and initial != "undefined":
            self._landscape = initial in ("left-up", "right-up")
            if on_change is not None:
                on_change(self._landscape)
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
                "ReleaseAccelerometer", None,
                Gio.DBusCallFlags.NONE, 5000, None,
            )
        except GLib.Error as exc:
            LOGGER.debug("ReleaseAccelerometer failed: %s", exc.message)
        self._proxy = None
        self._landscape = None

    def _get_property(self, name: str) -> Any:
        if self._proxy is None:
            return None
        try:
            value = self._proxy.get_cached_property(name)
        except Exception:
            value = None
        if value is None:
            try:
                props = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SYSTEM,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    IIO_BUS, IIO_PATH, DBUS_PROPS_IFACE, None,
                )
                result = props.call_sync(
                    "Get",
                    GLib.Variant("(ss)", (IIO_IFACE, name)),
                    Gio.DBusCallFlags.NONE, 5000, None,
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
        landscape = value in ("left-up", "right-up")
        if landscape == self._landscape:
            return
        self._landscape = landscape
        if self._on_change is not None:
            self._on_change(landscape)


# --------------------------------------------------------------------
# Public client
# --------------------------------------------------------------------


class OrientationClient:
    """Picks the best available accelerometer source and surfaces
    landscape/portrait transitions via an `on_change(landscape: bool)`
    callback."""

    def __init__(self) -> None:
        self._backend: Any = None
        self._backend_name = ""

    @property
    def running(self) -> bool:
        return self._backend is not None

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def start(self, on_change: Callable[[bool], None] | None = None) -> bool:
        # sensord first (Halium phones), then iio-sensor-proxy (desktop).
        for cls, name in (
            (_SensordBackend, "sensord"),
            (_IIOSensorProxyBackend, "iio-sensor-proxy"),
        ):
            backend = cls()
            if backend.start(on_change):
                self._backend = backend
                self._backend_name = name
                LOGGER.debug("orientation backend: %s", name)
                return True
        self._backend = None
        self._backend_name = ""
        return False

    def stop(self) -> None:
        if self._backend is None:
            return
        try:
            self._backend.stop()
        except Exception:
            pass
        self._backend = None
        self._backend_name = ""
