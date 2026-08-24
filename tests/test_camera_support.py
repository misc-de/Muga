"""Tests for the camera's supporting modules.

These are the small pieces the camera window leans on: device discovery and
classification, the letterbox maths every overlay uses to line up with the
visible image, and the sysfs torch. Each has a narrow, checkable contract and
none of them needed a camera to test.

The letterbox maths is the load-bearing one — the focus rectangle, the
recording dot, the rule-of-thirds grid and the viewfinder brackets all place
themselves through it, so an error there misplaces every piece of chrome at
once.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

import pytest

devices = pytest.importorskip("muga.camera_devices")
widgets = pytest.importorskip("muga.camera_widgets")
torch = pytest.importorskip("muga.camera_torch")


# ---------------------------------------------------------------------------
# IR camera filtering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    ["IR Camera", "Integrated IR Camera", "ir camera", "HP IR Camera"],
)
def test_ir_cameras_are_recognised(name) -> None:
    """Windows-Hello IR nodes only carry monochrome streams and have no
    business in a camera picker."""
    assert devices.is_ir_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["Integrated Webcam", "HD User Facing", "Logitech C920", "Camera",
     "Director Camera", "Mirage Cam"],
)
def test_ordinary_cameras_are_not_filtered(name) -> None:
    """The check must not fire on an "ir" that is merely inside a word —
    "Director" and "Mirage" both contain it."""
    assert devices.is_ir_name(name) is False


# ---------------------------------------------------------------------------
# Front / back classification
# ---------------------------------------------------------------------------

def _props(**values):
    props = MagicMock()
    props.get_string.side_effect = lambda key: values.get(key)
    return props


@pytest.mark.parametrize(
    ("location", "expected"),
    [("front", "front"), ("Front", "front"), ("back", "back"),
     ("rear", "back"), ("external", "external")],
)
def test_libcamera_location_wins(location, expected) -> None:
    """PipeWire/libcamera knows for certain; the name heuristic is a guess."""
    props = _props(**{"api.libcamera.location": location})
    assert devices.classify_location(props, "Some Camera") == expected


def test_libcamera_location_overrides_a_misleading_name() -> None:
    props = _props(**{"api.libcamera.location": "back"})
    assert devices.classify_location(props, "Front Camera") == "back"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Front Camera", "front"),
        ("User Facing HD", "front"),
        ("Rear Camera", "back"),
        ("Back Camera", "back"),
        ("Logitech C920", "unknown"),
    ],
)
def test_name_heuristics_are_the_fallback(name, expected) -> None:
    assert devices.classify_location(None, name) == expected


def test_classification_survives_a_raising_property_bag() -> None:
    props = MagicMock()
    props.get_string.side_effect = RuntimeError("gone")
    assert devices.classify_location(props, "Front Camera") == "front"


def test_classification_without_properties() -> None:
    assert devices.classify_location(None, "") == "unknown"


# ---------------------------------------------------------------------------
# Device paths and provenance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "key", ["device.path", "api.v4l2.path", "object.path"],
)
def test_device_path_is_read_from_any_known_key(key) -> None:
    assert devices.device_path(_props(**{key: "/dev/video3"})) == "/dev/video3"


def test_device_path_when_nothing_is_set() -> None:
    assert devices.device_path(_props()) == ""
    assert devices.device_path(None) == ""


def test_pipewire_devices_are_distinguishable() -> None:
    """v4l2deviceprovider entries carry no node.* keys; a PipeWire-managed
    device does, and it must be opened through its own element."""
    assert devices.is_pipewire_device(_props(**{"node.name": "v4l2_input.pci"})) is True
    assert devices.is_pipewire_device(_props(**{"device.path": "/dev/video0"})) is False
    assert devices.is_pipewire_device(None) is False


def test_device_props_survives_a_raising_device() -> None:
    dev = MagicMock()
    dev.get_properties.side_effect = RuntimeError("disposed")
    assert devices.device_props(dev) is None


# ---------------------------------------------------------------------------
# Capability parsing
# ---------------------------------------------------------------------------

class _Struct:
    def __init__(self, name, w, h) -> None:
        self._name, self._w, self._h = name, w, h

    def get_name(self):
        return self._name

    def get_int(self, key):
        return (True, self._w) if key == "width" else (True, self._h)


class _Caps:
    def __init__(self, structs) -> None:
        self._structs = structs

    def get_size(self):
        return len(self._structs)

    def get_structure(self, i):
        return self._structs[i]


def test_modes_are_sorted_largest_first() -> None:
    caps = _Caps([
        _Struct("video/x-raw", 640, 480),
        _Struct("video/x-raw", 1920, 1080),
        _Struct("video/x-raw", 1280, 720),
    ])
    assert [(w, h) for w, h, _ in devices.modes_from_caps(caps)] == [
        (1920, 1080), (1280, 720), (640, 480),
    ]


def test_mjpeg_modes_are_kept() -> None:
    """UVC cameras advertise their highest resolutions only via MJPG; dropping
    them leaves tiny modes, or no camera at all when raw is absent."""
    caps = _Caps([_Struct("image/jpeg", 3840, 2160), _Struct("video/x-raw", 640, 480)])
    modes = devices.modes_from_caps(caps)
    assert (3840, 2160, "jpeg") in modes
    assert (640, 480, "raw") in modes


def test_unknown_media_types_are_ignored() -> None:
    caps = _Caps([_Struct("audio/x-raw", 0, 0), _Struct("video/x-raw", 640, 480)])
    assert devices.modes_from_caps(caps) == [(640, 480, "raw")]


def test_zero_sized_modes_are_dropped() -> None:
    caps = _Caps([_Struct("video/x-raw", 0, 480), _Struct("video/x-raw", 640, 0)])
    assert devices.modes_from_caps(caps) == []


def test_duplicate_modes_collapse() -> None:
    caps = _Caps([_Struct("video/x-raw", 640, 480)] * 3)
    assert len(devices.modes_from_caps(caps)) == 1


def test_modes_from_no_caps() -> None:
    assert devices.modes_from_caps(None) == []


def test_modes_from_a_raising_caps_object() -> None:
    caps = MagicMock()
    caps.get_size.side_effect = RuntimeError("bad caps")
    assert devices.modes_from_caps(caps) == []


def test_resolutions_prefer_raw_over_jpeg() -> None:
    """Same size in both formats: raw needs no jpegdec in the pipeline."""
    caps = _Caps([_Struct("image/jpeg", 1920, 1080), _Struct("video/x-raw", 1920, 1080)])
    assert devices.resolutions_from_caps(caps) == [(1920, 1080)]


def test_resolutions_deduplicate_across_formats() -> None:
    caps = _Caps([
        _Struct("image/jpeg", 1920, 1080), _Struct("video/x-raw", 1920, 1080),
        _Struct("image/jpeg", 640, 480),
    ])
    assert devices.resolutions_from_caps(caps) == [(1920, 1080), (640, 480)]


def test_device_kinds_reports_the_formats() -> None:
    caps = _Caps([_Struct("image/jpeg", 1920, 1080), _Struct("video/x-raw", 640, 480)])
    assert devices.device_kinds(caps) == {"raw", "jpeg"}
    assert devices.device_kinds(None) == set()


# ---------------------------------------------------------------------------
# Letterbox geometry
# ---------------------------------------------------------------------------

def _picture(iw, ih, zoom=1.0):
    paintable = MagicMock()
    paintable.get_intrinsic_width.return_value = iw
    paintable.get_intrinsic_height.return_value = ih
    picture = MagicMock()
    picture.get_paintable.return_value = paintable
    picture.get_zoom.return_value = zoom
    return picture


def test_image_rect_fills_a_matching_aspect() -> None:
    rect = widgets.compute_image_rect(_picture(1600, 900), 800, 450)
    assert rect == (0.0, 0.0, 800.0, 450.0)


def test_image_rect_letterboxes_a_wide_image() -> None:
    """A 16:9 frame in a 4:3 widget: bars above and below."""
    left, top, w, h = widgets.compute_image_rect(_picture(1600, 900), 800, 600)
    assert (left, w) == (0.0, 800.0)
    assert top == pytest.approx(75.0)
    assert h == pytest.approx(450.0)


def test_image_rect_pillarboxes_a_tall_image() -> None:
    left, top, w, h = widgets.compute_image_rect(_picture(900, 1600), 800, 600)
    assert (top, h) == (0.0, 600.0)
    assert left == pytest.approx(231.25)
    assert w == pytest.approx(337.5)


def test_image_rect_is_centred() -> None:
    left, top, w, h = widgets.compute_image_rect(_picture(1600, 900), 800, 600)
    assert left + w / 2 == pytest.approx(400.0)
    assert top + h / 2 == pytest.approx(300.0)


def test_image_rect_without_a_paintable() -> None:
    """Before the first frame there is nothing to align to, and callers use
    the zero rect to skip painting entirely."""
    picture = MagicMock()
    picture.get_paintable.return_value = None
    assert widgets.compute_image_rect(picture, 800, 600) == (0.0, 0.0, 0.0, 0.0)


def test_image_rect_with_a_zero_sized_paintable() -> None:
    assert widgets.compute_image_rect(_picture(0, 0), 800, 600) == (0.0, 0.0, 0.0, 0.0)
    assert widgets.compute_image_rect(_picture(100, 0), 800, 600) == (0.0, 0.0, 0.0, 0.0)


def test_image_rect_follows_the_zoom() -> None:
    """Pinch-zoom scales about the widget centre; the chrome has to track it
    or the focus rectangle drifts off the subject."""
    plain = widgets.compute_image_rect(_picture(1600, 900), 800, 600)
    zoomed = widgets.compute_image_rect(_picture(1600, 900, zoom=2.0), 800, 600)
    assert zoomed[3] > plain[3], "the zoomed rect is not taller"
    assert zoomed[0] + zoomed[2] / 2 == pytest.approx(400.0), "zoom is not centred"


def test_image_rect_is_clipped_to_the_widget() -> None:
    """Zoomed past the edges, the rect must not report area outside the
    widget — the grid would be drawn off-screen."""
    left, top, w, h = widgets.compute_image_rect(_picture(1600, 900, zoom=4.0), 800, 600)
    assert left >= 0 and top >= 0
    assert left + w <= 800
    assert top + h <= 600


def test_image_rect_without_a_zoom_capable_picture() -> None:
    """Plain Gtk.Picture has no get_zoom; the helper is used with both."""
    paintable = MagicMock()
    paintable.get_intrinsic_width.return_value = 1600
    paintable.get_intrinsic_height.return_value = 900
    picture = SimpleNamespace(get_paintable=lambda: paintable)
    assert widgets.compute_image_rect(picture, 800, 450) == (0.0, 0.0, 800.0, 450.0)


# ---------------------------------------------------------------------------
# Torch
# ---------------------------------------------------------------------------

def test_torch_writes_to_the_first_working_node() -> None:
    handle = mock_open()
    with patch("builtins.open", handle), patch("os.access", return_value=True):
        assert torch.set_torch_sysfs(True) is True
    handle().write.assert_called()


def test_torch_off_writes_zero() -> None:
    handle = mock_open()
    with patch("builtins.open", handle), patch("os.access", return_value=True):
        torch.set_torch_sysfs(False)
    written = "".join(str(c[0][0]) for c in handle().write.call_args_list)
    assert "0" in written


def test_torch_reports_failure_when_no_node_exists() -> None:
    """Every non-Halium device lands here, so it has to be quiet about it."""
    with patch("os.access", return_value=False):
        assert torch.set_torch_sysfs(True) is False


def test_torch_survives_a_permission_error() -> None:
    with patch("os.access", return_value=True), \
         patch("builtins.open", side_effect=PermissionError("denied")):
        assert torch.set_torch_sysfs(True) is False
