"""Regression tests for the resilience / security / performance hardening pass.

Each test here pins down a defect that was reproduced against the previous
code, so the notes say what used to happen rather than just what should:

  * rotation overwrote the original in place — an interrupted save destroyed
    the photo, and the fallback encoder then re-read the corpse
  * batch move used Path.rename — it clobbered same-named destination files
    and could not cross a filesystem boundary at all
  * a hand-edited settings.json with a wrong value type, or a damaged media
    index, made the app fail to start
  * a partially applied zip update left a half-old/half-new tree with no
    rollback, and a dead worker thread pinned the UI on "Updating…"
  * a rolled-back remote branch was offered as an "update" (a downgrade)
  * the decompression-bomb cap was set so high it never fired before the OOM
    killer, and every GdkPixbuf path bypassed it entirely
  * a PROPFIND body was read into memory with no ceiling
  * the disk-cache budget defaulted to unlimited, so eviction never ran
  * writes on per-thread connections fought each other and were silently lost
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# 1.  Rotation never leaves a partial file over the user's photo
# ---------------------------------------------------------------------------

def _jpeg(path: Path, size=(400, 300)) -> bytes:
    PILImage = pytest.importorskip("PIL.Image")
    PILImage.new("RGB", size, (200, 30, 30)).save(path, quality=95)
    return path.read_bytes()


def test_rotation_leaves_original_intact_when_every_encoder_fails(tmp_path: Path) -> None:
    """Both encoders failing must be a no-op on disk, not a truncated file."""
    PILImage = pytest.importorskip("PIL.Image")

    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    from muga.viewer import ViewerWindow

    src = tmp_path / "urlaub.jpg"
    original = _jpeg(src)

    def dead_write(fp, *a, **kw):
        # Write a few bytes first — that is what a real ENOSPC looks like.
        with open(fp, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0" + b"\0" * 256)
        raise OSError(errno.ENOSPC, "No space left on device")

    with patch.object(PILImage.Image, "save", lambda self, fp, *a, **kw: dead_write(fp)), \
         patch.object(GdkPixbuf.Pixbuf, "savev", lambda self, fp, *a, **kw: dead_write(fp)):
        ok = ViewerWindow._save_rotation_to_disk(str(src), 90)

    assert ok is False, "a total failure must be reported, not swallowed"
    assert src.read_bytes() == original, "the original photo was modified"
    assert not list(tmp_path.glob("*.tmp")), "temp file left behind"


def test_rotation_falls_back_to_gdkpixbuf_on_an_intact_source(tmp_path: Path) -> None:
    """When PIL fails the fallback must still see the *original* file.

    Previously PIL had already truncated it in place, so the fallback was
    handed a corrupt file and failed too — silently.
    """
    PILImage = pytest.importorskip("PIL.Image")
    from muga.viewer import ViewerWindow

    src = tmp_path / "urlaub.jpg"
    _jpeg(src, (400, 300))

    def dead_write(fp, *a, **kw):
        with open(fp, "wb") as fh:
            fh.write(b"\xff\xd8")
        raise OSError(errno.ENOSPC, "No space left on device")

    with patch.object(PILImage.Image, "save", lambda self, fp, *a, **kw: dead_write(fp)):
        ok = ViewerWindow._save_rotation_to_disk(str(src), 90)

    assert ok is True
    with PILImage.open(src) as img:
        assert img.size == (300, 400), "fallback did not rotate the intact original"


def test_rotation_preserves_file_mode(tmp_path: Path) -> None:
    """os.replace carries the temp file's mode, so it has to be copied over."""
    pytest.importorskip("PIL.Image")
    from muga.viewer import ViewerWindow

    src = tmp_path / "privat.jpg"
    _jpeg(src)
    os.chmod(src, 0o600)
    assert ViewerWindow._save_rotation_to_disk(str(src), 180) is True
    assert src.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# 2.  Moving files never overwrites and never fails on a volume boundary
# ---------------------------------------------------------------------------

def test_move_does_not_clobber_a_same_named_file(tmp_path: Path) -> None:
    from muga.gallery_selection import _move_file_no_clobber

    src, dst = tmp_path / "cam", tmp_path / "album"
    src.mkdir(), dst.mkdir()
    (dst / "IMG_0001.jpg").write_text("wedding 2019")
    (src / "IMG_0001.jpg").write_text("holiday 2026")

    landed = _move_file_no_clobber(src / "IMG_0001.jpg", dst)

    assert landed.name == "IMG_0001 (2).jpg"
    assert (dst / "IMG_0001.jpg").read_text() == "wedding 2019", "existing photo lost"
    assert landed.read_text() == "holiday 2026"
    assert not (src / "IMG_0001.jpg").exists()


def test_move_keeps_finding_free_names(tmp_path: Path) -> None:
    from muga.gallery_selection import _move_file_no_clobber

    src, dst = tmp_path / "cam", tmp_path / "album"
    src.mkdir(), dst.mkdir()
    (dst / "a.jpg").write_text("first")
    names = []
    for n in range(2, 5):
        (src / "a.jpg").write_text(f"copy {n}")
        names.append(_move_file_no_clobber(src / "a.jpg", dst).name)
    assert names == ["a (2).jpg", "a (3).jpg", "a (4).jpg"]


def test_move_crosses_a_filesystem_boundary(tmp_path: Path) -> None:
    """EXDEV used to fail every single file — moving to an SD card never worked."""
    from muga.gallery_selection import _move_file_no_clobber

    src, dst = tmp_path / "cam", tmp_path / "card"
    src.mkdir(), dst.mkdir()
    payload = b"video-bytes" * 500
    (src / "clip.mp4").write_bytes(payload)
    os.chmod(src / "clip.mp4", 0o640)

    def no_link(a, b):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    with patch("os.link", no_link):
        landed = _move_file_no_clobber(src / "clip.mp4", dst)

    assert landed.read_bytes() == payload
    assert landed.stat().st_mode & 0o777 == 0o640, "copystat did not run"
    assert not (src / "clip.mp4").exists()


def test_move_keeps_the_source_when_the_copy_fails(tmp_path: Path) -> None:
    from muga.gallery_selection import _move_file_no_clobber

    src, dst = tmp_path / "cam", tmp_path / "card"
    src.mkdir(), dst.mkdir()
    (src / "important.jpg").write_text("the only copy")

    def no_link(a, b):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def dead_copy(a, b, **kw):
        Path(b).write_bytes(b"half")
        raise OSError(errno.ENOSPC, "No space left on device")

    with patch("os.link", no_link), patch("shutil.copyfile", dead_copy), pytest.raises(OSError):
        _move_file_no_clobber(src / "important.jpg", dst)

    assert (src / "important.jpg").read_text() == "the only copy"
    assert not (dst / "important.jpg").exists(), "partial copy left at the destination"


# ---------------------------------------------------------------------------
# 3.  A damaged settings.json or media index must not stop the app starting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("payload", "note"),
    [
        ('{"grid_columns": "vier"}', "wrong scalar type raised ValueError"),
        ("null", "top-level null raised AttributeError"),
        ('["a", "b"]', "top-level list raised AttributeError"),
        ('{"extra_locations": "not-a-list"}', "wrong container type was accepted as a str"),
        ('{"camera_jpeg_quality": "high"}', "wrong scalar type raised ValueError"),
        ("{truncated", "already handled, kept as a guard"),
    ],
)
def test_settings_load_survives_hand_edits(tmp_path: Path, monkeypatch, payload, note) -> None:
    from muga import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    (cfg / "settings.json").write_text(payload)

    settings = config.Settings.load()  # must not raise — note: %s % note

    assert isinstance(settings.grid_columns, int)
    assert isinstance(settings.extra_locations, list)
    assert isinstance(settings.camera_jpeg_quality, int)


def test_settings_load_keeps_valid_neighbours_of_a_bad_key(tmp_path: Path, monkeypatch) -> None:
    """One bad key must cost that key's value, not the whole file."""
    from muga import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    (cfg / "settings.json").write_text(json.dumps({
        "grid_columns": "vier",       # bad
        "theme": "dark",              # good
        "external_video_player": "mpv",
    }))

    settings = config.Settings.load()
    assert settings.theme == "dark"
    assert settings.external_video_player == "mpv"
    assert settings.grid_columns == config.Settings().grid_columns


def test_settings_save_reports_failure_instead_of_raising(tmp_path: Path, monkeypatch) -> None:
    """save() is called from ~24 UI callbacks; it must not abort them."""
    from muga import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    os.chmod(cfg, 0o500)
    try:
        assert config.Settings().save() is False
    finally:
        os.chmod(cfg, 0o700)


def test_database_rebuilds_itself_when_the_file_is_not_a_database(tmp_path: Path) -> None:
    from muga.database import Database

    path = tmp_path / "muga.sqlite3"
    path.write_text("this is not a database" * 100)

    db = Database(path)  # used to raise sqlite3.DatabaseError and kill startup

    assert db.list_media("photos", "newest") == []
    assert (tmp_path / "muga.sqlite3.corrupt").exists(), "damaged file not kept for inspection"


# ---------------------------------------------------------------------------
# 4.  The zip updater rolls back, refuses bombs and never downgrades
# ---------------------------------------------------------------------------

def _fake_install(root: Path) -> dict:
    files = {
        "muga/__init__.py": 'VERSION = "0.2.0"',
        "muga/app.py": "# old app",
        "muga/viewer.py": "# old viewer",
        "README.md": "old readme",
    }
    (root / "muga").mkdir(parents=True)
    for name, body in files.items():
        (root / name).write_text(body)
    return files


def _fake_update_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Muga-main/muga/__init__.py", 'VERSION = "0.3.0"')
        zf.writestr("Muga-main/muga/app.py", "# new app")
        zf.writestr("Muga-main/muga/viewer.py", "# new viewer")
        zf.writestr("Muga-main/README.md", "new readme")


def test_failed_update_rolls_back_completely(tmp_path: Path, monkeypatch) -> None:
    from muga import updater

    app = tmp_path / "app"
    before = _fake_install(app)
    zip_path = tmp_path / "update.zip"
    _fake_update_zip(zip_path)

    monkeypatch.setattr(updater, "_APP_DIR", app)
    monkeypatch.setattr(
        updater, "_http_download",
        lambda url, dest, timeout=120: bool(shutil.copy2(zip_path, dest)),
    )

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def flaky(src, dst, *a, **kw):
        # Count only writes into the installation, not the backup copies.
        if str(dst).startswith(str(app)):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy2(src, dst, *a, **kw)

    with patch("shutil.copy2", flaky):
        assert updater._apply_zip() is False

    after = {name: (app / name).read_text() for name in before}
    assert after == before, "installation left in a mixed old/new state"


def test_successful_update_applies_every_file(tmp_path: Path, monkeypatch) -> None:
    from muga import updater

    app = tmp_path / "app"
    _fake_install(app)
    zip_path = tmp_path / "update.zip"
    _fake_update_zip(zip_path)

    monkeypatch.setattr(updater, "_APP_DIR", app)
    monkeypatch.setattr(
        updater, "_http_download",
        lambda url, dest, timeout=120: bool(shutil.copy2(zip_path, dest)),
    )

    assert updater._apply_zip() is True
    assert (app / "muga" / "__init__.py").read_text() == 'VERSION = "0.3.0"'
    assert (app / "muga" / "app.py").read_text() == "# new app"


def test_update_refuses_a_decompression_bomb(tmp_path: Path, monkeypatch) -> None:
    from muga import updater

    app = tmp_path / "app"
    _fake_install(app)
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Muga-main/big.bin", b"\0" * (updater._MAX_UNPACKED_BYTES + 1024))

    monkeypatch.setattr(updater, "_APP_DIR", app)
    monkeypatch.setattr(
        updater, "_http_download",
        lambda url, dest, timeout=120: bool(shutil.copy2(bomb, dest)),
    )

    assert updater._apply_zip() is False
    assert (app / "muga" / "__init__.py").read_text() == 'VERSION = "0.2.0"'


@pytest.mark.parametrize(
    ("remote", "local", "expected"),
    [
        ("0.3.0", "0.2.0", True),
        ("0.2.1", "0.2.0", True),
        ("0.10.0", "0.9.0", True),
        ("0.2.0", "0.2.0", False),
        ("0.1.9", "0.2.0", False),   # was offered as an "update" → downgrade
        ("0.2.0", "0.3.0", False),
        (None, "0.2.0", False),
    ],
)
def test_only_a_newer_remote_counts_as_an_update(remote, local, expected) -> None:
    from muga.updater import _is_newer

    assert _is_newer(remote, local) is expected


# ---------------------------------------------------------------------------
# 5.  Decompression bombs are refused on every decode path
# ---------------------------------------------------------------------------

def test_pixel_cap_is_low_enough_to_matter() -> None:
    """The old 200 MP cap only errored past 2× — i.e. after a 1.2 GB
    allocation, which no phone survives. The cap has to bound a decode to
    something the target hardware can actually hold."""
    PILImage = pytest.importorskip("PIL.Image")
    from muga.thumbnails import MAX_IMAGE_PIXELS

    assert PILImage.MAX_IMAGE_PIXELS == MAX_IMAGE_PIXELS
    # 120 MP: covers a 108 MP phone sensor, bounds an RGB decode at ~360 MB.
    assert 100_000_000 <= MAX_IMAGE_PIXELS <= 150_000_000


def test_budget_gate_runs_before_any_decoder(tmp_path: Path) -> None:
    """The gate must not depend on Pillow's warnings filter, which pytest (and
    any other caller of warnings.resetwarnings) clears."""
    import warnings

    from muga.thumbnails import Thumbnailer

    bomb = tmp_path / "bomb.png"
    ihdr = (b"\x00\x00\x00\x0dIHDR"
            + (20000).to_bytes(4, "big") + (20000).to_bytes(4, "big")
            + b"\x08\x00\x00\x00\x00" + b"\x00\x00\x00\x00")
    bomb.write_bytes(b"\x89PNG\r\n\x1a\n" + ihdr)

    with warnings.catch_warnings():
        warnings.resetwarnings()          # wipe the promotion filter entirely
        assert Thumbnailer().ensure_thumbnail(bomb, "image") is None


@pytest.mark.parametrize(
    ("fmt", "suffix", "size"),
    [
        ("PNG", "png", (640, 480)),
        ("JPEG", "jpg", (1920, 1080)),
        ("GIF", "gif", (320, 200)),
        ("BMP", "bmp", (800, 600)),
        ("WEBP", "webp", (1024, 768)),
    ],
)
def test_header_parser_reads_dimensions(tmp_path: Path, fmt, suffix, size) -> None:
    PILImage = pytest.importorskip("PIL.Image")
    from muga.thumbnails import _dimensions_from_header

    path = tmp_path / f"img.{suffix}"
    PILImage.new("RGB", size, (10, 20, 30)).save(path, fmt)
    assert _dimensions_from_header(path) == size


@pytest.mark.parametrize(
    "payload",
    [b"", os.urandom(64), b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF"],
)
def test_header_parser_never_blocks_unreadable_files(tmp_path: Path, payload) -> None:
    """Undecodable input is the caller's problem; this gate only judges size."""
    from muga.thumbnails import _dimensions_from_header, image_within_pixel_budget

    path = tmp_path / "weird.bin"
    path.write_bytes(payload)
    assert _dimensions_from_header(path) is None
    assert image_within_pixel_budget(path) is True


def test_oversized_image_is_refused_without_decoding(tmp_path: Path) -> None:
    """The previous guard called GdkPixbuf.get_file_info, which allocates the
    full pixel buffer before reporting a size — asking the question was itself
    the bomb (1.5 GB on this input)."""
    import resource

    from muga.thumbnails import Thumbnailer, image_within_pixel_budget

    # Hand-built 20000×20000 PNG header: no decoder involved on either side.
    bomb = tmp_path / "bomb.png"
    ihdr = (b"\x00\x00\x00\x0dIHDR"
            + (20000).to_bytes(4, "big") + (20000).to_bytes(4, "big")
            + b"\x08\x00\x00\x00\x00" + b"\x00\x00\x00\x00")
    bomb.write_bytes(b"\x89PNG\r\n\x1a\n" + ihdr)

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert image_within_pixel_budget(bomb) is False
    assert Thumbnailer().ensure_thumbnail(bomb, "image") is None
    grew_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
    assert grew_mb < 64, f"refusing the file allocated {grew_mb:.0f} MB"


def test_normal_photo_still_thumbnails(tmp_path: Path) -> None:
    PILImage = pytest.importorskip("PIL.Image")
    from muga.thumbnails import Thumbnailer

    photo = tmp_path / "photo.jpg"
    PILImage.new("RGB", (4000, 3000), (30, 90, 140)).save(photo, quality=90)
    assert Thumbnailer().ensure_thumbnail(photo, "image") is not None


# ---------------------------------------------------------------------------
# 6.  Nextcloud responses are bounded
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal stand-in for http.client.HTTPResponse."""

    def __init__(self, body: bytes, declared_length: int | None = None) -> None:
        self._body = body
        self._pos = 0
        self._length = declared_length

    def getheader(self, name):
        if name == "Content-Length" and self._length is not None:
            return str(self._length)
        return None

    def read(self, size=None):
        chunk = self._body[self._pos:self._pos + (size or len(self._body))]
        self._pos += len(chunk)
        return chunk


def test_propfind_body_is_capped(monkeypatch) -> None:
    from muga import nextcloud

    monkeypatch.setattr(nextcloud, "_MAX_PROPFIND_BYTES", 4096)
    client = nextcloud.NextcloudClient("https://cloud.example.org", "alice", "pw")

    with pytest.raises(nextcloud.NextcloudResponseTooLarge):
        client._read_bounded(_FakeResponse(b"x" * 9000), 4096, "PROPFIND")

    # Declared length is rejected before a single chunk is buffered.
    with pytest.raises(nextcloud.NextcloudResponseTooLarge):
        client._read_bounded(_FakeResponse(b"", declared_length=99999), 4096, "PROPFIND")


def test_propfind_body_under_the_cap_reads_normally() -> None:
    from muga.nextcloud import NextcloudClient

    client = NextcloudClient("https://cloud.example.org", "alice", "pw")
    body = b"y" * 3000
    assert client._read_bounded(_FakeResponse(body, len(body)), 4096, "PROPFIND") == body


# ---------------------------------------------------------------------------
# 7.  The disk-cache budget is on by default
# ---------------------------------------------------------------------------

def test_cache_budget_defaults_to_a_bounded_value() -> None:
    from muga.config import Settings

    assert Settings().cache_max_mb > 0, "eviction never runs at 0 (= unlimited)"


def test_unlimited_cache_from_an_older_install_is_migrated(tmp_path: Path, monkeypatch) -> None:
    from muga import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    (cfg / "settings.json").write_text(json.dumps({"cache_max_mb": 0}))

    settings = config.Settings.load()
    assert settings.cache_max_mb > 0
    assert settings.cache_budget_migrated is True


def test_deliberately_chosen_unlimited_cache_is_respected(tmp_path: Path, monkeypatch) -> None:
    from muga import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    (cfg / "settings.json").write_text(
        json.dumps({"cache_max_mb": 0, "cache_budget_migrated": True}),
    )
    assert config.Settings.load().cache_max_mb == 0


def test_a_fresh_install_starts_in_english(tmp_path: Path, monkeypatch) -> None:
    """English is the source language, so it is what Muga shows until a
    translation is picked."""
    from muga import config
    from muga.i18n import SOURCE_LANGUAGE

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    assert config.Settings.load().language == SOURCE_LANGUAGE


def test_language_following_the_system_is_migrated_to_english(tmp_path: Path, monkeypatch) -> None:
    """"system" was the old default rather than a choice: it started Muga in
    German on a German desktop. Lift it to English once, and persist that."""
    from muga import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    (cfg / "settings.json").write_text(json.dumps({"language": "system"}))

    settings = config.Settings.load()
    assert settings.language == "en"
    assert settings.language_default_migrated is True
    # Written back, so the next start has nothing left to migrate.
    assert json.loads((cfg / "settings.json").read_text())["language"] == "en"


def test_deliberately_chosen_system_language_is_respected(tmp_path: Path, monkeypatch) -> None:
    """Picking "Use system language" in Settings sets the flag — the migration
    must not override it."""
    from muga import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    (cfg / "settings.json").write_text(
        json.dumps({"language": "system", "language_default_migrated": True}),
    )
    assert config.Settings.load().language == "system"


def test_a_chosen_translation_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    from muga import config

    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    (cfg / "settings.json").write_text(json.dumps({"language": "de"}))
    assert config.Settings.load().language == "de"


# ---------------------------------------------------------------------------
# 8.  Concurrent writes are serialised instead of being lost
# ---------------------------------------------------------------------------

def test_writes_go_through_a_single_connection(tmp_path: Path) -> None:
    """Per-thread write connections deadlocked against each other: most write
    methods leave the transaction open for the scanner to batch, so one thread
    held SQLite's write lock and every other thread's write raised."""
    from muga.database import Database

    db = Database(tmp_path / "t.sqlite3")
    seen = []

    def writer(tag: str) -> None:
        for i in range(200):
            db.upsert_remote_media(
                path=f"nextcloud://{tag}/{i}.jpg", category="nextcloud",
                media_type="image", folder=f"/{tag}", name=f"{i}.jpg",
                mtime=1.7e9, size=1, thumb_path=None,
            )
        seen.append(tag)

    errors: list[Exception] = []

    def guarded(tag: str) -> None:
        try:
            writer(tag)
        except Exception as exc:  # noqa: BLE001 — the assertion is the report
            errors.append(exc)

    threads = [threading.Thread(target=guarded, args=(f"t{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    db.commit()

    assert not errors, f"concurrent writes failed: {errors[:3]}"
    assert len(seen) == 4
    assert db.count_media("nextcloud") == 800, "writes were lost"


def test_reads_and_writes_use_different_connections(tmp_path: Path) -> None:
    from muga.database import Database

    db = Database(tmp_path / "t.sqlite3")
    assert db.conn is not db.wconn


def test_busy_database_is_retried(tmp_path: Path) -> None:
    from muga.database import Database

    db = Database(tmp_path / "t.sqlite3")
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "done"

    assert db._run_write(flaky) == "done"
    assert attempts["n"] == 3


def test_non_busy_errors_are_not_retried(tmp_path: Path) -> None:
    from muga.database import Database

    db = Database(tmp_path / "t.sqlite3")
    attempts = {"n": 0}

    def broken():
        attempts["n"] += 1
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError):
        db._run_write(broken)
    assert attempts["n"] == 1, "a genuine SQL error must fail immediately"


# ---------------------------------------------------------------------------
# The version is stated in three places
# ---------------------------------------------------------------------------

def _declared_versions() -> dict[str, str]:
    import re
    import tomllib

    root = Path(__file__).resolve().parent.parent
    xml = (root / "data" / "de.cais.Muga.metainfo.xml").read_text()
    return {
        "muga.VERSION": __import__("muga").VERSION,
        "pyproject": tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"],
        "metainfo": re.search(r'<release version="([^"]+)"', xml).group(1),
    }


def test_the_version_is_the_same_everywhere() -> None:
    """Three files carry it and each drives something different: the updater
    compares muga.VERSION against the remote, pip resolves pyproject, and the
    software centre reads the metainfo. A bump that misses one means the
    in-app update check disagrees with what is installed."""
    versions = _declared_versions()
    assert len(set(versions.values())) == 1, f"version drift: {versions}"


def test_the_newest_metainfo_release_is_the_current_version() -> None:
    """AppStream lists releases newest-first, so the top entry is what the
    software centre shows as installed."""
    versions = _declared_versions()
    assert versions["metainfo"] == versions["muga.VERSION"]


def test_the_version_parses_as_a_release() -> None:
    """_is_newer compares it numerically; a non-numeric version silently falls
    back to "different means newer", which is how a downgrade slips through."""
    from muga.updater import _version_tuple

    version = _declared_versions()["muga.VERSION"]
    parts = _version_tuple(version)
    assert all(part[0] >= 0 for part in parts), f"{version} has non-numeric parts"


def test_the_version_is_newer_than_the_previous_release() -> None:
    """Otherwise the update check offers nothing, or worse, offers a
    downgrade to whoever is already on the newer one."""
    import re

    from muga.updater import _is_newer

    root = Path(__file__).resolve().parent.parent
    xml = (root / "data" / "de.cais.Muga.metainfo.xml").read_text()
    listed = re.findall(r'<release version="([^"]+)"', xml)
    assert len(listed) >= 2, "only one release listed"
    assert _is_newer(listed[0], listed[1]), f"{listed[0]} is not newer than {listed[1]}"


# ---------------------------------------------------------------------------
# The Yaga -> Muga rename must not look like data loss
# ---------------------------------------------------------------------------

def _legacy_install(base: Path) -> None:
    """A pre-rename install: settings, media index and a warm cache."""
    (base / "config" / "yaga").mkdir(parents=True)
    (base / "config" / "yaga" / "settings.json").write_text(
        json.dumps({"theme": "dark", "grid_columns": 6}),
    )
    (base / "data" / "yaga").mkdir(parents=True)
    (base / "data" / "yaga" / "yaga.sqlite3").write_bytes(b"SQLite format 3\x00")
    (base / "cache" / "yaga" / "thumbnails").mkdir(parents=True)
    (base / "cache" / "yaga" / "thumbnails" / "abc.jpg").write_bytes(b"\xff\xd8")


def _point_config_at(monkeypatch, base: Path) -> None:
    from muga import config

    monkeypatch.setattr(config, "CONFIG_DIR", base / "config" / "muga")
    monkeypatch.setattr(config, "CACHE_DIR", base / "cache" / "muga")
    monkeypatch.setattr(config, "DATA_DIR", base / "data" / "muga")
    monkeypatch.setattr(config, "DB_PATH", base / "data" / "muga" / "muga.sqlite3")


def test_the_rename_carries_the_users_data_across(tmp_path: Path, monkeypatch) -> None:
    from muga import config

    _legacy_install(tmp_path)
    _point_config_at(monkeypatch, tmp_path)

    config.migrate_legacy_dirs()

    assert config.Settings.load().grid_columns == 6, "settings were lost"
    assert config.DB_PATH.is_file(), "the media index would come up empty"
    assert (config.CACHE_DIR / "thumbnails" / "abc.jpg").is_file(), "cache went cold"


def test_the_database_is_renamed_not_just_moved(tmp_path: Path, monkeypatch) -> None:
    """DB_PATH looks for muga.sqlite3 — moving the directory alone would leave
    a full index sitting there unread."""
    from muga import config

    _legacy_install(tmp_path)
    (tmp_path / "data" / "yaga" / "yaga.sqlite3-wal").write_bytes(b"wal")
    _point_config_at(monkeypatch, tmp_path)

    config.migrate_legacy_dirs()

    assert config.DB_PATH.is_file()
    assert (config.DB_PATH.parent / f"{config.DB_PATH.name}-wal").is_file(), \
        "a left-behind write-ahead log loses whatever it still holds"
    assert not (config.DATA_DIR / "yaga.sqlite3").exists()


def test_migration_leaves_an_existing_install_alone(tmp_path: Path, monkeypatch) -> None:
    """Only ever fires into an empty slot — a fresh Muga install with its own
    data must not be overwritten by whatever is next door."""
    from muga import config

    _legacy_install(tmp_path)
    _point_config_at(monkeypatch, tmp_path)
    config.CONFIG_DIR.mkdir(parents=True)
    (config.CONFIG_DIR / "settings.json").write_text(json.dumps({"grid_columns": 3}))

    config.migrate_legacy_dirs()

    assert config.Settings.load().grid_columns == 3
    assert (tmp_path / "config" / "yaga").is_dir(), "the old directory was consumed"


def test_migration_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    from muga import config

    _legacy_install(tmp_path)
    _point_config_at(monkeypatch, tmp_path)

    config.migrate_legacy_dirs()
    config.migrate_legacy_dirs()

    assert config.Settings.load().grid_columns == 6
    assert config.DB_PATH.is_file()


def test_a_fresh_install_has_nothing_to_migrate(tmp_path: Path, monkeypatch) -> None:
    from muga import config

    _point_config_at(monkeypatch, tmp_path)
    config.migrate_legacy_dirs()  # must not raise
    assert not config.CONFIG_DIR.exists()


# ---------------------------------------------------------------------------
# The keyring schema rename must not look like a lost Nextcloud connection
# ---------------------------------------------------------------------------
#
# The schema used to be de.furilabs.muga.nextcloud — the namespace of the
# FuriPhone's maker, not ours. Renaming it to de.cais.Muga.nextcloud changes
# the lookup key, so a password stored under the old one is invisible unless
# it is moved across. Without that, an existing install comes up disconnected
# and asks for an app password it already has.


class _FakeSchema:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSecret:
    """An in-memory stand-in for libsecret, keyed by (schema, attrs)."""

    COLLECTION_DEFAULT = "default"

    class SchemaFlags:
        NONE = 0

    class SchemaAttributeType:
        STRING = 0

    class Schema:
        @staticmethod
        def new(name, _flags, _attrs):
            return _FakeSchema(name)

    def __init__(self) -> None:
        self.store: dict[tuple[str, tuple], str] = {}
        self.store_fails = False

    @staticmethod
    def _key(schema, attrs):
        return (schema.name, tuple(sorted(attrs.items())))

    def password_lookup_sync(self, schema, attrs, _cancellable):
        return self.store.get(self._key(schema, attrs))

    def password_store_sync(self, schema, attrs, _collection, _label, password, _c):
        if self.store_fails:
            return False
        self.store[self._key(schema, attrs)] = password
        return True

    def password_clear_sync(self, schema, attrs, _cancellable):
        return self.store.pop(self._key(schema, attrs), None) is not None


def _settings_with_fake_keyring(monkeypatch, tmp_path):
    """A Settings pointed at a fake keyring and an empty credential file."""
    import gi.repository

    fake = _FakeSecret()
    monkeypatch.setattr(gi.repository, "Secret", fake, raising=False)
    monkeypatch.setattr("muga.config.Settings._CRED_FILE", tmp_path / "nc_password")

    from muga.config import Settings
    settings = Settings()
    settings.nextcloud_url = "https://nc.example"
    settings.nextcloud_user = "alice"
    attrs = {"server": "https://nc.example", "user": "alice"}
    return settings, fake, attrs


def test_password_under_the_old_schema_is_found_and_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, fake, attrs = _settings_with_fake_keyring(monkeypatch, tmp_path)
    old = _FakeSchema(settings._LEGACY_KEYRING_SCHEMA)
    fake.store[fake._key(old, attrs)] = "hunter2"

    assert settings.load_app_password() == "hunter2"

    new_key = fake._key(_FakeSchema(settings._KEYRING_SCHEMA), attrs)
    assert fake.store.get(new_key) == "hunter2", "not re-stored under the new schema"
    assert fake._key(old, attrs) not in fake.store, "the old entry was left behind"


def test_a_failed_move_keeps_the_old_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the new store fails, the old entry must survive — losing the password
    between two schemas would be worse than not migrating at all."""
    settings, fake, attrs = _settings_with_fake_keyring(monkeypatch, tmp_path)
    old_key = fake._key(_FakeSchema(settings._LEGACY_KEYRING_SCHEMA), attrs)
    fake.store[old_key] = "hunter2"
    fake.store_fails = True

    assert settings.load_app_password() == "hunter2", "the password is still returned"
    assert fake.store.get(old_key) == "hunter2", "the old entry must be kept"


def test_the_new_schema_wins_and_the_old_is_not_consulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, fake, attrs = _settings_with_fake_keyring(monkeypatch, tmp_path)
    old_key = fake._key(_FakeSchema(settings._LEGACY_KEYRING_SCHEMA), attrs)
    new_key = fake._key(_FakeSchema(settings._KEYRING_SCHEMA), attrs)
    fake.store[new_key] = "current"
    fake.store[old_key] = "stale"

    assert settings.load_app_password() == "current"
    assert fake.store[old_key] == "stale", "an unrelated old entry was touched"


def test_disconnect_clears_both_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnecting must not leave the password behind under the old name on
    an install that never went through the migration."""
    settings, fake, attrs = _settings_with_fake_keyring(monkeypatch, tmp_path)
    for schema in (settings._KEYRING_SCHEMA, settings._LEGACY_KEYRING_SCHEMA):
        fake.store[fake._key(_FakeSchema(schema), attrs)] = "hunter2"

    settings.clear_app_password()

    assert fake.store == {}, f"entries survived the disconnect: {fake.store}"


# ---------------------------------------------------------------------------
# 11.  The vulnerability audit covers what the project actually depends on
# ---------------------------------------------------------------------------
#
# The CI audit step used to name its packages by hand, which made the list a
# copy of pyproject.toml's — and a copy that nothing compared. A dependency
# added to the project would simply never have been scanned, and the job would
# still have gone green. scripts/audit_requirements.py derives the list now;
# these pin down that it stays derived.

def _pyproject() -> dict:
    import tomllib

    root = Path(__file__).resolve().parent.parent
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def test_every_declared_dependency_is_audited_or_named_as_an_exception() -> None:
    """Nothing may fall out of the audit silently. A dependency that cannot be
    installed from PyPI has to be listed in NOT_ON_PYPI, where it is visible,
    rather than being left off the list where it is not."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        from audit_requirements import NOT_ON_PYPI, _name_of, requirements
    finally:
        sys.path.pop(0)

    data = _pyproject()
    declared = [
        *data["build-system"]["requires"],
        *data["project"]["dependencies"],
    ]
    covered = {_name_of(req) for req in requirements()} | NOT_ON_PYPI
    missing = [req for req in declared if _name_of(req) not in covered]
    assert not missing, f"declared but neither audited nor excepted: {missing}"


def test_the_audit_carries_the_version_floors_across() -> None:
    """The floors are the whole point — Pillow's is pinned against specific
    decoder advisories. Installing the bare names would audit whatever pip
    happened to resolve, which is never the version being asserted about."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        from audit_requirements import requirements
    finally:
        sys.path.pop(0)

    audited = requirements()
    assert any(req.startswith("Pillow>=") for req in audited), audited
    assert any(req.startswith("setuptools>=") for req in audited), audited


def test_the_build_backend_is_audited_too() -> None:
    """setuptools has advisories of its own (CVE-2024-6345, CVE-2025-47273,
    CVE-2026-59890). It builds every release, so leaving it out looks at half
    the tree."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        from audit_requirements import _name_of, requirements
    finally:
        sys.path.pop(0)

    assert "setuptools" in {_name_of(req) for req in requirements()}


def test_the_setuptools_floor_clears_its_known_advisories() -> None:
    """83.0.0 is the first release past CVE-2026-59890 — an sdist silently
    including a file MANIFEST.in excludes, via an NFC/NFD filename collision.
    This project ships an sdist and uses MANIFEST.in to decide what is in it."""
    import re

    requires = _pyproject()["build-system"]["requires"]
    floor = next(req for req in requires if req.lower().startswith("setuptools"))
    major = int(re.search(r">=\s*(\d+)", floor).group(1))
    assert major >= 83, f"{floor} still admits versions with known advisories"

