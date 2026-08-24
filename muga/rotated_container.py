"""Reusable single-child container that rotates its child by 0/90/180/270°.

Used by the image viewer so a photo follows the physical device orientation
even when the compositor keeps the window in a fixed orientation (auto-rotate
off). The rotation is purely visual — it never touches the file on disk.

For 90/270 the child is allocated with its width and height swapped, then a
Gsk.Transform turns its contents so they render upright for the user when the
device is held in landscape. Allocating the swapped size (rather than rotating
a snapshot) lets a Gtk.Picture with ContentFit.CONTAIN re-fit itself into the
rotated box, so the image always fills the screen instead of spilling over.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Graphene, Gsk, Gtk


class RotatedContainer(Gtk.Widget):
    """Single-child container that rotates its child by 0/90/180/270°."""

    __gtype_name__ = "MugaRotatedContainer"

    def __init__(self) -> None:
        super().__init__()
        self._child: Gtk.Widget | None = None
        self._angle: int = 0

    def set_child(self, child: Gtk.Widget | None) -> None:
        if self._child is not None:
            self._child.unparent()
        self._child = child
        if child is not None:
            child.set_parent(self)

    def get_rotation(self) -> int:
        return self._angle

    def set_rotation(self, angle: int) -> None:
        a = angle % 360
        if a not in (0, 90, 180, 270):
            a = 0
        if a == self._angle:
            return
        self._angle = a
        self.queue_resize()

    def do_measure(self, orientation: Gtk.Orientation, for_size: int):  # type: ignore[override]
        if self._child is None:
            return (0, 0, -1, -1)
        if self._angle in (0, 180):
            return self._child.measure(orientation, for_size)
        # 90/270: report the perpendicular axis so the child gets the slot it
        # visually needs once rotated.
        other = (
            Gtk.Orientation.HORIZONTAL
            if orientation == Gtk.Orientation.VERTICAL
            else Gtk.Orientation.VERTICAL
        )
        return self._child.measure(other, for_size)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:  # type: ignore[override]
        if self._child is None:
            return
        a = self._angle
        if a == 0:
            self._child.allocate(width, height, baseline, None)
            return
        if a == 180:
            tr = (
                Gsk.Transform.new()
                .translate(Graphene.Point.alloc().init(width, height))
                .rotate(180)
            )
            self._child.allocate(width, height, baseline, tr)
            return
        if a == 90:
            tr = (
                Gsk.Transform.new()
                .translate(Graphene.Point.alloc().init(width, 0))
                .rotate(90)
            )
            self._child.allocate(height, width, baseline, tr)
            return
        # 270 (== -90)
        tr = (
            Gsk.Transform.new()
            .translate(Graphene.Point.alloc().init(0, height))
            .rotate(-90)
        )
        self._child.allocate(height, width, baseline, tr)

    def do_dispose(self) -> None:  # type: ignore[override]
        if self._child is not None:
            self._child.unparent()
            self._child = None
