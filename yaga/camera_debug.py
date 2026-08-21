"""Diagnostic logging shared by the camera modules.

Its own module so the capture/EXIF and pipeline halves of the camera can both
reach it without importing each other.
"""

from __future__ import annotations

import logging
import os

LOGGER = logging.getLogger("yaga.camera")

# Camera bring-up on Halium devices is hard to observe after the fact, so the
# pipeline steps log verbosely. Mirroring to stderr is opt-in: on a desktop the
# messages would just spam the journal.
CAMERA_DEBUG = bool(os.environ.get("YAGA_CAMERA_DEBUG"))


def dlog(message: str) -> None:
    """Diagnostic log.

    Goes through LOGGER.info so it lands in journald via Adw's logging, and
    additionally mirrors to stderr when YAGA_CAMERA_DEBUG is set — useful when
    running Yaga from a phone terminal where logger output may not be visible.
    """
    LOGGER.info(message)
    if CAMERA_DEBUG:
        print(message)
