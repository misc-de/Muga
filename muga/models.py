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
    # When the camera says the shot was taken, from EXIF. None when the file
    # carries no capture date — a screenshot, a download, a video. Distinct
    # from mtime: copying a photo off a card gives it today's mtime and leaves
    # taken_at at whenever it was shot.
    taken_at: float | None = None

    @property
    def display_time(self) -> float:
        """The timestamp the gallery files this item under.

        The capture date when the file has one, its mtime otherwise. Sorting,
        the month headers and the viewer's date label all read this, so a photo
        cannot appear under one month in the grid and another in the viewer.

        Deliberately not used for the info popover's "Modified" row: that one
        is about the file, and answering it with the capture date would be a
        different fact under the same label.
        """
        return self.taken_at if self.taken_at else self.mtime

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

