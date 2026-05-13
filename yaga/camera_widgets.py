"""Custom GTK widgets used by the camera viewfinder.

Extracted from camera.py for readability. Five small classes, all GTK-
side rendering tricks that don't touch the GStreamer pipeline:

  - `ImageChrome` — draws the L-shaped corner brackets + optional grid
    on the letterboxed image rect (not the widget allocation).
  - `RotatableIcon` / `RotatableSwitch` / `RotatableLabel` — snapshot-
    rotate their content by a configurable angle so the glyphs follow
    device orientation while the widget's hit box stays axis-aligned.
    Rotating the parent button would break touch coordinates.
  - `MirroredPicture` — Gtk.Picture subclass that can horizontally flip
    and/or zoom its content. Only the rendered view is transformed;
    saved frames are unaffected.

All five used to live in camera.py with leading-underscore names.
Re-exported under the original names from camera.py so call sites
don't need touching.
"""
from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Graphene, Gtk


def compute_image_rect(
    picture: Gtk.Picture, widget_w: int, widget_h: int
) -> tuple[float, float, float, float]:
    """Return the (x, y, w, h) rect of the actual letterboxed image
    inside a Gtk.Picture. Mirrors ContentFit.CONTAIN math plus the
    `set_zoom()` transform on MirroredPicture so all chrome (brackets,
    record dot, focus rect, grid) lines up with the visible image. Returns
    (0, 0, 0, 0) when there's no paintable yet — callers should skip
    painting on that signal."""
    paintable = picture.get_paintable()
    if paintable is None:
        return (0.0, 0.0, 0.0, 0.0)
    iw = max(0, paintable.get_intrinsic_width())
    ih = max(0, paintable.get_intrinsic_height())
    if iw <= 0 or ih <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    scale = min(widget_w / iw, widget_h / ih)
    img_w = iw * scale
    img_h = ih * scale
    off_x = (widget_w - img_w) / 2
    off_y = (widget_h - img_h) / 2
    zoom = getattr(picture, "get_zoom", lambda: 1.0)()
    if zoom != 1.0:
        cx, cy = widget_w / 2, widget_h / 2
        off_x = cx - (cx - off_x) * zoom
        off_y = cy - (cy - off_y) * zoom
        img_w *= zoom
        img_h *= zoom
    left = max(0.0, off_x)
    top = max(0.0, off_y)
    right = min(float(widget_w), off_x + img_w)
    bottom = min(float(widget_h), off_y + img_h)
    return (left, top, max(0.0, right - left), max(0.0, bottom - top))


class ImageChrome(Gtk.DrawingArea):
    """Single overlay that draws the L-shaped viewfinder corner brackets
    and (optionally) a rule-of-thirds grid, both positioned to the actual
    letterboxed image area inside the Gtk.Picture — not the widget
    allocation. Without this, on a tall phone window the brackets sit on
    the black bars instead of on the visible image.

    Mirrors Gtk.ContentFit.CONTAIN's centred-letterbox math plus
    MirroredPicture.set_zoom()'s centred scale, so brackets track the
    image through resolution changes and pinch-zoom."""

    __gtype_name__ = "YagaImageChrome"

    def __init__(self, picture: Gtk.Picture) -> None:
        super().__init__()
        self._picture = picture
        self._grid_visible = False
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_target(False)
        self.set_draw_func(self._on_draw)

    def set_grid_visible(self, visible: bool) -> None:
        if self._grid_visible == visible:
            return
        self._grid_visible = visible
        self.queue_draw()

    def get_grid_visible(self) -> bool:
        return self._grid_visible

    def _image_rect(self, w: int, h: int) -> tuple[float, float, float, float]:
        # Returns (0, 0, 0, 0) when no paintable / intrinsic size yet —
        # _on_draw skips painting brackets in that case, so we don't see
        # them jump from widget-bounds to image-rect once the first
        # preview frame lands.
        return compute_image_rect(self._picture, w, h)

    def _on_draw(
        self, _da: Gtk.DrawingArea, cr: Any, w: int, h: int
    ) -> None:
        x, y, rw, rh = self._image_rect(w, h)
        if rw <= 0 or rh <= 0:
            return

        # Rule-of-thirds grid, clipped to the image rect.
        if self._grid_visible:
            for offset, alpha, white in ((1.0, 0.35, False), (0.0, 0.75, True)):
                if white:
                    cr.set_source_rgba(1, 1, 1, alpha)
                else:
                    cr.set_source_rgba(0, 0, 0, alpha)
                cr.set_line_width(1.0)
                for i in (1, 2):
                    gx = x + rw * i / 3 + offset
                    cr.move_to(gx, y); cr.line_to(gx, y + rh)
                    gy = y + rh * i / 3 + offset
                    cr.move_to(x, gy); cr.line_to(x + rw, gy)
                cr.stroke()

        # Corner brackets, inset ~50 px from the image edges. The wide
        # inset leaves room for chrome buttons (close, gear, …) between
        # the bracket and the actual image corner. Length scales gently
        # with the smaller image dimension so the brackets stay
        # proportional across resolutions and zoom levels.
        cr.set_source_rgba(1, 1, 1, 0.85)
        cr.set_line_width(2.0)
        cr.set_line_cap(1)
        inset = 50.0
        length = max(12.0, min(rw, rh) * 0.06)
        for cx, cy, dx, dy in (
            (x + inset,       y + inset,       +1, +1),
            (x + rw - inset,  y + inset,       -1, +1),
            (x + inset,       y + rh - inset,  +1, -1),
            (x + rw - inset,  y + rh - inset,  -1, -1),
        ):
            cr.move_to(cx, cy + dy * length)
            cr.line_to(cx, cy)
            cr.line_to(cx + dx * length, cy)
        cr.stroke()


class RotatableIcon(Gtk.Image):
    """Gtk.Image that paints itself rotated by `rotation_deg` around its
    centre. Used inside the camera icon buttons so the glyph can follow
    device orientation while the button's hit area stays unchanged —
    rotating the entire button widget would break touch coordinates."""

    __gtype_name__ = "YagaRotatableIcon"

    def __init__(self) -> None:
        super().__init__()
        self._rotation_deg = 0.0

    def set_rotation_deg(self, deg: float) -> None:
        if abs(deg - self._rotation_deg) < 0.5:
            return
        self._rotation_deg = deg
        self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:  # type: ignore[override]
        if self._rotation_deg == 0:
            Gtk.Image.do_snapshot(self, snapshot)
            return
        w = self.get_width()
        h = self.get_height()
        snapshot.save()
        snapshot.translate(Graphene.Point().init(w / 2, h / 2))
        snapshot.rotate(self._rotation_deg)
        snapshot.translate(Graphene.Point().init(-w / 2, -h / 2))
        Gtk.Image.do_snapshot(self, snapshot)
        snapshot.restore()


class RotatableSwitch(Gtk.Switch):
    """Gtk.Switch that paints rotated around its centre, like the icons
    and labels. The switch's input still hits the original axis-aligned
    bounds, which is fine because a tap anywhere on the switch toggles
    it — the rotation is purely visual.

    Like RotatableLabel, the measure pass swaps width/height for
    90°/270° so the parent box allocates the rotated-bounds space."""

    __gtype_name__ = "YagaRotatableSwitch"

    def __init__(self) -> None:
        super().__init__()
        self._rotation_deg = 0.0

    def set_rotation_deg(self, deg: float) -> None:
        if abs(deg - self._rotation_deg) < 0.5:
            return
        old = self._rotation_deg
        self._rotation_deg = deg
        if (int(old) % 180 == 90) != (int(deg) % 180 == 90):
            self.queue_resize()
        else:
            self.queue_draw()

    def do_measure(self, orientation, for_size):  # type: ignore[override]
        if int(self._rotation_deg) % 180 == 90:
            opp = (
                Gtk.Orientation.VERTICAL
                if orientation == Gtk.Orientation.HORIZONTAL
                else Gtk.Orientation.HORIZONTAL
            )
            return Gtk.Switch.do_measure(self, opp, for_size)
        return Gtk.Switch.do_measure(self, orientation, for_size)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:  # type: ignore[override]
        if self._rotation_deg == 0:
            Gtk.Switch.do_snapshot(self, snapshot)
            return
        w = self.get_width()
        h = self.get_height()
        snapshot.save()
        snapshot.translate(Graphene.Point().init(w / 2, h / 2))
        snapshot.rotate(self._rotation_deg)
        snapshot.translate(Graphene.Point().init(-w / 2, -h / 2))
        Gtk.Switch.do_snapshot(self, snapshot)
        snapshot.restore()


class RotatableLabel(Gtk.Label):
    """Same snapshot-rotation trick as RotatableIcon but for text, so
    the timer's "3s" / "10s" label can rotate with device orientation.

    For 90°/270° rotations we also swap the measured width/height —
    the label paints rotated, so its parent box needs to allocate
    rotated-bounds space, not the unrotated text extents. Without
    that, a "Geotagging" label gets a wide-and-short slot and the
    rotated text spills outside its widget bounds (clipping the
    leading 'G' on the screen)."""

    __gtype_name__ = "YagaRotatableLabel"

    def __init__(self) -> None:
        super().__init__()
        self._rotation_deg = 0.0

    def set_rotation_deg(self, deg: float) -> None:
        if abs(deg - self._rotation_deg) < 0.5:
            return
        old = self._rotation_deg
        self._rotation_deg = deg
        # If we just crossed a 90° boundary, the measured size has
        # flipped — request a re-layout so the parent box reallocates.
        if (int(old) % 180 == 90) != (int(deg) % 180 == 90):
            self.queue_resize()
        else:
            self.queue_draw()

    def do_measure(self, orientation, for_size):  # type: ignore[override]
        if int(self._rotation_deg) % 180 == 90:
            # Rotated 90°/270°: ask Gtk.Label for the perpendicular
            # axis so the rotated text gets the slot it visually needs.
            opp = (
                Gtk.Orientation.VERTICAL
                if orientation == Gtk.Orientation.HORIZONTAL
                else Gtk.Orientation.HORIZONTAL
            )
            return Gtk.Label.do_measure(self, opp, for_size)
        return Gtk.Label.do_measure(self, orientation, for_size)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:  # type: ignore[override]
        if self._rotation_deg == 0:
            Gtk.Label.do_snapshot(self, snapshot)
            return
        w = self.get_width()
        h = self.get_height()
        snapshot.save()
        snapshot.translate(Graphene.Point().init(w / 2, h / 2))
        snapshot.rotate(self._rotation_deg)
        snapshot.translate(Graphene.Point().init(-w / 2, -h / 2))
        Gtk.Label.do_snapshot(self, snapshot)
        snapshot.restore()


class MirroredPicture(Gtk.Picture):
    """Gtk.Picture that can render its content horizontally flipped and/or
    zoomed about its center.

    Only the on-screen render is transformed — captured frames are
    unaffected, so text in front-cam selfies still reads correctly in
    saved files and zoom is purely a viewfinder affordance. Implementing
    zoom widget-side (rather than via a videocrop pipeline element) keeps
    the GStreamer chain minimal, which avoids negotiation failures on
    cameras whose only modes are MJPG.
    """

    __gtype_name__ = "YagaMirroredPicture"

    def __init__(self) -> None:
        super().__init__()
        self._mirrored = False
        self._zoom = 1.0

    def set_mirrored(self, mirrored: bool) -> None:
        if self._mirrored == mirrored:
            return
        self._mirrored = mirrored
        self.queue_draw()

    def set_zoom(self, zoom: float) -> None:
        zoom = max(1.0, min(8.0, zoom))
        if abs(zoom - self._zoom) < 0.01:
            return
        self._zoom = zoom
        self.queue_draw()

    def get_zoom(self) -> float:
        return self._zoom

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:  # type: ignore[override]
        zoom = self._zoom
        mirror = self._mirrored
        if zoom == 1.0 and not mirror:
            Gtk.Picture.do_snapshot(self, snapshot)
            return
        w = self.get_width()
        h = self.get_height()
        snapshot.save()
        if mirror:
            snapshot.translate(Graphene.Point().init(w, 0))
            snapshot.scale(-1.0, 1.0)
        if zoom != 1.0:
            cx = w / 2 if not mirror else w / 2
            cy = h / 2
            snapshot.translate(Graphene.Point().init(cx, cy))
            snapshot.scale(zoom, zoom)
            snapshot.translate(Graphene.Point().init(-cx, -cy))
        Gtk.Picture.do_snapshot(self, snapshot)
        snapshot.restore()
