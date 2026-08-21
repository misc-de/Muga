"""Tests for the gallery window.

Split the same way as the rest of the suite: the sort model, error mapping and
folder-path arithmetic are plain logic and run headless; construction, category
rebuilding and rendering need a real window and are skipped without a display.

The sort model is worth pinning down because it is a two-way mapping between a
(mode, direction) pair in the UI and a single string persisted per category —
an asymmetry there silently resets the user's choice.
"""

from __future__ import annotations

import threading
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


# ---------------------------------------------------------------------------
# Rendering and pagination against a real window
# ---------------------------------------------------------------------------

def _seed(window, count: int, category="photos", folder="/x", start_mtime=1.7e9):
    """Put *count* rows straight into the index, newest first."""
    rows = [
        (f"{folder}/IMG_{i:05d}.jpg", category, "image", folder,
         f"IMG_{i:05d}.jpg", start_mtime - i * 60, 1000, None, start_mtime)
        for i in range(count)
    ]
    window.database.wconn.executemany(
        "INSERT OR REPLACE INTO media"
        "(path,category,media_type,folder,name,mtime,size,thumb_path,seen_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)", rows,
    )
    window.database.commit()


@pytest.fixture
def seeded_window(gallery_window):
    """A gallery window whose index holds a known library."""
    gallery_window.database.clear_category("photos")
    gallery_window.category = "photos"
    gallery_window.current_folder = None
    gallery_window.settings.sort_modes = {}
    gallery_window._search_query = ""
    return gallery_window


@requires_display
def test_render_shows_the_first_page(seeded_window, pump) -> None:
    _seed(seeded_window, 40)
    seeded_window._render()
    pump()
    assert seeded_window.current_items, "nothing rendered"
    assert seeded_window._total_count == 40
    assert seeded_window.gallery_grid.row_store.get_n_items() > 0


@requires_display
def test_render_paginates_a_large_library(seeded_window, pump) -> None:
    """The first page is bounded regardless of library size — the whole point
    of not calling list_media here."""
    _seed(seeded_window, 500)
    seeded_window._render()
    pump()
    assert len(seeded_window.current_items) <= seeded_window._page_size
    assert seeded_window._has_more_items is True
    assert seeded_window._total_count == 500


@requires_display
def test_render_of_an_empty_category(seeded_window, pump) -> None:
    seeded_window._render()
    pump()
    assert seeded_window.current_items == []
    assert seeded_window._has_more_items is False


@requires_display
def test_load_more_appends_the_next_page(seeded_window, pump) -> None:
    _seed(seeded_window, 500)
    seeded_window._render()
    pump()
    first_page = len(seeded_window.current_items)

    seeded_window._load_more_items()
    pump()

    assert len(seeded_window.current_items) > first_page
    paths = [item.path for item in seeded_window.current_items]
    assert len(paths) == len(set(paths)), "the same items were loaded twice"


@requires_display
def test_load_more_stops_at_the_end(seeded_window, pump) -> None:
    _seed(seeded_window, 30)
    seeded_window._render()
    pump()
    seeded_window._load_more_items()
    assert seeded_window._has_more_items is False


@requires_display
def test_load_more_is_a_noop_without_more_items(seeded_window, pump) -> None:
    _seed(seeded_window, 10)
    seeded_window._render()
    pump()
    before = list(seeded_window.current_items)
    seeded_window._load_more_items()
    assert seeded_window.current_items == before


@requires_display
def test_load_more_clears_the_in_flight_flag_on_error(seeded_window, pump) -> None:
    """A stuck flag means lazy loading never runs again for the session."""
    _seed(seeded_window, 500)
    seeded_window._render()
    pump()
    with patch.object(type(seeded_window.database), "list_media_paginated",
                      side_effect=RuntimeError("db went away")), \
         pytest.raises(RuntimeError):
        seeded_window._load_more_items()
    assert seeded_window._lazy_loading_in_flight is False


@requires_display
def test_sliding_window_caps_what_is_held_in_memory(seeded_window, pump) -> None:
    """Jumping forward through months loads pages cumulatively; without the
    cap the ListView's allocation pass gets visibly slow."""
    seeded_window._MAX_LOADED_ITEMS = 400
    _seed(seeded_window, 2000)
    seeded_window._render()
    pump()
    for _ in range(12):
        seeded_window._load_more_items()
        pump()

    assert len(seeded_window.current_items) <= seeded_window._MAX_LOADED_ITEMS
    assert seeded_window._window_start_offset > 0, "the front was never dropped"


@requires_display
def test_sliding_window_keeps_the_newest_end(seeded_window, pump) -> None:
    """Eviction drops the front, so what remains is what the user scrolled to."""
    seeded_window._MAX_LOADED_ITEMS = 400
    _seed(seeded_window, 2000)
    seeded_window._render()
    pump()
    first_path = seeded_window.current_items[0].path
    for _ in range(12):
        seeded_window._load_more_items()
        pump()
    assert seeded_window.current_items[0].path != first_path


@requires_display
def test_sliding_window_leaves_a_small_library_alone(seeded_window, pump) -> None:
    _seed(seeded_window, 50)
    seeded_window._render()
    pump()
    seeded_window._evict_window_front_if_needed()
    assert seeded_window._window_start_offset == 0
    assert len(seeded_window.current_items) == 50


@requires_display
def test_render_preserves_scroll_position_on_a_refresh(seeded_window, pump) -> None:
    """A rescan re-renders the same view; snapping back to the top each time
    was the visible jank the key exists to avoid."""
    _seed(seeded_window, 500)
    seeded_window._render()
    pump()
    assert seeded_window._last_render_key == ("photos", None)
    seeded_window._render()
    pump()
    assert seeded_window._last_render_key == ("photos", None)


@requires_display
def test_render_resets_position_when_the_view_changes(seeded_window, pump) -> None:
    _seed(seeded_window, 100)
    seeded_window._render()
    pump()
    seeded_window.current_folder = "/x"
    seeded_window._render()
    pump()
    assert seeded_window._last_render_key == ("photos", "/x")


@requires_display
@pytest.mark.parametrize(
    "sort_mode", ["newest", "oldest", "name", "name_desc", "date", "date_asc"],
)
def test_every_sort_mode_renders(seeded_window, pump, sort_mode) -> None:
    _seed(seeded_window, 60)
    seeded_window.settings.sort_modes["photos"] = sort_mode
    seeded_window._render()
    pump()
    assert seeded_window.current_items, f"{sort_mode} rendered nothing"


@requires_display
def test_date_sorting_emits_month_headers(seeded_window, pump) -> None:
    from datetime import datetime

    seeded_window.settings.sort_modes["photos"] = "date"
    # Three months apart so each item lands in its own group.
    for months, i in enumerate(range(3)):
        ts = datetime(2026, 1 + months, 15).timestamp()
        _seed(seeded_window, 1, folder=f"/x{i}", start_mtime=ts)
    seeded_window._render()
    pump()

    headers = [
        seeded_window.gallery_grid.row_store.get_item(i).is_header
        for i in range(seeded_window.gallery_grid.row_store.get_n_items())
    ]
    assert sum(headers) == 3, f"expected one header per month, got {sum(headers)}"


@requires_display
def test_folder_sorting_renders_folder_tiles(seeded_window, pump) -> None:
    seeded_window.settings.sort_modes["photos"] = "folder"
    _seed(seeded_window, 5, folder="/x/holiday")
    _seed(seeded_window, 5, folder="/x/work")
    seeded_window._render()
    pump()
    assert seeded_window.gallery_grid.row_store.get_n_items() > 0


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@requires_display
def test_search_narrows_the_result(seeded_window, pump) -> None:
    _seed(seeded_window, 40)
    seeded_window._search_query = "IMG_0001"
    seeded_window._render()
    pump()
    assert 0 < seeded_window._total_count < 40
    assert all("IMG_0001" in i.name for i in seeded_window.current_items)


@requires_display
def test_search_with_no_hits(seeded_window, pump) -> None:
    _seed(seeded_window, 40)
    seeded_window._search_query = "zzzznothing"
    seeded_window._render()
    pump()
    assert seeded_window.current_items == []
    assert seeded_window._total_count == 0


@requires_display
def test_search_is_debounced(seeded_window) -> None:
    """A query per keystroke means a count(*) plus a page query per keystroke."""
    entry = MagicMock()
    entry.get_text.return_value = "hol"
    with patch.object(app_mod.GLib, "timeout_add", return_value=99) as timeout:
        seeded_window._on_search_changed(entry)
    timeout.assert_called_once()
    assert timeout.call_args[0][0] >= 100, "debounce shorter than a keystroke gap"


@requires_display
def test_search_replaces_a_pending_query(seeded_window) -> None:
    """Otherwise every keystroke leaves its own timer running."""
    entry = MagicMock()
    entry.get_text.return_value = "holi"
    seeded_window._search_debounce_id = 42
    with patch.object(app_mod.GLib, "timeout_add", return_value=43), \
         patch.object(app_mod.GLib, "source_remove") as remove:
        seeded_window._on_search_changed(entry)
    remove.assert_called_once_with(42)


# ---------------------------------------------------------------------------
# Applying settings
# ---------------------------------------------------------------------------

@requires_display
def test_apply_settings_stores_and_saves(seeded_window) -> None:
    from yaga.config import Settings

    new = Settings(**seeded_window.settings.__dict__)
    new.grid_columns = 7
    with patch.object(Settings, "save") as save:
        seeded_window.apply_settings(new)
    assert seeded_window.settings.grid_columns == 7
    save.assert_called_once()


@requires_display
def test_apply_settings_skips_a_rescan_for_cosmetic_changes(seeded_window) -> None:
    from yaga.config import Settings

    new = Settings(**seeded_window.settings.__dict__)
    new.grid_columns = 9
    new.theme = "dark"
    with patch.object(Settings, "save"):
        seeded_window.apply_settings(new)
    assert seeded_window._settings_needs_scan is False


@requires_display
def test_apply_settings_requests_a_rescan_when_a_folder_changes(seeded_window, tmp_path) -> None:
    from yaga.config import Settings

    new = Settings(**seeded_window.settings.__dict__)
    new.photos_dir = str(tmp_path / "elsewhere")
    with patch.object(Settings, "save"):
        seeded_window.apply_settings(new)
    assert seeded_window._settings_needs_scan is True


@requires_display
def test_apply_settings_leaves_selection_mode(seeded_window) -> None:
    from yaga.config import Settings

    seeded_window._selection_mode = True
    seeded_window._selected_paths = {"/x/a.jpg"}
    with patch.object(Settings, "save"):
        seeded_window.apply_settings(Settings(**seeded_window.settings.__dict__))
    assert seeded_window._selection_mode is False
    assert seeded_window._selected_paths == set()


@requires_display
def test_apply_settings_drops_the_shared_nc_client(seeded_window) -> None:
    """URL or credentials may have changed; a cached client would keep using
    the old ones."""
    from yaga.config import Settings

    client = MagicMock()
    seeded_window._nc_thumb_shared_client = client
    with patch.object(Settings, "save"):
        seeded_window.apply_settings(Settings(**seeded_window.settings.__dict__))
    assert seeded_window._nc_thumb_shared_client is None
    client.close.assert_called_once()


@requires_display
def test_apply_settings_honours_a_manual_disconnect(seeded_window) -> None:
    """Settings is the source of truth — re-enabling NC behind the user's back
    after they disconnected would be a surprise."""
    from yaga.config import Settings

    new = Settings(**seeded_window.settings.__dict__)
    new.nextcloud_enabled = True
    new.nextcloud_session_active = False
    with patch.object(Settings, "save"):
        seeded_window.apply_settings(new)
    assert seeded_window._nc_session_active is False


# ---------------------------------------------------------------------------
# Viewport filling
# ---------------------------------------------------------------------------

@requires_display
def test_viewport_fill_waits_for_a_real_measurement(seeded_window, pump) -> None:
    """An unmeasured scroller reports upper == page_size == 0, which read as
    "nothing fits" and pulled page after page until the whole library had
    been through the grid — 99 rounds for a 20k library, all before the first
    frame. It must load the first page and then wait."""
    _seed(seeded_window, 5000)
    seeded_window._render()
    pump()

    vadj = seeded_window.gallery_grid.get_vadjustment()
    assert vadj.get_page_size() == 0, "this window is unexpectedly realised"
    assert len(seeded_window.current_items) == seeded_window._page_size
    assert seeded_window._current_offset == seeded_window._page_size


@requires_display
def test_viewport_fill_gives_up_after_its_retries(seeded_window, pump) -> None:
    """A window that never gets allocated must not retry forever."""
    _seed(seeded_window, 5000)
    seeded_window._render()
    pump()
    seeded_window._fill_viewport_retries = seeded_window._FILL_VIEWPORT_MAX_RETRIES

    with patch.object(app_mod.GLib, "timeout_add") as timeout:
        seeded_window._maybe_fill_viewport()
    timeout.assert_not_called()


@requires_display
def test_viewport_fill_loads_when_the_page_is_short(seeded_window) -> None:
    """The case it exists for: a measured viewport taller than the content."""
    _seed(seeded_window, 500)
    seeded_window._has_more_items = True
    seeded_window._lazy_loading_in_flight = False
    seeded_window._total_count = 500
    seeded_window._current_offset = 200
    seeded_window.current_items = []

    vadj = MagicMock()
    vadj.get_page_size.return_value = 800.0
    vadj.get_upper.return_value = 600.0     # content shorter than the viewport
    with patch.object(type(seeded_window.gallery_grid), "get_vadjustment",
                      return_value=vadj), \
         patch.object(type(seeded_window), "_load_more_items") as load:
        seeded_window._maybe_fill_viewport()
    load.assert_called_once()


@requires_display
def test_viewport_fill_stops_when_the_page_is_full(seeded_window) -> None:
    seeded_window._has_more_items = True
    seeded_window._lazy_loading_in_flight = False
    vadj = MagicMock()
    vadj.get_page_size.return_value = 800.0
    vadj.get_upper.return_value = 5000.0    # plenty to scroll
    with patch.object(type(seeded_window.gallery_grid), "get_vadjustment",
                      return_value=vadj), \
         patch.object(type(seeded_window), "_load_more_items") as load:
        seeded_window._maybe_fill_viewport()
    load.assert_not_called()


@requires_display
def test_viewport_fill_is_inert_while_closing(seeded_window) -> None:
    seeded_window._closing = True
    seeded_window._has_more_items = True
    try:
        with patch.object(type(seeded_window), "_load_more_items") as load:
            seeded_window._maybe_fill_viewport()
        load.assert_not_called()
    finally:
        seeded_window._closing = False


@requires_display
def test_viewport_fill_does_not_reenter_a_running_load(seeded_window) -> None:
    seeded_window._has_more_items = True
    seeded_window._lazy_loading_in_flight = True
    with patch.object(type(seeded_window), "_load_more_items") as load:
        seeded_window._maybe_fill_viewport()
    load.assert_not_called()


# ---------------------------------------------------------------------------
# The scan thread
# ---------------------------------------------------------------------------

def _scan_win(**extra):
    """A ``self`` for _scan_thread with the scanner and the UI stubbed."""
    from yaga.config import Settings

    settings = Settings()
    settings.nextcloud_url = ""
    settings.nextcloud_user = ""
    defaults = dict(
        _=lambda s: s,
        _closing=False,
        category="photos",
        settings=settings,
        scanner=MagicMock(**{"scan.return_value": False,
                             "scan_nc_structure.return_value": False}),
        is_nc_active=lambda: False,
        _should_merge_nc=lambda: False,
        refresh=MagicMock(),
        _set_nc_syncing=MagicMock(),
        _set_nc_broken=MagicMock(),
        _reenable_refresh_button=MagicMock(),
        _on_nc_sync_failed=MagicMock(),
        _nc_unreachable=False,
        evict_cache_async=MagicMock(),
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def _run_scan(win, **kwargs):
    """Run _scan_thread, collecting what it hands back to the main loop."""
    scheduled = []
    with patch.object(app_mod.GLib, "idle_add",
                      side_effect=lambda fn, *a, **kw: scheduled.append((fn, a))):
        GalleryWindow._scan_thread(win, kwargs.pop("nc_folder", None), **kwargs)
    return scheduled


def _names(scheduled):
    return [getattr(fn, "_mock_name", None) or getattr(fn, "__name__", repr(fn))
            for fn, _ in scheduled]


def test_scan_always_re_enables_the_refresh_button() -> None:
    """It is disabled for the duration; a scan that dies without re-enabling
    it locks refresh for the rest of the session."""
    win = _scan_win(scanner=MagicMock(**{"scan.side_effect": RuntimeError("disk gone")}))
    scheduled = _run_scan(win)
    assert win._reenable_refresh_button in [fn for fn, _ in scheduled]


def test_scan_re_enables_refresh_even_when_settings_explode() -> None:
    win = _scan_win(is_nc_active=MagicMock(side_effect=RuntimeError("window gone")))
    scheduled = _run_scan(win)
    assert win._reenable_refresh_button in [fn for fn, _ in scheduled]


def test_scan_always_clears_the_syncing_spinner() -> None:
    win = _scan_win(scanner=MagicMock(**{"scan.side_effect": OSError("boom")}))
    scheduled = _run_scan(win)
    finals = [args for fn, args in scheduled if fn is win._set_nc_syncing]
    assert (False,) in finals, "the spinner was left running"


def test_scan_renders_local_changes_before_the_network_phase() -> None:
    """The user should see local edits at once rather than waiting on a slow
    Nextcloud sync."""
    win = _scan_win(scanner=MagicMock(**{"scan.return_value": True}))
    scheduled = _run_scan(win)
    assert win.refresh in [fn for fn, _ in scheduled]


def test_scan_does_not_rerender_when_nothing_changed() -> None:
    """An unchanged startup scan used to tear down and rebuild the grid — the
    visible stutter this flag exists to avoid."""
    win = _scan_win()
    scheduled = _run_scan(win)
    assert win.refresh not in [fn for fn, _ in scheduled]


def test_scan_skips_nextcloud_when_the_session_is_off() -> None:
    win = _scan_win(is_nc_active=lambda: False)
    win.settings.nextcloud_url = "https://cloud.example.org"
    win.settings.nextcloud_user = "alice"
    _run_scan(win)
    win.scanner.scan_nc_structure.assert_not_called()


def test_scan_flags_a_missing_password_as_broken() -> None:
    """Silently skipping would leave the user staring at an empty NC tab."""
    from yaga.config import Settings

    win = _scan_win(is_nc_active=lambda: True)
    win.settings.nextcloud_url = "https://cloud.example.org"
    win.settings.nextcloud_user = "alice"
    with patch.object(Settings, "load_app_password", return_value=""):
        scheduled = _run_scan(win)
    assert (win._set_nc_broken, (True,)) in scheduled


def test_scan_reports_a_failed_nextcloud_sync() -> None:
    """It trips the breaker so on-demand thumbnail fetches stop blocking ~20 s
    each against a server known to be down."""
    from yaga.config import Settings

    win = _scan_win(
        is_nc_active=lambda: True,
        scanner=MagicMock(**{"scan.return_value": False,
                             "scan_nc_structure.side_effect": ConnectionError("refused")}),
    )
    win.settings.nextcloud_url = "https://cloud.example.org"
    win.settings.nextcloud_user = "alice"
    with patch.object(Settings, "load_app_password", return_value="pw"):
        _run_scan(win)
    win._on_nc_sync_failed.assert_called_once()


def test_scan_clears_a_stale_broken_flag_after_recovery() -> None:
    from yaga.config import Settings

    win = _scan_win(is_nc_active=lambda: True, _nc_unreachable=True)
    win.settings.nextcloud_url = "https://cloud.example.org"
    win.settings.nextcloud_user = "alice"
    with patch.object(Settings, "load_app_password", return_value="pw"):
        scheduled = _run_scan(win)
    assert win._nc_unreachable is False
    assert (win._set_nc_broken, (False,)) in scheduled


def test_scan_scopes_to_the_current_category() -> None:
    """Pull-to-refresh should not walk every library the user has."""
    win = _scan_win(category="photos")
    _run_scan(win, scope="current")
    scanned = [c for c, _l, _p in win.scanner.scan.call_args[0][0]]
    assert scanned == ["photos"]


def test_scan_of_the_overview_covers_every_source() -> None:
    """Overview is a virtual aggregator; scoping to it alone silently no-op'd
    the pull-to-refresh gesture."""
    win = _scan_win(category="pictures")
    _run_scan(win, scope="current")
    scanned = [c for c, _l, _p in win.scanner.scan.call_args[0][0]]
    assert "pictures" not in scanned
    assert len(scanned) >= 1


def test_scan_of_the_nextcloud_tab_touches_no_local_folder() -> None:
    win = _scan_win(category="nextcloud")
    _run_scan(win, scope="current")
    win.scanner.scan.assert_not_called()


def test_full_scan_covers_every_local_category() -> None:
    win = _scan_win()
    _run_scan(win)
    scanned = [c for c, _l, _p in win.scanner.scan.call_args[0][0]]
    assert "nextcloud" not in scanned
    assert "pictures" not in scanned, "the aggregator is not a real folder"
    assert "photos" in scanned


def test_scan_passes_the_no_inherit_subtrees() -> None:
    """A folder exposed as its own category must not also be listed under a
    containing one."""
    win = _scan_win()
    win.settings.extra_locations = ["/x/holiday"]
    win.settings.extra_location_no_inherit = [True]
    _run_scan(win)
    excluded = win.scanner.scan.call_args.kwargs["excluded_subtrees"]
    assert "/x/holiday" in excluded


def test_scan_trims_the_cache_afterwards() -> None:
    """Thumbnail generation may have pushed the cache past its budget."""
    win = _scan_win()
    _run_scan(win)
    win.evict_cache_async.assert_called_once()


def test_scan_skips_rendering_into_a_closing_window() -> None:
    win = _scan_win(_closing=True, scanner=MagicMock(**{"scan.return_value": True}))
    scheduled = _run_scan(win)
    assert win.refresh not in [fn for fn, _ in scheduled]


# ---------------------------------------------------------------------------
# Nextcloud thumbnail workers
# ---------------------------------------------------------------------------

def _thumb_win(**extra):
    """A ``self`` for _nc_thumb_worker. The queue and its lock are real so the
    worker's own bookkeeping is exercised."""
    defaults = dict(
        _nc_thumb_lock=threading.Lock(),
        _nc_thumb_event=threading.Event(),
        _nc_thumb_queue=[],
        _nc_thumb_pending=set(),
        _nc_thumb_active_workers=1,
        _nc_thumb_shared_client=None,
        database=MagicMock(),
        _enqueue_thumb_update=MagicMock(),
        _on_nc_sync_failed=MagicMock(),
        _ensure_nc_thumb_client=MagicMock(),
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def test_thumb_worker_retires_without_credentials() -> None:
    """No password means every queued fetch would fail; the queue has to be
    dropped rather than retried forever."""
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=None),
                     _nc_thumb_queue=["nextcloud://a.jpg"],
                     _nc_thumb_pending={"nextcloud://a.jpg"})
    GalleryWindow._nc_thumb_worker(win)
    assert win._nc_thumb_active_workers == 0
    assert win._nc_thumb_queue == []
    assert win._nc_thumb_pending == set()


def test_thumb_worker_fetches_and_publishes() -> None:
    client = MagicMock()
    client.ensure_thumbnail.return_value = "/cache/t.jpg"
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client),
                     _nc_thumb_queue=["nextcloud://Photos/a.jpg"],
                     _nc_thumb_pending={"nextcloud://Photos/a.jpg"})
    win._nc_thumb_event.set()

    with patch.object(threading.Event, "wait", return_value=False):
        GalleryWindow._nc_thumb_worker(win)

    client.ensure_thumbnail.assert_called_once_with("/Photos/a.jpg")
    win.database.set_thumb.assert_called_once_with(
        "nextcloud://Photos/a.jpg", "/cache/t.jpg", "nextcloud")
    win._enqueue_thumb_update.assert_called_once_with(
        "nextcloud://Photos/a.jpg", "/cache/t.jpg")
    assert win._nc_thumb_pending == set(), "the path stayed marked as in flight"


def test_thumb_worker_is_fifo() -> None:
    """Tiles bind top-to-bottom, so thumbnails should arrive in the order the
    sort mode put them on screen."""
    client = MagicMock()
    client.ensure_thumbnail.return_value = "/cache/t.jpg"
    queue = [f"nextcloud://{i}.jpg" for i in range(5)]
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client),
                     _nc_thumb_queue=list(queue), _nc_thumb_pending=set(queue))
    with patch.object(threading.Event, "wait", return_value=False):
        GalleryWindow._nc_thumb_worker(win)
    fetched = [c[0][0] for c in client.ensure_thumbnail.call_args_list]
    assert fetched == [f"/{i}.jpg" for i in range(5)]


def test_thumb_worker_retires_when_the_queue_stays_empty() -> None:
    """Idle threads must not be kept alive for the life of the window."""
    client = MagicMock()
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client))
    with patch.object(threading.Event, "wait", return_value=False):
        GalleryWindow._nc_thumb_worker(win)
    assert win._nc_thumb_active_workers == 0


def test_thumb_worker_bails_out_on_a_dead_server() -> None:
    """~20 s per tile, all failing the same way, is not worth grinding
    through — trip the breaker and retire instead."""
    from yaga.nextcloud import NextcloudConnectionError

    client = MagicMock()
    client.ensure_thumbnail.side_effect = NextcloudConnectionError("server down")
    queue = [f"nextcloud://{i}.jpg" for i in range(10)]
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client),
                     _nc_thumb_queue=list(queue), _nc_thumb_pending=set(queue))

    GalleryWindow._nc_thumb_worker(win)

    assert client.ensure_thumbnail.call_count == 1, "kept fetching against a dead server"
    win._on_nc_sync_failed.assert_called_once()
    assert win._nc_thumb_active_workers == 0


def test_thumb_worker_continues_past_a_single_missing_preview() -> None:
    """A 404 on one file means that file has no preview, not that the server
    is gone."""
    client = MagicMock()
    client.ensure_thumbnail.side_effect = [None, "/cache/b.jpg"]
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client),
                     _nc_thumb_queue=["nextcloud://a.jpg", "nextcloud://b.jpg"],
                     _nc_thumb_pending={"nextcloud://a.jpg", "nextcloud://b.jpg"})
    with patch.object(threading.Event, "wait", return_value=False):
        GalleryWindow._nc_thumb_worker(win)
    assert client.ensure_thumbnail.call_count == 2
    win._enqueue_thumb_update.assert_called_once_with("nextcloud://b.jpg", "/cache/b.jpg")


def test_thumb_worker_continues_past_an_unexpected_error() -> None:
    client = MagicMock()
    client.ensure_thumbnail.side_effect = [ValueError("weird"), "/cache/b.jpg"]
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client),
                     _nc_thumb_queue=["nextcloud://a.jpg", "nextcloud://b.jpg"],
                     _nc_thumb_pending={"nextcloud://a.jpg", "nextcloud://b.jpg"})
    with patch.object(threading.Event, "wait", return_value=False):
        GalleryWindow._nc_thumb_worker(win)
    assert client.ensure_thumbnail.call_count == 2


def test_thumb_worker_batches_its_commits() -> None:
    """One commit per thumbnail across four workers was a storm of fsyncs and
    lock churn during a folder sync."""
    client = MagicMock()
    client.ensure_thumbnail.return_value = "/cache/t.jpg"
    queue = [f"nextcloud://{i}.jpg" for i in range(10)]
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client),
                     _nc_thumb_queue=list(queue), _nc_thumb_pending=set(queue))
    with patch.object(threading.Event, "wait", return_value=False):
        GalleryWindow._nc_thumb_worker(win)
    assert win.database.commit.call_count < 10, (
        f"{win.database.commit.call_count} commits for 10 thumbnails")
    assert win.database.commit.call_count >= 1, "the batch was never persisted"


def test_thumb_worker_flushes_before_going_idle() -> None:
    """Whatever was fetched has to reach disk even if the queue then dries up."""
    client = MagicMock()
    client.ensure_thumbnail.return_value = "/cache/t.jpg"
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client),
                     _nc_thumb_queue=["nextcloud://a.jpg"],
                     _nc_thumb_pending={"nextcloud://a.jpg"})
    with patch.object(threading.Event, "wait", return_value=False):
        GalleryWindow._nc_thumb_worker(win)
    win.database.commit.assert_called()


def test_thumb_worker_survives_a_failing_commit() -> None:
    client = MagicMock()
    client.ensure_thumbnail.return_value = "/cache/t.jpg"
    database = MagicMock()
    database.commit.side_effect = RuntimeError("database is locked")
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client),
                     _nc_thumb_queue=["nextcloud://a.jpg"],
                     _nc_thumb_pending={"nextcloud://a.jpg"}, database=database)
    with patch.object(threading.Event, "wait", return_value=False):
        GalleryWindow._nc_thumb_worker(win)
    assert win._nc_thumb_active_workers == 0, "the worker leaked its slot"


def test_thumb_worker_publishes_even_when_the_db_write_fails() -> None:
    """The tile update is in-memory; the DB write is pure persistence."""
    client = MagicMock()
    client.ensure_thumbnail.return_value = "/cache/t.jpg"
    database = MagicMock()
    database.set_thumb.side_effect = RuntimeError("locked")
    win = _thumb_win(_ensure_nc_thumb_client=MagicMock(return_value=client),
                     _nc_thumb_queue=["nextcloud://a.jpg"],
                     _nc_thumb_pending={"nextcloud://a.jpg"}, database=database)
    with patch.object(threading.Event, "wait", return_value=False):
        GalleryWindow._nc_thumb_worker(win)
    win._enqueue_thumb_update.assert_called_once()


def test_thumb_client_is_shared_between_workers() -> None:
    """Each worker gets its own keep-alive socket through the client's
    thread-local connections; building one client per worker would throw that
    away."""
    from yaga.config import Settings

    win = SimpleNamespace(_nc_thumb_shared_client=None, settings=Settings())
    win.settings.nextcloud_url = "https://cloud.example.org"
    win.settings.nextcloud_user = "alice"
    with patch.object(Settings, "load_app_password", return_value="pw"):
        first = GalleryWindow._ensure_nc_thumb_client(win)
        second = GalleryWindow._ensure_nc_thumb_client(win)
    assert first is second


def test_thumb_client_is_none_without_a_password() -> None:
    from yaga.config import Settings

    win = SimpleNamespace(_nc_thumb_shared_client=None, settings=Settings())
    with patch.object(Settings, "load_app_password", return_value=""):
        assert GalleryWindow._ensure_nc_thumb_client(win) is None


# ---------------------------------------------------------------------------
# Thumbnail-arrival batching
# ---------------------------------------------------------------------------

def _batch_win(**extra):
    defaults = dict(
        _closing=False,
        _pending_thumb_lock=threading.Lock(),
        _pending_thumb_updates={},
        _pending_thumb_idle=0,
        _update_item_thumb=MagicMock(),
    )
    defaults.update(extra)
    win = SimpleNamespace(**defaults)
    # Both methods schedule each other through idle_add.
    win._flush_thumb_updates = GalleryWindow._flush_thumb_updates.__get__(win, type(win))
    return win


def test_thumb_updates_are_coalesced_into_one_idle() -> None:
    """One idle_add per HTTP response would hammer the main loop during a
    folder sync."""
    win = _batch_win()
    with patch.object(app_mod.GLib, "idle_add", return_value=7) as idle:
        for i in range(20):
            GalleryWindow._enqueue_thumb_update(win, f"/p/{i}.jpg", f"/t/{i}.jpg")
    idle.assert_called_once()
    assert len(win._pending_thumb_updates) == 20


def test_thumb_updates_keep_only_the_newest_per_path() -> None:
    win = _batch_win()
    with patch.object(app_mod.GLib, "idle_add", return_value=7):
        GalleryWindow._enqueue_thumb_update(win, "/p/a.jpg", "/t/old.jpg")
        GalleryWindow._enqueue_thumb_update(win, "/p/a.jpg", "/t/new.jpg")
    assert win._pending_thumb_updates == {"/p/a.jpg": "/t/new.jpg"}


def test_thumb_flush_applies_a_bounded_chunk() -> None:
    """The main loop has to get a chance to paint between batches."""
    win = _batch_win(_pending_thumb_updates={f"/p/{i}.jpg": f"/t/{i}.jpg" for i in range(100)})
    with patch.object(app_mod.GLib, "idle_add", return_value=7):
        GalleryWindow._flush_thumb_updates(win)
    assert win._update_item_thumb.call_count == 24
    assert len(win._pending_thumb_updates) == 76, "the rest was dropped"


def test_thumb_flush_rearms_itself_for_the_leftovers() -> None:
    win = _batch_win(_pending_thumb_updates={f"/p/{i}.jpg": f"/t/{i}.jpg" for i in range(50)})
    with patch.object(app_mod.GLib, "idle_add", return_value=9) as idle:
        GalleryWindow._flush_thumb_updates(win)
    idle.assert_called_once()
    assert win._pending_thumb_idle == 9


def test_thumb_flush_does_not_rearm_when_done() -> None:
    win = _batch_win(_pending_thumb_updates={"/p/a.jpg": "/t/a.jpg"})
    with patch.object(app_mod.GLib, "idle_add") as idle:
        GalleryWindow._flush_thumb_updates(win)
    idle.assert_not_called()
    assert win._pending_thumb_updates == {}


def test_thumb_flush_merges_arrivals_from_the_meantime() -> None:
    """Workers keep delivering while the flush runs; those must not be lost."""
    win = _batch_win(_pending_thumb_updates={f"/p/{i}.jpg": f"/t/{i}.jpg" for i in range(30)})

    def deliver_more(path, thumb):
        if win._update_item_thumb.call_count == 1:
            win._pending_thumb_updates["/p/late.jpg"] = "/t/late.jpg"

    win._update_item_thumb.side_effect = deliver_more
    with patch.object(app_mod.GLib, "idle_add", return_value=9):
        GalleryWindow._flush_thumb_updates(win)
    assert "/p/late.jpg" in win._pending_thumb_updates


def test_thumb_flush_is_inert_while_closing() -> None:
    win = _batch_win(_closing=True, _pending_thumb_updates={"/p/a.jpg": "/t/a.jpg"})
    GalleryWindow._flush_thumb_updates(win)
    win._update_item_thumb.assert_not_called()
