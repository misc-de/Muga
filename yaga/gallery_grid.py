from __future__ import annotations

import dataclasses
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
    """One cell in the gallery grid: either a folder or a media item."""

    __gtype_name__ = "YagaMediaRow"

    def __init__(self) -> None:
        super().__init__()
        self.media_item: MediaItem | None = None
        self.folder_path: str | None = None
        self.folder_count: int = 0
        self.folder_thumbs: list[str] = []
        self.selected: bool = False

    @classmethod
    def from_media(cls, item: MediaItem, selected: bool = False) -> "MediaRow":
        row = cls()
        row.media_item = item
        row.selected = selected
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
        self.tiles: list[MediaRow] = []

    @classmethod
    def header(cls, text: str) -> "GalleryRow":
        row = cls()
        row.is_header = True
        row.header_text = text
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

        self.row_store = Gio.ListStore(item_type=GalleryRow)
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

    def append_media(self, item: MediaItem, selected: bool = False) -> None:
        self._building_row.append(MediaRow.from_media(item, selected))
        if len(self._building_row) >= self._cols:
            self._flush_tile_row()

    def append_header(self, text: str) -> None:
        self._flush_tile_row()
        self.row_store.append(GalleryRow.header(text))

    def set_empty(self, text: str, visible: bool) -> None:
        self.empty_label.set_label(text)
        self.empty_label.set_visible(visible)

    def get_vadjustment(self) -> Gtk.Adjustment:
        return self.scroller.get_vadjustment()

    def update_item_thumb(self, path: str, thumb_path: str) -> bool:
        n = self.row_store.get_n_items()
        for pos in range(n):
            gallery_row = self.row_store.get_item(pos)
            if gallery_row.is_header:
                continue
            for j, row in enumerate(gallery_row.tiles):
                if not row.is_folder and row.media_item and row.media_item.path == path:
                    new_tiles = gallery_row.tiles[:]
                    updated = dataclasses.replace(row.media_item, thumb_path=thumb_path)
                    new_tiles[j] = MediaRow.from_media(updated, row.selected)
                    self.row_store.splice(pos, 1, [GalleryRow.from_tiles(new_tiles)])
                    return True
        return False

    def update_folder_thumb(self, folder_path: str, thumb_path: str) -> bool:
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

    # ------------------------------------------------------------------
    # Factory callbacks
    # ------------------------------------------------------------------

    def _on_item_setup(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        # ── Header widget ────────────────────────────────────────────
        header_lbl = Gtk.Label()
        header_lbl.set_halign(Gtk.Align.FILL)
        header_lbl.set_xalign(0.0)
        header_lbl.set_hexpand(True)
        header_lbl.set_margin_start(10)
        header_lbl.add_css_class("date-header")

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
        stack.add_named(header_lbl, "header")
        stack.add_named(tile_box, "tiles")
        stack._header_lbl = header_lbl       # type: ignore[attr-defined]
        stack._tile_buttons = tile_buttons   # type: ignore[attr-defined]

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

        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", self._on_tile_right_click, list_item, tile_index)
        button.add_controller(gesture)

        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", self._on_tile_long_press, list_item, tile_index)
        button.add_controller(long_press)

        swipe = Gtk.GestureSwipe()
        swipe.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        swipe.connect("swipe", self._on_tile_swipe)
        button.add_controller(swipe)

        return button

    def _on_item_bind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        gallery_row: GalleryRow = list_item.get_item()
        stack = list_item.get_child()

        if gallery_row.is_header:
            stack.set_visible_child_name("header")
            stack._header_lbl.set_label(gallery_row.header_text)
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
            valid_thumbs = [t for t in row.folder_thumbs if t and Path(t).exists()]
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
        if item.thumb_path and Path(item.thumb_path).exists():
            single_pic.set_filename(item.thumb_path)
        elif is_nc_path(item.path):
            single_pic.set_paintable(
                icon_theme.lookup_icon(
                    "image-x-generic-symbolic", None, 96, 1,
                    Gtk.TextDirection.NONE, Gtk.IconLookupFlags.NONE,
                )
            )
        else:
            single_pic.set_filename(item.path)
        badge.set_visible(item.is_video and not self.owner._selection_mode)
        folder_label.set_visible(False)
        if self.owner._selection_mode:
            check.set_visible(True)
            if row.selected:
                check.set_from_icon_name("checkbox-checked-symbolic")
                check.add_css_class("checked")
            else:
                check.set_from_icon_name("checkbox-symbolic")
                check.remove_css_class("checked")
        else:
            check.set_visible(False)
        button.set_sensitive(True)

    def _on_item_unbind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
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

    def _on_tile_long_press(
        self,
        _gesture,
        _x,
        _y,
        list_item: Gtk.ListItem,
        tile_index: int,
    ) -> None:
        row = self._get_tile_item(list_item, tile_index)
        if row is None or row.is_folder or row.media_item is None:
            return
        if not self.owner._selection_mode:
            self.owner._enter_selection_mode()
        self.owner._toggle_selection(row.media_item.path)

    def _on_tile_swipe(self, gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        self.owner._on_folder_swipe(gesture, velocity_x, velocity_y)
