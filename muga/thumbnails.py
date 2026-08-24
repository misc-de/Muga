from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import THUMB_DIR
from .models import MediaItem

LOGGER = logging.getLogger(__name__)
_VIDEO_THUMB_TIMEOUT_SEC = 20


def _tmp_sibling(target: Path) -> Path:
    """A per-process, per-thread temp name beside *target*. Distinct idents keep
    two threads that race to thumbnail the same file (scanner pool vs the UI's
    on-demand loader) from writing the same temp."""
    return target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def _save_atomic(target: Path, write) -> None:
    """Run *write(tmp)* then atomically move tmp into place, so a concurrent or
    crashing writer can never leave a half-written thumbnail at *target*."""
    tmp = _tmp_sibling(target)
    try:
        write(tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("tmp.unlink failed", exc_info=True)
        raise


# Round-trip slack for the source-vs-thumbnail mtime comparison below. A
# thumbnail is stamped with its source's mtime, and that float crosses two
# filesystems' timestamp representations on the way back out; a
# sub-microsecond wobble is not an edited photo. Same value and same reasoning
# as scanner._MTIME_EPS.
_MTIME_EPS = 1e-6


def _stamp_source_mtime(target: Path, source_mtime: float | None) -> None:
    """Set *target*'s mtime to *source_mtime*, leaving its atime alone.

    atime is what the disk-cache evictor sorts on (LRU — see
    gallery_thumbnails.evict_cache), so it has to survive the stamp.

    Best-effort: a filesystem that refuses utime leaves the thumbnail carrying
    its generation time, which _thumbnail_is_current reads as "still current" —
    exactly the behaviour this module had before stamping existed.
    """
    if source_mtime is None:
        return
    try:
        os.utime(target, (target.stat().st_atime, source_mtime))
    except OSError:
        LOGGER.debug("could not stamp thumbnail mtime for %s", target, exc_info=True)


def _thumbnail_is_current(target: Path, source_mtime: float | None) -> bool:
    """True when the thumbnail cached at *target* still shows what is in its
    source file.

    ``thumb_path_for`` keys on the file path alone, so a photo edited in place
    — a rotation baked in by the viewer, an EXIF write after a capture, an
    external editor — keeps the same thumbnail filename. Testing
    ``target.exists()`` on its own would then serve the pre-edit image for as
    long as the photo keeps its name.

    Every thumbnail this module writes is stamped with its source's mtime, so
    the two timestamps are equal while the thumbnail is current, and the
    source's runs ahead the moment the photo is rewritten.

    The comparison is ``>=`` rather than ``==`` on purpose: thumbnails
    generated before stamping existed carry their *generation* time, which is
    later than the source's, so upgrading does not invalidate an entire
    library at once. The cost is that a source restored from a backup with an
    *older* mtime is missed here — the scanner catches that one, because it
    compares against the mtime recorded in the index and requires a match in
    both directions.
    """
    try:
        thumb_mtime = target.stat().st_mtime
    except OSError:
        return False
    if source_mtime is None:
        # Source unreadable — an unmounted drive, a permission change. There is
        # nothing to regenerate from, so the cached thumbnail is the best answer
        # available: a stale tile beats a blank one.
        return True
    return thumb_mtime >= source_mtime - _MTIME_EPS

# Sync the decompression-bomb cap with editor/_pil.py at import time so the
# scanner thumbnail path is also protected — a hostile NC server could
# otherwise feed us a multi-gigapixel PNG that OOMs the worker pool when a
# folder of NC items scrolls into view. Best-effort: if Pillow isn't
# installed we degrade to GdkPixbuf for thumbnails anyway.
# Single source of truth for the decompression-bomb cap, shared with
# editor/_pil.py and the GdkPixbuf paths below. 120 MP covers 108 MP phone
# sensors and medium-format bodies; past that a file is far more likely to be
# hostile than to be someone's holiday photo.
MAX_IMAGE_PIXELS = 120_000_000

# Lowest Pillow release without known decoder advisories. Kept in sync with
# the floor in pyproject.toml — but pyproject is not what most users install
# through: install.sh deliberately runs the app from the source tree against
# the system Python, so the declared floor is never enforced anywhere. On a
# phone distro shipping an old python3-pil that silently means outdated image
# decoders handling untrusted photos, so we say so at startup.
MIN_SAFE_PILLOW = (12, 3)

try:
    import warnings

    from PIL import Image as _PILImageInit  # noqa: N812 — module init, not a use
    _PILImageInit.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    # Pillow treats MAX_IMAGE_PIXELS as a *warning* threshold and only raises
    # past 2×, so on its own the cap does not actually stop a decode: a 288 MP
    # image allocated ~0.9 GB and the OOM killer beat the error to it on a
    # phone. Promoting the warning makes the number mean what it says.
    warnings.filterwarnings(
        "error", category=_PILImageInit.DecompressionBombWarning,
    )
except ImportError:
    LOGGER.debug("warnings.filterwarnings failed", exc_info=True)


def pillow_version_warning() -> str:
    """Return a warning if the installed Pillow predates MIN_SAFE_PILLOW.

    Empty string when Pillow is current or absent (absent is handled by the
    graceful-degradation paths; outdated is the dangerous case, because
    everything looks like it works).
    """
    try:
        from PIL import __version__ as pil_version
    except ImportError:
        return ""
    try:
        parts = tuple(int(p) for p in pil_version.split(".")[:2])
    except ValueError:
        return ""
    if parts >= MIN_SAFE_PILLOW:
        return ""
    return (
        f"Pillow {pil_version} is older than "
        f"{'.'.join(str(p) for p in MIN_SAFE_PILLOW)} and has known image-decoder "
        f"vulnerabilities. Photos are untrusted input — please update Pillow."
    )


def _dimensions_from_header(path) -> tuple[int, int] | None:
    """Read (width, height) straight out of the file header.

    Deliberately hand-rolled rather than delegating to the image libraries:
    ``GdkPixbuf.Pixbuf.get_file_info`` and ``PixbufLoader`` both allocate the
    full pixel buffer *before* reporting a size (measured: 1.5 GB on a 400 MP
    PNG), which makes them useless as a guard — asking the question was the
    bomb. Covers the formats a decompression bomb is actually built from;
    anything else returns None and is let through to the normal decode path.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                return (
                    int.from_bytes(head[16:20], "big"),
                    int.from_bytes(head[20:24], "big"),
                )
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return (
                    int.from_bytes(head[6:8], "little"),
                    int.from_bytes(head[8:10], "little"),
                )
            if head[:2] == b"BM":
                return (
                    int.from_bytes(head[18:22], "little", signed=True),
                    abs(int.from_bytes(head[22:26], "little", signed=True)),
                )
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                chunk = head[12:16]
                if chunk == b"VP8X":
                    # Extended: 24-bit canvas size minus one, little-endian,
                    # after the 4-byte size field and 3 reserved/flag bytes.
                    return (
                        int.from_bytes(head[24:27], "little") + 1,
                        int.from_bytes(head[27:30], "little") + 1,
                    )
                if chunk == b"VP8 ":
                    # Lossy: 3-byte start code 9d 01 2a, then 14-bit w/h.
                    if head[23:26] == b"\x9d\x01\x2a":
                        return (
                            int.from_bytes(head[26:28], "little") & 0x3FFF,
                            int.from_bytes(head[28:30], "little") & 0x3FFF,
                        )
                    return None
                if chunk == b"VP8L":
                    # Lossless: signature byte 0x2f, then 14-bit (w-1),
                    # 14-bit (h-1) packed little-endian across 4 bytes.
                    if head[20:21] == b"\x2f":
                        bits = int.from_bytes(head[21:25], "little")
                        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
                    return None
                return None
            if head[:2] == b"\xff\xd8":
                # JPEG: walk the marker chain to the frame header.
                fh.seek(2)
                while True:
                    marker = fh.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    code = marker[1]
                    if code in (0xD8, 0xD9) or 0xD0 <= code <= 0xD7:
                        continue
                    length_bytes = fh.read(2)
                    if len(length_bytes) < 2:
                        return None
                    length = int.from_bytes(length_bytes, "big")
                    # SOF0-SOF15 carry the dimensions; C4/C8/CC are not frames.
                    if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                        sof = fh.read(5)
                        if len(sof) < 5:
                            return None
                        return (
                            int.from_bytes(sof[3:5], "big"),
                            int.from_bytes(sof[1:3], "big"),
                        )
                    if length < 2:
                        return None
                    fh.seek(length - 2, 1)
    except OSError:
        return None
    return None


def image_within_pixel_budget(path) -> bool:
    """True if the image at *path* is small enough to decode.

    GdkPixbuf has no decompression-bomb protection at all, so it cheerfully
    decoded the very files Pillow had just rejected — every fallback path was
    a way around the cap. This gate closes them.

    Unknown or unreadable headers return True: this guards against absurd
    dimensions, it is not a format validator, and the caller's own error
    handling deals with files that turn out to be undecodable.
    """
    dims = _dimensions_from_header(path)
    if dims is None:
        return True
    width, height = dims
    if width <= 0 or height <= 0:
        return True
    if width * height > MAX_IMAGE_PIXELS:
        LOGGER.warning(
            "Refusing oversized image %s (%dx%d = %d px, limit %d)",
            path, width, height, width * height, MAX_IMAGE_PIXELS,
        )
        return False
    return True


class Thumbnailer:
    def __init__(self) -> None:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)

    def thumb_path_for(self, path: Path) -> Path:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        return THUMB_DIR / f"{digest}.jpg"

    def ensure_thumbnail(self, item_or_path: MediaItem | Path, media_type: str | None = None) -> str | None:
        if isinstance(item_or_path, MediaItem):
            path = Path(item_or_path.path)
            media_type = item_or_path.media_type
        else:
            path = item_or_path
        target = self.thumb_path_for(path)
        try:
            source_mtime: float | None = path.stat().st_mtime
        except OSError:
            source_mtime = None
        # Not `target.exists()`: a photo edited in place keeps its path, so the
        # cached thumbnail keeps its name. See _thumbnail_is_current.
        if _thumbnail_is_current(target, source_mtime):
            return str(target)
        if media_type == "video":
            thumb = self._video_thumbnail(path, target)
        else:
            thumb = self._image_thumbnail(path, target)
        if thumb:
            _stamp_source_mtime(target, source_mtime)
        return thumb

    def invalidate(self, path: Path) -> None:
        """Drop the cached thumbnail for *path*, so the next ensure_thumbnail
        regenerates it from the file as it now stands.

        ensure_thumbnail already invalidates itself on mtime; this is for the
        callers that want the guarantee without depending on timestamps at all
        — two rotations saved inside one filesystem timestamp tick, say."""
        try:
            self.thumb_path_for(path).unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("thumbnail invalidate failed for %s", path, exc_info=True)

    def clear(self) -> None:
        if THUMB_DIR.exists():
            shutil.rmtree(THUMB_DIR)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)

    def ensure_thumbnails_batch(self, items: list[MediaItem], max_workers: int | None = None) -> dict[str, str | None]:
        """
        Generate thumbnails for multiple items in parallel.
        Uses ThreadPoolExecutor to process videos concurrently.
        Returns dict mapping item paths to thumbnail paths (or None if failed).
        """
        if not items:
            return {}

        # Default: use CPU count (good for video encoding)
        if max_workers is None:
            max_workers = min(os.cpu_count() or 4, 8)  # Cap at 8 threads to avoid resource exhaustion
        
        result: dict[str, str | None] = {}
        
        def _ensure_one(item: MediaItem) -> tuple[str, str | None]:
            thumb = self.ensure_thumbnail(item)
            return (item.path, thumb)
        
        LOGGER.debug("Batch thumbnail generation for %d items (max_workers=%d)", len(items), max_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for path, thumb in executor.map(_ensure_one, items, timeout=300):
                result[path] = thumb
        
        return result

    def _image_thumbnail(self, path: Path, target: Path) -> str | None:
        # Size gate before any decoder sees the file. Pillow's own cap is a
        # warning promoted to an error, which works but depends on a global
        # warnings filter that any other code (or a test runner) can reset;
        # this check is explicit and covers the GdkPixbuf fallback too, which
        # has no bomb protection whatsoever.
        if not image_within_pixel_budget(path):
            return None
        # Try PIL/Pillow first (supports most standard formats)
        try:
            from PIL import Image as PILImage, ImageOps
            # Context manager closes the source file descriptor promptly — the
            # scanner runs this across thousands of files per sweep, so we don't
            # want to lean on GC timing to release handles.
            with PILImage.open(str(path)) as opened:
                # Apply EXIF orientation so portrait photos from phones aren't sideways.
                pic: PILImage.Image = opened
                try:
                    pic = ImageOps.exif_transpose(opened) or opened
                except Exception:
                    # No or broken EXIF: keep the pixels as they were stored.
                    LOGGER.debug("exif_transpose failed for %s", path, exc_info=True)
                # Resize to thumbnail size
                pic.thumbnail((320, 320), PILImage.Resampling.LANCZOS)
                # JPEG cannot store alpha — flatten anything non-RGB (RGBA, P, LA, ...)
                if pic.mode != "RGB":
                    pic = pic.convert("RGB")
                _save_atomic(target, lambda t: pic.save(str(t), "JPEG", quality=85))
                return str(target)
        except (PILImage.DecompressionBombError, PILImage.DecompressionBombWarning):
            # Refuse oversized images explicitly — a debug-level log buries
            # this in noise, but the user (or admin) wants to know that a
            # specific file was rejected for safety reasons.
            LOGGER.warning(
                "Skipping decompression-bomb image %s (exceeds %s pixels)",
                path, PILImage.MAX_IMAGE_PIXELS,
            )
        except Exception:
            LOGGER.debug("PIL thumbnail failed for %s", path, exc_info=True)
        
        # Fallback: Try GdkPixbuf (built-in GNOME library)
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf

            if not image_within_pixel_budget(path):
                return None
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 320, 320, True)
            if pixbuf:
                # Apply embedded EXIF orientation if present.
                pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
                _save_atomic(target, lambda t: pixbuf.savev(str(t), "jpeg", ["quality"], ["85"]))
                return str(target)
        except Exception:
            # GdkPixbuf fallback for what Pillow could not decode; the RAW
            # branch below is the last attempt.
            LOGGER.debug("GdkPixbuf thumbnail failed for %s", path, exc_info=True)
        
        # Optional: Try rawpy for RAW image support
        if path.suffix.lower() in {".raw", ".dng", ".cr2", ".nef", ".arw", ".raf", ".rw2", ".orf", ".x3f", ".dcr", ".crw"}:
            try:
                import rawpy
                with rawpy.imread(str(path)) as raw:
                    rgb = raw.postprocess()
                from PIL import Image as PILImage
                with PILImage.fromarray(rgb) as img:
                    img.thumbnail((320, 320), PILImage.Resampling.LANCZOS)
                    _save_atomic(target, lambda t: img.save(str(t), "JPEG", quality=85))
                return str(target)
            except Exception:
                # rawpy is optional and RAW formats vary wildly — give up on a
                # thumbnail for this file rather than failing the whole scan.
                LOGGER.debug("RAW thumbnail failed for %s", path, exc_info=True)
        
        return None

    def _video_thumbnail(self, path: Path, target: Path) -> str | None:
        # Encode to a private temp then atomically publish, so a concurrent
        # generator (scanner pool vs the UI's on-demand loader) or a killed
        # ffmpeg never leaves a truncated frame at *target*.
        tmp = _tmp_sibling(target)
        if shutil.which("ffmpegthumbnailer"):
            cmd = ["ffmpegthumbnailer", "-i", str(path), "-o", str(tmp), "-s", "320", "-q", "8"]
        elif shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-y", "-i", str(path), "-ss", "00:00:01", "-frames:v", "1", "-vf", "scale=320:-1", str(tmp)]
        else:
            return None
        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_VIDEO_THUMB_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            LOGGER.warning("Video thumbnailer timed out for %s", path)
            tmp.unlink(missing_ok=True)
            return None
        except (OSError, subprocess.CalledProcessError):
            tmp.unlink(missing_ok=True)
            return None
        if not tmp.exists():
            return None
        try:
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            return None
        return str(target)
