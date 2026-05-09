"""Decorative seasonal/occasional frame overlays.

A frame is a transparent RGBA PIL image the same size as the source photo,
ready to be alpha-composited on top. Each theme has a soft border + a set of
hand-drawn decorations (drawn at runtime via PIL primitives — no assets).
"""

from __future__ import annotations

import math

from ._pil import ImageDraw, PILImage


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


def _draw_soft_border(draw: "ImageDraw.ImageDraw", iw: int, ih: int, bw: int, c1: tuple, c2: tuple) -> None:
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


def _draw_star_shape(draw: "ImageDraw.ImageDraw", x: float, y: float, r: float, color: tuple, points: int = 5) -> None:
    coords = []
    for i in range(points * 2):
        angle = math.pi * i / points - math.pi / 2
        radius = r if i % 2 == 0 else r * 0.42
        coords.append((x + math.cos(angle) * radius, y + math.sin(angle) * radius))
    draw.polygon(coords, fill=color)


def _draw_flower(draw: "ImageDraw.ImageDraw", x: float, y: float, r: float, petal: tuple, center: tuple) -> None:
    for angle in range(0, 360, 60):
        dx = math.cos(math.radians(angle)) * r * 0.75
        dy = math.sin(math.radians(angle)) * r * 0.75
        draw.ellipse([x + dx - r * 0.45, y + dy - r * 0.45, x + dx + r * 0.45, y + dy + r * 0.45], fill=petal)
    draw.ellipse([x - r * 0.38, y - r * 0.38, x + r * 0.38, y + r * 0.38], fill=center)


def _draw_snowflake(draw: "ImageDraw.ImageDraw", x: float, y: float, r: float, color: tuple) -> None:
    for angle in range(0, 180, 30):
        dx = math.cos(math.radians(angle)) * r
        dy = math.sin(math.radians(angle)) * r
        draw.line([x - dx, y - dy, x + dx, y + dy], fill=color, width=max(1, int(r // 8)))


def _draw_bow(draw: "ImageDraw.ImageDraw", x: float, y: float, r: float, color: tuple, knot: tuple) -> None:
    draw.polygon([(x, y), (x - r, y - r * 0.55), (x - r, y + r * 0.55)], fill=color)
    draw.polygon([(x, y), (x + r, y - r * 0.55), (x + r, y + r * 0.55)], fill=color)
    draw.ellipse([x - r * 0.24, y - r * 0.24, x + r * 0.24, y + r * 0.24], fill=knot)
    draw.line([x - r * 0.35, y + r * 0.45, x - r * 0.62, y + r * 1.05], fill=color, width=max(2, int(r // 5)))
    draw.line([x + r * 0.35, y + r * 0.45, x + r * 0.62, y + r * 1.05], fill=color, width=max(2, int(r // 5)))


def _draw_leaf(draw: "ImageDraw.ImageDraw", x: float, y: float, r: float, angle: float, color: tuple) -> None:
    dx = math.cos(angle) * r
    dy = math.sin(angle) * r
    x0 = x - dx - r * 0.35
    y0 = y - dy - r * 0.18
    x1 = x + dx + r * 0.35
    y1 = y + dy + r * 0.18
    draw.ellipse([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], fill=color)


def _draw_gift(draw: "ImageDraw.ImageDraw", x: float, y: float, s: float, box: tuple, ribbon: tuple) -> None:
    draw.rounded_rectangle([x - s, y - s * 0.55, x + s, y + s], radius=max(2, int(s // 8)), fill=box)
    draw.rectangle([x - s, y - s * 0.12, x + s, y + s * 0.10], fill=ribbon)
    draw.rectangle([x - s * 0.12, y - s * 0.55, x + s * 0.12, y + s], fill=ribbon)
    _draw_bow(draw, x, y - s * 0.62, s * 0.35, ribbon, (255, 255, 255, 210))


def _draw_palm(draw: "ImageDraw.ImageDraw", x: float, y: float, r: float) -> None:
    trunk = (132, 89, 45, 225)
    leaf = (36, 151, 91, 232)
    draw.line([x, y, x + r * 0.20, y + r * 1.35], fill=trunk, width=max(3, int(r // 6)))
    for angle in [-2.7, -2.25, -1.85, -1.35, -0.95, -0.55]:
        ex = x + math.cos(angle) * r
        ey = y + math.sin(angle) * r * 0.62
        draw.line([x, y, ex, ey], fill=leaf, width=max(3, int(r // 8)))
        _draw_leaf(draw, (x + ex) / 2, (y + ey) / 2, r * 0.24, angle, leaf)


def _decorate_christmas(draw: "ImageDraw.ImageDraw", iw: int, ih: int, bw: int) -> None:
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


def _decorate_new_year(draw: "ImageDraw.ImageDraw", iw: int, ih: int, bw: int) -> None:
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


def _decorate_easter(draw: "ImageDraw.ImageDraw", iw: int, ih: int, bw: int) -> None:
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


def _decorate_wedding(draw: "ImageDraw.ImageDraw", iw: int, ih: int, bw: int) -> None:
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


def _decorate_birthday(draw: "ImageDraw.ImageDraw", iw: int, ih: int, bw: int) -> None:
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


def _decorate_spring(draw: "ImageDraw.ImageDraw", iw: int, ih: int, bw: int) -> None:
    petals = [(255, 177, 213, 235), (255, 229, 119, 235), (184, 222, 112, 235)]
    for i, x in enumerate(_edge_positions(iw, bw, 10)):
        _draw_flower(draw, x, bw * 0.55, bw * 0.18, petals[i % len(petals)], (255, 214, 77, 245))
    for i, y in enumerate(_edge_positions(ih, bw, 8)):
        draw.ellipse([iw - bw * 0.66, y - bw * 0.16, iw - bw * 0.30, y + bw * 0.13], fill=(89, 170, 91, 220))
    for x, y in [(bw * 0.95, ih - bw * 0.85), (iw - bw * 0.95, ih - bw * 0.85)]:
        for k in range(5):
            _draw_flower(draw, x + (k - 2) * bw * 0.22, y - abs(k - 2) * bw * 0.10, bw * 0.24, petals[k % len(petals)], (255, 220, 70, 245))


def _decorate_summer(draw: "ImageDraw.ImageDraw", iw: int, ih: int, bw: int) -> None:
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


def _decorate_winter(draw: "ImageDraw.ImageDraw", iw: int, ih: int, bw: int) -> None:
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
