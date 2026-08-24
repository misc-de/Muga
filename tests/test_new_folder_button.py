"""Guards for the "New folder" button being offered only where it can work.

Overview is a virtual aggregator over the other categories. Its path slot
carries a legacy value that is never scanned (see ``Settings.categories``), so
it has no directory of its own — a folder created "in" Overview would land
somewhere the view never shows.

The button is dimmed rather than hidden: the header keeps its shape as the user
moves between tabs, so nothing jumps around, and the tooltip explains the dimmed
state instead of leaving them to guess.
"""

from __future__ import annotations

import pytest

from tests.conftest import requires_display

pytestmark = requires_display


def _render_as(window, category: str, folder: str | None = None) -> None:
    """Put *window* on *category* and re-render, which is what syncs the header."""
    window.category = category
    window.current_folder = folder
    window._render()


def test_overview_cannot_create_a_folder(gallery_window) -> None:
    _render_as(gallery_window, "pictures")

    assert not gallery_window.new_folder_button.get_sensitive()


def test_a_real_category_can(gallery_window) -> None:
    _render_as(gallery_window, "photos")

    assert gallery_window.new_folder_button.get_sensitive()


def test_the_button_is_dimmed_not_hidden(gallery_window) -> None:
    """Hiding it would reflow the header on every tab change; the user would see
    the other buttons jump sideways as they browse."""
    _render_as(gallery_window, "pictures")

    assert gallery_window.new_folder_button.get_visible()


def test_the_dimmed_button_says_why(gallery_window) -> None:
    """A greyed-out control with no explanation reads as a bug."""
    _render_as(gallery_window, "pictures")
    disabled = gallery_window.new_folder_button.get_tooltip_text()

    _render_as(gallery_window, "photos")
    enabled = gallery_window.new_folder_button.get_tooltip_text()

    assert disabled and disabled != enabled
    assert "Overview" in disabled or "Übersicht" in disabled


@pytest.mark.parametrize("category", ["photos", "videos", "screenshots", "nextcloud"])
def test_every_other_tab_keeps_the_button(gallery_window, category: str) -> None:
    """Only Overview is virtual. Videos aggregates across categories for its
    *listing*, but it still has a folder of its own to create in."""
    _render_as(gallery_window, category)

    assert gallery_window.new_folder_button.get_sensitive()


def test_leaving_overview_restores_the_button(gallery_window) -> None:
    """The sync runs on every render, so the state has to come back — a
    one-way disable would leave the button dead for the rest of the session."""
    _render_as(gallery_window, "pictures")
    assert not gallery_window.new_folder_button.get_sensitive()

    _render_as(gallery_window, "photos")
    assert gallery_window.new_folder_button.get_sensitive()

    _render_as(gallery_window, "pictures")
    assert not gallery_window.new_folder_button.get_sensitive()


def test_a_subfolder_of_a_real_category_can_still_create(gallery_window) -> None:
    """Drilling into a folder does not change whose directory backs it."""
    _render_as(gallery_window, "photos", folder="Trip/Berlin")

    assert gallery_window.new_folder_button.get_sensitive()
