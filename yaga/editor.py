from __future__ import annotations

import math
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

from .models import MediaItem

try:
    from PIL import Image as PILImage, ImageEnhance, ImageFilter, ImageOps, ImageDraw
    _PIL_OK = True
except ImportError:
    PILImage = ImageEnhance = ImageFilter = ImageOps = ImageDraw = None
    _PIL_OK = False

# ---------------------------------------------------------------------------
# Sticker generators (drawn with Pillow at runtime, no external assets needed)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Frame themes
# ---------------------------------------------------------------------------

_FRAME_THEMES: list[tuple[str, str, tuple[int, int, int], tuple[int, int, int]]] = [
    ("christmas",  "Christmas",   (180, 28,  32),  (20,  115, 64)),
    ("silvester",  "New Year",    (25,  24,  34),  (255, 211, 84)),
    ("ostern",     "Easter",      (255, 176, 222), (128, 207, 130)),
    ("hochzeit",   "Wedding",     (255, 252, 242), (207, 181, 132)),
    ("geburtstag", "Birthday",    (255, 96,  165), (255, 215, 48)),
    ("fruehling",  "Spring",      (126, 204, 120), (255, 176, 214)),
    ("sommer",     "Summer",      (255, 205, 58),  (34,  174, 210)),
    ("winter",     "Winter",      (188, 226, 255), (86,  147, 220)),
]


def _frame_pil(iw: int, ih: int, theme: str) -> "PILImage.Image | None":
    """Transparent RGBA decorative border overlay."""
    theme_data = {t[0]: (t[2], t[3]) for t in _FRAME_THEMES}
    if theme not in theme_data:
        return None
    c1, c2 = theme_data[theme]
    frame = PILImage.new("RGBA", (iw, ih), (0, 0, 0, 0))
    draw = ImageDraw.Draw(frame)
    bw = max(28, min(iw, ih) // 8)
    _draw_soft_border(draw, iw, ih, bw, c1, c2)
    if theme == "christmas":
        _decorate_christmas(draw, iw, ih, bw)
    elif theme == "silvester":
        _decorate_new_year(draw, iw, ih, bw)
    elif theme == "ostern":
        _decorate_easter(draw, iw, ih, bw)
    elif theme == "hochzeit":
        _decorate_wedding(draw, iw, ih, bw)
    elif theme == "geburtstag":
        _decorate_birthday(draw, iw, ih, bw)
    elif theme == "fruehling":
        _decorate_spring(draw, iw, ih, bw)
    elif theme == "sommer":
        _decorate_summer(draw, iw, ih, bw)
    elif theme == "winter":
        _decorate_winter(draw, iw, ih, bw)
    return frame


def _draw_soft_border(draw: ImageDraw.ImageDraw, iw: int, ih: int, bw: int, c1: tuple, c2: tuple) -> None:
    shade = (0, 0, 0, 68)
    draw.rectangle([0, 0, iw, bw], fill=c1 + (110,))
    draw.rectangle([0, ih - bw, iw, ih], fill=c1 + (110,))
    draw.rectangle([0, 0, bw, ih], fill=c2 + (88,))
    draw.rectangle([iw - bw, 0, iw, ih], fill=c2 + (88,))
    for i in range(max(2, bw // 12)):
        draw.rectangle([i, i, iw - i - 1, ih - i - 1], outline=shade)
    ribbon = max(5, bw // 6)
    draw.rounded_rectangle([ribbon, ribbon, iw - ribbon - 1, ih - ribbon - 1], radius=bw // 2, outline=(255, 255, 255, 120), width=max(2, ribbon // 2))
    draw.rounded_rectangle([bw // 2, bw // 2, iw - bw // 2 - 1, ih - bw // 2 - 1], radius=bw // 2, outline=c2 + (220,), width=max(2, bw // 10))
    draw.rounded_rectangle([bw, bw, iw - bw - 1, ih - bw - 1], radius=bw // 3, outline=(255, 255, 255, 155), width=max(1, bw // 18))


def _edge_positions(length: int, margin: int, count: int) -> list[int]:
    if count <= 1:
        return [length // 2]
    usable = max(1, length - margin * 2)
    return [margin + round(usable * i / (count - 1)) for i in range(count)]


def _draw_star_shape(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple, points: int = 5) -> None:
    coords = []
    for i in range(points * 2):
        angle = math.pi * i / points - math.pi / 2
        radius = r if i % 2 == 0 else r * 0.42
        coords.append((x + math.cos(angle) * radius, y + math.sin(angle) * radius))
    draw.polygon(coords, fill=color)


def _draw_flower(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, petal: tuple, center: tuple) -> None:
    for angle in range(0, 360, 60):
        dx = math.cos(math.radians(angle)) * r * 0.75
        dy = math.sin(math.radians(angle)) * r * 0.75
        draw.ellipse([x + dx - r * 0.45, y + dy - r * 0.45, x + dx + r * 0.45, y + dy + r * 0.45], fill=petal)
    draw.ellipse([x - r * 0.38, y - r * 0.38, x + r * 0.38, y + r * 0.38], fill=center)


def _draw_snowflake(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    for angle in range(0, 180, 30):
        dx = math.cos(math.radians(angle)) * r
        dy = math.sin(math.radians(angle)) * r
        draw.line([x - dx, y - dy, x + dx, y + dy], fill=color, width=max(1, int(r // 8)))


def _draw_bow(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple, knot: tuple) -> None:
    draw.polygon([(x, y), (x - r, y - r * 0.55), (x - r, y + r * 0.55)], fill=color)
    draw.polygon([(x, y), (x + r, y - r * 0.55), (x + r, y + r * 0.55)], fill=color)
    draw.ellipse([x - r * 0.24, y - r * 0.24, x + r * 0.24, y + r * 0.24], fill=knot)
    draw.line([x - r * 0.35, y + r * 0.45, x - r * 0.62, y + r * 1.05], fill=color, width=max(2, int(r // 5)))
    draw.line([x + r * 0.35, y + r * 0.45, x + r * 0.62, y + r * 1.05], fill=color, width=max(2, int(r // 5)))


def _draw_leaf(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, angle: float, color: tuple) -> None:
    dx = math.cos(angle) * r
    dy = math.sin(angle) * r
    x0 = x - dx - r * 0.35
    y0 = y - dy - r * 0.18
    x1 = x + dx + r * 0.35
    y1 = y + dy + r * 0.18
    draw.ellipse([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], fill=color)


def _draw_gift(draw: ImageDraw.ImageDraw, x: float, y: float, s: float, box: tuple, ribbon: tuple) -> None:
    draw.rounded_rectangle([x - s, y - s * 0.55, x + s, y + s], radius=max(2, int(s // 8)), fill=box)
    draw.rectangle([x - s, y - s * 0.12, x + s, y + s * 0.10], fill=ribbon)
    draw.rectangle([x - s * 0.12, y - s * 0.55, x + s * 0.12, y + s], fill=ribbon)
    _draw_bow(draw, x, y - s * 0.62, s * 0.35, ribbon, (255, 255, 255, 210))


def _draw_palm(draw: ImageDraw.ImageDraw, x: float, y: float, r: float) -> None:
    trunk = (132, 89, 45, 225)
    leaf = (36, 151, 91, 232)
    draw.line([x, y, x + r * 0.20, y + r * 1.35], fill=trunk, width=max(3, int(r // 6)))
    for angle in [-2.7, -2.25, -1.85, -1.35, -0.95, -0.55]:
        ex = x + math.cos(angle) * r
        ey = y + math.sin(angle) * r * 0.62
        draw.line([x, y, ex, ey], fill=leaf, width=max(3, int(r // 8)))
        _draw_leaf(draw, (x + ex) / 2, (y + ey) / 2, r * 0.24, angle, leaf)


def _decorate_christmas(draw: ImageDraw.ImageDraw, iw: int, ih: int, bw: int) -> None:
    green = (30, 128, 72, 235)
    red = (218, 40, 45, 245)
    gold = (255, 218, 86, 240)
    for x in _edge_positions(iw, bw, 15):
        y = bw * 0.55 + math.sin(x / max(1, iw) * math.tau * 3) * bw * 0.16
        draw.line([x - bw * 0.35, y, x + bw * 0.35, y], fill=green, width=max(3, bw // 10))
        for angle in [-0.8, -0.35, 0.35, 0.8]:
            _draw_leaf(draw, x, y, bw * 0.24, angle, green)
        draw.ellipse([x - bw * 0.13, y - bw * 0.13, x + bw * 0.13, y + bw * 0.13], fill=red)
        if int(x) % 3 == 0:
            _draw_star_shape(draw, x, y + bw * 0.45, bw * 0.18, gold)
    for x, y in [(bw, bw), (iw - bw, bw), (bw, ih - bw), (iw - bw, ih - bw)]:
        _draw_star_shape(draw, x, y, bw * 0.62, gold)
        _draw_bow(draw, x, y + bw * 0.55, bw * 0.42, red, gold)


def _decorate_new_year(draw: ImageDraw.ImageDraw, iw: int, ih: int, bw: int) -> None:
    colors = [(255, 215, 74, 245), (119, 216, 255, 230), (255, 95, 126, 230), (180, 130, 255, 225)]
    centers = [(bw * 1.3, bw * 1.2), (iw - bw * 1.4, bw * 1.35), (iw * 0.5, ih - bw * 0.95), (iw * 0.5, bw * 0.8)]
    for idx, (cx, cy) in enumerate(centers):
        for ray in range(12):
            angle = math.tau * ray / 12
            r1 = bw * 0.18
            r2 = bw * (0.55 + 0.12 * (ray % 2))
            color = colors[(idx + ray) % len(colors)]
            draw.line([cx + math.cos(angle) * r1, cy + math.sin(angle) * r1, cx + math.cos(angle) * r2, cy + math.sin(angle) * r2], fill=color, width=max(1, bw // 12))
    for i, x in enumerate(_edge_positions(iw, bw, 12)):
        _draw_star_shape(draw, x, bw * 0.48 if i % 2 else ih - bw * 0.48, bw * 0.16, colors[i % len(colors)], points=4)
    for i, x in enumerate(_edge_positions(iw, bw, 18)):
        y = bw * 0.92 if i % 2 else ih - bw * 0.92
        draw.ellipse([x - bw * 0.08, y - bw * 0.08, x + bw * 0.08, y + bw * 0.08], fill=colors[(i + 2) % len(colors)])


def _decorate_easter(draw: ImageDraw.ImageDraw, iw: int, ih: int, bw: int) -> None:
    egg_colors = [(255, 185, 218, 238), (167, 220, 143, 238), (255, 232, 122, 238), (156, 205, 255, 238)]
    for i, x in enumerate(_edge_positions(iw, bw, 8)):
        y = ih - bw * 0.58
        color = egg_colors[i % len(egg_colors)]
        draw.ellipse([x - bw * 0.23, y - bw * 0.34, x + bw * 0.23, y + bw * 0.34], fill=color)
        draw.arc([x - bw * 0.17, y - bw * 0.10, x + bw * 0.17, y + bw * 0.22], 0, 180, fill=(255, 255, 255, 210), width=max(1, bw // 12))
    for y in _edge_positions(ih, bw, 6):
        _draw_flower(draw, bw * 0.45, y, bw * 0.20, (255, 220, 236, 230), (255, 200, 76, 245))
    for x in [bw * 1.15, iw - bw * 1.15]:
        y = bw * 0.92
        draw.ellipse([x - bw * 0.42, y - bw * 0.85, x - bw * 0.08, y], fill=(255, 245, 250, 230))
        draw.ellipse([x + bw * 0.08, y - bw * 0.85, x + bw * 0.42, y], fill=(255, 245, 250, 230))
        draw.ellipse([x - bw * 0.55, y - bw * 0.28, x + bw * 0.55, y + bw * 0.52], fill=(255, 250, 252, 230))


def _decorate_wedding(draw: ImageDraw.ImageDraw, iw: int, ih: int, bw: int) -> None:
    pearl = (255, 255, 246, 230)
    gold = (214, 181, 104, 230)
    for x in _edge_positions(iw, bw // 2, 18):
        draw.ellipse([x - bw * 0.09, bw * 0.42 - bw * 0.09, x + bw * 0.09, bw * 0.42 + bw * 0.09], fill=pearl)
        draw.ellipse([x - bw * 0.09, ih - bw * 0.42 - bw * 0.09, x + bw * 0.09, ih - bw * 0.42 + bw * 0.09], fill=pearl)
    cx, cy = iw - bw * 1.25, bw * 1.05
    draw.ellipse([cx - bw * 0.55, cy - bw * 0.30, cx + bw * 0.05, cy + bw * 0.30], outline=gold, width=max(2, bw // 8))
    draw.ellipse([cx - bw * 0.05, cy - bw * 0.30, cx + bw * 0.55, cy + bw * 0.30], outline=gold, width=max(2, bw // 8))
    for x, y in [(bw, ih - bw), (iw - bw, ih - bw), (bw, bw)]:
        _draw_flower(draw, x, y, bw * 0.33, (255, 255, 255, 235), gold)
        _draw_flower(draw, x + bw * 0.45, y - bw * 0.05, bw * 0.22, (255, 235, 242, 220), gold)


def _decorate_birthday(draw: ImageDraw.ImageDraw, iw: int, ih: int, bw: int) -> None:
    colors = [(255, 74, 134, 238), (255, 211, 58, 238), (74, 190, 255, 238), (132, 224, 109, 238)]
    for i, x in enumerate(_edge_positions(iw, bw, 7)):
        y = bw * 0.72
        color = colors[i % len(colors)]
        draw.ellipse([x - bw * 0.22, y - bw * 0.34, x + bw * 0.22, y + bw * 0.30], fill=color)
        draw.line([x, y + bw * 0.30, x - bw * 0.10, y + bw * 0.70], fill=(255, 255, 255, 180), width=max(1, bw // 12))
    for i, x in enumerate(_edge_positions(iw, bw, 14)):
        draw.rectangle([x, ih - bw * 0.65, x + bw * 0.12, ih - bw * 0.53], fill=colors[i % len(colors)])
    for i, x in enumerate(_edge_positions(iw, bw, 9)):
        y = bw * 0.12
        draw.polygon([(x - bw * 0.28, y), (x + bw * 0.28, y), (x, y + bw * 0.52)], fill=colors[(i + 1) % len(colors)])
    _draw_gift(draw, bw * 1.1, ih - bw * 0.72, bw * 0.48, colors[0], colors[1])
    _draw_gift(draw, iw - bw * 1.1, ih - bw * 0.72, bw * 0.48, colors[2], colors[3])


def _decorate_spring(draw: ImageDraw.ImageDraw, iw: int, ih: int, bw: int) -> None:
    petals = [(255, 177, 213, 235), (255, 229, 119, 235), (184, 222, 112, 235)]
    for i, x in enumerate(_edge_positions(iw, bw, 10)):
        _draw_flower(draw, x, bw * 0.55, bw * 0.18, petals[i % len(petals)], (255, 214, 77, 245))
    for i, y in enumerate(_edge_positions(ih, bw, 8)):
        draw.ellipse([iw - bw * 0.66, y - bw * 0.16, iw - bw * 0.30, y + bw * 0.13], fill=(89, 170, 91, 220))
    for x, y in [(bw * 0.95, ih - bw * 0.85), (iw - bw * 0.95, ih - bw * 0.85)]:
        for k in range(5):
            _draw_flower(draw, x + (k - 2) * bw * 0.22, y - abs(k - 2) * bw * 0.10, bw * 0.24, petals[k % len(petals)], (255, 220, 70, 245))


def _decorate_summer(draw: ImageDraw.ImageDraw, iw: int, ih: int, bw: int) -> None:
    sun = (255, 214, 58, 245)
    water = (44, 180, 214, 225)
    _draw_star_shape(draw, bw * 1.10, bw * 1.05, bw * 0.90, sun, points=18)
    _draw_palm(draw, iw - bw * 0.85, bw * 0.95, bw * 0.88)
    for x in _edge_positions(iw, bw, 12):
        y = ih - bw * 0.48
        draw.arc([x - bw * 0.22, y - bw * 0.12, x + bw * 0.22, y + bw * 0.24], 0, 180, fill=water, width=max(2, bw // 10))
    for x, y in [(iw - bw * 1.15, bw * 0.95), (iw - bw * 0.72, bw * 1.32)]:
        draw.ellipse([x - bw * 0.18, y - bw * 0.12, x + bw * 0.18, y + bw * 0.12], fill=(255, 245, 205, 235))
    draw.pieslice([bw * 0.45, ih - bw * 1.25, bw * 1.85, ih + bw * 0.15], 180, 360, fill=(255, 102, 92, 230))
    draw.line([bw * 1.15, ih - bw * 0.58, bw * 0.95, ih - bw * 0.15], fill=(255, 255, 255, 220), width=max(2, bw // 10))


def _decorate_winter(draw: ImageDraw.ImageDraw, iw: int, ih: int, bw: int) -> None:
    snow = (245, 252, 255, 240)
    blue = (138, 197, 245, 225)
    for i, x in enumerate(_edge_positions(iw, bw, 9)):
        _draw_snowflake(draw, x, bw * 0.55, bw * (0.18 + 0.04 * (i % 2)), snow)
        _draw_snowflake(draw, x, ih - bw * 0.55, bw * 0.16, blue)
    for y in _edge_positions(ih, bw, 7):
        draw.ellipse([bw * 0.36, y - bw * 0.08, bw * 0.52, y + bw * 0.08], fill=snow)
    for x in _edge_positions(iw, bw, 12):
        draw.polygon([(x - bw * 0.12, 0), (x + bw * 0.12, 0), (x, bw * 0.62)], fill=(228, 246, 255, 210))
    _draw_snowflake(draw, iw - bw * 1.05, bw * 1.10, bw * 0.56, snow)
    _draw_snowflake(draw, bw * 1.05, ih - bw * 1.10, bw * 0.48, snow)


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


# ---------------------------------------------------------------------------
# Filter presets (PIL, each takes/returns RGB Image)
# ---------------------------------------------------------------------------

def _filter_bw(img: "PILImage.Image") -> "PILImage.Image":
    return ImageOps.grayscale(img).convert("RGB")


def _filter_sepia(img: "PILImage.Image") -> "PILImage.Image":
    return img.convert("RGB", matrix=(
        0.393, 0.769, 0.189, 0,
        0.349, 0.686, 0.168, 0,
        0.272, 0.534, 0.131, 0,
    ))


def _filter_warm(img: "PILImage.Image") -> "PILImage.Image":
    r, g, b = img.split()
    r = r.point(lambda x: min(255, int(x * 1.18)))
    g = g.point(lambda x: min(255, int(x * 1.04)))
    b = b.point(lambda x: max(0,   int(x * 0.82)))
    return PILImage.merge("RGB", (r, g, b))


def _filter_cool(img: "PILImage.Image") -> "PILImage.Image":
    r, g, b = img.split()
    r = r.point(lambda x: max(0,   int(x * 0.82)))
    g = g.point(lambda x: min(255, int(x * 1.02)))
    b = b.point(lambda x: min(255, int(x * 1.20)))
    return PILImage.merge("RGB", (r, g, b))


def _filter_fade(img: "PILImage.Image") -> "PILImage.Image":
    result = ImageEnhance.Contrast(img).enhance(0.68)
    result = ImageEnhance.Brightness(result).enhance(1.06)
    return result.point(lambda x: int(x * 0.83 + 28))


def _filter_dramatic(img: "PILImage.Image") -> "PILImage.Image":
    result = ImageEnhance.Contrast(img).enhance(1.85)
    return ImageEnhance.Color(result).enhance(0.55)


def _filter_vintage(img: "PILImage.Image") -> "PILImage.Image":
    result = ImageEnhance.Color(img).enhance(0.65)
    result = ImageEnhance.Contrast(result).enhance(0.88)
    r, g, b = result.split()
    r = r.point(lambda x: min(255, int(x * 1.12 + 8)))
    g = g.point(lambda x: min(255, int(x * 1.04)))
    b = b.point(lambda x: max(0,   int(x * 0.80)))
    return PILImage.merge("RGB", (r, g, b))


def _filter_invert(img: "PILImage.Image") -> "PILImage.Image":
    return ImageOps.invert(img)


_FILTER_DEFS: list[tuple[str, str, object]] = [
    ("none",      "Original",    None),
    ("bw",        "S/W",         _filter_bw),
    ("sepia",     "Sepia",       _filter_sepia),
    ("warm",      "Warm",        _filter_warm),
    ("cool",      "Kalt",        _filter_cool),
    ("fade",      "Verblasst",   _filter_fade),
    ("dramatic",  "Dramatisch",  _filter_dramatic),
    ("vintage",   "Vintage",     _filter_vintage),
    ("invert",    "Invertieren", _filter_invert),
]


# ---------------------------------------------------------------------------
class EditorView(Gtk.Box):
    """In-app image editor with collapsible bottom-nav panels."""

    def __init__(self, item: MediaItem, translate=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._translate = translate or (lambda text: text)
        self._item = item
        self._original = PILImage.open(item.path)
        if self._original.mode not in ("RGB", "RGBA"):
            self._original = self._original.convert("RGB")
        self._working = self._original.copy()

        self._filter_mode = "none"
        self._brightness = 1.0
        self._contrast = 1.0
        self._red = 1.0
        self._green = 1.0
        self._blue = 1.0

        self._crop_mode = False
        self._crop_start: tuple[float, float] | None = None
        self._crop_current: tuple[float, float] | None = None
        self._pending_crop: tuple[int, int, int, int] | None = None
        self._crop_rect_disp: tuple[float, float, float, float] | None = None
        self._crop_active_handle: str | None = None
        self._crop_handle_orig: tuple[float, float, float, float] | None = None

        # sticker
        self._sticker_source: "str | PILImage.Image | None" = None
        self._sticker_rel = (0.5, 0.5)          # center as fraction of image
        self._sticker_size_frac = 0.25           # sticker width as fraction of image width
        self._stickers: list[dict] = []
        self._active_sticker: int | None = None
        self._sticker_zoom_start = 0.25
        self._sticker_del_rect: tuple[float, float, float, float] | None = None
        self._drag_sticker = False
        self._drag_sx = 0.0
        self._drag_sy = 0.0

        # frame overlay
        self._frame_theme: str | None = None

        # text sticker
        self._text_color: tuple = (255, 255, 255)

        # obfuscate (blur brush)
        self._obfuscate_mode: bool = False
        self._obfuscate_strokes: list[tuple[float, float, float, tuple[float, float, float, float]]] = []  # (x, y, r, (r, g, b, a))
        self._obfuscate_brush_size: float = 0.08
        self._obfuscate_drag_origin: tuple[float, float] | None = None

        # Undo/Redo history
        self._history_undo: list["PILImage.Image"] = []
        self._history_redo: list["PILImage.Image"] = []
        self._history_max_steps: int = 20  # Limit memory usage
        self._slider_snapshot_id: int | None = None  # Debounce timer for slider changes

        self._active_panel: str | None = None
        self._update_id: int | None = None
        self._nav_handler_ids: dict[str, int] = {}

        self._build_ui()
        self._schedule_update()

    def _(self, text: str) -> str:
        return self._translate(text)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── Preview overlay ──
        self._preview = Gtk.Picture()
        self._preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._preview.set_hexpand(True)
        self._preview.set_vexpand(True)

        self._draw_area = Gtk.DrawingArea()
        self._draw_area.set_hexpand(True)
        self._draw_area.set_vexpand(True)
        self._draw_area.set_draw_func(self._draw_overlay)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self._draw_area.add_controller(drag)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_overlay_click)
        self._draw_area.add_controller(click)

        zoom_g = Gtk.GestureZoom()
        zoom_g.connect("begin", self._on_sticker_zoom_begin)
        zoom_g.connect("scale-changed", self._on_sticker_zoom_scale)
        self._draw_area.add_controller(zoom_g)

        self._image_overlay = Gtk.Overlay()
        self._image_overlay.set_hexpand(True)
        self._image_overlay.set_vexpand(True)
        self._image_overlay.set_child(self._preview)
        self._image_overlay.add_overlay(self._draw_area)

        # ── Panel revealer (transition direction is set in _apply_orientation) ──
        self._panel_stack = Gtk.Stack()
        self._panel_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._panel_stack.set_vhomogeneous(False)
        self._panel_stack.add_named(self._build_panel_filter(), "filter")
        self._panel_stack.add_named(self._build_panel_adjust(), "adjust")
        self._panel_stack.add_named(self._build_panel_effects(), "effects")
        self._panel_stack.add_named(self._build_panel_sticker(), "sticker")
        self._panel_stack.add_named(self._build_panel_crop(), "crop")

        # In landscape the panels can get long — wrap in a ScrolledWindow.
        self._panel_scroller = Gtk.ScrolledWindow()
        self._panel_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._panel_scroller.set_child(self._panel_stack)

        self._panel_revealer = Gtk.Revealer()
        self._panel_revealer.set_transition_duration(180)
        self._panel_revealer.set_reveal_child(False)
        self._panel_revealer.set_child(self._panel_scroller)

        # ── Nav bar (orientation flipped on landscape vs portrait) ──
        self._nav_box = Gtk.Box(spacing=0)
        self._nav_box.add_css_class("toolbar")

        self._nav_btns: dict[str, Gtk.ToggleButton] = {}
        for key, icon, label in [
            ("filter",   "image-filter-symbolic",       "Filter"),
            ("adjust",   "display-brightness-symbolic", "Anpassen"),
            ("effects",  "image-adjust-symbolic",       "Effekte"),
            ("sticker",  "face-smile-symbolic",         "Sticker"),
            ("crop",     "crop-symbolic",               "Zuschneiden"),
        ]:
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.set_margin_top(6)
            inner.set_margin_bottom(6)
            inner.set_margin_start(6)
            inner.set_margin_end(6)
            inner.append(Gtk.Image.new_from_icon_name(icon))
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("caption")
            inner.append(lbl)
            btn = Gtk.ToggleButton()
            btn.add_css_class("flat")
            btn.set_child(inner)
            hid = btn.connect("toggled", self._on_nav_toggled, key)
            self._nav_handler_ids[key] = hid
            self._nav_box.append(btn)
            self._nav_btns[key] = btn

        # Apply initial layout; reapply whenever the editor's allocation flips
        # between landscape and portrait shape.
        self._is_landscape: bool | None = None
        self._apply_orientation(False)
        self.add_tick_callback(self._on_orientation_tick)

    # ── Responsive orientation ──

    def _on_orientation_tick(self, widget, _clock) -> bool:
        w = widget.get_width()
        h = widget.get_height()
        if w > 0 and h > 0:
            # Threshold with hysteresis to avoid flapping at near-square sizes.
            if self._is_landscape:
                landscape = w > h * 1.05
            else:
                landscape = w > h * 1.25
            if landscape != self._is_landscape:
                self._apply_orientation(landscape)
        return True  # GLib.SOURCE_CONTINUE

    def _apply_orientation(self, landscape: bool) -> None:
        if landscape == self._is_landscape:
            return
        self._is_landscape = landscape
        # Detach current children (a widget can only have one parent in GTK4).
        for child in (self._image_overlay, self._panel_revealer, self._nav_box):
            parent = child.get_parent()
            if parent is self:
                self.remove(child)
        if landscape:
            # Layout: [ nav | panel | image ] horizontally
            self.set_orientation(Gtk.Orientation.HORIZONTAL)
            self._nav_box.set_orientation(Gtk.Orientation.VERTICAL)
            self._nav_box.set_hexpand(False)
            self._nav_box.set_vexpand(True)
            for btn in self._nav_btns.values():
                btn.set_hexpand(False)
                btn.set_vexpand(False)
            self._panel_revealer.set_transition_type(
                Gtk.RevealerTransitionType.SLIDE_RIGHT
            )
            # Cap panel width so the image keeps the majority of the space.
            self._panel_scroller.set_size_request(280, -1)
            self.append(self._nav_box)
            self.append(self._panel_revealer)
            self.append(self._image_overlay)
        else:
            # Layout: [ image / panel / nav ] vertically (original)
            self.set_orientation(Gtk.Orientation.VERTICAL)
            self._nav_box.set_orientation(Gtk.Orientation.HORIZONTAL)
            self._nav_box.set_hexpand(True)
            self._nav_box.set_vexpand(False)
            for btn in self._nav_btns.values():
                btn.set_hexpand(True)
                btn.set_vexpand(False)
            self._panel_revealer.set_transition_type(
                Gtk.RevealerTransitionType.SLIDE_UP
            )
            self._panel_scroller.set_size_request(-1, -1)
            self.append(self._image_overlay)
            self.append(self._panel_revealer)
            self.append(self._nav_box)

    # ── Panel builders ──

    def _build_panel_filter(self) -> Gtk.Widget:
        # Build a square 72-px center-crop thumbnail from the working image once
        iw, ih = self._working.size
        side = min(iw, ih)
        base = self._working.crop(
            ((iw - side) // 2, (ih - side) // 2,
             (iw + side) // 2, (ih + side) // 2)
        ).resize((72, 72), PILImage.BILINEAR).convert("RGB")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroll.set_hexpand(True)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row.set_margin_start(8)
        row.set_margin_end(8)
        row.set_margin_top(6)
        row.set_margin_bottom(6)

        self._filter_btns: dict[str, Gtk.ToggleButton] = {}
        for mode, label, fn in _FILTER_DEFS:
            preview = fn(base) if fn else base.copy()
            texture = _pil_to_texture(preview)

            pic = Gtk.Picture()
            pic.set_paintable(texture)
            pic.set_size_request(64, 64)
            pic.set_content_fit(Gtk.ContentFit.FILL)
            pic.set_hexpand(False)

            lbl = Gtk.Label(label=label)
            lbl.add_css_class("caption")
            lbl.set_max_width_chars(8)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)

            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.set_margin_top(4)
            inner.set_margin_bottom(4)
            inner.set_margin_start(2)
            inner.set_margin_end(2)
            inner.append(pic)
            inner.append(lbl)

            btn = Gtk.ToggleButton()
            btn.add_css_class("flat")
            btn.set_active(mode == self._filter_mode)
            btn.set_child(inner)
            btn.connect("toggled", self._on_filter_toggled, mode)
            row.append(btn)
            self._filter_btns[mode] = btn

        scroll.set_child(row)
        return scroll

    def _build_panel_adjust(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        for attr, label in [
            ("_brightness", "Brightness"),
            ("_contrast",   "Contrast"),
            ("_red",        "Red"),
            ("_green",      "Green"),
            ("_blue",       "Blue"),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            l = Gtk.Label(label=label, xalign=0)
            l.set_size_request(80, -1)
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 2.0, 0.05)
            sc.set_value(getattr(self, attr))
            sc.set_hexpand(True)
            sc.set_draw_value(False)
            sc.connect("value-changed", self._on_slider, attr)
            reset = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
            reset.add_css_class("flat")
            reset.set_tooltip_text("Reset")
            reset.connect("clicked", self._reset_slider, attr, sc)
            row.append(l)
            row.append(sc)
            row.append(reset)
            box.append(row)
        return box

    def _build_panel_sticker(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # ── Sub-stack: one page per category ──
        self._sticker_sub_stack = Gtk.Stack()
        self._sticker_sub_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._sticker_sub_stack.set_vhomogeneous(False)

        # Emoji pages (smileys / emotions / symbole)
        for key, emojis in [
            ("smileys",  _STICKER_GROUPS[0][1]),
            ("emotions", _STICKER_GROUPS[1][1]),
            ("symbole",  _STICKER_GROUPS[2][1]),
        ]:
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            scroll.set_hexpand(True)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            row.set_margin_start(6); row.set_margin_end(6)
            row.set_margin_top(5);   row.set_margin_bottom(5)
            for emoji in emojis:
                try:
                    tex = _pil_to_texture(_emoji_to_pil(emoji, 56))
                    pic = Gtk.Picture()
                    pic.set_paintable(tex)
                    pic.set_size_request(52, 52)
                    pic.set_content_fit(Gtk.ContentFit.FILL)
                    pic.set_margin_top(3);    pic.set_margin_bottom(3)
                    pic.set_margin_start(2);  pic.set_margin_end(2)
                    btn = Gtk.Button()
                    btn.add_css_class("flat")
                    btn.set_child(pic)
                except Exception:
                    btn = Gtk.Button(label=emoji)
                    btn.add_css_class("flat")
                    btn.set_size_request(48, 48)
                btn.connect("clicked", self._on_sticker_clicked, emoji)
                row.append(btn)
            scroll.set_child(row)
            self._sticker_sub_stack.add_named(scroll, key)

        # Rahmen page (frame theme thumbnails as toggle buttons)
        rahmen_scroll = Gtk.ScrolledWindow()
        rahmen_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        rahmen_scroll.set_hexpand(True)
        rahmen_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        rahmen_row.set_margin_start(6); rahmen_row.set_margin_end(6)
        rahmen_row.set_margin_top(5);   rahmen_row.set_margin_bottom(5)
        self._frame_btns: dict[str, Gtk.ToggleButton] = {}
        self._frame_hids: dict[str, int] = {}
        iw, ih = self._working.size
        side = min(iw, ih)
        thumb_base = self._working.crop(
            ((iw - side) // 2, (ih - side) // 2,
             (iw + side) // 2, (ih + side) // 2)
        ).resize((64, 64), PILImage.BILINEAR).convert("RGB")
        # "Ohne Rahmen" button as first option
        none_tex = _pil_to_texture(thumb_base.copy())
        none_pic = Gtk.Picture()
        none_pic.set_paintable(none_tex)
        none_pic.set_size_request(60, 60)
        none_pic.set_content_fit(Gtk.ContentFit.FILL)
        none_lbl = Gtk.Label(label="No frame")
        none_lbl.add_css_class("caption")
        none_lbl.set_max_width_chars(9)
        none_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        none_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        none_inner.set_margin_top(3); none_inner.set_margin_bottom(3)
        none_inner.set_margin_start(2); none_inner.set_margin_end(2)
        none_inner.append(none_pic)
        none_inner.append(none_lbl)
        none_btn = Gtk.ToggleButton()
        none_btn.add_css_class("flat")
        none_btn.set_child(none_inner)
        none_btn.set_active(True)
        none_hid = none_btn.connect("toggled", self._on_frame_toggled, None)
        self._frame_btns["none"] = none_btn
        self._frame_hids["none"] = none_hid
        rahmen_row.append(none_btn)
        for theme_key, theme_label, _c1, _c2 in _FRAME_THEMES:
            preview = thumb_base.copy().convert("RGBA")
            f = _frame_pil(64, 64, theme_key)
            if f:
                preview = PILImage.alpha_composite(preview, f)
            tex = _pil_to_texture(preview)
            pic = Gtk.Picture()
            pic.set_paintable(tex)
            pic.set_size_request(60, 60)
            pic.set_content_fit(Gtk.ContentFit.FILL)
            lbl = Gtk.Label(label=self._(theme_label))
            lbl.add_css_class("caption")
            lbl.set_max_width_chars(9)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.set_margin_top(3); inner.set_margin_bottom(3)
            inner.set_margin_start(2); inner.set_margin_end(2)
            inner.append(pic)
            inner.append(lbl)
            fbtn = Gtk.ToggleButton()
            fbtn.add_css_class("flat")
            fbtn.set_child(inner)
            hid = fbtn.connect("toggled", self._on_frame_toggled, theme_key)
            self._frame_btns[theme_key] = fbtn
            self._frame_hids[theme_key] = hid
            rahmen_row.append(fbtn)
        rahmen_scroll.set_child(rahmen_row)
        self._sticker_sub_stack.add_named(rahmen_scroll, "rahmen")

        # Text page (entry + color row + add button)
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        text_box.set_margin_start(10); text_box.set_margin_end(10)
        text_box.set_margin_top(8);    text_box.set_margin_bottom(8)
        self._text_entry = Gtk.Entry()
        self._text_entry.set_placeholder_text("Text input…")
        self._text_entry.set_hexpand(True)
        text_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        color = Gdk.RGBA()
        color.red = 1.0
        color.green = 1.0
        color.blue = 1.0
        color.alpha = 1.0
        self._text_color_button = Gtk.ColorButton.new_with_rgba(color)
        self._text_color_button.set_tooltip_text("Text color")
        self._text_color_button.connect("color-set", self._on_text_color_set)
        add_btn = Gtk.Button(label="Add")
        add_btn.add_css_class("flat")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_text_add)
        text_row.append(self._text_entry)
        text_row.append(self._text_color_button)
        text_box.append(text_row)
        text_box.append(add_btn)
        self._sticker_sub_stack.add_named(text_box, "text")

        self._sticker_sub_revealer = Gtk.Revealer()
        self._sticker_sub_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._sticker_sub_revealer.set_transition_duration(150)
        self._sticker_sub_revealer.set_reveal_child(False)
        self._sticker_sub_revealer.set_child(self._sticker_sub_stack)
        outer.append(self._sticker_sub_revealer)

        # ── Category nav (5 buttons) ──
        cat_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        cat_box.set_hexpand(True)
        cat_box.set_margin_top(4)
        cat_box.set_margin_bottom(4)

        self._sticker_cat_btns: dict[str, Gtk.ToggleButton] = {}
        self._sticker_cat_hids: dict[str, int] = {}
        for key, label in [("smileys", "Smileys"), ("emotions", "Emotions"),
                            ("symbole", "Symbole"), ("rahmen", "Rahmen"), ("text", "Text")]:
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("flat")
            btn.set_hexpand(True)
            hid = btn.connect("toggled", self._on_sticker_cat_toggled, key)
            self._sticker_cat_hids[key] = hid
            cat_box.append(btn)
            self._sticker_cat_btns[key] = btn

        outer.append(cat_box)
        return outer

    def _on_sticker_cat_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active():
            for k, b in self._sticker_cat_btns.items():
                if k != key and b.get_active():
                    b.handler_block(self._sticker_cat_hids[k])
                    b.set_active(False)
                    b.handler_unblock(self._sticker_cat_hids[k])
            self._sticker_sub_stack.set_visible_child_name(key)
            self._sticker_sub_revealer.set_reveal_child(True)
        else:
            self._sticker_sub_revealer.set_reveal_child(False)

    def _on_frame_toggled(self, btn: Gtk.ToggleButton, theme) -> None:
        if btn.get_active():
            for k, b in self._frame_btns.items():
                if k != (theme or "none") and b.get_active():
                    b.handler_block(self._frame_hids[k])
                    b.set_active(False)
                    b.handler_unblock(self._frame_hids[k])
            self._frame_theme = theme
        else:
            # Don't allow deselecting — re-activate "No frame" instead
            none_btn = self._frame_btns.get("none")
            if none_btn and not none_btn.get_active():
                none_btn.handler_block(self._frame_hids["none"])
                none_btn.set_active(True)
                none_btn.handler_unblock(self._frame_hids["none"])
            self._frame_theme = None
        self._schedule_update()

    def _on_text_color_set(self, button: Gtk.ColorButton) -> None:
        rgba = button.get_rgba()
        self._text_color = (
            int(rgba.red * 255),
            int(rgba.green * 255),
            int(rgba.blue * 255),
        )

    def _on_text_add(self, _btn: Gtk.Button) -> None:
        text = self._text_entry.get_text().strip()
        if not text:
            return
        try:
            pil = _make_text_pil(text, 60, self._text_color)
            self._set_sticker(pil)
        except Exception:
            pass

    def _build_panel_crop(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        self._crop_btn = Gtk.ToggleButton(label="✂  Crop")
        self._crop_btn.add_css_class("flat")
        self._crop_btn.connect("toggled", self._on_crop_toggled)
        self._crop_apply_btn = Gtk.Button(label="Apply")
        self._crop_apply_btn.add_css_class("flat")
        self._crop_apply_btn.set_sensitive(False)
        self._crop_apply_btn.connect("clicked", self._apply_crop)
        self._crop_reset_btn = Gtk.Button(label="Reset")
        self._crop_reset_btn.add_css_class("flat")
        self._crop_reset_btn.connect("clicked", self._reset_working)
        box.append(self._crop_btn)
        box.append(self._crop_apply_btn)
        box.append(self._crop_reset_btn)
        return box

    def _build_panel_effects(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        obf_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._obfuscate_btn = Gtk.ToggleButton(label="✎  Verwischen")
        self._obfuscate_btn.set_hexpand(True)
        self._obfuscate_btn.connect("toggled", self._on_obfuscate_toggled)
        obf_reset = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        obf_reset.add_css_class("flat")
        obf_reset.set_tooltip_text("Pinselstriche zurücksetzen")
        obf_reset.connect("clicked", self._reset_obfuscate)
        obf_row.append(self._obfuscate_btn)
        obf_row.append(obf_reset)
        box.append(obf_row)
        size_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        size_lbl = Gtk.Label(label="Pinselgröße", xalign=0)
        size_lbl.set_size_request(100, -1)
        self._obfuscate_size_sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.02, 0.25, 0.01)
        self._obfuscate_size_sc.set_value(self._obfuscate_brush_size)
        self._obfuscate_size_sc.set_hexpand(True)
        self._obfuscate_size_sc.set_draw_value(False)
        self._obfuscate_size_sc.connect("value-changed", self._on_obfuscate_size_changed)
        size_row.append(size_lbl)
        size_row.append(self._obfuscate_size_sc)
        box.append(size_row)
        return box

    # ------------------------------------------------------------------
    # Nav toggle
    # ------------------------------------------------------------------

    def _on_nav_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active():
            for k, b in self._nav_btns.items():
                if k != key and b.get_active():
                    b.handler_block(self._nav_handler_ids[k])
                    b.set_active(False)
                    b.handler_unblock(self._nav_handler_ids[k])
            if key not in ("crop", "effects"):
                self._deactivate_crop()
            if key != "effects":
                self._deactivate_obfuscate()
            self._active_panel = key
            self._panel_stack.set_visible_child_name(key)
            self._panel_revealer.set_reveal_child(True)
            if key == "sticker":
                active_cat = next(
                    (k for k, b in self._sticker_cat_btns.items() if b.get_active()),
                    "smileys",
                )
                self._sticker_sub_stack.set_visible_child_name(active_cat)
                self._sticker_sub_revealer.set_reveal_child(True)
                if not self._sticker_cat_btns[active_cat].get_active():
                    sticker_btn = self._sticker_cat_btns[active_cat]
                    sticker_btn.handler_block(self._sticker_cat_hids[active_cat])
                    sticker_btn.set_active(True)
                    sticker_btn.handler_unblock(self._sticker_cat_hids[active_cat])
        else:
            self._active_panel = None
            self._panel_revealer.set_reveal_child(False)
            if key == "crop":
                self._deactivate_crop()
            if key == "effects":
                self._deactivate_obfuscate()

    def _deactivate_crop(self) -> None:
        self._crop_mode = False
        if hasattr(self, "_crop_btn"):
            self._crop_btn.handler_block_by_func(self._on_crop_toggled)
            self._crop_btn.set_active(False)
            self._crop_btn.handler_unblock_by_func(self._on_crop_toggled)
        self._crop_start = self._crop_current = None
        self._crop_rect_disp = None
        self._crop_active_handle = None
        self._draw_area.queue_draw()

    def _deactivate_obfuscate(self) -> None:
        self._obfuscate_mode = False
        self._obfuscate_drag_origin = None
        if hasattr(self, "_obfuscate_btn"):
            self._obfuscate_btn.handler_block_by_func(self._on_obfuscate_toggled)
            self._obfuscate_btn.set_active(False)
            self._obfuscate_btn.handler_unblock_by_func(self._on_obfuscate_toggled)
        self._draw_area.queue_draw()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_filter_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if not btn.get_active():
            return
        self._snapshot_state()  # Save state before filter change
        self._filter_mode = mode
        for m, b in self._filter_btns.items():
            if m != mode:
                b.set_active(False)
        self._schedule_update()

    def _on_crop_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._crop_mode = btn.get_active()
        if not self._crop_mode:
            self._crop_start = self._crop_current = None
            self._draw_area.queue_draw()

    def _apply_crop(self, _btn: Gtk.Button) -> None:
        if self._pending_crop:
            self._snapshot_state()  # Save state before crop
            self._working = self._working.crop(self._pending_crop)
            self._pending_crop = None
            self._crop_start = self._crop_current = None
            self._crop_rect_disp = None
            self._crop_active_handle = None
            self._crop_btn.set_active(False)
            self._crop_apply_btn.set_sensitive(False)
            self._schedule_update()

    def _reset_working(self, _btn: Gtk.Button) -> None:
        self._snapshot_state()  # Save current state before full reset
        self._working = self._original.copy()
        self._pending_crop = None
        self._crop_start = self._crop_current = None
        self._crop_rect_disp = None
        self._crop_active_handle = None
        self._crop_btn.set_active(False)
        self._crop_apply_btn.set_sensitive(False)
        self._schedule_update()

    def _on_sticker_clicked(self, _btn: Gtk.Button, source) -> None:
        self._set_sticker(source)

    def _set_sticker(self, source: "str | PILImage.Image | None") -> None:
        self._snapshot_state()  # Save state before sticker change
        self._sticker_source = source
        if source is None:
            if self._active_sticker is not None and 0 <= self._active_sticker < len(self._stickers):
                self._stickers.pop(self._active_sticker)
            self._active_sticker = len(self._stickers) - 1 if self._stickers else None
        else:
            self._stickers.append({"source": source, "rel": (0.5, 0.5), "size": 0.15})
            self._active_sticker = len(self._stickers) - 1
        self._sync_active_sticker()
        self._schedule_update()

    def _on_slider(self, sc: Gtk.Scale, attr: str) -> None:
        setattr(self, attr, sc.get_value())
        self._schedule_update()
        self._schedule_slider_snapshot()

    def _schedule_slider_snapshot(self) -> None:
        """Schedule a debounced snapshot for slider changes (500ms delay)."""
        if self._slider_snapshot_id is not None:
            GLib.source_remove(self._slider_snapshot_id)
        self._slider_snapshot_id = GLib.timeout_add(500, self._slider_snapshot_cb)

    def _slider_snapshot_cb(self) -> bool:
        """Called after slider movement stops (debounce callback)."""
        self._slider_snapshot_id = None
        self._snapshot_state()
        return GLib.SOURCE_REMOVE

    def _reset_slider(self, _button: Gtk.Button, attr: str, scale: Gtk.Scale) -> None:
        self._snapshot_state()  # Save state before reset
        setattr(self, attr, 1.0)
        scale.set_value(1.0)
        self._schedule_update()

    def _sync_active_sticker(self) -> None:
        if self._active_sticker is None or not (0 <= self._active_sticker < len(self._stickers)):
            self._sticker_source = None
            self._sticker_rel = (0.5, 0.5)
            self._sticker_size_frac = 0.15
            self._sticker_del_rect = None
            return
        sticker = self._stickers[self._active_sticker]
        self._sticker_source = sticker["source"]
        self._sticker_rel = sticker["rel"]
        self._sticker_size_frac = sticker["size"]
        self._sticker_del_rect = None

    def _store_active_sticker(self) -> None:
        if self._active_sticker is None or not (0 <= self._active_sticker < len(self._stickers)):
            return
        self._stickers[self._active_sticker]["rel"] = self._sticker_rel
        self._stickers[self._active_sticker]["size"] = self._sticker_size_frac

    # ── Sticker zoom (pinch to resize) ──

    def _on_sticker_zoom_begin(self, _g: Gtk.GestureZoom, _seq) -> None:
        self._sticker_zoom_start = self._sticker_size_frac

    def _on_sticker_zoom_scale(self, _g: Gtk.GestureZoom, scale_delta: float) -> None:
        if self._sticker_source is None:
            return
        self._sticker_size_frac = max(0.04, min(0.9, self._sticker_zoom_start * scale_delta))
        self._store_active_sticker()
        self._schedule_update()

    # ── Sticker delete (click on X badge) ──

    def _on_overlay_click(self, _g: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        if self._sticker_del_rect is None:
            return
        x1, y1, x2, y2 = self._sticker_del_rect
        if x1 <= x <= x2 and y1 <= y <= y2:
            self._set_sticker(None)

    # ------------------------------------------------------------------
    # Drag (crop rect + sticker move)
    # ------------------------------------------------------------------

    def _hit_crop_handle(self, x: float, y: float) -> str | None:
        if not self._crop_rect_disp:
            return None
        x1, y1, x2, y2 = self._crop_rect_disp
        for name, (cx, cy) in [("tl", (x1, y1)), ("tr", (x2, y1)),
                                ("bl", (x1, y2)), ("br", (x2, y2))]:
            if (x - cx) ** 2 + (y - cy) ** 2 <= 22 ** 2:
                return name
        return None

    def _on_drag_begin(self, _g: Gtk.GestureDrag, x: float, y: float) -> None:
        self._drag_sticker = False
        if self._obfuscate_mode:
            self._obfuscate_drag_origin = (x, y)
            self._add_obfuscate_stroke(x, y)
            return
        if self._crop_mode:
            handle = self._hit_crop_handle(x, y)
            if handle:
                self._crop_active_handle = handle
                self._crop_handle_orig = self._crop_rect_disp
                self._crop_start = None
            else:
                self._crop_active_handle = None
                self._crop_handle_orig = None
                self._crop_rect_disp = None
                self._pending_crop = None
                self._crop_apply_btn.set_sensitive(False)
                self._crop_start = (x, y)
                self._crop_current = (x, y)
        elif self._stickers:
            self._drag_sticker = True
            self._active_sticker = len(self._stickers) - 1
            self._sync_active_sticker()
            self._drag_sx, self._drag_sy = x, y

    def _on_drag_update(self, _g: Gtk.GestureDrag, ox: float, oy: float) -> None:
        if self._obfuscate_mode and self._obfuscate_drag_origin is not None:
            bx, by = self._obfuscate_drag_origin
            self._add_obfuscate_stroke(bx + ox, by + oy)
            return
        if self._crop_mode:
            if self._crop_active_handle and self._crop_handle_orig:
                x1, y1, x2, y2 = self._crop_handle_orig
                h = self._crop_active_handle
                if h == "tl":   x1 += ox; y1 += oy
                elif h == "tr": x2 += ox; y1 += oy
                elif h == "bl": x1 += ox; y2 += oy
                elif h == "br": x2 += ox; y2 += oy
                if abs(x2 - x1) > 16 and abs(y2 - y1) > 16:
                    self._crop_rect_disp = (min(x1, x2), min(y1, y2),
                                            max(x1, x2), max(y1, y2))
                self._draw_area.queue_draw()
            elif self._crop_start:
                sx, sy = self._crop_start
                self._crop_current = (sx + ox, sy + oy)
                self._draw_area.queue_draw()
        elif self._drag_sticker:
            self._move_sticker(self._drag_sx + ox, self._drag_sy + oy)

    def _on_drag_end(self, _g: Gtk.GestureDrag, ox: float, oy: float) -> None:
        if self._obfuscate_mode:
            self._obfuscate_drag_origin = None
            self._schedule_update()
            return
        if self._crop_mode:
            if self._crop_active_handle and self._crop_rect_disp:
                rect = self._display_to_image(*self._crop_rect_disp)
                if rect and (rect[2] - rect[0]) > 8 and (rect[3] - rect[1]) > 8:
                    self._pending_crop = rect
                    self._crop_apply_btn.set_sensitive(True)
                self._crop_active_handle = None
                self._crop_handle_orig = None
            elif self._crop_start:
                sx, sy = self._crop_start
                fx, fy = sx + ox, sy + oy
                x1, y1 = min(sx, fx), min(sy, fy)
                x2, y2 = max(sx, fx), max(sy, fy)
                if (x2 - x1) > 8 and (y2 - y1) > 8:
                    self._crop_rect_disp = (x1, y1, x2, y2)
                    rect = self._display_to_image(x1, y1, x2, y2)
                    if rect:
                        self._pending_crop = rect
                        self._crop_apply_btn.set_sensitive(True)
                self._crop_start = self._crop_current = None
            self._draw_area.queue_draw()
        self._drag_sticker = False

    def _move_sticker(self, px: float, py: float) -> None:
        dw = self._draw_area.get_width()
        dh = self._draw_area.get_height()
        iw, ih = self._working.size
        if dw <= 0 or dh <= 0:
            return
        scale = min(dw / iw, dh / ih)
        ox = (dw - iw * scale) / 2
        oy = (dh - ih * scale) / 2
        self._sticker_rel = (
            max(0.0, min(1.0, (px - ox) / (iw * scale))),
            max(0.0, min(1.0, (py - oy) / (ih * scale))),
        )
        self._store_active_sticker()
        self._schedule_update()

    def _sample_color_at(self, img: "PILImage.Image", rel_x: float, rel_y: float, sample_radius: int = 10) -> tuple[float, float, float, float]:
        """Sample average color from a region around a point. Returns (r, g, b, a) in 0-1 range."""
        iw, ih = img.size
        cx = int(rel_x * iw)
        cy = int(rel_y * ih)
        x1 = max(0, cx - sample_radius)
        y1 = max(0, cy - sample_radius)
        x2 = min(iw, cx + sample_radius + 1)
        y2 = min(ih, cy + sample_radius + 1)
        
        if x2 <= x1 or y2 <= y1:
            return (0.5, 0.5, 0.5, 0.3)  # Default gray if invalid region
        
        try:
            region = img.crop((x1, y1, x2, y2))
            # Convert to RGB if needed
            if region.mode != "RGB":
                region = region.convert("RGB")
            # Get average color
            import numpy as np
            arr = np.array(region, dtype=np.float32)
            avg = np.mean(arr, axis=(0, 1))
            r, g, b = avg / 255.0
            return (r, g, b, 0.35)  # Use slight transparency
        except Exception:
            return (0.5, 0.5, 0.5, 0.3)

    def _add_obfuscate_stroke(self, px: float, py: float) -> None:
        dw = self._draw_area.get_width()
        dh = self._draw_area.get_height()
        iw, ih = self._working.size
        if dw <= 0 or dh <= 0:
            return
        scale = min(dw / iw, dh / ih)
        ox = (dw - iw * scale) / 2
        oy = (dh - ih * scale) / 2
        rel_x = (px - ox) / (iw * scale)
        rel_y = (py - oy) / (ih * scale)
        # Sample the color from the underlying image
        color = self._sample_color_at(self._working, rel_x, rel_y)
        self._obfuscate_strokes.append((rel_x, rel_y, self._obfuscate_brush_size, color))
        self._draw_area.queue_draw()

    def _on_obfuscate_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._obfuscate_mode = btn.get_active()
        self._draw_area.queue_draw()

    def _on_obfuscate_size_changed(self, sc: Gtk.Scale) -> None:
        self._obfuscate_brush_size = sc.get_value()

    def _reset_obfuscate(self, _btn: Gtk.Button) -> None:
        self._snapshot_state()  # Save state before clearing obfuscate
        self._obfuscate_strokes = []
        self._schedule_update()

    def _display_to_image(
        self, x1: float, y1: float, x2: float, y2: float
    ) -> tuple[int, int, int, int] | None:
        dw = self._draw_area.get_width()
        dh = self._draw_area.get_height()
        iw, ih = self._working.size
        if dw <= 0 or dh <= 0:
            return None
        scale = min(dw / iw, dh / ih)
        ox = (dw - iw * scale) / 2
        oy = (dh - ih * scale) / 2

        def clip(v: float, lo: int, hi: int) -> int:
            return max(lo, min(hi, int(v)))

        return (
            clip((min(x1, x2) - ox) / scale, 0, iw),
            clip((min(y1, y2) - oy) / scale, 0, ih),
            clip((max(x1, x2) - ox) / scale, 0, iw),
            clip((max(y1, y2) - oy) / scale, 0, ih),
        )

    # ------------------------------------------------------------------
    # Overlay drawing (cairo)
    # ------------------------------------------------------------------

    def _draw_crop_rect(self, cr: cairo.Context, width: int, height: int,
                        rx: float, ry: float, rw: float, rh: float,
                        handles: bool) -> None:
        cr.set_source_rgba(0, 0, 0, 0.50)
        cr.rectangle(0, 0, width, height)
        cr.rectangle(rx, ry, rw, rh)
        cr.set_fill_rule(cairo.FillRule.EVEN_ODD)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.92)
        cr.set_line_width(1.5)
        cr.set_dash([])
        cr.rectangle(rx, ry, rw, rh)
        cr.stroke()
        if handles:
            for cx, cy in [(rx, ry), (rx + rw, ry), (rx, ry + rh), (rx + rw, ry + rh)]:
                cr.set_source_rgba(1, 1, 1, 0.96)
                cr.arc(cx, cy, 9.0, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(0.15, 0.15, 0.15, 0.70)
                cr.arc(cx, cy, 9.0, 0, 2 * math.pi)
                cr.set_line_width(1.5)
                cr.stroke()

    def _draw_overlay(self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int) -> None:
        if self._crop_mode:
            if self._crop_start and self._crop_current:
                x1, y1 = self._crop_start
                x2, y2 = self._crop_current
                self._draw_crop_rect(cr, width, height, min(x1, x2), min(y1, y2),
                                     abs(x2 - x1), abs(y2 - y1), handles=False)
            elif self._crop_rect_disp:
                x1, y1, x2, y2 = self._crop_rect_disp
                self._draw_crop_rect(cr, width, height, x1, y1, x2 - x1, y2 - y1, handles=True)

        if self._stickers and not self._crop_mode:
            iw, ih = self._working.size
            scale = min(width / iw, height / ih)
            ox = (width - iw * scale) / 2
            oy = (height - ih * scale) / 2
            active = self._stickers[self._active_sticker or len(self._stickers) - 1]
            rel = active["rel"]
            size = active["size"]
            scx = ox + rel[0] * iw * scale
            scy = oy + rel[1] * ih * scale
            half = size * iw * scale / 2
            # Delete button: red circle with × at top-right corner of sticker
            R = 12.0
            del_cx = scx + half
            del_cy = scy - half
            self._sticker_del_rect = (del_cx - R, del_cy - R, del_cx + R, del_cy + R)
            cr.set_source_rgba(0.88, 0.18, 0.18, 0.90)
            cr.arc(del_cx, del_cy, R, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.set_line_width(2.0)
            d = 5.0
            cr.move_to(del_cx - d, del_cy - d)
            cr.line_to(del_cx + d, del_cy + d)
            cr.move_to(del_cx + d, del_cy - d)
            cr.line_to(del_cx - d, del_cy + d)
            cr.stroke()

        if self._obfuscate_strokes and width > 0 and height > 0:
            iw, ih = self._working.size
            scale = min(width / iw, height / ih)
            ox = (width - iw * scale) / 2
            oy = (height - ih * scale) / 2
            for rel_x, rel_y, rel_r, color in self._obfuscate_strokes:
                cx = ox + rel_x * iw * scale
                cy = oy + rel_y * ih * scale
                r = max(4.0, rel_r * min(iw, ih) * scale)
                cr.set_source_rgba(*color)  # Use sampled color
                cr.arc(cx, cy, r, 0, 2 * math.pi)
                cr.fill()

    # ------------------------------------------------------------------
    # Image processing
    # ------------------------------------------------------------------

    def _apply_edits(self, img: "PILImage.Image") -> "PILImage.Image":
        result = img.convert("RGB")
        fn = next((f for k, _l, f in _FILTER_DEFS if k == self._filter_mode and f), None)
        if fn:
            result = fn(result)
        result = ImageEnhance.Brightness(result).enhance(self._brightness)
        result = ImageEnhance.Contrast(result).enhance(self._contrast)
        if not (self._red == self._green == self._blue == 1.0):
            r, g, b = result.split()
            r = r.point(lambda x: min(255, int(x * self._red)))
            g = g.point(lambda x: min(255, int(x * self._green)))
            b = b.point(lambda x: min(255, int(x * self._blue)))
            result = PILImage.merge("RGB", (r, g, b))
        if self._stickers:
            iw, ih = result.size
            canvas = PILImage.new("RGBA", (iw, ih), (0, 0, 0, 0))
            for sticker in self._stickers:
                px = max(32, int(sticker["size"] * iw))
                source = sticker["source"]
                if isinstance(source, str):
                    si = _get_emoji_pil(source, px)
                else:
                    si = source.resize((px, int(px * source.height / max(1, source.width))), PILImage.LANCZOS)
                rel = sticker["rel"]
                sx = int(rel[0] * iw - si.width / 2)
                sy = int(rel[1] * ih - si.height / 2)
                canvas.paste(si, (sx, sy), si if si.mode == "RGBA" else None)
            result = PILImage.alpha_composite(result.convert("RGBA"), canvas).convert("RGB")
        if self._obfuscate_strokes:
            iw2, ih2 = result.size
            blurred = result.filter(ImageFilter.GaussianBlur(radius=max(4, iw2 // 40)))
            obf_mask = PILImage.new("L", (iw2, ih2), 0)
            obf_draw = ImageDraw.Draw(obf_mask)
            for rel_x, rel_y, rel_r, _color in self._obfuscate_strokes:  # Ignore color tuple in processing
                cx = int(rel_x * iw2)
                cy = int(rel_y * ih2)
                r = max(4, int(rel_r * min(iw2, ih2)))
                obf_draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
            obf_mask = obf_mask.filter(ImageFilter.GaussianBlur(radius=6))
            result = PILImage.composite(blurred.convert("RGB"), result.convert("RGB"), obf_mask)
        if self._frame_theme is not None:
            iw, ih = result.size
            frame = _frame_pil(iw, ih, self._frame_theme)
            if frame:
                result = PILImage.alpha_composite(result.convert("RGBA"), frame).convert("RGB")
        return result

    def _schedule_update(self) -> None:
        if self._update_id is not None:
            GLib.source_remove(self._update_id)
        self._update_id = GLib.timeout_add(90, self._do_update)

    def _do_update(self) -> bool:
        self._update_id = None
        pw = self._preview.get_width() or 800
        ph = self._preview.get_height() or 600
        # Downscale working copy first so _apply_edits operates on a small image
        thumb = self._working.copy()
        thumb.thumbnail((pw * 2, ph * 2), PILImage.BILINEAR)
        img = self._apply_edits(thumb)
        self._preview.set_paintable(_pil_to_texture(img))
        self._draw_area.queue_draw()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Undo/Redo
    # ------------------------------------------------------------------

    def _snapshot_state(self) -> None:
        """Save current working image to undo stack (call after each edit)."""
        # Clear redo stack when new edit is made
        self._history_redo.clear()
        # Save current state to undo stack
        self._history_undo.append(self._working.copy())
        # Limit history size to prevent memory bloat
        if len(self._history_undo) > self._history_max_steps:
            self._history_undo.pop(0)

    def can_undo(self) -> bool:
        """Check if undo is available."""
        return len(self._history_undo) > 0

    def can_redo(self) -> bool:
        """Check if redo is available."""
        return len(self._history_redo) > 0

    def undo(self) -> None:
        """Undo last edit."""
        if not self.can_undo():
            return
        # Save current state to redo stack
        self._history_redo.append(self._working.copy())
        # Restore previous state
        self._working = self._history_undo.pop()
        # Trigger preview update
        self._schedule_update()

    def redo(self) -> None:
        """Redo last undone edit."""
        if not self.can_redo():
            return
        # Save current state to undo stack
        self._history_undo.append(self._working.copy())
        # Restore next state
        self._working = self._history_redo.pop()
        # Trigger preview update
        self._schedule_update()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_as_new(self) -> str:
        """Save edited image to local filesystem."""
        result = self._apply_edits(self._working)
        orig = Path(self._item.path)
        ext = orig.suffix.lower()
        if ext in (".heic", ".heif", ".avif"):
            ext = ".jpg"
        i = 1
        while True:
            dest = orig.parent / f"{orig.stem}_edit_{i}{ext}"
            if not dest.exists():
                break
            i += 1
        if ext in (".jpg", ".jpeg"):
            result.convert("RGB").save(str(dest), "JPEG", quality=95)
        elif ext == ".png":
            result.save(str(dest), "PNG")
        else:
            dest = orig.parent / f"{orig.stem}_edit_{i}.jpg"
            result.convert("RGB").save(str(dest), "JPEG", quality=95)
        return str(dest)

    def upload_to_nextcloud(self, local_edited_path: str, nextcloud_client) -> bool:
        """
        Upload edited local image back to Nextcloud at the original path.
        Used for cloud-sync workflow.
        """
        from .nextcloud import dav_path_from_nc, NC_PATH_PREFIX
        
        # Extract DAV path from the original Nextcloud item
        if not self._item.path.startswith(NC_PATH_PREFIX):
            return False
        
        original_dav_path = dav_path_from_nc(self._item.path)
        
        # Replace file extension if needed (e.g., HEIC → JPG)
        import os
        original_ext = os.path.splitext(original_dav_path)[0]
        edited_path = Path(local_edited_path)
        
        # Construct the upload path with the same name as original
        upload_dav_path = original_ext + edited_path.suffix
        
        try:
            return nextcloud_client.upload_file(local_edited_path, upload_dav_path)
        except Exception as exc:
            LOGGER.exception("Upload to Nextcloud failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Settings window
