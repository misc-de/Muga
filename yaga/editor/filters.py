"""Color/tone filter presets. Each takes and returns a PIL RGB Image."""

from __future__ import annotations

from ._pil import ImageEnhance, ImageOps, PILImage


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


# Filter table consumed by the editor's filter panel — drives the toggle row.
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
