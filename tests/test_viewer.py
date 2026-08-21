"""Tests for the fullscreen photo/video viewer.

The navigation, zoom and gesture rules carry real decisions — a swipe has to
be told apart from a pinch and from a tap, browsing photos should not stop on
every video clip, and zoom has to anchor on the pinch centre rather than the
top-left. Those are pure logic and run headless.

The construction and item-switching tests build a real ViewerWindow (skipped
without a display, since GTK aborts rather than raises there).
"""

from __future__ import annotations

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
