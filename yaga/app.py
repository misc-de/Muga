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

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk

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
from .camera import CameraWindow, camera_supported

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
    # Restrict to user-only — log lines may carry filenames, DAV paths and
    # server URL that other local accounts on a multi-user host shouldn't
    # see. Default umask would leave this 0644.
    try:
        DEBUG_LOG_PATH.chmod(0o600)
    except OSError:
        pass


def _enable_thread_dump_signal() -> None:
    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
        except RuntimeError:
            LOGGER.debug("Could not register SIGUSR1 thread dump handler", exc_info=True)


def _cleanup_abandoned_temp_files() -> None:
    """Clean up leftover _edit_*.* files from interrupted Nextcloud uploads.

    Scoped to the NC cache directory only. Earlier versions rglob'd
    ~/Pictures, ~/Photos and ~/Downloads, which would silently delete the
    user's own permanent edit-saves (the in-app editor's "Save" path
    writes <stem>_edit_<i>.<ext> next to the original on local items —
    those are intentional user files, not temp artifacts). The genuine
    temp-file shape only exists for NC uploads under CACHE_DIR/nextcloud,
    where evict_cache also eventually reaps them by size budget."""
    try:
        from .config import CACHE_DIR
        nc_cache = CACHE_DIR / "nextcloud"
        if not nc_cache.exists():
            return
        for temp_file in nc_cache.glob("*_edit_*.*"):
            try:
                temp_file.unlink(missing_ok=True)
                LOGGER.debug("Cleaned up NC upload temp file: %s", temp_file)
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
        # While a bulk delete/move worker is in flight: re-entry guard so
        # double-clicks on the toolbar buttons don't kick off a second pass.
        self._sel_busy: bool = False
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
        # Runtime gate: True only when the user has *actively* allowed NC for
        # this session. Scripts must NEVER flip this to True; only explicit UI
        # actions (Settings toggle/Connect button, viewer Einmalig/Dauerhaft)
        # may. Initialized from BOTH persistent flags so a saved Disconnect
        # survives app restarts.
        self._nc_session_active = bool(
            self.settings.nextcloud_enabled
            and getattr(self.settings, "nextcloud_session_active", True)
        )
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

        # Tracked reference to a currently-open settings dialog. Adw.Preferences-
        # Window is transient_for the parent but not auto-registered with the
        # Adw.Application, so app.get_windows() can't find it for cleanup. We
        # need an explicit reference so _recreate_window_for_layout_change can
        # destroy it before destroying the parent — without it, parent-destroy
        # doesn't reliably cascade to the dialog and the old modal lingers,
        # producing two visible settings dialogs after a recreate.
        self._settings_dialog: SettingsWindow | None = None

        # Dynamic tile-size CSS (updated via tick callback whenever the scroller resizes)
        self._tile_css = Gtk.CssProvider()
        self._grid_width = 0

        self._apply_theme()
        self._load_css()
        self._build_ui()
        self._theme_handler_id = Adw.StyleManager.get_default().connect(
            "notify::dark", self._on_system_theme_changed,
        )
        self.refresh(scan=True)
        # Note: a previous iteration auto-reopened the settings dialog on the
        # appearance page after a nav-position-driven window recreate, but
        # however we sequenced the destroys/timeouts the just-torn-down old
        # modal dialog left a stale grab in GTK's tracker. The reopened
        # dialog rendered and reacted visually but every action handler was
        # silent. Without auto-reopen the recreation works reliably; the
        # user reopens settings via the header gear button if they want to
        # make further changes.

    def _(self, text: str) -> str:
        return self.translator.gettext(text)

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)
        self.status.set_visible(bool(text))

    def is_nc_visible(self) -> bool:
        """May the gallery show Nextcloud entries (tab, merged tiles, cached
        thumbnails)? Driven by the persistent "Nextcloud active" preference, so
        a manual Disconnect keeps everything visible from the local cache."""
        return (
            bool(self.settings.nextcloud_enabled)
            and bool(self.settings.nextcloud_url)
            and bool(self.settings.nextcloud_user)
        )

    def is_nc_active(self) -> bool:
        """May this code make a *network* call to Nextcloud? Combines the
        runtime session flag with credentials. Scripts must use this — not
        nextcloud_enabled — to honor the user's manual disconnect."""
        return (
            self._nc_session_active
            and bool(self.settings.nextcloud_url)
            and bool(self.settings.nextcloud_user)
        )

    def _should_merge_nc(self) -> bool:
        """True when NC items should be folded into the current Pictures view.
        Cached NC items remain visible even on a manual disconnect; a fresh
        thumbnail will only be fetched once the user reconnects."""
        return (
            self.category == "pictures"
            and self.is_nc_visible()
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

        # Pack order on the start (left) edge: refresh first so the icon
        # the user reaches for ("aktualisieren") sits at the top-left
        # corner of the titlebar; back follows immediately after so the
        # navigation pair stays grouped. The back arrow is only revealed
        # once `current_folder` is set (see _render()).
        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_button.set_tooltip_text(self._("Refresh"))
        self.refresh_button.connect("clicked", lambda _b: self.refresh(scan=True, scope="current"))
        self.header.pack_start(self.refresh_button)

        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.back_button.set_tooltip_text(self._("Back"))
        self.back_button.connect("clicked", self._on_back)
        self.back_button.set_visible(False)
        self.header.pack_start(self.back_button)

        self.search_button = Gtk.ToggleButton()
        self.search_button.set_icon_name("system-search-symbolic")
        self.search_button.set_tooltip_text(self._("Search"))
        self.header.pack_start(self.search_button)

        self.new_folder_button = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self.new_folder_button.set_tooltip_text(self._("New folder"))
        self.new_folder_button.connect("clicked", lambda _b: self._prompt_new_folder())
        self.header.pack_start(self.new_folder_button)

        title = Adw.WindowTitle(title=APP_NAME, subtitle="")
        self.header.set_title_widget(title)

        self.settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        self.settings_button.set_tooltip_text(self._("Settings"))
        self.settings_button.connect("clicked", self._open_settings)
        self.header.pack_start(self.settings_button)

        self.sort_button = Gtk.MenuButton(icon_name="view-sort-descending-symbolic")
        self.sort_button.set_tooltip_text(self._("Sort"))
        self._sort_popover = Gtk.Popover()
        self._sort_popover.set_autohide(True)
        self._sort_popover.set_child(self._build_sort_controls())
        self.sort_button.set_popover(self._sort_popover)
        self.header.pack_end(self.sort_button)

        self.camera_button = Gtk.Button.new_from_icon_name("camera-photo-symbolic")
        self.camera_button.set_tooltip_text(self._("Open camera"))
        self.camera_button.connect("clicked", self._open_camera)
        self.camera_button.set_sensitive(camera_supported())
        self.header.pack_end(self.camera_button)

        # ── Selection-mode header widgets (hidden until long-press activates) ──
        # Swapped layout: trash sits on the LEFT (start), close on the RIGHT
        # (end). Mirrors how Files/Photos lay out destructive bulk actions on
        # the same side as the leading title and keeps the cancel-X at the
        # window-close position the user already reaches for.
        self._sel_delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self._sel_delete_btn.set_tooltip_text(self._("Delete selected"))
        self._sel_delete_btn.add_css_class("destructive-action")
        self._sel_delete_btn.set_visible(False)
        self._sel_delete_btn.connect("clicked", lambda _: self._sel_delete_selected())
        self.header.pack_start(self._sel_delete_btn)

        self._sel_move_btn = Gtk.Button.new_from_icon_name("document-revert-symbolic")
        self._sel_move_btn.set_tooltip_text(self._("Move selected"))
        self._sel_move_btn.set_visible(False)
        self._sel_move_btn.connect("clicked", lambda _: self._sel_move_selected())
        self.header.pack_start(self._sel_move_btn)

        self._sel_share_btn = Gtk.Button.new_from_icon_name("folder-publicshare-symbolic")
        self._sel_share_btn.set_tooltip_text(self._("Share selected"))
        self._sel_share_btn.set_visible(False)
        self._sel_share_btn.connect("clicked", lambda _: self._sel_share_selected())
        self.header.pack_start(self._sel_share_btn)

        self._sel_title = Adw.WindowTitle(title="", subtitle="")
        self._sel_title.set_visible(False)

        self._sel_cancel_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        self._sel_cancel_btn.set_tooltip_text(self._("Cancel selection"))
        self._sel_cancel_btn.set_visible(False)
        self._sel_cancel_btn.connect("clicked", lambda _: self._exit_selection_mode())
        self.header.pack_end(self._sel_cancel_btn)

        # Search bar (toggled via the magnifier in the header). Uses a
        # GtkSearchBar so the entry slides down as a top-bar and animates with
        # the standard GNOME search look.
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(
            self._("Filename, date, month, EXIF…")
        )
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_search_changed)

        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_child(self.search_entry)
        self.search_bar.set_show_close_button(False)
        self.search_bar.set_search_mode(False)
        self.search_bar.connect_entry(self.search_entry)
        # Closing the search bar (toggle off, ESC) wipes the entry so that
        # reopening doesn't silently reapply an old filter, and so the
        # gallery snaps back to its normal view.
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode_toggled)
        self.toolbar.add_top_bar(self.search_bar)
        # Toggle button drives the search bar visibility.
        self.search_button.bind_property(
            "active", self.search_bar, "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        self._search_query: str = ""
        self._search_debounce_id: int = 0

        # Category nav bar — orientation and placement come from settings.
        # Adw.ToolbarView only knows top/bottom bars, so left/right wrap the
        # gallery content in a horizontal Gtk.Box with the nav as a side rail.
        nav_position = getattr(self.settings, "nav_position", "top")
        if nav_position not in ("top", "bottom", "left", "right"):
            nav_position = "top"
        self._nav_position = nav_position
        nav_orientation = (
            Gtk.Orientation.VERTICAL
            if nav_position in ("left", "right")
            else Gtk.Orientation.HORIZONTAL
        )
        self.nav_box = Gtk.Box(orientation=nav_orientation, spacing=0)
        if nav_orientation == Gtk.Orientation.HORIZONTAL:
            # Top/bottom: keep the view-switcher styling (border + padding,
            # plus libadwaita's min-width on descendant buttons that fans
            # them out evenly across the rail).
            self.nav_box.add_css_class("view-switcher")
            self.nav_box.set_hexpand(True)
        else:
            # Left/right side rail: skip view-switcher because libadwaita's
            # min-width on toggle children makes the rail roughly twice as
            # wide as the icon+label needs. Use a positional class instead so
            # the rail sizes to its content (capped via .nav-sidebar CSS).
            self.nav_box.add_css_class("nav-sidebar")
            self.nav_box.add_css_class(f"nav-sidebar-{nav_position}")
            self.nav_box.set_vexpand(True)

        if nav_position == "top":
            self.toolbar.add_top_bar(self.nav_box)
        elif nav_position == "bottom":
            self.toolbar.add_bottom_bar(self.nav_box)
        # For "left" / "right" the nav_box is parented below as part of the content row.

        # Swipe gesture on the nav bar itself: switch categories along the bar's
        # main axis. We use Gtk.GestureDrag rather than Gtk.GestureSwipe so we
        # can force-claim the event sequence on a motion threshold. The
        # category buttons' internal Gtk.GestureClick claims the sequence on
        # press and doesn't release it on mere motion inside the button bounds,
        # which would otherwise lock the swipe out — even in CAPTURE phase.
        # On drag-update we set the sequence state to CLAIMED once motion
        # exceeds a small threshold; that cancels the button's pending click
        # and lets us track velocity through to drag-end. A stationary tap
        # never crosses the threshold, so the button's click still fires
        # normally for a real category select.
        nav_drag = Gtk.GestureDrag()
        nav_drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        nav_drag.connect("drag-begin", self._on_nav_drag_begin)
        nav_drag.connect("drag-update", self._on_nav_drag_update)
        nav_drag.connect("drag-end", self._on_nav_drag_end)
        self.nav_box.add_controller(nav_drag)

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
        # Android-style pull-to-refresh: GestureDrag captures the user's
        # touch from the moment the gallery is at the top. While they
        # over-drag downward we apply a rubber-banded margin to the grid
        # so the list visibly "wobbles" down; only on release past the
        # threshold do we fire a refresh (scoped to the current folder).
        # The old edge-overshot + scroll handlers triggered immediately
        # on any over-pull, which felt twitchy.
        self._pull_started_at_top = False
        self._pull_offset_px = 0.0
        self._pull_threshold_px = 80.0
        self._pull_animation: Adw.TimedAnimation | None = None
        pull_gesture = Gtk.GestureDrag.new()
        # CAPTURE phase: we have to observe motion *before* the inner
        # ScrolledWindow's pan gesture claims it. With BUBBLE the pan
        # already swallowed pure-vertical touches at the top edge of
        # categories with lots of tiles (Overview, Photos) — the user
        # then had to jerk horizontally first to "free" the sequence
        # before the vertical pull would register.
        pull_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        pull_gesture.connect("drag-begin", self._on_pull_drag_begin)
        pull_gesture.connect("drag-update", self._on_pull_drag_update)
        pull_gesture.connect("drag-end", self._on_pull_drag_end)
        # Attach to the overlay (gallery_grid), not the scroller itself —
        # the overlay has no competing pan controller and is the same
        # widget folder_swipe uses successfully.
        self.gallery_grid.add_controller(pull_gesture)
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

        if nav_position == "left":
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            row.set_hexpand(True)
            row.set_vexpand(True)
            row.append(self.nav_box)
            row.append(content)
            self.toolbar.set_content(row)
        elif nav_position == "right":
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            row.set_hexpand(True)
            row.set_vexpand(True)
            row.append(content)
            row.append(self.nav_box)
            self.toolbar.set_content(row)
        else:
            self.toolbar.set_content(content)
        self._rebuild_categories()

        # Mobile breakpoint: at narrow widths the titlebar Refresh icon
        # disappears (pull-to-refresh handles it on touch), keeping the
        # header from looking cramped next to the back arrow on phones.
        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 600px"),
        )
        breakpoint.add_setter(self.refresh_button, "visible", False)
        # Clear any pre-existing breakpoints on rebuilds (nav-layout swap
        # recreates the window, but apply_settings rebuilds in place).
        try:
            self.add_breakpoint(breakpoint)
        except Exception:
            # Older libadwaita doesn't expose breakpoints — silently fall
            # back to "icon always visible". Better than crashing the UI.
            pass

    # ------------------------------------------------------------------
    # Sort popover
    # ------------------------------------------------------------------

    # Internal sort_mode strings ↔ (mode-key, descending bool) tuples.
    _SORT_KEYS = ["none", "date", "folder", "name"]
    _SORT_TO_INTERNAL = {
        ("none",   True):  "newest",
        ("none",   False): "oldest",
        ("date",   True):  "date",
        ("date",   False): "date_asc",
        ("folder", True):  "folder_desc",
        ("folder", False): "folder",
        ("name",   True):  "name_desc",
        ("name",   False): "name",
    }
    _INTERNAL_TO_SORT = {v: k for k, v in _SORT_TO_INTERNAL.items()}

    def _build_sort_controls(self) -> Gtk.Box:
        # Label texts in the dropdown — matched 1:1 with self._SORT_KEYS.
        self._sort_dropdown_labels = ["None", "Date", "Folder", "Name"]
        store = Gtk.StringList()
        for label in self._sort_dropdown_labels:
            store.append(self._(label))
        self._sort_dropdown = Gtk.DropDown.new(store, None)
        self._sort_dropdown.set_valign(Gtk.Align.CENTER)
        self._sort_dropdown.connect("notify::selected", self._on_sort_dropdown_changed)

        self._sort_dir_btn = Gtk.Button()
        self._sort_dir_btn.set_valign(Gtk.Align.CENTER)
        self._sort_dir_btn.add_css_class("flat")
        self._sort_dir_btn.connect("clicked", self._on_sort_direction_clicked)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.append(self._sort_dropdown)
        box.append(self._sort_dir_btn)

        self._sort_updating = False
        self._sync_sort_controls()
        return box

    def _current_sort_internal(self) -> str:
        sort_key = (
            f"{self.category}\x00{self.current_folder}"
            if self.current_folder is not None else self.category
        )
        default = "folder" if self.category == "nextcloud" else self.settings.sort_mode
        return self.settings.sort_modes.get(sort_key, default)

    def _sync_sort_controls(self) -> None:
        """Set dropdown + direction icon based on the persisted sort mode."""
        internal = self._current_sort_internal()
        mode_key, desc = self._INTERNAL_TO_SORT.get(internal, ("none", True))
        try:
            idx = self._SORT_KEYS.index(mode_key)
        except ValueError:
            idx = 0
        self._sort_updating = True
        try:
            if self._sort_dropdown.get_selected() != idx:
                self._sort_dropdown.set_selected(idx)
        finally:
            self._sort_updating = False
        # Icon shows current direction; tooltip explains what a click would do.
        icon_name = (
            "view-sort-descending-symbolic" if desc
            else "view-sort-ascending-symbolic"
        )
        self._sort_dir_btn.set_icon_name(icon_name)
        self._sort_dir_btn.set_tooltip_text(
            self._("Descending") if desc else self._("Ascending")
        )
        # Mirror the direction icon onto the header MenuButton so the user can
        # see the current sort direction at a glance without opening the popover.
        if hasattr(self, "sort_button") and self.sort_button is not None:
            try:
                self.sort_button.set_icon_name(icon_name)
            except Exception:
                pass

    def _on_sort_dropdown_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._sort_updating:
            return
        idx = dropdown.get_selected()
        if idx < 0 or idx >= len(self._SORT_KEYS):
            return
        mode_key = self._SORT_KEYS[idx]
        # Preserve the current direction across mode changes.
        _prev_mode_key, desc = self._INTERNAL_TO_SORT.get(
            self._current_sort_internal(), ("none", True),
        )
        self._apply_sort_mode(mode_key, desc)

    def _on_sort_direction_clicked(self, _btn: Gtk.Button) -> None:
        mode_key, desc = self._INTERNAL_TO_SORT.get(
            self._current_sort_internal(), ("none", True),
        )
        self._apply_sort_mode(mode_key, not desc)

    def _apply_sort_mode(self, mode_key: str, desc: bool) -> None:
        internal = self._SORT_TO_INTERNAL[(mode_key, desc)]
        sort_key = (
            f"{self.category}\x00{self.current_folder}"
            if self.current_folder is not None else self.category
        )
        self.settings.sort_modes[sort_key] = internal
        self.settings.save()
        self._sync_sort_controls()
        if getattr(self, "_sort_popover", None) is not None:
            self._sort_popover.popdown()
        self._render()

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

        # Horizontal nav (top/bottom): each button stretches to fill the row.
        # Vertical nav (left/right): buttons take their natural width and stack
        # at the start; the nav_box itself vexpands so the side rail spans the
        # full window height even with few categories.
        is_vertical = self.nav_box.get_orientation() == Gtk.Orientation.VERTICAL

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
            # Overview has no backing path (it aggregates); every other
            # category still requires a path to make sense in the nav.
            if not path and category != "pictures":
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
            if is_vertical:
                # Side rail: each button takes the rail's natural width
                # automatically (vertical Gtk.Box gives every child the full
                # cross-axis width). Anchor at the top so few categories
                # don't get stretched into rectangles by the box's vexpand.
                button.set_vexpand(False)
                button.set_valign(Gtk.Align.START)
            else:
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
        # but ONLY if the user has actively allowed NC for this session
        # (is_nc_active() respects manual disconnects too).
        need_nc = self.is_nc_active() and (
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
                elif self.category == "pictures":
                    # Overview is a virtual aggregator — to "refresh what
                    # I'm looking at" we have to re-scan every category it
                    # unions. Without this the pull-to-refresh gesture
                    # silently no-op'd on Overview.
                    local_cats = [
                        (c, l, p)
                        for c, l, p in self.settings.categories()
                        if c not in ("nextcloud", "pictures")
                    ]
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
                    if c not in ("nextcloud", "pictures")
                ]
            if local_cats:
                self.scanner.scan(
                    local_cats,
                    excluded_subtrees=self.settings.excluded_subtrees(),
                )

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
        # Sync the dropdown + direction icon to whatever was saved for this view.
        if hasattr(self, "_sort_dropdown"):
            self._sync_sort_controls()
        # Back arrow surfaces whenever the user has drilled into a
        # subfolder. Selection mode flips it back off in _enter_selection_mode.
        self.back_button.set_visible(self.current_folder is not None)
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

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        new = entry.get_text().strip()
        if new == self._search_query:
            # Nothing changed but the user might have hit ESC / cleared via
            # the entry's clear icon — close the search bar in that case.
            if not new and self.search_bar.get_search_mode():
                self.search_bar.set_search_mode(False)
            return
        # Debounce: a search-changed signal fires on every keystroke, but each
        # render runs a COUNT(*) plus a SELECT with multi-OR LIKE clauses on a
        # potentially huge media table. Without debouncing the main loop ends
        # up frozen while the user is typing.
        if getattr(self, "_search_debounce_id", 0):
            GLib.source_remove(self._search_debounce_id)
            self._search_debounce_id = 0
        # Empty query takes effect immediately so the gallery snaps back.
        if not new:
            self._search_query = ""
            self._render()
            if self.search_bar.get_search_mode():
                self.search_bar.set_search_mode(False)
            return
        # Otherwise wait 250 ms after the last keystroke before querying.
        def _fire():
            self._search_debounce_id = 0
            current = self.search_entry.get_text().strip()
            if current == self._search_query:
                return GLib.SOURCE_REMOVE
            self._search_query = current
            self._render()
            return GLib.SOURCE_REMOVE
        self._search_debounce_id = GLib.timeout_add(250, _fire)

    def _on_search_mode_toggled(self, search_bar: Gtk.SearchBar, _param) -> None:
        if search_bar.get_search_mode():
            return
        # Cancel any pending debounced render so it doesn't fire after the bar
        # is already closed.
        if getattr(self, "_search_debounce_id", 0):
            GLib.source_remove(self._search_debounce_id)
            self._search_debounce_id = 0
        # Search bar just closed: drop the query so a category switch later
        # doesn't re-run a stale filter, and clear the entry text.
        had_query = bool(self._search_query)
        self._search_query = ""
        if self.search_entry.get_text():
            self.search_entry.set_text("")
        if had_query:
            self._render()

    def _render_flat(self, sort_mode: str) -> None:
        include_nc = self._should_merge_nc()
        media_filter = self.settings.media_filter_for(self.category)
        self._total_count = self.database.count_media(
            self.category, self.current_folder, include_nc=include_nc,
            media_filter=media_filter,
        )
        page = self.database.list_media_paginated(
            self.category, sort_mode, self.current_folder,
            self._page_size, 0, include_nc=include_nc,
            media_filter=media_filter,
        )
        self.current_items = list(page)
        self._current_offset = len(page)
        self._has_more_items = self._current_offset < self._total_count
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
        self._total_count = self.database.count_media(
            self.category, direct_folder, include_nc=include_nc,
            media_filter=media_filter,
        )
        page = self.database.list_media_paginated(
            self.category, sort_mode, direct_folder,
            self._page_size, 0, include_nc=include_nc,
            media_filter=media_filter,
        )
        self.current_items = list(page)
        self._current_offset = len(page)
        self._has_more_items = self._current_offset < self._total_count
        for item in page:
            self.gallery_grid.append_media(item)
        total = len(folders) + len(self.current_items)
        self._set_empty_state(visible=total == 0)
        self._set_status("")

    def _render_date_groups(self, ascending: bool = False) -> None:
        order = "oldest" if ascending else "newest"
        include_nc = self._should_merge_nc()
        media_filter = self.settings.media_filter_for(self.category)
        self._total_count = self.database.count_media(
            self.category, self.current_folder, include_nc=include_nc,
            media_filter=media_filter,
        )
        page = self.database.list_media_paginated(
            self.category, order, self.current_folder,
            self._page_size, 0, include_nc=include_nc,
            media_filter=media_filter,
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
            self.gallery_grid.append_header(
                self._month_header_markup(dt), year=dt.year, month=dt.month,
            )
            self._date_last_key = key
        self.gallery_grid.append_media(item)

    # English month names indexed by month-1. Used as translation keys so
    # the in-app language switch (Translator) drives the header text
    # instead of the system locale — strftime("%B") follows LC_TIME and
    # ignored the user's pick in Settings → Language.
    _MONTH_NAMES_EN = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )

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
            if sort_mode == "folder" and not self._search_query:
                # Folder mode merges NC only at the root.
                include_nc = include_nc and self.current_folder in (None, "/")
                folder_arg = self.current_folder or "/"
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

        # Guard against double-creation: pressing Enter inside the EntryRow
        # AND the dialog's own default-response Enter handling could otherwise
        # both fire "create" before the dialog closes.
        done_state = {"fired": False}

        def _done(_dialog, response):
            if done_state["fired"]:
                return
            done_state["fired"] = True
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

    # Coalesces multiple back-to-back scan-completion calls into a single
    # eviction pass — without these guards a user flipping fast between
    # folders kicks off N parallel rglob walks of THUMB_DIR + _NC_CACHE.
    _EVICT_MIN_INTERVAL_SEC = 60.0

    def evict_cache_async(self) -> None:
        """Run eviction in a daemon thread so the main loop never blocks on it.
        Coalesces re-entry: while a worker is in flight, or a worker has
        just finished within the throttle window, the call is dropped."""
        if getattr(self.settings, "cache_max_mb", 0) <= 0:
            return
        # Lazily attach the re-entry guard so existing instances in tests
        # that bypass __init__ keep working.
        if not hasattr(self, "_evict_lock"):
            self._evict_lock = threading.Lock()
            self._evict_in_flight = False
            self._evict_last_finished_at = 0.0
        with self._evict_lock:
            if self._evict_in_flight:
                return
            now = time.monotonic()
            if now - self._evict_last_finished_at < self._EVICT_MIN_INTERVAL_SEC:
                return
            self._evict_in_flight = True
        threading.Thread(target=self._evict_cache_worker, daemon=True).start()

    def _evict_cache_worker(self) -> None:
        try:
            self.evict_cache()
        finally:
            with self._evict_lock:
                self._evict_in_flight = False
                self._evict_last_finished_at = time.monotonic()

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

        Bails out silently when NC isn't actively allowed for this session —
        we never re-establish the connection on our own; that requires explicit
        consent (Settings toggle/Connect button or the viewer's "Einmalig/
        Dauerhaft" prompt)."""
        if not self.is_nc_active():
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

    def _cancel_nc_thumb_queue(self) -> None:
        """Drop every queued NC thumbnail fetch. Workers currently mid-HTTP
        run to completion (the requests library has no cheap interrupt),
        but no new fetches start. Call this on every navigation that
        changes ``current_folder`` so rapid folder hopping doesn't keep
        the previous folder's thumbnails downloading in the background."""
        with self._nc_thumb_lock:
            if not self._nc_thumb_queue:
                return
            for path in self._nc_thumb_queue:
                self._nc_thumb_pending.discard(path)
            self._nc_thumb_queue.clear()
        # Wake idle workers so they re-evaluate the now-empty queue and
        # fall through to their idle-timeout path instead of blocking.
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
                        # FIFO: tiles are bound top-to-bottom as the gallery is
                        # built, so popping from the front means thumbs arrive
                        # in the same order as the active sort mode dictates.
                        path = self._nc_thumb_queue.pop(0)
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
        # Drop the previous folder's queued thumbnail fetches before we
        # change views — bind on the new folder will re-queue whatever
        # actually scrolls into the new viewport.
        self._cancel_nc_thumb_queue()
        self.current_folder = folder
        self._render()
        # No bulk thumbnail pre-fetch on folder open: the gallery requests each
        # tile's NC thumbnail on demand as it scrolls into view.

    def _open_item(self, _button, item: MediaItem) -> None:
        if item.is_video and self.settings.external_video_player.strip():
            # `--` is an end-of-options marker so a hypothetical filename
            # starting with '-' (we currently never emit one, but cheap
            # defense) can't be reinterpreted as an option by the player.
            # Matches the convention used in _open_externally below.
            subprocess.Popen(
                shlex.split(self.settings.external_video_player) + ["--", item.path],
            )
            return
        items = self.current_items or self.database.list_media(
            item.category, self.settings.get_sort_mode(item.category, self.current_folder),
            self.current_folder,
            media_filter=self.settings.media_filter_for(item.category),
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
            ("Move", "document-revert-symbolic", self._move_item),
            ("Share", "folder-publicshare-symbolic", self._share_item),
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
        self.search_button.set_visible(False)
        self.refresh_button.set_visible(False)
        self.settings_button.set_visible(False)
        self.sort_button.set_visible(False)
        self._sel_cancel_btn.set_visible(True)
        self._sel_delete_btn.set_visible(True)
        self._sel_move_btn.set_visible(True)
        self._sel_share_btn.set_visible(True)
        self.header.set_title_widget(self._sel_title)
        self._update_sel_title()
        # Force every materialised tile to re-bind so the checkbox overlay
        # appears across the visible viewport instead of only on the tile
        # that was just long-pressed.
        self.gallery_grid.refresh_selection_state()

    def _exit_selection_mode(self) -> None:
        self._selection_mode = False
        self._selected_paths.clear()
        self._sel_cancel_btn.set_visible(False)
        self._sel_delete_btn.set_visible(False)
        self._sel_move_btn.set_visible(False)
        self._sel_share_btn.set_visible(False)
        # Restore normal header
        title = Adw.WindowTitle(title=APP_NAME, subtitle="")
        self.header.set_title_widget(title)
        # Mirror _render()'s rule so leaving multi-select inside a
        # subfolder still shows the back arrow.
        self.back_button.set_visible(self.current_folder is not None)
        self.new_folder_button.set_visible(True)
        self.search_button.set_visible(True)
        self.refresh_button.set_visible(True)
        self.settings_button.set_visible(True)
        self.sort_button.set_visible(True)
        # Splice every visible row back so check-mark overlays disappear,
        # without re-querying the database or losing the scroll position.
        self.gallery_grid.refresh_selection_state()

    def _toggle_selection(self, path: str) -> None:
        if self._sel_busy:
            return
        if path in self._selected_paths:
            self._selected_paths.discard(path)
        else:
            self._selected_paths.add(path)
        if not self._selected_paths:
            # Clearing the last item exits selection mode entirely.
            self._exit_selection_mode()
            return
        self._update_sel_title()
        # Re-bind just this tile so the checkbox visual catches up. Falls
        # back to a full render when the path isn't in the materialised
        # window (lazy-loaded paths land here on first toggle).
        if not self.gallery_grid.update_tile_for_path(path):
            self._render()

    def _update_sel_title(self) -> None:
        n = len(self._selected_paths)
        self._sel_title.set_title(f"{n} {self._('selected')}")
        self._sel_title.set_subtitle("")

    def _sel_delete_selected(self) -> None:
        if self._sel_busy:
            return
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
        if response != "delete" or self._sel_busy:
            return
        n = len(paths)
        category = self.category
        self._set_sel_busy(True, self._("Deleting %d items…") % n)

        def _worker() -> None:
            errors: list[tuple[str, Exception]] = []
            try:
                for path in paths:
                    try:
                        Gio.File.new_for_path(path).trash(None)
                        self.database.delete_path(path, category)
                    except Exception as e:
                        errors.append((path, e))
            except Exception:
                LOGGER.exception("Bulk delete worker crashed")
            finally:
                # Always unfreeze the toolbar via the done-handler.
                GLib.idle_add(self._on_sel_delete_done, n, errors)

        threading.Thread(target=_worker, daemon=True, name="sel-delete").start()

    def _on_sel_delete_done(self, total: int, errors: list[tuple[str, Exception]]) -> bool:
        self._set_sel_busy(False, "")
        self._exit_selection_mode()
        succeeded = total - len(errors)
        if not errors:
            self._set_status(self._("Deleted %d items") % total)
        elif len(errors) == total:
            self._show_error_dialog(
                self._("Delete failed"),
                self._("Could not delete all files. Check file permissions or disk state."),
                f"{len(errors)}/{total} items",
            )
        else:
            self._set_status(
                self._("Deleted %d/%d items (%d failed)") % (
                    succeeded, total, len(errors),
                )
            )
        return GLib.SOURCE_REMOVE

    def _sel_move_selected(self) -> None:
        if self._sel_busy or not self._selected_paths:
            return
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._on_sel_move_response)
        chooser.show()

    def _on_sel_move_response(self, chooser: Gtk.FileChooserNative, response: int) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            chooser.destroy()
            return
        folder = chooser.get_file().get_path()
        chooser.destroy()
        # Snapshot the selection BEFORE the worker starts and BEFORE we exit
        # selection mode at completion — the previous version computed the
        # success count from self._selected_paths after _exit_selection_mode
        # had cleared it, so the status always read "Moved 0 items".
        paths = list(self._selected_paths)
        if not paths:
            return
        n = len(paths)
        category = self.category
        self._set_sel_busy(True, self._("Moving %d items…") % n)

        def _worker() -> None:
            errors: list[tuple[str, Exception]] = []
            try:
                for path in paths:
                    # Catch every per-item failure (sqlite errors, weird
                    # filesystems, …) so one bad file doesn't kill the loop
                    # and leave _sel_busy stuck at True with the toolbar
                    # frozen. Specific exception types were too narrow.
                    try:
                        target = Path(folder) / Path(path).name
                        Path(path).rename(target)
                        self.database.delete_path(path, category)
                    except Exception as e:
                        errors.append((path, e))
            except Exception:
                LOGGER.exception("Bulk move worker crashed")
            finally:
                # Always schedule the done-handler, even on catastrophic
                # worker failure — _on_sel_move_done is what unfreezes the
                # toolbar and exits selection mode.
                GLib.idle_add(self._on_sel_move_done, n, errors)

        threading.Thread(target=_worker, daemon=True, name="sel-move").start()

    def _on_sel_move_done(self, total: int, errors: list[tuple[str, Exception]]) -> bool:
        self._set_sel_busy(False, "")
        self._exit_selection_mode()
        succeeded = total - len(errors)
        # Trigger a rescan so the destination shows up if the user navigates
        # there. Same pattern as the single-item move path.
        self.refresh(scan=True)
        if not errors:
            self._set_status(self._("Moved %d items") % total)
        elif len(errors) == total:
            self._show_error_dialog(
                self._("Move failed"),
                self._("Could not move files. Check file permissions and disk space."),
                f"{len(errors)} file(s) failed",
            )
        else:
            self._set_status(
                self._("Moved %d items (%d failed)") % (succeeded, len(errors))
            )
        return GLib.SOURCE_REMOVE

    def _set_sel_busy(self, busy: bool, status: str) -> None:
        """Toggle the in-flight state for bulk delete/move. Disables the
        toolbar buttons while a worker thread runs and surfaces a status
        line so the user sees the operation is making progress."""
        self._sel_busy = busy
        for btn in (
            self._sel_cancel_btn,
            self._sel_delete_btn,
            self._sel_move_btn,
            self._sel_share_btn,
        ):
            btn.set_sensitive(not busy)
        if status:
            self._set_status(status)

    def _delete_item(self, item: MediaItem) -> None:
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
        """Context-menu single-image share entry — opens the same dialog the
        viewer/selection share buttons use, just with a one-element list."""
        self.open_share_dialog([item.path])

    def _sel_share_selected(self) -> None:
        if self._sel_busy or not self._selected_paths:
            return
        # Snapshot before the dialog runs — selection mode might be exited
        # asynchronously (e.g. dialog close races with a long-press).
        paths = list(self._selected_paths)
        self.open_share_dialog(paths)

    def open_share_dialog(self, paths: list[str]) -> None:
        """Show the share-method picker for *paths*. Currently exposes only
        an Email option (via xdg-email --attach), but the dialog shape is
        ready for additional channels."""
        from .nextcloud import is_nc_path
        # NC items live under nextcloud:// — xdg-email can't attach those.
        # Drop them with a status hint instead of silently ignoring.
        local_paths = [p for p in paths if not is_nc_path(p)]
        skipped_nc = len(paths) - len(local_paths)

        n = len(local_paths)
        if n == 0:
            if skipped_nc:
                self._set_status(self._(
                    "Cannot share Nextcloud items directly — open them first to download."
                ))
            return

        heading = (
            self._("Share image")
            if n == 1
            else self._("Share %d images") % n
        )
        body = self._("Choose how to share:")
        if skipped_nc:
            body += "\n\n" + self._(
                "%d Nextcloud item(s) skipped (not downloaded locally)."
            ) % skipped_nc
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", self._("Cancel"))
        if shutil.which("xdg-email"):
            dialog.add_response("email", self._("Email"))
            dialog.set_default_response("email")
            dialog.set_response_appearance("email", Adw.ResponseAppearance.SUGGESTED)
        else:
            dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_share_dialog_response, local_paths)
        dialog.present(self)

    def _on_share_dialog_response(
        self, _dialog, response: str, paths: list[str],
    ) -> None:
        if response != "email" or not paths:
            return
        # xdg-email reads --attach + filename pairs. Absolute paths from the
        # scanner can't start with '-', so they can't be misread as options.
        argv = ["xdg-email"]
        for p in paths:
            argv.extend(["--attach", p])
        try:
            subprocess.Popen(argv)
        except OSError as exc:
            LOGGER.exception("xdg-email failed: %s", exc)
            self._set_status(self._("Could not complete action"))

    def _open_externally(self, item: MediaItem) -> None:
        # '--' is a real end-of-options marker in xdg-open: a hypothetical
        # filename starting with '-' (we currently never emit one, but cheap
        # defense) can't be reinterpreted as an option.
        subprocess.Popen(["xdg-open", "--", item.path])

    # ------------------------------------------------------------------
    # Navigation handlers
    # ------------------------------------------------------------------

    def _on_category_toggled(self, button: Gtk.ToggleButton, category: str) -> None:
        if not button.get_active():
            if category == self.category and self.current_folder is not None:
                button.handler_block_by_func(self._on_category_toggled)
                button.set_active(True)
                button.handler_unblock_by_func(self._on_category_toggled)
                self._cancel_nc_thumb_queue()
                self.current_folder = None
                self._render()
            return
        self._cancel_nc_thumb_queue()
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
        # Stop fetching the now-leaving folder's thumbnails; the parent view
        # re-queues whatever it actually shows.
        self._cancel_nc_thumb_queue()
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

    # Motion in px below which we treat a press+release as a tap (synthesise
    # the button click) rather than a swipe. Above this on the primary axis
    # the existing _on_nav_swipe velocity logic gets a shot.
    _NAV_SWIPE_TAP_PX = 16

    def _on_nav_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        # Claim immediately so the ToggleButton's internal Gtk.GestureClick
        # can't lock the press sequence and starve us of motion / release
        # events. With "claim on motion threshold" the click already won by
        # the time motion accumulated, which is why every previous variant
        # failed to swipe over icons. We pay for this by losing the button's
        # press visual; in exchange the gesture is reliable.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._nav_drag_start_us = GLib.get_monotonic_time()
        # Remember which button the press landed on so a non-swipe release
        # can synthesise the click that the denied GestureClick would have.
        self._nav_press_button = self._find_nav_button_at(x, y)

    def _on_nav_drag_update(self, _gesture: Gtk.GestureDrag, _ox: float, _oy: float) -> None:
        # Decisions happen on drag-end; cumulative offset is the truthful signal.
        return

    def _on_nav_drag_end(self, _gesture: Gtk.GestureDrag, ox: float, oy: float) -> None:
        elapsed_us = max(1, GLib.get_monotonic_time() - getattr(self, "_nav_drag_start_us", 0))
        is_vertical = (
            self.nav_box.get_orientation() == Gtk.Orientation.VERTICAL
        )
        primary, secondary = (oy, ox) if is_vertical else (ox, oy)
        press_button = getattr(self, "_nav_press_button", None)
        self._nav_press_button = None

        if abs(primary) >= self._NAV_SWIPE_TAP_PX and abs(primary) > abs(secondary):
            # Real swipe — convert offset/time to px/s and reuse the swipe handler.
            scale = 1_000_000 / elapsed_us
            self._on_nav_swipe(None, ox * scale, oy * scale)
            return
        # Tap: synthesise the click on the button that was under the press
        # point. set_active(True) emits "toggled" → _on_category_toggled,
        # exactly the path a normal click would have followed.
        if press_button is not None and not press_button.get_active():
            try:
                press_button.set_active(True)
            except Exception:
                pass

    def _find_nav_button_at(self, x: float, y: float) -> "Gtk.ToggleButton | None":
        """Walk nav_box children and return the ToggleButton whose
        allocation contains (x, y) in nav_box coords. Used by the swipe
        gesture to know which category a finger-down was aiming at, even
        though we claim the sequence before the button's click sees it."""
        child = self.nav_box.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.ToggleButton):
                ok, bounds = child.compute_bounds(self.nav_box)
                if ok:
                    bx = bounds.get_x()
                    by = bounds.get_y()
                    if (bx <= x <= bx + bounds.get_width()
                        and by <= y <= by + bounds.get_height()):
                        return child
            child = child.get_next_sibling()
        return None

    def _on_nav_swipe(self, _gesture, velocity_x: float, velocity_y: float) -> None:
        """Swipe on the nav bar to step through categories along its main axis.

        Horizontal nav (top/bottom): velocity_x picks the direction, swipe
        right (positive x) jumps to the next category, left to the previous.
        Vertical nav (left/right side rail): velocity_y instead, down = next,
        up = previous. The same 350 px/s threshold the folder-back swipe uses
        keeps stray finger drags from triggering a category jump. No wrap at
        the ends — silent no-op so the user can't accidentally lap past the
        last category back to the first.
        """
        if self._selection_mode:
            return
        is_vertical = (
            self.nav_box.get_orientation() == Gtk.Orientation.VERTICAL
        )
        if is_vertical:
            primary, secondary = velocity_y, velocity_x
        else:
            primary, secondary = velocity_x, velocity_y
        if abs(primary) < 350 or abs(primary) <= abs(secondary):
            return
        cats = [cat for cat, _label, _path in self.settings.categories()]
        if not cats or self.category not in cats:
            return
        idx = cats.index(self.category)
        new_idx = idx + (1 if primary > 0 else -1)
        if not (0 <= new_idx < len(cats)):
            return
        target = self.category_buttons.get(cats[new_idx])
        if target is not None:
            target.set_active(True)  # fires _on_category_toggled

    def _open_settings(self, _button: Gtk.Button) -> None:
        # Idempotent: if a dialog is already open, just bring it to the front
        # instead of stacking a second one. The reference is cleared in
        # _on_settings_dialog_closed when the dialog destroys.
        existing = self._settings_dialog
        if existing is not None:
            try:
                existing.present()
                return
            except Exception:
                # Stale reference (rare race after destroy) — fall through
                # and create a fresh one.
                self._settings_dialog = None
        dialog = SettingsWindow(self)
        self._settings_dialog = dialog
        dialog.connect("close-request", self._on_settings_dialog_closed)
        dialog.connect("destroy", self._on_settings_dialog_closed)
        dialog.present()

    def _on_settings_dialog_closed(self, _dialog) -> bool:
        # Drop the reference so the next gear-button click creates a fresh
        # dialog. Returning False on close-request lets the close proceed.
        self._settings_dialog = None
        return False

    def _open_camera(self, _button: Gtk.Button) -> None:
        save_dir = Path(self.settings.photos_dir)
        video_dir = Path(self.settings.videos_dir) if self.settings.videos_dir else save_dir
        win = CameraWindow(
            self,
            save_dir=save_dir,
            video_dir=video_dir,
            translator=self._,
            on_captured=lambda _p: self.refresh(scan=True, scope="current"),
            handedness=self.settings.handedness,
        )
        win.present()

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
        # Resync the runtime gate with the persisted preferences. Settings is
        # the source of truth here — anything else would re-enable NC behind
        # the user's back when applying settings after a manual disconnect.
        self._nc_session_active = bool(
            self.settings.nextcloud_enabled
            and getattr(self.settings, "nextcloud_session_active", True)
        )
        # Block our own notify::dark handler while we tear down and rebuild —
        # otherwise set_color_scheme synchronously triggers _rebuild_categories
        # on the nav_box that _build_ui is about to discard, which on rapid
        # back-and-forth theme switches deadlocks GTK's layout pass (the
        # observed dark→light→dark freeze).
        mgr = Adw.StyleManager.get_default()
        handler_id = getattr(self, "_theme_handler_id", 0)
        if handler_id:
            mgr.handler_block(handler_id)
        try:
            self._apply_theme()
        finally:
            if handler_id:
                mgr.handler_unblock(handler_id)
        # Defer the heavy widget-tree rebuild + scan so the GTK signal that
        # delivered us here (typically Adw.ComboRow notify::selected from the
        # settings dialog) can finish dispatching before we tear down the tree
        # it's still operating on. Synchronous rebuilds from inside a child
        # signal — especially ones that change the toolbar topology, e.g.
        # moving the category nav between top-bar and side rail — have been
        # observed to lock up GTK's layout pass. Coalesce duplicate requests
        # so a quick succession of combo changes only rebuilds once.
        if not getattr(self, "_settings_rebuild_pending", False):
            self._settings_rebuild_pending = True
            GLib.idle_add(self._do_settings_rebuild, priority=GLib.PRIORITY_HIGH)

    def _do_settings_rebuild(self) -> bool:
        self._settings_rebuild_pending = False
        # Detect nav-position changes: those swap the toolbar topology
        # (top/bottom-bar vs. side rail in a horizontal Gtk.Box wrapper) and
        # cannot be safely rebuilt in place. The previous in-place attempt
        # deadlocked GTK's layout pass; the hide/rebuild/show variant cleared
        # the deadlock but left the still-open modal settings dialog with a
        # broken input grab (window appeared visible but accepted no input).
        # Recreating the GalleryWindow is the only fully robust path: every
        # transient child (the settings dialog) gets cleanly destroyed with
        # the old window, the new window starts with a fresh layout pass,
        # and persisted settings (already saved by apply_settings above) are
        # picked up by the new window's Settings.load() in __init__.
        new_position = getattr(self.settings, "nav_position", "top")
        if new_position not in ("top", "bottom", "left", "right"):
            new_position = "top"
        old_position = getattr(self, "_nav_position", "top")
        if old_position != new_position:
            self._recreate_window_for_layout_change()
            return GLib.SOURCE_REMOVE
        # Lighter changes (theme, grid columns, cache budget, NC flags …) just
        # rebuild the toolbar tree in place — no topology change, no deadlock.
        self._build_ui()
        self.refresh(scan=True)
        return GLib.SOURCE_REMOVE

    def _recreate_window_for_layout_change(self) -> None:
        """Replace this window with a fresh GalleryWindow on the same app.

        Any transient children (settings dialog) get destroyed with self when
        ``self.destroy()`` runs at the end. The new window goes through the
        same __init__ path as a normal app launch, so its Settings.load()
        picks up nav_position (already persisted by apply_settings) and the
        new layout is built once, cleanly, with no in-place tree mutation.

        We stash a one-shot hint on the Adw.Application — which survives the
        window swap — telling the new window to reopen the settings dialog
        on the same page the user was just looking at. Without this the user
        would be kicked out to the bare gallery after every nav-position
        change, even though they were mid-flow in Settings/Appearance.
        """
        app = self.get_application()
        if app is None:
            # No application context — fall back to in-place rebuild rather
            # than orphaning the window. Shouldn't happen for a presented
            # window, but the guard keeps the path total.
            self._build_ui()
            self.refresh(scan=True)
            return
        new_window = GalleryWindow(app)
        new_window.present()
        # Explicitly tear down our tracked settings dialog before destroying
        # ourselves. Adw.PreferencesWindow with transient_for=parent isn't
        # auto-registered with the Adw.Application, so iterating
        # app.get_windows() cannot find it. Without this destroy the old
        # dialog survives the parent destroy on some WMs and the user ends
        # up with two settings dialogs visible after a recreate.
        dialog = self._settings_dialog
        if dialog is not None:
            self._settings_dialog = None
            try:
                dialog.destroy()
            except Exception:
                pass
        # Destroy after present + dialog cleanup so the app has a window at
        # all times — Adw quits the main loop when the last window goes
        # away, which would take the new window down with it on certain WMs.
        self.destroy()

    # ------------------------------------------------------------------
    # CSS / theme
    # ------------------------------------------------------------------

    def _on_grid_tick(self, widget: Gtk.Widget, _clock) -> bool:
        width = widget.get_width()
        if width != self._grid_width:
            self._grid_width = width
            self._update_tile_size(width)
        return GLib.SOURCE_CONTINUE

    # ──────────────────────────────────────────────────────────────────
    # Pull-to-refresh (drag gesture with rubber-band wobble)
    # ──────────────────────────────────────────────────────────────────

    def _on_pull_drag_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        # Only arm the pull when the gallery is fully scrolled to the top
        # at the moment the press starts. Anywhere else the gesture must
        # stay a no-op so normal kinetic scrolling and tile clicks keep
        # working.
        adj = self.gallery_grid.get_vadjustment()
        self._pull_started_at_top = adj.get_value() <= adj.get_lower() + 1.0
        self._pull_offset_px = 0.0
        if self._pull_animation is not None:
            self._pull_animation.pause()
            self._pull_animation = None

    def _on_pull_drag_update(self, _gesture: Gtk.GestureDrag,
                             _offset_x: float, offset_y: float) -> None:
        if not self._pull_started_at_top or self._selection_mode:
            return
        if offset_y <= 0:
            # User changed mind and dragged upward — collapse any
            # visual offset and stop tracking until the next touch-down.
            if self._pull_offset_px != 0:
                self._pull_offset_px = 0.0
                self.gallery_grid.grid_view.set_margin_top(0)
                self.gallery_grid.pull_revealer.set_reveal_child(False)
            return
        # 1:1 follow up to threshold, then diminishing returns so the
        # extra pull "fights back" the way an Android list bounces past
        # its natural limit.
        if offset_y <= self._pull_threshold_px:
            eased = offset_y
        else:
            excess = offset_y - self._pull_threshold_px
            eased = self._pull_threshold_px + min(excess * 0.4, 60.0)
        self._pull_offset_px = eased
        self.gallery_grid.grid_view.set_margin_top(int(eased))
        self.gallery_grid.pull_revealer.set_reveal_child(eased >= 24)

    def _on_pull_drag_end(self, _gesture: Gtk.GestureDrag,
                          _offset_x: float, _offset_y: float) -> None:
        if not self._pull_started_at_top:
            return
        triggered = self._pull_offset_px >= self._pull_threshold_px
        self._pull_started_at_top = False
        if triggered:
            self._trigger_pull_refresh()
            self._animate_pull_release(duration_ms=420, easing=Adw.Easing.EASE_OUT_CUBIC)
        else:
            self._animate_pull_release(duration_ms=260, easing=Adw.Easing.EASE_OUT_BACK)
        self._pull_offset_px = 0.0

    def _animate_pull_release(self, duration_ms: int, easing: "Adw.Easing") -> None:
        """Spring the grid's top margin back to 0. EASE_OUT_BACK gives a
        small wobble at the end; EASE_OUT_CUBIC just glides."""
        start = float(self.gallery_grid.grid_view.get_margin_top())
        if start <= 0:
            self.gallery_grid.pull_revealer.set_reveal_child(False)
            return
        target = Adw.CallbackAnimationTarget.new(
            lambda v: self.gallery_grid.grid_view.set_margin_top(max(0, int(v)))
        )
        animation = Adw.TimedAnimation.new(
            self.gallery_grid.grid_view, start, 0.0, duration_ms, target,
        )
        animation.set_easing(easing)
        animation.connect(
            "done",
            lambda *_: self.gallery_grid.pull_revealer.set_reveal_child(False),
        )
        self._pull_animation = animation
        animation.play()

    def _trigger_pull_refresh(self) -> None:
        if not self.refresh_button.get_sensitive():
            return
        LOGGER.info("Pull refresh triggered for category %s", self.category)
        self.gallery_grid.pull_revealer.set_reveal_child(True)
        # scope="current" keeps the scan limited to the active category —
        # the pull gesture is a "refresh what I'm looking at" affordance.
        self.refresh(scan=True, scope="current")
        GLib.timeout_add(
            1200,
            lambda: self.gallery_grid.pull_revealer.set_reveal_child(False) or False,
        )

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
                background: transparent;
                color: @window_fg_color;
                font-size: 32px;
            }
            /* Up/down arrows pinned to the right edge of every month header.
               Subtle by default (low opacity), full opacity on hover so they
               stay discoverable without competing with the date typography.
               Hit-area sized for touch (~44px square per Apple HIG / Material
               minimum); padding rather than icon scaling does the work, so
               the icon glyph itself stays at its default symbolic 16px. */
            .date-header-nav {
                opacity: 0.45;
                min-width: 44px;
                min-height: 44px;
                padding: 12px;
            }
            .date-header-nav:hover {
                opacity: 1.0;
            }
            /* Pin the icon glyph at the default symbolic size: without this,
               some themes scale the icon proportionally with the button's
               padding/min-size, which would defeat the "big button, small
               icon" intent. */
            .date-header-nav image {
                -gtk-icon-size: 16px;
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
            /* Side rail (nav at left/right). Sized to content, with a
               separator border on the side facing the gallery. No max-width
               here: combined with the descendant button min-width override
               below, GTK4 was observed to enter a measure feedback loop when
               a long category label pushed the natural width past the cap. */
            .nav-sidebar {
                padding: 4px 2px;
            }
            .nav-sidebar button {
                /* Cancel libadwaita's button min-width so the rail tracks the
                   actual icon+label size rather than the toolbar-toggle width. */
                min-width: 0;
                padding: 6px 4px;
                margin: 1px 2px;
            }
            .nav-sidebar-left {
                border-right: 1px solid @borders;
            }
            .nav-sidebar-right {
                border-left: 1px solid @borders;
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
            .viewer-filename {
                /* Same black-pill look as the date, but at the regular font size */
                padding: 8px 18px;
                background: rgba(0,0,0,0.55);
                color: white;
                border-radius: 14px;
            }
            /* Editor toolbar: icons follow the standard window foreground so
               they stay legible in both light and dark themes (the viewer
               window's fullscreen black backdrop would otherwise tint the
               toolbar dark and make the symbolic icons disappear). */
            .editor-nav,
            .editor-nav button,
            .editor-nav image,
            .editor-nav label {
                color: @window_fg_color;
            }
            .editor-nav {
                background-color: @headerbar_bg_color;
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
