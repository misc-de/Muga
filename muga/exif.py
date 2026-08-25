"""EXIF extraction, shared by the scanner and the viewer.

This used to live inside ``viewer.py`` as a private helper, which meant EXIF
only ever existed for pictures the user had opened full-screen: the info
popover parsed it and cached it, nothing else did. Search matched against that
cache, so "find the photos from that Canon" answered from whichever handful of
files happened to have been viewed. The scanner needs the same parser to fill
the index for the whole library, so it moved here.

Two things come out of a file:

* ``fields`` — human-readable strings, shown in the info popover and indexed
  for full-text search.
* ``taken_at`` — when the camera says the shot was taken, which is not the
  same as the file's mtime. A photo copied off a card today has today's mtime
  and a capture date from whenever it was shot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)

try:
    from PIL import Image as PILImage
    _PIL_OK = True
except ImportError:  # pragma: no cover - Pillow is a hard dependency
    PILImage = None  # type: ignore[assignment]
    _PIL_OK = False


# Base-IFD tags.
_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_DATETIME = 306
_TAG_EXIF_IFD = 34665
_TAG_GPS_IFD = 34853
# Exif sub-IFD tags. DateTimeOriginal is what the shutter recorded;
# DateTimeDigitized is when it was written, which for a scan of a negative is
# the scanning date — close enough as a fallback, better than the file mtime.
_TAG_DATETIME_ORIGINAL = 36867
_TAG_DATETIME_DIGITIZED = 36868
# GPS sub-IFD tags.
_GPS_LAT_REF, _GPS_LAT = 1, 2
_GPS_LON_REF, _GPS_LON = 3, 4


@dataclass(frozen=True)
class ExifInfo:
    """What one file's EXIF yields. Empty when there is none, or none we read."""

    fields: dict[str, str] = field(default_factory=dict)
    taken_at: float | None = None

    def __bool__(self) -> bool:
        return bool(self.fields) or self.taken_at is not None


def _parse_datetime(raw) -> float | None:
    """Turn an EXIF timestamp into seconds since the epoch, or None.

    EXIF writes ``YYYY:MM:DD HH:MM:SS`` with no timezone — it is wall-clock
    time on the camera. ``mktime`` interprets it in the local zone, which is
    the closest thing to what the photographer saw on the display, and matches
    how the search formats it back.
    """
    if not raw:
        return None
    text = str(raw).strip().rstrip("\x00")
    if not text:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            parsed = time.strptime(text, fmt)
        except ValueError:
            continue
        # A camera with a dead clock writes zeros; that is not a date.
        if parsed.tm_year < 1900:
            return None
        try:
            return time.mktime(parsed)
        except (OverflowError, ValueError):
            return None
    return None


def _parse_coordinate(values, ref: str, negative_ref: str) -> float | None:
    """Degrees/minutes/seconds rationals → signed decimal degrees."""
    try:
        degrees = float(values[0]) + float(values[1]) / 60 + float(values[2]) / 3600
    except (TypeError, IndexError, ValueError, ZeroDivisionError):
        return None
    if str(ref).strip().upper().startswith(negative_ref):
        degrees = -degrees
    return degrees


def extract(path: str | Path) -> ExifInfo:
    """Read the EXIF this app cares about. Never raises.

    Called for every indexed image during a scan, so anything unreadable —
    a truncated file, a format Pillow will not open, a permission error — has
    to degrade to "no EXIF" rather than interrupt the scan.
    """
    if not _PIL_OK or PILImage is None:
        return ExifInfo()
    fields: dict[str, str] = {}
    taken_at: float | None = None
    try:
        with PILImage.open(path) as img:
            exif = img.getexif()
            if not exif:
                return ExifInfo()

            make = str(exif.get(_TAG_MAKE, "") or "").strip()
            model = str(exif.get(_TAG_MODEL, "") or "").strip()
            # Join exactly as the viewer always has. Tempting to collapse the
            # bodies that repeat the maker in the model ("NIKON CORPORATION
            # NIKON D750"), but this move is meant to be behaviour-neutral —
            # changing what the popover shows belongs in its own change.
            camera = f"{make} {model}" if make and model else (model or make)
            if camera:
                fields["Camera"] = camera

            try:
                sub = exif.get_ifd(_TAG_EXIF_IFD)
            except Exception:
                sub = {}
            taken_at = (
                _parse_datetime(sub.get(_TAG_DATETIME_ORIGINAL))
                or _parse_datetime(sub.get(_TAG_DATETIME_DIGITIZED))
                or _parse_datetime(exif.get(_TAG_DATETIME))
            )
            if taken_at is not None:
                # Indexed as text too, so a search for "2019" matches through
                # the same full-text path as a filename would.
                fields["Taken"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(taken_at))

            if _TAG_GPS_IFD in exif:
                try:
                    gps = exif.get_ifd(_TAG_GPS_IFD)
                except Exception:
                    gps = {}
                lat = _parse_coordinate(gps.get(_GPS_LAT), gps.get(_GPS_LAT_REF, "N"), "S")
                lon = _parse_coordinate(gps.get(_GPS_LON), gps.get(_GPS_LON_REF, "E"), "W")
                if lat is not None and lon is not None:
                    fields["GPS"] = f"{lat:.4f}, {lon:.4f}"
    except Exception:
        # Unreadable file, unsupported format, truncated EXIF — all the same
        # answer here. Debug, not warning: on a library with a few thousand
        # PNGs this would otherwise be thousands of log lines saying "a PNG
        # has no EXIF".
        LOGGER.debug("No readable EXIF in %s", path, exc_info=True)
        return ExifInfo()
    return ExifInfo(fields=fields, taken_at=taken_at)


def extract_fields(path: str | Path) -> dict[str, str]:
    """Just the displayable fields — the shape the viewer's popover wants."""
    return extract(path).fields
