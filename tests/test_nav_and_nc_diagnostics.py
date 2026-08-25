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

def _nav_window(*, category: str = "photos", folder: str | None = None):
    return SimpleNamespace(
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
    )


def _button(active: bool):
    button = MagicMock()
    button.get_active.return_value = active
    return button


def test_tapping_the_active_tab_reloads_it() -> None:
    """The reported behaviour: nothing happened at all."""
    win = _nav_window()
    GalleryWindow._on_category_toggled(win, _button(False), "photos")
    win.refresh.assert_called_once_with(scan=True, scope="current")


def test_tapping_the_active_tab_keeps_it_lit() -> None:
    """A ToggleButton would otherwise stay visually off, leaving the gallery
    with no tab selected at all."""
    win = _nav_window()
    button = _button(False)
    GalleryWindow._on_category_toggled(win, button, "photos")
    button.set_active.assert_called_once_with(True)


def test_tapping_the_active_tab_inside_a_folder_goes_up_instead(tmp_path) -> None:
    """Drilled into a subfolder, the tap means "back to the top" — that was
    the one case which already worked, and it must keep working."""
    win = _nav_window(folder="/photos/holiday")
    GalleryWindow._on_category_toggled(win, _button(False), "photos")
    assert win.current_folder is None
    win._render.assert_called_once()
    win.refresh.assert_not_called()


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
