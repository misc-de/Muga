"""Files handed over by the desktop become viewer items, indexed or not."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from muga import app as app_module
from muga.opened_files import OPENED_CATEGORY, items_for_paths


def _touch(folder, *names):
    for name in names:
        (folder / name).write_bytes(b"x")


def test_one_file_brings_its_folder_and_starts_on_itself(tmp_path):
    _touch(tmp_path, "a.jpg", "b.jpg", "c.png", "notes.txt", ".hidden.jpg")
    items, index = items_for_paths([str(tmp_path / "b.jpg")])
    assert [item.name for item in items] == ["a.jpg", "b.jpg", "c.png"]
    assert index == 1
    assert items[index].category == OPENED_CATEGORY


def test_several_files_are_exactly_the_set_picked(tmp_path):
    _touch(tmp_path, "a.jpg", "b.jpg", "c.jpg")
    picked = [str(tmp_path / "c.jpg"), str(tmp_path / "a.jpg")]
    items, index = items_for_paths(picked)
    assert [item.name for item in items] == ["c.jpg", "a.jpg"]
    assert index == 0


def test_a_missing_file_gives_nothing_to_show(tmp_path):
    assert items_for_paths([str(tmp_path / "gone.jpg")]) == ([], 0)


def test_an_unknown_suffix_is_still_tried_as_an_image(tmp_path):
    _touch(tmp_path, "drawing.svg", "photo.jpg")
    items, index = items_for_paths([str(tmp_path / "drawing.svg")])
    assert [item.name for item in items] == ["drawing.svg"]
    assert items[index].media_type == "image"


def test_a_huge_folder_only_contributes_the_files_around_the_opened_one(tmp_path, monkeypatch):
    monkeypatch.setattr("muga.opened_files._MAX_NEIGHBOURS", 2)
    _touch(tmp_path, *(f"{i:03}.jpg" for i in range(20)))
    items, index = items_for_paths([str(tmp_path / "010.jpg")])
    assert [item.name for item in items] == ["008.jpg", "009.jpg", "010.jpg", "011.jpg", "012.jpg"]
    assert index == 2


def test_a_portal_path_is_replaced_by_a_reachable_original(tmp_path, monkeypatch):
    _touch(tmp_path, "a.jpg", "b.jpg")
    portal = tmp_path / "doc"
    portal.mkdir()
    _touch(portal, "b.jpg")
    monkeypatch.setattr(os, "getxattr", lambda _p, _n: str(tmp_path / "b.jpg").encode() + b"\0")
    items, index = items_for_paths([str(portal / "b.jpg")])
    assert [item.path for item in items] == [str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")]
    assert index == 1


def test_a_portal_path_stays_when_the_original_is_out_of_reach(tmp_path, monkeypatch):
    _touch(tmp_path, "b.jpg")
    monkeypatch.setattr(os, "getxattr", lambda _p, _n: b"/nowhere/b.jpg\0")
    items, _index = items_for_paths([str(tmp_path / "b.jpg")])
    assert [item.path for item in items] == [str(tmp_path / "b.jpg")]


@pytest.mark.parametrize(
    ("in_flatpak", "nodes", "expected"),
    [
        (True, [], {"GSK_RENDERER": "ngl", "LIBGL_ALWAYS_SOFTWARE": "1"}),
        (True, ["/dev/dri/renderD128"], {}),
        (False, [], {}),
    ],
)
def test_software_gl_only_in_a_sandbox_without_a_render_node(monkeypatch, in_flatpak, nodes, expected):
    monkeypatch.delenv("GSK_RENDERER", raising=False)
    monkeypatch.delenv("LIBGL_ALWAYS_SOFTWARE", raising=False)
    monkeypatch.setattr(app_module.os.path, "exists", lambda _p: in_flatpak)
    monkeypatch.setattr(app_module.glob, "glob", lambda _p: nodes)
    app_module._go_straight_to_software_gl()
    seen = {k: os.environ[k] for k in ("GSK_RENDERER", "LIBGL_ALWAYS_SOFTWARE") if k in os.environ}
    # monkeypatch.delenv restores the originals, but what we set must go too.
    for key in seen:
        monkeypatch.delenv(key)
    assert seen == expected


def test_an_explicit_renderer_wins(monkeypatch):
    monkeypatch.setenv("GSK_RENDERER", "vulkan")
    monkeypatch.delenv("LIBGL_ALWAYS_SOFTWARE", raising=False)
    monkeypatch.setattr(app_module.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(app_module.glob, "glob", lambda _p: [])
    app_module._go_straight_to_software_gl()
    assert os.environ["GSK_RENDERER"] == "vulkan"
    monkeypatch.delenv("LIBGL_ALWAYS_SOFTWARE", raising=False)


def _closing_gallery(monkeypatch, windows):
    monkeypatch.setattr(app_module, "GalleryWindow", MagicMock)
    app = SimpleNamespace(get_windows=lambda: windows)
    app_module.GalleryApplication.on_window_removed(app, None, SimpleNamespace(standalone=True))


def _gallery(visible):
    gallery = MagicMock()
    gallery.get_visible.return_value = visible
    return gallery


def test_the_last_viewer_takes_a_hidden_gallery_with_it(monkeypatch):
    gallery = _gallery(False)
    gallery.standalone = False
    _closing_gallery(monkeypatch, [gallery])
    gallery.close_hidden.assert_called_once()


def test_a_shown_gallery_stays(monkeypatch):
    gallery = _gallery(True)
    gallery.standalone = False
    _closing_gallery(monkeypatch, [gallery])
    gallery.close_hidden.assert_not_called()


def test_the_gallery_waits_for_the_other_viewer(monkeypatch):
    gallery = _gallery(False)
    gallery.standalone = False
    _closing_gallery(monkeypatch, [gallery, SimpleNamespace(standalone=True)])
    gallery.close_hidden.assert_not_called()


def test_closing_a_viewer_inside_a_shown_gallery_changes_nothing(monkeypatch):
    gallery = _gallery(False)
    monkeypatch.setattr(app_module, "GalleryWindow", MagicMock)
    app = SimpleNamespace(get_windows=lambda: [gallery])
    app_module.GalleryApplication.on_window_removed(app, None, SimpleNamespace(standalone=False))
    gallery.close_hidden.assert_not_called()
