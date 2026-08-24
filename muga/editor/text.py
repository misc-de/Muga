"""Render text to a transparent RGBA PIL image (used for text stickers)."""

from __future__ import annotations

import cairo
import gi

gi.require_version("PangoCairo", "1.0")

from gi.repository import Pango, PangoCairo

from ._pil import PILImage


def _make_text_pil(text: str, font_size: int, color: tuple) -> "PILImage.Image":
    """Render text to a transparent RGBA PIL image via PangoCairo (with outline)."""
    msurf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    mcr = cairo.Context(msurf)
    layout = PangoCairo.create_layout(mcr)
    layout.set_text(text, -1)
    PangoCairo.context_set_resolution(layout.get_context(), 96)
    desc = Pango.FontDescription.from_string("Sans Bold")
    desc.set_absolute_size(font_size * Pango.SCALE)
    layout.set_font_description(desc)
    PangoCairo.update_layout(mcr, layout)
    _, ink = layout.get_pixel_extents()
    w = max(1, ink.width) + 12
    h = max(1, ink.height) + 12
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    ox, oy = -ink.x + 6, -ink.y + 6
    layout2 = PangoCairo.create_layout(cr)
    layout2.set_text(text, -1)
    PangoCairo.context_set_resolution(layout2.get_context(), 96)
    layout2.set_font_description(desc)
    PangoCairo.update_layout(cr, layout2)
    cr.move_to(ox, oy)
    cr.set_source_rgba(0, 0, 0, 0.75)
    PangoCairo.layout_path(cr, layout2)
    cr.set_line_width(4.0)
    cr.stroke()
    cr.move_to(ox, oy)
    cr.set_source_rgba(color[0] / 255, color[1] / 255, color[2] / 255, 1.0)
    PangoCairo.show_layout(cr, layout2)
    data = bytes(surface.get_data())
    return PILImage.frombytes("RGBA", (w, h), data, "raw", "BGRA")
