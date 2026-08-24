"""Tests for camera.py's orientation-driven layout.

The camera window has no title bar: the shutter and the options row are
positioned by hand for each combination of device orientation and handedness,
so that the shutter lands under the user's thumb however the phone is held.
The four orientations form two mirror pairs, and the code deliberately
expresses one canonical case per pair plus a mechanical flip — these tests pin
that symmetry down, because a hand-tweaked mirror is exactly what drifts.

Gtk.Align and Gtk.Orientation are enums, not widgets, so this runs headless.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

camera = pytest.importorskip("muga.camera")

from gi.repository import Gtk  # noqa: E402

from muga.camera_orientation import (  # noqa: E402
    ORIENT_BOTTOM_UP,
    ORIENT_LEFT_UP,
    ORIENT_NORMAL,
    ORIENT_RIGHT_UP,
)

ALL = (ORIENT_NORMAL, ORIENT_BOTTOM_UP, ORIENT_LEFT_UP, ORIENT_RIGHT_UP)


# ---------------------------------------------------------------------------
# Align flipping
# ---------------------------------------------------------------------------

def test_flip_align_swaps_the_ends() -> None:
    flip = camera.CameraWindow._flip_align
    assert flip(Gtk.Align.START) == Gtk.Align.END
    assert flip(Gtk.Align.END) == Gtk.Align.START


@pytest.mark.parametrize("align", [Gtk.Align.CENTER, Gtk.Align.FILL, Gtk.Align.BASELINE])
def test_flip_align_leaves_symmetric_alignments_alone(align) -> None:
    assert camera.CameraWindow._flip_align(align) == align


def test_flip_align_is_an_involution() -> None:
    flip = camera.CameraWindow._flip_align
    for align in (Gtk.Align.START, Gtk.Align.END, Gtk.Align.CENTER):
        assert flip(flip(align)) == align


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

def _placement_win():
    win = SimpleNamespace()
    win._flip_align = camera.CameraWindow._flip_align
    return win


def test_apply_placement_sets_aligns_and_margins() -> None:
    widget = MagicMock()
    camera.CameraWindow._apply_placement(
        _placement_win(), widget,
        halign=Gtk.Align.END, valign=Gtk.Align.START,
        m_top=10, m_bottom=20, m_start=30, m_end=40,
    )
    widget.set_halign.assert_called_once_with(Gtk.Align.END)
    widget.set_valign.assert_called_once_with(Gtk.Align.START)
    widget.set_margin_top.assert_called_once_with(10)
    widget.set_margin_bottom.assert_called_once_with(20)
    widget.set_margin_start.assert_called_once_with(30)
    widget.set_margin_end.assert_called_once_with(40)


def test_apply_placement_flip_mirrors_both_axes() -> None:
    widget = MagicMock()
    camera.CameraWindow._apply_placement(
        _placement_win(), widget,
        halign=Gtk.Align.END, valign=Gtk.Align.START,
        m_top=10, m_bottom=20, m_start=30, m_end=40,
        flip=True,
    )
    widget.set_halign.assert_called_once_with(Gtk.Align.START)
    widget.set_valign.assert_called_once_with(Gtk.Align.END)
    widget.set_margin_top.assert_called_once_with(20)
    widget.set_margin_bottom.assert_called_once_with(10)
    widget.set_margin_start.assert_called_once_with(40)
    widget.set_margin_end.assert_called_once_with(30)


def test_apply_placement_zeroes_what_it_is_not_given() -> None:
    """Margins are reset upstream, so an unset margin must land as 0 rather
    than inheriting the previous orientation's value."""
    widget = MagicMock()
    camera.CameraWindow._apply_placement(
        _placement_win(), widget, halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
    )
    for setter in (widget.set_margin_top, widget.set_margin_bottom,
                   widget.set_margin_start, widget.set_margin_end):
        setter.assert_called_once_with(0)


# ---------------------------------------------------------------------------
# Layout dispatch
# ---------------------------------------------------------------------------

def _layout_win(**extra):
    defaults = dict(
        _applied_layout=None,
        _layout_is_landscape=False,
        _handedness="right",
        _rotatable_icons=[],
        _quality_button=MagicMock(),
        _settings_button=MagicMock(),
        _build_quality_popover=MagicMock(),
        _build_settings_popover=MagicMock(),
        _position_record_dot=MagicMock(),
        _position_swipe_hint=MagicMock(),
        _shutter=MagicMock(),
        _options_bar=MagicMock(),
        _layout_portrait=MagicMock(),
        _layout_landscape=MagicMock(),
        _layout_landscape_right_center=MagicMock(),
        get_height=lambda: 1600,
        get_width=lambda: 720,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def test_layout_is_skipped_when_nothing_changed() -> None:
    """It rebuilds both popovers, so re-running it on every orientation tick
    would churn widgets 60 times a second."""
    win = _layout_win(_applied_layout=ORIENT_NORMAL)
    camera.CameraWindow._apply_layout_for(win, ORIENT_NORMAL)
    win._build_quality_popover.assert_not_called()
    win._layout_portrait.assert_not_called()


@pytest.mark.parametrize(
    ("orientation", "handedness", "expected"),
    [
        (ORIENT_NORMAL, "right", "_layout_portrait"),
        (ORIENT_BOTTOM_UP, "right", "_layout_portrait"),
        (ORIENT_LEFT_UP, "right", "_layout_landscape_right_center"),
        (ORIENT_RIGHT_UP, "right", "_layout_landscape_right_center"),
        (ORIENT_NORMAL, "left", "_layout_portrait"),
        (ORIENT_LEFT_UP, "left", "_layout_landscape"),
        (ORIENT_RIGHT_UP, "left", "_layout_landscape"),
        (ORIENT_NORMAL, "neutral", "_layout_portrait"),
        (ORIENT_LEFT_UP, "neutral", "_layout_portrait"),
        (ORIENT_RIGHT_UP, "neutral", "_layout_portrait"),
    ],
)
def test_layout_dispatch(orientation, handedness, expected) -> None:
    win = _layout_win(_handedness=handedness)
    camera.CameraWindow._apply_layout_for(win, orientation)
    for name in ("_layout_portrait", "_layout_landscape", "_layout_landscape_right_center"):
        called = getattr(win, name).called
        assert called == (name == expected), f"{name} called={called} for {orientation}/{handedness}"


def test_neutral_keeps_portrait_but_still_flips_upside_down() -> None:
    """Neutral handedness ignores landscape, but an upside-down phone must
    still swap the icons row and the shutter."""
    win = _layout_win(_handedness="neutral")
    camera.CameraWindow._apply_layout_for(win, ORIENT_BOTTOM_UP)
    assert win._layout_portrait.call_args.kwargs["flip_180"] is True

    win2 = _layout_win(_handedness="neutral")
    camera.CameraWindow._apply_layout_for(win2, ORIENT_LEFT_UP)
    assert win2._layout_portrait.call_args.kwargs["flip_180"] is False


@pytest.mark.parametrize(
    ("orientation", "is_right_up"),
    [(ORIENT_LEFT_UP, False), (ORIENT_RIGHT_UP, True)],
)
def test_landscape_pairs_flip_on_right_up(orientation, is_right_up) -> None:
    win = _layout_win(_handedness="left")
    camera.CameraWindow._apply_layout_for(win, orientation)
    assert win._layout_landscape.call_args.kwargs["is_right_up"] is is_right_up


def test_layout_rotates_every_icon() -> None:
    icons = [MagicMock() for _ in range(3)]
    win = _layout_win(_rotatable_icons=icons)
    camera.CameraWindow._apply_layout_for(win, ORIENT_LEFT_UP)
    for icon in icons:
        icon.set_rotation_deg.assert_called_once_with(
            camera._ICON_ROTATION_DEG[ORIENT_LEFT_UP],
        )


def test_layout_prunes_orphaned_icons_after_rebuilding_popovers() -> None:
    """Pruning has to come after set_popover, because set_popover is what
    orphans the old popover's labels."""
    live, orphan = MagicMock(), MagicMock()
    live.get_parent.return_value = object()
    orphan.get_parent.return_value = None
    win = _layout_win(_rotatable_icons=[live, orphan])

    camera.CameraWindow._apply_layout_for(win, ORIENT_NORMAL)

    assert win._rotatable_icons == [live]
    orphan.set_rotation_deg.assert_not_called()


def test_layout_resets_margins_before_positioning() -> None:
    """Otherwise the previous orientation's margins leak into the new one."""
    win = _layout_win()
    camera.CameraWindow._apply_layout_for(win, ORIENT_NORMAL)
    for widget in (win._shutter, win._options_bar):
        for setter in (widget.set_margin_top, widget.set_margin_bottom,
                       widget.set_margin_start, widget.set_margin_end):
            setter.assert_any_call(0)


def test_layout_survives_a_failing_popover_rebuild() -> None:
    """A popover that fails to build must not leave the shutter unpositioned."""
    win = _layout_win(_build_quality_popover=MagicMock(side_effect=RuntimeError("boom")))
    camera.CameraWindow._apply_layout_for(win, ORIENT_NORMAL)
    win._layout_portrait.assert_called_once()


def test_layout_survives_a_failing_record_dot() -> None:
    win = _layout_win(_position_record_dot=MagicMock(side_effect=RuntimeError("boom")))
    camera.CameraWindow._apply_layout_for(win, ORIENT_NORMAL)
    win._layout_portrait.assert_called_once()


def test_layout_offsets_scale_with_the_window() -> None:
    win = _layout_win(get_height=lambda: 2400, get_width=lambda: 1080)
    camera.CameraWindow._apply_layout_for(win, ORIENT_NORMAL)
    kwargs = win._layout_portrait.call_args.kwargs
    assert kwargs["third"] == 400
    assert kwargs["user_vertical"] == 360


def test_layout_offsets_have_a_floor_on_a_tiny_window() -> None:
    win = _layout_win(get_height=lambda: 0, get_width=lambda: 0)
    camera.CameraWindow._apply_layout_for(win, ORIENT_NORMAL)
    kwargs = win._layout_portrait.call_args.kwargs
    assert kwargs["third"] == 48
    assert kwargs["user_vertical"] == 120


# ---------------------------------------------------------------------------
# Portrait / landscape placement
# ---------------------------------------------------------------------------

def _place_win(**extra):
    win = SimpleNamespace(
        _shutter=MagicMock(), _options_bar=MagicMock(),
        _flip_align=camera.CameraWindow._flip_align, **extra,
    )
    win._apply_placement = camera.CameraWindow._apply_placement.__get__(win, type(win))
    return win


def test_portrait_right_handed_puts_the_shutter_bottom_right() -> None:
    win = _place_win()
    camera.CameraWindow._layout_portrait(
        win, flip_180=False, neutral=False, right=True, third=200, user_vertical=240,
    )
    win._shutter.set_halign.assert_called_once_with(Gtk.Align.END)
    win._shutter.set_valign.assert_called_once_with(Gtk.Align.END)
    win._options_bar.set_halign.assert_called_once_with(Gtk.Align.CENTER)
    win._options_bar.set_valign.assert_called_once_with(Gtk.Align.START)


def test_portrait_left_handed_mirrors_the_shutter() -> None:
    win = _place_win()
    camera.CameraWindow._layout_portrait(
        win, flip_180=False, neutral=False, right=False, third=200, user_vertical=240,
    )
    win._shutter.set_halign.assert_called_once_with(Gtk.Align.START)


def test_portrait_neutral_centres_the_shutter() -> None:
    win = _place_win()
    camera.CameraWindow._layout_portrait(
        win, flip_180=False, neutral=True, right=False, third=200, user_vertical=240,
    )
    win._shutter.set_halign.assert_called_once_with(Gtk.Align.CENTER)


def test_portrait_flip_180_is_the_exact_mirror() -> None:
    """The upside-down layout is generated, not hand-written; if it ever
    stops matching the mirror, that is a bug."""
    upright, flipped = _place_win(), _place_win()
    kw = dict(neutral=False, right=True, third=200, user_vertical=240)
    camera.CameraWindow._layout_portrait(upright, flip_180=False, **kw)
    camera.CameraWindow._layout_portrait(flipped, flip_180=True, **kw)

    flip = camera.CameraWindow._flip_align
    assert flipped._shutter.set_halign.call_args[0][0] == flip(
        upright._shutter.set_halign.call_args[0][0])
    assert flipped._shutter.set_valign.call_args[0][0] == flip(
        upright._shutter.set_valign.call_args[0][0])
    # Margins swap across both axes.
    assert (flipped._shutter.set_margin_top.call_args[0][0]
            == upright._shutter.set_margin_bottom.call_args[0][0])
    assert (flipped._shutter.set_margin_start.call_args[0][0]
            == upright._shutter.set_margin_end.call_args[0][0])


def test_landscape_right_center_pins_the_shutter_to_the_users_right() -> None:
    """LEFT_UP maps the user's right edge to the widget's top."""
    win = _place_win()
    camera.CameraWindow._layout_landscape_right_center(
        win, is_right_up=False, neutral=False, right=True, third=200, user_vertical=240,
    )
    win._shutter.set_valign.assert_called_once_with(Gtk.Align.START)
    win._options_bar.set_valign.assert_called_once_with(Gtk.Align.END)


def test_landscape_right_center_flips_for_right_up() -> None:
    win = _place_win()
    camera.CameraWindow._layout_landscape_right_center(
        win, is_right_up=True, neutral=False, right=True, third=200, user_vertical=240,
    )
    win._shutter.set_valign.assert_called_once_with(Gtk.Align.END)
    win._options_bar.set_valign.assert_called_once_with(Gtk.Align.START)


# ---------------------------------------------------------------------------
# Recording dot
# ---------------------------------------------------------------------------

def _dot_win(orientation, pic_w=1000, pic_h=800, rect=(0, 0, 0, 0)):
    picture = MagicMock()
    picture.get_width.return_value = pic_w
    picture.get_height.return_value = pic_h
    return SimpleNamespace(
        _record_dot=MagicMock(), _picture=picture,
        _device_orientation=orientation, _rect=rect,
    )


def _position(win, monkeypatch):
    monkeypatch.setattr(camera, "_compute_image_rect", lambda pic, w, h: win._rect)
    camera.CameraWindow._position_record_dot(win)
    return win._record_dot


def test_record_dot_falls_back_to_the_widget_corner(monkeypatch) -> None:
    """Before the first frame the paintable has no intrinsic size."""
    win = _dot_win(ORIENT_NORMAL, rect=(0, 0, 0, 0))
    dot = _position(win, monkeypatch)
    dot.set_halign.assert_called_once_with(Gtk.Align.END)
    dot.set_valign.assert_called_once_with(Gtk.Align.START)
    dot.set_margin_end.assert_called_with(28)
    dot.set_margin_top.assert_called_with(28)


@pytest.mark.parametrize(
    ("orientation", "halign", "valign"),
    [
        (ORIENT_NORMAL, Gtk.Align.END, Gtk.Align.START),
        (ORIENT_BOTTOM_UP, Gtk.Align.START, Gtk.Align.END),
        (ORIENT_LEFT_UP, Gtk.Align.START, Gtk.Align.START),
        (ORIENT_RIGHT_UP, Gtk.Align.END, Gtk.Align.END),
    ],
)
def test_record_dot_tracks_the_users_top_right(monkeypatch, orientation, halign, valign) -> None:
    """The dot means "this image is recording", so it belongs in the user's
    top-right whichever way the phone is turned."""
    win = _dot_win(orientation, rect=(100, 50, 800, 700))
    dot = _position(win, monkeypatch)
    dot.set_halign.assert_called_once_with(halign)
    dot.set_valign.assert_called_once_with(valign)


def test_record_dot_sits_inside_the_letterboxed_image(monkeypatch) -> None:
    """With letterboxing the widget extends past the visible frame; a dot at
    the widget corner would float in the black bar."""
    inset = camera._RECORD_DOT_INSET
    # 1000x800 widget, 800x700 image at (100, 50) → 100px bars left/right,
    # 50px top, 50px bottom.
    win = _dot_win(ORIENT_NORMAL, pic_w=1000, pic_h=800, rect=(100, 50, 800, 700))
    dot = _position(win, monkeypatch)
    dot.set_margin_end.assert_called_with(100 + inset)
    dot.set_margin_top.assert_called_with(50 + inset)


def test_record_dot_resets_stale_margins(monkeypatch) -> None:
    win = _dot_win(ORIENT_NORMAL, rect=(100, 50, 800, 700))
    dot = _position(win, monkeypatch)
    for setter in (dot.set_margin_top, dot.set_margin_bottom,
                   dot.set_margin_start, dot.set_margin_end):
        assert setter.call_args_list[0][0][0] == 0, "margins were not reset first"
