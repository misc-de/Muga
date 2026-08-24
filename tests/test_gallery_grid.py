"""Tests for the gallery grid.

The grid packs media items into fixed-width tile rows inside a ListView and
keeps two path indexes alongside so a freshly generated thumbnail can be
pushed into a single visible tile without a full re-render. The interesting
behaviour is that bookkeeping — an index left pointing at an evicted row pins
it in memory, and one pointing at a stale row updates a tile that is no longer
on screen.

Row models and the exists-cache are plain objects and run headless; anything
that builds a widget needs a display.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import requires_display

gg = pytest.importorskip("muga.gallery_grid")

from muga.models import MediaItem  # noqa: E402


def _item(name="a.jpg", path=None, thumb=None) -> MediaItem:
    return MediaItem(
        id=1, path=path or f"/x/{name}", category="photos", media_type="image",
        folder="/x", name=name, mtime=1.7e9, size=1, thumb_path=thumb,
    )


# ---------------------------------------------------------------------------
# Row models
# ---------------------------------------------------------------------------

def test_media_row_from_media() -> None:
    row = gg.MediaRow.from_media(_item())
    assert row.media_item is not None
    assert row.is_folder is False
    assert row.folder_path is None


def test_media_row_from_folder() -> None:
    row = gg.MediaRow.from_folder("/x/holiday", 12, ["/t/a.jpg", "/t/b.jpg"])
    assert row.is_folder is True
    assert row.folder_count == 12
    assert row.folder_thumbs == ["/t/a.jpg", "/t/b.jpg"]
    assert row.media_item is None


def test_gallery_row_header_carries_its_date() -> None:
    """The per-header arrows find the adjacent month in the store from these
    rather than re-parsing the markup."""
    row = gg.GalleryRow.header("<b>March</b>", year=2026, month=3)
    assert row.is_header is True
    assert (row.header_year, row.header_month) == (2026, 3)
    assert row.tiles == []


def test_gallery_row_header_without_a_date() -> None:
    row = gg.GalleryRow.header("Folders")
    assert row.header_year is None
    assert row.header_month is None


def test_gallery_row_from_tiles_copies_the_list() -> None:
    """The builder reuses its list between rows; sharing it would make every
    row show the last row's tiles."""
    tiles = [gg.MediaRow.from_media(_item())]
    row = gg.GalleryRow.from_tiles(tiles)
    tiles.append(gg.MediaRow.from_media(_item(name="b.jpg")))
    assert len(row.tiles) == 1


# ---------------------------------------------------------------------------
# Thumbnail existence cache
# ---------------------------------------------------------------------------

def _cache_grid(ttl=30.0, cap=100):
    grid = SimpleNamespace(
        _exists_cache=__import__("collections").OrderedDict(),
        _EXISTS_TTL=ttl,
        _EXISTS_CACHE_MAX=cap,
    )
    return grid


def test_exists_cache_avoids_a_second_stat(tmp_path: Path) -> None:
    """This sits in the tile-bind hot path; a stat per tile per scroll frame
    is what it exists to avoid."""
    photo = tmp_path / "t.jpg"
    photo.write_bytes(b"x")
    grid = _cache_grid()

    assert gg.GalleryGrid._thumb_exists(grid, str(photo)) is True
    photo.unlink()
    assert gg.GalleryGrid._thumb_exists(grid, str(photo)) is True, "cache was not used"


def test_exists_cache_expires(tmp_path: Path) -> None:
    photo = tmp_path / "t.jpg"
    grid = _cache_grid(ttl=0.0)
    assert gg.GalleryGrid._thumb_exists(grid, str(photo)) is False
    photo.write_bytes(b"x")
    assert gg.GalleryGrid._thumb_exists(grid, str(photo)) is True, "TTL never expired"


def test_exists_cache_rejects_an_empty_path() -> None:
    assert gg.GalleryGrid._thumb_exists(_cache_grid(), "") is False


def test_exists_cache_is_bounded() -> None:
    grid = _cache_grid(cap=10)
    for i in range(50):
        gg.GalleryGrid._thumb_exists(grid, f"/nonexistent/{i}.jpg")
    assert len(grid._exists_cache) <= 10


def test_exists_cache_evicts_the_least_recently_used() -> None:
    """A hot path that keeps being probed must not be thrown out for a
    one-off lookup."""
    grid = _cache_grid(cap=3)
    for i in range(3):
        gg.GalleryGrid._thumb_exists(grid, f"/p/{i}.jpg")
    gg.GalleryGrid._thumb_exists(grid, "/p/0.jpg")      # touch the oldest
    gg.GalleryGrid._thumb_exists(grid, "/p/new.jpg")    # forces an eviction
    assert "/p/0.jpg" in grid._exists_cache, "the recently used entry was evicted"
    assert "/p/1.jpg" not in grid._exists_cache


# ---------------------------------------------------------------------------
# Real grid
# ---------------------------------------------------------------------------

@pytest.fixture
def grid(gallery_window):
    return gallery_window.gallery_grid


@requires_display
def test_grid_packs_tiles_into_rows(grid) -> None:
    grid.clear()
    grid.set_columns(4)
    for i in range(9):
        grid.append_media(_item(name=f"{i}.jpg", path=f"/x/{i}.jpg"))
    grid.finish()
    # 9 items at 4 per row → 3 rows (the last one partial).
    assert grid.row_store.get_n_items() == 3


@requires_display
def test_grid_flushes_a_partial_row_on_finish(grid) -> None:
    """Without the flush the last few photos of a library never appear."""
    grid.clear()
    grid.set_columns(4)
    grid.append_media(_item(name="only.jpg", path="/x/only.jpg"))
    assert grid.row_store.get_n_items() == 0
    grid.finish()
    assert grid.row_store.get_n_items() == 1


@requires_display
def test_grid_header_closes_the_open_row(grid) -> None:
    """A month header must not appear in the middle of the previous month's
    tiles."""
    grid.clear()
    grid.set_columns(4)
    grid.append_media(_item(name="a.jpg", path="/x/a.jpg"))
    grid.append_header("April", year=2026, month=4)
    grid.finish()

    first = grid.row_store.get_item(0)
    second = grid.row_store.get_item(1)
    assert first.is_header is False
    assert second.is_header is True


@requires_display
def test_grid_clear_resets_everything(grid) -> None:
    grid.clear()
    grid.set_columns(4)
    for i in range(5):
        grid.append_media(_item(name=f"{i}.jpg", path=f"/x/{i}.jpg"))
    grid.finish()
    grid.clear()
    assert grid.row_store.get_n_items() == 0
    assert grid._item_index == {}
    assert grid._building_row == []


@requires_display
def test_grid_indexes_items_for_targeted_updates(grid) -> None:
    """update_item_thumb finds a single tile through this instead of
    re-rendering the whole view."""
    grid.clear()
    grid.set_columns(4)
    grid.append_media(_item(name="a.jpg", path="/x/a.jpg"))
    grid.finish()
    assert "/x/a.jpg" in grid._item_index


@requires_display
def test_grid_eviction_drops_rows_and_their_index_entries(grid) -> None:
    """An index entry left behind pins a MediaRow that is no longer in the
    store — the leak the sliding window exists to prevent."""
    grid.clear()
    grid.set_columns(2)
    for i in range(8):
        grid.append_media(_item(name=f"{i}.jpg", path=f"/x/{i}.jpg"))
    grid.finish()
    assert grid.row_store.get_n_items() == 4

    grid.evict_front_rows(2)

    assert grid.row_store.get_n_items() == 2
    for gone in ("/x/0.jpg", "/x/1.jpg", "/x/2.jpg", "/x/3.jpg"):
        assert gone not in grid._item_index, f"{gone} still indexed after eviction"
    for kept in ("/x/4.jpg", "/x/7.jpg"):
        assert kept in grid._item_index


@requires_display
def test_grid_eviction_keeps_a_re_added_folder(grid) -> None:
    """A later page may re-add the same folder; forgetting it blindly would
    break the newer tile's updates."""
    grid.clear()
    grid.set_columns(1)
    grid.append_folder("/x/holiday", 3, [])
    grid.finish()
    first_tile = grid._folder_index["/x/holiday"]

    grid.append_folder("/x/holiday", 5, [])
    grid.finish()
    assert grid._folder_index["/x/holiday"] is not first_tile

    grid.evict_front_rows(1)
    assert "/x/holiday" in grid._folder_index, "the newer folder tile was forgotten"


@requires_display
def test_grid_eviction_of_nothing_is_a_noop(grid) -> None:
    grid.clear()
    grid.append_media(_item())
    grid.finish()
    before = grid.row_store.get_n_items()
    grid.evict_front_rows(0)
    grid.evict_front_rows(-5)
    assert grid.row_store.get_n_items() == before


@requires_display
def test_grid_eviction_clamps_to_the_store_size(grid) -> None:
    grid.clear()
    grid.append_media(_item())
    grid.finish()
    grid.evict_front_rows(999)
    assert grid.row_store.get_n_items() == 0


@requires_display
def test_grid_empty_label_toggles(grid) -> None:
    grid.set_empty("Nothing here", visible=True)
    assert grid.empty_label.get_visible() is True
    grid.set_empty("", visible=False)
    assert grid.empty_label.get_visible() is False


@requires_display
def test_grid_update_thumb_reports_a_miss(grid) -> None:
    grid.clear()
    assert grid.update_item_thumb("/not/indexed.jpg", "/t/x.jpg") is False


@requires_display
def test_grid_update_thumb_clears_a_stale_miss(grid, tmp_path) -> None:
    """The thumbnail was generated after the bind that cached "not there"; a
    stale miss would leave the tile blank until the next full render."""
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"x")
    grid.clear()
    grid.set_columns(4)
    grid.append_media(_item(name="a.jpg", path="/x/a.jpg"))
    grid.finish()

    grid._exists_cache[str(thumb)] = (time.monotonic(), False)   # stale miss
    assert grid.update_item_thumb("/x/a.jpg", str(thumb)) is True

    cached = grid._exists_cache.get(str(thumb))
    assert cached is None or cached[1] is True, "the stale miss survived"


@requires_display
def test_grid_update_thumb_rewrites_the_item(grid, tmp_path) -> None:
    """MediaItem is frozen, so the row has to be given a replaced copy — an
    in-place assignment would raise and the tile would never update."""
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"x")
    grid.clear()
    grid.set_columns(4)
    grid.append_media(_item(name="a.jpg", path="/x/a.jpg", thumb=None))
    grid.finish()

    grid.update_item_thumb("/x/a.jpg", str(thumb))
    assert grid._item_index["/x/a.jpg"].media_item.thumb_path == str(thumb)


@requires_display
def test_grid_update_thumb_ignores_a_folder_tile(grid) -> None:
    grid.clear()
    grid.append_folder("/x/holiday", 2, [])
    grid.finish()
    assert grid.update_item_thumb("/x/holiday", "/t/x.jpg") is False
