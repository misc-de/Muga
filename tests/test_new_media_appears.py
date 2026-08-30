"""A picture written to disk shows up on its own, without a manual refresh.

Before this, the gallery only learned about a file when something asked it to
scan: startup, the refresh button, pull-to-refresh, or the built-in camera
handing back a capture — and that last one triggered a rescan scoped to the
category being *viewed*, so a shot taken while Videos or Nextcloud was open
never reached the index at all. Anything written by another app was invisible
until the user refreshed by hand.

Two pieces fix that, and both are covered here:

  * ``MediaScanner.index_file`` folds a single known file into the index,
    making the same category/folder decisions a full walk would.
  * ``MediaWatcher`` monitors the indexed roots and reports what changed.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from gi.repository import GLib

from muga.database import Database
from muga.scanner import MediaScanner
from muga.thumbnails import Thumbnailer
from muga.watcher import MediaWatcher


def _jpeg(path: Path, colour=(10, 20, 30)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), colour).save(path)
    return path


@pytest.fixture
def library(tmp_path):
    """A scanner over a Photos root, plus the category tuples it takes."""
    photos = tmp_path / "Photos"
    photos.mkdir()
    database = Database(tmp_path / "index.sqlite3")
    scanner = MediaScanner(database, Thumbnailer())
    categories = [("photos", "Photos", str(photos))]
    return SimpleNamespace(
        root=photos, database=database, scanner=scanner, categories=categories,
        tmp_path=tmp_path,
    )


# ---------------------------------------------------------------------------
# index_file: one file, indexed the way a full walk would have indexed it
# ---------------------------------------------------------------------------

def test_a_new_photo_lands_in_the_overview(library) -> None:
    """The report: a new picture was not added to the Overview by itself."""
    shot = _jpeg(library.root / "20260825_101530.jpg")

    assert library.scanner.index_file(shot, library.categories) is True

    assert [i.name for i in library.database.list_media("pictures")] == [shot.name]


def test_it_lands_in_a_subfolder_under_its_own_folder_name(library) -> None:
    shot = _jpeg(library.root / "Camera" / "shot.jpg")

    library.scanner.index_file(shot, library.categories)

    item = library.database.get_media_by_path(str(shot))
    assert item is not None
    assert item.folder == "Camera"
    assert item.category == "photos"


def test_a_photo_in_the_root_is_filed_under_slash(library) -> None:
    shot = _jpeg(library.root / "shot.jpg")

    library.scanner.index_file(shot, library.categories)

    assert library.database.get_media_by_path(str(shot)).folder == "/"


def test_it_gets_a_thumbnail(library) -> None:
    shot = _jpeg(library.root / "shot.jpg")

    library.scanner.index_file(shot, library.categories)

    thumb = library.database.get_media_by_path(str(shot)).thumb_path
    assert thumb and Path(thumb).exists()


def test_a_file_outside_every_root_is_ignored(library) -> None:
    stray = _jpeg(library.tmp_path / "Elsewhere" / "shot.jpg")

    assert library.scanner.index_file(stray, library.categories) is False
    assert library.database.get_media_by_path(str(stray)) is None


def test_a_file_in_a_hidden_directory_is_ignored(library) -> None:
    """The walk never descends into hidden directories — they hold tooling,
    not photos — so indexing one file at a time must not sneak one in."""
    hidden = _jpeg(library.root / ".cache" / "icon.png")

    assert library.scanner.index_file(hidden, library.categories) is False


def test_a_non_media_file_is_ignored(library) -> None:
    note = library.root / "notes.txt"
    note.write_text("not a picture")

    assert library.scanner.index_file(note, library.categories) is False


def test_a_symlink_is_ignored(library) -> None:
    real = _jpeg(library.tmp_path / "real.jpg")
    link = library.root / "link.jpg"
    link.symlink_to(real)

    assert library.scanner.index_file(link, library.categories) is False


def test_overlapping_roots_both_claim_the_file(library) -> None:
    """Exactly what a full scan does — the Overview's dedup collapses it back
    to one tile, so claiming it once would instead lose it from a category."""
    inner = library.root / "Camera"
    inner.mkdir()
    categories = library.categories + [("extra_0", "Camera", str(inner))]
    shot = _jpeg(inner / "shot.jpg")

    library.scanner.index_file(shot, categories)

    with library.database.lock:
        rows = library.database.conn.execute(
            "SELECT category FROM media WHERE path = ? ORDER BY category",
            (str(shot),),
        ).fetchall()
    assert [r[0] for r in rows] == ["extra_0", "photos"]


def test_a_no_inherit_subtree_belongs_to_itself_only(library) -> None:
    """A location flagged "do not inherit" is scanned under its own category
    and subtracted from the parent root's walk. Single-file indexing has to
    apply the same carve-out or the file shows up under both."""
    inner = library.root / "Camera"
    inner.mkdir()
    categories = library.categories + [("extra_0", "Camera", str(inner))]
    shot = _jpeg(inner / "shot.jpg")

    library.scanner.index_file(shot, categories, excluded_subtrees=[str(inner)])

    with library.database.lock:
        rows = library.database.conn.execute(
            "SELECT category FROM media WHERE path = ?", (str(shot),),
        ).fetchall()
    assert [r[0] for r in rows] == ["extra_0"]


def test_the_root_itself_being_excluded_still_indexes(library) -> None:
    """"Do not inherit" carves the subtree out of *parent* roots. Applying it
    to the category the subtree actually is would leave the file nowhere."""
    shot = _jpeg(library.root / "shot.jpg")

    indexed = library.scanner.index_file(
        shot, library.categories, excluded_subtrees=[str(library.root)],
    )

    assert indexed is True


def test_an_edited_file_drops_its_stale_thumbnail(library) -> None:
    """A path-keyed thumbnail survives an edit in place and would keep showing
    the old pixels."""
    shot = _jpeg(library.root / "shot.jpg", colour=(10, 20, 30))
    library.scanner.index_file(shot, library.categories)
    thumb = Path(library.database.get_media_by_path(str(shot)).thumb_path)
    before = thumb.read_bytes()

    _jpeg(shot, colour=(240, 30, 30))
    library.scanner.index_file(shot, library.categories)

    assert thumb.read_bytes() != before


def test_re_indexing_an_unchanged_file_reports_no_change(library) -> None:
    """The watcher reports the capture the camera callback has already
    indexed, and fires on files that were merely touched. Re-rendering the
    grid for those would jolt it under the user's finger."""
    shot = _jpeg(library.root / "shot.jpg")

    assert library.scanner.index_file(shot, library.categories) is True
    assert library.scanner.index_file(shot, library.categories) is False
    assert library.database.count_media("pictures") == 1


def test_a_lost_thumbnail_is_regenerated(library) -> None:
    """An unchanged file whose thumbnail was evicted by the cache budget still
    has work to do — skipping it would leave a blank tile."""
    shot = _jpeg(library.root / "shot.jpg")
    library.scanner.index_file(shot, library.categories)
    thumb = Path(library.database.get_media_by_path(str(shot)).thumb_path)
    thumb.unlink()

    assert library.scanner.index_file(shot, library.categories) is True
    assert thumb.exists()


def test_a_second_root_added_later_gets_its_own_row(library) -> None:
    """The file is unchanged, but the new category has no row for it yet."""
    shot = _jpeg(library.root / "Camera" / "shot.jpg")
    library.scanner.index_file(shot, library.categories)
    inner = library.root / "Camera"
    grown = library.categories + [("extra_0", "Camera", str(inner))]

    assert library.scanner.index_file(shot, grown) is True
    assert library.database.get_media_by_path(str(shot), "extra_0") is not None


def test_a_vanished_file_is_forgotten(library) -> None:
    shot = _jpeg(library.root / "shot.jpg")
    library.scanner.index_file(shot, library.categories)
    shot.unlink()

    assert library.scanner.forget_file(shot) is True
    assert library.database.count_media("pictures") == 0


def test_forgetting_an_unknown_file_reports_no_change(library) -> None:
    """The watcher reports every path under a root, most of which were never
    in the index — those must not fake a change and force a re-render."""
    assert library.scanner.forget_file(library.root / "never-seen.jpg") is False


# ---------------------------------------------------------------------------
# MediaWatcher
# ---------------------------------------------------------------------------

def _drain(seen: list, timeout: float = 5.0) -> list:
    """Iterate the main loop until the watcher reports a batch, or give up.

    inotify delivery plus the watcher's own debounce mean the batch does not
    exist yet when the file is written; a plain assert would race it.
    """
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not seen and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.01)
    return seen


@pytest.fixture
def watcher():
    seen: list[set[str]] = []
    made = MediaWatcher(seen.append, debounce_ms=50)
    yield SimpleNamespace(watcher=made, seen=seen)
    made.stop()


def test_a_photo_written_by_another_app_is_reported(watcher, tmp_path) -> None:
    """The whole point: nothing in Muga created this file."""
    root = tmp_path / "Photos"
    root.mkdir()
    watcher.watcher.watch([root])

    _jpeg(root / "outside.jpg")

    batches = _drain(watcher.seen)
    assert batches, "the new file was never reported"
    assert str(root / "outside.jpg") in set().union(*batches)


def test_a_photo_in_a_subfolder_is_reported(watcher, tmp_path) -> None:
    root = tmp_path / "Photos"
    (root / "Camera").mkdir(parents=True)
    watcher.watcher.watch([root])

    _jpeg(root / "Camera" / "shot.jpg")

    batches = _drain(watcher.seen)
    assert batches
    assert str(root / "Camera" / "shot.jpg") in set().union(*batches)


def test_a_folder_created_after_the_watch_starts_is_watched_too(
    watcher, tmp_path,
) -> None:
    """A camera app that files shots under this month's folder creates the
    folder first; without picking it up, everything written into it is
    invisible."""
    root = tmp_path / "Photos"
    root.mkdir()
    watcher.watcher.watch([root])
    fresh = root / "2026-08"
    fresh.mkdir()
    _drain(watcher.seen)
    watcher.seen.clear()

    _jpeg(fresh / "shot.jpg")

    batches = _drain(watcher.seen)
    assert batches
    assert str(fresh / "shot.jpg") in set().union(*batches)


def test_a_burst_is_reported_as_one_batch(watcher, tmp_path) -> None:
    """Copying a folder of photos in must cost one index pass and one
    re-render, not one per file."""
    root = tmp_path / "Photos"
    root.mkdir()
    watcher.watcher.watch([root])

    for i in range(5):
        _jpeg(root / f"shot{i}.jpg")

    batches = _drain(watcher.seen)
    assert batches
    assert len(set().union(*batches)) >= 5


def test_hidden_directories_are_not_watched(watcher, tmp_path) -> None:
    root = tmp_path / "Photos"
    (root / ".thumbnails").mkdir(parents=True)
    watcher.watcher.watch([root])

    assert watcher.watcher.watched_count == 1


def test_overlapping_roots_are_watched_once(watcher, tmp_path) -> None:
    """Two configured roots nested in each other must not double every event."""
    root = tmp_path / "Photos"
    inner = root / "Camera"
    inner.mkdir(parents=True)

    watcher.watcher.watch([root, inner])

    assert watcher.watcher.watched_count == 2


def test_the_watch_limit_is_respected(tmp_path) -> None:
    """A library deeper than the inotify quota must degrade to "not watched",
    not to "watches fail at random"."""
    root = tmp_path / "Photos"
    for i in range(6):
        (root / f"folder{i}").mkdir(parents=True)
    made = MediaWatcher(lambda _paths: None, debounce_ms=50, max_watches=3)
    try:
        made.watch([root])
        assert made.watched_count == 3
    finally:
        made.stop()


def test_stopping_ends_the_reports(watcher, tmp_path) -> None:
    root = tmp_path / "Photos"
    root.mkdir()
    watcher.watcher.watch([root])
    watcher.watcher.stop()

    _jpeg(root / "shot.jpg")
    _drain(watcher.seen, timeout=0.5)

    assert watcher.seen == []
    assert watcher.watcher.watched_count == 0


def test_re_watching_replaces_the_previous_roots(watcher, tmp_path) -> None:
    """A folder the user just removed from the settings must stop firing."""
    old_root = tmp_path / "Old"
    new_root = tmp_path / "New"
    old_root.mkdir()
    new_root.mkdir()
    watcher.watcher.watch([old_root])
    watcher.watcher.watch([new_root])

    _jpeg(old_root / "shot.jpg")
    _drain(watcher.seen, timeout=0.5)

    assert watcher.seen == []


def test_a_missing_root_is_skipped(watcher, tmp_path) -> None:
    """An unmounted SD card is a normal state, not an error."""
    watcher.watcher.watch([tmp_path / "not-there"])

    assert watcher.watcher.watched_count == 0


# ---------------------------------------------------------------------------
# How the window uses both
# ---------------------------------------------------------------------------

def _window(library) -> SimpleNamespace:
    """Just the attributes ``_index_paths`` touches."""
    return SimpleNamespace(
        _closing=False,
        settings=SimpleNamespace(
            categories=lambda: library.categories,
            excluded_subtrees=lambda: [],
        ),
        scanner=library.scanner,
        database=library.database,
        refresh=MagicMock(return_value=None),
    )


def test_the_window_indexes_a_reported_path_and_re_renders(library) -> None:
    from muga.app import GalleryWindow

    shot = _jpeg(library.root / "shot.jpg")
    win = _window(library)

    GalleryWindow._index_paths(win, [str(shot)])

    assert library.database.count_media("pictures") == 1
    # The re-render is bounced through the main loop, so run it out.
    GLib.MainContext.default().iteration(False)
    win.refresh.assert_called_once_with(False)


def test_the_window_does_not_re_render_for_nothing(library) -> None:
    """The watcher reports every path under a root — a log file, a partial
    download. Re-rendering on those would jolt the grid while the user reads."""
    from muga.app import GalleryWindow

    noise = library.root / "notes.txt"
    noise.write_text("not a picture")
    win = _window(library)

    GalleryWindow._index_paths(win, [str(noise)])

    GLib.MainContext.default().iteration(False)
    win.refresh.assert_not_called()


def test_the_window_forgets_a_reported_path_that_is_gone(library) -> None:
    from muga.app import GalleryWindow

    shot = _jpeg(library.root / "shot.jpg")
    library.scanner.index_file(shot, library.categories)
    shot.unlink()
    win = _window(library)

    GalleryWindow._index_paths(win, [str(shot)])

    assert library.database.count_media("pictures") == 0
