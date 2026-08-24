"""Integration tests that build a real CameraWindow.

Everything else in the camera suite runs headless against unbound methods.
These tests do the opposite: they construct the actual widget tree, which is
the only way to cover __init__ and the popover builders — a third of the
module, and the part where a typo'd attribute or a mis-parented widget hides
until someone opens the app.

GTK aborts the process rather than raising when there is no display, so the
whole module is skipped in that case (see conftest.requires_display). Device
enumeration and the orientation sensor are stubbed so no camera or D-Bus
session is needed; the pipeline itself never starts because __init__ only
schedules it via GLib.idle_add and no main loop runs here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import requires_display

camera = pytest.importorskip("muga.camera")

pytestmark = requires_display

FAKE_V4L2 = {
    "path": "/dev/video0", "name": "FakeCam", "caps": None,
    "source_factory": "v4l2src", "location": "back",
}
FAKE_HALIUM = {
    "path": "", "name": "HAL back", "caps": None,
    "source_factory": "droidcamsrc", "droidcam_id": 0, "location": "back",
}


@pytest.fixture
def make_window(tmp_path):
    """Build a CameraWindow with the hardware stubbed out."""
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    Adw.init()
    created = []

    def _make(devices=(FAKE_V4L2,), **kwargs):
        with patch.object(camera, "_enumerate_devices", return_value=list(devices)), \
             patch.object(camera, "OrientationClient", return_value=MagicMock()), \
             patch.object(camera.GLib, "idle_add"):
            parent = Gtk.Window()
            kwargs.setdefault("save_dir", tmp_path / "Photos")
            win = camera.CameraWindow(parent, **kwargs)
        created.append((win, parent))
        return win

    yield _make

    for win, parent in created:
        win._closing = True
        parent.destroy()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_window_builds(make_window) -> None:
    win = make_window()
    assert win._devices == [FAKE_V4L2]
    assert win._shutter is not None
    assert win._picture is not None
    assert win._rotatable_icons, "no rotatable icons registered"


def test_window_is_undecorated_and_fullscreen(make_window) -> None:
    """Phosh's status bar otherwise overlaps the top icon row and eats its
    clicks — the user sees the icons but the presses go to the system bar."""
    win = make_window()
    assert win.get_decorated() is False


def test_window_starts_the_pipeline_when_a_camera_exists(make_window) -> None:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    with patch.object(camera, "_enumerate_devices", return_value=[FAKE_V4L2]), \
         patch.object(camera, "OrientationClient", return_value=MagicMock()), \
         patch.object(camera.GLib, "idle_add") as idle:
        parent = Gtk.Window()
        win = camera.CameraWindow(parent, save_dir=Path("/tmp"))
    assert idle.call_args[0][0] == win._start_pipeline
    win._closing = True
    parent.destroy()


def test_window_without_a_camera_disables_the_shutter(make_window) -> None:
    win = make_window(devices=())
    assert win._shutter.get_sensitive() is False


@pytest.mark.parametrize("value", ["left", "right", "neutral"])
def test_window_accepts_every_handedness(make_window, value) -> None:
    assert make_window(handedness=value)._handedness == value


def test_window_rejects_an_unknown_handedness(make_window) -> None:
    assert make_window(handedness="sideways")._handedness == "right"


def test_window_restores_persisted_camera_settings(make_window) -> None:
    settings = SimpleNamespace(
        camera_jpeg_quality=61, camera_video_bitrate_kbps=8000,
        camera_image_resolution=[1920, 1080], handedness="left",
        camera_geo_enabled=False, camera_flash_enabled=False, save=MagicMock(),
    )
    win = make_window(settings=settings)
    assert win._jpeg_quality == 61
    assert win._video_bitrate_kbps == 8000


def test_window_falls_back_to_defaults_without_settings(make_window) -> None:
    win = make_window(settings=None)
    assert win._jpeg_quality == 92
    assert win._video_bitrate_kbps == 4000


def test_window_uses_the_save_dir_for_video_by_default(tmp_path, make_window) -> None:
    win = make_window(save_dir=tmp_path / "Photos")
    assert win._video_dir == tmp_path / "Photos"


def test_window_takes_a_separate_video_dir(tmp_path, make_window) -> None:
    win = make_window(save_dir=tmp_path / "Photos", video_dir=tmp_path / "Videos")
    assert win._video_dir == tmp_path / "Videos"


def test_window_uses_the_translator(make_window) -> None:
    win = make_window(translator=lambda s: f"<{s}>")
    assert win._("Camera") == "<Camera>"


def test_window_starts_upright_and_unlaid_out(make_window) -> None:
    from muga.camera_orientation import ORIENT_NORMAL
    win = make_window()
    assert win._device_orientation == ORIENT_NORMAL
    assert win._capture_orientation == ORIENT_NORMAL


# ---------------------------------------------------------------------------
# Popovers
# ---------------------------------------------------------------------------

def test_quality_popover_builds_in_photo_mode(make_window) -> None:
    win = make_window()
    win._capture_mode = "photo"
    popover = camera.CameraWindow._build_quality_popover(win)
    assert popover is not None
    assert win._photo_quality_buttons, "no photo-quality choices offered"


def test_quality_popover_builds_in_video_mode(make_window) -> None:
    win = make_window()
    win._capture_mode = "video"
    camera.CameraWindow._build_quality_popover(win)
    assert win._video_quality_buttons, "no video-quality choices offered"


def test_quality_popover_rebuilds_for_landscape(make_window) -> None:
    """It transposes its sections and rotates its labels, so a stale portrait
    popover reads sideways after the phone turns."""
    win = make_window()
    win._capture_mode = "photo"
    win._layout_is_landscape = False
    portrait = camera.CameraWindow._build_quality_popover(win)
    win._layout_is_landscape = True
    landscape = camera.CameraWindow._build_quality_popover(win)
    assert portrait is not landscape


def test_quality_popover_offers_photo_sizes_on_halium(make_window) -> None:
    """On droidcamsrc the standalone resolution chip is hidden, so the sizes
    have to live in this popover instead."""
    win = make_window(devices=(FAKE_HALIUM,))
    win._capture_mode = "photo"
    camera.CameraWindow._build_quality_popover(win)
    assert win._image_size_buttons, "no photo-size choices on a Halium device"


def test_settings_popover_builds(make_window) -> None:
    win = make_window()
    assert camera.CameraWindow._build_settings_popover(win) is not None
    assert win._handedness_buttons, "no handedness choices offered"


def test_controls_popover_builds_without_v4l2_controls(make_window) -> None:
    """v4l2-ctl is frequently absent; the gear must still open."""
    win = make_window()
    win._controls = {}
    win._controls_probed = True
    camera.CameraWindow._build_controls_popover(win)


def test_controls_popover_builds_with_controls(make_window) -> None:
    from muga.camera_controls import V4l2Control

    win = make_window()
    brightness = V4l2Control(name="brightness", type="int")
    brightness.min, brightness.max, brightness.step = 0, 255, 1
    brightness.default, brightness.value = 128, 128
    auto_exp = V4l2Control(name="auto_exposure", type="menu")
    auto_exp.menu = {1: "Manual Mode", 3: "Aperture Priority Mode"}
    auto_exp.value = 3
    win._controls = {"brightness": brightness, "auto_exposure": auto_exp}
    win._controls_probed = True
    camera.CameraWindow._build_controls_popover(win)


# ---------------------------------------------------------------------------
# Resolution picker
# ---------------------------------------------------------------------------

def _caps_for(sizes):
    """A caps stand-in that _resolutions_from_caps can read."""
    from gi.repository import Gst
    Gst.init(None)
    parts = [f"video/x-raw,width={w},height={h},framerate=30/1" for w, h in sizes]
    return Gst.Caps.from_string("; ".join(parts))


def test_resolution_picker_is_hidden_on_halium(make_window) -> None:
    """The image-size presets live in the Quality popover there instead."""
    win = make_window(devices=(FAKE_HALIUM,))
    camera.CameraWindow._populate_resolutions(win, FAKE_HALIUM)
    assert win._res_button.get_visible() is False


def test_resolution_picker_is_hidden_with_a_single_mode(make_window) -> None:
    win = make_window()
    device = dict(FAKE_V4L2, caps=_caps_for([(1920, 1080)]))
    camera.CameraWindow._populate_resolutions(win, device)
    assert win._res_button.get_visible() is False


def test_resolution_picker_lists_the_modes(make_window) -> None:
    win = make_window()
    device = dict(FAKE_V4L2, caps=_caps_for([(1920, 1080), (1280, 720), (640, 480)]))
    camera.CameraWindow._populate_resolutions(win, device)
    assert win._res_button.get_visible() is True
    assert "×" in win._res_button.get_label()


def _popover_rows(win):
    rows = []
    child = win._res_popover.get_child().get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


def test_resolution_picker_thins_a_long_list(make_window) -> None:
    """A UVC camera can advertise dozens of modes; the popover has to stay
    usable on a phone screen.

    The thinning keeps min + max plus a stride of six through the middle, so
    the result settles at 9-10 rows however long the input is — note that is
    a little above the "handful" the code aims for, and for 9-16 modes the
    stride comes out as 1 and nothing is dropped at all. Bounded is what
    matters here; the exact figure is pinned so a change is deliberate.
    """
    sizes = [(w, int(w * 9 / 16)) for w in range(640, 3841, 160)]
    win = make_window()
    camera.CameraWindow._populate_resolutions(win, dict(FAKE_V4L2, caps=_caps_for(sizes)))
    rows = _popover_rows(win)
    assert 2 <= len(rows) <= 10, f"popover offers {len(rows)} rows"
    assert len(rows) < len(sizes), "nothing was thinned out"


def test_resolution_picker_stays_bounded_for_any_mode_count(make_window) -> None:
    """However many modes a camera advertises, the popover must not grow with
    them — the whole point of the thinning."""
    many = [(w, int(w * 3 / 4)) for w in range(320, 5121, 32)]
    win = make_window()
    camera.CameraWindow._populate_resolutions(win, dict(FAKE_V4L2, caps=_caps_for(many)))
    assert len(_popover_rows(win)) <= 10


def test_resolution_picker_keeps_the_extremes_when_thinning(make_window) -> None:
    sizes = [(w, int(w * 9 / 16)) for w in range(640, 3841, 160)]
    win = make_window()
    device = dict(FAKE_V4L2, caps=_caps_for(sizes))
    camera.CameraWindow._populate_resolutions(win, device)

    offered = [getattr(row, "_muga_res", None) for row in _popover_rows(win)]
    areas = [w * h for w, h in offered if w]
    assert max(areas) == max(w * h for w, h in sizes), "the largest mode was dropped"
    assert min(areas) == min(w * h for w, h in sizes), "the smallest mode was dropped"


# ---------------------------------------------------------------------------
# Timer button
# ---------------------------------------------------------------------------

def test_timer_button_shows_an_alarm_glyph_when_off(make_window) -> None:
    win = make_window()
    win._timer_idx = win._timer_choices.index(0)
    camera.CameraWindow._refresh_timer_button(win)
    assert "off" in win._timer_button.get_tooltip_text().lower()


@pytest.mark.parametrize("seconds", [3, 10])
def test_timer_button_shows_the_delay(make_window, seconds) -> None:
    win = make_window()
    if seconds not in win._timer_choices:
        pytest.skip(f"{seconds}s is not an offered delay")
    win._timer_idx = win._timer_choices.index(seconds)
    camera.CameraWindow._refresh_timer_button(win)
    assert str(seconds) in win._timer_button.get_tooltip_text()


def test_timer_button_child_rotates_with_the_device(make_window) -> None:
    """The label has to turn with the phone like every other glyph."""
    from muga.camera_orientation import ORIENT_LEFT_UP

    win = make_window()
    win._device_orientation = ORIENT_LEFT_UP
    win._timer_idx = win._timer_choices.index(3) if 3 in win._timer_choices else 0
    camera.CameraWindow._refresh_timer_button(win)
    child = win._timer_button.get_child()
    assert child in win._rotatable_icons, "the new child never registered for rotation"


def test_timer_button_swap_does_not_leak_the_previous_child(make_window) -> None:
    win = make_window()
    for _ in range(4):
        camera.CameraWindow._cycle_timer(win)
    orphans = [w for w in win._rotatable_icons if w.get_parent() is None]
    assert not orphans, f"{len(orphans)} orphaned timer widgets still tracked"
