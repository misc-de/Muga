"""In-app photo editor.

This used to be a single 1700-line ``yaga/editor.py``; the pure-Pillow image
operations now live in dedicated submodules and the GTK widget in
``view.py``. The historic flat-import surface is preserved by re-exporting
from here, so ``from yaga.editor import EditorView, _frame_pil, …`` keeps
working everywhere it was used before.
"""

from __future__ import annotations

from ._pil import (
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageOps,
    PILImage,
    _PIL_OK,
)
from .filters import (
    _FILTER_DEFS,
    _filter_bw,
    _filter_cool,
    _filter_dramatic,
    _filter_fade,
    _filter_invert,
    _filter_sepia,
    _filter_vintage,
    _filter_warm,
)
from .frames import (
    _FRAME_THEMES,
    _decorate_birthday,
    _decorate_christmas,
    _decorate_easter,
    _decorate_new_year,
    _decorate_spring,
    _decorate_summer,
    _decorate_wedding,
    _decorate_winter,
    _draw_bow,
    _draw_flower,
    _draw_gift,
    _draw_leaf,
    _draw_palm,
    _draw_snowflake,
    _draw_soft_border,
    _draw_star_shape,
    _edge_positions,
    _frame_pil,
)
from .stickers import (
    _EMOJI_PIL_CACHE,
    _STICKER_GROUPS,
    _emoji_to_pil,
    _get_emoji_pil,
    _make_heart,
    _make_sparkle,
    _make_star,
    _pil_to_texture,
)
from .text import _make_text_pil
from .view import EditorView

__all__ = [
    "EditorView",
    "_PIL_OK",
    "PILImage",
    "_FRAME_THEMES",
    "_frame_pil",
]
