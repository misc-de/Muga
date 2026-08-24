"""Turning database rows into grid content, one page at a time.

Split out of ``GalleryWindow``. The gallery never holds a whole library in
memory: it renders a first page, appends more as the user scrolls, and — when
sorted by date — drops months that have scrolled far enough off the top. That
sliding window is what keeps jumping through years of photos responsive, and
it is the reason these methods are worth reading together.

Four render shapes share the paging machinery: flat, folder tiles, date groups
and search results.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from .models import MediaItem

if TYPE_CHECKING:
    from .config import Settings
    from .database import Database
    from .gallery_grid import GalleryGrid

LOGGER = logging.getLogger(__name__)


class GalleryRenderMixin:
    """Rendering, pagination and the sliding month window.

    The block below is the contract with the host class: every name is created
    in ``GalleryWindow.__init__`` (or defined on it) and only annotated here.
    """

    settings: Settings
    database: Database
    gallery_grid: GalleryGrid
    back_button: Gtk.Button
    category: str
    current_folder: str | None
    current_items: list[MediaItem]
    _closing: bool
    _search_query: str

    # Paging state.
    _page_size: int
    _current_offset: int
    _total_count: int
    _has_more_items: bool
    _window_start_offset: int
    _MAX_LOADED_ITEMS: int
    _last_render_key: tuple[str, str | None] | None
    _date_last_key: tuple[int, int] | None
    _lazy_loading_attached: bool
    _lazy_loading_in_flight: bool
    _fill_viewport_retries: int

    # English month names indexed by month-1. Used as translation keys so
    # the in-app language switch (Translator) drives the header text
    # instead of the system locale — strftime("%B") follows LC_TIME and
    # ignored the user's pick in Settings → Language.
    _MONTH_NAMES_EN = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )

    # Attempts to wait for the scroller to be measured before deciding whether
    # the viewport needs filling. 20 × 50 ms ≈ 1 s, after which the scroll
    # handler takes over on the user's first scroll.
    _FILL_VIEWPORT_MAX_RETRIES = 20

    if TYPE_CHECKING:
        # Provided by GalleryWindow; no runtime definition, so these can never
        # shadow the real methods.
        def _(self, text: str) -> str: ...
        def _set_status(self, text: str) -> None: ...
        def _set_empty_state(self, visible: bool) -> None: ...
        def _should_merge_nc(self) -> bool: ...
        def _sync_sort_controls(self) -> None: ...
        def _sync_new_folder_button(self) -> None: ...

    def _render(self) -> None:
        # Preserve scroll position when refreshing the same view (e.g. after scan)
        render_key = (self.category, self.current_folder)
        vadj = self.gallery_grid.get_vadjustment()
        saved_pos = vadj.get_value() if render_key == self._last_render_key else 0.0
        self._last_render_key = render_key

        self.gallery_grid.clear()
        self.current_items = []
        self._current_offset = 0
        self._window_start_offset = 0
        self._has_more_items = False
        self._date_last_key = None

        sort_mode = self.settings.get_sort_mode(self.category, self.current_folder)
        # Sync the dropdown + direction icon to whatever was saved for this view.
        if hasattr(self, "_sort_dropdown"):
            self._sync_sort_controls()
        # Back arrow surfaces whenever the user has drilled into a
        # subfolder. Selection mode flips it back off in _enter_selection_mode.
        self.back_button.set_visible(self.current_folder is not None)
        # Overview has no directory of its own to create a folder in.
        self._sync_new_folder_button()
        if self._search_query:
            self._render_search(sort_mode)
        elif sort_mode in ("folder", "folder_desc"):
            self._render_folders()
        elif sort_mode in ("date", "date_asc"):
            self._render_date_groups(ascending=(sort_mode == "date_asc"))
        else:
            self._render_flat(sort_mode)
        self.gallery_grid.finish()

        if saved_pos > 0:
            def _restore() -> bool:
                vadj.set_value(saved_pos)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(_restore, priority=GLib.PRIORITY_HIGH_IDLE)

        # Lazy-loading is connected once per window; the handler bails if no more items.
        self._setup_lazy_loading()
        # If the first page didn't fill the viewport, keep loading until it does.
        if self._has_more_items:
            GLib.idle_add(self._maybe_fill_viewport, priority=GLib.PRIORITY_LOW)

    def _render_search(self, sort_mode: str) -> None:
        """Search results, paginated and respecting the active sort.
        Date-grouping (month headers) applies for date / date_asc just like
        the regular gallery render."""
        include_nc = self._should_merge_nc()
        # Date sorts map onto newest/oldest for the SQL ORDER BY; the actual
        # grouping is rebuilt client-side from the items.
        if sort_mode in ("date", "date_asc"):
            query_sort = "oldest" if sort_mode == "date_asc" else "newest"
            grouped = True
        else:
            # newest / oldest / name / name_desc / folder / folder_desc — pass
            # through to the DB unchanged.
            query_sort = sort_mode
            grouped = False

        media_filter = self.settings.media_filter_for(self.category)
        self._total_count = self.database.search_media_count(
            self.category, self._search_query,
            self.current_folder, include_nc=include_nc,
            media_filter=media_filter,
        )
        page = self.database.search_media(
            self.category, self._search_query, query_sort,
            self.current_folder, include_nc=include_nc,
            limit=self._page_size, offset=0,
            media_filter=media_filter,
        )
        self.current_items = list(page)
        self._current_offset = len(page)
        self._has_more_items = self._current_offset < self._total_count
        self._date_last_key = None
        for item in page:
            if grouped:
                self._append_date_grouped(item)
            else:
                self.gallery_grid.append_media(item)
        self._set_status("")
        self._set_empty_state(visible=not self.current_items)

    def _load_first_page(
        self, sort_mode: str, folder: str | None, *,
        include_nc: bool, media_filter: str | None,
    ) -> list[MediaItem]:
        """Fetch the first page for (category, folder, sort_mode) and reset the
        pagination bookkeeping (_total_count / current_items / _current_offset /
        _has_more_items). Returns the page so callers can append the items in
        whatever layout (flat / date-grouped) they need."""
        self._total_count = self.database.count_media(
            self.category, folder, include_nc=include_nc,
            media_filter=media_filter,
        )
        page = self.database.list_media_paginated(
            self.category, sort_mode, folder,
            self._page_size, 0, include_nc=include_nc,
            media_filter=media_filter,
        )
        self.current_items = list(page)
        self._current_offset = len(page)
        self._has_more_items = self._current_offset < self._total_count
        return page

    def _render_flat(self, sort_mode: str) -> None:
        include_nc = self._should_merge_nc()
        media_filter = self.settings.media_filter_for(self.category)
        page = self._load_first_page(
            sort_mode, self.current_folder,
            include_nc=include_nc, media_filter=media_filter,
        )
        for item in page:
            self.gallery_grid.append_media(item)
        self._set_status("")
        self._set_empty_state(visible=not self.current_items)

    def _render_folders(self) -> None:
        sort_mode = self.settings.get_sort_mode(self.category, self.current_folder)
        media_filter = self.settings.media_filter_for(self.category)
        folders = self.database.child_folders(
            self.category, self.current_folder, media_filter=media_filter,
        )
        for folder, count, thumbs in folders:
            self.gallery_grid.append_folder(folder, count, thumbs)
        direct_folder = self.current_folder or "/"
        # NC items are merged in only at the root view of Pictures (NC has its
        # own folder layout that doesn't map onto local Pictures subfolders).
        include_nc = self._should_merge_nc() and self.current_folder in (None, "/")
        page = self._load_first_page(
            sort_mode, direct_folder,
            include_nc=include_nc, media_filter=media_filter,
        )
        for item in page:
            self.gallery_grid.append_media(item)
        total = len(folders) + len(self.current_items)
        self._set_empty_state(visible=total == 0)
        self._set_status("")

    def _render_date_groups(self, ascending: bool = False) -> None:
        order = "oldest" if ascending else "newest"
        include_nc = self._should_merge_nc()
        media_filter = self.settings.media_filter_for(self.category)
        page = self._load_first_page(
            order, self.current_folder,
            include_nc=include_nc, media_filter=media_filter,
        )
        self._date_last_key = None
        for item in page:
            self._append_date_grouped(item)
        self._set_status("")
        self._set_empty_state(visible=not self.current_items)

    def _append_date_grouped(self, item: MediaItem) -> None:
        dt = datetime.fromtimestamp(item.mtime)
        key = (dt.year, dt.month)
        if key != self._date_last_key:
            self.gallery_grid.append_header(
                self._month_header_markup(dt), year=dt.year, month=dt.month,
            )
            self._date_last_key = key
        self.gallery_grid.append_media(item)

    def _month_header_markup(self, dt: datetime) -> str:
        # Two-line month/year header; the year is sized relative to the
        # surrounding label so it scales with the .date-header CSS.
        month = GLib.markup_escape_text(self._(self._MONTH_NAMES_EN[dt.month - 1]))
        year = GLib.markup_escape_text(f"{dt.year:04d}")
        return (
            f"<span weight='600'>{month}</span>\n"
            f"<span size='65%' alpha='65%'>{year}</span>"
        )

    def _visible_child_folder_for_item(self, item_folder: str) -> str | None:
        if item_folder in ("", "/"):
            return None
        parent = self.current_folder
        if parent in (None, "/"):
            return item_folder.split("/", 1)[0]
        parent_prefix = f"{parent}/"
        if not item_folder.startswith(parent_prefix):
            return None
        remainder = item_folder[len(parent_prefix):]
        # Empty remainder means item_folder == parent_prefix (a stray
        # trailing slash); the item lives directly in `parent`, no child
        # folder to surface. The previous version chained an "or `"/" not
        # in remainder and item_folder == parent` clause whose second
        # half was unreachable after the startswith check above (parent
        # plus a slash can't equal parent). Dropped for clarity.
        if not remainder:
            return None
        return f"{parent}/{remainder.split('/', 1)[0]}"

    def _setup_lazy_loading(self) -> None:
        """Hook the scroll listener once per window — handler bails out itself
        when there's nothing more to load."""
        if self._lazy_loading_attached:
            return
        self.gallery_grid.get_vadjustment().connect("notify::value", self._on_scroll)
        self._lazy_loading_attached = True

    def _maybe_fill_viewport(self) -> bool:
        """If the freshly rendered first page didn't fill the visible area, keep
        loading more pages so the user actually has something to scroll."""
        if self._closing:
            return GLib.SOURCE_REMOVE
        if not self._has_more_items or self._lazy_loading_in_flight:
            return GLib.SOURCE_REMOVE
        vadj = self.gallery_grid.get_vadjustment()
        page = vadj.get_page_size()
        if page <= 0:
            # The scroller has not been allocated yet — at startup, or while
            # the window is unmapped. "No size" is not "nothing fits": reading
            # it as an empty viewport made this reload one page after another
            # until the whole library had been through the grid (measured: 99
            # rounds for a 20k library, all before the first frame). Wait for
            # a real measurement instead.
            self._fill_viewport_retries += 1
            if self._fill_viewport_retries <= self._FILL_VIEWPORT_MAX_RETRIES:
                GLib.timeout_add(50, self._maybe_fill_viewport)
            return GLib.SOURCE_REMOVE
        self._fill_viewport_retries = 0
        if vadj.get_upper() <= page + 1:
            self._load_more_items()
        return GLib.SOURCE_REMOVE

    def _on_scroll(self, vadj: Gtk.Adjustment, _param) -> None:
        if not self._has_more_items or self._lazy_loading_in_flight:
            return
        upper = vadj.get_upper()
        page = vadj.get_page_size()
        current = vadj.get_value()
        # Trigger when user is within one viewport of the bottom.
        if upper > 0 and current + page * 2 >= upper:
            self._load_more_items()

    def _load_more_items(self) -> None:
        if not self._has_more_items or self._current_offset >= self._total_count:
            self._has_more_items = False
            return
        self._lazy_loading_in_flight = True
        try:
            sort_mode = self.settings.get_sort_mode(self.category, self.current_folder)
            if sort_mode in ("date", "date_asc"):
                query_sort = "oldest" if sort_mode == "date_asc" else "newest"
                grouped = True
            else:
                query_sort = sort_mode
                grouped = False
            include_nc = self._should_merge_nc()
            if sort_mode == "folder" and not self._search_query:
                # Folder mode merges NC only at the root.
                include_nc = include_nc and self.current_folder in (None, "/")
                folder_arg: str | None = self.current_folder or "/"
            else:
                folder_arg = self.current_folder
            media_filter = self.settings.media_filter_for(self.category)
            if self._search_query:
                next_items = self.database.search_media(
                    self.category, self._search_query, query_sort, folder_arg,
                    include_nc=include_nc,
                    limit=self._page_size, offset=self._current_offset,
                    media_filter=media_filter,
                )
            else:
                next_items = self.database.list_media_paginated(
                    self.category, query_sort, folder_arg,
                    self._page_size, self._current_offset, include_nc=include_nc,
                    media_filter=media_filter,
                )
            if not next_items:
                self._has_more_items = False
                return

            # Make sure any partially filled tile row from the previous page is
            # flushed before we start a new chunk — otherwise headers (date mode)
            # would attach to a half-row and shift tiles around.
            self.gallery_grid.finish()

            for item in next_items:
                self.current_items.append(item)
                if grouped:
                    self._append_date_grouped(item)
                else:
                    self.gallery_grid.append_media(item)
            self.gallery_grid.finish()

            self._current_offset += len(next_items)
            self._has_more_items = self._current_offset < self._total_count
            # Cap memory + ListView load. Without this, repeatedly
            # jumping forward through months accumulates thousands of
            # rows and the grid grinds to a halt.
            self._evict_window_front_if_needed()
            LOGGER.debug(
                "Lazy-loaded %d more items (window: %d, db offset: %d..%d / %d)",
                len(next_items), len(self.current_items),
                self._window_start_offset, self._current_offset,
                self._total_count,
            )
        finally:
            self._lazy_loading_in_flight = False
        # If the fresh chunk still didn't fill the viewport (large screens with
        # tiny page size), keep going on the next idle.
        if self._has_more_items:
            GLib.idle_add(self._maybe_fill_viewport, priority=GLib.PRIORITY_LOW)

    def _evict_window_front_if_needed(self) -> None:
        """Drop the oldest loaded items when the window exceeds
        _MAX_LOADED_ITEMS. Eviction is aligned to a header boundary
        so the visible structure stays consistent: we trim whole
        month groups from the front, never half a group.

        The user complaint that triggered this: repeatedly jumping
        from month to month via the header arrow loads pages
        cumulatively (up to 32 pages of 200 items each per arrow tap),
        so after several hops the row_store holds many thousands of
        rows. ListView's allocation pass on that store starts to lag
        visibly. Capping the window keeps perceived scroll/jump
        latency flat regardless of how far the user has navigated.

        Reverse-load on scroll-back is not yet implemented; once the
        front is dropped, scrolling back above the new first row
        shows nothing further. That's an accepted trade-off until the
        symmetric path lands.
        """
        if len(self.current_items) <= self._MAX_LOADED_ITEMS:
            return
        target_remaining = max(self._page_size, self._MAX_LOADED_ITEMS // 2)
        target_evict = len(self.current_items) - target_remaining
        store = self.gallery_grid.row_store
        n_rows = store.get_n_items()
        items_dropped = 0
        rows_to_drop = 0
        # Walk forward in the row store, accumulating media items, until
        # we have at least `target_evict` items lined up for removal.
        while rows_to_drop < n_rows and items_dropped < target_evict:
            row = store.get_item(rows_to_drop)
            rows_to_drop += 1
            if row is None or row.is_header:
                continue
            items_dropped += len(getattr(row, "tiles", []) or [])
        # Align the cut to the next header so the new first row is
        # always a header — otherwise the topmost tile row would be
        # orphaned without its month context.
        while rows_to_drop < n_rows:
            row = store.get_item(rows_to_drop)
            if row is None:
                rows_to_drop += 1
                continue
            if row.is_header:
                break
            items_dropped += len(getattr(row, "tiles", []) or [])
            rows_to_drop += 1
        if rows_to_drop <= 0 or items_dropped <= 0:
            return
        # evict_front_rows splices the front AND prunes the grid's path/folder
        # indexes for the dropped rows (so they don't pin evicted MediaRows).
        # Gtk.ListView's internal scroll anchor keeps the currently-visible
        # row stable across the model edit. We deliberately don't compute a
        # vadj delta here: the upper bound only updates after the next
        # allocation pass, so any synchronous adjustment would race with
        # ListView's own repositioning and could compound into a worse jump.
        self.gallery_grid.evict_front_rows(rows_to_drop)
        self.current_items = self.current_items[items_dropped:]
        self._window_start_offset += items_dropped
