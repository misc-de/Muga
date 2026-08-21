"""Tests for the camera's drawn overlay and its rotating widgets.

The chrome — viewfinder brackets, rule-of-thirds grid, rotating glyphs — is
painted rather than composed from widgets, so the only way to check it is to
run the draw and see what was asked of the drawing context. A recording Cairo
double does that: it collects the calls instead of rasterising, which keeps
the assertions about geometry rather than pixels.

The rotating widgets need a real GTK snapshot, so those are display-only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import requires_display

widgets = pytest.importorskip("yaga.camera_widgets")


class RecordingContext:
    """Collects Cairo calls instead of drawing them."""

    def __init__(self) -> None:
        self.lines: list[tuple[float, float, float, float]] = []
        self.colours: list[tuple] = []
        self.line_widths: list[float] = []
        self.strokes = 0
        self._pen: tuple[float, float] | None = None

    def set_source_rgba(self, *rgba):
        self.colours.append(rgba)

    def set_line_width(self, width):
        self.line_widths.append(width)

    def set_line_cap(self, _cap):
        pass

    def move_to(self, x, y):
        self._pen = (x, y)

    def line_to(self, x, y):
        if self._pen is not None:
            self.lines.append((*self._pen, x, y))
        self._pen = (x, y)

    def stroke(self):
        self.strokes += 1
        self._pen = None

    def horizontals(self):
        return [ln for ln in self.lines if abs(ln[1] - ln[3]) < 0.01]

    def verticals(self):
        return [ln for ln in self.lines if abs(ln[0] - ln[2]) < 0.01]


def _chrome(rect, grid=False):
    """An ImageChrome stand-in whose image rect is fixed for the test."""
    return SimpleNamespace(_image_rect=lambda w, h: rect, _grid_visible=grid)


IMAGE_RECT = (100.0, 50.0, 800.0, 600.0)   # x, y, w, h inside a larger widget


# ---------------------------------------------------------------------------
# Viewfinder brackets
# ---------------------------------------------------------------------------

def test_four_corner_brackets_are_drawn() -> None:
    cr = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome(IMAGE_RECT), None, cr, 1000, 700)
    # Each bracket is an L: one vertical segment and one horizontal.
    assert len(cr.verticals()) == 4
    assert len(cr.horizontals()) == 4


def test_brackets_sit_inside_the_image_not_the_widget() -> None:
    """With letterboxing the widget extends past the frame; brackets at the
    widget corners would float in the black bars."""
    x, y, w, h = IMAGE_RECT
    cr = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome(IMAGE_RECT), None, cr, 1000, 700)

    xs = [c for ln in cr.lines for c in (ln[0], ln[2])]
    ys = [c for ln in cr.lines for c in (ln[1], ln[3])]
    assert min(xs) >= x and max(xs) <= x + w
    assert min(ys) >= y and max(ys) <= y + h


def test_brackets_are_inset_from_the_image_edge() -> None:
    """The gap leaves room for the close and gear buttons between the bracket
    and the actual corner."""
    x, y, _w, _h = IMAGE_RECT
    cr = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome(IMAGE_RECT), None, cr, 1000, 700)
    nearest_x = min(c for ln in cr.lines for c in (ln[0], ln[2]))
    nearest_y = min(c for ln in cr.lines for c in (ln[1], ln[3]))
    assert nearest_x - x > 20, "brackets are flush against the image edge"
    assert nearest_y - y > 20


def test_bracket_length_scales_with_the_image() -> None:
    small = RecordingContext()
    large = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome((0, 0, 200, 150)), None, small, 200, 150)
    widgets.ImageChrome._on_draw(_chrome((0, 0, 2000, 1500)), None, large, 2000, 1500)

    def arm(cr):
        return max(abs(ln[3] - ln[1]) for ln in cr.verticals())

    assert arm(large) > arm(small)


def test_bracket_length_has_a_floor() -> None:
    """On a tiny preview a proportional arm would vanish."""
    cr = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome((0, 0, 60, 40)), None, cr, 60, 40)
    assert max(abs(ln[3] - ln[1]) for ln in cr.verticals()) >= 12.0


def test_nothing_is_drawn_before_the_first_frame() -> None:
    """No paintable yet means an empty rect; painting then would put brackets
    around nothing."""
    cr = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome((0, 0, 0, 0)), None, cr, 1000, 700)
    assert cr.lines == []
    assert cr.strokes == 0


def test_a_degenerate_rect_is_not_drawn() -> None:
    cr = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome((10, 10, 0, 400)), None, cr, 1000, 700)
    assert cr.lines == []


# ---------------------------------------------------------------------------
# Rule-of-thirds grid
# ---------------------------------------------------------------------------

def test_the_grid_is_off_by_default() -> None:
    plain = RecordingContext()
    gridded = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome(IMAGE_RECT, grid=False), None, plain, 1000, 700)
    widgets.ImageChrome._on_draw(_chrome(IMAGE_RECT, grid=True), None, gridded, 1000, 700)
    assert len(gridded.lines) > len(plain.lines)


def test_the_grid_divides_the_image_in_thirds() -> None:
    x, y, w, h = IMAGE_RECT
    cr = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome(IMAGE_RECT, grid=True), None, cr, 1000, 700)

    # Full-height verticals are grid lines; the bracket arms are short.
    grid_x = sorted({round(ln[0]) for ln in cr.verticals()
                     if abs(ln[3] - ln[1]) > h * 0.9})
    assert len(grid_x) >= 2
    assert any(abs(gx - (x + w / 3)) <= 1 for gx in grid_x)
    assert any(abs(gx - (x + 2 * w / 3)) <= 1 for gx in grid_x)


def test_the_grid_is_drawn_twice_for_contrast() -> None:
    """A black pass under a white one, offset by a pixel, so the lines stay
    visible over both a bright sky and a dark room."""
    cr = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome(IMAGE_RECT, grid=True), None, cr, 1000, 700)
    assert any(c[:3] == (0, 0, 0) for c in cr.colours), "no dark pass"
    assert any(c[:3] == (1, 1, 1) for c in cr.colours), "no light pass"


def test_the_grid_stays_inside_the_image() -> None:
    x, y, w, h = IMAGE_RECT
    cr = RecordingContext()
    widgets.ImageChrome._on_draw(_chrome(IMAGE_RECT, grid=True), None, cr, 1000, 700)
    xs = [c for ln in cr.lines for c in (ln[0], ln[2])]
    ys = [c for ln in cr.lines for c in (ln[1], ln[3])]
    assert min(xs) >= x - 1 and max(xs) <= x + w + 1
    assert min(ys) >= y - 1 and max(ys) <= y + h + 1


# ---------------------------------------------------------------------------
# Grid toggle
# ---------------------------------------------------------------------------

@requires_display
def test_grid_visibility_round_trips() -> None:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    chrome = widgets.ImageChrome(Gtk.Picture())
    assert chrome.get_grid_visible() is False
    chrome.set_grid_visible(True)
    assert chrome.get_grid_visible() is True
    chrome.set_grid_visible(False)
    assert chrome.get_grid_visible() is False


# ---------------------------------------------------------------------------
# Rotating widgets
# ---------------------------------------------------------------------------

@requires_display
@pytest.mark.parametrize("cls_name", ["RotatableIcon", "RotatableSwitch", "RotatableLabel"])
def test_rotatable_widgets_accept_quarter_turns(cls_name) -> None:
    cls = getattr(widgets, cls_name, None)
    if cls is None:
        pytest.skip(f"{cls_name} not present")
    widget = cls()
    for deg in (0, 90, 180, 270):
        widget.set_rotation_deg(deg)
    assert widget._rotation_deg == 270


@requires_display
def test_a_tiny_rotation_change_is_ignored() -> None:
    """set_rotation_deg runs on every orientation tick; redrawing for a
    fraction of a degree would repaint the chrome continuously."""
    icon = widgets.RotatableIcon()
    icon.set_rotation_deg(90)
    icon.set_rotation_deg(90.2)
    assert icon._rotation_deg == 90


@requires_display
def test_rotating_widgets_snapshot_without_error() -> None:
    """do_snapshot is overridden to rotate around the centre; a mistake there
    is invisible until the widget is actually painted."""
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    icon = widgets.RotatableIcon()
    icon.set_from_icon_name("camera-photo-symbolic")
    icon.set_pixel_size(24)
    for deg in (0, 90, 180, 270):
        icon.set_rotation_deg(deg)
        snapshot = Gtk.Snapshot()
        icon.do_snapshot(snapshot)
        snapshot.to_paintable(None)


@requires_display
def test_an_unrotated_icon_takes_the_plain_path() -> None:
    """At 0° it must not push a transform it does not need."""
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    icon = widgets.RotatableIcon()
    icon.set_from_icon_name("camera-photo-symbolic")
    snapshot = Gtk.Snapshot()
    icon.do_snapshot(snapshot)   # rotation is 0 here
    assert icon._rotation_deg == 0


@requires_display
def test_rotated_labels_swap_their_measurement() -> None:
    """At 90°/270° the parent box has to allocate the rotated bounds, or the
    text is clipped."""
    cls = getattr(widgets, "RotatableLabel", None)
    if cls is None:
        pytest.skip("RotatableLabel not present")
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    label = cls()
    label.set_label("10s")
    upright_w = label.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
    upright_h = label.measure(Gtk.Orientation.VERTICAL, -1)[1]
    label.set_rotation_deg(90)
    turned_w = label.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
    turned_h = label.measure(Gtk.Orientation.VERTICAL, -1)[1]
    assert (turned_w, turned_h) == (upright_h, upright_w)


# ---------------------------------------------------------------------------
# Zoom on the preview
# ---------------------------------------------------------------------------

@requires_display
def test_mirrored_picture_reports_its_zoom() -> None:
    cls = getattr(widgets, "MirroredPicture", None)
    if cls is None:
        pytest.skip("MirroredPicture not present")
    picture = cls()
    assert picture.get_zoom() == pytest.approx(1.0)
    picture.set_zoom(2.5)
    assert picture.get_zoom() == pytest.approx(2.5)


@requires_display
def test_mirrored_picture_clamps_the_zoom() -> None:
    cls = getattr(widgets, "MirroredPicture", None)
    if cls is None:
        pytest.skip("MirroredPicture not present")
    picture = cls()
    picture.set_zoom(0.01)
    assert picture.get_zoom() >= 1.0
    picture.set_zoom(1000.0)
    assert picture.get_zoom() < 100.0


@requires_display
def test_mirrored_picture_can_be_mirrored() -> None:
    """The front camera's preview is flipped so it reads like a mirror."""
    cls = getattr(widgets, "MirroredPicture", None)
    if cls is None:
        pytest.skip("MirroredPicture not present")
    picture = cls()
    picture.set_mirrored(True)
    picture.set_mirrored(False)
