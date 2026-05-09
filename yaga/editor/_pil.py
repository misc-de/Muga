"""Single source of truth for the optional Pillow import.

Pillow is a soft dependency: the rest of the app degrades gracefully when
it's missing. Submodules of yaga.editor import their PIL symbols from here
so the import-failure path lives in exactly one place.
"""

from __future__ import annotations

try:
    from PIL import (
        Image as PILImage,
        ImageDraw,
        ImageEnhance,
        ImageFilter,
        ImageOps,
    )
    _PIL_OK = True
except ImportError:
    PILImage = ImageEnhance = ImageFilter = ImageOps = ImageDraw = None  # type: ignore[assignment]
    _PIL_OK = False
