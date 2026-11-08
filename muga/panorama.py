"""360° photos: recognising them and showing them as a sphere.

A 360° photo is an ordinary image in the *equirectangular* projection — the
whole sphere unrolled onto a 2:1 rectangle, 360° across and 180° down — that
says so in its XMP metadata, using Google's Photo Sphere schema (namespace
``http://ns.google.com/photos/1.0/panorama/``, conventionally ``GPano``).
Cameras (Ricoh Theta, Insta360, GoPro), phone panorama modes and the stitching
tools all write it; Google Photos, Facebook and Flickr all read it. A
panorama that covers less than the full sphere says where its pixels sit in
the full one (the ``CroppedArea*`` and ``FullPano*`` fields).

Three parts, usable on their own:

* :func:`read_panorama_info` — the XMP check, pure Python, no GTK.
* :class:`PanoramaView` — a ``Gtk.GLArea`` that draws the sphere around the
  viewer. OpenGL is reached through libepoxy with ``ctypes``: GTK 4 links
  epoxy and dispatches through it, so it is already loaded, works with
  desktop GL and with GLES alike (the phone runs GLES through libhybris), and
  needs no PyOpenGL — nothing new to ship in the Flatpak.
* :class:`MotionSource` — the phone's orientation from the Plinius sensor
  bridge (``org.plinius.SensorProxyBridge.Gravity``, signal ``Orientation``:
  the sensor hub's game rotation vector, gyroscope and accelerometer fused).
  Elsewhere it is simply unavailable and the view follows the fingers only.

Coordinates, shared by the shader and the matrices below: the viewer stands
at the centre, y points up, -z points at the middle of the picture (longitude
0) and x to the right. Longitude grows to the right, latitude upwards.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

LOGGER = logging.getLogger(__name__)

GPANO_NS = "http://ns.google.com/photos/1.0/panorama/"

# The XMP packet of a JPEG sits in an APP1 segment near the start of the file
# (a segment holds at most 64 KiB); EXIF and an ICC profile can come first.
# Reading this much finds it without decoding anything.
_XMP_SCAN_BYTES = 512 * 1024

# Long-edge cap for the sphere's texture. A phone screen 2400 px tall that
# shows 100° of the sphere needs about 8600 px around the full circle to be
# drawn one texel per pixel; 8192 is the limit of many mobile GPUs.
PANORAMA_MAX_DIM = 8192


# ---------------------------------------------------------------------------
# Recognising a 360° photo
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PanoramaInfo:
    """Where the picture lies on the sphere, as fractions of the full pano.

    ``left``/``top`` are the offset of the image inside the full panorama,
    ``width``/``height`` its share of it (1.0 / 1.0 for a complete sphere).
    ``initial_*`` is the view the author asked to open with (degrees,
    ``initial_heading`` relative to the middle of the full panorama).
    ``pose_heading`` is the compass bearing the middle of the panorama faces,
    when the camera recorded it: with a compass on the phone the sphere then
    lies the way the world did.
    """

    left: float = 0.0
    top: float = 0.0
    width: float = 1.0
    height: float = 1.0
    initial_heading: float = 0.0
    initial_pitch: float = 0.0
    initial_fov: float | None = None
    pose_heading: float | None = None

    @property
    def wraps(self) -> bool:
        """True when the picture goes all the way round (no seam to stop at)."""
        return self.width >= 0.999


def _xmp_fields(xmp: str) -> dict[str, str]:
    """The GPano fields of an XMP packet, in attribute or element form.

    Both forms are legal RDF and both occur in the wild::

        <rdf:Description GPano:ProjectionType="equirectangular" .../>
        <GPano:ProjectionType>equirectangular</GPano:ProjectionType>

    The prefix is whatever the packet binds the namespace to — usually, but
    not necessarily, ``GPano``.
    """
    prefixes = set(re.findall(r'xmlns:([\w.-]+)\s*=\s*["\']' + re.escape(GPANO_NS) + r'["\']', xmp))
    fields: dict[str, str] = {}
    for prefix in prefixes:
        p = re.escape(prefix)
        for key, value in re.findall(p + r':(\w+)\s*=\s*"([^"]*)"', xmp):
            fields.setdefault(key, value)
        for key, value in re.findall(p + r":(\w+)\s*=\s*'([^']*)'", xmp):
            fields.setdefault(key, value)
        for key, value in re.findall(r"<" + p + r":(\w+)>\s*([^<]*?)\s*</" + p + r":\1>", xmp):
            fields.setdefault(key, value)
    return fields


def _number(fields: dict[str, str], key: str) -> float | None:
    try:
        value = float(fields[key])
    except (KeyError, ValueError):
        return None
    return value if math.isfinite(value) else None


def panorama_info_from_xmp(xmp: str) -> PanoramaInfo | None:
    """Parse an XMP packet; ``None`` unless it describes an equirectangular pano."""
    fields = _xmp_fields(xmp)
    if fields.get("ProjectionType", "").strip().lower() != "equirectangular":
        return None
    full_w = _number(fields, "FullPanoWidthPixels")
    full_h = _number(fields, "FullPanoHeightPixels")
    crop_w = _number(fields, "CroppedAreaImageWidthPixels")
    crop_h = _number(fields, "CroppedAreaImageHeightPixels")
    left = _number(fields, "CroppedAreaLeftPixels") or 0.0
    top = _number(fields, "CroppedAreaTopPixels") or 0.0
    info = {}
    # Without the full-pano size the picture is taken to be the whole sphere;
    # nonsense values (zero, negative, a crop outside the pano) are too.
    if full_w and full_h and full_w > 0 and full_h > 0:
        crop_w = crop_w if crop_w and 0 < crop_w <= full_w else full_w
        crop_h = crop_h if crop_h and 0 < crop_h <= full_h else full_h
        left = min(max(left, 0.0), full_w - crop_w)
        top = min(max(top, 0.0), full_h - crop_h)
        info = dict(left=left / full_w, top=top / full_h, width=crop_w / full_w, height=crop_h / full_h)
    heading = 0.0
    view_heading = _number(fields, "InitialViewHeadingDegrees")
    if view_heading is not None:
        # Both headings are compass bearings; the view's position in the
        # picture is their difference. Without the pose, the middle of the
        # picture is assumed to face north.
        heading = view_heading - (_number(fields, "PoseHeadingDegrees") or 0.0)
        heading = (heading + 180.0) % 360.0 - 180.0
    pose = _number(fields, "PoseHeadingDegrees")
    pitch = _number(fields, "InitialViewPitchDegrees") or 0.0
    fov = _number(fields, "InitialHorizontalFOVDegrees")
    if fov is not None and not 10.0 <= fov <= 170.0:
        fov = None
    return PanoramaInfo(initial_heading=heading, initial_pitch=max(-90.0, min(90.0, pitch)),
                        initial_fov=fov, pose_heading=None if pose is None else pose % 360.0, **info)


def read_panorama_info(path: str) -> PanoramaInfo | None:
    """``PanoramaInfo`` for a 360° photo at *path*, else ``None``.

    Only the start of the file is read: the XMP packet is located by its
    ``<x:xmpmeta`` wrapper rather than by walking JPEG segments, which also
    covers PNG, WebP and HEIF files that carry the same packet. Never raises.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_XMP_SCAN_BYTES)
    except OSError:
        return None
    if GPANO_NS.encode() not in head:
        return None
    start = head.find(b"<x:xmpmeta")
    end = head.find(b"</x:xmpmeta>", start)
    if start < 0 or end < 0:
        return None
    try:
        xmp = head[start:end].decode("utf-8", errors="replace")
        return panorama_info_from_xmp(xmp)
    except Exception:  # a malformed packet is simply not a panorama
        LOGGER.debug("unreadable XMP in %s", path, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Rotations (3×3, row-major nested tuples)
# ---------------------------------------------------------------------------

Matrix = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
IDENTITY: Matrix = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def mat_mul(a: Matrix, b: Matrix) -> Matrix:
    return tuple(  # type: ignore[return-value]
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3)
    )


def mat_apply(m: Matrix, v: Sequence[float]) -> tuple[float, float, float]:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def rot_x(deg: float) -> Matrix:
    """Turns -z (straight ahead) upwards for a positive angle."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))


def rot_y(deg: float) -> Matrix:
    """Right-handed turn about the vertical: lowers the longitude by *deg*."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))


def rot_z(deg: float) -> Matrix:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def view_matrix(yaw: float, pitch: float) -> Matrix:
    """Camera → world for a view *yaw* degrees right of the middle, *pitch* up."""
    return mat_mul(rot_y(-yaw), rot_x(pitch))


def longitude_of(v: Sequence[float]) -> float:
    """Degrees right of the middle of the picture."""
    return math.degrees(math.atan2(v[0], -v[2]))


def latitude_of(v: Sequence[float]) -> float:
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return math.degrees(math.asin(max(-1.0, min(1.0, v[1] / n))))


def quaternion_matrix(x: float, y: float, z: float, w: float) -> Matrix:
    """Rotation of a unit quaternion (Android order: x, y, z, then w)."""
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


# Android's sensor world (x east, y north, z up) in the sphere's frame (y up,
# north straight ahead at -z). The game rotation vector has no real north —
# its "north" is wherever the sensor hub started — so the view is re-centred
# on the picture's initial heading when motion begins.
ANDROID_TO_SPHERE: Matrix = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))


def device_view(quaternion: Sequence[float], screen_angle: int) -> Matrix:
    """Camera → world for the phone's orientation, looking out of its back.

    The device frame (x right, y to the top edge, z out of the screen) is the
    camera frame when the picture is shown upright in portrait;
    *screen_angle* turns the screen's axes onto the device's when it is held
    sideways (90: the device's left edge is up, 270: its right edge).
    """
    return mat_mul(mat_mul(ANDROID_TO_SPHERE, quaternion_matrix(*quaternion)), rot_z(screen_angle))


def screen_angle_for(quaternion: Sequence[float], landscape: bool, current: int) -> int:
    """Which way the screen's axes lie on the device, for the view's shape.

    Muga leaves the picture alone while the sphere follows the phone, and
    the compositor turns its output only by whole quarters: a portrait view
    is the device upright (0), a landscape one has the left (90) or the right
    edge (270) up, whichever gravity says. Lying flat keeps *current* when
    that fits the shape. Called when the view's shape changes, not per frame,
    so the sphere turns its axes exactly when the screen does.
    """
    if not landscape:
        return 0
    up = mat_apply(tuple(zip(*quaternion_matrix(*quaternion))), (0.0, 0.0, 1.0))  # type: ignore[arg-type]
    if abs(up[0]) < 0.17 and current in (90, 270):  # within ~10° of flat across
        return current
    return 90 if up[0] < 0 else 270


# ---------------------------------------------------------------------------
# OpenGL through libepoxy
# ---------------------------------------------------------------------------

GL_COLOR_BUFFER_BIT = 0x4000
GL_TRIANGLE_STRIP = 0x0005
GL_UNSIGNED_BYTE = 0x1401
GL_FLOAT = 0x1406
GL_RGB = 0x1907
GL_RGBA = 0x1908
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE0 = 0x84C0
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_UNPACK_ALIGNMENT = 0x0CF5
GL_MAX_TEXTURE_SIZE = 0x0D33
GL_VIEWPORT = 0x0BA2
GL_ARRAY_BUFFER = 0x8892
GL_STATIC_DRAW = 0x88E4
GL_FRAGMENT_SHADER = 0x8B30
GL_VERTEX_SHADER = 0x8B31
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82

_u = ctypes.c_uint
_i = ctypes.c_int
_f = ctypes.c_float
_p = ctypes.c_void_p
_ip = ctypes.POINTER(ctypes.c_int)
_up = ctypes.POINTER(ctypes.c_uint)

# name: (restype, argtypes)
_GL_FUNCTIONS: dict[str, tuple[object, tuple[object, ...]]] = {
    "glActiveTexture": (None, (_u,)),
    "glAttachShader": (None, (_u, _u)),
    "glBindAttribLocation": (None, (_u, _u, ctypes.c_char_p)),
    "glBindBuffer": (None, (_u, _u)),
    "glBindTexture": (None, (_u, _u)),
    "glBindVertexArray": (None, (_u,)),
    "glBufferData": (None, (_u, ctypes.c_ssize_t, _p, _u)),
    "glClear": (None, (_u,)),
    "glClearColor": (None, (_f, _f, _f, _f)),
    "glCompileShader": (None, (_u,)),
    "glCreateProgram": (_u, ()),
    "glCreateShader": (_u, (_u,)),
    "glDeleteBuffers": (None, (_i, _up)),
    "glDeleteProgram": (None, (_u,)),
    "glDeleteShader": (None, (_u,)),
    "glDeleteTextures": (None, (_i, _up)),
    "glDeleteVertexArrays": (None, (_i, _up)),
    "glDisable": (None, (_u,)),
    "glDrawArrays": (None, (_u, _i, _i)),
    "glEnableVertexAttribArray": (None, (_u,)),
    "glGenBuffers": (None, (_i, _up)),
    "glGenTextures": (None, (_i, _up)),
    "glGenVertexArrays": (None, (_i, _up)),
    "glGetIntegerv": (None, (_u, _ip)),
    "glGetProgramInfoLog": (None, (_u, _i, _ip, ctypes.c_char_p)),
    "glGetProgramiv": (None, (_u, _u, _ip)),
    "glGetShaderInfoLog": (None, (_u, _i, _ip, ctypes.c_char_p)),
    "glGetShaderiv": (None, (_u, _u, _ip)),
    "glGetUniformLocation": (_i, (_u, ctypes.c_char_p)),
    "glLinkProgram": (None, (_u,)),
    "glPixelStorei": (None, (_u, _i)),
    "glShaderSource": (None, (_u, _i, ctypes.POINTER(ctypes.c_char_p), _ip)),
    "glTexImage2D": (None, (_u, _i, _i, _i, _i, _i, _u, _u, _p)),
    "glTexParameteri": (None, (_u, _u, _i)),
    "glUniform1f": (None, (_i, _f)),
    "glUniform1i": (None, (_i, _i)),
    "glUniform2f": (None, (_i, _f, _f)),
    "glUniform4f": (None, (_i, _f, _f, _f, _f)),
    "glUniformMatrix3fv": (None, (_i, _i, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_float))),
    "glUseProgram": (None, (_u,)),
    "glVertexAttribPointer": (None, (_u, _i, _u, ctypes.c_ubyte, _i, _p)),
    "glViewport": (None, (_i, _i, _i, _i)),
}


class _EpoxyFunction:
    """One GL entry point, called through epoxy's dispatch pointer.

    epoxy exports every function as a pointer variable (``epoxy_glClear``)
    that starts out at a resolver stub and is rewritten to the driver's
    function on the first call. The pointer is therefore read again after
    that call, so later calls go straight to the driver.
    """

    def __init__(self, lib: ctypes.CDLL, name: str, restype, argtypes) -> None:
        self._var = ctypes.c_void_p.in_dll(lib, "epoxy_" + name)
        self._proto = ctypes.CFUNCTYPE(restype, *argtypes)
        self._fn: Any = None
        self.name = name

    def __call__(self, *args):
        if self._fn is None:
            if not self._var.value:
                raise RuntimeError(f"{self.name} is not available")
            result = self._proto(self._var.value)(*args)
            self._fn = self._proto(self._var.value)
            return result
        return self._fn(*args)


class GL:
    """The handful of GL calls the sphere needs. Raises OSError without epoxy."""

    _lib: ctypes.CDLL | None = None

    if TYPE_CHECKING:
        # The entry points are set per instance from _GL_FUNCTIONS
        def __getattr__(self, name: str) -> Callable[..., Any]: ...

    def __init__(self) -> None:
        if GL._lib is None:
            name = ctypes.util.find_library("epoxy") or "libepoxy.so.0"
            GL._lib = ctypes.CDLL(name)
        for fname, (restype, argtypes) in _GL_FUNCTIONS.items():
            setattr(self, fname, _EpoxyFunction(GL._lib, fname, restype, argtypes))

    def integer(self, pname: int, count: int = 1) -> list[int]:
        values = (ctypes.c_int * count)()
        self.glGetIntegerv(pname, values)
        return list(values)

    def compile(self, kind: int, source: str) -> int:
        shader = self.glCreateShader(kind)
        src = ctypes.c_char_p(source.encode())
        self.glShaderSource(shader, 1, ctypes.byref(src), None)
        self.glCompileShader(shader)
        ok = ctypes.c_int(0)
        self.glGetShaderiv(shader, GL_COMPILE_STATUS, ctypes.byref(ok))
        if not ok.value:
            log = ctypes.create_string_buffer(2048)
            self.glGetShaderInfoLog(shader, 2048, None, log)
            self.glDeleteShader(shader)
            raise RuntimeError("shader does not compile: " + log.value.decode(errors="replace"))
        return shader

    def program(self, vertex: str, fragment: str) -> int:
        vs = self.compile(GL_VERTEX_SHADER, vertex)
        fs = self.compile(GL_FRAGMENT_SHADER, fragment)
        prog = self.glCreateProgram()
        self.glAttachShader(prog, vs)
        self.glAttachShader(prog, fs)
        self.glBindAttribLocation(prog, 0, b"a_pos")
        self.glLinkProgram(prog)
        self.glDeleteShader(vs)
        self.glDeleteShader(fs)
        ok = ctypes.c_int(0)
        self.glGetProgramiv(prog, GL_LINK_STATUS, ctypes.byref(ok))
        if not ok.value:
            log = ctypes.create_string_buffer(2048)
            self.glGetProgramInfoLog(prog, 2048, None, log)
            self.glDeleteProgram(prog)
            raise RuntimeError("program does not link: " + log.value.decode(errors="replace"))
        return prog


# One quad over the whole viewport. Every corner carries the direction of its
# ray in camera space; interpolating directions linearly across the screen is
# exact for a perspective view, so the fragment shader only normalises and
# looks up. GLSL ES 1.00 and GLSL 1.50 differ only in these keywords.
_VERTEX_SHADER = """
ATTRIBUTE vec2 a_pos;
VARYING_OUT vec3 v_ray;
uniform mat3 u_rotation;
uniform vec2 u_tan_half_fov;
void main() {
    v_ray = u_rotation * vec3(a_pos * u_tan_half_fov, -1.0);
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

_FRAGMENT_SHADER = """
VARYING_IN vec3 v_ray;
uniform sampler2D u_picture;
uniform vec4 u_crop;   /* left, top, width, height as fractions of the full pano */
uniform float u_wraps; /* 1.0 when the picture goes all the way round */
const float PI = 3.14159265358979;
void main() {
    vec3 d = normalize(v_ray);
    vec2 full = vec2(0.5 + atan(d.x, -d.z) / (2.0 * PI),
                     0.5 - asin(clamp(d.y, -1.0, 1.0)) / PI);
    vec2 uv = (full - u_crop.xy) / u_crop.zw;
    if (u_wraps > 0.5)
        uv.x = fract(uv.x);
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0)
        FRAG_COLOR = vec4(0.08, 0.08, 0.08, 1.0);
    else
        FRAG_COLOR = vec4(TEXTURE(u_picture, uv).rgb, 1.0);
}
"""


def _shader_sources(es: bool) -> tuple[str, str]:
    if es:
        vs_head = "#version 100\nprecision highp float;\n#define ATTRIBUTE attribute\n#define VARYING_OUT varying\n"
        fs_head = (
            "#version 100\n#ifdef GL_FRAGMENT_PRECISION_HIGH\nprecision highp float;\n#else\n"
            "precision mediump float;\n#endif\n#define VARYING_IN varying\n"
            "#define TEXTURE texture2D\n#define FRAG_COLOR gl_FragColor\n"
        )
    else:
        vs_head = "#version 150\n#define ATTRIBUTE in\n#define VARYING_OUT out\n"
        fs_head = "#version 150\n#define VARYING_IN in\n#define TEXTURE texture\nout vec4 frag_color;\n#define FRAG_COLOR frag_color\n"
    return vs_head + _VERTEX_SHADER, fs_head + _FRAGMENT_SHADER


def pixbuf_rows(pixbuf: GdkPixbuf.Pixbuf) -> bytes:
    """The pixels without row padding, as glTexImage2D reads them."""
    width, height = pixbuf.get_width(), pixbuf.get_height()
    row = width * pixbuf.get_n_channels()
    stride = pixbuf.get_rowstride()
    data = pixbuf.get_pixels()
    if stride == row:
        return data if len(data) == row * height else data[: row * height]
    view = memoryview(data)
    return b"".join(view[y * stride: y * stride + row] for y in range(height))


# ---------------------------------------------------------------------------
# The phone's orientation
# ---------------------------------------------------------------------------

_SENSOR_BUS = "net.hadess.SensorProxy"
_SENSOR_PATH = "/net/hadess/SensorProxy/Gravity"
_SENSOR_IFACE = "org.plinius.SensorProxyBridge.Gravity"


class _SharedClaim:
    """The process's one claim on the orientation sensor, shared by its views.

    The bridge counts claims per D-Bus connection, and a process has one: a
    view releasing while another has just claimed would switch the sensor
    off under the second. So the views register here, and the claim goes out
    with the first and back with the last. When the bridge restarts (a new
    owner of its name) it has forgotten every claim: they are sent again.
    """

    def __init__(self) -> None:
        self._listeners: list[Callable[[tuple[float, float, float, float]], None]] = []
        self._bus: Gio.DBusConnection | None = None
        self._subscription = 0
        self._watch = 0

    def add(self, bus: Gio.DBusConnection, listener: Callable[[tuple[float, float, float, float]], None]) -> None:
        self._listeners.append(listener)
        if len(self._listeners) > 1:
            return
        self._bus = bus
        self._subscription = bus.signal_subscribe(
            _SENSOR_BUS, _SENSOR_IFACE, "Orientation", _SENSOR_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_signal,
        )
        # Also fires once right away for the running bridge; a second claim
        # from the same connection is a no-op there
        self._watch = Gio.bus_watch_name_on_connection(
            bus, _SENSOR_BUS, Gio.BusNameWatcherFlags.NONE, self._on_bridge_appeared, None,
        )
        self._call("ClaimOrientation")

    def remove(self, listener: Callable[[tuple[float, float, float, float]], None]) -> None:
        if listener not in self._listeners:
            return
        self._listeners.remove(listener)
        if self._listeners or self._bus is None:
            return
        self._bus.signal_unsubscribe(self._subscription)
        Gio.bus_unwatch_name(self._watch)
        self._subscription = self._watch = 0
        self._call("ReleaseOrientation")

    def _call(self, method: str) -> None:
        if self._bus is not None:
            self._bus.call(_SENSOR_BUS, _SENSOR_PATH, _SENSOR_IFACE, method, None, None,
                           Gio.DBusCallFlags.NONE, -1, None, None)

    def _on_bridge_appeared(self, _bus, _name, _owner) -> None:
        if self._listeners:
            self._call("ClaimOrientation")

    def _on_signal(self, _bus, _sender, _path, _iface, _signal, params: GLib.Variant) -> None:
        sample = tuple(params.unpack())
        for listener in list(self._listeners):
            listener(sample)  # type: ignore[arg-type]


_shared_claim = _SharedClaim()


class MotionSource:
    """The phone's orientation from the Plinius sensor bridge, while started.

    ``available()`` is a cheap synchronous check; ``start(callback)`` calls
    ``callback((x, y, z, w))`` on the main loop for each sample (about 50 per
    second) until ``stop()``. The claim behind it is shared within the
    process (see ``_SharedClaim``); the bridge also drops it by itself when
    the process goes away.
    """

    def __init__(self) -> None:
        self._bus: Gio.DBusConnection | None = None
        self._callback: Callable[[tuple[float, float, float, float]], None] | None = None

    def _connection(self) -> Gio.DBusConnection | None:
        if self._bus is None:
            try:
                self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            except GLib.Error:
                return None
        return self._bus

    def _property(self, name: str):
        bus = self._connection()
        if bus is None:
            return None
        try:
            reply = bus.call_sync(
                _SENSOR_BUS, _SENSOR_PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (_SENSOR_IFACE, name)),
                GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 500, None,
            )
            return reply.unpack()[0]
        except GLib.Error:
            return None

    def available(self) -> bool:
        return bool(self._property("HasOrientation"))

    def references_north(self) -> bool:
        """Whether the orientation's heading is the compass one (else arbitrary)."""
        return self._property("OrientationReference") == "north"

    def start(self, callback: Callable[[tuple[float, float, float, float]], None]) -> None:
        bus = self._connection()
        if bus is None or self._callback is not None:
            return
        self._callback = callback
        _shared_claim.add(bus, callback)

    def stop(self) -> None:
        if self._callback is None:
            return
        _shared_claim.remove(self._callback)
        self._callback = None


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

# Field of view across the longer side of the view, in degrees
FOV_DEFAULT = 100.0
FOV_MIN = 30.0
FOV_MAX = 120.0
# A drag that starts this close to the left or right edge (share of the
# width) turns the page instead of looking round, so browsing goes on.
EDGE_SHARE = 0.12
# A flick keeps turning and slows down with this time constant (seconds).
FLING_TAU = 0.35
# Each motion sample moves the drawn orientation this far towards itself:
# smooths the sensor's 50 Hz steps on a 60-120 Hz display.
MOTION_SMOOTHING = 0.5


class PanoramaView(Gtk.GLArea):
    """The inside of the sphere, turned by dragging or by moving the phone.

    *on_failed* is called (once, on the main loop) when GL cannot be set up,
    so the caller can show the flat picture instead.
    """

    __gtype_name__ = "MugaPanoramaView"

    def __init__(self, pixbuf: GdkPixbuf.Pixbuf, info: PanoramaInfo,
                 on_failed: Callable[[], object] | None = None, path: str | None = None) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_has_depth_buffer(False)
        self.set_auto_render(True)
        self.info = info
        self._pixbuf: GdkPixbuf.Pixbuf | None = pixbuf
        self._path = path
        self._on_failed = on_failed
        self._failed = False
        self._gl: GL | None = None
        self._program = 0
        self._texture = 0
        self._buffer = 0
        self._vao = 0
        self._uniforms: dict[str, int] = {}

        self.yaw = info.initial_heading
        self.pitch = info.initial_pitch
        self.fov = FOV_DEFAULT
        self._drag_start = (0.0, 0.0)
        self._drag_last = (0.0, 0.0, 0)  # x, y, µs
        self._velocity = (0.0, 0.0)      # degrees per second while flung
        self._fling_tick = 0
        self._zoom_start_fov = self.fov

        self._motion = MotionSource()
        self._motion_on = False
        self._quaternion: tuple[float, float, float, float] | None = None
        self._shown_rotation: Matrix | None = None
        self._recentre = True
        self._yaw_offset = 0.0
        self._screen_angle = 0
        self._screen_landscape: bool | None = None
        self._north = False

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("render", self._on_render)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        self._drag = drag
        zoom = Gtk.GestureZoom()
        zoom.connect("begin", lambda *_: setattr(self, "_zoom_start_fov", self.fov))
        zoom.connect("scale-changed", self._on_zoom)
        self.add_controller(zoom)
        scroll = Gtk.EventControllerScroll()
        scroll.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        if path and max(pixbuf.get_width(), pixbuf.get_height()) < PANORAMA_MAX_DIM:
            self._load_sharper(path, pixbuf.get_width())

    # -- what the viewer asks -------------------------------------------------

    def is_edge_drag(self, x: float) -> bool:
        """Whether a drag starting at *x* (own coordinates) turns the page."""
        width = max(self.get_width(), 1)
        return x < width * EDGE_SHARE or x > width * (1 - EDGE_SHARE)

    def drag_started_at_edge(self) -> bool:
        return self.is_edge_drag(self._drag_start[0])

    def reset_view(self) -> None:
        self._stop_fling()
        self.yaw = self.info.initial_heading
        self.pitch = self.info.initial_pitch
        self.fov = FOV_DEFAULT
        self._recentre = True
        self.queue_render()

    def motion_available(self) -> bool:
        return self._motion.available()

    def motion_active(self) -> bool:
        """Whether the phone's orientation steers the view right now."""
        return self._motion_on

    def set_motion(self, on: bool) -> None:
        if on == self._motion_on:
            return
        self._motion_on = on
        if on:
            self._recentre = True
            self._quaternion = None
            self._shown_rotation = None
            self._screen_landscape = None
            self._north = self._motion.references_north()
            self._motion.start(self._on_orientation)
        else:
            self._motion.stop()
            # Carry on from where the phone was pointing
            if self._shown_rotation is not None:
                forward = mat_apply(self._shown_rotation, (0.0, 0.0, -1.0))
                self.yaw = longitude_of(forward)
                self.pitch = latitude_of(forward)
            self._shown_rotation = None
        self.queue_render()

    def zoom_by(self, factor: float) -> None:
        self.fov = min(max(self.fov / factor, FOV_MIN), FOV_MAX)
        self.queue_render()

    # -- input ----------------------------------------------------------------

    def _degrees_per_pixel(self) -> float:
        w, h = max(self.get_width(), 1), max(self.get_height(), 1)
        return self.fov / max(w, h)

    def _on_drag_begin(self, _gesture, x: float, y: float) -> None:
        self._stop_fling()
        self._drag_start = (x, y)
        self._drag_last = (0.0, 0.0, GLib.get_monotonic_time())
        self._velocity = (0.0, 0.0)

    def _on_drag_update(self, _gesture, dx: float, dy: float) -> None:
        if self.drag_started_at_edge():
            return
        last_x, last_y, last_t = self._drag_last
        now = GLib.get_monotonic_time()
        step = self._degrees_per_pixel()
        d_yaw, d_pitch = -(dx - last_x) * step, (dy - last_y) * step
        self._turn(d_yaw, d_pitch)
        dt = (now - last_t) / 1e6
        if dt > 0.004:
            # Smoothed velocity for the flick at the end
            vx, vy = self._velocity
            self._velocity = (0.6 * d_yaw / dt + 0.4 * vx, 0.6 * d_pitch / dt + 0.4 * vy)
        self._drag_last = (dx, dy, now)

    def _on_drag_end(self, _gesture, _dx: float, _dy: float) -> None:
        if self.drag_started_at_edge():
            return
        # A finger that stopped before lifting does not fling
        if (GLib.get_monotonic_time() - self._drag_last[2]) > 80_000:
            self._velocity = (0.0, 0.0)
        if math.hypot(*self._velocity) > 20.0:
            self._fling_last = GLib.get_monotonic_time()
            self._fling_tick = self.add_tick_callback(self._on_fling_tick)

    def _turn(self, d_yaw: float, d_pitch: float) -> None:
        if self._motion_on:
            # The phone decides the pitch; the fingers only turn round
            self._yaw_offset += d_yaw
        else:
            self.yaw = (self.yaw + d_yaw + 180.0) % 360.0 - 180.0
            self.pitch = min(max(self.pitch + d_pitch, -90.0), 90.0)
        self.queue_render()

    def _on_fling_tick(self, _widget, clock) -> bool:
        now = GLib.get_monotonic_time()
        dt = (now - self._fling_last) / 1e6
        self._fling_last = now
        decay = math.exp(-dt / FLING_TAU)
        vx, vy = self._velocity[0] * decay, self._velocity[1] * decay
        self._velocity = (vx, vy)
        self._turn(vx * dt, vy * dt)
        if math.hypot(vx, vy) < 2.0:
            self._fling_tick = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _stop_fling(self) -> None:
        if self._fling_tick:
            self.remove_tick_callback(self._fling_tick)
            self._fling_tick = 0

    def _on_zoom(self, _gesture, scale: float) -> None:
        self.fov = min(max(self._zoom_start_fov / max(scale, 0.01), FOV_MIN), FOV_MAX)
        self.queue_render()

    def _on_scroll(self, _controller, _dx: float, dy: float) -> bool:
        self.zoom_by(1.1 ** -dy)
        return True

    # -- motion ---------------------------------------------------------------

    def _on_orientation(self, q: tuple[float, float, float, float]) -> None:
        self._quaternion = q
        self.queue_render()

    def _motion_rotation(self) -> Matrix | None:
        q = self._quaternion
        if q is None:
            return None
        landscape = self.get_width() > self.get_height()
        if landscape != self._screen_landscape:
            self._screen_angle = screen_angle_for(q, landscape, self._screen_angle)
            self._screen_landscape = landscape
        device = device_view(q, self._screen_angle)
        if self._recentre:
            if self._north and self.info.pose_heading is not None:
                # Standing where the photo was taken: the phone's compass
                # heading picks the same direction out of the picture
                # (longitude 0 of the device view is magnetic north)
                self._yaw_offset = -self.info.pose_heading
            else:
                # The phone points wherever it points; that becomes the
                # view the picture opens with (or the one dragged to)
                forward = mat_apply(device, (0.0, 0.0, -1.0))
                self._yaw_offset = self.yaw - longitude_of(forward)
            self._recentre = False
        target = mat_mul(rot_y(-self._yaw_offset), device)
        shown = self._shown_rotation
        if shown is not None:
            # Move part of the way; the rest follows with the next frames
            blended = tuple(
                tuple(shown[i][j] + (target[i][j] - shown[i][j]) * MOTION_SMOOTHING for j in range(3))
                for i in range(3)
            )
            target = _orthonormal(blended)  # type: ignore[arg-type]
            if max(abs(target[i][j] - shown[i][j]) for i in range(3) for j in range(3)) > 1e-4:
                self.queue_render()
        self._shown_rotation = target
        return target

    # -- GL -------------------------------------------------------------------

    def _fail(self, why: str) -> None:
        LOGGER.warning("360° view unavailable: %s", why)
        if not self._failed:
            self._failed = True
            if self._on_failed is not None:
                GLib.idle_add(self._on_failed)

    def _on_realize(self, _area) -> None:
        self.make_current()
        error = self.get_error()
        if error is not None:
            self._fail(error.message)
            return
        try:
            gl = GL()
            context = self.get_context()
            es = bool(context is not None and context.get_use_es())
            vs, fs = _shader_sources(es)
            self._program = gl.program(vs, fs)
            for name in ("u_rotation", "u_tan_half_fov", "u_picture", "u_crop", "u_wraps"):
                self._uniforms[name] = gl.glGetUniformLocation(self._program, name.encode())
            ids = (ctypes.c_uint * 1)()
            # Core profiles draw nothing without a vertex array object;
            # GLES 2 has none (and needs none)
            major, _minor = context.get_version() if context is not None else (2, 0)
            if not es or major >= 3:
                gl.glGenVertexArrays(1, ids)
                self._vao = ids[0]
                gl.glBindVertexArray(self._vao)
            gl.glGenBuffers(1, ids)
            self._buffer = ids[0]
            corners = (ctypes.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
            gl.glBindBuffer(GL_ARRAY_BUFFER, self._buffer)
            gl.glBufferData(GL_ARRAY_BUFFER, ctypes.sizeof(corners), corners, GL_STATIC_DRAW)
            gl.glVertexAttribPointer(0, 2, GL_FLOAT, 0, 0, None)
            gl.glEnableVertexAttribArray(0)
            self._gl = gl
            self._upload()
        except (OSError, RuntimeError, AttributeError, ValueError) as exc:
            self._gl = None
            self._fail(str(exc))

    def _upload(self) -> None:
        """Moves the pending pixbuf into the texture (context current)."""
        gl, pixbuf = self._gl, self._pixbuf
        if gl is None or pixbuf is None:
            return
        limit = gl.integer(GL_MAX_TEXTURE_SIZE)[0] or 4096
        width, height = pixbuf.get_width(), pixbuf.get_height()
        if max(width, height) > limit:
            scale = limit / max(width, height)
            pixbuf = pixbuf.scale_simple(max(1, int(width * scale)), max(1, int(height * scale)),
                                         GdkPixbuf.InterpType.BILINEAR)
            width, height = pixbuf.get_width(), pixbuf.get_height()
        fmt = GL_RGBA if pixbuf.get_n_channels() == 4 else GL_RGB
        rows = pixbuf_rows(pixbuf)
        if not self._texture:
            ids = (ctypes.c_uint * 1)()
            gl.glGenTextures(1, ids)
            self._texture = ids[0]
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, self._texture)
        gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        # No mipmaps: their level choice jumps at the seam where the
        # longitude wraps and would draw a line down the picture there
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        gl.glTexImage2D(GL_TEXTURE_2D, 0, fmt, width, height, 0, fmt, GL_UNSIGNED_BYTE, rows)
        self._pixbuf = None

    def _load_sharper(self, path: str, have_width: int) -> None:
        """Decodes the photo again at the sphere's own cap, off the UI thread.

        The viewer's decode is capped for flat photos; a sphere shows only a
        slice of its width at a time and gets visibly softer from that cap.
        """
        def worker() -> None:
            try:
                info = GdkPixbuf.Pixbuf.get_file_info(path)
                width = info[1] if info and info[1] else 0
                if width <= have_width:
                    return
                if width > PANORAMA_MAX_DIM:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, PANORAMA_MAX_DIM, PANORAMA_MAX_DIM, True)
                else:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
                pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
            except GLib.Error:
                return
            GLib.idle_add(self._sharper_loaded, pixbuf)

        threading.Thread(target=worker, name="muga-panorama-decode", daemon=True).start()

    def _sharper_loaded(self, pixbuf: GdkPixbuf.Pixbuf) -> bool:
        if self.get_realized() and self._gl is not None:
            self.make_current()
            self._pixbuf = pixbuf
            try:
                self._upload()
            except (RuntimeError, ValueError):
                LOGGER.debug("sharper texture not uploaded", exc_info=True)
            self.queue_render()
        return GLib.SOURCE_REMOVE

    def _on_render(self, _area, _context) -> bool:
        gl = self._gl
        if gl is None or not self._texture:
            return False
        if self._pixbuf is not None:
            self._upload()
        x, y, width, height = gl.integer(GL_VIEWPORT, 4)
        if width <= 0 or height <= 0:
            return False
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(GL_COLOR_BUFFER_BIT)

        rotation = self._motion_rotation() if self._motion_on else None
        if rotation is None:
            rotation = view_matrix(self.yaw, self.pitch)
        half = math.tan(math.radians(self.fov) / 2)
        if width >= height:
            tan_x, tan_y = half, half * height / width
        else:
            tan_x, tan_y = half * width / height, half

        gl.glUseProgram(self._program)
        # GLSL matrices are column-major
        columns = (ctypes.c_float * 9)(*(rotation[r][c] for c in range(3) for r in range(3)))
        gl.glUniformMatrix3fv(self._uniforms["u_rotation"], 1, 0, columns)
        gl.glUniform2f(self._uniforms["u_tan_half_fov"], tan_x, tan_y)
        info = self.info
        gl.glUniform4f(self._uniforms["u_crop"], info.left, info.top, info.width, info.height)
        gl.glUniform1f(self._uniforms["u_wraps"], 1.0 if info.wraps else 0.0)
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, self._texture)
        gl.glUniform1i(self._uniforms["u_picture"], 0)
        if self._vao:
            gl.glBindVertexArray(self._vao)
        else:
            gl.glBindBuffer(GL_ARRAY_BUFFER, self._buffer)
            gl.glVertexAttribPointer(0, 2, GL_FLOAT, 0, 0, None)
            gl.glEnableVertexAttribArray(0)
        gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        return True

    def _on_unrealize(self, _area) -> None:
        self._stop_fling()
        self.set_motion(False)
        gl = self._gl
        if gl is None:
            return
        self.make_current()
        if self.get_error() is not None:
            return
        ids = (ctypes.c_uint * 1)()
        if self._texture:
            ids[0] = self._texture
            gl.glDeleteTextures(1, ids)
        if self._buffer:
            ids[0] = self._buffer
            gl.glDeleteBuffers(1, ids)
        if self._vao:
            ids[0] = self._vao
            gl.glDeleteVertexArrays(1, ids)
        if self._program:
            gl.glDeleteProgram(self._program)
        self._texture = self._buffer = self._vao = self._program = 0
        self._gl = None


def _orthonormal(m: Matrix) -> Matrix:
    """The nearest rotation to a slightly skewed one (Gram-Schmidt on columns)."""
    c0 = [m[0][0], m[1][0], m[2][0]]
    c1 = [m[0][1], m[1][1], m[2][1]]
    n0 = math.sqrt(sum(v * v for v in c0)) or 1.0
    c0 = [v / n0 for v in c0]
    dot = sum(a * b for a, b in zip(c0, c1))
    c1 = [b - dot * a for a, b in zip(c0, c1)]
    n1 = math.sqrt(sum(v * v for v in c1)) or 1.0
    c1 = [v / n1 for v in c1]
    c2 = [c0[1] * c1[2] - c0[2] * c1[1], c0[2] * c1[0] - c0[0] * c1[2], c0[0] * c1[1] - c0[1] * c1[0]]
    return ((c0[0], c1[0], c2[0]), (c0[1], c1[1], c2[1]), (c0[2], c1[2], c2[2]))


# ---------------------------------------------------------------------------
# As the phone's background (plinius-ambient)
# ---------------------------------------------------------------------------

AMBIENT_SCHEMA = "org.plinius.ambient"


def ambient_settings(backend: Gio.SettingsBackend | None = None) -> Gio.Settings | None:
    """plinius-ambient's settings, when it is installed and shows 360° photos
    (0.4 on: the photo is the background, under any effect)."""
    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup(AMBIENT_SCHEMA, True) if source is not None else None
    if schema is None or not schema.has_key("panorama") or not schema.has_key("panorama-enabled"):
        return None
    return Gio.Settings.new_full(schema, backend, None)


def set_as_background(path: str, settings: Gio.Settings | None = None) -> str:
    """Copies the photo into ~/.local/share/backgrounds/360 and shows it there.

    A copy, like GNOME's own "Set as Background": moving or deleting the
    photo later must not take the background with it. Earlier copies stay:
    GNOME Settings lists that folder under its 360° heading, so one can
    switch between them and GNOME's wallpapers. The animated effect (snow,
    rain...) is left as it is: it is drawn over the photo. Returns the
    copy's path; raises OSError or GLib.Error when it cannot be done.
    """
    settings = settings or ambient_settings()
    if settings is None:
        raise OSError("plinius-ambient is not installed")
    folder = os.path.join(GLib.get_user_data_dir(), "backgrounds", "360")
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, os.path.basename(path))
    if os.path.abspath(path) != os.path.abspath(target):
        src = Gio.File.new_for_path(path)
        src.copy(Gio.File.new_for_path(target), Gio.FileCopyFlags.OVERWRITE, None, None, None)
    settings.set_string("panorama", target)
    settings.set_boolean("panorama-enabled", True)
    return target
