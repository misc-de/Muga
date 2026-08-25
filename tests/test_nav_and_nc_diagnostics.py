"""Re-tapping the active tab, and why the Nextcloud badge is red.

Two things a user reported, both about the app not answering an obvious
question:

* tapping the tab you are already on did nothing at all — the ToggleButton
  reports that as being switched *off*, and the handler returned early;
* the red Nextcloud badge appeared with an empty tooltip, nothing in the log,
  and nothing in the diagnostics report. Two of the three code paths that
  raise it passed no reason, and the one that logged did so at info level.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import muga.app as app_mod

GalleryWindow = app_mod.GalleryWindow


# ---------------------------------------------------------------------------
# Category tabs
# ---------------------------------------------------------------------------

def _nav_window(*, category: str = "photos", folder: str | None = None,
                query: str = "", search_open: bool = False,
                selecting: bool = False):
    win = SimpleNamespace(
        category=category,
        current_folder=folder,
        settings=SimpleNamespace(last_category="", save=MagicMock()),
        category_buttons={},
        _cancel_nc_thumb_queue=MagicMock(),
        _render=MagicMock(),
        refresh=MagicMock(),
        # The handler blocks itself around the programmatic set_active, so the
        # stand-in has to carry the attribute it looks up.
        _on_category_toggled=lambda *a: None,
        # State a re-tap has to put back the way it found it.
        _search_query=query,
        search_bar=MagicMock(),
        search_entry=MagicMock(),
        _selection_mode=selecting,
        _exit_selection_mode=MagicMock(),
    )
    win.search_bar.get_search_mode.return_value = search_open
    # The reset itself is under test rather than mocked out — it is where the
    # "start this view over" decisions live.
    win._reset_category_view = lambda: GalleryWindow._reset_category_view(win)
    return win


def _button(active: bool):
    button = MagicMock()
    button.get_active.return_value = active
    return button


def test_tapping_the_active_tab_reloads_it_from_the_top() -> None:
    """The reported behaviour, twice over: first the tap did nothing at all,
    then it reloaded in place — which looks the same from the sofa, because
    a reload that keeps the scroll position changes nothing on screen."""
    win = _nav_window()
    GalleryWindow._on_category_toggled(win, _button(False), "photos")
    win.refresh.assert_called_once_with(
        scan=True, scope="current", reset_scroll=True,
    )


def test_tapping_the_active_tab_keeps_it_lit() -> None:
    """A ToggleButton would otherwise stay visually off, leaving the gallery
    with no tab selected at all."""
    win = _nav_window()
    button = _button(False)
    GalleryWindow._on_category_toggled(win, button, "photos")
    button.set_active.assert_called_once_with(True)


def test_tapping_the_active_tab_inside_a_folder_comes_back_up(tmp_path) -> None:
    """Drilled into a subfolder, the tap still climbs back out — and now
    reloads from there like every other re-tap, rather than only re-rendering
    what was already in hand."""
    win = _nav_window(folder="/photos/holiday")
    GalleryWindow._on_category_toggled(win, _button(False), "photos")
    assert win.current_folder is None
    win.refresh.assert_called_once_with(
        scan=True, scope="current", reset_scroll=True,
    )


def test_tapping_the_active_tab_drops_the_search_filter() -> None:
    """A filtered gallery is not what the tab looks like when you walk into
    it, so "start over" has to include the query."""
    win = _nav_window(query="holiday", search_open=True)
    GalleryWindow._on_category_toggled(win, _button(False), "photos")
    assert win._search_query == ""
    win.search_entry.set_text.assert_called_once_with("")
    win.search_bar.set_search_mode.assert_called_once_with(False)


def test_dropping_the_filter_does_not_render_on_the_way_out() -> None:
    """_on_search_mode_toggled renders when it closes on a live query. The
    query is cleared first so the reload is the only render — closing the bar
    first would build the unfiltered view twice."""
    win = _nav_window(query="holiday", search_open=True)
    order = []
    win.search_bar.set_search_mode.side_effect = lambda _m: order.append(
        win._search_query,
    )
    GalleryWindow._on_category_toggled(win, _button(False), "photos")
    assert order == [""], "the bar was closed while the query still stood"


def test_tapping_the_active_tab_leaves_selection_mode() -> None:
    """The tabs stay reachable while multi-select is on; reloading underneath
    a selection would leave check-marks pointing at rows that were rebuilt."""
    win = _nav_window(selecting=True)
    GalleryWindow._on_category_toggled(win, _button(False), "photos")
    win._exit_selection_mode.assert_called_once()


def test_tapping_the_active_tab_leaves_a_quiet_view_alone() -> None:
    """No search, no selection: nothing to undo, and in particular no
    pointless set_search_mode on a bar that is already closed."""
    win = _nav_window()
    GalleryWindow._on_category_toggled(win, _button(False), "photos")
    win.search_bar.set_search_mode.assert_not_called()
    win._exit_selection_mode.assert_not_called()


def test_a_tab_switched_off_by_another_tab_does_nothing() -> None:
    """When a different tab takes over, GTK switches this one off. That path
    belongs to the incoming tab; acting on it here would reload the category
    the user just left."""
    win = _nav_window(category="videos")
    GalleryWindow._on_category_toggled(win, _button(False), "photos")
    win.refresh.assert_not_called()
    win._render.assert_not_called()


def test_switching_to_another_tab_still_renders() -> None:
    win = _nav_window(category="videos")
    other = _button(True)
    win.category_buttons = {"videos": _button(True), "photos": other}
    GalleryWindow._on_category_toggled(win, other, "photos")
    assert win.category == "photos"
    assert win.current_folder is None
    win._render.assert_called_once()


# ---------------------------------------------------------------------------
# The Nextcloud badge explains itself
# ---------------------------------------------------------------------------

def _broken_window():
    return SimpleNamespace(
        _closing=False, _nc_broken_img=None, _nc_broken_reason="",
        _nc_broken_since="", _nc_unreachable=False,
        _=lambda text: text,
        _cancel_nc_thumb_queue=MagicMock(),
        _set_nc_broken=MagicMock(),
        _handle_nextcloud_error=MagicMock(),
    )


def test_a_broken_badge_records_its_reason() -> None:
    win = _broken_window()
    GalleryWindow._set_nc_broken(win, True, "Nextcloud authentication failed")
    assert win._nc_broken_reason == "Nextcloud authentication failed"
    assert win._nc_broken_since


def test_a_reason_is_recorded_even_with_no_badge_widget() -> None:
    """A narrow window has no badge to hang a tooltip on, and on a phone a
    tooltip is unreachable anyway — the diagnostics report is the answer, and
    it can only report what was stored."""
    win = _broken_window()
    win._nc_broken_img = None
    GalleryWindow._set_nc_broken(win, True, "Could not connect to Nextcloud")
    assert win._nc_broken_reason == "Could not connect to Nextcloud"


def test_a_missing_reason_still_says_something() -> None:
    win = _broken_window()
    GalleryWindow._set_nc_broken(win, True)
    assert win._nc_broken_reason, "the badge went up with an empty reason again"


def test_clearing_the_badge_clears_the_reason() -> None:
    win = _broken_window()
    GalleryWindow._set_nc_broken(win, True, "boom")
    GalleryWindow._set_nc_broken(win, False)
    assert win._nc_broken_reason == "" and win._nc_broken_since == ""


def test_a_sync_failure_is_logged_with_its_type(caplog) -> None:
    win = _broken_window()
    win._nc_error_reason = lambda e: GalleryWindow._nc_error_reason(win, e)
    with caplog.at_level(logging.WARNING, logger="muga.app"), \
            patch.object(app_mod.GLib, "idle_add", lambda *a, **k: None):
        GalleryWindow._on_nc_sync_failed(win, TimeoutError("timed out after 20s"))
    assert any("TimeoutError" in r.getMessage() for r in caplog.records)
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_the_sync_failure_log_carries_a_traceback(caplog) -> None:
    """exc_info=error rather than True: the thumbnail worker calls this from
    outside any except block, where exc_info=True logs "NoneType: None"."""
    win = _broken_window()
    win._nc_error_reason = lambda e: GalleryWindow._nc_error_reason(win, e)
    with caplog.at_level(logging.WARNING, logger="muga.app"), \
            patch.object(app_mod.GLib, "idle_add", lambda *a, **k: None):
        GalleryWindow._on_nc_sync_failed(win, TimeoutError("nope"))
    record = next(r for r in caplog.records if "Nextcloud unreachable" in r.getMessage())
    assert record.exc_info and record.exc_info[0] is TimeoutError


@pytest.mark.parametrize(
    "error, expected",
    [
        (PermissionError("401"), "authentication"),
        (FileNotFoundError("404"), "not found"),
        (TimeoutError("timeout"), "connect"),
    ],
)
def test_each_failure_kind_gets_its_own_wording(error, expected) -> None:
    win = _broken_window()
    assert expected in GalleryWindow._nc_error_reason(win, error).lower()


def test_the_diagnostics_report_names_the_reason() -> None:
    """The actual complaint: a red icon and nothing anywhere saying why."""
    import inspect

    from muga import settings_window

    source = inspect.getsource(settings_window)
    assert "_nc_broken_reason" in source
    assert "Connection marked broken" in source
