"""Tests for the scanner's Nextcloud side.

The structure scan is the part that decides what the gallery shows for a
remote library, and it carries three decisions worth pinning:

  * an unchanged remote must not be re-upserted. Every row rewritten fires the
    FTS triggers, and doing that for a whole library on every sync is the
    difference between a smooth scroll and visible stutter.
  * a vanished Photos folder prunes; a failed *connection* must not. The
    difference matters because pruning on a timeout would empty the gallery
    every time the network hiccups.
  * writes go out in batches with a real preemption gap, so the main thread
    can get its reads in between.

A fake client stands in for the WebDAV layer — the shapes it returns are the
ones list_files really produces (see tests/test_nextcloud_client.py, which
checks them against recorded PROPFIND bodies).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

scanner_mod = pytest.importorskip("muga.scanner")

from muga.database import Database  # noqa: E402
from muga.nextcloud import nc_path  # noqa: E402
from muga.thumbnails import Thumbnailer  # noqa: E402

DAV_ROOT = "/remote.php/dav/files/alice"


class FakeClient:
    """Stands in for NextcloudClient with the shapes list_files returns."""

    def __init__(self, files=(), *, thumbs=None, list_error=None) -> None:
        self.dav_root = DAV_ROOT
        self._files = list(files)
        self._thumbs = thumbs if thumbs is not None else {}
        self._list_error = list_error
        self.listed: list[str] = []
        self.thumbed: list[str] = []
        self.thumbed_mtimes: list[float | None] = []
        self.downloaded: list[str] = []
        self.downloaded_mtimes: list[float | None] = []

    def list_files(self, folder):
        self.listed.append(folder)
        if self._list_error is not None:
            raise self._list_error
        return self._files

    def ensure_thumbnail(self, dav, size=256, remote_mtime=None):
        # remote_mtime is recorded, not acted on: the freshness rule itself is
        # the real client's business (tests/test_thumbnail_freshness.py). What
        # matters here is that the scanner passes the listing's mtime through,
        # since without it a file replaced on the server keeps its cached
        # thumbnail forever.
        self.thumbed.append(dav)
        self.thumbed_mtimes.append(remote_mtime)
        return self._thumbs.get(dav)

    def download_file(self, dav, remote_mtime=None):
        self.downloaded.append(dav)
        self.downloaded_mtimes.append(remote_mtime)
        return f"/cache/{Path(dav).name}"


def _file(name, folder="Photos", *, mtime=1.7e9, size=1000):
    dav = f"{DAV_ROOT}/{folder}/{name}".replace("//", "/")
    return {"dav_path": dav, "name": name, "mtime": mtime, "size": size}


@pytest.fixture
def scanner(tmp_path):
    database = Database(tmp_path / "index.sqlite3")
    return scanner_mod.MediaScanner(database, Thumbnailer()), database


# ---------------------------------------------------------------------------
# Folder derivation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("dav", "expected"),
    [
        (f"{DAV_ROOT}/Photos/a.jpg", "/"),
        (f"{DAV_ROOT}/Photos/2026/a.jpg", "2026"),
        (f"{DAV_ROOT}/Photos/2026/june/a.jpg", "2026/june"),
        (f"{DAV_ROOT}/Elsewhere/a.jpg", "Elsewhere"),
    ],
)
def test_folders_are_rooted_at_the_photos_path(scanner, dav, expected) -> None:
    """The gallery's folder view shows paths relative to the configured Photos
    folder, not the user's whole Nextcloud."""
    sc, _db = scanner
    assert sc._nc_folder(dav, DAV_ROOT + "/", "Photos") == expected


def test_folder_derivation_tolerates_a_slashed_photos_path(scanner) -> None:
    sc, _db = scanner
    assert sc._nc_folder(f"{DAV_ROOT}/Photos/2026/a.jpg", DAV_ROOT + "/", "/Photos/") == "2026"


def test_folder_derivation_without_the_dav_prefix(scanner) -> None:
    """A server can return an href that is not under the expected root."""
    sc, _db = scanner
    assert sc._nc_folder("/other/Photos/a.jpg", DAV_ROOT + "/", "Photos") == "other/Photos"


# ---------------------------------------------------------------------------
# The structure scan
# ---------------------------------------------------------------------------

def _indexed(db):
    """Everything the scan wrote, videos included.

    The plain nextcloud listing is image-only by default — the tab mirrors
    the historic Pictures view — so the scan's own output is read with the
    filter dropped.
    """
    return db.list_media("nextcloud", "newest", media_filter="both")


def test_a_fresh_scan_indexes_every_media_file(scanner) -> None:
    sc, db = scanner
    client = FakeClient([_file("a.jpg"), _file("b.png"), _file("c.mp4")])

    assert sc.scan_nc_structure(client, "Photos") is True

    rows = _indexed(db)
    assert {r.name for r in rows} == {"a.jpg", "b.png", "c.mp4"}
    assert all(r.path.startswith("nextcloud://") for r in rows)


def test_videos_are_indexed_but_not_in_the_default_listing(scanner) -> None:
    """They surface under the Videos tab, which aggregates across sources."""
    sc, db = scanner
    sc.scan_nc_structure(FakeClient([_file("a.jpg"), _file("c.mp4")]), "Photos")

    assert {r.name for r in db.list_media("nextcloud", "newest")} == {"a.jpg"}
    assert {r.name for r in _indexed(db)} == {"a.jpg", "c.mp4"}
    assert "c.mp4" in {r.name for r in db.list_media("videos", "newest")}


def test_non_media_files_are_skipped(scanner) -> None:
    """A Nextcloud Photos folder routinely holds sidecars and stray documents."""
    sc, db = scanner
    client = FakeClient([_file("a.jpg"), _file("notes.txt"), _file("a.jpg.xmp")])
    sc.scan_nc_structure(client, "Photos")
    assert {r.name for r in _indexed(db)} == {"a.jpg"}


def test_media_types_are_recorded(scanner) -> None:
    sc, db = scanner
    client = FakeClient([_file("a.jpg"), _file("c.mp4")])
    sc.scan_nc_structure(client, "Photos")
    kinds = {r.name: r.media_type for r in _indexed(db)}
    assert kinds == {"a.jpg": "image", "c.mp4": "video"}


def test_the_structure_scan_downloads_nothing(scanner) -> None:
    """Thumbnails are fetched on demand as tiles scroll into view; pulling
    them here would make the sync take as long as the library is large."""
    sc, _db = scanner
    client = FakeClient([_file(f"{i}.jpg") for i in range(20)])
    sc.scan_nc_structure(client, "Photos")
    assert client.thumbed == []
    assert client.downloaded == []


def test_an_unchanged_remote_reports_no_change(scanner) -> None:
    """The caller skips the re-render on False — an unchanged startup sync
    used to tear down and rebuild the whole grid."""
    sc, _db = scanner
    files = [_file("a.jpg"), _file("b.jpg")]
    assert sc.scan_nc_structure(FakeClient(files), "Photos") is True
    assert sc.scan_nc_structure(FakeClient(files), "Photos") is False


def test_unchanged_rows_are_not_rewritten(scanner) -> None:
    """Re-upserting fires the FTS triggers for every row."""
    sc, db = scanner
    files = [_file(f"{i}.jpg") for i in range(10)]
    sc.scan_nc_structure(FakeClient(files), "Photos")

    with patch.object(type(db), "upsert_remote_media_bulk") as upsert:
        sc.scan_nc_structure(FakeClient(files), "Photos")
    upsert.assert_not_called()


def test_unchanged_rows_are_still_touched(scanner) -> None:
    """Without the seen_at touch the prune at the end would delete them."""
    sc, db = scanner
    files = [_file(f"{i}.jpg") for i in range(5)]
    sc.scan_nc_structure(FakeClient(files), "Photos")
    sc.scan_nc_structure(FakeClient(files), "Photos")
    assert db.count_media("nextcloud") == 5, "unchanged rows were pruned"


def test_a_changed_size_is_picked_up(scanner) -> None:
    sc, db = scanner
    sc.scan_nc_structure(FakeClient([_file("a.jpg", size=1000)]), "Photos")
    assert sc.scan_nc_structure(FakeClient([_file("a.jpg", size=2000)]), "Photos") is True
    assert _indexed(db)[0].size == 2000


def test_a_changed_mtime_is_picked_up(scanner) -> None:
    sc, db = scanner
    sc.scan_nc_structure(FakeClient([_file("a.jpg", mtime=1.7e9)]), "Photos")
    assert sc.scan_nc_structure(
        FakeClient([_file("a.jpg", mtime=1.8e9)]), "Photos") is True
    assert _indexed(db)[0].mtime == pytest.approx(1.8e9)


def test_a_float_rounding_wobble_is_not_a_change(scanner) -> None:
    """mtime round-trips through SQLite as a REAL; a sub-microsecond
    difference is storage noise, not an edit."""
    sc, _db = scanner
    sc.scan_nc_structure(FakeClient([_file("a.jpg", mtime=1.7e9)]), "Photos")
    assert sc.scan_nc_structure(
        FakeClient([_file("a.jpg", mtime=1.7e9 + 1e-9)]), "Photos") is False


def test_removed_files_are_pruned(scanner) -> None:
    sc, db = scanner
    sc.scan_nc_structure(FakeClient([_file("a.jpg"), _file("b.jpg")]), "Photos")
    assert sc.scan_nc_structure(FakeClient([_file("a.jpg")]), "Photos") is True
    assert {r.name for r in _indexed(db)} == {"a.jpg"}


def test_files_are_grouped_by_folder(scanner) -> None:
    sc, db = scanner
    client = FakeClient([
        _file("a.jpg", folder="Photos"),
        _file("b.jpg", folder="Photos/2026"),
        _file("c.jpg", folder="Photos/2026/june"),
    ])
    sc.scan_nc_structure(client, "Photos")
    folders = {r.name: r.folder for r in _indexed(db)}
    assert folders == {"a.jpg": "/", "b.jpg": "2026", "c.jpg": "2026/june"}


def test_writes_go_out_in_batches(scanner) -> None:
    """One lock acquisition per row starves the main thread's reads."""
    sc, db = scanner
    client = FakeClient([_file(f"{i}.jpg") for i in range(250)])

    with patch.object(type(db), "upsert_remote_media_bulk",
                      side_effect=db.upsert_remote_media_bulk) as bulk, \
         patch.object(scanner_mod.time, "sleep"):
        sc.scan_nc_structure(client, "Photos")

    assert bulk.call_count >= 2, "everything went out in one batch"
    assert bulk.call_count <= 10, f"{bulk.call_count} batches for 250 files"
    assert db.count_media("nextcloud") == 250


def test_batches_yield_to_the_main_thread(scanner) -> None:
    """The sleep is a real preemption window, not a rate limit — without it
    the sync holds the DB lock back-to-back and the UI stutters."""
    sc, _db = scanner
    client = FakeClient([_file(f"{i}.jpg") for i in range(250)])
    with patch.object(scanner_mod.time, "sleep") as sleep:
        sc.scan_nc_structure(client, "Photos")
    assert sleep.called
    assert all(0 < c[0][0] <= 0.05 for c in sleep.call_args_list), (
        "the yield is long enough to slow the sync down")


def test_a_vanished_photos_folder_prunes_everything(scanner) -> None:
    """The folder is genuinely gone, so the cached rows are stale."""
    sc, db = scanner
    sc.scan_nc_structure(FakeClient([_file("a.jpg")]), "Photos")

    gone = FakeClient(list_error=FileNotFoundError("404"))
    assert sc.scan_nc_structure(gone, "Photos") is True

    assert db.count_media("nextcloud") == 0
    assert sc.missing_root.get("nextcloud") == "Photos"


def test_a_vanished_folder_with_nothing_indexed_reports_no_change(scanner) -> None:
    sc, _db = scanner
    gone = FakeClient(list_error=FileNotFoundError("404"))
    assert sc.scan_nc_structure(gone, "Photos") is False


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("refused"),
        TimeoutError("timed out"),
        PermissionError("401"),
        OSError("TLS handshake failed"),
    ],
)
def test_a_broken_connection_is_raised_not_swallowed(scanner, error) -> None:
    """Returning False here left the UI spinning with no feedback while the
    user had no idea the connection had broken — and pruning on it would
    empty the gallery on every network hiccup."""
    sc, db = scanner
    sc.scan_nc_structure(FakeClient([_file("a.jpg")]), "Photos")

    with pytest.raises(type(error)):
        sc.scan_nc_structure(FakeClient(list_error=error), "Photos")

    assert db.count_media("nextcloud") == 1, "a connection failure pruned the index"


def test_a_recovered_connection_clears_the_missing_flag(scanner) -> None:
    sc, _db = scanner
    sc.scan_nc_structure(FakeClient(list_error=FileNotFoundError()), "Photos")
    assert "nextcloud" in sc.missing_root

    sc.scan_nc_structure(FakeClient([_file("a.jpg")]), "Photos")
    assert "nextcloud" not in sc.missing_root


def test_an_empty_remote_folder(scanner) -> None:
    sc, db = scanner
    assert sc.scan_nc_structure(FakeClient([]), "Photos") is False
    assert db.count_media("nextcloud") == 0


# ---------------------------------------------------------------------------
# On-demand folder thumbnails
# ---------------------------------------------------------------------------

def test_folder_thumbs_fetch_only_what_is_missing(scanner, tmp_path) -> None:
    sc, db = scanner
    files = [_file("a.jpg", folder="Photos/2026"), _file("b.jpg", folder="Photos/2026")]
    sc.scan_nc_structure(FakeClient(files), "Photos")
    # A real file, not just a recorded path: "already has a thumbnail" now means
    # the thumbnail is actually there. See the test below for why.
    have = tmp_path / "a.jpg"
    have.write_bytes(b"\xff\xd8thumb")
    db.set_thumb(nc_path(files[0]["dav_path"]), str(have), "nextcloud")
    db.commit()

    client = FakeClient(files, thumbs={files[1]["dav_path"]: "/cache/b.jpg"})
    seen = []
    sc.load_nc_folder_thumbs(client, "2026", lambda p, t: seen.append((p, t)))

    assert client.thumbed == [files[1]["dav_path"]], "re-fetched a thumbnail it had"
    assert len(seen) == 1


def test_folder_thumbs_refetch_when_the_recorded_file_is_gone(scanner) -> None:
    """A thumb_path whose file has vanished — a cache wipe, an eviction, the
    cache directory renamed under the app's feet — used to be read as "this one
    is done" and skipped for ever, leaving the tile on its placeholder with
    nothing in the app able to recover it."""
    sc, db = scanner
    files = [_file("a.jpg", folder="Photos/2026")]
    sc.scan_nc_structure(FakeClient(files), "Photos")
    db.set_thumb(nc_path(files[0]["dav_path"]), "/gone/yaga/thumbnails/a.jpg", "nextcloud")
    db.commit()

    client = FakeClient(files, thumbs={files[0]["dav_path"]: "/cache/a.jpg"})
    seen = []
    sc.load_nc_folder_thumbs(client, "2026", lambda p, t: seen.append((p, t)))

    assert client.thumbed == [files[0]["dav_path"]], "the placeholder would never recover"
    assert seen == [(nc_path(files[0]["dav_path"]), "/cache/a.jpg")]


def test_folder_thumbs_report_each_arrival(scanner) -> None:
    """The callback is what turns the tile without a full re-render."""
    sc, db = scanner
    files = [_file(f"{i}.jpg", folder="Photos/2026") for i in range(3)]
    sc.scan_nc_structure(FakeClient(files), "Photos")
    client = FakeClient(files, thumbs={f["dav_path"]: f"/cache/{f['name']}" for f in files})

    seen = []
    sc.load_nc_folder_thumbs(client, "2026", lambda p, t: seen.append((p, t)))
    db.commit()

    assert len(seen) == 3
    assert {db.get_media_by_path(p, "nextcloud").thumb_path for p, _t in seen} == {
        f"/cache/{f['name']}" for f in files}


def test_folder_thumbs_skip_a_server_that_has_none(scanner) -> None:
    """A 404 on a preview means that one file has none, not that the sync
    should stop."""
    sc, _db = scanner
    files = [_file(f"{i}.jpg", folder="Photos/2026") for i in range(3)]
    sc.scan_nc_structure(FakeClient(files), "Photos")

    seen = []
    sc.load_nc_folder_thumbs(FakeClient(files, thumbs={}), "2026",
                             lambda p, t: seen.append(p))
    assert seen == []


def test_folder_thumbs_on_an_empty_folder(scanner) -> None:
    sc, _db = scanner
    sc.load_nc_folder_thumbs(FakeClient([]), "empty", lambda p, t: None)


# ---------------------------------------------------------------------------
# The eager variant
# ---------------------------------------------------------------------------

def test_the_eager_scan_fetches_thumbnails(scanner) -> None:
    """_scan_nextcloud is the thumbnail-fetching variant, kept for the
    "download originals" setting.

    It leaves its transaction open — scan() commits once at the end for the
    whole sweep — so the test commits before reading back.
    """
    sc, db = scanner
    files = [_file("a.jpg"), _file("b.jpg")]
    client = FakeClient(files, thumbs={f["dav_path"]: f"/cache/{f['name']}" for f in files})

    sc._scan_nextcloud(client, "Photos", thumbnail_only=True)
    db.commit()

    assert len(client.thumbed) == 2
    assert client.downloaded == [], "thumbnail_only still downloaded originals"
    assert db.count_media("nextcloud") == 2


def test_the_eager_scan_leaves_committing_to_its_caller(scanner) -> None:
    """One commit per sweep rather than per row; a commit here would undo
    that batching."""
    sc, db = scanner
    sc._scan_nextcloud(FakeClient([_file("a.jpg")]), "Photos")
    assert db.count_media("nextcloud") == 0, "the eager scan committed on its own"
    db.commit()
    assert db.count_media("nextcloud") == 1


def test_the_eager_scan_downloads_when_asked(scanner) -> None:
    sc, _db = scanner
    files = [_file("a.jpg"), _file("b.jpg")]
    client = FakeClient(files)
    sc._scan_nextcloud(client, "Photos", thumbnail_only=False)
    assert len(client.downloaded) == 2


def test_the_eager_scan_forwards_the_server_mtime(scanner) -> None:
    """Both Nextcloud caches key on the DAV path, so a file replaced on the
    server keeps its cache filename. The listing already carries the server's
    mtime — dropping it here is what would leave the pre-change thumbnail and
    the pre-change download in place for good."""
    sc, _db = scanner
    files = [_file("a.jpg", mtime=1_700_000_000.0), _file("b.jpg", mtime=1_700_000_900.0)]
    client = FakeClient(files)

    sc._scan_nextcloud(client, "Photos", thumbnail_only=False)

    assert client.thumbed_mtimes == [1_700_000_000.0, 1_700_000_900.0]
    assert client.downloaded_mtimes == [1_700_000_000.0, 1_700_000_900.0]


def test_the_eager_scan_skips_non_media(scanner) -> None:
    sc, db = scanner
    client = FakeClient([_file("a.jpg"), _file("readme.txt")])
    sc._scan_nextcloud(client, "Photos")
    db.commit()
    assert db.count_media("nextcloud") == 1


# ---------------------------------------------------------------------------
# Server-supplied checksums
# ---------------------------------------------------------------------------

def test_checksum_parsing_picks_the_strongest_offered() -> None:
    import xml.etree.ElementTree as ET

    from muga.nextcloud import _parse_checksum

    def prop(inner):
        return ET.fromstring(
            f'<prop xmlns:oc="http://owncloud.org/ns">{inner}</prop>'
        )

    assert _parse_checksum(prop(
        "<oc:checksums><oc:checksum>SHA1:ABC123 MD5:def456</oc:checksum></oc:checksums>"
    )) == "sha1:abc123"
    assert _parse_checksum(prop(
        "<oc:checksums><oc:checksum>SHA256:aa SHA1:bb</oc:checksum></oc:checksums>"
    )) == "sha256:aa"


def test_a_missing_or_empty_checksum_is_none() -> None:
    """Both are normal: most files on most servers carry none, and the caller
    simply falls back to comparing names and sizes."""
    import xml.etree.ElementTree as ET

    from muga.nextcloud import _parse_checksum

    def prop(inner):
        return ET.fromstring(
            f'<prop xmlns:oc="http://owncloud.org/ns">{inner}</prop>'
        )

    assert _parse_checksum(prop("")) is None
    assert _parse_checksum(prop("<oc:checksums/>")) is None
    assert _parse_checksum(prop("<oc:checksums></oc:checksums>")) is None
    assert _parse_checksum(prop(
        "<oc:checksums><oc:checksum>NONSENSE</oc:checksum></oc:checksums>"
    )) is None


def test_the_propfind_asks_for_checksums() -> None:
    """Nothing downstream can use them if the request never requests them."""
    import inspect

    from muga import nextcloud

    source = inspect.getsource(nextcloud.NextcloudClient.list_files)
    assert "oc:checksums" in source
    assert 'xmlns:oc="http://owncloud.org/ns"' in source
