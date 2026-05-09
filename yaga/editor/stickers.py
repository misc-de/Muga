"""Sticker generators (procedural shapes + emoji rendering).

Stickers are produced as transparent PIL RGBA images so the rest of the
editor pipeline can composite them with alpha_composite.
"""

from __future__ import annotations

import math

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gdk, GLib, Pango, PangoCairo

from ._pil import ImageDraw, PILImage


def _make_star(size: int = 96) -> "PILImage.Image":
    img = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    r_out = size / 2 - 3
    r_in = r_out * 0.38
    pts = []
    for i in range(10):
        a = math.pi / 5 * i - math.pi / 2
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.polygon(pts, fill=(255, 215, 0, 255))
    return img


def _make_heart(size: int = 96) -> "PILImage.Image":
    img = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2 + 2
    sc = size / 36
    pts = [(cx + 16 * math.sin(math.radians(i)) ** 3 * sc,
            cy - (13 * math.cos(math.radians(i))
                  - 5 * math.cos(math.radians(2 * i))
                  - 2 * math.cos(math.radians(3 * i))
                  - math.cos(math.radians(4 * i))) * sc)
           for i in range(360)]
    draw.polygon(pts, fill=(255, 60, 80, 255))
    return img


def _make_sparkle(size: int = 96) -> "PILImage.Image":
    img = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    r_out = size / 2 - 3
    r_in = r_out * 0.12
    pts = []
    for i in range(8):
        a = math.pi / 4 * i - math.pi / 2
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.polygon(pts, fill=(255, 255, 255, 230))
    return img


def _pil_to_texture(img: "PILImage.Image") -> Gdk.Texture:
    rgba = img.convert("RGBA")
    w, h = rgba.size
    raw = rgba.tobytes()
    gbytes = GLib.Bytes.new(raw)
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, gbytes, w * 4)


# ---------------------------------------------------------------------------
# Emoji sticker rendering via Pango+Cairo
# ---------------------------------------------------------------------------

def _emoji_to_pil(char: str, size: int = 96) -> "PILImage.Image":
    """Render an emoji character to a square PIL RGBA image using PangoCairo."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    layout = PangoCairo.create_layout(cr)
    layout.set_text(char, -1)
    PangoCairo.context_set_resolution(layout.get_context(), 96)
    desc = Pango.FontDescription.from_string("Noto Color Emoji")
    desc.set_absolute_size(int(size * 0.88) * Pango.SCALE)
    layout.set_font_description(desc)
    PangoCairo.update_layout(cr, layout)
    _, ink = layout.get_pixel_extents()
    w = ink.width or size
    h = ink.height or size
    cr.move_to(max(0.0, (size - w) / 2.0 - ink.x),
               max(0.0, (size - h) / 2.0 - ink.y))
    PangoCairo.show_layout(cr, layout)
    data = bytes(surface.get_data())
    return PILImage.frombytes("RGBA", (size, size), data, "raw", "BGRA")


_STICKER_GROUPS: list[tuple[str, list[str]]] = [
    ("Smileys",         ["😀", "😂", "🥰", "😎", "🤩", "🥳", "😇", "🙈"]),
    ("Herzen & Sterne", ["❤", "🧡", "💛", "💚", "💙", "💜", "⭐", "🌟"]),
    ("Symbole",         ["🔥", "💫", "✨", "🎉", "🎊", "🌈", "🏆", "💬"]),
]

# Cache rendered emoji at a fixed master size; rescale on demand.
_EMOJI_PIL_CACHE: "dict[str, PILImage.Image]" = {}


def _get_emoji_pil(char: str, px: int) -> "PILImage.Image":
    _MASTER = 256
    if char not in _EMOJI_PIL_CACHE:
        _EMOJI_PIL_CACHE[char] = _emoji_to_pil(char, _MASTER)
    base = _EMOJI_PIL_CACHE[char]
    if px == _MASTER:
        return base
    return base.resize((px, px), PILImage.LANCZOS)
