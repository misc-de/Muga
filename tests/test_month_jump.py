"""Where the month arrows land.

The gallery's month arrows used to work by finding the next header among the
rows already built and scrolling a measured number of pixels to it. Three
versions of that shipped and all three left the user stranded in the middle of
a long month: inside one, the next header is thousands of photos away and not
loaded at all, and loading it moves the store under the very widgets the
measurement is taken from.

The jump is a load now. These tests cover the part that decides where it goes
— two SQL queries against a real database — because that is where the answer
comes from, and unlike a scroll position it can be checked exactly.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from muga.database import Database


def _at(year: int, month: int, day: int = 15) -> float:
    """A local-time timestamp inside that month — local, because that is what
    the month headers are cut with."""
    return datetime(year, month, day, 12, 0).timestamp()


def _library(tmp_path: Path, plan: list[tuple[int, int, int]]) -> Database:
    """plan: [(year, month, count), ...] — newest month first is not required;
    the query sorts."""
    db = Database(tmp_path / "test.sqlite3")
    for year, month, count in plan:
        for i in range(count):
            path = tmp_path / f"{year}-{month:02d}-{i:04d}.jpg"
            path.write_bytes(b"x")
            db.upsert_media(path=path, category="photos", media_type="image",
                            folder="", thumb_path=None)
            db.wconn.execute(
                "UPDATE media SET mtime = ?, taken_at = ? WHERE path = ?",
                (_at(year, month, 1 + i % 27), _at(year, month, 1 + i % 27), str(path)),
            )
    db.commit()
    return db


NEWEST = dict(sort_mode="newest")


def test_the_next_month_down_is_the_one_just_older(tmp_path: Path) -> None:
    db = _library(tmp_path, [(2026, 3, 5), (2026, 1, 5), (2025, 11, 5)])
    offset, year, month = db.month_jump_target(
        "photos", "newest", None, year=2026, month=3, direction=+1)
    assert (year, month) == (2026, 1), "it skipped a month or invented one"
    assert offset == 5, "the offset is not where January starts"


def test_it_skips_over_months_that_hold_nothing(tmp_path: Path) -> None:
    """February has no photos, so it is not a month you can jump to."""
    db = _library(tmp_path, [(2026, 3, 5), (2025, 12, 3)])
    _offset, year, month = db.month_jump_target(
        "photos", "newest", None, year=2026, month=3, direction=+1)
    assert (year, month) == (2025, 12)


def test_the_offset_counts_everything_that_sorts_ahead(tmp_path: Path) -> None:
    """A long month is exactly the case that broke before: the offset has to
    carry all of it, however many photos that is."""
    db = _library(tmp_path, [(2026, 3, 4), (2026, 2, 250), (2026, 1, 7)])
    offset, year, month = db.month_jump_target(
        "photos", "newest", None, year=2026, month=2, direction=+1)
    assert (year, month) == (2026, 1)
    assert offset == 254, f"January starts at 254, not {offset}"


def test_the_offset_lands_on_the_first_item_of_that_month(tmp_path: Path) -> None:
    """Read the library back at the offset the jump reports: the item there
    has to be the newest one of the month it named."""
    db = _library(tmp_path, [(2026, 3, 4), (2026, 2, 40), (2026, 1, 7)])
    offset, year, month = db.month_jump_target(
        "photos", "newest", None, year=2026, month=3, direction=+1)
    page = db.list_media_paginated("photos", "newest", None, limit=1, offset=offset)
    assert page, "the offset is past the end of the library"
    landed = datetime.fromtimestamp(page[0].display_time)
    assert (landed.year, landed.month) == (year, month) == (2026, 2)


def test_the_arrow_back_up_returns_to_the_newer_month(tmp_path: Path) -> None:
    db = _library(tmp_path, [(2026, 3, 4), (2026, 2, 40), (2026, 1, 7)])
    offset, year, month = db.month_jump_target(
        "photos", "newest", None, year=2026, month=1, direction=-1)
    assert (year, month) == (2026, 2)
    assert offset == 4, "February starts right after March's four photos"


def test_there_is_no_month_past_the_oldest(tmp_path: Path) -> None:
    db = _library(tmp_path, [(2026, 3, 4), (2026, 2, 4)])
    assert db.month_jump_target(
        "photos", "newest", None, year=2026, month=2, direction=+1) is None


def test_there_is_no_month_above_the_newest(tmp_path: Path) -> None:
    db = _library(tmp_path, [(2026, 3, 4), (2026, 2, 4)])
    assert db.month_jump_target(
        "photos", "newest", None, year=2026, month=3, direction=-1) is None


def test_an_oldest_first_sort_walks_the_other_way(tmp_path: Path) -> None:
    """Down the list is older in the newest-first sorts and newer in the
    oldest-first ones; the offsets are counted from the other end too."""
    db = _library(tmp_path, [(2026, 3, 4), (2026, 2, 40), (2026, 1, 7)])
    offset, year, month = db.month_jump_target(
        "photos", "oldest", None, year=2026, month=1, direction=+1)
    assert (year, month) == (2026, 2)
    assert offset == 7, "February follows January's seven photos"

    offset, year, month = db.month_jump_target(
        "photos", "oldest", None, year=2026, month=2, direction=-1)
    assert (year, month) == (2026, 1)
    assert offset == 0


def test_a_year_boundary_is_a_month_boundary(tmp_path: Path) -> None:
    """December to January crosses a year; a boundary computed by adding one
    to the month number would run off the end of it."""
    db = _library(tmp_path, [(2026, 1, 3), (2025, 12, 6)])
    offset, year, month = db.month_jump_target(
        "photos", "newest", None, year=2026, month=1, direction=+1)
    assert (year, month) == (2025, 12)
    assert offset == 3

    offset, year, month = db.month_jump_target(
        "photos", "newest", None, year=2025, month=12, direction=-1)
    assert (year, month) == (2026, 1)
    assert offset == 0


def test_the_file_date_sorts_use_the_file_date(tmp_path: Path) -> None:
    """The "Date (file)" views group by mtime, so their months have to be cut
    with mtime as well — otherwise the jump lands in the month next door."""
    db = _library(tmp_path, [(2026, 3, 4)])
    path = tmp_path / "moved.jpg"
    path.write_bytes(b"x")
    db.upsert_media(path=path, category="photos", media_type="image",
                    folder="", thumb_path=None)
    # Taken in January, copied off the card in March: the two views file it
    # under different months.
    db.wconn.execute(
        "UPDATE media SET mtime = ?, taken_at = ? WHERE path = ?",
        (_at(2026, 3, 2), _at(2026, 1, 9), str(path)))
    db.commit()

    assert db.month_jump_target(
        "photos", "file_newest", None, year=2026, month=3, direction=+1) is None
    _offset, year, month = db.month_jump_target(
        "photos", "newest", None, year=2026, month=3, direction=+1)
    assert (year, month) == (2026, 1)


def test_a_month_at_the_turn_of_midnight_stays_in_its_month(tmp_path: Path) -> None:
    """Boundaries are local time. In UTC a photo taken at 00:30 on the first
    belongs to the month before, and the jump would count it on the wrong
    side."""
    db = Database(tmp_path / "test.sqlite3")
    for name, when in (("early.jpg", datetime(2026, 3, 1, 0, 30)),
                       ("late.jpg", datetime(2026, 2, 28, 23, 30))):
        path = tmp_path / name
        path.write_bytes(b"x")
        db.upsert_media(path=path, category="photos", media_type="image",
                        folder="", thumb_path=None)
        db.wconn.execute(
            "UPDATE media SET mtime = ?, taken_at = ? WHERE path = ?",
            (when.timestamp(), when.timestamp(), str(path)))
    db.commit()

    offset, year, month = db.month_jump_target(
        "photos", "newest", None, year=2026, month=3, direction=+1)
    assert (year, month) == (2026, 2)
    assert offset == 1, "the photo from just after midnight was counted as February"
