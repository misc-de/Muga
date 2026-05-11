from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from .models import MediaItem
from .nextcloud import is_nc_path

if TYPE_CHECKING:
    from .app import GalleryWindow


_MAX_COLS = 10  # maximum supported grid columns


class MediaRow(GObject.Object):
    """One cell in the gallery grid: either a folder or a media item.

    Selection state intentionally lives on the GalleryWindow (`_selected_paths`)
    rather than on the row — bind reads it from there at draw time, so the
    visible check-mark always tracks the live set even when the row was
    materialized before selection mode was entered."""

    __gtype_name__ = "YagaMediaRow"

    def __init__(self) -> None:
        super().__init__()
        self.media_item: MediaItem | None = None
        self.folder_path: str | None = None
        self.folder_count: int = 0
        self.folder_thumbs: list[str] = []

    @classmethod
    def from_media(cls, item: MediaItem) -> "MediaRow":
        row = cls()
        row.media_item = item
        return row

    @classmethod
    def from_folder(cls, folder: str, count: int, thumbs: list[str]) -> "MediaRow":
        row = cls()
        row.folder_path = folder
        row.folder_count = count
        row.folder_thumbs = thumbs
        return row

    @property
    def is_folder(self) -> bool:
        return self.folder_path is not None


class GalleryRow(GObject.Object):
    """One row in the gallery list: either a date-section header or a row of tiles."""

    __gtype_name__ = "YagaGalleryRow"

    def __init__(self) -> None:
        super().__init__()
        self.is_header: bool = False
        self.header_text: str = ""
        # Year / month carried alongside the header text so the per-header
        # up/down arrows can find adjacent headers in the row store without
        # re-parsing the markup.
        self.header_year: int | None = None
        self.header_month: int | None = None
        self.tiles: list[MediaRow] = []

    @classmethod
    def header(cls, text: str, year: int | None = None, month: int | None = None) -> "GalleryRow":
        row = cls()
        row.is_header = True
        row.header_text = text
        row.header_year = year
        row.header_month = month
        return row

    @classmethod
    def from_tiles(cls, tiles: list[MediaRow]) -> "GalleryRow":
        row = cls()
        row.tiles = list(tiles)
        return row


class _GalleryListView(Gtk.ListView):
    """ListView that exposes the GridView column-count API for interface compatibility."""

    def set_min_columns(self, _n: int) -> None:
        pass

    def set_max_columns(self, _n: int) -> None:
        pass


class GalleryGrid(Gtk.Overlay):
    def __init__(self, owner: "GalleryWindow") -> None:
        super().__init__()
        self.owner = owner
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._cols = 4
        self._building_row: list[MediaRow] = []

        # Stamped by the long-press handler; the click handler on the same
        # button uses it to ignore the release that follows a long-press.
        self._last_long_press_at = 0.0

        # Per-bind stat cache: Path.exists() on every thumbnail fires once
        # per visible tile and dominates scroll latency on slow disks (a
        # 6×20 viewport = 120 syscalls per layout pass). Cache for a short
        # TTL — long enough to coalesce a single layout/scroll burst, short
        # enough that newly-arrived thumbs and evictions are picked up by
        # the next bind without explicit invalidation.
        self._exists_cache: dict[str, tuple[float, bool]] = {}
        self._EXISTS_TTL = 5.0
        self._EXISTS_CACHE_MAX = 4096

        # Currently-bound list items, tracked so refresh_selection_state can
        # reach into the live widgets and re-run _bind_tile on each. Going
        # through Gio.ListStore.splice + items-changed proved unreliable —
        # ListView only re-binds tiles that scroll back into view, leaving
        # the currently-visible viewport stale until the user scrolls.
        self._bound_list_items: list[Gtk.ListItem] = []

        self.row_store = Gio.ListStore(item_type=GalleryRow)
        # When pagination appends new rows past the current last header, the
        # *previously* last header's down-arrow is now wrong (it shows
        # "hidden" because at bind time there was no header below). Listen
        # for net-add events on the store and refresh the visibility of all
        # currently-bound headers so the affordance tracks the live state.
        # In-place updates (splice with removed == added — e.g. thumbnail
        # arrivals) are skipped to avoid churn.
        self._refresh_arrows_pending = False
        self.row_store.connect("items-changed", self._on_row_store_items_changed)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_item_setup)
        factory.connect("bind", self._on_item_bind)
        factory.connect("unbind", self._on_item_unbind)

        self.grid_view = _GalleryListView()
        self.grid_view.set_model(Gtk.NoSelection(model=self.row_store))
        self.grid_view.set_factory(factory)
        self.grid_view.add_css_class("gallery-grid")
        self.grid_view.set_hexpand(True)
        self.grid_view.set_vexpand(True)
        self.grid_view.set_show_separators(False)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_hexpand(True)
        self.scroller.set_vexpand(True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.grid_view)

        self.empty_label = Gtk.Label()
        self.empty_label.set_halign(Gtk.Align.CENTER)
        self.empty_label.set_valign(Gtk.Align.CENTER)
        self.empty_label.add_css_class("dim-label")
        self.empty_label.add_css_class("title-3")
        self.empty_label.set_visible(False)

        spinner = Gtk.Spinner()
        spinner.set_size_request(28, 28)
        spinner.start()
        pull_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        pull_box.set_halign(Gtk.Align.CENTER)
        pull_box.set_valign(Gtk.Align.START)
        pull_box.set_margin_top(8)
        pull_box.append(spinner)
        self.pull_revealer = Gtk.Revealer()
        self.pull_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.pull_revealer.set_transition_duration(180)
        self.pull_revealer.set_child(pull_box)
        self.pull_revealer.set_halign(Gtk.Align.CENTER)
        self.pull_revealer.set_valign(Gtk.Align.START)
        self.pull_revealer.set_reveal_child(False)

        self.set_child(self.scroller)
        self.add_overlay(self.pull_revealer)
        self.add_overlay(self.empty_label)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_columns(self, columns: int) -> None:
        self._cols = min(max(int(columns), 2), _MAX_COLS)
        self.grid_view.set_min_columns(columns)
        self.grid_view.set_max_columns(columns)

    def clear(self) -> None:
        self.row_store.remove_all()
        self._building_row = []
        self.empty_label.set_visible(False)

    def finish(self) -> None:
        """Flush the last partial tile row. Call after all append_* calls."""
        self._flush_tile_row()

    def append_folder(self, folder: str, count: int, thumbs: list[str]) -> None:
        self._building_row.append(MediaRow.from_folder(folder, count, thumbs))
        if len(self._building_row) >= self._cols:
            self._flush_tile_row()

    def append_media(self, item: MediaItem) -> None:
        self._building_row.append(MediaRow.from_media(item))
        if len(self._building_row) >= self._cols:
            self._flush_tile_row()

    def append_header(self, text: str, year: int | None = None, month: int | None = None) -> None:
        self._flush_tile_row()
        self.row_store.append(GalleryRow.header(text, year=year, month=month))

    def set_empty(self, text: str, visible: bool) -> None:
        self.empty_label.set_label(text)
        self.empty_label.set_visible(visible)

    def get_vadjustment(self) -> Gtk.Adjustment:
        return self.scroller.get_vadjustment()

    def update_item_thumb(self, path: str, thumb_path: str) -> bool:
        # A fresh thumb just landed on disk — drop any stale "doesn't
        # exist" cache entry so the next bind sees the file.
        self._exists_cache.pop(thumb_path, None)
        # Hot path: NC sync of a folder fans out as a thumb-arrival storm.
        # The previous version spliced the entire GalleryRow on every
        # arrival, which forced ListView to re-bind every tile in that
        # row's widget — O(cols) work per arrival times N arrivals.
        # Instead we mutate the MediaRow's frozen MediaItem in place
        # (replacing it with a new instance — MediaRow is mutable) and
        # only re-bind the affected list_item's widget. Because
        # row_store and _bound_list_items reference the same MediaRow
        # objects, the mutation is visible to a later re-bind too if the
        # tile scrolls out and back.
        for list_item in self._bound_list_items:
            gallery_row = list_item.get_item()
            if gallery_row is None or gallery_row.is_header:
                continue
            for tile in gallery_row.tiles:
                if (
                    not tile.is_folder
                    and tile.media_item is not None
                    and tile.media_item.path == path
                ):
                    tile.media_item = dataclasses.replace(
                        tile.media_item, thumb_path=thumb_path,
                    )
                    self._apply_binding(list_item)
                    return True
        # Not currently bound but still in the model — mutate so the
        # next bind picks up the new thumb_path.
        n = self.row_store.get_n_items()
        for pos in range(n):
            gallery_row = self.row_store.get_item(pos)
            if gallery_row.is_header:
                continue
            for tile in gallery_row.tiles:
                if (
                    not tile.is_folder
                    and tile.media_item is not None
                    and tile.media_item.path == path
                ):
                    tile.media_item = dataclasses.replace(
                        tile.media_item, thumb_path=thumb_path,
                    )
                    return True
        return False

    def update_tile_for_path(self, path: str) -> bool:
        """Re-render the tile that holds *path*. Returns True if the path
        is in a currently-bound list item, False if the caller should
        fall back to a full re-render (path is in the model but scrolled
        out, or hasn't been lazy-loaded yet)."""
        for list_item in self._bound_list_items:
            row = list_item.get_item()
            if row is None or row.is_header:
                continue
            for tile in row.tiles:
                if (
                    not tile.is_folder
                    and tile.media_item is not None
                    and tile.media_item.path == path
                ):
                    self._apply_binding(list_item)
                    return True
        return False

    def _thumb_exists(self, path: str) -> bool:
        """Cached Path.exists() for thumbnail paths, scoped to the visible
        bind hot path. Falls back to a real stat after TTL expiry."""
        if not path:
            return False
        now = time.monotonic()
        cached = self._exists_cache.get(path)
        if cached is not None and now - cached[0] < self._EXISTS_TTL:
            return cached[1]
        ok = Path(path).exists()
        if len(self._exists_cache) >= self._EXISTS_CACHE_MAX:
            # Crude bulk eviction — drop half the entries to avoid an O(n²)
            # spiral on long scroll sessions. Keeps recency loosely via
            # insertion order (Python 3.7+ dict ordering).
            for k in list(self._exists_cache)[: self._EXISTS_CACHE_MAX // 2]:
                self._exists_cache.pop(k, None)
        self._exists_cache[path] = (now, ok)
        return ok

    def refresh_selection_state(self) -> None:
        """Re-render every currently-bound list item so checkbox visibility
        and per-tile check state catch up with the owner's current
        ``_selection_mode`` / ``_selected_paths``. Going through
        Gio.ListStore.splice + items-changed didn't reliably re-bind the
        already-visible viewport on GTK4 ListView (only newly-scrolled-in
        rows picked up the change), so we touch the live widgets
        directly."""
        for list_item in list(self._bound_list_items):
            self._apply_binding(list_item)

    def update_folder_thumb(self, folder_path: str, thumb_path: str) -> bool:
        self._exists_cache.pop(thumb_path, None)
        n = self.row_store.get_n_items()
        for pos in range(n):
            gallery_row = self.row_store.get_item(pos)
            if gallery_row.is_header:
                continue
            for j, tile in enumerate(gallery_row.tiles):
                if tile.is_folder and tile.folder_path == folder_path:
                    thumbs = [thumb_path, *[t for t in tile.folder_thumbs if t != thumb_path]][:4]
                    new_tiles = gallery_row.tiles[:]
                    new_tiles[j] = MediaRow.from_folder(tile.folder_path, tile.folder_count, thumbs)
                    self.row_store.splice(pos, 1, [GalleryRow.from_tiles(new_tiles)])
                    return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _flush_tile_row(self) -> None:
        if self._building_row:
            self.row_store.append(GalleryRow.from_tiles(self._building_row))
            self._building_row = []

    def _on_header_nav(self, header_box: Gtk.Box, direction: int) -> None:
        """Jump to the next/previous header from *header_box*'s row in a
        single, predictable vadjustment write.

        direction = -1 (up arrow) → previous header in row store
        direction = +1 (down arrow) → next header in row store

        Strategy: row heights are measured from currently-bound widgets
        (source header is always rendered; tile-row height is read from any
        bound tile-row in the viewport). Multiplying those measured heights
        by the count of intervening rows in row_store gives the target's
        content_y to within ListView's internal per-row padding (which is
        zero in our configuration). We then write the vadjustment exactly
        once so the target appears at the same screen Y the source was at —
        no scroll_to, no retry loop, no two-stage flicker.
        """
        list_item: Gtk.ListItem | None = getattr(header_box, "_list_item", None)
        if list_item is None:
            return
        current_pos = list_item.get_position()
        target_pos = self._find_adjacent_header_pos(current_pos, direction)
        # Going forward and we ran off the end of the currently-loaded
        # rows: pull the next page(s) until another month header appears
        # or there's nothing left in the database. Capped so a pathological
        # "every file is the same month" dataset can't lock up the UI.
        if target_pos is None and direction > 0:
            target_pos = self._load_more_until_next_header(current_pos)
        if target_pos is None:
            return
        src_widget = list_item.get_child()
        if src_widget is None:
            return
        src_y = self._content_y_of(src_widget)
        if src_y is None:
            return

        vadj = self.scroller.get_vadjustment()
        target_screen_y = src_y - vadj.get_value()

        header_h, tile_h = self._measure_row_heights(src_widget)
        delta = 0.0
        start = min(current_pos, target_pos)
        end   = max(current_pos, target_pos)
        for i in range(start, end):
            row = self.row_store.get_item(i)
            if row is None:
                continue
            delta += header_h if row.is_header else tile_h
        if target_pos < current_pos:
            delta = -delta

        target_content_y = src_y + delta
        self._set_vadj_clamped(vadj, target_content_y - target_screen_y)

    def _measure_row_heights(self, src_widget: Gtk.Widget) -> tuple[float, float]:
        """Return measured (header_h, tile_h) from currently-bound widgets.

        The source header is guaranteed to be rendered (the click just
        landed on it) so its allocation gives us the exact header row
        height. Tile row height is read from any tile-row list_item that's
        currently bound; if none happens to be bound (e.g. user clicked an
        arrow while no tiles were on screen), fall back to the CSS cell_size
        estimate. The fallback is rare and never compounds with header
        measurement, so the residual error stays inside a single tile-row
        height — small enough that the user's eye doesn't catch it."""
        header_h = float(src_widget.get_allocated_height() or 152)
        tile_h: float | None = None
        for li in list(self._bound_list_items):
            try:
                row = li.get_item()
                if row is None or row.is_header:
                    continue
                widget = li.get_child()
                if widget is None:
                    continue
                h = widget.get_allocated_height()
                if h > 0:
                    tile_h = float(h)
                    break
            except Exception:
                continue
        if tile_h is None:
            scroller_width = self.scroller.get_width() or 800
            cols = self._cols or 4
            tile_h = float(max(32, scroller_width // cols))
        return header_h, tile_h

    def _set_vadj_clamped(self, vadj: Gtk.Adjustment, value: float) -> None:
        upper = max(0.0, vadj.get_upper() - vadj.get_page_size())
        vadj.set_value(max(0.0, min(value, upper)))

    def _load_more_until_next_header(self, current_pos: int) -> int | None:
        """Drive the owner's paginated loader forward until a row with
        is_header=True shows up *after* current_pos, or the loader signals
        end-of-data. Returns the new header's row_store index, or None.

        The loader synchronously appends rows to row_store on each call,
        so polling the store after each step is safe. Capped at 32 pages
        — long enough to chew through "100 photos a month for years" but
        bounded against any edge case where pages somehow never contain
        a header."""
        owner = getattr(self, "owner", None)
        loader = getattr(owner, "_load_more_items", None)
        if loader is None:
            return None
        max_pages = 32
        for _ in range(max_pages):
            if not getattr(owner, "_has_more_items", False):
                return None
            before = self.row_store.get_n_items()
            loader()
            after = self.row_store.get_n_items()
            if after == before:
                # Loader bailed (in-flight guard, empty page, …) — give up
                # rather than spin.
                return None
            found = self._find_adjacent_header_pos(current_pos, +1)
            if found is not None:
                return found
        return None

    def _find_adjacent_header_pos(self, current_pos: int, direction: int) -> int | None:
        """Walk row_store from *current_pos* in *direction* until a header
        row is found. Returns the row index, or None if the boundary is hit
        without an adjacent header."""
        n = self.row_store.get_n_items()
        pos = current_pos + direction
        while 0 <= pos < n:
            row: GalleryRow | None = self.row_store.get_item(pos)
            if row is not None and row.is_header:
                return pos
            pos += direction
        return None

    def _set_header_arrow_visibility(self, list_item: Gtk.ListItem) -> None:
        """Apply per-header up/down arrow visibility based on whether there
        is an adjacent header in each direction. Extracted from
        _apply_binding so items-changed handling can re-run it without
        going through the full bind path."""
        stack = list_item.get_child()
        if stack is None:
            return
        try:
            pos = list_item.get_position()
        except Exception:
            return
        stack._nav_up.set_visible(self._find_adjacent_header_pos(pos, -1) is not None)
        # Show the down arrow whenever there is *or could be* a next
        # header — i.e. an already-loaded one further down, or more
        # paginated items still waiting in the database. The handler
        # itself drives pagination forward in the second case.
        has_next_loaded = self._find_adjacent_header_pos(pos, +1) is not None
        owner = getattr(self, "owner", None)
        has_more_db = bool(getattr(owner, "_has_more_items", False))
        stack._nav_down.set_visible(has_next_loaded or has_more_db)

    def _on_row_store_items_changed(
        self, _store: Gio.ListStore, _position: int, removed: int, added: int,
    ) -> None:
        """Refresh arrow visibility on already-bound headers when the store
        gains rows. Pagination is the main case: a new header at the end
        of the store turns the previously-last header's down-arrow from
        "hidden" to "visible", but no fresh bind fires for that row
        (its position is unchanged). Debounced via idle so a burst of
        appends only triggers one refresh pass."""
        if added <= removed:
            return  # in-place replacement or pure removal — no new headers
        if self._refresh_arrows_pending:
            return
        self._refresh_arrows_pending = True
        GLib.idle_add(self._refresh_header_arrows_on_idle,
                      priority=GLib.PRIORITY_LOW)

    def _refresh_header_arrows_on_idle(self) -> bool:
        self._refresh_arrows_pending = False
        for li in list(self._bound_list_items):
            try:
                row = li.get_item()
                if row is None or not row.is_header:
                    continue
                self._set_header_arrow_visibility(li)
            except Exception:
                continue
        return GLib.SOURCE_REMOVE

    def _content_y_of(self, widget: Gtk.Widget) -> float | None:
        """Y of *widget* within the listview's scrollable content coord
        space. Independent of current scroll value — that's exactly what we
        need to translate back into a vadjustment offset."""
        ok, bounds = widget.compute_bounds(self.grid_view)
        if not ok:
            return None
        return bounds.get_y()

    # ------------------------------------------------------------------
    # Factory callbacks
    # ------------------------------------------------------------------

    def _on_item_setup(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        # ── Header widget ────────────────────────────────────────────
        # Two-line month/year label centered in the row, with a stacked pair
        # of up/down arrows pinned to the trailing edge. The arrows jump to
        # the previous/next month header in the row store.
        header_lbl = Gtk.Label()
        header_lbl.set_halign(Gtk.Align.FILL)
        header_lbl.set_xalign(0.5)
        header_lbl.set_justify(Gtk.Justification.CENTER)
        header_lbl.set_hexpand(True)
        header_lbl.add_css_class("date-header")

        nav_up = Gtk.Button.new_from_icon_name("go-up-symbolic")
        nav_up.add_css_class("flat")
        nav_up.add_css_class("date-header-nav")
        nav_up.set_tooltip_text("Previous month")
        # set_visible(False) on per-bind basis: the first (newest) header has
        # no previous month, the last (oldest) has no next month. Hidden
        # buttons still occupy layout space; using set_opacity / set_sensitive
        # would keep them visually present, but the user asked for them gone
        # at the boundaries. Layout adapts because the parent vbox doesn't
        # reserve a slot for an invisible child.
        nav_down = Gtk.Button.new_from_icon_name("go-down-symbolic")
        nav_down.add_css_class("flat")
        nav_down.add_css_class("date-header-nav")
        nav_down.set_tooltip_text("Next month")

        nav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        nav_box.set_halign(Gtk.Align.END)
        nav_box.set_valign(Gtk.Align.CENTER)
        nav_box.set_margin_end(12)
        nav_box.append(nav_up)
        nav_box.append(nav_down)

        # CenterBox keeps the date label anchored to the row's geometric
        # center even when only one side (here: end) carries a widget.
        # A plain HBox would size the label to "row width minus nav_box",
        # shifting the visual center off-axis.
        header_box = Gtk.CenterBox()
        header_box.set_hexpand(True)
        header_box.set_center_widget(header_lbl)
        header_box.set_end_widget(nav_box)

        # Capture the list_item closures bind the click handler against — the
        # bind step refreshes _header_list_item before each row is shown, so a
        # recycled pool widget always navigates from its current row's date.
        nav_up.connect("clicked", lambda _b: self._on_header_nav(header_box, -1))
        nav_down.connect("clicked", lambda _b: self._on_header_nav(header_box, +1))

        # ── Tile row widget ──────────────────────────────────────────
        tile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        tile_box.set_hexpand(True)
        tile_box.set_homogeneous(True)

        tile_buttons: list[Gtk.Button] = []
        for i in range(_MAX_COLS):
            btn = self._make_tile_button(i, list_item)
            tile_box.append(btn)
            tile_buttons.append(btn)

        # ── Stack switching between header and tiles ─────────────────
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.NONE)
        stack.add_named(header_box, "header")
        stack.add_named(tile_box, "tiles")
        stack._header_lbl = header_lbl       # type: ignore[attr-defined]
        stack._header_box = header_box       # type: ignore[attr-defined]
        stack._nav_up = nav_up               # type: ignore[attr-defined]
        stack._nav_down = nav_down           # type: ignore[attr-defined]
        stack._tile_buttons = tile_buttons   # type: ignore[attr-defined]
        # Stamped fresh on every bind in _apply_binding so the recycled pool
        # widget's arrow handlers always see the current row's list_item.
        header_box._list_item = None         # type: ignore[attr-defined]

        list_item.set_child(stack)

    def _make_tile_button(self, tile_index: int, list_item: Gtk.ListItem) -> Gtk.Button:
        single_pic = Gtk.Picture()
        single_pic.set_content_fit(Gtk.ContentFit.COVER)
        single_pic.set_hexpand(False)
        single_pic.set_vexpand(False)

        pic_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        pic_row1.set_vexpand(False)
        pic_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        pic_row2.set_vexpand(False)
        preview_pics: list[Gtk.Picture] = []
        for i in range(4):
            picture = Gtk.Picture()
            picture.set_content_fit(Gtk.ContentFit.COVER)
            picture.set_hexpand(False)
            picture.set_vexpand(False)
            preview_pics.append(picture)
            (pic_row1 if i < 2 else pic_row2).append(picture)

        pic_grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pic_grid.set_hexpand(False)
        pic_grid.set_vexpand(False)
        pic_grid.append(pic_row1)
        pic_grid.append(pic_row2)
        pic_grid.set_visible(False)

        badge = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        badge.add_css_class("osd")
        badge.set_halign(Gtk.Align.CENTER)
        badge.set_valign(Gtk.Align.CENTER)
        badge.set_visible(False)

        folder_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END)
        folder_label.add_css_class("folder-label")
        folder_label.set_halign(Gtk.Align.FILL)
        folder_label.set_valign(Gtk.Align.END)
        folder_label.set_hexpand(True)
        folder_label.set_visible(False)

        check = Gtk.Image.new_from_icon_name("checkbox-symbolic")
        check.set_halign(Gtk.Align.END)
        check.set_valign(Gtk.Align.START)
        check.add_css_class("sel-check")
        check.set_visible(False)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(single_pic)
        overlay.add_overlay(pic_grid)
        overlay.add_overlay(badge)
        overlay.add_overlay(folder_label)
        overlay.add_overlay(check)

        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("gallery-tile")
        button.set_hexpand(False)
        button.set_vexpand(False)
        button.set_child(overlay)

        button._single_pic = single_pic        # type: ignore[attr-defined]
        button._preview_pics = preview_pics    # type: ignore[attr-defined]
        button._pic_grid = pic_grid            # type: ignore[attr-defined]
        button._badge = badge                  # type: ignore[attr-defined]
        button._folder_label = folder_label    # type: ignore[attr-defined]
        button._check = check                  # type: ignore[attr-defined]
        button._tile_index = tile_index        # type: ignore[attr-defined]
        button._current_item: MediaRow | None = None  # type: ignore[attr-defined]

        button.connect("clicked", self._on_tile_clicked, list_item)

        right_click = Gtk.GestureClick(button=3)
        right_click.connect("pressed", self._on_tile_right_click, list_item, tile_index)
        button.add_controller(right_click)

        # 2-second long-press → enter selection mode. GtkGestureLongPress is
        # hard-capped at delay_factor 2.0 (≤ 1 s on a 500 ms system default),
        # so we roll our own with a GLib timeout. The press detector runs in
        # CAPTURE phase so set_state(CLAIMED) on fire propagates to the
        # button's built-in click gesture and suppresses the trailing
        # release-click. The timestamp guard in _on_tile_clicked is a belt-
        # and-suspenders fallback for any GTK routing edge cases.
        press_g = Gtk.GestureClick.new()
        press_g.set_button(1)  # primary mouse + touch (touch reports as 1)
        press_g.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        press_g.set_exclusive(False)
        press_g.connect("pressed", self._on_tile_press, list_item, tile_index)
        press_g.connect("released", self._on_tile_release_or_cancel)
        press_g.connect("cancel", self._on_tile_press_cancel)
        button.add_controller(press_g)

        # Cancel the pending long-press if the pointer drifts beyond a small
        # threshold — typical of a scroll gesture or a drag, neither of which
        # should trigger selection mode.
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_tile_motion, press_g)
        button.add_controller(motion)

        swipe = Gtk.GestureSwipe()
        swipe.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        swipe.connect("swipe", self._on_tile_swipe)
        button.add_controller(swipe)

        return button

    def _on_item_bind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        if list_item not in self._bound_list_items:
            self._bound_list_items.append(list_item)
        self._apply_binding(list_item)

    def _apply_binding(self, list_item: Gtk.ListItem) -> None:
        """Render *list_item* against its current model row. Pulled out of
        _on_item_bind so refresh_selection_state can re-run it for every
        already-bound list item without going through items-changed."""
        gallery_row: GalleryRow | None = list_item.get_item()
        stack = list_item.get_child()
        if gallery_row is None or stack is None:
            return

        if gallery_row.is_header:
            stack.set_visible_child_name("header")
            # header_text may include Pango markup (e.g. month/year two-liner)
            stack._header_lbl.set_markup(gallery_row.header_text)
            # Stamp the current list_item so the arrow callbacks know which
            # position to navigate from (pool widgets are recycled, so the
            # closure captured at setup time has no row identity on its own).
            stack._header_box._list_item = list_item
            # Hide the up arrow at the first (newest) header and the down
            # arrow at the last (oldest); show both at any header that has
            # neighbours in both directions. Pool widgets are recycled, so
            # this runs on every bind. Pagination keeps the visibility live
            # via _on_row_store_items_changed → _refresh_header_arrows_on_idle.
            self._set_header_arrow_visibility(list_item)
            return

        stack.set_visible_child_name("tiles")
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        # Keep exactly self._cols slots visible so every tile stays at 1/cols width;
        # empty trailing slots are bound as invisible placeholders (not hidden).
        for i, btn in enumerate(stack._tile_buttons):
            if i >= self._cols:
                btn.set_visible(False)
                btn._current_item = None
                continue
            btn.set_visible(True)
            if i < len(gallery_row.tiles):
                self._bind_tile(btn, gallery_row.tiles[i], icon_theme)
            else:
                self._bind_empty_tile(btn)

    def _bind_empty_tile(self, button: Gtk.Button) -> None:
        """Bind a placeholder cell that holds its 1/cols slot but renders nothing."""
        button._current_item = None
        button._single_pic.set_paintable(None)
        button._single_pic.set_visible(False)
        button._pic_grid.set_visible(False)
        for picture in button._preview_pics:
            picture.set_paintable(None)
            picture.set_visible(False)
        button._badge.set_visible(False)
        button._folder_label.set_visible(False)
        button._check.set_visible(False)
        button.set_sensitive(False)
        button.set_can_target(False)
        button.add_css_class("empty")

    def _bind_tile(self, button: Gtk.Button, row: MediaRow, icon_theme) -> None:
        button._current_item = row
        button.set_can_target(True)
        button.remove_css_class("empty")
        single_pic: Gtk.Picture = button._single_pic
        preview_pics: list[Gtk.Picture] = button._preview_pics
        pic_grid: Gtk.Widget = button._pic_grid
        badge: Gtk.Image = button._badge
        folder_label: Gtk.Label = button._folder_label
        check: Gtk.Image = button._check

        if row.is_folder:
            valid_thumbs = [t for t in row.folder_thumbs if self._thumb_exists(t)]
            if len(valid_thumbs) >= 2:
                single_pic.set_visible(False)
                pic_grid.set_visible(True)
                for i, picture in enumerate(preview_pics):
                    if i < len(valid_thumbs):
                        picture.set_filename(valid_thumbs[i])
                        picture.set_visible(True)
                    else:
                        picture.set_paintable(None)
                        picture.set_visible(False)
            else:
                single_pic.set_visible(True)
                pic_grid.set_visible(False)
                if valid_thumbs:
                    single_pic.set_filename(valid_thumbs[0])
                else:
                    single_pic.set_paintable(
                        icon_theme.lookup_icon(
                            "folder-pictures-symbolic", None, 96, 1,
                            Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE,
                        )
                    )
            label = row.folder_path.rsplit("/", 1)[-1] if row.folder_path != "/" else "/"
            folder_label.set_label(label)
            folder_label.set_halign(Gtk.Align.FILL)
            folder_label.set_valign(Gtk.Align.END)
            folder_label.set_visible(True)
            badge.set_visible(False)
            check.set_visible(False)
            button.set_sensitive(True)
            return

        item = row.media_item
        assert item is not None
        single_pic.set_visible(True)
        pic_grid.set_visible(False)
        if item.thumb_path and self._thumb_exists(item.thumb_path):
            single_pic.set_filename(item.thumb_path)
        elif is_nc_path(item.path):
            single_pic.set_paintable(
                icon_theme.lookup_icon(
                    "image-x-generic-symbolic", None, 96, 1,
                    Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE,
                )
            )
            # Fetch the NC thumbnail in the background; gallery is updated when it arrives.
            requester = getattr(self.owner, "request_nc_thumbnail", None)
            if requester is not None:
                requester(item.path)
        else:
            single_pic.set_filename(item.path)
        badge.set_visible(item.is_video and not self.owner._selection_mode)
        folder_label.set_visible(False)
        if self.owner._selection_mode:
            check.set_visible(True)
            if item.path in self.owner._selected_paths:
                check.set_from_icon_name("checkbox-checked-symbolic")
                check.add_css_class("checked")
            else:
                check.set_from_icon_name("checkbox-symbolic")
                check.remove_css_class("checked")
        else:
            check.set_visible(False)
            check.remove_css_class("checked")
        button.set_sensitive(True)

    def _on_item_unbind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        if list_item in self._bound_list_items:
            self._bound_list_items.remove(list_item)
        stack = list_item.get_child()
        for btn in stack._tile_buttons:
            btn._single_pic.set_paintable(None)
            btn._current_item = None
            for picture in btn._preview_pics:
                picture.set_paintable(None)

    # ------------------------------------------------------------------
    # Gesture / click callbacks
    # ------------------------------------------------------------------

    def _get_tile_item(self, list_item: Gtk.ListItem, tile_index: int) -> MediaRow | None:
        gallery_row: GalleryRow | None = list_item.get_item()
        if gallery_row is None or gallery_row.is_header:
            return None
        if tile_index >= len(gallery_row.tiles):
            return None
        return gallery_row.tiles[tile_index]

    def _on_tile_clicked(self, button: Gtk.Button, list_item: Gtk.ListItem) -> None:
        row = button._current_item
        if row is None:
            return
        # Long-press release fires the button's "clicked" signal too; without
        # this guard the long-press would enter selection mode and the same
        # release-event would immediately toggle it back off.
        if time.monotonic() - self._last_long_press_at < 0.4:
            return
        if self.owner._selection_mode:
            if not row.is_folder and row.media_item is not None:
                self.owner._toggle_selection(row.media_item.path)
            return
        if row.is_folder:
            self.owner._open_folder(None, row.folder_path)
        else:
            self.owner._open_item(None, row.media_item)

    def _on_tile_right_click(
        self,
        gesture: Gtk.GestureClick,
        _n: int,
        x: float,
        y: float,
        list_item: Gtk.ListItem,
        tile_index: int,
    ) -> None:
        row = self._get_tile_item(list_item, tile_index)
        if row is None or row.is_folder:
            return
        widget = gesture.get_widget()
        self.owner._show_context_menu(gesture, 1, x, y, row.media_item, widget)

    # ── Custom 2-second long-press ───────────────────────────────────
    # GtkGestureLongPress maxes out around 1 s; we want a slower, more
    # deliberate hold before entering selection mode (the user explicitly
    # asked for "after holding two seconds"). Per-press state lives on the
    # gesture object so multiple touch sequences across tiles don't
    # clobber each other.

    # 2000 ms feels like ~3 s in practice (event-routing latency on top of
    # the timer); 1300 ms lands at the user's "two-ish seconds" mark.
    _LONG_PRESS_HOLD_MS = 1300
    _LONG_PRESS_MOVE_THRESHOLD_SQ = 16.0 * 16.0  # ~16 px before we abort

    def _on_tile_press(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        list_item: Gtk.ListItem,
        tile_index: int,
    ) -> None:
        self._abort_long_press(gesture)
        gesture._press_x = x       # type: ignore[attr-defined]
        gesture._press_y = y       # type: ignore[attr-defined]
        gesture._long_press_timer_id = GLib.timeout_add(  # type: ignore[attr-defined]
            self._LONG_PRESS_HOLD_MS,
            self._fire_long_press,
            gesture,
            list_item,
            tile_index,
        )

    def _on_tile_release_or_cancel(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
    ) -> None:
        self._abort_long_press(gesture)

    def _on_tile_press_cancel(self, gesture: Gtk.GestureClick, _seq) -> None:
        self._abort_long_press(gesture)

    def _on_tile_motion(
        self,
        _ctrl: Gtk.EventControllerMotion,
        x: float,
        y: float,
        gesture: Gtk.GestureClick,
    ) -> None:
        timer_id = getattr(gesture, "_long_press_timer_id", 0)
        if not timer_id:
            return
        dx = x - getattr(gesture, "_press_x", 0.0)
        dy = y - getattr(gesture, "_press_y", 0.0)
        if dx * dx + dy * dy > self._LONG_PRESS_MOVE_THRESHOLD_SQ:
            self._abort_long_press(gesture)

    def _abort_long_press(self, gesture: Gtk.GestureClick) -> None:
        timer_id = getattr(gesture, "_long_press_timer_id", 0)
        if timer_id:
            GLib.source_remove(timer_id)
        gesture._long_press_timer_id = 0  # type: ignore[attr-defined]

    def _fire_long_press(
        self,
        gesture: Gtk.GestureClick,
        list_item: Gtk.ListItem,
        tile_index: int,
    ) -> bool:
        gesture._long_press_timer_id = 0  # type: ignore[attr-defined]
        row = self._get_tile_item(list_item, tile_index)
        # Folders and empty cells aren't selectable — bail without entering
        # selection mode so a stale tile that recycled into a folder slot
        # doesn't surprise the user.
        if row is None or row.is_folder or row.media_item is None:
            return GLib.SOURCE_REMOVE
        self._last_long_press_at = time.monotonic()
        try:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except Exception:
            pass
        if not self.owner._selection_mode:
            self.owner._enter_selection_mode()
        self.owner._toggle_selection(row.media_item.path)
        return GLib.SOURCE_REMOVE

    def _on_tile_swipe(self, gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        self.owner._on_folder_swipe(gesture, velocity_x, velocity_y)
