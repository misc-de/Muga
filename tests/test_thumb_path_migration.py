"""Guards for the thumbnail paths the Yaga -> Muga rename left behind.

``migrate_legacy_dirs`` renames ~/.cache/yaga to ~/.cache/muga, and every
cached thumbnail travels with it. ``thumb_path`` in the index, though, stores an
*absolute* path — so on every pre-rename install those rows kept naming a
directory that no longer exists. The grid asked for a file that was not there
and drew the placeholder, while the real thumbnail sat next door under the new
name.

Local items recover on their own: the scanner recomputes the path, finds no
file, re-decodes and writes the row back. Nextcloud items never did — the folder
sync skipped anything that already carried a thumb_path — so an entire remote
library stayed on placeholder tiles for good. That is what these tests pin down.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from muga.database import Database


def _thumb(cache_root: Path, name: str) -> Path:
    """Create a thumbnail file under *cache_root* and return its path."""
    target = cache_root / "thumbnails" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8thumb")
    return target


def _point_database_at(monkeypatch: pytest.MonkeyPatch, legacy: Path, current: Path) -> None:
    """Aim the migration's two prefixes at test directories.

    Patched on ``muga.database`` rather than ``muga.config``: the module imports
    the two names by value, so patching the source module would not reach it.
    """
    import muga.database as database

    monkeypatch.setattr(database, "LEGACY_CACHE_DIR", legacy)
    monkeypatch.setattr(database, "CACHE_DIR", current)


def _pre_rename_index(db_path: Path, rows: list[tuple[str, str]]) -> None:
    """Write a v6 index whose rows carry *rows* as (path, thumb_path).

    Built by hand at user_version 6 so opening it exercises exactly the v7 step,
    the way an index written by 0.3.1 does.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE media (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            category TEXT NOT NULL,
            media_type TEXT NOT NULL,
            folder TEXT NOT NULL,
            name TEXT NOT NULL,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            thumb_path TEXT,
            seen_at REAL NOT NULL,
            UNIQUE(path, category)
        );
        PRAGMA user_version = 6;
        """
    )
    conn.executemany(
        "INSERT INTO media(path, category, media_type, folder, name, mtime, size,"
        " thumb_path, seen_at) VALUES(?, 'nextcloud', 'image', '/', 'x.jpg', 1, 1, ?, 1)",
        rows,
    )
    conn.commit()
    conn.close()


def _stored_thumbs(db: Database) -> list[str]:
    return [r[0] for r in db.wconn.execute(
        "SELECT thumb_path FROM media ORDER BY id").fetchall()]


def test_the_rename_repoints_thumbnails_in_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-visible symptom: a Nextcloud library of placeholder tiles whose
    thumbnails are all sitting on disk under the new cache name."""
    legacy, current = tmp_path / "cache" / "yaga", tmp_path / "cache" / "muga"
    moved = _thumb(current, "nextcloud/album_photo.jpg")
    _point_database_at(monkeypatch, legacy, current)

    db_path = tmp_path / "muga.sqlite3"
    _pre_rename_index(db_path, [
        ("nextcloud://server/album/photo.jpg",
         str(legacy / "thumbnails" / "nextcloud" / "album_photo.jpg")),
    ])

    db = Database(db_path)

    assert _stored_thumbs(db) == [str(moved)]
    assert Path(_stored_thumbs(db)[0]).is_file(), "the row still names a file that is not there"


def test_paths_outside_the_old_cache_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the rename's own prefix may be rewritten. A thumbnail a user placed
    elsewhere — or one already pointing at the new directory — must survive."""
    legacy, current = tmp_path / "cache" / "yaga", tmp_path / "cache" / "muga"
    _point_database_at(monkeypatch, legacy, current)
    elsewhere = "/srv/shared/thumbs/holiday.jpg"
    already_new = str(current / "thumbnails" / "local.jpg")

    db_path = tmp_path / "muga.sqlite3"
    _pre_rename_index(db_path, [
        ("nextcloud://server/a.jpg", elsewhere),
        ("nextcloud://server/b.jpg", already_new),
    ])

    db = Database(db_path)

    assert _stored_thumbs(db) == [elsewhere, already_new]


def test_a_cache_path_containing_an_underscore_is_not_corrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefix test uses substr, not LIKE. Under LIKE the "_" in a home
    directory is a single-character wildcard, so a same-length path that is not
    under the old cache at all would match — and the rewrite would mangle it."""
    legacy = tmp_path / "my_cache" / "yaga"
    current = tmp_path / "my_cache" / "muga"
    _point_database_at(monkeypatch, legacy, current)
    # Same length as the legacy prefix, differing only where LIKE's "_" would
    # happily match anything.
    lookalike = str(legacy).replace("my_cache", "myXcache") + "/thumbnails/a.jpg"

    db_path = tmp_path / "muga.sqlite3"
    _pre_rename_index(db_path, [
        ("nextcloud://server/a.jpg", lookalike),
        ("nextcloud://server/b.jpg", str(legacy / "thumbnails" / "b.jpg")),
    ])

    db = Database(db_path)

    stored = _stored_thumbs(db)
    assert stored[0] == lookalike, "an unrelated path was rewritten"
    assert stored[1] == str(current / "thumbnails" / "b.jpg")


def test_the_migration_is_recorded_and_does_not_run_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second open must not touch rows again — and a row that legitimately
    names the old directory later (a user restoring a backup) is not the
    migration's business any more."""
    legacy, current = tmp_path / "cache" / "yaga", tmp_path / "cache" / "muga"
    _point_database_at(monkeypatch, legacy, current)

    db_path = tmp_path / "muga.sqlite3"
    _pre_rename_index(db_path, [
        ("nextcloud://server/a.jpg", str(legacy / "thumbnails" / "a.jpg")),
    ])

    db = Database(db_path)
    assert db.wconn.execute("PRAGMA user_version").fetchone()[0] >= 7

    db.set_thumb("nextcloud://server/a.jpg", str(legacy / "thumbnails" / "again.jpg"), "nextcloud")
    db.commit()
    reopened = Database(db_path)

    assert _stored_thumbs(reopened) == [str(legacy / "thumbnails" / "again.jpg")]


def test_a_fresh_index_is_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A first-ever install has no legacy rows; the step must be a no-op rather
    than something a new user can notice."""
    legacy, current = tmp_path / "cache" / "yaga", tmp_path / "cache" / "muga"
    _point_database_at(monkeypatch, legacy, current)

    db = Database(tmp_path / "muga.sqlite3")

    assert db.wconn.execute("PRAGMA user_version").fetchone()[0] >= 7
    assert db.count_media("nextcloud") == 0
