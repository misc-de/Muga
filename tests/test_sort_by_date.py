"""The two date sorts, and that grouping never disagrees with ordering.

A photo has two dates: when the file last changed, and when the shutter fired.
Copying a shoot off a card gives every file today's mtime and leaves the
capture date years back, so the two orders are genuinely different — hence two
dropdown entries rather than one.

The trap this guards against is subtler than the sort itself: the month headers
are cut client-side from the items, while the order comes from SQL. Derive them
from different timestamps and photos appear under months they did not sort
into, which reads as the grid being randomly wrong.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from muga.database import Database
from muga.gallery_render import GalleryRenderMixin, _group_time
from muga.models import MediaItem


def _item(name: str, *, mtime: float, taken: float | None = None) -> MediaItem:
    return MediaItem(
        id=0, path=f"/m/{name}", category="photos", media_type="image",
        folder="/m", name=name, mtime=mtime, size=1, thumb_path=None, taken_at=taken,
    )


def _ts(text: str) -> float:
    return time.mktime(time.strptime(text, "%Y-%m-%d"))


# ---------------------------------------------------------------------------
# display_time
# ---------------------------------------------------------------------------

def test_display_time_prefers_the_capture_date() -> None:
    item = _item("a.jpg", mtime=_ts("2026-08-25"), taken=_ts("2019-07-14"))
    assert item.display_time == item.taken_at


def test_display_time_falls_back_to_mtime() -> None:
    item = _item("b.png", mtime=_ts("2026-08-25"))
    assert item.display_time == item.mtime


def test_a_zero_capture_date_is_not_treated_as_a_date() -> None:
    """0.0 is falsy and would otherwise silently mean 1970."""
    item = _item("c.jpg", mtime=_ts("2026-08-25"), taken=0.0)
    assert item.display_time == item.mtime


# ---------------------------------------------------------------------------
# The mode table
# ---------------------------------------------------------------------------

def test_every_date_mode_names_a_real_query_order() -> None:
    orders = Database._ORDERS if hasattr(Database, "_ORDERS") else None
    for mode, (query_sort, _use_taken) in GalleryRenderMixin._DATE_MODES.items():
        assert query_sort in ("newest", "oldest", "file_newest", "file_oldest"), mode
        if orders is not None:
            assert query_sort in orders


def test_the_file_modes_group_by_the_file_date(monkeypatch) -> None:
    for mode in ("date", "date_asc"):
        _query, use_taken = GalleryRenderMixin._DATE_MODES[mode]
        assert use_taken is False, mode


def test_the_capture_modes_group_by_the_capture_date() -> None:
    for mode in ("date_taken", "date_taken_asc"):
        _query, use_taken = GalleryRenderMixin._DATE_MODES[mode]
        assert use_taken is True, mode


def test_grouping_and_ordering_use_the_same_timestamp() -> None:
    """The actual invariant: for each mode, the field the header is cut from
    matches the field the SQL sorted by."""
    sorts_by_capture = {"newest", "oldest"}
    for mode, (query_sort, use_taken) in GalleryRenderMixin._DATE_MODES.items():
        assert use_taken == (query_sort in sorts_by_capture), mode


def test_group_time_follows_the_mode() -> None:
    item = _item("a.jpg", mtime=_ts("2026-08-25"), taken=_ts("2019-07-14"))
    assert _group_time(item, True) == item.taken_at
    assert _group_time(item, False) == item.mtime


def test_a_non_date_mode_is_not_grouped() -> None:
    for mode in ("newest", "oldest", "name", "name_desc", "folder", "folder_desc"):
        assert GalleryRenderMixin._DATE_MODES.get(mode) is None


# ---------------------------------------------------------------------------
# Ordering in SQL
# ---------------------------------------------------------------------------

@pytest.fixture
def indexed(tmp_path):
    """Three photos whose file order and capture order are reversed."""
    db = Database(tmp_path / "i.sqlite3")
    rows = [
        # (name, mtime, taken) — newest file is the oldest photo
        ("alt.jpg", _ts("2026-08-25"), _ts("2015-01-01")),
        ("mittel.jpg", _ts("2026-08-20"), _ts("2019-06-15")),
        ("neu.jpg", _ts("2026-08-15"), _ts("2024-12-31")),
    ]
    with db.wlock:
        db.wconn.executemany(
            "INSERT INTO media(path,category,media_type,folder,name,mtime,size,"
            "thumb_path,seen_at,taken_at,exif_data) "
            "VALUES(?,'photos','image','/m',?,?,1,NULL,1,?,'{}')",
            [(f"/m/{n}", n, mt, tk) for n, mt, tk in rows],
        )
        db.wconn.commit()
    return db


def test_newest_sorts_by_the_capture_date(indexed) -> None:
    names = [i.name for i in indexed.list_media("photos", "newest")]
    assert names == ["neu.jpg", "mittel.jpg", "alt.jpg"]


def test_oldest_reverses_it(indexed) -> None:
    names = [i.name for i in indexed.list_media("photos", "oldest")]
    assert names == ["alt.jpg", "mittel.jpg", "neu.jpg"]


def test_file_newest_sorts_by_the_file_date(indexed) -> None:
    """The opposite order — that is the whole point of offering both."""
    names = [i.name for i in indexed.list_media("photos", "file_newest")]
    assert names == ["alt.jpg", "mittel.jpg", "neu.jpg"]


def test_file_oldest_reverses_that(indexed) -> None:
    names = [i.name for i in indexed.list_media("photos", "file_oldest")]
    assert names == ["neu.jpg", "mittel.jpg", "alt.jpg"]


def test_a_file_without_a_capture_date_sorts_by_its_mtime(tmp_path) -> None:
    db = Database(tmp_path / "i.sqlite3")
    with db.wlock:
        db.wconn.executemany(
            "INSERT INTO media(path,category,media_type,folder,name,mtime,size,"
            "thumb_path,seen_at,taken_at,exif_data) "
            "VALUES(?,'photos','image','/m',?,?,1,NULL,1,?,'{}')",
            [
                ("/m/foto.jpg", "foto.jpg", _ts("2026-01-01"), _ts("2020-01-01")),
                ("/m/shot.png", "shot.png", _ts("2022-01-01"), None),
            ],
        )
        db.wconn.commit()
    # 2022 (the screenshot's mtime) is newer than 2020 (the photo's capture).
    assert [i.name for i in db.list_media("photos", "newest")] == ["shot.png", "foto.jpg"]


def test_paginated_and_full_listings_agree(indexed) -> None:
    """They carry separate ORDER BY maps; a change to one has to reach both."""
    full = [i.name for i in indexed.list_media("photos", "newest")]
    paged = [i.name for i in indexed.list_media_paginated("photos", "newest", limit=10)]
    assert full == paged
    full_file = [i.name for i in indexed.list_media("photos", "file_newest")]
    paged_file = [i.name for i in indexed.list_media_paginated(
        "photos", "file_newest", limit=10)]
    assert full_file == paged_file


def test_search_honours_the_file_order_too(indexed) -> None:
    names = [i.name for i in indexed.search_media("photos", "", "file_newest")]
    assert names == ["alt.jpg", "mittel.jpg", "neu.jpg"]


def test_an_unknown_sort_mode_still_returns_rows(indexed) -> None:
    """A hand-edited settings.json must not empty the gallery."""
    assert len(indexed.list_media("photos", "nonsense")) == 3


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

def _headers_for(items, use_taken):
    """Run the real grouping and collect the (year, month) of each header."""
    seen = []

    class _Grid:
        @staticmethod
        def append_header(_markup, year, month):
            seen.append((year, month))

        @staticmethod
        def append_media(_item):
            pass

    window = SimpleNamespace(
        gallery_grid=_Grid(), _date_last_key=None,
        _month_header_markup=lambda dt: "",
        _=lambda text: text,
    )
    for item in items:
        GalleryRenderMixin._append_date_grouped(window, item, use_taken)
    return seen


def test_headers_follow_the_capture_date_in_capture_mode() -> None:
    items = [
        _item("a.jpg", mtime=_ts("2026-08-25"), taken=_ts("2019-07-14")),
        _item("b.jpg", mtime=_ts("2026-08-25"), taken=_ts("2019-07-20")),
        _item("c.jpg", mtime=_ts("2026-08-25"), taken=_ts("2021-12-24")),
    ]
    assert _headers_for(items, True) == [(2019, 7), (2021, 12)]


def test_headers_follow_the_file_date_in_file_mode() -> None:
    """Same three photos, same order — one header, because all three files
    were written in the same month."""
    items = [
        _item("a.jpg", mtime=_ts("2026-08-25"), taken=_ts("2019-07-14")),
        _item("b.jpg", mtime=_ts("2026-08-25"), taken=_ts("2019-07-20")),
        _item("c.jpg", mtime=_ts("2026-08-25"), taken=_ts("2021-12-24")),
    ]
    assert _headers_for(items, False) == [(2026, 8)]


# ---------------------------------------------------------------------------
# Popovers let go when they close
# ---------------------------------------------------------------------------

from tests.conftest import requires_display  # noqa: E402


@requires_display
def test_a_closed_popover_releases_its_parent() -> None:
    """set_parent() keeps a popover alive after it hides. Without unparenting,
    every long-press left another invisible popover attached to the same
    widget, and those stack in front of the live one and swallow the outside
    clicks that are supposed to dismiss it."""
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    anchor = Gtk.Button()
    window = Gtk.Window()
    window.set_child(anchor)

    def _children(widget):
        out, child = [], widget.get_first_child()
        while child is not None:
            out.append(child)
            child = child.get_next_sibling()
        return out

    for _ in range(3):
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        popover.set_autohide(True)
        popover.connect("closed", lambda pop: pop.unparent())
        # "closed" is emitted directly rather than via popup()/popdown():
        # popdown on a popover that was never shown emits nothing, and popping
        # one up needs a mapped window. What is under test is the handler, not
        # GTK's visibility bookkeeping.
        popover.emit("closed")

    attached = [c for c in _children(anchor) if isinstance(c, Gtk.Popover)]
    assert attached == [], f"{len(attached)} popover(s) still attached"
    window.destroy()


@requires_display
def test_the_gallery_context_menu_unparents_itself() -> None:
    """The real one, not a stand-in: guards the wiring in gallery_selection."""
    import inspect

    from muga import gallery_selection

    source = inspect.getsource(gallery_selection)
    assert "popover.set_autohide(True)" in source
    assert 'popover.connect("closed", lambda pop: pop.unparent())' in source


@requires_display
def test_the_viewer_info_popover_unparents_itself() -> None:
    import inspect

    from muga import viewer

    source = inspect.getsource(viewer)
    assert "popover.set_autohide(True)" in source
    assert 'popover.connect("closed", lambda pop: pop.unparent())' in source
