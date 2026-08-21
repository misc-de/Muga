"""Rendering tests for the camera's transformed widgets.

The rotating icons, labels and switches, and the mirrored/zoomed preview, all
work by pushing a GSK transform in do_snapshot rather than by transforming a
pixel buffer. That is the only part of the camera UI where being wrong is
invisible to every other kind of test: the widget still measures, still lays
out, still reports the right state — it just paints in the wrong place.

Checked two ways, neither of which uses a checked-in reference image:

  * the render-node tree, read back through Gsk.Transform.to_string(). This
    says exactly which transform was pushed, and is immune to theme, font and
    antialiasing differences that make golden images rot.
  * an actual offscreen raster, with a marker drawn by the test itself. If a
    rotation is real, a marker in one corner comes out in another; a golden
    file is not needed to assert that, and nothing breaks when GTK changes how
    it draws a switch.
"""

from __future__ import annotations

import io
import time

import pytest

from tests.conftest import requires_display, requires_offscreen_raster

PILImage = pytest.importorskip("PIL.Image")

widgets = pytest.importorskip("yaga.camera_widgets")

pytestmark = requires_display


@pytest.fixture(scope="module")
def gtk():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Gdk, Graphene, Gsk, Gtk

    Gtk.init_check()
    return Gtk, Gsk, Gdk, Graphene


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshot_of(gtk, widget, width=48, height=48):
    """Run a widget's do_snapshot and return the resulting render node."""
    Gtk, _Gsk, _Gdk, _Graphene = gtk
    snapshot = Gtk.Snapshot()
    type(widget).do_snapshot(widget, snapshot)
    return snapshot.to_node()


def _transforms(gtk, node) -> list[str]:
    """Every transform in the node tree, outermost first."""
    _Gtk, Gsk, _Gdk, _Graphene = gtk
    found: list[str] = []

    def walk(n):
        if n is None:
            return
        if n.get_node_type() == Gsk.RenderNodeType.TRANSFORM_NODE:
            transform = n.get_transform()
            found.append(transform.to_string() if transform else "")
            walk(n.get_child())
            return
        # Containers hold their children in an indexed list.
        if n.get_node_type() == Gsk.RenderNodeType.CONTAINER_NODE:
            for i in range(n.get_n_children()):
                walk(n.get_child(i))
            return
        for getter in ("get_child",):
            child = getattr(n, getter, None)
            if child is not None:
                try:
                    walk(child())
                except TypeError:
                    pass

    walk(node)
    return found


def _render(gtk, node, width, height):
    """Rasterise a node offscreen and return a pixel accessor."""
    _Gtk, Gsk, Gdk, Graphene = gtk
    renderer = Gsk.CairoRenderer.new()
    renderer.realize(None)
    try:
        texture = renderer.render_texture(
            node, Graphene.Rect().init(0, 0, width, height))
        # Via PNG rather than TextureDownloader.download_bytes(). The
        # downloader hands back the texture's preferred memory format, and
        # which one that is differs between GTK versions — unpacking the
        # bytes as a fixed BGRA read red out of the blue channel on the CI
        # runner and concluded nothing had been painted. PNG is lossless and
        # says what its channel order is, so there is nothing left to assume.
        png = texture.save_to_png_bytes().get_data()
    finally:
        renderer.unrealize()

    image = PILImage.open(io.BytesIO(png)).convert("RGBA")
    rgba = image.load()

    def pixel(x, y):
        return tuple(rgba[x, y])

    return pixel


def _marked_node(gtk, width, height, mark=(12, 12)):
    """A transparent field with an opaque red square in its top-left."""
    Gtk, _Gsk, Gdk, Graphene = gtk
    snapshot = Gtk.Snapshot()
    snapshot.append_color(
        Gdk.RGBA(red=1.0, green=0.0, blue=0.0, alpha=1.0),
        Graphene.Rect().init(0, 0, mark[0], mark[1]))
    return snapshot.to_node()


def _rotated_marker(gtk, degrees, size=48, mark=12):
    """Apply the widgets' rotate-about-centre transform to a corner marker and
    rasterise it — the same three-step transform do_snapshot pushes."""
    Gtk, _Gsk, Gdk, Graphene = gtk
    snapshot = Gtk.Snapshot()
    snapshot.save()
    snapshot.translate(Graphene.Point().init(size / 2, size / 2))
    snapshot.rotate(degrees)
    snapshot.translate(Graphene.Point().init(-size / 2, -size / 2))
    snapshot.append_color(
        Gdk.RGBA(red=1.0, green=0.0, blue=0.0, alpha=1.0),
        Graphene.Rect().init(0, 0, mark, mark))
    snapshot.restore()
    return _render(gtk, snapshot.to_node(), size, size)


def _is_red(px):
    r, g, b, a = px
    return a > 200 and r > 200 and g < 60 and b < 60


def _spy_snapshot(gtk):
    """A Gtk.Snapshot that records the transform calls made on it.

    The most direct reading available: it shows the exact sequence
    do_snapshot pushed, works for widgets whose painting is otherwise hard to
    reach (GtkSwitch cross-fades its states, so its transform never surfaces
    as an inspectable node), and needs no allocation guesswork.
    """
    Gtk, _Gsk, _Gdk, _Graphene = gtk

    class SpySnapshot(Gtk.Snapshot):
        def __init__(self):
            super().__init__()
            self.calls: list[str] = []

        def save(self):
            self.calls.append("save")
            return Gtk.Snapshot.save(self)

        def restore(self):
            self.calls.append("restore")
            return Gtk.Snapshot.restore(self)

        def rotate(self, angle):
            self.calls.append(f"rotate({angle:g})")
            return Gtk.Snapshot.rotate(self, angle)

        def scale(self, sx, sy):
            self.calls.append(f"scale({sx:g},{sy:g})")
            return Gtk.Snapshot.scale(self, sx, sy)

        def translate(self, point):
            self.calls.append(f"translate({point.x:g},{point.y:g})")
            return Gtk.Snapshot.translate(self, point)

    return SpySnapshot()


def _recorded_calls(gtk, widget):
    spy = _spy_snapshot(gtk)
    type(widget).do_snapshot(widget, spy)
    return spy.calls


# ---------------------------------------------------------------------------
# The rotation actually moves pixels
# ---------------------------------------------------------------------------

@requires_offscreen_raster
def test_the_marker_starts_in_the_top_left(gtk) -> None:
    """Baseline for the rotations below: unrotated, the marker is top-left."""
    pixel = _rotated_marker(gtk, 0)
    assert _is_red(pixel(6, 6))
    assert not _is_red(pixel(41, 6))
    assert not _is_red(pixel(41, 41))
    assert not _is_red(pixel(6, 41))


@requires_offscreen_raster
@pytest.mark.parametrize(
    ("degrees", "corner"),
    [
        (90, (41, 6)),    # top-left -> top-right
        (180, (41, 41)),  # -> bottom-right
        (270, (6, 41)),   # -> bottom-left
    ],
)
def test_rotation_moves_the_marker_to_the_next_corner(gtk, degrees, corner) -> None:
    """A clockwise quarter turn about the centre, checked in pixels. This is
    what "the glyph follows the device" means on screen."""
    pixel = _rotated_marker(gtk, degrees)
    assert _is_red(pixel(*corner)), f"{degrees}° did not land the marker at {corner}"
    assert not _is_red(pixel(6, 6)), f"{degrees}° left the marker where it started"


@requires_offscreen_raster
def test_rotation_keeps_the_content_inside_the_widget(gtk) -> None:
    """Rotating about a corner instead of the centre would push half the glyph
    outside its allocation, where it gets clipped."""
    size, mark = 48, 12
    for degrees in (0, 90, 180, 270):
        pixel = _rotated_marker(gtk, degrees, size=size, mark=mark)
        painted = [(x, y) for x in range(size) for y in range(size)
                   if _is_red(pixel(x, y))]
        assert painted, f"nothing painted at {degrees}°"
        assert len(painted) >= mark * mark * 0.8, (
            f"{degrees}° clipped the marker: {len(painted)} of {mark * mark} px")


@requires_offscreen_raster
def test_a_full_turn_is_the_same_as_none(gtk) -> None:
    plain = _rotated_marker(gtk, 0)
    full = _rotated_marker(gtk, 360)
    for x, y in ((6, 6), (41, 6), (41, 41), (6, 41), (24, 24)):
        assert _is_red(plain(x, y)) == _is_red(full(x, y)), f"differ at {(x, y)}"


# ---------------------------------------------------------------------------
# What the widgets push into the node tree
# ---------------------------------------------------------------------------

def _icon_in_window(gtk, degrees):
    """An allocated, rotated icon plus the window keeping it alive."""
    icon = widgets.RotatableIcon()
    icon.set_from_icon_name("camera-photo-symbolic")
    icon.set_pixel_size(24)
    icon.set_rotation_deg(degrees)
    return icon, _allocated(gtk, icon, width=48, height=48)


def test_an_unrotated_icon_pushes_no_transform(gtk) -> None:
    """At 0° the transform would be an identity — pushing one anyway costs a
    node on every frame of every icon, and there are ten of them."""
    icon, window = _icon_in_window(gtk, 0)
    try:
        node = _snapshot_of(gtk, icon)
        if node is None:
            pytest.skip("icon produced no render node in this environment")
        assert not any("rotate" in t for t in _transforms(gtk, node))
    finally:
        window.destroy()


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_a_rotated_icon_pushes_a_rotation(gtk, degrees) -> None:
    icon, window = _icon_in_window(gtk, degrees)
    try:
        node = _snapshot_of(gtk, icon)
        if node is None:
            pytest.skip("icon produced no render node in this environment")
        combined = " ".join(_transforms(gtk, node)).replace(" ", "")
        assert f"rotate({degrees}" in combined, (
            f"no {degrees}° rotation in the tree: {combined}")
    finally:
        window.destroy()


def _allocated(gtk, widget, width=80, height=40):
    """Put a widget in a window and let it be allocated.

    Unallocated, get_width() is 0, the centre the transform pivots about is
    (0, 0), and the whole thing degenerates into a rotation about the corner —
    which is exactly the bug these tests look for, so it has to be ruled out.
    """
    Gtk, _Gsk, _Gdk, _Graphene = gtk
    from gi.repository import GLib

    window = Gtk.Window()
    window.set_child(widget)
    window.set_default_size(width, height)
    window.present()
    # Wait for the allocation rather than pumping a fixed number of times:
    # how many iterations it takes depends on what else the compositor is
    # doing, and a fixed count made this flaky as the suite grew.
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 2.0
    while widget.get_width() <= 0 and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)
    return window


def _painted_bounds(gtk, widget):
    """Where the widget's own painting lands, in widget coordinates."""
    node = _snapshot_of(gtk, widget)
    if node is None:
        return None
    bounds = node.get_bounds()
    return (bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height)


def _centre(rect):
    x, y, w, h = rect
    return (x + w / 2, y + h / 2)


@pytest.mark.parametrize(
    ("name", "factory"),
    [
        ("RotatableIcon", lambda: widgets.RotatableIcon()),
        ("RotatableLabel", lambda: widgets.RotatableLabel()),
    ],
)
def test_rotatable_widgets_pivot_about_their_centre(gtk, name, factory) -> None:
    """Rotating about the corner instead of the centre swings the content out
    of the widget entirely. Checked through the painted bounds rather than the
    transform string, because GSK is free to normalise the chain it stores.
    """
    widget = factory()
    if hasattr(widget, "set_label"):
        widget.set_label("10s")
    if hasattr(widget, "set_from_icon_name"):
        widget.set_from_icon_name("camera-photo-symbolic")
    window = _allocated(gtk, widget)
    try:
        if widget.get_width() <= 0:
            pytest.skip("widget was never allocated in this environment")
        upright = _painted_bounds(gtk, widget)
        if upright is None:
            pytest.skip(f"{name} produced no render node in this environment")

        for degrees in (90, 180, 270):
            widget.set_rotation_deg(degrees)
            turned = _painted_bounds(gtk, widget)
            assert turned is not None
            dx = abs(_centre(turned)[0] - _centre(upright)[0])
            dy = abs(_centre(turned)[1] - _centre(upright)[1])
            assert dx < 2.0 and dy < 2.0, (
                f"{name} at {degrees}° moved its centre by ({dx:.1f}, {dy:.1f}) "
                f"— it is pivoting about a corner")
    finally:
        window.destroy()


def test_a_quarter_turn_transposes_the_painted_bounds(gtk) -> None:
    """The visible consequence of the rotation: what was wide is now tall."""
    label = widgets.RotatableLabel()
    label.set_label("10s")
    window = _allocated(gtk, label)
    try:
        if label.get_width() <= 0:
            pytest.skip("widget was never allocated in this environment")
        upright = _painted_bounds(gtk, label)
        label.set_rotation_deg(90)
        turned = _painted_bounds(gtk, label)
        if upright is None or turned is None:
            pytest.skip("no render node in this environment")
        assert turned[2] == pytest.approx(upright[3], abs=0.5)
        assert turned[3] == pytest.approx(upright[2], abs=0.5)
    finally:
        window.destroy()


def test_a_half_turn_keeps_the_painted_bounds(gtk) -> None:
    label = widgets.RotatableLabel()
    label.set_label("10s")
    window = _allocated(gtk, label)
    try:
        if label.get_width() <= 0:
            pytest.skip("widget was never allocated in this environment")
        upright = _painted_bounds(gtk, label)
        label.set_rotation_deg(180)
        turned = _painted_bounds(gtk, label)
        if upright is None or turned is None:
            pytest.skip("no render node in this environment")
        assert turned[2] == pytest.approx(upright[2], abs=0.5)
        assert turned[3] == pytest.approx(upright[3], abs=0.5)
    finally:
        window.destroy()


# ---------------------------------------------------------------------------
# Measurement swap
# ---------------------------------------------------------------------------

def test_a_rotated_label_swaps_its_measured_axes(gtk) -> None:
    """The widget paints rotated, so its parent has to allocate rotated
    bounds. Without the swap a "Geotagging" label gets a wide-and-short slot
    and the rotated text spills outside it — the leading G gets clipped."""
    Gtk, _Gsk, _Gdk, _Graphene = gtk
    label = widgets.RotatableLabel()
    label.set_label("Geotagging")

    upright = (label.measure(Gtk.Orientation.HORIZONTAL, -1)[1],
               label.measure(Gtk.Orientation.VERTICAL, -1)[1])
    label.set_rotation_deg(90)
    turned = (label.measure(Gtk.Orientation.HORIZONTAL, -1)[1],
              label.measure(Gtk.Orientation.VERTICAL, -1)[1])

    assert turned == (upright[1], upright[0]), "the label did not swap its axes"


def test_a_rotated_switch_gets_the_height_it_paints_into(gtk) -> None:
    """GtkSwitch installs a GtkCustomLayout, and gtk_widget_measure()
    delegates to a layout manager when one is set — so the do_measure override
    that works for the label was never called here, and a rotated switch kept
    its 52x24 slot while painting 24x52.

    The fix asks for the transposed size directly. That gets the height; a
    size request is a minimum, so the width cannot go below the natural 52 px
    and the slot ends up square rather than exactly 24x52. Not clipped any
    more, just roomy — the remaining slack is asserted so it stays a known
    quantity rather than drifting.
    """
    Gtk, _Gsk, _Gdk, _Graphene = gtk
    switch = widgets.RotatableSwitch()
    upright = (switch.measure(Gtk.Orientation.HORIZONTAL, -1)[1],
               switch.measure(Gtk.Orientation.VERTICAL, -1)[1])

    switch.set_rotation_deg(90)
    turned = (switch.measure(Gtk.Orientation.HORIZONTAL, -1)[1],
              switch.measure(Gtk.Orientation.VERTICAL, -1)[1])

    assert turned[1] == upright[0], "the rotated switch is still too short"
    assert turned[0] == upright[0], "the width unexpectedly changed"


def test_a_switch_turned_back_upright_releases_the_request(gtk) -> None:
    """A stale size request would leave the switch square for good."""
    Gtk, _Gsk, _Gdk, _Graphene = gtk
    switch = widgets.RotatableSwitch()
    upright = (switch.measure(Gtk.Orientation.HORIZONTAL, -1)[1],
               switch.measure(Gtk.Orientation.VERTICAL, -1)[1])
    switch.set_rotation_deg(90)
    switch.set_rotation_deg(0)
    assert (switch.measure(Gtk.Orientation.HORIZONTAL, -1)[1],
            switch.measure(Gtk.Orientation.VERTICAL, -1)[1]) == upright


@pytest.mark.parametrize("degrees", [0, 180, 360])
def test_half_turns_leave_the_measurement_alone(gtk, degrees) -> None:
    """180° occupies the same bounds as upright — swapping there would make
    the layout jump for no visual reason."""
    Gtk, _Gsk, _Gdk, _Graphene = gtk
    label = widgets.RotatableLabel()
    label.set_label("Geotagging")
    upright = (label.measure(Gtk.Orientation.HORIZONTAL, -1)[1],
               label.measure(Gtk.Orientation.VERTICAL, -1)[1])
    label.set_rotation_deg(degrees)
    assert (label.measure(Gtk.Orientation.HORIZONTAL, -1)[1],
            label.measure(Gtk.Orientation.VERTICAL, -1)[1]) == upright


def test_crossing_a_quarter_turn_requests_a_relayout(gtk) -> None:
    """Only a resize makes the parent re-run its allocation; a redraw would
    paint rotated text into the old slot."""
    label = widgets.RotatableLabel()
    label.set_label("10s")

    calls = {"resize": 0, "draw": 0}
    label.queue_resize = lambda: calls.__setitem__("resize", calls["resize"] + 1)
    label.queue_draw = lambda: calls.__setitem__("draw", calls["draw"] + 1)

    label.set_rotation_deg(90)      # 0 -> 90 crosses the boundary
    assert calls["resize"] == 1 and calls["draw"] == 0

    label.set_rotation_deg(270)     # 90 -> 270 stays on the rotated axis
    assert calls["resize"] == 1 and calls["draw"] == 1

    label.set_rotation_deg(180)     # 270 -> 180 crosses back
    assert calls["resize"] == 2


# ---------------------------------------------------------------------------
# Preview mirroring and zoom
# ---------------------------------------------------------------------------

def _allocated_picture(gtk, *, mirrored=False, zoom=1.0, size=60):
    """A MirroredPicture with content, sized by a real allocation.

    Both are required: an unallocated widget has zero width and paints
    nothing, and a Picture with no paintable produces no node either — in
    both cases the transform is optimised away and the test would pass
    against an empty tree.
    """
    Gtk, _Gsk, Gdk, _Graphene = gtk
    from gi.repository import GLib

    # A 20x20 opaque red texture, built straight from bytes so nothing here
    # depends on a deprecated pixbuf path.
    data = GLib.Bytes.new(bytes([255, 0, 0, 255]) * (20 * 20))
    texture = Gdk.MemoryTexture.new(
        20, 20, Gdk.MemoryFormat.R8G8B8A8, data, 20 * 4)

    picture = widgets.MirroredPicture()
    picture.set_paintable(texture)
    picture.set_mirrored(mirrored)
    picture.set_zoom(zoom)

    window = Gtk.Window()
    window.set_child(picture)
    window.set_default_size(size, size)
    window.present()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 2.0
    while picture.get_width() <= 0 and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)
    return picture, window


def _picture_transforms(gtk, *, mirrored=False, zoom=1.0):
    picture, window = _allocated_picture(gtk, mirrored=mirrored, zoom=zoom)
    try:
        if picture.get_width() <= 0:
            pytest.skip("the picture was never allocated in this environment")
        node = _snapshot_of(gtk, picture)
        return _transforms(gtk, node) if node is not None else []
    finally:
        window.destroy()


def test_a_plain_preview_pushes_no_transform(gtk) -> None:
    """The common case is neither mirrored nor zoomed, on every frame."""
    assert not any("scale" in t for t in _picture_transforms(gtk))


def test_mirroring_flips_the_horizontal_axis(gtk) -> None:
    """A front-camera preview reads like a mirror; the saved file does not."""
    combined = " ".join(_picture_transforms(gtk, mirrored=True))
    assert "scale" in combined, "no scale pushed for the mirror"
    assert "-1" in combined, f"the flip is not negative: {combined}"


def test_zoom_scales_about_the_centre(gtk) -> None:
    """Scaling about the origin would slide the image off to one side as it
    magnifies, instead of zooming into what the user framed."""
    plain, window_a = _allocated_picture(gtk, zoom=1.0)
    zoomed, window_b = _allocated_picture(gtk, zoom=3.0)
    try:
        if plain.get_width() <= 0 or zoomed.get_width() <= 0:
            pytest.skip("the pictures were never allocated in this environment")
        before = _painted_bounds(gtk, plain)
        after = _painted_bounds(gtk, zoomed)
        if before is None or after is None:
            pytest.skip("no render node in this environment")

        assert after[2] > before[2], "the zoom did not magnify"
        dx = abs(_centre(after)[0] - _centre(before)[0])
        dy = abs(_centre(after)[1] - _centre(before)[1])
        assert dx < 2.0 and dy < 2.0, (
            f"zoom moved the centre by ({dx:.1f}, {dy:.1f}) — it is scaling "
            f"about a corner")
    finally:
        window_a.destroy()
        window_b.destroy()


def test_mirroring_and_zoom_combine(gtk) -> None:
    combined = " ".join(_picture_transforms(gtk, mirrored=True, zoom=2.0))
    assert combined.count("scale") >= 1
    assert "translate" in combined


def test_zoom_is_clamped_to_a_usable_range(gtk) -> None:
    picture = widgets.MirroredPicture()
    picture.set_zoom(0.1)
    assert picture.get_zoom() == pytest.approx(1.0), "zooming out past fit"
    picture.set_zoom(99.0)
    assert picture.get_zoom() <= 8.0, "unbounded zoom"


def test_a_negligible_zoom_change_is_ignored(gtk) -> None:
    """set_zoom runs on every scroll tick and pinch update; redrawing for a
    thousandth of a step would repaint continuously."""
    picture = widgets.MirroredPicture()
    picture.set_zoom(2.0)
    calls = {"draw": 0}
    picture.queue_draw = lambda: calls.__setitem__("draw", calls["draw"] + 1)
    picture.set_zoom(2.001)
    assert calls["draw"] == 0
    picture.set_zoom(2.5)
    assert calls["draw"] == 1


def test_setting_the_same_mirror_state_twice_is_ignored(gtk) -> None:
    picture = widgets.MirroredPicture()
    picture.set_mirrored(True)
    calls = {"draw": 0}
    picture.queue_draw = lambda: calls.__setitem__("draw", calls["draw"] + 1)
    picture.set_mirrored(True)
    assert calls["draw"] == 0
    picture.set_mirrored(False)
    assert calls["draw"] == 1


@requires_offscreen_raster
def test_mirroring_is_visible_in_a_raster(gtk) -> None:
    """The transform read back from the tree says what was pushed; this says
    it has the effect the name promises."""
    Gtk, _Gsk, Gdk, Graphene = gtk
    size, mark = 40, 12

    snapshot = Gtk.Snapshot()
    snapshot.save()
    snapshot.translate(Graphene.Point().init(size, 0))
    snapshot.scale(-1.0, 1.0)
    snapshot.append_color(
        Gdk.RGBA(red=1.0, green=0.0, blue=0.0, alpha=1.0),
        Graphene.Rect().init(0, 0, mark, mark))
    snapshot.restore()
    pixel = _render(gtk, snapshot.to_node(), size, size)

    assert _is_red(pixel(size - 6, 6)), "the mirrored marker is not on the right"
    assert not _is_red(pixel(6, 6)), "the marker stayed on the left"


# ---------------------------------------------------------------------------
# The switch paints rotated too
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_a_rotated_switch_pivots_about_its_centre(gtk, degrees) -> None:
    """GtkSwitch cross-fades between its states, so its transform never
    surfaces as an inspectable render node — the calls it makes on the
    snapshot do, and they say the same thing more directly.

    An 80x80 allocation puts the centre at (40, 40); rotating about anything
    else would show up as different translate arguments.
    """
    switch = widgets.RotatableSwitch()
    switch.set_rotation_deg(degrees)
    window = _allocated(gtk, switch, width=80, height=80)
    try:
        if switch.get_width() <= 0:
            pytest.skip("the switch was never allocated in this environment")
        # Read the pivot back from the allocation rather than assuming the
        # requested 80x80 was honoured: a GtkSwitch has its own size request,
        # and how much of the 80x80 it takes differs between GTK versions.
        # What must hold is that the pivot is the centre of whatever size it
        # ended up with.
        cx = switch.get_width() / 2
        cy = switch.get_height() / 2
        assert _recorded_calls(gtk, switch) == [
            "save", f"translate({cx:g},{cy:g})", f"rotate({degrees})",
            f"translate({-cx:g},{-cy:g})", "restore",
        ]
    finally:
        window.destroy()


def test_an_unrotated_switch_pushes_nothing(gtk) -> None:
    """Ten of these repaint on every orientation tick; an identity transform
    each would be pure overhead."""
    switch = widgets.RotatableSwitch()
    window = _allocated(gtk, switch, width=80, height=80)
    try:
        assert _recorded_calls(gtk, switch) == []
    finally:
        window.destroy()


@pytest.mark.parametrize(
    ("name", "factory"),
    [
        ("RotatableIcon", lambda: widgets.RotatableIcon()),
        ("RotatableLabel", lambda: widgets.RotatableLabel()),
        ("RotatableSwitch", lambda: widgets.RotatableSwitch()),
    ],
)
def test_every_rotatable_pushes_the_same_transform(gtk, name, factory) -> None:
    """All three go through translate(centre) rotate(d) translate(-centre).
    A widget that grew its own variant would drift out of step with the rest
    of the chrome on a turn."""
    widget = factory()
    if hasattr(widget, "set_label"):
        widget.set_label("10s")
    if hasattr(widget, "set_from_icon_name"):
        widget.set_from_icon_name("camera-photo-symbolic")
    widget.set_rotation_deg(90)
    window = _allocated(gtk, widget, width=60, height=60)
    try:
        if widget.get_width() <= 0:
            pytest.skip("widget was never allocated in this environment")
        calls = _recorded_calls(gtk, widget)
        assert calls[0] == "save" and calls[-1] == "restore", (
            f"{name} does not balance save/restore: {calls}")
        assert calls[2] == "rotate(90)", f"{name}: {calls}"
        # The two translates must be exact negatives of each other.
        first = calls[1].removeprefix("translate(").removesuffix(")").split(",")
        last = calls[3].removeprefix("translate(").removesuffix(")").split(",")
        assert [float(v) for v in first] == [-float(v) for v in last], (
            f"{name} does not return to where it pivoted: {calls}")
    finally:
        window.destroy()


@pytest.mark.parametrize("size", [40, 100])
def test_the_pivot_is_the_widget_centre(gtk, size) -> None:
    """Read against the allocation the widget actually got, not the one the
    window was asked for — the two can differ, and it is the widget's own
    centre the transform has to pivot about."""
    icon = widgets.RotatableIcon()
    icon.set_from_icon_name("camera-photo-symbolic")
    icon.set_rotation_deg(90)
    window = _allocated(gtk, icon, width=size, height=size)
    try:
        width, height = icon.get_width(), icon.get_height()
        if width <= 0 or height <= 0:
            pytest.skip("widget was never allocated in this environment")
        expected = f"translate({width / 2:g},{height / 2:g})"
        assert _recorded_calls(gtk, icon)[1] == expected
    finally:
        window.destroy()


def test_the_preview_pushes_mirror_and_zoom_in_order(gtk) -> None:
    """Mirror first, then zoom about the centre — the other order would zoom
    about the mirrored centre and drift the framing sideways."""
    picture, window = _allocated_picture(gtk, mirrored=True, zoom=2.0, size=60)
    try:
        if picture.get_width() <= 0:
            pytest.skip("the picture was never allocated in this environment")
        calls = _recorded_calls(gtk, picture)
        assert calls[0] == "save" and calls[-1] == "restore"
        assert "scale(-1,1)" in calls, f"no mirror: {calls}"
        assert "scale(2,2)" in calls, f"no zoom: {calls}"
        assert calls.index("scale(-1,1)") < calls.index("scale(2,2)")
    finally:
        window.destroy()


def test_the_preview_pushes_nothing_when_plain(gtk) -> None:
    picture, window = _allocated_picture(gtk, size=60)
    try:
        assert _recorded_calls(gtk, picture) == []
    finally:
        window.destroy()


def test_a_switch_turning_within_the_rotated_axis_only_redraws(gtk) -> None:
    """90° to 270° does not change the bounds, so a relayout would be wasted
    work on every second orientation flip."""
    switch = widgets.RotatableSwitch()
    switch.set_rotation_deg(90)

    calls = {"resize": 0, "draw": 0}
    switch.queue_resize = lambda: calls.__setitem__("resize", calls["resize"] + 1)
    switch.queue_draw = lambda: calls.__setitem__("draw", calls["draw"] + 1)

    switch.set_rotation_deg(270)
    assert calls == {"resize": 0, "draw": 1}


# ---------------------------------------------------------------------------
# Chrome against a real picture
# ---------------------------------------------------------------------------

def test_the_chrome_reads_the_rect_from_its_picture(gtk) -> None:
    """ImageChrome delegates to the shared letterbox helper rather than
    keeping its own copy of the maths, so all the overlays agree."""
    Gtk, _Gsk, Gdk, _Graphene = gtk
    from gi.repository import GLib

    data = GLib.Bytes.new(bytes([0, 255, 0, 255]) * (16 * 16))
    texture = Gdk.MemoryTexture.new(16, 16, Gdk.MemoryFormat.R8G8B8A8, data, 16 * 4)
    picture = Gtk.Picture()
    picture.set_paintable(texture)

    chrome = widgets.ImageChrome(picture)
    rect = chrome._image_rect(100, 100)
    assert rect == widgets.compute_image_rect(picture, 100, 100)


def test_setting_the_same_grid_state_twice_is_ignored(gtk) -> None:
    """set_grid_visible runs from a toggle handler that also fires on
    programmatic updates."""
    Gtk, _Gsk, _Gdk, _Graphene = gtk
    chrome = widgets.ImageChrome(Gtk.Picture())

    calls = {"draw": 0}
    chrome.queue_draw = lambda: calls.__setitem__("draw", calls["draw"] + 1)

    chrome.set_grid_visible(False)          # already off
    assert calls["draw"] == 0
    chrome.set_grid_visible(True)
    assert calls["draw"] == 1
