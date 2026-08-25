"""The MCP write tools, and the two things that keep them contained.

The gate: they do not exist unless the user switched write access on, and
turning the server on or widening how far it listens does not grant it.

The sandbox: ``_resolve_for_write`` is the entire security boundary. Every
path a client supplies goes through it, and anything that resolves outside the
configured media folders — traversal, a symlink pointing out, an absolute path
somewhere else entirely — has to be refused. These tests are deliberately
paranoid; a hole here means a networked client can delete arbitrary files.
"""

from __future__ import annotations

import pytest

from muga.config import Settings
from muga.database import Database
from muga import mcp_server
from muga.thumbnails import Thumbnailer
from muga.scanner import MediaScanner


@pytest.fixture
def library(tmp_path):
    """A gallery with two media folders and a file outside both."""
    photos = tmp_path / "Photos"
    videos = tmp_path / "Videos"
    outside = tmp_path / "elsewhere"
    for d in (photos, videos, outside):
        d.mkdir()
    (photos / "one.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 40)
    (photos / "two.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"y" * 40)
    (outside / "secret.txt").write_text("not yours")
    (outside / "stray.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"z" * 40)

    db = Database(tmp_path / "index.sqlite3")
    MediaScanner(db, Thumbnailer()).scan([("photos", "Photos", str(photos))])

    settings = Settings()
    settings.photos_dir = str(photos)
    settings.videos_dir = str(videos)
    settings.screenshots_dir = ""
    settings.pictures_hidden = True
    settings.mcp_write_enabled = True
    return db, settings, photos, videos, outside


@pytest.fixture
def tools(library):
    db, settings, *_ = library
    return mcp_server.GalleryTools(db, settings)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_write_tools_are_absent_by_default(library) -> None:
    db, settings, *_ = library
    settings.mcp_write_enabled = False
    names = [d["name"] for d in mcp_server.GalleryTools(db, settings).descriptors()]
    for tool in ("delete_media", "move_media", "add_media"):
        assert tool not in names


def test_write_tools_appear_once_allowed(tools) -> None:
    names = [d["name"] for d in tools.descriptors()]
    for tool in ("delete_media", "move_media", "add_media"):
        assert tool in names


def test_a_write_call_is_refused_while_the_switch_is_off(library) -> None:
    """Hiding a tool from the listing is presentation; refusing to run it is
    the rule. A client that remembers the name must still be turned away."""
    db, settings, photos, *_ = library
    settings.mcp_write_enabled = False
    tools = mcp_server.GalleryTools(db, settings)
    with pytest.raises(PermissionError):
        tools.call("delete_media", {"path": str(photos / "one.jpg")})
    assert (photos / "one.jpg").exists()


def test_the_switch_takes_effect_without_a_restart(library) -> None:
    db, settings, photos, *_ = library
    tools = mcp_server.GalleryTools(db, settings)
    assert "delete_media" in [d["name"] for d in tools.descriptors()]
    settings.mcp_write_enabled = False
    assert "delete_media" not in [d["name"] for d in tools.descriptors()]
    with pytest.raises(PermissionError):
        tools.call("delete_media", {"path": str(photos / "one.jpg")})


def test_the_default_setting_is_off() -> None:
    assert Settings().mcp_write_enabled is False


def test_a_refused_write_is_explained_not_reported_as_an_internal_error(library) -> None:
    db, settings, photos, *_ = library
    settings.mcp_write_enabled = False
    protocol = mcp_server._Protocol(mcp_server.GalleryTools(db, settings))
    reply = protocol.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "delete_media", "arguments": {"path": str(photos / "one.jpg")}},
    })
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is True
    assert "write access" in text.lower()


# ---------------------------------------------------------------------------
# The sandbox
# ---------------------------------------------------------------------------

def test_a_path_outside_the_media_folders_is_refused(tools, library) -> None:
    _db, _s, _photos, _videos, outside = library
    with pytest.raises(ValueError, match="outside"):
        tools.call("delete_media", {"path": str(outside / "stray.jpg")})
    assert (outside / "stray.jpg").exists()


def test_traversal_out_of_a_media_folder_is_refused(tools, library) -> None:
    """``…/Photos/../elsewhere/secret.txt`` resolves out of the library, and
    resolving happens before the comparison precisely so it cannot."""
    _db, _s, photos, _videos, outside = library
    escape = photos / ".." / "elsewhere" / "secret.txt"
    with pytest.raises(ValueError, match="outside"):
        tools.call("delete_media", {"path": str(escape)})
    assert (outside / "secret.txt").exists()


def test_a_symlink_inside_the_library_is_refused(tools, library) -> None:
    """Even pointing somewhere legal: deleting the link and deleting its
    target are different acts, and the client cannot say which it meant."""
    _db, _s, photos, _videos, _outside = library
    link = photos / "link.jpg"
    link.symlink_to(photos / "one.jpg")
    with pytest.raises(ValueError, match="symlink"):
        tools.call("delete_media", {"path": str(link)})
    assert (photos / "one.jpg").exists()


def test_a_symlink_pointing_out_of_the_library_is_refused(tools, library) -> None:
    _db, _s, photos, _videos, outside = library
    link = photos / "escape.txt"
    link.symlink_to(outside / "secret.txt")
    with pytest.raises(ValueError):
        tools.call("delete_media", {"path": str(link)})
    assert (outside / "secret.txt").exists()


def test_a_relative_path_is_refused(tools) -> None:
    """Relative to what? The server's working directory is not something a
    client can reason about."""
    with pytest.raises(ValueError, match="absolute"):
        tools.call("delete_media", {"path": "Photos/one.jpg"})


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_an_empty_path_is_refused(tools, bad) -> None:
    with pytest.raises(ValueError):
        tools.call("delete_media", {"path": bad})


def test_writes_are_refused_when_no_media_folder_is_configured(library) -> None:
    db, settings, photos, *_ = library
    settings.photos_dir = ""
    settings.videos_dir = ""
    settings.screenshots_dir = ""
    tools = mcp_server.GalleryTools(db, settings)
    with pytest.raises(ValueError, match="no media folders"):
        tools.call("delete_media", {"path": str(photos / "one.jpg")})


def test_a_directory_cannot_be_deleted(tools, library) -> None:
    _db, _s, photos, _videos, _outside = library
    (photos / "trip").mkdir()
    with pytest.raises(ValueError, match="not a file"):
        tools.call("delete_media", {"path": str(photos / "trip")})
    assert (photos / "trip").is_dir()


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------

def test_delete_moves_to_trash_and_clears_the_index(library, monkeypatch) -> None:
    """Gio.trash is stubbed because pytest's tmp_path is on tmpfs, where the
    real one refuses with "trashing on system internal mounts is not
    supported". What is under test is the surrounding contract: index row
    dropped, gallery notified, trash used rather than unlink."""
    db, settings, photos, _videos, _outside = library
    from gi.repository import Gio

    trashed = []
    monkeypatch.setattr(
        Gio.File, "trash",
        lambda self, _c=None: (trashed.append(self.get_path()), True)[1],
    )
    seen = []
    tools = mcp_server.GalleryTools(db, settings, on_change=lambda: seen.append(1))

    target = photos / "one.jpg"
    result = tools.call("delete_media", {"path": str(target)})

    assert result["deleted"] is True and result["to"] == "trash"
    assert trashed == [str(target)]
    assert db.get_media_by_path(str(target)) is None
    assert seen == [1]


def test_a_file_survives_a_filesystem_without_a_trash(tools, library) -> None:
    """tmpfs has none, and so do FAT cards and many network mounts. The tool
    has to report that and leave the file alone — never fall back to an
    unrecoverable delete."""
    _db, _s, photos, _videos, _outside = library
    target = photos / "one.jpg"
    with pytest.raises(ValueError, match="trash"):
        tools.call("delete_media", {"path": str(target)})
    assert target.exists(), "the file was destroyed after trashing failed"


def test_a_failed_delete_leaves_the_index_alone(tools, library) -> None:
    """A row dropped for a file that is still there would show as a photo
    missing from the gallery until the next scan puts it back."""
    db, _s, photos, _videos, _outside = library
    target = photos / "one.jpg"
    with pytest.raises(ValueError):
        tools.call("delete_media", {"path": str(target)})
    assert db.get_media_by_path(str(target)) is not None


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------

def test_move_relocates_a_file_between_media_folders(tools, library) -> None:
    _db, _s, photos, videos, _outside = library
    result = tools.call("move_media", {
        "path": str(photos / "one.jpg"), "target_folder": str(videos),
    })
    assert result["moved"] is True
    assert not (photos / "one.jpg").exists()
    assert (videos / "one.jpg").exists()


def test_move_can_rename(tools, library) -> None:
    _db, _s, photos, _videos, _outside = library
    tools.call("move_media", {
        "path": str(photos / "one.jpg"), "target_folder": str(photos), "name": "renamed.jpg",
    })
    assert (photos / "renamed.jpg").exists()
    assert not (photos / "one.jpg").exists()


def test_move_will_not_overwrite(tools, library) -> None:
    _db, _s, photos, _videos, _outside = library
    with pytest.raises(ValueError, match="already exists"):
        tools.call("move_media", {
            "path": str(photos / "one.jpg"), "target_folder": str(photos), "name": "two.jpg",
        })
    assert (photos / "one.jpg").exists()
    assert (photos / "two.jpg").read_bytes().endswith(b"y" * 40)


def test_move_out_of_the_library_is_refused(tools, library) -> None:
    _db, _s, photos, _videos, outside = library
    with pytest.raises(ValueError, match="outside"):
        tools.call("move_media", {
            "path": str(photos / "one.jpg"), "target_folder": str(outside),
        })
    assert (photos / "one.jpg").exists()


@pytest.mark.parametrize("name", ["../escape.jpg", "sub/dir.jpg", "..", "."])
def test_move_refuses_a_name_that_is_a_path(tools, library, name) -> None:
    """A name is a name. Allowing a separator would put the file anywhere the
    target folder can reach."""
    _db, _s, photos, _videos, _outside = library
    with pytest.raises(ValueError, match="plain file name"):
        tools.call("move_media", {
            "path": str(photos / "one.jpg"), "target_folder": str(photos), "name": name,
        })


# ---------------------------------------------------------------------------
# Adding
# ---------------------------------------------------------------------------

def test_add_copies_a_file_in_and_leaves_the_original(tools, library) -> None:
    _db, _s, photos, _videos, outside = library
    result = tools.call("add_media", {
        "source_path": str(outside / "stray.jpg"), "target_folder": str(photos),
    })
    assert result["added"] is True
    assert (photos / "stray.jpg").exists()
    assert (outside / "stray.jpg").exists(), "the source was moved, not copied"


def test_add_refuses_a_non_media_file(tools, library) -> None:
    """Otherwise the tool is a way to copy any readable file into the library
    and have its path listed back out."""
    _db, _s, photos, _videos, outside = library
    with pytest.raises(ValueError, match="not an image or video"):
        tools.call("add_media", {
            "source_path": str(outside / "secret.txt"), "target_folder": str(photos),
        })
    assert not (photos / "secret.txt").exists()


def test_add_refuses_a_target_outside_the_library(tools, library) -> None:
    _db, _s, _photos, _videos, outside = library
    with pytest.raises(ValueError, match="outside"):
        tools.call("add_media", {
            "source_path": str(outside / "stray.jpg"), "target_folder": str(outside),
        })


def test_add_will_not_overwrite(tools, library) -> None:
    _db, _s, photos, _videos, outside = library
    with pytest.raises(ValueError, match="already exists"):
        tools.call("add_media", {
            "source_path": str(outside / "stray.jpg"),
            "target_folder": str(photos), "name": "one.jpg",
        })
    assert (photos / "one.jpg").read_bytes().endswith(b"x" * 40)


def test_add_preserves_the_modification_time(tools, library) -> None:
    """A photo imported today must not jump to the top of a gallery sorted by
    date just because it was copied."""
    import os

    _db, _s, photos, _videos, outside = library
    source = outside / "stray.jpg"
    os.utime(source, (1_400_000_000, 1_400_000_000))
    tools.call("add_media", {
        "source_path": str(source), "target_folder": str(photos),
    })
    assert (photos / "stray.jpg").stat().st_mtime == pytest.approx(1_400_000_000, abs=2)


def test_a_missing_source_is_reported(tools, library) -> None:
    _db, _s, photos, _videos, _outside = library
    with pytest.raises(ValueError):
        tools.call("add_media", {
            "source_path": "/nonexistent/nope.jpg", "target_folder": str(photos),
        })


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------

def test_a_write_notifies_the_gallery(library) -> None:
    """Without this a photo deleted over MCP stays on screen until the user
    refreshes and then opens a tile that is gone."""
    db, settings, photos, videos, _outside = library
    seen = []
    tools = mcp_server.GalleryTools(db, settings, on_change=lambda: seen.append(1))
    tools.call("move_media", {
        "path": str(photos / "one.jpg"), "target_folder": str(videos),
    })
    assert seen == [1]


def test_a_broken_change_callback_does_not_fail_the_write(library) -> None:
    """The file is already moved by then; reporting failure would tell the
    client to retry an operation that succeeded."""
    db, settings, photos, videos, _outside = library

    def _boom():
        raise RuntimeError("window is gone")

    tools = mcp_server.GalleryTools(db, settings, on_change=_boom)
    result = tools.call("move_media", {
        "path": str(photos / "one.jpg"), "target_folder": str(videos),
    })
    assert result["moved"] is True


def test_moving_drops_the_stale_index_row(library) -> None:
    db, settings, photos, videos, _outside = library
    tools = mcp_server.GalleryTools(db, settings)
    assert db.get_media_by_path(str(photos / "one.jpg")) is not None
    tools.call("move_media", {
        "path": str(photos / "one.jpg"), "target_folder": str(videos),
    })
    assert db.get_media_by_path(str(photos / "one.jpg")) is None
