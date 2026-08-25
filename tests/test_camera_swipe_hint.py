"""Where the "←  swipe  →" pill sits.

The photo <-> video swipe is a gesture on the SHUTTER, not on the preview, so
the hint has to be stacked against the shutter. Parked at the screen's bottom
centre (as it first was) it pointed at the wrong target: users swiped across
the image, where nothing listens.

These tests build real widgets on purpose. The placement leans on GTK's own
measurement of the pill — which moves with the theme font and, in landscape,
with the label's rotated measure — so asserting on margins alone would pin the
arithmetic without pinning the result. Instead each case lays the chrome out
through the real _apply_layout_for, resolves both widgets to rectangles the
way GTK's align + margin allocation does, and asks the question the user
actually cares about: is the pill next to the shutter, in the user's frame of
reference, and still on screen?
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.conftest import requires_display

camera = pytest.importorskip("muga.camera")

from gi.repository import Gtk  # noqa: E402

from muga.camera_orientation import (  # noqa: E402
    ORIENT_BOTTOM_UP,
    ORIENT_LEFT_UP,
    ORIENT_NORMAL,
    ORIENT_RIGHT_UP,
)
from muga.camera_widgets import RotatableLabel  # noqa: E402

pytestmark = requires_display

ALL = (ORIENT_NORMAL, ORIENT_BOTTOM_UP, ORIENT_LEFT_UP, ORIENT_RIGHT_UP)
HANDS = ("right", "left", "neutral")

# A phone-shaped surface. The compositor keeps the surface in portrait and
# rotates the buffer, so these dimensions hold for all four orientations.
W, H = 720, 1600


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _laid_out(handedness: str, orientation: str, width=W, height=H):
    """Run the real layout for one handedness/orientation and return
    (shutter_rect, hint_rect) in widget coordinates."""
    overlay = Gtk.Overlay()
    overlay.set_child(Gtk.Picture())
    shutter = Gtk.Box()
    shutter.add_css_class("shutter-button")
    shutter.set_size_request(camera._SHUTTER_SIZE, camera._SHUTTER_SIZE)
    overlay.add_overlay(shutter)
    hint = RotatableLabel()
    hint.set_label("←  swipe  →")
    hint.add_css_class("camera-swipe-hint")
    hint.set_can_target(False)
    hint.set_opacity(0.0)
    overlay.add_overlay(hint)

    win = SimpleNamespace(
        _applied_layout=None,
        _layout_is_landscape=False,
        _handedness=handedness,
        _rotatable_icons=[hint],
        _quality_button=MagicMock(),
        _settings_button=MagicMock(),
        _build_quality_popover=MagicMock(),
        _build_settings_popover=MagicMock(),
        _position_record_dot=MagicMock(),
        _shutter=shutter,
        _options_bar=Gtk.Box(),
        _swipe_hint=hint,
        _device_orientation=orientation,
        get_width=lambda: width,
        get_height=lambda: height,
    )
    win._flip_align = camera.CameraWindow._flip_align
    win._stack_along = camera.CameraWindow._stack_along
    for name in ("_apply_placement", "_apply_layout_for", "_hint_extent",
                 "_layout_portrait", "_layout_landscape",
                 "_layout_landscape_right_center", "_position_swipe_hint"):
        setattr(win, name, MethodType(getattr(camera.CameraWindow, name), win))

    win._apply_layout_for(orientation)
    # Measure after the layout — that is what applies the rotation, and a
    # rotated label measures transposed. GTK folds the widget's margins into
    # measure(), so take them back off to get the size the pill draws at.
    hint_w = (hint.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
              - hint.get_margin_start() - hint.get_margin_end())
    hint_h = (hint.measure(Gtk.Orientation.VERTICAL, -1)[1]
              - hint.get_margin_top() - hint.get_margin_bottom())
    side = camera._SHUTTER_SIZE
    return (win, _rect(shutter, side, side, width, height),
            _rect(hint, hint_w, hint_h, width, height))


def _span(align, m_start, m_end, extent, length):
    """The interval a widget occupies on one axis — GTK's align + margin
    allocation, in the one dimension."""
    if align == Gtk.Align.START:
        start = m_start
    elif align == Gtk.Align.END:
        start = length - m_end - extent
    else:
        start = m_start + (length - m_start - m_end - extent) / 2
    return start, start + extent


def _rect(widget, extent_w, extent_h, width, height):
    x0, x1 = _span(widget.get_halign(), widget.get_margin_start(),
                   widget.get_margin_end(), extent_w, width)
    y0, y1 = _span(widget.get_valign(), widget.get_margin_top(),
                   widget.get_margin_bottom(), extent_h, height)
    return x0, y0, x1, y1


def _user_axes(rect, orientation):
    """Re-express a widget rectangle as (down-axis span, across-axis span) in
    the user's frame, where "down" is whichever widget edge the user sees at
    the bottom for this orientation."""
    x0, y0, x1, y1 = rect
    down = camera._USER_DOWN_EDGE[orientation]
    if down == "bottom":
        return (y0, y1), (x0, x1)
    if down == "top":
        return (-y1, -y0), (-x1, -x0)
    if down == "end":
        return (x0, x1), (-y1, -y0)
    return (-x1, -x0), (y0, y1)


def _relation(handedness, orientation, **size):
    """(placement, gap, cross-centre offset, on-screen) for one case."""
    width = size.get("width", W)
    height = size.get("height", H)
    _win, shutter, hint = _laid_out(handedness, orientation, width, height)
    (sd0, sd1), (sa0, sa1) = _user_axes(shutter, orientation)
    (hd0, hd1), (ha0, ha1) = _user_axes(hint, orientation)
    if hd0 >= sd1:
        placement, gap = "below", hd0 - sd1
    elif hd1 <= sd0:
        placement, gap = "above", sd0 - hd1
    else:
        placement, gap = "overlap", 0.0
    onscreen = (hint[0] >= -0.5 and hint[1] >= -0.5
                and hint[2] <= width + 0.5 and hint[3] <= height + 0.5)
    return placement, gap, ((ha0 + ha1) / 2) - ((sa0 + sa1) / 2), onscreen


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("orientation", ALL)
@pytest.mark.parametrize("handedness", HANDS)
def test_swipe_hint_sits_below_the_shutter(handedness, orientation) -> None:
    """On a phone-sized surface there is room under the shutter in every
    orientation and for every handedness, so that is where the pill goes —
    right where the gesture it advertises has to happen."""
    placement, gap, _cross, onscreen = _relation(handedness, orientation)
    assert placement == "below"
    assert gap == pytest.approx(camera._SWIPE_HINT_GAP, abs=1)
    assert onscreen


@pytest.mark.parametrize("orientation", ALL)
@pytest.mark.parametrize("handedness", HANDS)
def test_swipe_hint_lines_up_with_the_shutter(handedness, orientation) -> None:
    """Across the axis it is centred on the shutter (not on the screen), so a
    corner-mounted shutter takes the pill with it. It only comes off centre
    where the screen edge stops it."""
    _placement, _gap, cross, _onscreen = _relation(handedness, orientation)
    assert abs(cross) <= camera._SWIPE_HINT_INSET


@pytest.mark.parametrize("orientation", (ORIENT_NORMAL, ORIENT_BOTTOM_UP))
@pytest.mark.parametrize("handedness", ("right", "left"))
def test_swipe_hint_flips_above_a_cramped_shutter(handedness, orientation) -> None:
    """Squeeze the surface until the shutter's own inset leaves no room
    underneath and the pill goes above it instead — anywhere but under the
    shutter or off the screen.

    Only the portrait lays can run out: they pin the shutter to the user's
    bottom edge, while the landscape ones put it half-way up the user's
    vertical axis with the whole half-screen to spare."""
    placement, gap, _cross, onscreen = _relation(
        handedness, orientation, width=480, height=240,
    )
    assert placement == "above"
    assert gap == pytest.approx(camera._SWIPE_HINT_GAP, abs=1)
    assert onscreen


def test_swipe_hint_placement_is_idempotent() -> None:
    """GTK folds margins into measure(), so a placement that measures the
    pill after having moved it once reads its own margins back as size and
    walks the pill further out on every reposition. It runs at least twice for
    real — once per layout, once more when the window maps."""
    win, _shutter, _hint = _laid_out("right", ORIENT_NORMAL)

    def margins():
        pill = win._swipe_hint
        return (pill.get_margin_top(), pill.get_margin_bottom(),
                pill.get_margin_start(), pill.get_margin_end())

    before = margins()
    win._position_swipe_hint()
    win._position_swipe_hint()
    assert margins() == before


def test_swipe_hint_follows_the_shutter_across_a_rotation() -> None:
    """Same window, both portrait lays: the pill ends up on opposite widget
    edges because the user's "down" did."""
    _win, _shutter, normal = _laid_out("right", ORIENT_NORMAL)
    _win2, _shutter2, flipped = _laid_out("right", ORIENT_BOTTOM_UP)
    assert normal[1] > H / 2      # widget-bottom half
    assert flipped[3] < H / 2     # widget-top half


# ---------------------------------------------------------------------------
# Staying measurable
# ---------------------------------------------------------------------------

def test_swipe_hint_is_measurable_while_hidden() -> None:
    """The pill is hidden by opacity rather than visibility because GTK
    measures an invisible widget as 0x0 — and its measured size is what the
    placement stacks against."""
    hint = RotatableLabel()
    hint.set_label("←  swipe  →")
    assert hint.get_opacity() == 1.0
    hint.set_opacity(0.0)
    assert hint.measure(Gtk.Orientation.HORIZONTAL, -1)[1] > 0

    hint.set_visible(False)
    assert hint.measure(Gtk.Orientation.HORIZONTAL, -1)[1] == 0


def test_swipe_hint_pulse_fades_out_without_hiding_the_widget() -> None:
    hint = MagicMock()
    win = SimpleNamespace(
        _swipe_hint=hint, _swipe_hint_phase=0.03, _swipe_hint_direction=-1,
        _swipe_hint_cycles_left=1, _swipe_hint_pulse_id=42,
    )
    assert camera.CameraWindow._swipe_hint_tick(win) is False
    assert win._swipe_hint_pulse_id is None
    hint.set_opacity.assert_called_once_with(0.0)
    hint.set_visible.assert_not_called()
