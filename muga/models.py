from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".avif"}
RAW_EXTENSIONS = {".raw", ".dng", ".cr2", ".nef", ".arw", ".raf", ".rw2", ".orf", ".x3f", ".dcr", ".crw"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".3gp", ".mpeg", ".mpg"}


@dataclass(frozen=True)
class MediaItem:
    id: int
    path: str
    category: str
    media_type: str
    folder: str
    name: str
    mtime: float
    size: int
    thumb_path: str | None = None

    @property
    def is_video(self) -> bool:
        return self.media_type == "video"

    @property
    def parent(self) -> str:
        return str(Path(self.path).parent)


def media_type_for(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in RAW_EXTENSIONS:
        return "image"  # RAW files are treated as images
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None

