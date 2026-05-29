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
            pass
        raise

# Sync the decompression-bomb cap with editor/_pil.py at import time so the
# scanner thumbnail path is also protected — a hostile NC server could
# otherwise feed us a multi-gigapixel PNG that OOMs the worker pool when a
# folder of NC items scrolls into view. Best-effort: if Pillow isn't
# installed we degrade to GdkPixbuf for thumbnails anyway.
try:
    from PIL import Image as _PILImageInit  # noqa: N812 — module init, not a use
    _PILImageInit.MAX_IMAGE_PIXELS = 200_000_000
except ImportError:
    pass


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
        if target.exists():
            return str(target)
        if media_type == "video":
            return self._video_thumbnail(path, target)
        return self._image_thumbnail(path, target)

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
        import os
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
        # Try PIL/Pillow first (supports most standard formats)
        try:
            from PIL import Image as PILImage, ImageOps
            # Context manager closes the source file descriptor promptly — the
            # scanner runs this across thousands of files per sweep, so we don't
            # want to lean on GC timing to release handles.
            with PILImage.open(str(path)) as img:
                # Apply EXIF orientation so portrait photos from phones aren't sideways.
                try:
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                # Resize to thumbnail size
                img.thumbnail((320, 320), PILImage.LANCZOS)
                # JPEG cannot store alpha — flatten anything non-RGB (RGBA, P, LA, ...)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                _save_atomic(target, lambda t: img.save(str(t), "JPEG", quality=85))
                return str(target)
        except PILImage.DecompressionBombError:
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

            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 320, 320, True)
            if pixbuf:
                # Apply embedded EXIF orientation if present.
                pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
                _save_atomic(target, lambda t: pixbuf.savev(str(t), "jpeg", ["quality"], ["85"]))
                return str(target)
        except Exception:
            pass
        
        # Optional: Try rawpy for RAW image support
        if path.suffix.lower() in {".raw", ".dng", ".cr2", ".nef", ".arw", ".raf", ".rw2", ".orf", ".x3f", ".dcr", ".crw"}:
            try:
                import rawpy
                with rawpy.imread(str(path)) as raw:
                    rgb = raw.postprocess()
                from PIL import Image as PILImage
                with PILImage.fromarray(rgb) as img:
                    img.thumbnail((320, 320), PILImage.LANCZOS)
                    _save_atomic(target, lambda t: img.save(str(t), "JPEG", quality=85))
                return str(target)
            except Exception:
                pass
        
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
