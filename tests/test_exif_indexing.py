"""EXIF is read during the scan, and date search uses the capture date.

Before this, EXIF only existed for photos the user had opened full-screen —
the viewer's info popover was the only thing that parsed it. Searching for a
camera name therefore answered from whichever handful of files happened to
have been viewed, and "photos from 2019" matched the file's mtime, so a photo
copied off a card today counted as today's.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from muga import exif
from muga.config import Settings
from muga.database import Database
from muga.scanner import MediaScanner
from muga.thumbnails import Thumbnailer

PILImage = pytest.importorskip("PIL.Image")


def _photo(path: Path, *, taken: str | None = None, camera: tuple[str, str] | None = None,
           gps: tuple[float, float] | None = None) -> Path:
    tags = PILImage.Exif()
    if camera:
        tags[271], tags[272] = camera
    if taken:
        tags.get_ifd(0x8769)[36867] = taken
    if gps:
        from PIL.TiffImagePlugin import IFDRational

        def dms(value: float):
            deg = int(value)
            minutes = int((value - deg) * 60)
            seconds = (value - deg - minutes / 60) * 3600
            return (IFDRational(deg, 1), IFDRational(minutes, 1),
                    IFDRational(int(seconds * 10000), 10000))

        tags.get_ifd(0x8825).update({1: "N", 2: dms(gps[0]), 3: "E", 4: dms(gps[1])})
    path.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", (32, 24), (10, 40, 90)).save(path, exif=tags.tobytes())
    return path


@pytest.fixture
def scanned(tmp_path):
    """A scanned library; returns (db, settings, photos_dir)."""
    photos = tmp_path / "Photos"
    photos.mkdir()
    db = Database(tmp_path / "index.sqlite3")

    def _scan():
        MediaScanner(db, Thumbnailer()).scan([("photos", "Photos", str(photos))])

    settings = Settings()
    settings.photos_dir = str(photos)
    settings.videos_dir = ""
    settings.screenshots_dir = ""
    settings.pictures_hidden = True
    return db, settings, photos, _scan


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

def test_capture_time_is_read_from_datetime_original(tmp_path) -> None:
    path = _photo(tmp_path / "a.jpg", taken="2019:07:14 18:30:00")
    info = exif.extract(path)
    assert info.taken_at is not None
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(info.taken_at)) == "2019-07-14 18:30"


def test_capture_time_is_absent_without_a_date(tmp_path) -> None:
    assert exif.extract(_photo(tmp_path / "b.jpg", camera=("Muga", "Cam"))).taken_at is None


def test_a_dead_camera_clock_is_not_a_date(tmp_path) -> None:
    """Bodies with a flat backup battery write zeros; that is not 1899."""
    path = _photo(tmp_path / "c.jpg", taken="0000:00:00 00:00:00")
    assert exif.extract(path).taken_at is None


@pytest.mark.parametrize("junk", ["", "   ", "not a date", "2019", "13:00"])
def test_an_unparseable_date_is_ignored(tmp_path, junk) -> None:
    path = _photo(tmp_path / "d.jpg", taken=junk)
    assert exif.extract(path).taken_at is None


def test_a_non_image_yields_nothing(tmp_path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    info = exif.extract(notes)
    assert not info and info.taken_at is None and info.fields == {}


def test_the_capture_date_is_also_indexed_as_text(tmp_path) -> None:
    """So it reaches full-text search through the same path a filename does."""
    path = _photo(tmp_path / "e.jpg", taken="2019:07:14 18:30:00")
    assert exif.extract(path).fields["Taken"].startswith("2019-07-14")


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def test_scanning_fills_in_exif_without_anyone_opening_the_photo(scanned) -> None:
    db, _settings, photos, scan = scanned
    _photo(photos / "trip.jpg", taken="2019:07:14 18:30:00", camera=("Canon", "EOS R5"))
    scan()
    item = db.get_media_by_path(str(photos / "trip.jpg"))
    assert item is not None and item.taken_at is not None
    assert "Canon" in (db.get_exif_data(item.path, item.category) or "")


def test_a_photo_without_exif_is_marked_as_parsed(scanned) -> None:
    """Otherwise every scan would re-open every screenshot forever, looking
    for EXIF that is never going to be there."""
    db, _settings, photos, scan = scanned
    bare = photos / "screenshot.png"
    bare.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", (20, 20)).save(bare)
    scan()
    index = db.load_scan_index("photos")
    assert index[str(bare)][2] is True, "the file would be re-parsed on every scan"


def test_an_existing_library_gets_exif_on_the_next_scan(scanned) -> None:
    """The upgrade case. Every file is unchanged on disk, so the scanner's skip
    would step over all of them and taken_at would only ever fill in for
    photos added later."""
    db, _settings, photos, scan = scanned
    path = _photo(photos / "old.jpg", taken="2015:05:01 12:00:00")
    scan()

    # Simulate a row written before EXIF was indexed.
    with db.wlock:
        db.wconn.execute(
            "UPDATE media SET taken_at = NULL, exif_data = NULL WHERE path = ?", (str(path),),
        )
        db.wconn.commit()
    assert db.get_media_by_path(str(path)).taken_at is None

    scan()
    assert db.get_media_by_path(str(path)).taken_at is not None


def test_an_already_parsed_file_is_skipped_on_a_re_scan(scanned) -> None:
    db, _settings, photos, scan = scanned
    _photo(photos / "done.jpg", taken="2015:05:01 12:00:00")
    scan()

    opened = []
    original = exif.extract

    def _tracking(path):
        opened.append(str(path))
        return original(path)

    import muga.scanner as scanner_module
    scanner_module.exif.extract = _tracking  # type: ignore[attr-defined]
    try:
        scan()
    finally:
        scanner_module.exif.extract = original  # type: ignore[attr-defined]
    assert opened == [], "an unchanged, already-parsed file was re-opened"


def test_a_changed_file_is_re_parsed(scanned) -> None:
    db, _settings, photos, scan = scanned
    path = _photo(photos / "edited.jpg", taken="2015:05:01 12:00:00")
    scan()
    _photo(path, taken="2020:09:09 09:09:09")
    os.utime(path, (time.time(), time.time()))
    scan()
    taken = db.get_media_by_path(str(path)).taken_at
    assert time.strftime("%Y", time.localtime(taken)) == "2020"


def test_videos_are_not_opened_looking_for_exif(scanned) -> None:
    """Pillow cannot read a container, so it would cost an open and a failed
    decode per clip for a guaranteed empty answer."""
    db, _settings, photos, scan = scanned
    clip = photos / "clip.mp4"
    clip.write_bytes(b"\x00" * 200)
    scan()
    item = db.get_media_by_path(str(clip))
    assert item is not None and item.taken_at is None
    assert db.get_exif_data(item.path, item.category) is None


# ---------------------------------------------------------------------------
# Date search
# ---------------------------------------------------------------------------

def test_search_finds_a_photo_by_the_year_it_was_taken(scanned) -> None:
    """The point of the whole change: the file was written today."""
    db, _settings, photos, scan = scanned
    _photo(photos / "berlin.jpg", taken="2019:07:14 18:30:00")
    scan()
    hits = db.search_media("photos", "2019")
    assert [i.name for i in hits] == ["berlin.jpg"]


def test_a_photo_does_not_answer_to_the_year_it_was_copied(scanned) -> None:
    db, _settings, photos, scan = scanned
    _photo(photos / "berlin.jpg", taken="2019:07:14 18:30:00")
    scan()
    this_year = time.strftime("%Y")
    assert this_year != "2019"
    assert db.search_media("photos", this_year) == []


def test_a_file_without_a_capture_date_still_answers_to_its_mtime(scanned) -> None:
    """mtime stays the fallback — a screenshot has no EXIF and must still be
    findable by date."""
    db, _settings, photos, scan = scanned
    shot = photos / "screenshot.png"
    PILImage.new("RGB", (20, 20)).save(shot)
    scan()
    assert [i.name for i in db.search_media("photos", time.strftime("%Y"))] == ["screenshot.png"]


def test_month_search_uses_the_capture_month(scanned) -> None:
    db, _settings, photos, scan = scanned
    _photo(photos / "xmas.jpg", taken="2021:12:24 09:15:00")
    scan()
    assert [i.name for i in db.search_media("photos", "Dezember")] == ["xmas.jpg"]
    assert [i.name for i in db.search_media("photos", "December")] == ["xmas.jpg"]


def test_year_month_search_uses_the_capture_date(scanned) -> None:
    db, _settings, photos, scan = scanned
    _photo(photos / "xmas.jpg", taken="2021:12:24 09:15:00")
    scan()
    assert [i.name for i in db.search_media("photos", "2021-12")] == ["xmas.jpg"]
    assert db.search_media("photos", "2021-11") == []


def test_camera_and_gps_are_searchable_after_a_plain_scan(scanned) -> None:
    db, _settings, photos, scan = scanned
    _photo(photos / "berlin.jpg", camera=("Canon", "EOS R5"), gps=(52.5200, 13.4050))
    scan()
    assert [i.name for i in db.search_media("photos", "Canon")] == ["berlin.jpg"]
    assert [i.name for i in db.search_media("photos", "52.5200")] == ["berlin.jpg"]


def test_the_capture_date_reaches_mcp(scanned) -> None:
    from muga import mcp_server

    db, settings, photos, scan = scanned
    _photo(photos / "berlin.jpg", taken="2019:07:14 18:30:00")
    scan()
    tools = mcp_server.GalleryTools(db, settings)
    detail = tools.call("get_media", {"path": str(photos / "berlin.jpg")})
    assert detail["taken"].startswith("2019-07-14T18:30")


def test_mcp_omits_the_key_when_there_is_no_capture_date(scanned) -> None:
    """Absent says "unknown" more honestly than a timestamp copied from the
    file's mtime would."""
    from muga import mcp_server

    db, settings, photos, scan = scanned
    shot = photos / "screenshot.png"
    PILImage.new("RGB", (20, 20)).save(shot)
    scan()
    tools = mcp_server.GalleryTools(db, settings)
    assert "taken" not in tools.call("get_media", {"path": str(shot)})
