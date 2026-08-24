"""Guards against the Overview showing the same file twice.

`media` is unique on (path, category), so one photo can hold several rows — and
that is deliberate: a file inside two overlapping media folders has to appear in
each folder's own tab. The aggregate views (Overview, Videos) select *across*
categories, though, and there the extra rows are the same picture again: a
duplicate tile in the grid, and a folder tile whose count is inflated by copies
that opening it never shows.

Everything the aggregate drives — the grid, its pagination, the total count and
the folder listing — has to agree on one row per file, or the tile count stops
matching the tiles.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from muga.database import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "index.sqlite3")


def _add(db: Database, tmp_path: Path, name: str, *, category: str,
         folder: str = "/", media_type: str = "image") -> Path:
    """Index *name* under *category*. The file itself is shared between calls,
    which is exactly the situation: one file, several categories."""
    f = tmp_path / name
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\xff\xd8data")
    db.upsert_media(path=f, category=category, media_type=media_type,
                    folder=folder, thumb_path=None)
    return f


def _paths(items) -> list[str]:
    return [i.path for i in items]


def test_a_file_in_two_media_folders_appears_once(db: Database, tmp_path: Path) -> None:
    """The reported symptom: two overlapping extra locations put the same photo
    in the index twice, and Overview drew it twice."""
    shared = _add(db, tmp_path, "Musik/cover.jpg", category="location:0", folder="Musik")
    _add(db, tmp_path, "Musik/cover.jpg", category="location:1", folder="Musik")
    db.commit()

    overview = db.list_media("pictures")

    assert _paths(overview) == [str(shared)]
    assert db.count_media("pictures") == 1, "the count would disagree with the grid"


def test_each_folder_tab_still_shows_its_own_copy(db: Database, tmp_path: Path) -> None:
    """Deduping is only the aggregate's business. A folder's own tab must stay
    complete — that is why the second row exists in the first place."""
    shared = _add(db, tmp_path, "Musik/cover.jpg", category="location:0", folder="Musik")
    _add(db, tmp_path, "Musik/cover.jpg", category="location:1", folder="Musik")
    db.commit()

    assert _paths(db.list_media("location:0")) == [str(shared)]
    assert _paths(db.list_media("location:1")) == [str(shared)]


def test_pagination_does_not_hand_back_the_same_photo_twice(
    db: Database, tmp_path: Path,
) -> None:
    """Pages are cut with LIMIT/OFFSET over the same predicate. If the duplicate
    survived there, a page would carry the picture twice and the last page would
    run past the end of the deduped count."""
    for i in range(4):
        _add(db, tmp_path, f"Musik/{i}.jpg", category="location:0", folder="Musik")
        _add(db, tmp_path, f"Musik/{i}.jpg", category="location:1", folder="Musik")
    db.commit()

    total = db.count_media("pictures")
    seen: list[str] = []
    for offset in range(0, total, 2):
        seen += _paths(db.list_media_paginated("pictures", limit=2, offset=offset))

    assert total == 4
    assert len(seen) == len(set(seen)) == 4


def test_a_folder_tile_counts_files_not_rows(db: Database, tmp_path: Path) -> None:
    """A folder holding ten pictures under two categories used to read as
    twenty — a number the folder could never show when opened."""
    for i in range(10):
        _add(db, tmp_path, f"Artwork/{i}.jpg", category="location:0", folder="Artwork")
        _add(db, tmp_path, f"Artwork/{i}.jpg", category="location:1", folder="Artwork")
    db.commit()

    children = {name: count for name, count, _thumbs in db.child_folders("pictures", None)}

    assert children == {"Artwork": 10}
    assert len(db.list_media("pictures", folder="Artwork")) == 10


def test_videos_aggregate_is_deduped_too(db: Database, tmp_path: Path) -> None:
    """Videos aggregates across every category the same way Overview does, so it
    carries the same defect — a fixed Overview beside a doubled Videos tab would
    just move the confusion."""
    clip = _add(db, tmp_path, "Clips/a.mp4", category="location:0",
                folder="Clips", media_type="video")
    _add(db, tmp_path, "Clips/a.mp4", category="location:1",
         folder="Clips", media_type="video")
    db.commit()

    assert _paths(db.list_media("videos")) == [str(clip)]
    assert db.count_media("videos") == 1


def test_a_collision_outside_the_filter_keeps_its_row(db: Database, tmp_path: Path) -> None:
    """The subquery repeats the filter instead of deduping the whole table.

    A file indexed as an image in one place and (wrongly, or as a sidecar) under
    a video-only category elsewhere must still appear in the image view. Deduping
    globally could hand the surviving id to the row the filter excludes, and the
    picture would vanish from Overview altogether.
    """
    photo = _add(db, tmp_path, "Mixed/clip.jpg", category="location:0",
                 folder="Mixed", media_type="video")
    _add(db, tmp_path, "Mixed/clip.jpg", category="location:1",
         folder="Mixed", media_type="image")
    db.commit()

    assert _paths(db.list_media("pictures")) == [str(photo)], "the image view lost the file"
    assert _paths(db.list_media("videos")) == [str(photo)]


def test_distinct_files_are_never_collapsed(db: Database, tmp_path: Path) -> None:
    """Guards the obvious over-correction: deduping by path must not merge two
    different photos that happen to share a folder or a name."""
    a = _add(db, tmp_path, "Trip/one.jpg", category="photos", folder="Trip")
    b = _add(db, tmp_path, "Trip/two.jpg", category="photos", folder="Trip")
    c = _add(db, tmp_path, "Other/one.jpg", category="location:0", folder="Other")
    db.commit()

    assert sorted(_paths(db.list_media("pictures"))) == sorted([str(a), str(b), str(c)])
    assert db.count_media("pictures") == 3


def _indexes(db: Database) -> set[str]:
    return {r[0] for r in db.wconn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='media'")}


def test_the_dedup_subquery_has_an_index(db: Database) -> None:
    """Without (media_type, path) SQLite sorts the whole filtered set into a
    temp B-tree to group by path. On the 12k-row library this was measured on,
    that was 42 ms per Overview count instead of 24 — every render, on a
    phone."""
    assert "idx_media_type_path" in _indexes(db)


def test_an_older_database_gains_the_index(tmp_path: Path) -> None:
    """A pre-v8 index only gets it through the migration, not the schema."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE media (
            id INTEGER PRIMARY KEY, path TEXT NOT NULL, category TEXT NOT NULL,
            media_type TEXT NOT NULL, folder TEXT NOT NULL, name TEXT NOT NULL,
            mtime REAL NOT NULL, size INTEGER NOT NULL, thumb_path TEXT,
            seen_at REAL NOT NULL, UNIQUE(path, category)
        );
        PRAGMA user_version = 7;
        """
    )
    conn.commit()
    conn.close()

    assert "idx_media_type_path" in _indexes(Database(path))
