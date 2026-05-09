from __future__ import annotations

import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import signal
import shlex
import subprocess
import sys
import threading
import shutil
import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk

from . import APP_ID, APP_NAME
from .config import DEBUG_LOG_PATH, Settings, THUMB_DIR
from .database import Database
from .gallery_grid import GalleryGrid
from .i18n import Translator
from .models import MediaItem
from .settings_window import SettingsWindow
from .scanner import MediaScanner
from .thumbnails import Thumbnailer
from .viewer import ViewerWindow

LOGGER = logging.getLogger(__name__)


def _configure_debug_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        return
    DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(DEBUG_LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)


def _enable_thread_dump_signal() -> None:
    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
        except RuntimeError:
            LOGGER.debug("Could not register SIGUSR1 thread dump handler", exc_info=True)


def _cleanup_abandoned_temp_files() -> None:
    """Clean up leftover _edit_*.jpg files from crashes or interrupted edits."""
    try:
        home = Path.home()
        # Scan common photo directories for orphaned temp files
        for pattern_dir in [home / "Pictures", home / "Photos", home / "Downloads"]:
            if pattern_dir.exists():
                for temp_file in pattern_dir.rglob("*_edit_*.jpg"):
                    try:
                        temp_file.unlink(missing_ok=True)
                        LOGGER.debug("Cleaned up temp file: %s", temp_file)
                    except OSError as e:
                        LOGGER.debug("Could not remove temp file %s: %s", temp_file, e)
    except Exception as e:
        LOGGER.debug("Temp file cleanup failed: %s", e)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class GalleryApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        _configure_debug_logging()
        _enable_thread_dump_signal()
        GLib.set_application_name(APP_NAME)
        self.connect("activate", self.on_activate)

    def on_activate(self, _app: Adw.Application) -> None:
        # If --trace is active, prove the main loop is alive via a 1 Hz heartbeat
        # so the watchdog can distinguish "idle main loop" from "frozen main loop".
        if "yaga.tracer" in sys.modules:
            sys.modules["yaga.tracer"].start_heartbeat()

        icons_dir = Path(__file__).parent / "data" / "icons"
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icons_dir))

        # Cleanup leftover temp files from previous sessions
        _cleanup_abandoned_temp_files()

        window = GalleryWindow(self)
        window.present()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class GalleryWindow(Adw.ApplicationWindow):
    def __init__(self, app: GalleryApplication) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1120, 760)
        self.set_icon_name(APP_ID)

        self.settings = Settings.load()
        self.translator = Translator(self.settings.language)
        self.database = Database()
        self.thumbnailer = Thumbnailer()
        self.scanner = MediaScanner(self.database, self.thumbnailer)
        self.category = self._first_existing_category()
        self.current_folder: str | None = None
        self.current_items: list[MediaItem] = []
        self.category_buttons: dict[str, Gtk.ToggleButton] = {}
        self._selection_mode: bool = False
        self._selected_paths: set[str] = set()
        self._nc_spinner: Gtk.Spinner | None = None
        self._nc_broken_img: Gtk.Image | None = None
        # On-demand NC thumbnail loader (used by gallery_grid when binding tiles)
        self._nc_thumb_pending: set[str] = set()
        self._nc_thumb_lock = threading.Lock()
        self._nc_thumb_queue: list[str] = []
        self._nc_thumb_event = threading.Event()
        self._nc_thumb_active_workers = 0
        self._nc_thumb_worker_target = 4  # parallel HTTPS thumb fetchers
        self._nc_thumb_shared_client = None  # lazily built, reused across workers
        # Coalesced thumbnail updates from the worker → batched on the main loop
        self._pending_thumb_updates: dict[str, str] = {}
        self._pending_thumb_lock = threading.Lock()
        self._pending_thumb_idle = 0

        # Pagination for large galleries
        self._page_size: int = 200  # Items per page
        self._current_offset: int = 0
        self._total_count: int = 0
        self._has_more_items: bool = False
        self._date_last_key: tuple[int, int] | None = None  # (year, month) of last date header
        self._lazy_loading_attached: bool = False
        self._lazy_loading_in_flight: bool = False

        # Track last-rendered view so we can preserve scroll position on refresh
        self._last_render_key: tuple[str, str | None] | None = None

        # Dynamic tile-size CSS (updated via tick callback whenever the scroller resizes)
        self._tile_css = Gtk.CssProvider()
        self._grid_width = 0

        self._apply_theme()
        self._load_css()
        self._build_ui()
        Adw.StyleManager.get_default().connect("notify::dark", self._on_system_theme_changed)
        self.refresh(scan=True)

    def _(self, text: str) -> str:
        return self.translator.gettext(text)

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)
        self.status.set_visible(bool(text))

    def _should_merge_nc(self) -> bool:
        """True when NC items should be folded into the current Pictures view."""
        return (
            self.category == "pictures"
            and self.settings.nextcloud_enabled
            and getattr(self.settings, "nextcloud_show_in_pictures", False)
        )

    def _set_empty_state(self, visible: bool) -> None:
        """Pick an appropriate empty-state label for the current view."""
        missing = self.scanner.missing_root.get(self.category)
        if visible and missing is not None:
            display = self.current_folder or Path(missing).name or missing
            text = self._("Folder %s not found") % display
        else:
            text = self._("No pictures found")
        self.gallery_grid.set_empty(text, visible)

    def _show_error_dialog(self, title: str, message: str, details: str = "") -> None:
        """Show an error dialog with title, message, and optional details."""
        dialog = Adw.AlertDialog(heading=title, body=message)
        dialog.add_response("close", self._("Close"))
        dialog.set_default_response("close")
        if details:
            dialog.set_body(f"{message}\n\n{details}")
        dialog.present(self)

    def _handle_file_error(self, error: Exception, file_path: str = "") -> None:
        """Handle file-related errors with specific messages."""
        if isinstance(error, FileNotFoundError):
            self._show_error_dialog(
                self._("File not found"),
                self._("Could not access the file. It may have been moved, deleted, or you don't have permission."),
                f"Path: {file_path}" if file_path else ""
            )
        elif isinstance(error, PermissionError):
            self._show_error_dialog(
                self._("Permission denied"),
                self._("You don't have permission to access this file."),
                f"Path: {file_path}" if file_path else ""
            )
        elif isinstance(error, OSError):
            details = str(error) if str(error) else ""
            self._show_error_dialog(
                self._("System error"),
                self._("Could not access the file due to a system error."),
                details
            )
        else:
            self._show_error_dialog(
                self._("Error"),
                self._("An unexpected error occurred."),
                str(error)
            )

    def _handle_nextcloud_error(self, error: Exception) -> None:
        """Handle Nextcloud-specific errors with recovery suggestions."""
        if isinstance(error, PermissionError):
            self._show_error_dialog(
                self._("Nextcloud authentication failed"),
                self._("The app password is incorrect or the account has been revoked. Check Nextcloud settings."),
                str(error)
            )
        elif isinstance(error, FileNotFoundError):
            self._show_error_dialog(
                self._("Nextcloud path not found"),
                self._("The folder or file doesn't exist on the Nextcloud server. It may have been deleted."),
                str(error)
            )
        elif isinstance(error, ConnectionError):
            self._show_error_dialog(
                self._("Connection failed"),
                self._("Could not connect to Nextcloud. Check your internet connection and server URL."),
                ""
            )
        else:
            self._show_error_dialog(
                self._("Nextcloud error"),
                self._("An error occurred while accessing Nextcloud."),
                str(error)
            )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.toolbar = Adw.ToolbarView()
        self.set_content(self.toolbar)

        self.header = Adw.HeaderBar()
        self.toolbar.add_top_bar(self.header)

        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.back_button.set_tooltip_text(self._("Back"))
        self.back_button.connect("clicked", self._on_back)
        self.header.pack_start(self.back_button)

        self.new_folder_button = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self.new_folder_button.set_tooltip_text(self._("New folder"))
        self.new_folder_button.connect("clicked", lambda _b: self._prompt_new_folder())
        self.header.pack_start(self.new_folder_button)

        title = Adw.WindowTitle(title=APP_NAME, subtitle="")
        self.header.set_title_widget(title)

        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_button.set_tooltip_text(self._("Refresh"))
        self.refresh_button.connect("clicked", lambda _b: self.refresh(scan=True))

        self.settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        self.settings_button.set_tooltip_text(self._("Settings"))
        self.settings_button.connect("clicked", self._open_settings)
        self.header.pack_start(self.settings_button)

        self.sort_button = Gtk.MenuButton(icon_name="view-sort-descending-symbolic")
        self.sort_button.set_tooltip_text(self._("Sort"))
        self.sort_button.set_popover(self._sort_popover())
        self.header.pack_end(self.sort_button)

        # ── Selection-mode header widgets (hidden until long-press activates) ──
        self._sel_cancel_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        self._sel_cancel_btn.set_tooltip_text(self._("Cancel selection"))
        self._sel_cancel_btn.set_visible(False)
        self._sel_cancel_btn.connect("clicked", lambda _: self._exit_selection_mode())
        self.header.pack_start(self._sel_cancel_btn)

        self._sel_title = Adw.WindowTitle(title="", subtitle="")
        self._sel_title.set_visible(False)

        self._sel_delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self._sel_delete_btn.set_tooltip_text(self._("Delete selected"))
        self._sel_delete_btn.add_css_class("destructive-action")
        self._sel_delete_btn.set_visible(False)
        self._sel_delete_btn.connect("clicked", lambda _: self._sel_delete_selected())
        self.header.pack_end(self._sel_delete_btn)

        self._sel_move_btn = Gtk.Button.new_from_icon_name("folder-move-symbolic")
        self._sel_move_btn.set_tooltip_text(self._("Move selected"))
        self._sel_move_btn.set_visible(False)
        self._sel_move_btn.connect("clicked", lambda _: self._sel_move_selected())
        self.header.pack_end(self._sel_move_btn)

        # Category nav bar (styled like a bottom switcher bar)
        self.nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.nav_box.set_hexpand(True)
        self.nav_box.add_css_class("view-switcher")
        self.toolbar.add_top_bar(self.nav_box)

        # Status label (hidden when empty)
        self.status = Gtk.Label(xalign=0)
        self.status.set_hexpand(True)
        self.status.set_vexpand(False)
        self.status.set_margin_start(16)
        self.status.set_margin_end(16)
        self.status.set_margin_top(6)
        self.status.set_margin_bottom(4)
        self.status.add_css_class("dim-label")
        self.status.set_visible(False)

        # Virtualized grid (GridView only renders visible tiles)
        self.gallery_grid = GalleryGrid(self)
        self.gallery_grid.scroller.add_tick_callback(self._on_grid_tick)
        self.gallery_grid.scroller.connect("edge-overshot", self._on_scroll_edge_overshot)
        scroll_refresh = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_refresh.connect("scroll", self._on_pull_refresh_scroll)
        self.gallery_grid.scroller.add_controller(scroll_refresh)
        folder_swipe = Gtk.GestureSwipe()
        folder_swipe.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        folder_swipe.connect("swipe", self._on_folder_swipe)
        self.gallery_grid.add_controller(folder_swipe)
        self._apply_grid_settings()
        self._grid_width = 0  # force CSS update after rebuild

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_hexpand(True)
        content.set_vexpand(True)
        content.append(self.status)
        content.append(self.gallery_grid)
        self.toolbar.set_content(content)
        self._rebuild_categories()

    # ------------------------------------------------------------------
    # Sort popover
    # ------------------------------------------------------------------

    def _sort_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        for mode, label, icon in [
            ("newest", "Newest first", "view-sort-descending-symbolic"),
            ("oldest", "Oldest first", "view-sort-ascending-symbolic"),
            ("name", "Name", "format-text-bold-symbolic"),
            ("folder", "Folder", "folder-symbolic"),
            ("date", "Date", "view-calendar-symbolic"),
        ]:
            button = Gtk.Button()
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Image.new_from_icon_name(icon))
            row.append(Gtk.Label(label=self._(label), xalign=0))
            button.set_child(row)
            button.connect("clicked", self._set_sort_mode, mode, popover)
            box.append(button)
        popover.set_child(box)
        return popover

    # ------------------------------------------------------------------
    # Category navigation
    # ------------------------------------------------------------------

    def _rebuild_categories(self) -> None:
        child = self.nav_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.nav_box.remove(child)
            child = next_child
        self.category_buttons.clear()

        _icons = {
            "photos": "camera-photo-symbolic",
            "pictures": "image-x-generic-symbolic",
            "videos": "video-display-symbolic",
            "screenshots": "applets-screenshooter-symbolic",
        }
        _nc_icon_dir = Path(__file__).parent / "data" / "icons"
        _dark = Adw.StyleManager.get_default().get_dark()
        self._nc_spinner = None
        self._nc_broken_img = None
        for category, label, path in self.settings.categories():
            if not path:
                continue
            if category == "nextcloud":
                img = self._make_nc_icon(_nc_icon_dir, _dark)
            else:
                img = Gtk.Image.new_from_icon_name(_icons.get(category, "folder-symbolic"))
                img.set_pixel_size(22)
            lbl = Gtk.Label(label=self._(label))
            lbl.add_css_class("caption")
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            vbox.set_halign(Gtk.Align.CENTER)
            vbox.append(img)
            vbox.append(lbl)
            button = Gtk.ToggleButton()
            if category == "nextcloud":
                spinner = Gtk.Spinner()
                spinner.set_size_request(14, 14)
                spinner.set_halign(Gtk.Align.END)
                spinner.set_valign(Gtk.Align.START)
                spinner.set_visible(False)
                broken_img = Gtk.Image.new_from_icon_name("network-error-symbolic")
                broken_img.set_pixel_size(14)
                broken_img.add_css_class("error")
                broken_img.set_halign(Gtk.Align.END)
                broken_img.set_valign(Gtk.Align.START)
                broken_img.set_visible(False)
                overlay = Gtk.Overlay()
                overlay.set_child(vbox)
                overlay.add_overlay(spinner)
                overlay.add_overlay(broken_img)
                button.set_child(overlay)
                self._nc_spinner = spinner
                self._nc_broken_img = broken_img
            else:
                button.set_child(vbox)
            button.add_css_class("flat")
            button.set_hexpand(True)
            button.set_tooltip_text(str(Path(path).expanduser()))
            button.set_active(category == self.category)
            button.connect("toggled", self._on_category_toggled, category)
            self.nav_box.append(button)
            self.category_buttons[category] = button

    def _make_nc_icon(self, icon_dir: Path, dark: bool) -> Gtk.Image:
        name = "nc-icon-dark.png" if dark else "nc-icon-light.png"
        png = icon_dir / name
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(png), 22, 22, True)
            img = Gtk.Image.new_from_pixbuf(pixbuf)
            img.set_pixel_size(22)
            return img
        except Exception:
            img = Gtk.Image.new_from_icon_name("folder-remote-symbolic")
            img.set_pixel_size(22)
            return img

    def _on_system_theme_changed(self, _mgr, _param) -> None:
        self._rebuild_categories()

    def _apply_grid_settings(self) -> None:
        columns = min(max(int(self.settings.grid_columns), 2), 10)
        self.gallery_grid.set_columns(columns)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def refresh(self, scan: bool = False, scope: str | None = None) -> None:
        """scope=None scans all local + NC; scope="current" scans only the active category."""
        if scan:
            self._render()
            self.refresh_button.set_sensitive(False)
            nc_folder = self.current_folder if self.category == "nextcloud" else None
            threading.Thread(
                target=self._scan_thread, args=(nc_folder, scope), daemon=True
            ).start()
            return
        self.refresh_button.set_sensitive(True)
        self._render()

    def _scan_thread(self, nc_folder: str | None, scope: str | None = None) -> None:
        only_current = scope == "current"
        # Touch NC for full scans, when NC is the active category, or when the
        # current Pictures view is configured to fold in Nextcloud entries —
        # but ONLY if the user has Nextcloud actively enabled. We never bring
        # the connection back up automatically; that requires explicit consent.
        need_nc = self.settings.nextcloud_enabled and (
            (not only_current)
            or self.category == "nextcloud"
            or self._should_merge_nc()
        )
        try:
            nc_client = None
            if need_nc and self.settings.nextcloud_url and self.settings.nextcloud_user:
                pwd = self.settings.load_app_password()
                if pwd:
                    from .nextcloud import NextcloudClient
                    nc_client = NextcloudClient(
                        self.settings.nextcloud_url,
                        self.settings.nextcloud_user,
                        pwd,
                    )
                    LOGGER.info("Nextcloud client created for %s", self.settings.nextcloud_url)
                else:
                    LOGGER.info("No Nextcloud app password available; skipping scan")
                    GLib.idle_add(self._set_nc_broken, True)

            # Phase 1: local categories
            if only_current:
                if self.category == "nextcloud":
                    local_cats: list = []
                else:
                    local_cats = [
                        (c, l, p)
                        for c, l, p in self.settings.categories()
                        if c == self.category
                    ]
            else:
                local_cats = [
                    (c, l, p)
                    for c, l, p in self.settings.categories()
                    if c != "nextcloud"
                ]
            if local_cats:
                self.scanner.scan(local_cats)

            # Phase 2: NC structure scan (no thumbnails)
            if nc_client is not None:
                GLib.idle_add(self._set_nc_syncing, True)
                GLib.idle_add(self._set_nc_broken, False)
                self.scanner.scan_nc_structure(nc_client, self.settings.nextcloud_photos_path)
                GLib.idle_add(self.refresh, False)  # show folder structure immediately
                # No bulk thumbnail pre-fetch: tiles request their own thumbnail when
                # they scroll into view, which keeps the UI responsive on large folders.
        except Exception as e:
            LOGGER.exception("Media scan failed: %s", e)
            GLib.idle_add(self._set_nc_broken, True)
        finally:
            nc_client = None
            GLib.idle_add(self._set_nc_syncing, False)
            GLib.idle_add(self.refresh, False)
            GLib.idle_add(lambda: self.refresh_button.set_sensitive(True))
            # Trim cache after every scan: thumbnail generation may have grown
            # the disk footprint past the user's configured budget.
            self.evict_cache_async()

    def _set_nc_syncing(self, active: bool) -> None:
        if self._nc_spinner is not None:
            self._nc_spinner.set_visible(active)
            if active:
                self._nc_spinner.start()
            else:
                self._nc_spinner.stop()

    def _set_nc_broken(self, active: bool) -> None:
        if self._nc_broken_img is not None:
            self._nc_broken_img.set_visible(active)

    def _update_item_thumb(self, path: str, thumb_path: str) -> None:
        updated = self.gallery_grid.update_item_thumb(path, thumb_path)
        # Folder-card thumb updates only matter while the user is browsing the
        # NC folder hierarchy. In Pictures (or other) views the NC items show as
        # flat tiles, so we skip the extra DB lookup.
        if self.category == "nextcloud":
            item = self.database.get_media_by_path(path, "nextcloud")
            if item is not None:
                folder_path = self._visible_child_folder_for_item(item.folder)
                if folder_path is not None:
                    updated = self.gallery_grid.update_folder_thumb(folder_path, thumb_path) or updated
        if updated:
            LOGGER.debug("Updated visible thumbnail for %s", path)

    def _enqueue_thumb_update(self, path: str, thumb_path: str) -> None:
        """Buffer thumbnail-arrival events from background workers and flush
        them on a single idle tick so we don't hammer the main loop with one
        idle_add per HTTP response."""
        with self._pending_thumb_lock:
            self._pending_thumb_updates[path] = thumb_path
            if self._pending_thumb_idle == 0:
                self._pending_thumb_idle = GLib.idle_add(
                    self._flush_thumb_updates,
                    priority=GLib.PRIORITY_DEFAULT_IDLE,
                )

    def _flush_thumb_updates(self) -> bool:
        with self._pending_thumb_lock:
            updates = self._pending_thumb_updates
            self._pending_thumb_updates = {}
            self._pending_thumb_idle = 0
        # Process at most a chunk per idle tick so the main loop can paint
        # between batches; remaining work re-arms itself.
        chunk_limit = 24
        items = list(updates.items())
        for path, thumb in items[:chunk_limit]:
            self._update_item_thumb(path, thumb)
        leftover = dict(items[chunk_limit:])
        if leftover:
            with self._pending_thumb_lock:
                # Merge any updates that arrived while we were processing.
                leftover.update(self._pending_thumb_updates)
                self._pending_thumb_updates = leftover
                if self._pending_thumb_idle == 0:
                    self._pending_thumb_idle = GLib.idle_add(
                        self._flush_thumb_updates,
                        priority=GLib.PRIORITY_DEFAULT_IDLE,
                    )
        return GLib.SOURCE_REMOVE

    def _render(self) -> None:
        # Preserve scroll position when refreshing the same view (e.g. after scan)
        render_key = (self.category, self.current_folder)
        vadj = self.gallery_grid.get_vadjustment()
        saved_pos = vadj.get_value() if render_key == self._last_render_key else 0.0
        self._last_render_key = render_key

        self.gallery_grid.clear()
        self.current_items = []
        self._current_offset = 0
        self._has_more_items = False
        self._date_last_key = None

        sort_mode = self.settings.get_sort_mode(self.category, self.current_folder)
        self.back_button.set_visible(False)
        if sort_mode == "folder":
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

    def _render_flat(self, sort_mode: str) -> None:
        include_nc = self._should_merge_nc()
        self._total_count = self.database.count_media(
            self.category, self.current_folder, include_nc=include_nc,
        )
        page = self.database.list_media_paginated(
            self.category, sort_mode, self.current_folder,
            self._page_size, 0, include_nc=include_nc,
        )
        self.current_items = list(page)
        self._current_offset = len(page)
        self._has_more_items = self._current_offset < self._total_count
        for item in page:
            self.gallery_grid.append_media(item, item.path in self._selected_paths)
        self._set_status("")
        self._set_empty_state(visible=not self.current_items)

    def _render_folders(self) -> None:
        sort_mode = self.settings.get_sort_mode(self.category, self.current_folder)
        folders = self.database.child_folders(self.category, self.current_folder)
        for folder, count, thumbs in folders:
            self.gallery_grid.append_folder(folder, count, thumbs)
        direct_folder = self.current_folder or "/"
        # NC items are merged in only at the root view of Pictures (NC has its
        # own folder layout that doesn't map onto local Pictures subfolders).
        include_nc = self._should_merge_nc() and self.current_folder in (None, "/")
        self._total_count = self.database.count_media(
            self.category, direct_folder, include_nc=include_nc,
        )
        page = self.database.list_media_paginated(
            self.category, sort_mode, direct_folder,
            self._page_size, 0, include_nc=include_nc,
        )
        self.current_items = list(page)
        self._current_offset = len(page)
        self._has_more_items = self._current_offset < self._total_count
        for item in page:
            self.gallery_grid.append_media(item, item.path in self._selected_paths)
        total = len(folders) + len(self.current_items)
        self._set_empty_state(visible=total == 0)
        self._set_status("")

    def _render_date_groups(self, ascending: bool = False) -> None:
        order = "oldest" if ascending else "newest"
        include_nc = self._should_merge_nc()
        self._total_count = self.database.count_media(
            self.category, self.current_folder, include_nc=include_nc,
        )
        page = self.database.list_media_paginated(
            self.category, order, self.current_folder,
            self._page_size, 0, include_nc=include_nc,
        )
        self.current_items = list(page)
        self._current_offset = len(page)
        self._has_more_items = self._current_offset < self._total_count
        self._date_last_key = None
        for item in page:
            self._append_date_grouped(item)
        self._set_status("")
        self._set_empty_state(visible=not self.current_items)

    def _append_date_grouped(self, item: MediaItem) -> None:
        dt = datetime.fromtimestamp(item.mtime)
        key = (dt.year, dt.month)
        if key != self._date_last_key:
            self.gallery_grid.append_header(self._month_header_markup(dt))
            self._date_last_key = key
        self.gallery_grid.append_media(item, item.path in self._selected_paths)

    def _month_header_markup(self, dt: datetime) -> str:
        # Two-line month/year header (locale-aware month name); the year is sized
        # relative to the surrounding label so it scales with the .date-header CSS.
        month = GLib.markup_escape_text(dt.strftime("%B"))
        year = GLib.markup_escape_text(dt.strftime("%Y"))
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
        if not remainder or "/" not in remainder and item_folder == parent:
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
        if not self._has_more_items or self._lazy_loading_in_flight:
            return GLib.SOURCE_REMOVE
        vadj = self.gallery_grid.get_vadjustment()
        if vadj.get_upper() <= vadj.get_page_size() + 1:
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
            if sort_mode == "folder":
                # Folder mode merges NC only at the root.
                include_nc = include_nc and self.current_folder in (None, "/")
                folder_arg = self.current_folder or "/"
            else:
                folder_arg = self.current_folder
            next_items = self.database.list_media_paginated(
                self.category, query_sort, folder_arg,
                self._page_size, self._current_offset, include_nc=include_nc,
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
                    self.gallery_grid.append_media(item, item.path in self._selected_paths)
            self.gallery_grid.finish()

            self._current_offset += len(next_items)
            self._has_more_items = self._current_offset < self._total_count
            LOGGER.debug(
                "Lazy-loaded %d more items (total visible: %d / %d)",
                len(next_items), len(self.current_items), self._total_count,
            )
        finally:
            self._lazy_loading_in_flight = False
        # If the fresh chunk still didn't fill the viewport (large screens with
        # tiny page size), keep going on the next idle.
        if self._has_more_items:
            GLib.idle_add(self._maybe_fill_viewport, priority=GLib.PRIORITY_LOW)

    # ------------------------------------------------------------------
    # Item actions
    # ------------------------------------------------------------------

    def _category_root(self, category: str) -> str | None:
        """Filesystem path or NC photos_path that backs *category*, or None."""
        for cat, _label, path in self.settings.categories():
            if cat == category:
                return path
        return None

    def _prompt_new_folder(self) -> None:
        """Adwaita-styled dialog asking for a folder name; creates it on confirm."""
        if self.scanner.missing_root.get(self.category) is not None:
            self._show_error_dialog(
                self._("Cannot create folder"),
                self._("The current location is not available."),
            )
            return

        entry = Adw.EntryRow()
        entry.set_title(self._("Folder name"))
        entry.set_show_apply_button(False)
        # Wrap in a list-style group so it gets Adwaita rounded corners
        group = Adw.PreferencesGroup()
        group.add(entry)

        dialog = Adw.AlertDialog(
            heading=self._("New folder"),
            body=self._("Create a new folder in %s") % (self.current_folder or "/"),
        )
        dialog.set_extra_child(group)
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("create", self._("Create"))
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)

        # Enter on the entry confirms
        entry.connect("entry-activated", lambda _e: dialog.response("create"))

        def _done(_dialog, response):
            if response != "create":
                return
            name = entry.get_text().strip()
            if not name:
                return
            self._create_folder_in_current(name)

        dialog.connect("response", _done)
        dialog.present(self)
        # Focus the entry so the user can start typing immediately
        GLib.idle_add(lambda: (entry.grab_focus(), GLib.SOURCE_REMOVE)[1])

    def _create_folder_in_current(self, name: str) -> None:
        # Disallow path separators in folder name
        if "/" in name or "\\" in name:
            self._show_error_dialog(
                self._("Invalid folder name"),
                self._("Folder names cannot contain slashes."),
            )
            return

        if self.category == "nextcloud":
            self._create_nc_folder(name)
        else:
            self._create_local_folder(name)

    def _create_local_folder(self, name: str) -> None:
        root = self._category_root(self.category)
        if not root:
            return
        parent = Path(root).expanduser()
        if self.current_folder:
            parent = parent / self.current_folder
        new_dir = parent / name
        try:
            new_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            self._show_error_dialog(
                self._("Folder exists"),
                self._("A folder named %s already exists here.") % name,
            )
            return
        except OSError as exc:
            self._show_error_dialog(
                self._("Could not create folder"), str(exc),
            )
            return
        self.refresh(scan=True, scope="current")

    def _create_nc_folder(self, name: str) -> None:
        from .nextcloud import NextcloudClient
        pwd = self.settings.load_app_password()
        if not pwd:
            self._show_error_dialog(
                self._("Not connected"),
                self._("Nextcloud password is unavailable."),
            )
            return
        photos_path = self.settings.nextcloud_photos_path or "Photos"
        rel_parts = [photos_path.strip("/")]
        if self.current_folder:
            rel_parts.append(self.current_folder.strip("/"))
        rel_parts.append(name)
        rel = "/".join(p for p in rel_parts if p)

        def _worker():
            try:
                client = NextcloudClient(
                    self.settings.nextcloud_url, self.settings.nextcloud_user, pwd,
                )
                dav = f"{client.dav_root}/{rel}"
                ok = client.mkcol(dav)
            except Exception as exc:
                LOGGER.exception("NC folder creation failed: %s", exc)
                ok = False
            GLib.idle_add(self._on_nc_folder_created, name, ok)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_nc_folder_created(self, name: str, ok: bool) -> bool:
        if not ok:
            self._show_error_dialog(
                self._("Could not create folder"),
                self._("The Nextcloud server rejected the new folder %s.") % name,
            )
        else:
            self.refresh(scan=True, scope="current")
        return GLib.SOURCE_REMOVE

    # ── Disk cache management ────────────────────────────────────────
    def cache_size_bytes(self) -> int:
        """Total bytes used by the on-disk cache (thumbnails + NC files)."""
        from .nextcloud import _NC_CACHE
        total = 0
        for root in (THUMB_DIR, _NC_CACHE):
            if not root.exists():
                continue
            for f in root.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    pass
        return total

    def evict_cache(self) -> int:
        """Trim the disk cache to the configured maximum.
        Returns the number of bytes freed. Uses LRU eviction (oldest atime
        gets deleted first). cache_max_mb <= 0 means unlimited (no-op)."""
        max_mb = getattr(self.settings, "cache_max_mb", 0) or 0
        if max_mb <= 0:
            return 0
        max_bytes = int(max_mb) * 1024 * 1024
        from .nextcloud import _NC_CACHE
        files: list[tuple[float, int, "Path"]] = []
        total = 0
        for root in (THUMB_DIR, _NC_CACHE):
            if not root.exists():
                continue
            for f in root.rglob("*"):
                try:
                    if not f.is_file():
                        continue
                    stat = f.stat()
                    files.append((stat.st_atime, stat.st_size, f))
                    total += stat.st_size
                except OSError:
                    pass
        if total <= max_bytes:
            return 0
        # Oldest atime first — least-recently used.
        files.sort(key=lambda row: row[0])
        freed = 0
        for _atime, size, path in files:
            if total <= max_bytes:
                break
            try:
                path.unlink()
                total -= size
                freed += size
            except OSError:
                pass
        if freed:
            LOGGER.info("Evicted %.1f MB from disk cache", freed / 1024 / 1024)
        return freed

    def evict_cache_async(self) -> None:
        """Run eviction in a daemon thread so the main loop never blocks on it."""
        if getattr(self.settings, "cache_max_mb", 0) <= 0:
            return
        threading.Thread(target=self.evict_cache, daemon=True).start()

    def clear_cache(self) -> None:
        """Wipe the entire on-disk cache (thumbnails + downloaded NC files).
        Runs synchronously; callers that need to keep the UI responsive should
        wrap this in a thread."""
        from .nextcloud import _NC_CACHE
        try:
            self.thumbnailer.clear()
        except Exception:
            LOGGER.exception("Failed to clear thumbnail cache")
        if _NC_CACHE.exists():
            try:
                shutil.rmtree(_NC_CACHE, ignore_errors=True)
            except Exception:
                LOGGER.exception("Failed to clear Nextcloud file cache")

    # ── On-demand Nextcloud thumbnail loader ──────────────────────────
    def request_nc_thumbnail(self, item_path: str) -> None:
        """Queue a NC thumbnail fetch for *item_path*. Thread-safe; idempotent
        per path. Spawns workers up to the configured pool size on demand.

        Bails out silently when the user has Nextcloud disabled — we never
        re-establish the connection on our own; that requires explicit consent
        (Settings toggle or the viewer's "Einmalig/Dauerhaft" prompt)."""
        if not self.settings.nextcloud_enabled:
            return
        with self._nc_thumb_lock:
            if item_path in self._nc_thumb_pending:
                return
            self._nc_thumb_pending.add(item_path)
            self._nc_thumb_queue.append(item_path)
            workers_to_start = self._nc_thumb_worker_target - self._nc_thumb_active_workers
            # Cap by queue depth so we don't spin up 4 threads for a single item.
            workers_to_start = min(workers_to_start, len(self._nc_thumb_queue))
            self._nc_thumb_active_workers += max(0, workers_to_start)
        for _ in range(max(0, workers_to_start)):
            threading.Thread(target=self._nc_thumb_worker, daemon=True).start()
        self._nc_thumb_event.set()

    def _ensure_nc_thumb_client(self):
        """Lazily build a single NextcloudClient that all worker threads share.
        The client uses thread-local persistent HTTPS connections, so each
        worker effectively gets its own keep-alive socket."""
        if self._nc_thumb_shared_client is not None:
            return self._nc_thumb_shared_client
        pwd = self.settings.load_app_password()
        if not pwd:
            return None
        try:
            from .nextcloud import NextcloudClient
            self._nc_thumb_shared_client = NextcloudClient(
                self.settings.nextcloud_url, self.settings.nextcloud_user, pwd,
            )
        except Exception as exc:
            LOGGER.exception("NC thumb worker init failed: %s", exc)
            return None
        return self._nc_thumb_shared_client

    def _nc_thumb_worker(self) -> None:
        from .nextcloud import dav_path_from_nc
        client = self._ensure_nc_thumb_client()
        if client is None:
            with self._nc_thumb_lock:
                self._nc_thumb_active_workers -= 1
                self._nc_thumb_pending.clear()
                self._nc_thumb_queue.clear()
            return
        try:
            while True:
                with self._nc_thumb_lock:
                    if self._nc_thumb_queue:
                        # LIFO: most recently bound tile = most likely on screen.
                        path = self._nc_thumb_queue.pop()
                    else:
                        path = None
                        self._nc_thumb_event.clear()
                if path is None:
                    # Wait briefly for new work; exit if queue stays empty so we
                    # don't keep idle threads alive forever.
                    if not self._nc_thumb_event.wait(timeout=15.0):
                        with self._nc_thumb_lock:
                            if not self._nc_thumb_queue:
                                self._nc_thumb_active_workers -= 1
                                return
                    continue
                thumb = None
                try:
                    dav = dav_path_from_nc(path)
                    thumb = client.ensure_thumbnail(dav)
                except Exception:
                    LOGGER.debug("NC thumb fetch failed for %s", path, exc_info=True)
                finally:
                    with self._nc_thumb_lock:
                        self._nc_thumb_pending.discard(path)
                if thumb:
                    try:
                        self.database.set_thumb(path, thumb, "nextcloud")
                        self.database.commit()
                    except Exception:
                        LOGGER.debug("NC thumb DB write failed for %s", path, exc_info=True)
                    self._enqueue_thumb_update(path, thumb)
        except Exception:
            LOGGER.exception("NC thumb worker crashed")
            with self._nc_thumb_lock:
                self._nc_thumb_active_workers = max(0, self._nc_thumb_active_workers - 1)

    def _open_folder(self, _button, folder: str) -> None:
        self.current_folder = folder
        self._render()
        # No bulk thumbnail pre-fetch on folder open: the gallery requests each
        # tile's NC thumbnail on demand as it scrolls into view.

    def _open_item(self, _button, item: MediaItem) -> None:
        if item.is_video and self.settings.external_video_player.strip():
            subprocess.Popen(shlex.split(self.settings.external_video_player) + [item.path])
            return
        items = self.current_items or self.database.list_media(
            item.category, self.settings.get_sort_mode(item.category, self.current_folder), self.current_folder
        )
        # Match by path — frozen MediaItem __eq__ compares all fields, and thumb_path
        # may differ between the cached current_items and the clicked tile (async thumb update).
        index = next((i for i, it in enumerate(items) if it.path == item.path), -1)
        if index < 0:
            items = [item]
            index = 0
        ViewerWindow(self, items, index, self.settings.external_video_player).present()

    def _show_context_menu(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        item: MediaItem,
        parent: Gtk.Widget,
    ) -> None:
        popover = Gtk.Popover()
        popover.set_parent(parent)
        popover.set_pointing_to(Gdk.Rectangle(int(x), int(y), 1, 1))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for label, icon, callback in [
            ("Delete", "user-trash-symbolic", self._delete_item),
            ("Move", "folder-move-symbolic", self._move_item),
            ("Share", "mail-send-symbolic", self._share_item),
            ("Open externally", "document-open-symbolic", self._open_externally),
        ]:
            button = Gtk.Button()
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Image.new_from_icon_name(icon))
            row.append(Gtk.Label(label=self._(label), xalign=0))
            button.set_child(row)
            button.connect("clicked", lambda _b, cb=callback, it=item, p=popover: (p.popdown(), cb(it)))
            box.append(button)
        popover.set_child(box)
        popover.popup()

    # ------------------------------------------------------------------
    # Multi-select
    # ------------------------------------------------------------------

    def _enter_selection_mode(self) -> None:
        self._selection_mode = True
        self.back_button.set_visible(False)
        self.new_folder_button.set_visible(False)
        self.refresh_button.set_visible(False)
        self.settings_button.set_visible(False)
        self.sort_button.set_visible(False)
        self._sel_cancel_btn.set_visible(True)
        self._sel_delete_btn.set_visible(True)
        self._sel_move_btn.set_visible(True)
        self.header.set_title_widget(self._sel_title)
        self._update_sel_title()

    def _exit_selection_mode(self) -> None:
        self._selection_mode = False
        self._selected_paths.clear()
        self._sel_cancel_btn.set_visible(False)
        self._sel_delete_btn.set_visible(False)
        self._sel_move_btn.set_visible(False)
        # Restore normal header
        title = Adw.WindowTitle(title=APP_NAME, subtitle="")
        self.header.set_title_widget(title)
        self.back_button.set_visible(False)
        self.new_folder_button.set_visible(True)
        self.refresh_button.set_visible(True)
        self.settings_button.set_visible(True)
        self.sort_button.set_visible(True)
        self._render()

    def _toggle_selection(self, path: str) -> None:
        if path in self._selected_paths:
            self._selected_paths.discard(path)
        else:
            self._selected_paths.add(path)
        if not self._selected_paths:
            self._exit_selection_mode()
            return
        self._update_sel_title()
        self._render()

    def _update_sel_title(self) -> None:
        n = len(self._selected_paths)
        self._sel_title.set_title(f"{n} {self._('selected')}")
        self._sel_title.set_subtitle("")

    def _sel_delete_selected(self) -> None:
        paths = list(self._selected_paths)
        n = len(paths)
        if n == 0:
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=self._("Delete selection?"),
            body=self._("Photos will be moved to trash."),
        )
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("delete", self._("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_sel_delete_confirmed, paths)
        dialog.present()

    def _on_sel_delete_confirmed(self, _dialog, response: str, paths: list[str]) -> None:
        if response != "delete":
            return
        errors: list[tuple[str, Exception]] = []
        for path in paths:
            try:
                Gio.File.new_for_path(path).trash(None)
                self.database.delete_path(path, self.category)
            except GLib.Error as e:
                errors.append((path, e))
            except Exception as e:
                errors.append((path, e))
        self._exit_selection_mode()
        if not errors:
            self._set_status(self._(f"Deleted {len(paths)} items"))
        elif len(errors) == len(paths):
            self._show_error_dialog(
                self._("Delete failed"),
                self._("Could not delete all files. Check file permissions or disk state."),
                f"{len(errors)}/{len(paths)} items"
            )
        else:
            self._set_status(self._(f"Deleted {len(paths) - len(errors)}/{len(paths)} items ({len(errors)} failed)"))

    def _sel_move_selected(self) -> None:
        if not self._selected_paths:
            return
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._on_sel_move_response)
        chooser.show()

    def _on_sel_move_response(self, chooser: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            folder = chooser.get_file().get_path()
            errors: list[tuple[str, Exception]] = []
            for path in list(self._selected_paths):
                try:
                    target = Path(folder) / Path(path).name
                    Path(path).rename(target)
                    self.database.delete_path(path, self.category)
                except (OSError, PermissionError) as e:
                    errors.append((path, e))
            self._exit_selection_mode()
            self.refresh(scan=True)
            if not errors:
                self._set_status(self._(f"Moved {len(self._selected_paths)} items"))
            elif len(errors) == len(self._selected_paths):
                self._show_error_dialog(
                    self._("Move failed"),
                    self._("Could not move files. Check file permissions and disk space."),
                    f"{len(errors)} file(s) failed"
                )
            else:
                self._set_status(self._(f"Moved {len(self._selected_paths) - len(errors)} items ({len(errors)} failed)"))
        chooser.destroy()

    def _del_item(self, item: MediaItem) -> None:
        try:
            Gio.File.new_for_path(item.path).trash(None)
            if item.thumb_path:
                try:
                    Path(item.thumb_path).unlink(missing_ok=True)
                except OSError:
                    pass
            self.database.delete_path(item.path, item.category)
            self._set_status(self._("Deleted"))
            self._render()
        except GLib.Error as e:
            if "Permission" in str(e):
                self._show_error_dialog(
                    self._("Cannot delete"),
                    self._("Permission denied. The file or folder is protected."),
                    "",
                )
            else:
                self._show_error_dialog(
                    self._("Delete failed"),
                    self._("Could not move the file to trash."),
                    str(e),
                )
        except Exception as e:
            self._handle_file_error(e, item.path)

    def _move_item(self, item: MediaItem) -> None:
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._move_item_response, item)
        chooser.show()

    def _move_item_response(self, chooser: Gtk.FileChooserNative, response: int, item: MediaItem) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            folder = chooser.get_file().get_path()
            target = Path(folder) / item.name
            try:
                Path(item.path).rename(target)
                self.database.delete_path(item.path, item.category)
                self.refresh(scan=True)
                self._set_status(self._("Moved"))
            except OSError:
                self._set_status(self._("Could not complete action"))
        chooser.destroy()

    def _share_item(self, item: MediaItem) -> None:
        if shutil.which("xdg-email"):
            subprocess.Popen(["xdg-email", "--attach", item.path])
            return
        self._set_status(self._("Could not complete action"))

    def _open_externally(self, item: MediaItem) -> None:
        subprocess.Popen(["xdg-open", item.path])

    # ------------------------------------------------------------------
    # Navigation handlers
    # ------------------------------------------------------------------

    def _on_category_toggled(self, button: Gtk.ToggleButton, category: str) -> None:
        if not button.get_active():
            if category == self.category and self.current_folder is not None:
                button.handler_block_by_func(self._on_category_toggled)
                button.set_active(True)
                button.handler_unblock_by_func(self._on_category_toggled)
                self.current_folder = None
                self._render()
            return
        self.category = category
        self.current_folder = None
        for other_category, other_button in self.category_buttons.items():
            if other_category != category:
                other_button.set_active(False)
        self.settings.last_category = category
        self.settings.save()
        self._render()

    def _on_back(self, _button: Gtk.Button) -> None:
        self._go_back_folder()

    def _go_back_folder(self) -> None:
        if not self.current_folder or "/" not in self.current_folder:
            self.current_folder = None
        else:
            self.current_folder = self.current_folder.rsplit("/", 1)[0]
        self._render()

    def _on_folder_swipe(self, _gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        if self._selection_mode or self.current_folder is None:
            return
        if abs(velocity_x) < 350 or abs(velocity_x) <= abs(velocity_y):
            return
        if velocity_x > 0:
            self._go_back_folder()

    def _set_sort_mode(self, _button: Gtk.Button, mode: str, popover: Gtk.Popover) -> None:
        sort_key = f"{self.category}\x00{self.current_folder}" if self.current_folder is not None else self.category
        if mode == "date":
            # Repeated click toggles ascending/descending.
            current = self.settings.sort_modes.get(sort_key)
            mode = "date_asc" if current == "date" else "date"
        self.settings.sort_modes[sort_key] = mode
        self.settings.save()
        popover.popdown()
        self._render()

    def _open_settings(self, _button: Gtk.Button) -> None:
        SettingsWindow(self).present()

    def _show_privacy_info(self, _button: Gtk.Button) -> None:
        """Show privacy and help information dialog."""
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=self._("Privacy & Help"),
        )
        
        # Build content with privacy information
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(12)
        content_box.set_margin_bottom(12)
        content_box.set_margin_start(12)
        content_box.set_margin_end(12)
        
        # Section 1: EXIF Data
        section1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title1 = Gtk.Label(label=self._("EXIF Data"))
        title1.add_css_class("title-2")
        title1.set_halign(Gtk.Align.START)
        section1.append(title1)
        
        text1 = Gtk.Label(
            label=self._(
                "Photos often contain sensitive metadata (EXIF data):\n"
                "• Camera make & model\n"
                "• GPS coordinates and location history\n"
                "• Timestamp and date taken\n\n"
                "This app displays EXIF data in the Image Info panel. "
                "Be careful when sharing photos online, as metadata "
                "can reveal your location and privacy details."
            ),
            wrap=True,
            justify=Gtk.Justification.LEFT,
        )
        text1.add_css_class("body")
        section1.append(text1)
        content_box.append(section1)
        
        # Section 2: Photo Deletion
        section2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title2 = Gtk.Label(label=self._("Deleting Photos"))
        title2.add_css_class("title-2")
        title2.set_halign(Gtk.Align.START)
        section2.append(title2)
        
        text2 = Gtk.Label(
            label=self._(
                "When you delete photos in Yaga, they are moved to trash. "
                "They can typically be recovered from your system trash until "
                "it is permanently emptied. For secure deletion, consider using "
                "specialized tools or encrypted storage."
            ),
            wrap=True,
            justify=Gtk.Justification.LEFT,
        )
        text2.add_css_class("body")
        section2.append(text2)
        content_box.append(section2)
        
        # Section 3: Nextcloud
        section3 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title3 = Gtk.Label(label=self._("Nextcloud Integration"))
        title3.add_css_class("title-2")
        title3.set_halign(Gtk.Align.START)
        section3.append(title3)
        
        text3 = Gtk.Label(
            label=self._(
                "Nextcloud passwords are stored in your system keyring "
                "(or local file with restricted permissions). "
                "Ensure your Nextcloud instance uses HTTPS to protect data in transit."
            ),
            wrap=True,
            justify=Gtk.Justification.LEFT,
        )
        text3.add_css_class("body")
        section3.append(text3)
        content_box.append(section3)
        
        # Scrollable container
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(content_box)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_max_content_height(400)
        
        dialog.set_extra_child(scrolled)
        dialog.add_response("close", self._("OK"))
        dialog.set_default_response("close")
        dialog.present()

    def apply_settings(self, settings: Settings) -> None:
        self._selection_mode = False
        self._selected_paths.clear()
        self.settings = settings
        self.settings.save()
        self.translator.language = settings.language
        # Invalidate the shared NC client — credentials/URL may have changed.
        old_client = self._nc_thumb_shared_client
        self._nc_thumb_shared_client = None
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass
        self._apply_theme()
        self._build_ui()
        self.refresh(scan=True)

    # ------------------------------------------------------------------
    # CSS / theme
    # ------------------------------------------------------------------

    def _on_grid_tick(self, widget: Gtk.Widget, _clock) -> bool:
        width = widget.get_width()
        if width != self._grid_width:
            self._grid_width = width
            self._update_tile_size(width)
        return GLib.SOURCE_CONTINUE

    def _on_scroll_edge_overshot(self, _scroller, pos: Gtk.PositionType) -> None:
        if pos == Gtk.PositionType.TOP:
            self._trigger_pull_refresh()

    def _on_pull_refresh_scroll(self, _controller: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        adjustment = self.gallery_grid.get_vadjustment()
        if dy < -0.8 and adjustment.get_value() <= adjustment.get_lower() + 1:
            self._trigger_pull_refresh()
            return True
        return False

    def _trigger_pull_refresh(self) -> None:
        if not self.refresh_button.get_sensitive():
            return
        LOGGER.info("Pull refresh triggered for category %s", self.category)
        self.gallery_grid.pull_revealer.set_reveal_child(True)
        self.refresh(scan=True, scope="current")
        GLib.timeout_add(1200, lambda: self.gallery_grid.pull_revealer.set_reveal_child(False) or False)

    def _update_tile_size(self, scroller_width: int) -> None:
        if scroller_width <= 0:
            return
        columns = min(max(int(self.settings.grid_columns), 2), 10)
        # Subtract 2px margin per tile to prevent layout feedback loop
        cell_size = max(32, scroller_width // columns)
        # Only set height: the homogeneous Box distributes width automatically,
        # so min/max-width here would create a measurement feedback loop.
        self._tile_css.load_from_data(
            f""".gallery-tile {{
                min-height: {cell_size}px;
                max-height: {cell_size}px;
            }}""".encode()
        )

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        # Pre-baked rotation classes in 5° increments (0..355°). Toggling a class
        # is much cheaper than rewriting a CssProvider during a live gesture.
        rotation_css = "\n".join(
            f".rot-{i*5} {{ transform: rotate({i*5}deg); }}" for i in range(72)
        ).encode()
        provider.load_from_data(
            b"""
            .gallery-tile {
                padding: 0;
                margin: 1px;
                border-radius: 0;
                min-width: 0;
                min-height: 0;
            }
            .gallery-tile > * {
                margin: 0;
            }
            .gallery-tile.empty,
            .gallery-tile.empty:hover,
            .gallery-tile.empty:active {
                background: transparent;
                box-shadow: none;
            }
            listview.gallery-grid > row {
                padding: 0;
            }
            listview.gallery-grid > row:hover,
            listview.gallery-grid > row:selected {
                background: transparent;
            }
            gridview.gallery-grid > child {
                padding: 1px;
            }
            .date-header {
                min-height: 120px;
                padding: 16px 8px;
                background: #202020;
                color: white;
                font-size: 32px;
            }
            .folder-label {
                background: rgba(0,0,0,0.55);
                color: white;
                padding: 4px 8px;
                font-weight: 600;
            }
            .view-switcher {
                border-top: 1px solid @borders;
                padding-top: 4px;
            }
            .sel-check {
                background: alpha(@window_bg_color, 0.75);
                border-radius: 999px;
                padding: 2px;
                margin: 5px;
                -gtk-icon-size: 18px;
            }
            .sel-check.checked {
                background: @accent_bg_color;
                color: @accent_fg_color;
            }
            .viewer-date {
                /* Floating pill at the top of the viewer; readable over any image */
                padding: 12px 24px 14px 24px;
                margin-top: 12px;
                background: rgba(0,0,0,0.55);
                color: white;
                border-radius: 18px;
            }
            .viewer-date-day {
                font-size: 32px;
                font-weight: 500;
                opacity: 0.95;
            }
            .viewer-date-year {
                font-size: 22px;
                opacity: 0.65;
                margin-top: -4px;
            }
            """
            + rotation_css
        )
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        Gtk.StyleContext.add_provider_for_display(
            display, self._tile_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _apply_theme(self) -> None:
        style = Adw.StyleManager.get_default()
        style.set_color_scheme(
            {
                "system": Adw.ColorScheme.DEFAULT,
                "light": Adw.ColorScheme.FORCE_LIGHT,
                "dark": Adw.ColorScheme.FORCE_DARK,
            }.get(self.settings.theme, Adw.ColorScheme.DEFAULT)
        )

    def _first_existing_category(self) -> str:
        known = {cat for cat, _label, _path in self.settings.categories()}
        if self.settings.last_category in known:
            return self.settings.last_category
        return self.settings.categories()[0][0]


# ---------------------------------------------------------------------------
# In-app image editor
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------



def main() -> int:
    # Strip our own debug flags before GTK sees argv.
    trace_enabled = False
    trace_path: Path | None = None
    argv = list(sys.argv)
    if "--trace" in argv:
        trace_enabled = True
        argv.remove("--trace")
    while "--trace-file" in argv:
        i = argv.index("--trace-file")
        if i + 1 < len(argv):
            trace_path = Path(argv[i + 1]).expanduser()
            del argv[i : i + 2]
        else:
            del argv[i]
    sys.argv = argv

    if trace_enabled:
        from .tracer import install as install_tracer
        install_tracer(trace_path)

    app = GalleryApplication()
    return app.run()
