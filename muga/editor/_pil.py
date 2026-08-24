"""Single source of truth for the optional Pillow import.

Pillow is a soft dependency: the rest of the app degrades gracefully when
it's missing. Submodules of muga.editor import their PIL symbols from here
so the import-failure path lives in exactly one place.
"""

from __future__ import annotations

import warnings

try:
    from PIL import (
        Image as PILImage,
        ImageDraw,
        ImageEnhance,
        ImageFilter,
        ImageOps,
        ImageStat,
    )
    # Hard cap on accepted pixel count. A malicious file from a compromised
    # Nextcloud server, or one dropped into a scanned folder by someone else,
    # could otherwise OOM the gallery process.
    #
    # 120 MP covers 108 MP phone sensors and medium-format bodies with room to
    # spare. The number alone is not the protection, though: Pillow only
    # *warns* above MAX_IMAGE_PIXELS and keeps allocating, and raises
    # DecompressionBombError only past 2×. With the old 200 MP cap that meant
    # a 288 MP image sailed through and allocated ~0.9 GB — on a 4 GB phone
    # the OOM killer arrives long before the error does. Promoting the warning
    # to an exception makes the cap mean what it says, and bounds a decode at
    # ~360 MB for RGB.
    from ..thumbnails import MAX_IMAGE_PIXELS

    PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    warnings.filterwarnings("error", category=PILImage.DecompressionBombWarning)
    _PIL_OK = True
except ImportError:
    PILImage = ImageEnhance = ImageFilter = ImageOps = ImageDraw = ImageStat = None  # type: ignore[assignment]
    _PIL_OK = False
