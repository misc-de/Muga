"""Tests for the gallery window.

Split the same way as the rest of the suite: the sort model, error mapping and
folder-path arithmetic are plain logic and run headless; construction, category
rebuilding and rendering need a real window and are skipped without a display.

The sort model is worth pinning down because it is a two-way mapping between a
(mode, direction) pair in the UI and a single string persisted per category —
an asymmetry there silently resets the user's choice.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import requires_display

app_mod = pytest.importorskip("yaga.app")

from yaga.models import MediaItem  # noqa: E402

GalleryWindow = app_mod.GalleryWindow


# Class-level tables the methods read off ``self``; a SimpleNamespace has to
# carry them explicitly.
_CLASS_ATTRS = ("_SORT_KEYS", "_SORT_TO_INTERNAL", "_INTERNAL_TO_SORT",
                "_MONTH_NAMES_EN")


def _win(*bind, **attrs) -> SimpleNamespace:
    """A stand-in ``self``.

    Names passed positionally are bound from the real class — used where the
    method under test dispatches to a sibling and that dispatch is part of
    what the test is checking.
    """
    attrs.setdefault("_", lambda s: s)
    for name in _CLASS_ATTRS:
        attrs.setdefault(name, getattr(GalleryWindow, name))
    win = SimpleNamespace(**attrs)
    for name in bind:
        setattr(win, name, getattr(GalleryWindow, name).__get__(win, type(win)))
    return win


# ---------------------------------------------------------------------------
# Sort model
# ---------------------------------------------------------------------------

def test_sort_mapping_is_a_bijection() -> None:
    """Every persisted string must map back to exactly one (mode, direction)
    pair, or reopening a folder silently changes its sort order."""
    forward = GalleryWindow._SORT_TO_INTERNAL
    back = GalleryWindow._INTERNAL_TO_SORT
    assert len(forward) == len(back)
    for pair, internal in forward.items():
        assert back[internal] == pair


def test_every_sort_key_has_both_directions() -> None:
    for key in GalleryWindow._SORT_KEYS:
        assert (key, True) in GalleryWindow._SORT_TO_INTERNAL
        assert (key, False) in GalleryWindow._SORT_TO_INTERNAL


def test_current_sort_defaults_per_category() -> None:
    """Nextcloud opens grouped by folder; everything else follows the global
    preference."""
    settings = SimpleNamespace(sort_mode="newest", sort_modes={})
    win = _win(category="nextcloud", current_folder=None, settings=settings)
    assert GalleryWindow._current_sort_internal(win) == "folder"

    win.category = "photos"
    assert GalleryWindow._current_sort_internal(win) == "newest"


def test_current_sort_is_remembered_per_folder() -> None:
    """A folder's own choice must win over the category default."""
    settings = SimpleNamespace(
        sort_mode="newest",
        sort_modes={"photos": "name", "photos\x00/holiday": "oldest"},
    )
    win = _win(category="photos", current_folder=None, settings=settings)
    assert GalleryWindow._current_sort_internal(win) == "name"

    win.current_folder = "/holiday"
    assert GalleryWindow._current_sort_internal(win) == "oldest"


def test_apply_sort_persists_and_rerenders() -> None:
    settings = SimpleNamespace(sort_mode="newest", sort_modes={}, save=MagicMock())
    win = _win(category="photos", current_folder=None, settings=settings,
               _sync_sort_controls=MagicMock(), _render=MagicMock(),
               _sort_popover=None)
    GalleryWindow._apply_sort_mode(win, "name", desc=False)
    assert settings.sort_modes["photos"] == "name"
    settings.save.assert_called_once()
    win._render.assert_called_once()


def test_apply_sort_scopes_to_the_open_folder() -> None:
    settings = SimpleNamespace(sort_mode="newest", sort_modes={}, save=MagicMock())
    win = _win(category="photos", current_folder="/holiday", settings=settings,
               _sync_sort_controls=MagicMock(), _render=MagicMock(), _sort_popover=None)
    GalleryWindow._apply_sort_mode(win, "date", desc=True)
    assert settings.sort_modes == {"photos\x00/holiday": "date"}


def test_sort_direction_toggle_keeps_the_mode() -> None:
    settings = SimpleNamespace(sort_mode="name", sort_modes={"photos": "name"},
                               save=MagicMock())
    win = _win("_current_sort_internal", "_apply_sort_mode",
               category="photos", current_folder=None, settings=settings,
               _sync_sort_controls=MagicMock(), _render=MagicMock(), _sort_popover=None)
    GalleryWindow._on_sort_direction_clicked(win, None)
    assert settings.sort_modes["photos"] == "name_desc"


def test_sort_dropdown_keeps_the_direction() -> None:
    """Switching from "Name ascending" to Date should stay ascending."""
    settings = SimpleNamespace(sort_mode="name", sort_modes={"photos": "name"},
                               save=MagicMock())
    dropdown = MagicMock()
    dropdown.get_selected.return_value = GalleryWindow._SORT_KEYS.index("date")
    win = _win("_current_sort_internal", "_apply_sort_mode",
               category="photos", current_folder=None, settings=settings,
               _sort_updating=False, _sync_sort_controls=MagicMock(),
               _render=MagicMock(), _sort_popover=None)
    GalleryWindow._on_sort_dropdown_changed(win, dropdown, None)
    assert settings.sort_modes["photos"] == "date_asc"


def test_sort_dropdown_ignores_programmatic_updates() -> None:
    """_sync_sort_controls sets the dropdown; without the guard that would
    loop straight back into a save."""
    settings = SimpleNamespace(sort_mode="newest", sort_modes={}, save=MagicMock())
    win = _win(_sort_updating=True, settings=settings, _render=MagicMock())
    GalleryWindow._on_sort_dropdown_changed(win, MagicMock(), None)
    settings.save.assert_not_called()


def test_sort_dropdown_ignores_an_out_of_range_index() -> None:
    settings = SimpleNamespace(sort_mode="newest", sort_modes={}, save=MagicMock())
    dropdown = MagicMock()
    dropdown.get_selected.return_value = 99
    win = _win(_sort_updating=False, settings=settings, _render=MagicMock())
    GalleryWindow._on_sort_dropdown_changed(win, dropdown, None)
    settings.save.assert_not_called()


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("error", "title"),
    [
        (FileNotFoundError("gone"), "File not found"),
        (PermissionError("nope"), "Permission denied"),
        (OSError("ENOSPC"), "System error"),
        (ValueError("huh"), "Error"),
    ],
)
def test_file_errors_get_a_specific_message(error, title) -> None:
    win = _win(_show_error_dialog=MagicMock())
    GalleryWindow._handle_file_error(win, error, "/x/a.jpg")
    assert win._show_error_dialog.call_args[0][0] == title


def test_file_error_includes_the_path() -> None:
    win = _win(_show_error_dialog=MagicMock())
    GalleryWindow._handle_file_error(win, FileNotFoundError(), "/x/a.jpg")
    assert "/x/a.jpg" in win._show_error_dialog.call_args[0][2]


def test_file_error_without_a_path() -> None:
    win = _win(_show_error_dialog=MagicMock())
    GalleryWindow._handle_file_error(win, FileNotFoundError())
    assert win._show_error_dialog.call_args[0][2] == ""


@pytest.mark.parametrize(
    ("error", "title"),
    [
        (PermissionError("401"), "Nextcloud authentication failed"),
        (FileNotFoundError("404"), "Nextcloud path not found"),
        (ConnectionError("refused"), "Connection failed"),
        (TimeoutError("timeout"), "Connection failed"),
        (OSError("dns"), "Connection failed"),
        (ValueError("?"), "Nextcloud error"),
    ],
)
def test_nextcloud_errors_get_a_recovery_hint(error, title) -> None:
    win = _win(_show_error_dialog=MagicMock())
    GalleryWindow._handle_nextcloud_error(win, error)
    assert win._show_error_dialog.call_args[0][0] == title


def test_nextcloud_connection_error_subclass_is_recognised() -> None:
    """The client raises its own ConnectionError subclass for a dead server."""
    from yaga.nextcloud import NextcloudConnectionError

    win = _win(_show_error_dialog=MagicMock())
    GalleryWindow._handle_nextcloud_error(win, NextcloudConnectionError("down"))
    assert win._show_error_dialog.call_args[0][0] == "Connection failed"


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (PermissionError(), "authentication"),
        (FileNotFoundError(), "not found"),
        (ConnectionError(), "connect"),
    ],
)
def test_nc_error_reason_is_a_short_tooltip(error, fragment) -> None:
    win = _win()
    assert fragment in GalleryWindow._nc_error_reason(win, error).lower()


# ---------------------------------------------------------------------------
# Nextcloud gating
# ---------------------------------------------------------------------------

def _nc_settings(**over):
    base = dict(nextcloud_enabled=True, nextcloud_url="https://cloud.example.org",
                nextcloud_user="alice", nextcloud_show_in_pictures=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_nc_is_visible_only_when_fully_configured() -> None:
    assert GalleryWindow.is_nc_visible(_win(settings=_nc_settings())) is True
    for missing in ("nextcloud_url", "nextcloud_user"):
        win = _win(settings=_nc_settings(**{missing: ""}))
        assert GalleryWindow.is_nc_visible(win) is False
    assert GalleryWindow.is_nc_visible(_win(settings=_nc_settings(nextcloud_enabled=False))) is False


def test_nc_stays_visible_after_a_manual_disconnect() -> None:
    """Cached thumbnails should keep showing; only the network is off."""
    win = _win(settings=_nc_settings(), _nc_session_active=False)
    assert GalleryWindow.is_nc_visible(win) is True
    assert GalleryWindow.is_nc_active(win) is False


def test_nc_is_active_only_with_a_live_session() -> None:
    win = _win(settings=_nc_settings(), _nc_session_active=True)
    assert GalleryWindow.is_nc_active(win) is True


# ---------------------------------------------------------------------------
# Folder-path arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("parent", "item_folder", "expected"),
    [
        (None, "holiday/2026", "holiday"),
        (None, "holiday", "holiday"),
        ("holiday", "holiday/2026", "holiday/2026"),
        ("holiday", "holiday/2026/june", "holiday/2026"),
        ("holiday", "work/2026", None),
        ("holiday", "holiday", None),
        (None, "", None),
        (None, "/", None),
        ("holiday", "holiday/", None),
    ],
)
def test_visible_child_folder(parent, item_folder, expected) -> None:
    win = _win(current_folder=parent)
    assert GalleryWindow._visible_child_folder_for_item(win, item_folder) == expected


# ---------------------------------------------------------------------------
# Date headers
# ---------------------------------------------------------------------------

def test_month_header_uses_the_in_app_language() -> None:
    """strftime("%B") follows LC_TIME and ignored the language picked in
    Settings, so the month name goes through the translator instead."""
    translated = {"March": "März"}
    win = _win(_=lambda s: translated.get(s, s))
    markup = GalleryWindow._month_header_markup(win, datetime(2026, 3, 15))
    assert "März" in markup
    assert "2026" in markup


def test_month_header_escapes_markup() -> None:
    win = _win(_=lambda s: "<b>evil</b>")
    markup = GalleryWindow._month_header_markup(win, datetime(2026, 3, 15))
    assert "&lt;b&gt;evil&lt;/b&gt;" in markup


def test_month_names_cover_the_year() -> None:
    assert len(GalleryWindow._MONTH_NAMES_EN) == 12


def test_date_grouping_emits_one_header_per_month() -> None:
    grid = MagicMock()
    win = _win(gallery_grid=grid, _date_last_key=None,
               _month_header_markup=lambda dt: f"{dt.year}-{dt.month}")
    march = datetime(2026, 3, 5).timestamp()
    april = datetime(2026, 4, 1).timestamp()
    for ts in (march, march + 60, april):
        item = MediaItem(id=1, path="/x", category="photos", media_type="image",
                         folder="/x", name="a.jpg", mtime=ts, size=1, thumb_path=None)
        GalleryWindow._append_date_grouped(win, item)

    assert grid.append_header.call_count == 2, "a header per item, not per month"
    assert grid.append_media.call_count == 3


# ---------------------------------------------------------------------------
# Real gallery window
# ---------------------------------------------------------------------------

@requires_display
def test_gallery_builds(gallery_window) -> None:
    assert gallery_window.category
    assert gallery_window.gallery_grid is not None
    assert gallery_window.database is not None


@requires_display
def test_gallery_status_line_hides_when_empty(gallery_window) -> None:
    gallery_window._set_status("working…")
    assert gallery_window.status.get_visible() is True
    gallery_window._set_status("")
    assert gallery_window.status.get_visible() is False


@requires_display
def test_gallery_sync_sort_controls_reflects_the_mode(gallery_window) -> None:
    gallery_window.settings.sort_modes[gallery_window.category] = "name_desc"
    gallery_window._sync_sort_controls()
    assert gallery_window._sort_dropdown.get_selected() == GalleryWindow._SORT_KEYS.index("name")
    assert "descending" in gallery_window._sort_dir_btn.get_icon_name()


@requires_display
def test_gallery_rebuilds_its_category_buttons(gallery_window) -> None:
    gallery_window._rebuild_categories()
    assert gallery_window.category_buttons, "no category buttons built"
    for key in gallery_window.category_buttons:
        assert key in {c[0] for c in gallery_window.settings.categories()}


@requires_display
def test_gallery_mobile_breakpoint(gallery_window) -> None:
    """Before the window is realised the width is 0; the mobile layout is the
    safer guess (better to hide a chip than flash it on a phone)."""
    assert gallery_window._is_mobile_width() is True


@requires_display
def test_gallery_error_dialog_opens(gallery_window) -> None:
    gallery_window._show_error_dialog("Title", "Message", "details")


@requires_display
def test_gallery_empty_state_toggles(gallery_window) -> None:
    gallery_window._set_empty_state(visible=True)
    gallery_window._set_empty_state(visible=False)


# ---------------------------------------------------------------------------
# Rescan gating
# ---------------------------------------------------------------------------

def _settings(**over):
    from yaga.config import Settings

    base = Settings()
    for key, value in over.items():
        setattr(base, key, value)
    return base


def test_scan_signature_ignores_cosmetic_settings() -> None:
    """Rescanning the library plus Nextcloud on every theme change was the
    dominant settings-interaction latency."""
    a = _settings(theme="light", grid_columns=4, language="en", sort_mode="newest")
    b = _settings(theme="dark", grid_columns=8, language="de", sort_mode="name")
    assert GalleryWindow._scan_signature(a) == GalleryWindow._scan_signature(b)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("photos_dir", "/somewhere/else"),
        ("videos_dir", "/other"),
        ("extra_locations", ["/mnt/card"]),
        ("nextcloud_url", "https://other.example.org"),
        ("nextcloud_user", "bob"),
        ("nextcloud_photos_path", "Camera"),
        ("nextcloud_enabled", True),
        ("nextcloud_thumbnail_only", False),
        ("nextcloud_show_in_pictures", True),
    ],
)
def test_scan_signature_reacts_to_what_changes_the_index(field, value) -> None:
    before = _settings()
    after = _settings(**{field: value})
    assert GalleryWindow._scan_signature(before) != GalleryWindow._scan_signature(after)


def test_scan_signature_is_hashable() -> None:
    """It is compared, so the list fields have to be tupled."""
    sig = GalleryWindow._scan_signature(_settings(extra_locations=["/a", "/b"]))
    assert hash(sig)


# ---------------------------------------------------------------------------
# Disk-cache accounting
# ---------------------------------------------------------------------------

def _cache_win(tmp_path, budget_mb):
    thumbs = tmp_path / "thumbnails"
    nc_cache = tmp_path / "nextcloud"
    thumbs.mkdir()
    nc_cache.mkdir()
    return SimpleNamespace(settings=SimpleNamespace(cache_max_mb=budget_mb)), thumbs, nc_cache


def _fill(directory, name, size, atime):
    import os

    path = directory / name
    path.write_bytes(b"\0" * size)
    os.utime(path, (atime, atime))
    return path


def test_cache_size_counts_both_stores(tmp_path, monkeypatch) -> None:
    win, thumbs, nc_cache = _cache_win(tmp_path, 0)
    _fill(thumbs, "a.jpg", 1000, 1)
    _fill(nc_cache, "b.jpg", 2000, 1)
    monkeypatch.setattr(app_mod, "THUMB_DIR", thumbs)
    with patch("yaga.nextcloud._NC_CACHE", nc_cache):
        assert GalleryWindow.cache_size_bytes(win) == 3000


def test_cache_size_of_an_empty_cache(tmp_path, monkeypatch) -> None:
    win, thumbs, nc_cache = _cache_win(tmp_path, 0)
    monkeypatch.setattr(app_mod, "THUMB_DIR", tmp_path / "missing")
    with patch("yaga.nextcloud._NC_CACHE", nc_cache):
        assert GalleryWindow.cache_size_bytes(win) == 0


def test_eviction_is_off_at_an_unlimited_budget(tmp_path, monkeypatch) -> None:
    win, thumbs, nc_cache = _cache_win(tmp_path, 0)
    _fill(thumbs, "a.jpg", 5_000_000, 1)
    monkeypatch.setattr(app_mod, "THUMB_DIR", thumbs)
    with patch("yaga.nextcloud._NC_CACHE", nc_cache):
        assert GalleryWindow.evict_cache(win) == 0
    assert (thumbs / "a.jpg").exists()


def test_eviction_leaves_a_cache_under_budget_alone(tmp_path, monkeypatch) -> None:
    win, thumbs, nc_cache = _cache_win(tmp_path, 10)
    _fill(thumbs, "a.jpg", 1000, 1)
    monkeypatch.setattr(app_mod, "THUMB_DIR", thumbs)
    with patch("yaga.nextcloud._NC_CACHE", nc_cache):
        assert GalleryWindow.evict_cache(win) == 0
    assert (thumbs / "a.jpg").exists()


def test_eviction_drops_the_least_recently_used_first(tmp_path, monkeypatch) -> None:
    """LRU by atime: the photo the user just looked at must outlive the one
    they opened last year."""
    win, thumbs, nc_cache = _cache_win(tmp_path, 1)   # 1 MB budget
    old = _fill(thumbs, "old.jpg", 700_000, atime=1_000_000)
    recent = _fill(thumbs, "recent.jpg", 700_000, atime=2_000_000)
    monkeypatch.setattr(app_mod, "THUMB_DIR", thumbs)

    with patch("yaga.nextcloud._NC_CACHE", nc_cache):
        freed = GalleryWindow.evict_cache(win)

    assert freed >= 700_000
    assert not old.exists(), "the oldest entry survived"
    assert recent.exists(), "the freshest entry was evicted"


def test_eviction_stops_once_under_budget(tmp_path, monkeypatch) -> None:
    win, thumbs, nc_cache = _cache_win(tmp_path, 1)
    for i in range(5):
        _fill(thumbs, f"{i}.jpg", 300_000, atime=1_000_000 + i)
    monkeypatch.setattr(app_mod, "THUMB_DIR", thumbs)

    with patch("yaga.nextcloud._NC_CACHE", nc_cache):
        GalleryWindow.evict_cache(win)

    remaining = sum(f.stat().st_size for f in thumbs.iterdir())
    assert remaining <= 1024 * 1024
    assert list(thumbs.iterdir()), "everything was deleted, not just the excess"


def test_eviction_covers_the_nextcloud_cache(tmp_path, monkeypatch) -> None:
    """Downloaded originals are the part that actually grows."""
    win, thumbs, nc_cache = _cache_win(tmp_path, 1)
    big = _fill(nc_cache, "download.jpg", 2_000_000, atime=1_000_000)
    monkeypatch.setattr(app_mod, "THUMB_DIR", thumbs)
    with patch("yaga.nextcloud._NC_CACHE", nc_cache):
        assert GalleryWindow.evict_cache(win) > 0
    assert not big.exists()


def test_eviction_survives_a_vanishing_file(tmp_path, monkeypatch) -> None:
    """The scanner's thumbnail pool writes into the same directory."""
    win, thumbs, nc_cache = _cache_win(tmp_path, 1)
    _fill(thumbs, "a.jpg", 2_000_000, atime=1_000_000)
    monkeypatch.setattr(app_mod, "THUMB_DIR", thumbs)

    real_unlink = Path.unlink
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError("raced with the scanner")
        return real_unlink(self, *a, **kw)

    with patch("yaga.nextcloud._NC_CACHE", nc_cache), patch.object(Path, "unlink", flaky):
        GalleryWindow.evict_cache(win)   # must not raise
