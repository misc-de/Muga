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
    # Hard cap on accepted pixel count. Pillow only emits a *warning* by
    # default (≈89 MP) and continues to allocate; a malicious file from a
    # compromised Nextcloud server or another user dropping into a scanned
    # folder could trivially OOM the gallery process.
    # 200 MP comfortably covers 108 MP phone cameras and medium-format
    # bodies; anything beyond 2× this raises Image.DecompressionBombError
    # which call sites catch and degrade gracefully.
    PILImage.MAX_IMAGE_PIXELS = 200_000_000
    _PIL_OK = True
except ImportError:
    PILImage = ImageEnhance = ImageFilter = ImageOps = ImageDraw = None  # type: ignore[assignment]
    _PIL_OK = False
