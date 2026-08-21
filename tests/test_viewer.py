"""Tests for the fullscreen photo/video viewer.

The navigation, zoom and gesture rules carry real decisions — a swipe has to
be told apart from a pinch and from a tap, browsing photos should not stop on
every video clip, and zoom has to anchor on the pinch centre rather than the
top-left. Those are pure logic and run headless.

The construction and item-switching tests build a real ViewerWindow (skipped
without a display, since GTK aborts rather than raises there).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import requires_display

viewer = pytest.importorskip("yaga.viewer")

from yaga.models import MediaItem  # noqa: E402


def _item(path="/x/a.jpg", name="a.jpg", media_type="image") -> MediaItem:
    return MediaItem(id=1, path=path, category="photos", media_type=media_type,
                     folder="/x", name=name, mtime=1.7e9, size=1, thumb_path=None)


# ---------------------------------------------------------------------------
# Stepping through the list
# ---------------------------------------------------------------------------

def _nav_win(items, index=0):
    return SimpleNamespace(items=items, index=index, show_item=MagicMock())


def test_step_advances_and_wraps() -> None:
    win = _nav_win([_item(name=f"{i}.jpg") for i in range(3)])
    viewer.ViewerWindow._step(win, 1)
    assert win.index == 1
    win.index = 2
    viewer.ViewerWindow._step(win, 1)
    assert win.index == 0, "did not wrap to the start"


def test_step_backwards_wraps() -> None:
    win = _nav_win([_item(name=f"{i}.jpg") for i in range(3)], index=0)
    viewer.ViewerWindow._step(win, -1)
    assert win.index == 2


def test_step_skips_videos_while_browsing_photos() -> None:
    """Browsing a holiday folder should not stop on every clip."""
    items = [_item(name="a.jpg"), _item(name="b.mp4", media_type="video"),
             _item(name="c.jpg")]
    win = _nav_win(items, index=0)
    viewer.ViewerWindow._step(win, 1)
    assert win.index == 2, "stopped on the video"


def test_step_does_not_skip_when_already_on_a_video() -> None:
    """Once the user opened a video, next/previous should walk clips too."""
    items = [_item(name="a.mp4", media_type="video"),
             _item(name="b.mp4", media_type="video"), _item(name="c.jpg")]
    win = _nav_win(items, index=0)
    viewer.ViewerWindow._step(win, 1)
    assert win.index == 1


def test_step_wraps_back_to_the_only_photo() -> None:
    """One photo among videos: the skip loop comes all the way round to it."""
    items = [_item(name="a.jpg"), _item(name="b.mp4", media_type="video")]
    win = _nav_win(items, index=0)
    viewer.ViewerWindow._step(win, 1)
    assert win.index == 0


def test_step_always_lands_somewhere_in_a_video_only_list() -> None:
    """All videos: skipping is off (the current item is a video too), so the
    step is an ordinary one and must not hang looking for a photo."""
    items = [_item(name=f"{i}.mp4", media_type="video") for i in range(4)]
    win = _nav_win(items, index=0)
    viewer.ViewerWindow._step(win, 1)
    assert win.index == 1
    win.show_item.assert_called_once()


def test_step_skip_loop_always_terminates() -> None:
    """Exhaustive check over every photo/video arrangement up to five items.

    Note this also shows _step's ``for ... else`` branch ("Only videos in the
    list — keep current item") is dead code: skipping is only enabled when the
    *current* item is a photo, which guarantees the loop finds at least that
    item again before it runs out. Harmless, but it is not a safety net.
    """
    import itertools

    for n in range(1, 6):
        for kinds in itertools.product(["image", "video"], repeat=n):
            items = [_item(name=f"{i}.x", media_type=k) for i, k in enumerate(kinds)]
            for start in range(n):
                for direction in (1, -1):
                    win = _nav_win(list(items), index=start)
                    viewer.ViewerWindow._step(win, direction)
                    assert 0 <= win.index < n


def test_step_on_an_empty_list_is_a_noop() -> None:
    win = _nav_win([])
    viewer.ViewerWindow._step(win, 1)
    win.show_item.assert_not_called()


def test_step_by_zero_is_the_current_item() -> None:
    win = _nav_win([_item(name=f"{i}.jpg") for i in range(3)], index=1)
    viewer.ViewerWindow._step(win, 0)
    assert win.index == 1


# ---------------------------------------------------------------------------
# Swipe navigation
# ---------------------------------------------------------------------------

def _gesture_win(**extra):
    defaults = dict(
        zoom_scale=1.0, _zoom_committed=False, last_gesture_nav_at=0,
        previous=MagicMock(), next=MagicMock(),
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def test_swipe_left_goes_forward() -> None:
    win = _gesture_win()
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, -200, 0)
    win.next.assert_called_once()


def test_swipe_right_goes_back() -> None:
    win = _gesture_win()
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, 200, 0)
    win.previous.assert_called_once()


def test_swipe_is_ignored_while_zoomed_in() -> None:
    """Panning a zoomed photo must not flip to the next one."""
    win = _gesture_win(zoom_scale=2.0)
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, -300, 0)
    win.next.assert_not_called()


def test_swipe_is_ignored_during_a_pinch() -> None:
    win = _gesture_win(_zoom_committed=True)
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, -300, 0)
    win.next.assert_not_called()


def test_short_swipes_are_ignored() -> None:
    win = _gesture_win()
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, -50, 0)
    win.next.assert_not_called()


def test_vertical_swipes_are_ignored() -> None:
    """A mostly-vertical drag is a scroll, not a page turn."""
    win = _gesture_win()
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, -100, -200)
    win.next.assert_not_called()


def test_diagonal_swipe_needs_to_be_clearly_horizontal() -> None:
    win = _gesture_win()
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, -200, -150)
    win.next.assert_not_called()
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, -200, -50)
    win.next.assert_called_once()


def test_rapid_swipes_are_rate_limited() -> None:
    """A flick can emit both swipe and drag-end; without the guard one gesture
    skips two photos."""
    from gi.repository import GLib

    win = _gesture_win(last_gesture_nav_at=GLib.get_monotonic_time())
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, -300, 0)
    win.next.assert_not_called()


def test_swipe_records_its_timestamp() -> None:
    win = _gesture_win()
    viewer.ViewerWindow._navigate_from_horizontal_motion(win, -300, 0)
    assert win.last_gesture_nav_at > 0


# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------

def _zoom_win(**extra):
    defaults = dict(zoom_scale=1.0, zoom_start_scale=1.0,
                    zoom_view=None, zoom_scroller=None, _apply_zoom=MagicMock())
    defaults.update(extra)
    return SimpleNamespace(**defaults)


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0.2, 1.0), (1.0, 1.0), (2.5, 2.5), (6.0, 6.0), (99.0, 6.0)],
)
def test_zoom_is_clamped(requested, expected) -> None:
    """Below 1 the photo would shrink inside the viewport; above 6 it is just
    a wall of pixels."""
    win = _zoom_win()
    viewer.ViewerWindow._set_zoom(win, requested)
    assert win.zoom_scale == expected


def test_reset_zoom_returns_to_fit() -> None:
    win = _zoom_win(zoom_scale=3.0, zoom_start_scale=3.0)
    viewer.ViewerWindow._reset_zoom(win)
    assert win.zoom_scale == 1.0
    assert win.zoom_start_scale == 1.0


def test_pinch_below_one_percent_is_not_a_zoom() -> None:
    """Stray two-finger touches would otherwise nudge the zoom on every tap."""
    win = _zoom_win(_zoom_committed=False, _set_zoom=MagicMock(), _zoom_anchor=None)
    viewer.ViewerWindow._on_zoom_scale_changed(win, None, 1.005)
    win._set_zoom.assert_not_called()
    assert win._zoom_committed is False


def test_pinch_past_the_threshold_commits() -> None:
    win = _zoom_win(_zoom_committed=False, _set_zoom=MagicMock(), _zoom_anchor=None)
    viewer.ViewerWindow._on_zoom_scale_changed(win, None, 1.5)
    assert win._zoom_committed is True
    win._set_zoom.assert_called_once_with(1.5)


def test_adjustment_focus_keeps_the_pinch_centre_still() -> None:
    """Zooming should magnify what is under the fingers, not the top-left."""
    adj = MagicMock()
    adj.get_lower.return_value = 0.0
    adj.get_upper.return_value = 2000.0
    adj.get_page_size.return_value = 500.0
    viewer.ViewerWindow._set_adjustment_for_focus(
        SimpleNamespace(), adj, content_pos=400, scale=2.0, focus_pos=300,
    )
    adj.set_value.assert_called_once_with(500.0)


def test_adjustment_focus_clamps_to_the_scrollable_range() -> None:
    adj = MagicMock()
    adj.get_lower.return_value = 0.0
    adj.get_upper.return_value = 1000.0
    adj.get_page_size.return_value = 400.0
    viewer.ViewerWindow._set_adjustment_for_focus(
        SimpleNamespace(), adj, content_pos=5000, scale=4.0, focus_pos=0,
    )
    adj.set_value.assert_called_once_with(600.0)   # upper - page_size

    adj.reset_mock()
    viewer.ViewerWindow._set_adjustment_for_focus(
        SimpleNamespace(), adj, content_pos=0, scale=1.0, focus_pos=900,
    )
    adj.set_value.assert_called_once_with(0.0)


def test_apply_zoom_leaves_the_natural_size_at_fit() -> None:
    view_widget = MagicMock()
    win = _zoom_win(zoom_scale=1.0, zoom_view=view_widget,
                    zoom_scroller=MagicMock(), _apply_zoom=None)
    viewer.ViewerWindow._apply_zoom(win)
    view_widget.set_size_request.assert_called_once_with(-1, -1)


def test_apply_zoom_scales_the_request() -> None:
    view_widget = MagicMock()
    scroller = MagicMock()
    scroller.get_width.return_value = 800
    scroller.get_height.return_value = 600
    win = _zoom_win(zoom_scale=2.0, zoom_view=view_widget, zoom_scroller=scroller,
                    _apply_zoom=None, get_width=lambda: 800, get_height=lambda: 600)
    viewer.ViewerWindow._apply_zoom(win)
    view_widget.set_size_request.assert_called_once_with(1600, 1200)


def test_apply_zoom_without_a_mounted_view() -> None:
    win = _zoom_win(zoom_view=None, zoom_scroller=None, _apply_zoom=None)
    viewer.ViewerWindow._apply_zoom(win)


# ---------------------------------------------------------------------------
# Tap handling
# ---------------------------------------------------------------------------

def _tap_win(**extra):
    defaults = dict(
        _zoom_committed=False, _click_press_x=100.0, _click_press_y=100.0,
        _chrome_visible=True, _current_is_video=False,
        _set_chrome_visible=MagicMock(), _reset_zoom=MagicMock(),
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def test_single_tap_toggles_the_chrome() -> None:
    win = _tap_win(_chrome_visible=True)
    viewer.ViewerWindow._on_viewer_pressed(win, None, 1, 100.0, 100.0)
    win._set_chrome_visible.assert_called_once_with(False)


def test_double_tap_resets_the_zoom() -> None:
    win = _tap_win()
    viewer.ViewerWindow._on_viewer_pressed(win, None, 2, 100.0, 100.0)
    win._reset_zoom.assert_called_once()


def test_double_tap_on_a_video_does_not_reset_zoom() -> None:
    win = _tap_win(_current_is_video=True)
    viewer.ViewerWindow._on_viewer_pressed(win, None, 2, 100.0, 100.0)
    win._reset_zoom.assert_not_called()


def test_a_drag_is_not_a_tap() -> None:
    """Anything past ~12 px is a swipe; toggling the chrome on it makes the
    overlay flicker during every navigation."""
    win = _tap_win()
    viewer.ViewerWindow._on_viewer_pressed(win, None, 1, 200.0, 100.0)
    win._set_chrome_visible.assert_not_called()


def test_a_pinch_is_not_a_tap() -> None:
    win = _tap_win(_zoom_committed=True)
    viewer.ViewerWindow._on_viewer_pressed(win, None, 1, 100.0, 100.0)
    win._set_chrome_visible.assert_not_called()


def test_press_begin_records_the_origin() -> None:
    win = SimpleNamespace()
    viewer.ViewerWindow._on_viewer_press_begin(win, None, 1, 42.0, 24.0)
    assert (win._click_press_x, win._click_press_y) == (42.0, 24.0)


# ---------------------------------------------------------------------------
# Rotation bookkeeping
# ---------------------------------------------------------------------------

@requires_display
def test_rotate_by_step_accumulates_modulo_360() -> None:
    """Builds a real spinner into the stack, so it needs a display."""
    win = SimpleNamespace(
        _current_display_path="/x/a.jpg", _rotation=270,
        stack=MagicMock(), _reset_zoom=MagicMock(),
        zoom_view=None, zoom_scroller=None, _sensor_rotator=None,
        _rotate_worker=MagicMock(),
    )
    win.stack.get_first_child.return_value = None
    with patch.object(viewer.threading, "Thread") as thread:
        viewer.ViewerWindow._rotate_by_step(win, 1)
    assert win._rotation == 0, "270 + 90 must wrap to 0, not 360"
    thread.assert_called_once()


def test_rotate_by_step_needs_a_displayed_image() -> None:
    win = SimpleNamespace(_current_display_path=None, _rotation=0)
    viewer.ViewerWindow._rotate_by_step(win, 1)
    assert win._rotation == 0


def test_rotate_by_zero_steps_is_a_noop() -> None:
    win = SimpleNamespace(_current_display_path="/x/a.jpg", _rotation=90)
    viewer.ViewerWindow._rotate_by_step(win, 0)
    assert win._rotation == 90


# ---------------------------------------------------------------------------
# Real viewer window
# ---------------------------------------------------------------------------

@pytest.fixture
def viewer_window(tmp_path, gallery_window):
    from PIL import Image

    parent = gallery_window
    made = []

    def _make(count=3):
        items = []
        for i in range(count):
            path = tmp_path / f"photo{i}.jpg"
            Image.new("RGB", (120, 90), (i * 20, 40, 60)).save(path)
            items.append(MediaItem(
                id=i, path=str(path), category="photos", media_type="image",
                folder=str(tmp_path), name=path.name, mtime=1.7e9 - i,
                size=path.stat().st_size, thumb_path=None,
            ))
        win = viewer.ViewerWindow(parent, items, 0, "")
        made.append(win)
        return win

    yield _make

    for win in made:
        win._closing = True


@requires_display
def test_viewer_builds(viewer_window) -> None:
    win = viewer_window()
    assert win.index == 0
    assert len(win.items) == 3
    assert win.zoom_scale == 1.0


@requires_display
def test_viewer_shows_the_filename(viewer_window) -> None:
    win = viewer_window()
    win._update_filename_label(win.items[0])
    assert "photo0.jpg" in win.filename_label.get_text()


@requires_display
def test_viewer_navigation_moves_through_the_list(viewer_window) -> None:
    win = viewer_window()
    win._do_next()
    assert win.index == 1
    win._do_previous()
    assert win.index == 0


@requires_display
def test_viewer_rotation_resets_on_item_change(viewer_window) -> None:
    """An unsaved rotation belongs to the photo it was made on."""
    win = viewer_window()
    win._rotation = 90
    win.index = 1
    win.show_item()
    assert win._rotation == 0


# ---------------------------------------------------------------------------
# EXIF read-out for the info panel
# ---------------------------------------------------------------------------

def _photo_with_exif(tmp_path: Path, tags: dict | None = None, gps: dict | None = None) -> str:
    """Write a JPEG carrying *tags* in the 0th IFD and *gps* in the GPS IFD."""
    PILImage = pytest.importorskip("PIL.Image")
    exif = PILImage.Exif()
    for tag, value in (tags or {}).items():
        exif[tag] = value
    if gps:
        exif.get_ifd(0x8825).update(gps)
    path = tmp_path / "shot.jpg"
    PILImage.new("RGB", (40, 30), (9, 9, 9)).save(path, exif=exif.tobytes())
    return str(path)


def _dms(value: float):
    from PIL.TiffImagePlugin import IFDRational

    deg = int(value)
    minutes = int((value - deg) * 60)
    seconds = (value - deg - minutes / 60) * 3600
    return (IFDRational(deg, 1), IFDRational(minutes, 1),
            IFDRational(int(seconds * 10000), 10000))


def test_exif_readout_combines_make_and_model(tmp_path: Path) -> None:
    path = _photo_with_exif(tmp_path, {271: "Canon", 272: "EOS R5"})
    assert viewer._extract_exif(path)["Camera"] == "Canon EOS R5"


def test_exif_readout_with_only_a_model(tmp_path: Path) -> None:
    path = _photo_with_exif(tmp_path, {272: "Pixel 8"})
    assert viewer._extract_exif(path)["Camera"] == "Pixel 8"


def test_exif_readout_with_only_a_make(tmp_path: Path) -> None:
    path = _photo_with_exif(tmp_path, {271: "Yaga"})
    assert viewer._extract_exif(path)["Camera"] == "Yaga"


def test_exif_readout_strips_padding(tmp_path: Path) -> None:
    """Cameras routinely pad these fields with spaces or NULs."""
    path = _photo_with_exif(tmp_path, {271: "  Canon  ", 272: " EOS  "})
    assert viewer._extract_exif(path)["Camera"] == "Canon EOS"


def test_exif_readout_of_a_photo_without_tags(tmp_path: Path) -> None:
    PILImage = pytest.importorskip("PIL.Image")
    path = tmp_path / "bare.jpg"
    PILImage.new("RGB", (10, 10)).save(path)
    assert viewer._extract_exif(str(path)) == {}


def test_exif_readout_converts_gps_to_decimal(tmp_path: Path) -> None:
    path = _photo_with_exif(tmp_path, gps={1: "N", 2: _dms(52.5200), 3: "E", 4: _dms(13.4050)})
    gps = viewer._extract_exif(path)["GPS"]
    lat, lon = (float(part) for part in gps.split(","))
    assert lat == pytest.approx(52.52, abs=0.001)
    assert lon == pytest.approx(13.405, abs=0.001)


def test_exif_readout_signs_southern_and_western_coordinates(tmp_path: Path) -> None:
    path = _photo_with_exif(tmp_path, gps={1: "S", 2: _dms(33.8688), 3: "W", 4: _dms(151.2093)})
    lat, lon = (float(p) for p in viewer._extract_exif(path)["GPS"].split(","))
    assert lat < 0 and lon < 0


def test_exif_readout_skips_a_half_written_gps_block(tmp_path: Path) -> None:
    """Latitude without longitude is not a position."""
    path = _photo_with_exif(tmp_path, gps={1: "N", 2: _dms(52.52)})
    assert "GPS" not in viewer._extract_exif(path)


def test_exif_readout_survives_a_truncated_gps_triple(tmp_path: Path) -> None:
    """Degrees and minutes but no seconds — the shape a partially written or
    truncated GPS block actually has. The camera name must survive it."""
    from PIL.TiffImagePlugin import IFDRational

    two_of_three = (IFDRational(52, 1), IFDRational(31, 1))
    path = _photo_with_exif(tmp_path, {272: "TestCam"},
                            gps={1: "N", 2: two_of_three, 3: "E", 4: two_of_three})
    result = viewer._extract_exif(path)
    assert result.get("Camera") == "TestCam", "the camera name went down with the GPS"
    assert "GPS" not in result


def test_exif_readout_survives_a_zero_denominator(tmp_path: Path) -> None:
    """A 0/0 rational is what some cameras write for "no fix"."""
    from PIL.TiffImagePlugin import IFDRational

    broken = (IFDRational(52, 1), IFDRational(31, 1), IFDRational(1, 0))
    path = _photo_with_exif(tmp_path, {272: "TestCam"},
                            gps={1: "N", 2: broken, 3: "E", 4: broken})
    result = viewer._extract_exif(path)
    assert result.get("Camera") == "TestCam"


def test_exif_readout_of_a_missing_file(tmp_path: Path) -> None:
    assert viewer._extract_exif(str(tmp_path / "gone.jpg")) == {}


def test_exif_readout_of_a_non_image(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    assert viewer._extract_exif(str(path)) == {}


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

def _key_win(**extra):
    from gi.repository import Gdk

    defaults = dict(
        _editor=None, previous=MagicMock(), next=MagicMock(),
        _toggle_fullscreen=MagicMock(), close=MagicMock(),
        _exit_edit_mode=MagicMock(), _on_editor_undo=MagicMock(),
        _on_editor_redo=MagicMock(),
        props=SimpleNamespace(fullscreened=False),
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults), Gdk


@pytest.mark.parametrize("key", ["KEY_Left", "KEY_Up"])
def test_key_goes_back(key) -> None:
    win, Gdk = _key_win()
    assert viewer.ViewerWindow._on_key(win, None, getattr(Gdk, key), 0, 0) is True
    win.previous.assert_called_once()


@pytest.mark.parametrize("key", ["KEY_Right", "KEY_Down", "KEY_space"])
def test_key_goes_forward(key) -> None:
    win, Gdk = _key_win()
    assert viewer.ViewerWindow._on_key(win, None, getattr(Gdk, key), 0, 0) is True
    win.next.assert_called_once()


def test_f11_toggles_fullscreen() -> None:
    win, Gdk = _key_win()
    viewer.ViewerWindow._on_key(win, None, Gdk.KEY_F11, 0, 0)
    win._toggle_fullscreen.assert_called_once()


def test_escape_leaves_fullscreen_before_closing() -> None:
    """Otherwise a fullscreen viewer closes the window on the first Escape and
    the user loses their place."""
    win, Gdk = _key_win(props=SimpleNamespace(fullscreened=True))
    viewer.ViewerWindow._on_key(win, None, Gdk.KEY_Escape, 0, 0)
    win._toggle_fullscreen.assert_called_once()
    win.close.assert_not_called()


def test_escape_closes_a_windowed_viewer() -> None:
    win, Gdk = _key_win()
    viewer.ViewerWindow._on_key(win, None, Gdk.KEY_Escape, 0, 0)
    win.close.assert_called_once()


def test_unhandled_keys_pass_through() -> None:
    win, Gdk = _key_win()
    assert viewer.ViewerWindow._on_key(win, None, Gdk.KEY_a, 0, 0) is False


def test_escape_leaves_edit_mode_first() -> None:
    win, Gdk = _key_win(_editor=MagicMock())
    assert viewer.ViewerWindow._on_key(win, None, Gdk.KEY_Escape, 0, 0) is True
    win._exit_edit_mode.assert_called_once()
    win.close.assert_not_called()


def test_navigation_keys_are_inert_in_edit_mode() -> None:
    """Arrow keys belong to the editor's own widgets while it is open."""
    win, Gdk = _key_win(_editor=MagicMock())
    assert viewer.ViewerWindow._on_key(win, None, Gdk.KEY_Right, 0, 0) is False
    win.next.assert_not_called()


def test_ctrl_z_undoes_in_the_editor() -> None:
    win, Gdk = _key_win(_editor=MagicMock())
    viewer.ViewerWindow._on_key(win, None, Gdk.KEY_z, 0, Gdk.ModifierType.CONTROL_MASK)
    win._on_editor_undo.assert_called_once()


def test_ctrl_shift_z_redoes() -> None:
    """GIMP/Inkscape convention, deliberately not the desktop default — the
    shortcut should not change with the desktop."""
    win, Gdk = _key_win(_editor=MagicMock())
    mods = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
    viewer.ViewerWindow._on_key(win, None, Gdk.KEY_Z, 0, mods)
    win._on_editor_redo.assert_called_once()
    win._on_editor_undo.assert_not_called()


def test_ctrl_y_also_redoes() -> None:
    win, Gdk = _key_win(_editor=MagicMock())
    viewer.ViewerWindow._on_key(win, None, Gdk.KEY_y, 0, Gdk.ModifierType.CONTROL_MASK)
    win._on_editor_redo.assert_called_once()


def test_plain_z_is_not_an_undo() -> None:
    win, Gdk = _key_win(_editor=MagicMock())
    assert viewer.ViewerWindow._on_key(win, None, Gdk.KEY_z, 0, 0) is False
    win._on_editor_undo.assert_not_called()


# ---------------------------------------------------------------------------
# Item switching in a real viewer
# ---------------------------------------------------------------------------

@requires_display
def test_show_item_decodes_and_mounts_the_photo(viewer_window, pump) -> None:
    """The decode runs on a worker and lands through idle_add, so nothing is
    on screen until the loop turns."""
    win = viewer_window()
    win.show_item()
    pump()
    assert win.stack.get_first_child() is not None
    assert win._current_display_path == win.items[0].path


@requires_display
def test_show_item_discards_a_stale_decode(viewer_window, pump) -> None:
    """Swiping fast means the previous item's decode lands after the new one
    was requested; without the token it would paint over the new photo."""
    win = viewer_window()
    win.show_item()
    first_token = win._show_token
    win.index = 1
    win.show_item()
    pump()
    assert win._show_token > first_token
    assert win._current_display_path == win.items[1].path


@requires_display
def test_show_item_resets_the_view_state(viewer_window, pump) -> None:
    win = viewer_window()
    win.zoom_scale = 3.0
    win._rotation = 90
    win.index = 1
    win.show_item()
    pump()
    assert win.zoom_scale == 1.0
    assert win._rotation == 0


@requires_display
def test_show_item_enables_the_photo_actions(viewer_window, pump) -> None:
    win = viewer_window()
    win.show_item()
    pump()
    assert win.delete_button.get_visible() is True
    assert win.rotate_button.get_visible() is True


@requires_display
def test_show_item_keeps_the_chrome_choice_across_navigation(viewer_window, pump) -> None:
    """Otherwise the header and pills snap back into view on every swipe."""
    win = viewer_window()
    win._set_chrome_visible(False)
    win.index = 1
    win.show_item()
    pump()
    assert win._chrome_visible is False


@requires_display
def test_show_item_hides_photo_actions_for_a_video(viewer_window, pump, tmp_path) -> None:
    win = viewer_window()
    video = dataclasses_replace_media_type(win.items[0], "video")
    win.items = [video]
    win.index = 0
    win.show_item()
    pump()
    assert win._current_is_video is True
    assert win.rotate_button.get_visible() is False


def dataclasses_replace_media_type(item, media_type):
    import dataclasses

    return dataclasses.replace(item, media_type=media_type)


@requires_display
def test_viewer_blocks_a_nextcloud_item_while_disconnected(viewer_window, pump) -> None:
    """Opening an NC photo with the session off must explain itself rather
    than spin on a download that cannot happen."""
    import dataclasses

    win = viewer_window()
    nc_item = dataclasses.replace(win.items[0], path="nextcloud://Photos/a.jpg")
    win.items = [nc_item]
    win.index = 0
    with patch.object(type(win.parent_window), "is_nc_active", return_value=False):
        win.show_item()
    pump()
    assert win.stack.get_first_child() is not None


@requires_display
def test_rotated_pixbuf_is_ignored_after_close(viewer_window, pump) -> None:
    """The rotate worker can land after the window went away."""
    win = viewer_window()
    win._closing = True
    win._show_rotated_pixbuf(None)   # must not touch the torn-down stack


# ---------------------------------------------------------------------------
# EXIF cache
# ---------------------------------------------------------------------------

def _cache_win(cached=None, **extra):
    database = MagicMock()
    database.get_exif_data.return_value = cached
    parent = SimpleNamespace(database=database, _=lambda s: s)
    return SimpleNamespace(parent_window=parent, **extra), database


def test_exif_cache_hit_avoids_parsing() -> None:
    """Parsing is the expensive half; a hit must not touch the file at all."""
    win, database = _cache_win(cached='{"Camera": "TestCam"}')
    assert viewer.ViewerWindow._get_cached_exif_only(win, _item()) == {"Camera": "TestCam"}
    database.get_exif_data.assert_called_once()


def test_exif_cache_miss_is_reported_as_none() -> None:
    """None means "decide for yourself whether to parse", which is not the
    same as an empty dict ("parsed, nothing found")."""
    win, _ = _cache_win(cached=None)
    assert viewer.ViewerWindow._get_cached_exif_only(win, _item()) is None
    win2, _ = _cache_win(cached="")
    assert viewer.ViewerWindow._get_cached_exif_only(win2, _item()) is None


def test_exif_cache_survives_a_corrupt_entry() -> None:
    win, _ = _cache_win(cached="{not json")
    assert viewer.ViewerWindow._get_cached_exif_only(win, _item()) is None


def test_exif_parse_persists_what_it_found(tmp_path: Path) -> None:
    path = _photo_with_exif(tmp_path, {272: "TestCam"})
    win, database = _cache_win()
    item = _item(path=path)

    result = viewer.ViewerWindow._parse_and_cache_exif(win, item)

    assert result["Camera"] == "TestCam"
    database.set_exif_data.assert_called_once()
    assert "TestCam" in database.set_exif_data.call_args[0][1]
    database.commit.assert_called_once()


def test_exif_parse_does_not_cache_an_empty_result(tmp_path: Path) -> None:
    """A photo without EXIF would otherwise write an empty row and the miss
    would be indistinguishable from a hit."""
    PILImage = pytest.importorskip("PIL.Image")
    path = tmp_path / "bare.jpg"
    PILImage.new("RGB", (10, 10)).save(path)
    win, database = _cache_win()

    assert viewer.ViewerWindow._parse_and_cache_exif(win, _item(path=str(path))) == {}
    database.set_exif_data.assert_not_called()


def test_exif_parse_returns_its_result_when_caching_fails(tmp_path: Path) -> None:
    path = _photo_with_exif(tmp_path, {272: "TestCam"})
    win, database = _cache_win()
    database.set_exif_data.side_effect = RuntimeError("database is locked")
    result = viewer.ViewerWindow._parse_and_cache_exif(win, _item(path=path))
    assert result["Camera"] == "TestCam"


# ---------------------------------------------------------------------------
# Deleting the current item
# ---------------------------------------------------------------------------

def _trashed():
    """Stand in for Gio.File.trash.

    tmpfs has no trash directory, so the real call raises and the method
    returns early — these tests are about what happens to the list and the
    index afterwards, not about GIO's trash implementation.
    """
    from gi.repository import Gio

    return patch.object(Gio.File, "trash", return_value=True)


@requires_display
def test_delete_removes_the_item_and_moves_on(viewer_window, pump) -> None:
    win = viewer_window(count=3)
    win.show_item()
    pump()
    doomed = win.items[0].path

    with _trashed(), patch.object(type(win.parent_window), "refresh"):
        win._delete_current_item()
    pump()

    assert len(win.items) == 2
    assert doomed not in [i.path for i in win.items]


@requires_display
def test_delete_drops_every_index_row_for_the_path(viewer_window, pump) -> None:
    """Overview and Videos can hold a row for the same path under another
    category, so the delete is deliberately category-agnostic — a scoped one
    left the tile behind."""
    win = viewer_window(count=2)
    win.show_item()
    pump()
    doomed = win.items[0].path
    with _trashed(), patch.object(type(win.parent_window), "refresh"), \
         patch.object(type(win.parent_window.database), "delete_path") as delete:
        win._delete_current_item()
    delete.assert_called_once_with(doomed)


@requires_display
def test_delete_of_the_last_item_closes_the_viewer(viewer_window, pump) -> None:
    win = viewer_window(count=1)
    win.show_item()
    pump()
    with _trashed(), patch.object(type(win.parent_window), "refresh"), \
         patch.object(type(win), "close") as close:
        win._delete_current_item()
    close.assert_called_once()


@requires_display
def test_delete_clamps_the_index_at_the_end(viewer_window, pump) -> None:
    """Deleting the last photo has to step back, not off the end."""
    win = viewer_window(count=3)
    win.index = 2
    win.show_item()
    pump()
    with _trashed(), patch.object(type(win.parent_window), "refresh"):
        win._delete_current_item()
    pump()
    assert win.index == 1


@requires_display
def test_delete_on_an_empty_list_closes(viewer_window) -> None:
    win = viewer_window(count=1)
    win.items = []
    with patch.object(type(win), "close") as close:
        win._delete_current_item()
    close.assert_called_once()


@requires_display
def test_delete_reports_a_failure_and_keeps_the_item(viewer_window, pump) -> None:
    from gi.repository import Gio, GLib

    win = viewer_window(count=2)
    win.show_item()
    pump()
    before = len(win.items)
    with patch.object(Gio.File, "trash", side_effect=GLib.Error("read-only fs")), \
         patch.object(type(win.parent_window), "_set_status") as status:
        win._delete_current_item()
    assert len(win.items) == before, "the item vanished from the list but not from disk"
    status.assert_called_once()


# ---------------------------------------------------------------------------
# Edit mode
# ---------------------------------------------------------------------------

@requires_display
def test_edit_mode_swaps_the_header_actions(viewer_window, pump) -> None:
    win = viewer_window()
    win.show_item()
    pump()
    win._enter_edit_mode()
    pump()

    assert win._editor is not None
    assert win.save_edit_button.get_visible() is True
    assert win.cancel_edit_button.get_visible() is True
    assert win.delete_button.get_visible() is False
    assert win.rotate_button.get_visible() is False

    win._exit_edit_mode()
    pump()
    assert win._editor is None
    assert win.save_edit_button.get_visible() is False
    assert win.close_button.get_visible() is True


@requires_display
def test_edit_mode_refuses_a_video(viewer_window, pump) -> None:
    import dataclasses

    win = viewer_window()
    win.items = [dataclasses.replace(win.items[0], media_type="video")]
    win.index = 0
    win._enter_edit_mode()
    assert win._editor is None


@requires_display
def test_edit_mode_refuses_a_raw_file(viewer_window, pump) -> None:
    """Pillow cannot open them, so the editor would come up empty."""
    import dataclasses

    win = viewer_window()
    raw = dataclasses.replace(win.items[0], path="/x/photo.dng", name="photo.dng")
    win.items = [raw]
    win.index = 0
    win._current_display_path = raw.path
    with patch.object(type(win.parent_window), "_set_status") as status:
        win._enter_edit_mode()
    assert win._editor is None
    status.assert_called_once()


@requires_display
def test_edit_mode_refuses_a_nextcloud_item(viewer_window, pump) -> None:
    """The editor works on local files; an NC path has no local original."""
    import dataclasses

    win = viewer_window()
    win.items = [dataclasses.replace(win.items[0], path="nextcloud://Photos/a.jpg")]
    win.index = 0
    win._current_display_path = None
    with patch.object(type(win.parent_window), "_set_status") as status:
        win._enter_edit_mode()
    assert win._editor is None
    status.assert_called_once()


@requires_display
def test_edit_mode_refuses_a_vanished_file(viewer_window, pump, tmp_path) -> None:
    import dataclasses

    win = viewer_window()
    win.items = [dataclasses.replace(win.items[0], path=str(tmp_path / "gone.jpg"))]
    win.index = 0
    win._current_display_path = str(tmp_path / "gone.jpg")
    with patch.object(type(win.parent_window), "_set_status"):
        win._enter_edit_mode()
    assert win._editor is None


@requires_display
def test_edit_mode_stops_a_running_slideshow(viewer_window, pump) -> None:
    win = viewer_window()
    win.show_item()
    pump()
    win._slideshow_active = True
    with patch.object(type(win), "_stop_slideshow") as stop:
        win._enter_edit_mode()
    stop.assert_called_once()
    win._exit_edit_mode()
    pump()


@requires_display
def test_exit_edit_mode_cleans_the_editor_up(viewer_window, pump) -> None:
    """A detached editor keeps its full-resolution copies alive until its
    pending timeout would have fired."""
    win = viewer_window()
    win.show_item()
    pump()
    win._enter_edit_mode()
    pump()
    editor = win._editor
    with patch.object(type(editor), "cleanup") as cleanup:
        win._exit_edit_mode()
    cleanup.assert_called_once()
    pump()


@requires_display
def test_exit_edit_mode_without_an_editor(viewer_window, pump) -> None:
    win = viewer_window()
    win.show_item()
    pump()
    win._exit_edit_mode()   # must not raise
    pump()
