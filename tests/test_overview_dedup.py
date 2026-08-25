"""The Overview shows a photo once, however many places it is filed in.

Two things produce repeats, and neither is a duplicate *path*, which is all
the existing dedup could see:

* a picture that exists both on this device and on the Nextcloud server;
* the same picture filed into several folders on the server itself — by far
  the common case. On the user's phone, 277 of 12k pictures were affected, one
  shot sitting in Familie/2023/Grillen, in Familie/2023 and in
  Public/20230506: three rows, three paths, one photo.

Name plus byte size is the key. A copy is the same bytes under the same name;
an edited version has a different size and stays visible as the separate file
it is.
"""

from __future__ import annotations

import time

import pytest

from muga.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "index.sqlite3")


def _add(db, path, category, name, size, *, folder="/f", mtime=None,
         media_type="image", checksum=None):
    db.upsert_remote_media(
        path=path, category=category, media_type=media_type, folder=folder,
        name=name, mtime=mtime if mtime is not None else time.time(),
        size=size, thumb_path=None, checksum=checksum,
    )


def _overview(db, *, include_nc=True, folder=None, media_filter="both"):
    return db.list_media(
        "pictures", folder=folder, include_nc=include_nc, media_filter=media_filter,
    )


# ---------------------------------------------------------------------------
# The reported symptom
# ---------------------------------------------------------------------------

def test_a_photo_in_both_places_is_listed_once(db) -> None:
    _add(db, "/home/u/Photos/IMG_1234.jpg", "photos", "IMG_1234.jpg", 204800)
    _add(db, "nextcloud://cloud/Photos/IMG_1234.jpg", "nextcloud", "IMG_1234.jpg", 204800)
    db.commit()
    assert [i.name for i in _overview(db)] == ["IMG_1234.jpg"]


def test_the_local_copy_is_the_one_kept(db) -> None:
    """It opens without the network and without a download."""
    _add(db, "/home/u/Photos/IMG_1234.jpg", "photos", "IMG_1234.jpg", 204800)
    _add(db, "nextcloud://cloud/Photos/IMG_1234.jpg", "nextcloud", "IMG_1234.jpg", 204800)
    db.commit()
    assert _overview(db)[0].path == "/home/u/Photos/IMG_1234.jpg"


def test_the_local_copy_wins_regardless_of_insertion_order(db) -> None:
    """A library synced before the local folder was added has the remote row
    at the lower id, and lowest-id-wins would pick it."""
    _add(db, "nextcloud://cloud/Photos/first.jpg", "nextcloud", "first.jpg", 900)
    _add(db, "/home/u/Photos/first.jpg", "photos", "first.jpg", 900)
    db.commit()
    assert _overview(db)[0].category == "photos"


def test_the_count_matches_what_is_listed(db) -> None:
    """The folder tiles and the status line read from count_media; a count
    that disagrees with the grid is its own bug."""
    for i in range(4):
        _add(db, f"/home/u/Photos/IMG_{i}.jpg", "photos", f"IMG_{i}.jpg", 1000 + i)
        _add(db, f"nextcloud://cloud/P/IMG_{i}.jpg", "nextcloud", f"IMG_{i}.jpg", 1000 + i)
    db.commit()
    assert db.count_media("pictures", include_nc=True, media_filter="both") == 4
    assert len(_overview(db)) == 4


def test_many_duplicates_collapse(db) -> None:
    """The actual complaint was a screen of repeats, not a single pair."""
    for i in range(50):
        _add(db, f"/home/u/Photos/P{i:03d}.jpg", "photos", f"P{i:03d}.jpg", 5000 + i)
        _add(db, f"nextcloud://cloud/P/P{i:03d}.jpg", "nextcloud", f"P{i:03d}.jpg", 5000 + i)
    db.commit()
    listed = _overview(db)
    assert len(listed) == 50
    assert len({i.name for i in listed}) == 50


def test_the_same_photo_in_three_server_folders_is_listed_once(db) -> None:
    """The shape actually found on the phone: one shot, three folders on the
    server, no local copy involved at all."""
    for folder in ("Familie/2023/Grillen", "Familie/2023", "Public/20230506"):
        _add(db, f"nextcloud://dav/Photos/{folder}/RX103553.JPG", "nextcloud",
             "RX103553.JPG", 11608065, folder=folder)
    db.commit()
    assert len(_overview(db)) == 1


def test_server_side_repeats_collapse_across_a_whole_library(db) -> None:
    for i in range(30):
        for folder in ("Alben/A", "Alben/B", "Jahr/2023"):
            _add(db, f"nextcloud://dav/P/{folder}/S{i:03d}.JPG", "nextcloud",
                 f"S{i:03d}.JPG", 900000 + i, folder=folder)
    db.commit()
    assert len(_overview(db)) == 30


def test_which_server_copy_survives_is_stable(db) -> None:
    """Whichever one it is, it must be the same on the next render — a photo
    that moves between folders as you scroll is its own kind of broken."""
    for folder in ("A", "B", "C"):
        _add(db, f"nextcloud://dav/P/{folder}/x.jpg", "nextcloud", "x.jpg", 4242, folder=folder)
    db.commit()
    first = _overview(db)[0].path
    assert all(_overview(db)[0].path == first for _ in range(3))


# ---------------------------------------------------------------------------
# What must NOT collapse
# ---------------------------------------------------------------------------

def test_an_edited_copy_stays_visible(db) -> None:
    """Same name, different bytes — two different pictures, and hiding one
    would lose the user's edit."""
    _add(db, "/home/u/Photos/edit.jpg", "photos", "edit.jpg", 500)
    _add(db, "nextcloud://cloud/Photos/edit.jpg", "nextcloud", "edit.jpg", 900)
    db.commit()
    assert len(_overview(db)) == 2


def test_a_photo_only_in_the_cloud_is_kept(db) -> None:
    _add(db, "nextcloud://cloud/Photos/remote.jpg", "nextcloud", "remote.jpg", 111)
    db.commit()
    assert [i.name for i in _overview(db)] == ["remote.jpg"]


def test_two_different_local_photos_are_both_kept(db) -> None:
    """Same name in different folders, different sizes — a common shape when
    two cameras both write IMG_0001.jpg."""
    _add(db, "/home/u/Photos/IMG_0001.jpg", "photos", "IMG_0001.jpg", 100, folder="/a")
    _add(db, "/home/u/Screens/IMG_0001.jpg", "screenshots", "IMG_0001.jpg", 200, folder="/b")
    db.commit()
    assert len(_overview(db)) == 2


def test_the_dedup_is_case_insensitive_on_the_name(db) -> None:
    """A server that normalises case must not produce a second tile."""
    _add(db, "/home/u/Photos/Foto.JPG", "photos", "Foto.JPG", 4242)
    _add(db, "nextcloud://cloud/Photos/foto.jpg", "nextcloud", "foto.jpg", 4242)
    db.commit()
    assert len(_overview(db)) == 1


# ---------------------------------------------------------------------------
# Only the Overview, and only when Nextcloud is merged in
# ---------------------------------------------------------------------------

def test_without_nextcloud_the_behaviour_is_unchanged(db) -> None:
    """The old path-based dedup still applies: one file in two local
    categories is one tile, and nothing is compared by name."""
    for category in ("photos", "screenshots"):
        _add(db, "/home/u/Photos/IMG_1234.jpg", category, "IMG_1234.jpg", 204800)
    _add(db, "/home/u/Other/IMG_1234.jpg", "photos", "IMG_1234.jpg", 204800, folder="/o")
    db.commit()
    # Two distinct files that happen to share a name and size: both stay,
    # because without a remote source there is nothing to merge.
    assert len(_overview(db, include_nc=False)) == 2


def test_the_nextcloud_tab_itself_is_untouched(db) -> None:
    """Its own tab has to show what is actually on the server, whether or not
    a local copy exists."""
    _add(db, "/home/u/Photos/IMG_1234.jpg", "photos", "IMG_1234.jpg", 204800)
    _add(db, "nextcloud://cloud/Photos/IMG_1234.jpg", "nextcloud", "IMG_1234.jpg", 204800)
    db.commit()
    assert len(db.list_media("nextcloud")) == 1


def test_a_category_tab_still_shows_its_own_file(db) -> None:
    _add(db, "/home/u/Photos/IMG_1234.jpg", "photos", "IMG_1234.jpg", 204800)
    _add(db, "nextcloud://cloud/Photos/IMG_1234.jpg", "nextcloud", "IMG_1234.jpg", 204800)
    db.commit()
    assert len(db.list_media("photos")) == 1


def test_the_videos_aggregate_is_unaffected(db) -> None:
    """Nextcloud videos are never merged into it, so it keeps the path-based
    dedup it always had."""
    _add(db, "/home/u/V/clip.mp4", "videos", "clip.mp4", 900, media_type="video")
    _add(db, "/home/u/V/clip.mp4", "photos", "clip.mp4", 900, media_type="video")
    db.commit()
    assert len(db.list_media("videos")) == 1


# ---------------------------------------------------------------------------
# Interaction with the rest of the query
# ---------------------------------------------------------------------------

def test_paginated_and_full_listings_agree(db) -> None:
    for i in range(10):
        _add(db, f"/home/u/Photos/P{i}.jpg", "photos", f"P{i}.jpg", 700 + i)
        _add(db, f"nextcloud://cloud/P/P{i}.jpg", "nextcloud", f"P{i}.jpg", 700 + i)
    db.commit()
    full = [i.path for i in _overview(db)]
    paged = [i.path for i in db.list_media_paginated(
        "pictures", "newest", limit=50, include_nc=True, media_filter="both")]
    assert full == paged


def test_search_does_not_return_the_duplicate_either(db) -> None:
    _add(db, "/home/u/Photos/holiday.jpg", "photos", "holiday.jpg", 4096)
    _add(db, "nextcloud://cloud/P/holiday.jpg", "nextcloud", "holiday.jpg", 4096)
    db.commit()
    hits = db.search_media("pictures", "holiday", include_nc=True, media_filter="both")
    assert len(hits) == 1
    assert db.search_media_count(
        "pictures", "holiday", include_nc=True, media_filter="both") == 1


def test_a_folder_filter_keeps_the_remote_copy_of_a_photo_stored_elsewhere(db) -> None:
    """The reason the dedup runs inside the filtered set rather than against
    the whole table: the local twin lives in a folder this view is not showing,
    so suppressing the remote row would make the photo vanish entirely."""
    _add(db, "/home/u/Other/shared.jpg", "photos", "shared.jpg", 8080, folder="/other")
    _add(db, "nextcloud://cloud/P/shared.jpg", "nextcloud", "shared.jpg", 8080, folder="/f")
    db.commit()
    listed = _overview(db, folder="/f")
    assert [i.name for i in listed] == ["shared.jpg"]
    assert listed[0].path.startswith("nextcloud://")


# ---------------------------------------------------------------------------
# Server-supplied checksums, where they exist
# ---------------------------------------------------------------------------

def test_a_renamed_server_copy_is_caught_by_its_checksum(db) -> None:
    """The one thing name-and-size cannot see. Only works when the server
    reports a checksum, which it does for files uploaded with one."""
    _add(db, "nextcloud://dav/P/A/urlaub.jpg", "nextcloud", "urlaub.jpg", 5000,
         checksum="sha1:aaa")
    _add(db, "nextcloud://dav/P/B/ferien.jpg", "nextcloud", "ferien.jpg", 5000,
         checksum="sha1:aaa")
    db.commit()
    assert len(_overview(db)) == 1


def test_different_checksums_are_two_pictures(db) -> None:
    _add(db, "nextcloud://dav/P/A/a.jpg", "nextcloud", "a.jpg", 5000, checksum="sha1:aaa")
    _add(db, "nextcloud://dav/P/B/b.jpg", "nextcloud", "b.jpg", 5000, checksum="sha1:bbb")
    db.commit()
    assert len(_overview(db)) == 2


def test_a_checksum_on_only_one_copy_does_not_split_a_pair(db) -> None:
    """The regression this design exists to avoid: local files are never
    hashed, so a synced photo has a checksum on the server side only. Keying
    on the checksum alone would have shown both copies again."""
    _add(db, "/home/u/Photos/sync.jpg", "photos", "sync.jpg", 900)
    _add(db, "nextcloud://dav/P/sync.jpg", "nextcloud", "sync.jpg", 900, checksum="sha1:bbb")
    db.commit()
    listed = _overview(db)
    assert len(listed) == 1
    assert listed[0].category == "photos", "the local copy should still win"


def test_a_library_without_any_checksums_is_unaffected(db) -> None:
    """Most servers report none. That path has to behave exactly as before."""
    for folder in ("A", "B", "C"):
        _add(db, f"nextcloud://dav/P/{folder}/x.jpg", "nextcloud", "x.jpg", 4242, folder=folder)
    db.commit()
    assert len(_overview(db)) == 1


def test_an_empty_checksum_is_treated_as_absent(db) -> None:
    """Some servers send the element with nothing in it."""
    _add(db, "nextcloud://dav/P/A/y.jpg", "nextcloud", "y.jpg", 77, checksum="")
    _add(db, "nextcloud://dav/P/B/y.jpg", "nextcloud", "y.jpg", 77, checksum="")
    db.commit()
    assert len(_overview(db)) == 1


def test_a_checksum_is_not_erased_by_a_later_sync(db) -> None:
    """A server that stops reporting them — or a re-scan that skipped the
    property — must not throw away what is already known."""
    _add(db, "nextcloud://dav/P/z.jpg", "nextcloud", "z.jpg", 10, checksum="sha1:ccc")
    _add(db, "nextcloud://dav/P/z.jpg", "nextcloud", "z.jpg", 10, checksum=None)
    db.commit()
    row = db.conn.execute(
        "SELECT checksum FROM media WHERE path = ?", ("nextcloud://dav/P/z.jpg",)
    ).fetchone()
    assert row[0] == "sha1:ccc"


def test_a_library_without_checksums_skips_the_second_window(db) -> None:
    """The window is not free — 32 ms of a 91 ms page on a real phone library
    — and with nothing to compare it can only rank every row 1."""
    _add(db, "nextcloud://dav/P/a.jpg", "nextcloud", "a.jpg", 10)
    db.commit()
    assert db.has_checksums() is False
    where, _args = db._build_list_where(
        "pictures", None, True, "both", db.has_checksums())
    assert "rn_sum" not in where


def test_one_checksum_is_enough_to_switch_it_on(db) -> None:
    _add(db, "nextcloud://dav/P/a.jpg", "nextcloud", "a.jpg", 10, checksum="sha1:aaa")
    db.commit()
    assert db.has_checksums() is True
    where, _args = db._build_list_where(
        "pictures", None, True, "both", db.has_checksums())
    assert "rn_sum" in where


def test_the_flag_flips_as_soon_as_one_is_written(db) -> None:
    """Answered once and cached, so a write has to update it — otherwise the
    first sync that brings checksums in would not take effect until restart."""
    _add(db, "nextcloud://dav/P/a.jpg", "nextcloud", "a.jpg", 10)
    db.commit()
    assert db.has_checksums() is False
    _add(db, "nextcloud://dav/P/b.jpg", "nextcloud", "b.jpg", 20, checksum="sha1:bbb")
    db.commit()
    assert db.has_checksums() is True


def test_the_bulk_path_flips_it_too(db) -> None:
    db.upsert_remote_media_bulk([{
        "path": "nextcloud://dav/P/c.jpg", "category": "nextcloud",
        "media_type": "image", "folder": "/f", "name": "c.jpg",
        "mtime": 1.0, "size": 30, "checksum": "sha1:ccc",
    }])
    db.commit()
    assert db.has_checksums() is True
