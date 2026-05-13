"""Device orientation backend for the camera viewfinder.

Reports a 4-state device orientation matching iio-sensor-proxy's
vocabulary:

  - "normal"     : portrait, top of device up (default "richtig herum")
  - "bottom-up"  : portrait, top down (upside-down, "falsch herum")
  - "left-up"    : landscape, left device edge up
  - "right-up"   : landscape, right device edge up

Two transports, tried in order:

  1. **sensord (com.nokia.SensorService)** — what the Halium / gst-droid
     stack provides on FuriOS / Droidian. We follow the Sensor-Suite
     pattern (https://github.com/misc-de/Sensor-Suite): load + request
     the accelerometer plugin over D-Bus, then read raw samples from
     /run/sensord.sock and classify the 4-state lay from the X/Y axes.
  2. **iio-sensor-proxy (net.hadess.SensorProxy)** — standard on desktop
     Linux distros. Already publishes the 4-state string directly.

Whichever connects, the client exposes the same surface:

    client = OrientationClient()
    if client.start(on_change=lambda orientation: ...):
        # on_change("normal" | "bottom-up" | "left-up" | "right-up")
        # fires after hysteresis on every transition
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

# Public orientation values — match iio-sensor-proxy's vocabulary.
ORIENT_NORMAL = "normal"
ORIENT_BOTTOM_UP = "bottom-up"
ORIENT_LEFT_UP = "left-up"
ORIENT_RIGHT_UP = "right-up"
ALL_ORIENTATIONS = (ORIENT_NORMAL, ORIENT_BOTTOM_UP, ORIENT_LEFT_UP, ORIENT_RIGHT_UP)


def is_landscape(orientation: str) -> bool:
    return orientation in (ORIENT_LEFT_UP, ORIENT_RIGHT_UP)


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
# Sanity caps for the binary protocol on /run/sensord.sock. At 10 Hz a
# packet carries 1–2 records — 1024 is wildly generous. Without these,
# a corrupt `count` field would make the receive buffer grow forever
# (the read loop only consumes bytes once a full record arrives, so
# count=0xFFFFFFFF blocks consumption indefinitely).
_SENSORD_MAX_RECORDS = 1024
_SENSORD_MAX_BUF = 1 << 20  # 1 MiB ceiling on accumulated bytes
# Backoff for sensord-socket reconnect after IO_HUP. Sensord can
# restart (rare, but happens on system updates); we don't want
# orientation to silently die until the user restarts the app.
_SENSORD_RECONNECT_DELAY_MS = 2000

# --- iio-sensor-proxy -----------------------------------------------

IIO_BUS = "net.hadess.SensorProxy"
IIO_PATH = "/net/hadess/SensorProxy"
IIO_IFACE = "net.hadess.SensorProxy"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

# --- classification thresholds --------------------------------------

# Enter landscape only when the X axis clearly dominates (>0.62 of the
# combined horizontal magnitude); exit only when it clearly stops
# dominating (<0.38). Avoids flapping when the phone is held near 45deg.
_LANDSCAPE_ENTER = 0.62
_LANDSCAPE_EXIT = 0.38
# Minimum horizontal magnitude to even classify. If the phone is face-up
# or face-down the gravity vector is along Z and X/Y are both near zero;
# in that case we keep whatever orientation was last in effect.
_MIN_HORIZONTAL_G = 0.30
# Smoothing factor on the (x, y) samples. Higher = snappier but
# jitterier; 0.25 reaches ~63% of a step input in 4 samples.
_EWMA_ALPHA = 0.25


def _classify_orientation(
    smoothed_x: float, smoothed_y: float, current: str
) -> str:
    """Map a smoothed (x, y) acceleration vector to a 4-state
    orientation string with hysteresis. The Y mapping (normal /
    bottom-up) follows the Android-standard convention. The X mapping
    is inverted relative to that convention because the user's HAL
    (verified empirically on a FuriOS / Halium device) reports +X
    when the device is in right-up and -X in left-up — the opposite
    sign of what Android specifies. Inverting here keeps the rest of
    the pipeline (layout, EXIF orientation tag) consistent."""
    ax, ay = abs(smoothed_x), abs(smoothed_y)
    if ax + ay < _MIN_HORIZONTAL_G:
        return current
    ratio_x = ax / (ax + ay + 1e-9)
    is_currently_landscape = current in (ORIENT_LEFT_UP, ORIENT_RIGHT_UP)
    if is_currently_landscape:
        becoming_landscape = ratio_x > _LANDSCAPE_EXIT
    else:
        becoming_landscape = ratio_x > _LANDSCAPE_ENTER
    if becoming_landscape:
        # Sign of X picks which side of the device is up — inverted
        # from the Android convention to match this HAL's mounting.
        return ORIENT_RIGHT_UP if smoothed_x > 0 else ORIENT_LEFT_UP
    # Portrait: sign of Y picks normal vs upside-down.
    return ORIENT_NORMAL if smoothed_y > 0 else ORIENT_BOTTOM_UP


# --------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------


class _SensordBackend:
    """Talks to com.nokia.SensorService for accelerometer samples and
    decides the 4-state orientation from the X/Y axes."""

    INTERVAL_MS = 100  # 10 Hz is plenty for an orientation flip.

    def __init__(self) -> None:
        self._bus: Any = None
        self._sock: _socket.socket | None = None
        self._watch_id: int | None = None
        self._session_id: int | None = None
        self._buf = b""
        self._smoothed_x = 0.0
        self._smoothed_y = 0.0
        self._smoothed_seeded = False
        self._orientation: str | None = None
        self._on_change: Callable[[str], None] | None = None
        self._reconnect_source: int | None = None
        self._stopped = False

    def start(self, on_change: Callable[[str], None] | None) -> bool:
        self._on_change = on_change
        self._stopped = False
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
        self._stopped = True
        if self._reconnect_source is not None:
            try:
                GLib.source_remove(self._reconnect_source)
            except Exception:
                pass
            self._reconnect_source = None
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
        self._orientation = None
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
            LOGGER.debug("sensord socket HUP/ERR; scheduling reconnect")
            self._watch_id = None
            self._teardown_socket()
            self._schedule_reconnect()
            return False
        assert self._sock is not None
        try:
            chunk = self._sock.recv(4096)
            if not chunk:
                # Peer closed the stream cleanly — treat as HUP.
                self._watch_id = None
                self._teardown_socket()
                self._schedule_reconnect()
                return False
            self._buf += chunk
            if len(self._buf) > _SENSORD_MAX_BUF:
                # A corrupt header would otherwise grow the buffer
                # unboundedly. Drop everything and resync on the next
                # header that fits — losing one packet costs at most
                # 100 ms of orientation samples.
                LOGGER.debug(
                    "sensord buffer ceiling hit (%d bytes); dropping", len(self._buf)
                )
                self._buf = b""
                return True
            while len(self._buf) >= _SENSORD_HDR.size:
                (count,) = _SENSORD_HDR.unpack_from(self._buf)
                if count > _SENSORD_MAX_RECORDS:
                    # Implausible record count — protocol desync.
                    # Drop the buffer and let the next chunk
                    # re-establish framing.
                    LOGGER.debug(
                        "sensord protocol desync (count=%d); dropping buffer", count
                    )
                    self._buf = b""
                    break
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
            self._watch_id = None
            self._teardown_socket()
            self._schedule_reconnect()
            return False
        return True

    def _teardown_socket(self) -> None:
        """Drop the socket + buffer without touching the D-Bus session
        — used when reconnect logic wants to redo just the stream side."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._buf = b""

    def _schedule_reconnect(self) -> None:
        if self._stopped:
            return
        if self._reconnect_source is not None:
            return
        self._reconnect_source = GLib.timeout_add(
            _SENSORD_RECONNECT_DELAY_MS, self._reconnect
        )

    def _reconnect(self) -> bool:
        self._reconnect_source = None
        if self._stopped:
            return False
        # Re-run the full handshake. Cheapest path is to call start()
        # again, which already handles the bus / sensor request and
        # the socket connect. The previous D-Bus session id will be
        # stale; releasing it might fail silently — that's fine.
        cb = self._on_change
        try:
            self.stop_for_reconnect()
        except Exception:
            pass
        ok = self.start(cb)
        if not ok:
            LOGGER.debug("sensord reconnect failed; retrying")
            self._schedule_reconnect()
        return False

    def stop_for_reconnect(self) -> None:
        """Like stop() but doesn't set the stopped flag — used by the
        reconnect path which is about to call start() again."""
        was_stopped = self._stopped
        self.stop()
        self._stopped = was_stopped

    def _process_sample(self, x: float, y: float, _z: float) -> None:
        if not self._smoothed_seeded:
            self._smoothed_x = x
            self._smoothed_y = y
            self._smoothed_seeded = True
        else:
            self._smoothed_x += _EWMA_ALPHA * (x - self._smoothed_x)
            self._smoothed_y += _EWMA_ALPHA * (y - self._smoothed_y)
        current = self._orientation if self._orientation is not None else ORIENT_NORMAL
        new = _classify_orientation(self._smoothed_x, self._smoothed_y, current)
        if new != self._orientation:
            LOGGER.debug(
                "orientation sensord -> %s (x=%+.2fg y=%+.2fg)",
                new, self._smoothed_x, self._smoothed_y,
            )
            self._orientation = new
            if self._on_change is not None:
                self._on_change(new)


class _IIOSensorProxyBackend:
    """Desktop fallback — reads the 4-state AccelerometerOrientation
    string from iio-sensor-proxy directly."""

    def __init__(self) -> None:
        self._proxy: Any = None
        self._signal_id: int | None = None
        self._on_change: Callable[[str], None] | None = None
        self._orientation: str | None = None

    def start(self, on_change: Callable[[str], None] | None) -> bool:
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
        if isinstance(initial, str) and initial in ALL_ORIENTATIONS:
            self._orientation = initial
            LOGGER.debug("orientation iio-sensor-proxy -> %s", initial)
            if on_change is not None:
                on_change(initial)
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
        self._orientation = None

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
        if not isinstance(value, str) or value not in ALL_ORIENTATIONS:
            return
        if value == self._orientation:
            return
        LOGGER.debug("orientation iio-sensor-proxy -> %s", value)
        self._orientation = value
        if self._on_change is not None:
            self._on_change(value)


# --------------------------------------------------------------------
# Public client
# --------------------------------------------------------------------


class OrientationClient:
    """Picks the best available accelerometer source and surfaces the
    4-state device orientation via `on_change(orientation: str)`."""

    def __init__(self) -> None:
        self._backend: Any = None
        self._backend_name = ""

    @property
    def running(self) -> bool:
        return self._backend is not None

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def start(
        self, on_change: Callable[[str], None] | None = None
    ) -> bool:
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
