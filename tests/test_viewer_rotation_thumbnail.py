"""Guards for the gallery-thumbnail refresh after a viewer rotation is saved.

When the user rotates a photo and saves it, the pixels are baked into the file
on disk. The thumbnail is keyed by the (unchanged) source path, so it keeps its
filename — ``Thumbnailer.ensure_thumbnail`` notices the rewritten source by its
mtime and regenerates (see tests/test_thumbnail_freshness.py).

``ViewerWindow._refresh_thumbnail_after_rotation`` adds the two things the
thumbnailer cannot do by itself: writing the regenerated thumbnail back to the
index, and turning the tile in the live grid instead of leaving it sideways
until the next full rescan.
"""
from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

import muga.thumbnails as thumbnails
from muga.models import MediaItem
from muga.thumbnails import Thumbnailer
from muga.viewer import ViewerWindow

PILImage = pytest.importorskip("PIL.Image")


def _make_image(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
    PILImage.new("RGB", size, color).save(str(path), "JPEG", quality=90)


def _touch_newer(path: Path, seconds: float = 10.0) -> None:
    """Move *path*'s mtime *seconds* into the future, leaving atime alone."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + seconds))


class _FakeDatabase:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def set_thumb(self, path: str, thumb_path: str, category: str) -> None:
        self.calls.append((path, thumb_path, category))


class _FakeParent:
    def __init__(self, thumbnailer: Thumbnailer) -> None:
        self.thumbnailer = thumbnailer
        self.database = _FakeDatabase()
        self.thumb_updates: list[tuple[str, str]] = []

    def _enqueue_thumb_update(self, path: str, thumb_path: str) -> None:
        self.thumb_updates.append((path, thumb_path))


def _flush_idle() -> None:
    """Drive pending GLib idle callbacks synchronously — the refresh pushes the
    grid update through ``GLib.idle_add``, which never fires without a loop."""
    from gi.repository import GLib

    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


@pytest.fixture()
def thumbnailer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Thumbnailer:
    thumb_dir = tmp_path / "thumbs"
    monkeypatch.setattr(thumbnails, "THUMB_DIR", thumb_dir)
    return Thumbnailer()


def _refresh(parent: _FakeParent, path: str, item: MediaItem | None) -> None:
    fake_self = types.SimpleNamespace(parent_window=parent)
    ViewerWindow._refresh_thumbnail_after_rotation(fake_self, path, item)
    _flush_idle()


def test_rotation_regenerates_stale_thumbnail(thumbnailer: Thumbnailer, tmp_path: Path) -> None:
    source = tmp_path / "photo.jpg"
    _make_image(source, (40, 20), (200, 30, 30))  # landscape

    first_thumb = thumbnailer.ensure_thumbnail(source, "image")
    assert first_thumb is not None
    landscape_size = PILImage.open(first_thumb).size
    assert landscape_size[0] > landscape_size[1]

    # Simulate the rotation being baked into the file: swap in a portrait image.
    _make_image(source, (20, 40), (30, 30, 200))
    # Push the source's mtime clear of the thumbnail's stamp. A real rotation is
    # seconds of user interaction after the thumbnail was written; doing it
    # explicitly keeps the test off filesystems whose timestamp granularity
    # would land both writes in the same tick.
    _touch_newer(source)

    parent = _FakeParent(thumbnailer)
    item = MediaItem(
        id=1, path=str(source), category="pictures", media_type="image",
        folder="/", name="photo.jpg", mtime=0.0, size=0,
    )
    _refresh(parent, str(source), item)

    # Thumb regenerated in place (same deterministic path, new portrait content).
    refreshed = str(thumbnailer.thumb_path_for(source))
    portrait_size = PILImage.open(refreshed).size
    assert portrait_size[1] > portrait_size[0]
    assert portrait_size != landscape_size

    # Grid + DB were notified with the regenerated thumb.
    assert parent.thumb_updates == [(str(source), refreshed)]
    assert parent.database.calls == [(str(source), refreshed, "pictures")]


def test_refresh_skips_when_item_missing(thumbnailer: Thumbnailer, tmp_path: Path) -> None:
    source = tmp_path / "photo.jpg"
    _make_image(source, (40, 20), (10, 10, 10))
    parent = _FakeParent(thumbnailer)

    _refresh(parent, str(source), None)

    assert parent.thumb_updates == []
    assert parent.database.calls == []


def test_refresh_skips_nc_item_whose_display_path_differs(
    thumbnailer: Thumbnailer, tmp_path: Path,
) -> None:
    """An NC item is viewed from a throwaway local download; its rotation never
    persists to the server, so the display path (the temp file) must not be
    mistaken for the stored item path and thumbnailed."""
    download = tmp_path / "nc-download.jpg"
    _make_image(download, (40, 20), (10, 10, 10))
    parent = _FakeParent(thumbnailer)
    item = MediaItem(
        id=2,
        path="nc://server/album/photo.jpg",
        category="nextcloud",
        media_type="image",
        folder="/album",
        name="photo.jpg",
        mtime=0.0,
        size=0,
    )

    _refresh(parent, str(download), item)

    assert parent.thumb_updates == []
    assert parent.database.calls == []
