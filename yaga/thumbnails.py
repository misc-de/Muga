from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import THUMB_DIR
from .models import MediaItem

LOGGER = logging.getLogger(__name__)


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
            from PIL import Image as PILImage
            img = PILImage.open(str(path))
            # Resize to thumbnail size
            img.thumbnail((320, 320), PILImage.LANCZOS)
            # JPEG cannot store alpha — flatten anything non-RGB (RGBA, P, LA, ...)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(str(target), "JPEG", quality=85)
            return str(target)
        except Exception:
            LOGGER.debug("PIL thumbnail failed for %s", path, exc_info=True)
        
        # Fallback: Try GdkPixbuf (built-in GNOME library)
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf

            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 320, 320, True)
            if pixbuf:
                pixbuf.savev(str(target), "jpeg", ["quality"], ["85"])
                return str(target)
        except Exception:
            pass
        
        # Optional: Try rawpy for RAW image support
        if path.suffix.lower() in {".raw", ".dng", ".cr2", ".nef", ".arw", ".raf", ".rw2", ".orf", ".x3f", ".dcr", ".crw"}:
            try:
                import rawpy
                raw = rawpy.imread(str(path))
                rgb = raw.postprocess()
                from PIL import Image as PILImage
                img = PILImage.fromarray(rgb)
                img.thumbnail((320, 320), PILImage.LANCZOS)
                img.save(str(target), "JPEG", quality=85)
                return str(target)
            except Exception:
                pass
        
        return None

    def _video_thumbnail(self, path: Path, target: Path) -> str | None:
        if shutil.which("ffmpegthumbnailer"):
            cmd = ["ffmpegthumbnailer", "-i", str(path), "-o", str(target), "-s", "320", "-q", "8"]
        elif shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-y", "-i", str(path), "-ss", "00:00:01", "-frames:v", "1", "-vf", "scale=320:-1", str(target)]
        else:
            return None
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            return None
        return str(target) if target.exists() else None

