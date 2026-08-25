"""The scanner does not descend into hidden directories.

They hold tooling, not photos. The case that prompted this: on a phone the
git checkout sits in the same home directory the gallery scans, and the
Flatpak build tree next to it (``.flatpak-build``, ``.flatpak-builder``,
``.repo``) carries a dozen packaged app icons — which arrived in the gallery
as pictures. A location pointed at ``$HOME`` has the same problem with
``.cache`` and ``.local``.

The one thing that must keep working is a root the user picked deliberately,
even a hidden one: the walk seeds its stack with the root directly, so the
check only ever refuses to *descend* into a hidden directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from muga.database import Database
from muga.scanner import MediaScanner
from muga.thumbnails import Thumbnailer


@pytest.fixture
def scan(tmp_path):
    """Scan a category rooted at *root* and return the indexed names."""
    db = Database(tmp_path / "index.sqlite3")
    scanner = MediaScanner(db, Thumbnailer())

    def _scan(root: Path, category: str = "photos") -> set[str]:
        scanner.scan([(category, category.title(), str(root))])
        return {item.name for item in db.list_media(category)}

    return _scan


def _jpeg(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0JPEG_STUB")
    return path


def test_a_hidden_subdirectory_is_not_descended_into(tmp_path, scan) -> None:
    root = tmp_path / "Pictures"
    _jpeg(root / "holiday.jpg")
    _jpeg(root / ".flatpak-build" / "de.cais.Muga.png")
    assert scan(root) == {"holiday.jpg"}


def test_nested_content_below_a_hidden_directory_stays_out(tmp_path, scan) -> None:
    """Skipping the directory has to skip everything under it, however deep —
    the build tree that prompted this buries its icons several levels down."""
    root = tmp_path / "Pictures"
    _jpeg(root / "holiday.jpg")
    _jpeg(root / ".flatpak-build" / "x86_64" / "files" / "share" / "icons" / "app.png")
    assert scan(root) == {"holiday.jpg"}


def test_a_hidden_directory_deep_in_the_tree_is_skipped_too(tmp_path, scan) -> None:
    root = tmp_path / "Pictures"
    _jpeg(root / "2026" / "summer" / "beach.jpg")
    _jpeg(root / "2026" / "summer" / ".thumbnails" / "beach.png")
    assert scan(root) == {"beach.jpg"}


def test_a_root_the_user_chose_is_scanned_even_when_hidden(tmp_path, scan) -> None:
    """The whole point of the exception: picking a hidden folder as a media
    location is a deliberate act, and it must still work."""
    root = tmp_path / ".private-photos"
    _jpeg(root / "secret.jpg")
    _jpeg(root / "trip" / "boat.jpg")
    assert scan(root) == {"secret.jpg", "boat.jpg"}


def test_a_hidden_directory_below_a_hidden_root_is_still_skipped(tmp_path, scan) -> None:
    """The exception covers the root itself, not everything beneath it."""
    root = tmp_path / ".private-photos"
    _jpeg(root / "secret.jpg")
    _jpeg(root / ".cache" / "junk.jpg")
    assert scan(root) == {"secret.jpg"}


def test_a_directory_merely_containing_a_dot_is_not_hidden(tmp_path, scan) -> None:
    """Only a leading dot hides a directory — "2026.backup" is a normal name
    and a plausible one for a folder full of photos."""
    root = tmp_path / "Pictures"
    _jpeg(root / "2026.backup" / "old.jpg")
    assert scan(root) == {"old.jpg"}


def test_hidden_files_are_still_indexed(tmp_path, scan) -> None:
    """Documents the boundary of this change: it filters directories, not
    files. A dotfile that is genuinely an image stays visible."""
    root = tmp_path / "Pictures"
    _jpeg(root / ".hidden-photo.jpg")
    assert scan(root) == {".hidden-photo.jpg"}


def test_an_indexed_hidden_directory_is_pruned_on_the_next_scan(tmp_path) -> None:
    """Upgrades matter here: a library scanned before this change has rows for
    files under hidden directories. The next scan stops seeing them, and
    prune_missing has to clear them out rather than leaving tiles that open
    nothing useful."""
    db = Database(tmp_path / "index.sqlite3")
    scanner = MediaScanner(db, Thumbnailer())
    root = tmp_path / "Pictures"
    _jpeg(root / "holiday.jpg")
    stale = _jpeg(root / ".flatpak-build" / "icon.jpg")

    # Simulate the pre-change state: the hidden file is in the index.
    db.upsert_media(path=stale, category="photos", media_type="image",
                    folder=str(stale.parent), thumb_path=None)
    db.commit()
    assert {i.name for i in db.list_media("photos")} == {"icon.jpg"}

    scanner.scan([("photos", "Photos", str(root))])
    assert {i.name for i in db.list_media("photos")} == {"holiday.jpg"}
