from __future__ import annotations

import faulthandler
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk

from . import APP_ID, APP_NAME, mcp_server
from .config import DEBUG_LOG_PATH, Settings, migrate_legacy_dirs, resolve_nav_position
from .database import Database
from .gtk_util import display_is_desktop, idle_once
from .gallery_grid import GalleryGrid
from .gallery_render import GalleryRenderMixin
from .gallery_selection import GallerySelectionMixin
from .gallery_style import GalleryStyleGestureMixin
from .gallery_thumbnails import GalleryWindowThumbnailsMixin
from .i18n import Translator
from .models import MediaItem
from .settings_window import SettingsWindow

if TYPE_CHECKING:
    from .nextcloud import NextcloudClient
from .scanner import MediaScanner
from .thumbnails import Thumbnailer, pillow_version_warning
from .viewer import ViewerWindow
from .watcher import MediaWatcher
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
        LOGGER.debug("DEBUG_LOG_PATH.chmod failed", exc_info=True)


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
        # Release the MCP port on the way out. Its worker threads are daemons
        # and would not hold the process open, but the listening socket lives
        # until the process actually exits — long enough for a relaunch to hit
        # "address already in use".
        self.connect("shutdown", lambda _app: mcp_server.shutdown())

    def on_activate(self, _app: Adw.Application) -> None:
        # If --trace is active, prove the main loop is alive via a 1 Hz heartbeat
        # so the watchdog can distinguish "idle main loop" from "frozen main loop".
        if "muga.tracer" in sys.modules:
            sys.modules["muga.tracer"].start_heartbeat()

        icons_dir = Path(__file__).parent / "data" / "icons"
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icons_dir))

        # Cleanup leftover temp files from previous sessions
        _cleanup_abandoned_temp_files()

        window = GalleryWindow(self)
        window.present()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class GalleryWindow(
    GalleryRenderMixin,
    GallerySelectionMixin,
    GalleryStyleGestureMixin,
    GalleryWindowThumbnailsMixin,
    Adw.ApplicationWindow,
):
    def __init__(self, app: GalleryApplication) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1120, 760)
        self.set_icon_name(APP_ID)

        # Settings.load() and Database() both self-heal on damaged input, but
        # they are the very first things to run at startup and the only ones
        # whose failure means no window at all — on a phone that shows up as
        # "the app just doesn't open", with the traceback on a terminal nobody
        # is looking at. Fall back to defaults / an in-memory index so the user
        # still gets a working window and a visible explanation.
        self._startup_warning: str = ""
        try:
            self.settings = Settings.load()
        except Exception:
            LOGGER.exception("Could not load settings — starting with defaults")
            self.settings = Settings()
            self._startup_warning = "settings"
        self.translator = Translator(self.settings.language)
        try:
            self.database = Database()
        except Exception:
            LOGGER.exception("Could not open the media index — using a temporary one")
            self.database = Database(Path(tempfile.gettempdir()) / "muga-fallback.sqlite3")
            self._startup_warning = "database"
        self.thumbnailer = Thumbnailer()
        self.scanner = MediaScanner(self.database, self.thumbnailer)
        # The MCP server is a module-level singleton, so this both starts it
        # when it was left enabled and re-points an already-running one at
        # this window's database — a nav-position change builds the new window
        # before destroying the old one, and a window-owned server would still
        # be holding the port at that moment.
        try:
            mcp_server.sync_with_settings(
                self.settings, self.database, on_change=self._on_mcp_change,
            )
        except Exception:
            LOGGER.exception("Could not bring up the MCP server")
        self.category = self._first_existing_category()
        self.current_folder: str | None = None
        self.current_items: list[MediaItem] = []
        self.category_buttons: dict[str, Gtk.ToggleButton] = {}
        self._selection_mode: bool = False
        self._selected_paths: set[str] = set()
        # Set when this window is being torn down (e.g. replaced on a
        # nav-position change). Background scan / thumbnail / bulk-op workers
        # bounce their results back via GLib.idle_add and can fire after
        # destroy(); the bounce-back handlers check this flag and bail so they
        # never touch a finalized widget tree.
        self._closing: bool = False
        # While a bulk delete/move worker is in flight: re-entry guard so
        # double-clicks on the toolbar buttons don't kick off a second pass.
        self._sel_busy: bool = False
        self._nc_spinner: Gtk.Spinner | None = None
        self._nc_broken_img: Gtk.Image | None = None
        # Why the broken badge is showing, and since when. Kept on the window
        # so the diagnostics report can name it — see _set_nc_broken.
        self._nc_broken_reason: str = ""
        self._nc_broken_since: str = ""
        # On-demand NC thumbnail loader (used by gallery_grid when binding tiles)
        self._nc_thumb_pending: set[str] = set()
        self._nc_thumb_lock = threading.Lock()
        self._nc_thumb_queue: list[str] = []
        self._nc_thumb_event = threading.Event()
        self._nc_thumb_active_workers = 0
        self._nc_thumb_worker_target = 4  # parallel HTTPS thumb fetchers
        self._nc_thumb_shared_client: "NextcloudClient | None" = None  # lazily built, reused across workers
        # Circuit breaker: set once an NC network op fails (sync or thumb fetch)
        # so we stop hammering a server we already know is unreachable. Reset on
        # the next successful sync or on (re)connect via apply_settings().
        self._nc_unreachable = False
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
        # On-demand LOCAL thumbnail loader: when a local tile binds without a
        # cached thumb (scanner hasn't reached it, or generation failed), we
        # decode it on this small pool instead of loading the full-resolution
        # file on the UI thread — the latter janks scrolling. Mirrors the NC
        # on-demand path; idempotent per path via the pending set.
        self._local_thumb_pending: set[str] = set()
        # Paths whose thumbnail generation permanently failed (broken file,
        # missing ffmpeg, …). Without this, every rebind of that tile while
        # scrolling re-submits the same doomed decode. Cleared when the file's
        # row is rescanned (a successful regen replaces the entry on its own).
        self._local_thumb_failed: set[str] = set()
        self._local_thumb_lock = threading.Lock()
        self._local_thumb_pool: ThreadPoolExecutor | None = None

        # Pagination for large galleries
        self._page_size: int = 200  # Items per page
        self._current_offset: int = 0
        self._total_count: int = 0
        self._has_more_items: bool = False
        self._date_last_key: tuple[int, int] | None = None  # (year, month) of last date header
        self._lazy_loading_attached: bool = False
        self._lazy_loading_in_flight: bool = False
        # Guards the retry loop in _maybe_fill_viewport.
        self._fill_viewport_retries: int = 0
        # Sliding window: keep at most _MAX_LOADED_ITEMS items in memory
        # at once. When forward-loading pushes the total over the cap,
        # drop the oldest items from the front (aligned to a header
        # boundary so the visible structure stays consistent). This
        # bounds the performance cost of repeatedly jumping forward
        # through months — previously the row_store and current_items
        # grew without limit, and after a few thousand items the
        # ListView's layout/binding loops became visibly slow.
        self._MAX_LOADED_ITEMS: int = 1500
        # _window_start_offset = database offset of the first item
        # still loaded in current_items. Starts at 0 and only ever
        # increases (forward eviction); reverse re-fetch is not yet
        # implemented, so scrolling past the start gives no items.
        self._window_start_offset: int = 0

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
        # Watch the indexed folders so a picture written by anything else —
        # the system camera, a screenshot tool, a file manager copy, a sync
        # client — reaches the grid on its own. Started after the first render
        # so the initial scan isn't racing a watcher over the same files.
        self._watcher = MediaWatcher(self._on_disk_changed)
        self._start_watching()
        pil_warning = pillow_version_warning()
        if pil_warning:
            LOGGER.warning("%s", pil_warning)
            self._set_status(self._("Pillow is outdated — please update it."))
        if self._startup_warning:
            # _set_status needs the UI, so this waits until after _build_ui.
            if self._startup_warning == "settings":
                self._set_status(self._(
                    "Your settings could not be read and were reset to defaults."
                ))
            else:
                self._set_status(self._(
                    "The media index was damaged and is being rebuilt."
                ))
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

    def _is_mobile_width(self) -> bool:
        """Window narrower than 600px → mobile layout. Mirrors the
        Adw.Breakpoint condition we set up for the refresh icon. Used
        anywhere we need to honour the mobile-or-desktop split outside
        of breakpoint-driven setters (e.g. visibility resets in
        _exit_selection_mode). Falls back to True (= mobile) before
        the window has been realised, since we'd rather hide the icon
        than flash it on first paint on a phone."""
        width = self.get_width()
        if width <= 0:
            return True
        return width < 600

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
        elif isinstance(error, (ConnectionError, TimeoutError, OSError)):
            # Covers refused/reset (ConnectionError), socket timeout
            # (TimeoutError) and DNS/TLS/other socket faults (OSError). The two
            # OSError subclasses above are matched earlier, so this is the
            # catch-all transport branch.
            self._show_error_dialog(
                self._("Connection failed"),
                self._("Could not connect to Nextcloud. Check your internet connection and server URL."),
                str(error) if str(error) else "",
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
        # "auto" (the default) resolves per display: left rail on a desktop
        # screen, top bar on a phone. An explicit pick wins either way.
        nav_position = resolve_nav_position(
            getattr(self.settings, "nav_position", "auto"),
            desktop=display_is_desktop(),
        )
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


    # ------------------------------------------------------------------
    # Sort popover
    # ------------------------------------------------------------------

    # Internal sort_mode strings ↔ (mode-key, descending bool) tuples.
    # "none" is the ungrouped default; the two date modes add month headers.
    # They are split because a photo's two dates are genuinely different facts:
    # date_file is when the file last changed, date_taken is when the shutter
    # fired. Copying a shoot off a card gives every file today's date_file and
    # leaves date_taken where it belongs.
    _SORT_KEYS = ["none", "date_taken", "date_file", "folder", "name"]
    _SORT_TO_INTERNAL = {
        ("none",       True):  "newest",
        ("none",       False): "oldest",
        # Legacy keys: "date"/"date_asc" predate the split and mean the file
        # date, which is what they always sorted by. Settings written before
        # this keep working untouched.
        ("date_file",  True):  "date",
        ("date_file",  False): "date_asc",
        ("date_taken", True):  "date_taken",
        ("date_taken", False): "date_taken_asc",
        ("folder",     True):  "folder_desc",
        ("folder",     False): "folder",
        ("name",       True):  "name_desc",
        ("name",       False): "name",
    }
    _INTERNAL_TO_SORT = {v: k for k, v in _SORT_TO_INTERNAL.items()}

    def _sort_labels(self) -> list[str]:
        """Dropdown labels, in _SORT_KEYS order and already translated.

        Written as literals inside _() rather than translated from a list of
        msgids: xgettext only sees string constants, so `self._(labels[i])`
        would ship them untranslated — and the regression test that catches
        missing msgids inspects literals too, so nothing would flag it.
        """
        return [
            self._("None"),
            self._("Date (recorded)"),
            self._("Date (file)"),
            self._("Folder"),
            self._("Name"),
        ]

    def _build_sort_controls(self) -> Gtk.Box:
        # Label texts in the dropdown — matched 1:1 with self._SORT_KEYS.
        self._sort_dropdown_labels = self._sort_labels()
        store = Gtk.StringList()
        for label in self._sort_dropdown_labels:
            store.append(label)
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
                LOGGER.debug("sort_button.set_icon_name failed", exc_info=True)

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


    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def refresh(self, scan: bool = False, scope: str | None = None) -> None:
        """scope=None scans all local + NC; scope="current" scans only the active category."""
        if self._closing:
            return
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
        import time as _time
        _thread_start = _time.time()
        only_current = scope == "current"
        scope_label = f"Kategorie „{self.category}“" if only_current else "alle Kategorien"
        LOGGER.info("Scan-Start: %s", scope_label)
        # Touch NC for full scans, when NC is the active category, or when the
        # current Pictures view is configured to fold in Nextcloud entries —
        # but ONLY if the user has actively allowed NC for this session
        # (is_nc_active() respects manual disconnects too).
        # Guarded: these read live window state, and anything escaping here
        # would skip the finally block below — the one that re-enables the
        # refresh button. A stuck-disabled refresh is exactly the failure the
        # finally exists to prevent.
        try:
            need_nc = self.is_nc_active() and (
                (not only_current)
                or self.category == "nextcloud"
                or self._should_merge_nc()
            )
        except Exception:
            LOGGER.exception("Could not determine Nextcloud scan scope")
            need_nc = False
        # Track whether each phase actually changed the index, so we only
        # re-render the gallery when there's something new to show (an
        # unchanged startup scan no longer tears down + rebuilds the grid —
        # that unconditional rebuild was the visible "hackelig" stutter).
        local_changed = False
        nc_changed = False
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
                    # A configured account with no retrievable password: the
                    # keyring is locked, or the secret was removed behind the
                    # app's back. Warning, not info — this is exactly the state
                    # that shows a red badge, and at info level it never
                    # reached the log the user is asked to send in.
                    LOGGER.warning(
                        "Nextcloud: no app password could be read for %s@%s "
                        "(keyring locked or entry removed) — skipping sync",
                        self.settings.nextcloud_user, self.settings.nextcloud_url,
                    )
                    GLib.idle_add(
                        self._set_nc_broken, True,
                        self._("No Nextcloud password could be read"),
                    )
            elif need_nc:
                LOGGER.info("Nextcloud: keine URL/Benutzer konfiguriert, übersprungen")

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
                local_changed = self.scanner.scan(
                    local_cats,
                    excluded_subtrees=self.settings.excluded_subtrees(),
                )
            # Show local changes immediately — before the (potentially slow)
            # Nextcloud phase even starts — so the user sees local edits at once.
            if local_changed and not self._closing:
                GLib.idle_add(self.refresh, False)

            # Phase 2: NC structure scan (no thumbnails), strictly after local.
            if nc_client is not None:
                GLib.idle_add(self._set_nc_syncing, True)
                GLib.idle_add(self._set_nc_broken, False)
                try:
                    nc_changed = self.scanner.scan_nc_structure(
                        nc_client, self.settings.nextcloud_photos_path
                    )
                except Exception as nc_err:
                    # Connection/timeout/auth failure during the sync. React
                    # fast: flag broken, tell the user once, and trip the
                    # breaker so on-demand thumbnail fetches don't keep
                    # blocking ~20 s each against a server we know is down.
                    self._on_nc_sync_failed(nc_err)
                else:
                    # Sync went through → clear any stale broken state so a
                    # recovered connection re-enables thumbnail fetches.
                    if self._nc_unreachable:
                        self._nc_unreachable = False
                        GLib.idle_add(self._set_nc_broken, False)
                # No bulk thumbnail pre-fetch: tiles request their own thumbnail when
                # they scroll into view, which keeps the UI responsive on large folders.
        except Exception as e:
            LOGGER.exception("Media scan failed: %s", e)
            GLib.idle_add(
                self._set_nc_broken, True,
                self._("The media scan failed: %s") % type(e).__name__,
            )
        finally:
            nc_client = None
            GLib.idle_add(self._set_nc_syncing, False)
            # Re-render only if the NC phase added something new (local already
            # rendered above). Nothing changed → keep the view that
            # refresh(scan=True) rendered before the scan, no rebuild, no jank.
            if nc_changed and not self._closing:
                GLib.idle_add(self.refresh, False)
            GLib.idle_add(self._reenable_refresh_button)
            LOGGER.info("Scan abgeschlossen in %.1fs", _time.time() - _thread_start)
            # Trim cache after every scan: thumbnail generation may have grown
            # the disk footprint past the user's configured budget.
            self.evict_cache_async()

    def _set_nc_syncing(self, active: bool) -> None:
        if self._closing:
            return
        if self._nc_spinner is not None:
            self._nc_spinner.set_visible(active)
            if active:
                self._nc_spinner.start()
            else:
                self._nc_spinner.stop()

    def _set_nc_broken(self, active: bool, reason: str = "") -> None:
        if self._closing:
            return
        # Remembered even when the badge widget is not built yet (a narrow
        # window hides it), so the diagnostics report can always answer "why is
        # Nextcloud red" — a tooltip is no use to someone pasting a log into a
        # bug report, and on a phone it is not reachable at all.
        if active:
            self._nc_broken_reason = reason or self._("Unknown reason")
            self._nc_broken_since = datetime.now().isoformat(timespec="seconds")
        else:
            self._nc_broken_reason = ""
            self._nc_broken_since = ""
        if self._nc_broken_img is not None:
            self._nc_broken_img.set_visible(active)
            # A hovered tooltip explains *why* the connection is marked broken,
            # so the red badge isn't a mystery. Cleared when the badge hides.
            self._nc_broken_img.set_tooltip_text(reason if active and reason else None)

    def _reenable_refresh_button(self) -> bool:
        if not self._closing:
            self.refresh_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Live filesystem watching
    # ------------------------------------------------------------------

    def _start_watching(self) -> None:
        """(Re)point the watcher at the folders that are actually indexed.

        Called again after a settings change, so a root the user just removed
        stops firing and one they just added starts.
        """
        watcher = getattr(self, "_watcher", None)
        if watcher is None:
            return
        roots = [
            Path(path).expanduser()
            for category, _label, path in self.settings.categories()
            if category not in ("nextcloud", "pictures") and path
        ]
        try:
            watcher.watch(roots)
        except Exception:
            # A library too deep for the inotify quota, a root that vanished
            # mid-setup — none of it is worth losing the window over. Without
            # a watcher the gallery is exactly as up-to-date as it was before
            # one existed: refresh by hand.
            LOGGER.exception("Could not watch the media folders")

    def _stop_watching(self) -> None:
        watcher = getattr(self, "_watcher", None)
        if watcher is not None:
            watcher.stop()

    def _on_disk_changed(self, paths: set[str]) -> None:
        """The watcher reports files that changed under the indexed roots.

        Delivered on the main loop (that is where Gio dispatches monitor
        events), so the indexing — a thumbnail decode and an EXIF read per
        file — is handed to a worker and only the re-render comes back.
        """
        if self._closing or not paths:
            return
        threading.Thread(
            target=self._index_paths, args=(sorted(paths),), daemon=True,
        ).start()

    def _index_paths(self, paths: list[str]) -> None:
        """Worker: index each path that exists, forget each one that doesn't."""
        categories = self.settings.categories()
        excluded = self.settings.excluded_subtrees()
        changed = False
        try:
            for raw in paths:
                if self._closing:
                    return
                path = Path(raw)
                try:
                    if path.exists():
                        changed |= self.scanner.index_file(path, categories, excluded)
                    else:
                        changed |= self.scanner.forget_file(path)
                except Exception:
                    LOGGER.exception("Could not index %s", path)
            if changed and not self._closing:
                GLib.idle_add(self.refresh, False)
        finally:
            # This thread lives for one batch, and reading the index opened a
            # connection with a 16 MB page cache behind it. Long-lived threads
            # keep theirs; a per-batch one has to hand it back or a busy day
            # of photo-taking leaks one file descriptor per capture.
            self.database.close_thread_connection()

    def _on_camera_captured(self, path: Path) -> None:
        """A shot from the built-in camera just hit disk.

        Indexed directly instead of through a scoped rescan: the rescan this
        used to trigger only covered the category being *viewed*, so a photo
        taken while Videos or Nextcloud was open never reached the index at
        all, and even on Overview the picture only appeared after a walk of
        the whole library. The watcher would catch this file too — this path
        just gets it on screen at once, and still works when the library is
        too deep to watch.
        """
        if self._closing:
            return
        threading.Thread(
            target=self._index_paths, args=([str(path)],), daemon=True,
        ).start()

    def _nc_error_reason(self, error: Exception) -> str:
        """A short, human tooltip for the broken badge."""
        if isinstance(error, PermissionError):
            return self._("Nextcloud authentication failed")
        if isinstance(error, FileNotFoundError):
            return self._("Nextcloud path not found")
        return self._("Could not connect to Nextcloud")

    def _on_nc_sync_failed(self, error: Exception) -> None:
        """Worker-thread entry point for a failed NC network op.

        Trips the circuit breaker, stops queued thumbnail fetches against the
        dead server, marks the badge broken, and surfaces the error to the user
        exactly once per broken episode (so repeated retries don't spam dialogs).
        """
        first_failure = not self._nc_unreachable
        # Logged here rather than at each call site: this is the one funnel
        # every network failure passes through, and the exception type plus the
        # traceback are what tell a timeout apart from a rejected password.
        # exc_info=error, not True: this is also called from the thumbnail
        # worker outside any except block, where exc_info=True would log a bare
        # "NoneType: None" instead of the traceback.
        LOGGER.warning(
            "Nextcloud unreachable — %s: %s", type(error).__name__, error,
            exc_info=error,
        )
        self._nc_unreachable = True
        # Drop anything still queued so workers stop blocking on the dead server.
        self._cancel_nc_thumb_queue()
        GLib.idle_add(self._set_nc_broken, True, self._nc_error_reason(error))
        if first_failure:
            GLib.idle_add(self._handle_nextcloud_error, error)

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


    # ------------------------------------------------------------------
    # Item actions
    # ------------------------------------------------------------------

    def _category_root(self, category: str) -> str | None:
        """Filesystem path or NC photos_path that backs *category*, or None."""
        for cat, _label, path in self.settings.categories():
            if cat == category:
                return path
        return None

    def _on_mcp_change(self) -> None:
        """An MCP write tool changed something on disk — re-render.

        Called on the MCP server's worker thread, so the refresh is bounced to
        the main loop: touching the widget tree from another thread is what GTK
        crashes on. Skipped once the window is tearing down, like every other
        deferred callback here.
        """
        def _refresh() -> bool:
            if not self._closing:
                self.refresh(scan=False)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_refresh)

    def _sync_new_folder_button(self) -> None:
        """Offer "New folder" only where a new folder could actually land.

        Overview is a virtual aggregator over the other categories — it has no
        directory of its own (its path slot carries a legacy value that is never
        scanned, see Settings.categories), so a folder created "in" Overview
        would sit somewhere the view never shows.

        Insensitive rather than hidden: the header keeps its shape as the user
        switches tabs, and the tooltip says why the button is dimmed instead of
        leaving them to guess.
        """
        can_create = self.category != "pictures"
        self.new_folder_button.set_sensitive(can_create)
        self.new_folder_button.set_tooltip_text(
            self._("New folder") if can_create
            else self._("Overview combines your other folders — open one to create a folder in it")
        )

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
        idle_once(entry.grab_focus)

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
        if self._closing:
            return GLib.SOURCE_REMOVE
        if not ok:
            self._show_error_dialog(
                self._("Could not create folder"),
                self._("The Nextcloud server rejected the new folder %s.") % name,
            )
        else:
            self.refresh(scan=True, scope="current")
        return GLib.SOURCE_REMOVE

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
        # Fallback for the rare case where the grid has no cached page (e.g. a
        # tile clicked while a refresh is mid-flight). Bounded to the same
        # window the gallery itself keeps in memory: the unbounded list_media
        # this replaces pulled every row in the category — ~150 ms on a 50k
        # library on a desktop, and this runs on the main loop, so on a phone
        # it was a visible freeze between tap and viewer.
        items = self.current_items or self.database.list_media_paginated(
            item.category, self.settings.get_sort_mode(item.category, self.current_folder),
            self.current_folder,
            limit=self._MAX_LOADED_ITEMS, offset=0,
            media_filter=self.settings.media_filter_for(item.category),
        )
        # Match by path — frozen MediaItem __eq__ compares all fields, and thumb_path
        # may differ between the cached current_items and the clicked tile (async thumb update).
        index = next((i for i, it in enumerate(items) if it.path == item.path), -1)
        if index < 0:
            items = [item]
            index = 0
        ViewerWindow(self, items, index, self.settings.external_video_player).present()


    # ------------------------------------------------------------------
    # Multi-select
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Navigation handlers
    # ------------------------------------------------------------------

    def _on_category_toggled(self, button: Gtk.ToggleButton, category: str) -> None:
        if not button.get_active():
            # Tapping the category you are already in. A ToggleButton reports
            # that as being switched *off*, but the tab must stay lit — there
            # is no such thing as "no category selected" here.
            if category != self.category:
                # Some other tab took over; that path re-activates the new one.
                return
            button.handler_block_by_func(self._on_category_toggled)
            button.set_active(True)
            button.handler_unblock_by_func(self._on_category_toggled)
            self._cancel_nc_thumb_queue()
            if self.current_folder is not None:
                # Drilled into a subfolder — the tap means "back to the top".
                self.current_folder = None
                self._render()
            else:
                # Already at the top of this category, so the tap can only mean
                # "load it again". Previously nothing happened at all, which
                # made the tab look unresponsive.
                self.refresh(scan=True, scope="current")
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
            on_captured=self._on_camera_captured,
            handedness=self.settings.handedness,
            settings=self.settings,
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
                "When you delete photos in Muga, they are moved to trash. "
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

    @staticmethod
    def _scan_signature(s: Settings) -> tuple:
        """The settings fields that change *which files get indexed* — folder
        roots and the Nextcloud connection. A settings change that leaves this
        untouched (theme, grid columns, language, sort, cache budget, …) needs
        only a re-render, never a fresh disk + network scan."""
        return (
            s.photos_dir, s.pictures_dir, s.videos_dir, s.screenshots_dir,
            tuple(s.extra_locations), tuple(s.extra_location_no_inherit),
            s.nextcloud_url, s.nextcloud_user, s.nextcloud_photos_path,
            s.nextcloud_enabled, s.nextcloud_thumbnail_only,
            s.nextcloud_show_in_pictures,
            getattr(s, "nextcloud_session_active", True),
        )

    def apply_settings(self, settings: Settings) -> None:
        self._selection_mode = False
        self._selected_paths.clear()
        # Decide BEFORE overwriting self.settings whether the change actually
        # requires a rescan — most settings tweaks (theme, columns, language)
        # don't, and rescanning the whole library + Nextcloud on every one of
        # them was the dominant settings-interaction latency.
        self._settings_needs_scan = (
            self._scan_signature(self.settings) != self._scan_signature(settings)
        )
        self.settings = settings
        self.settings.save()
        self.translator.language = settings.language
        # settings is a fresh object, so the server's reference to the old one
        # is now stale — resync rather than leaving its tools reading a copy
        # that no longer reflects what the user just changed.
        try:
            mcp_server.sync_with_settings(
                self.settings, self.database, on_change=self._on_mcp_change,
            )
        except Exception:
            LOGGER.exception("Could not apply MCP settings")
        # Invalidate the shared NC client — credentials/URL may have changed.
        old_client = self._nc_thumb_shared_client
        self._nc_thumb_shared_client = None
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                LOGGER.debug("old_client.close failed", exc_info=True)
        # Resync the runtime gate with the persisted preferences. Settings is
        # the source of truth here — anything else would re-enable NC behind
        # the user's back when applying settings after a manual disconnect.
        self._nc_session_active = bool(
            self.settings.nextcloud_enabled
            and getattr(self.settings, "nextcloud_session_active", True)
        )
        # Credentials/URL or the session gate may have changed (e.g. a fresh
        # Connect) — give the breaker a clean slate so the next sync can prove
        # the connection works again and re-enable thumbnail fetches.
        self._nc_unreachable = False
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
        # Compare resolved positions, not stored ones: "auto" that already
        # resolves to the current side is not a layout change and must not
        # cost the user a window recreation.
        new_position = resolve_nav_position(
            getattr(self.settings, "nav_position", "auto"),
            desktop=display_is_desktop(),
        )
        old_position = getattr(self, "_nav_position", "top")
        if old_position != new_position:
            self._recreate_window_for_layout_change()
            return GLib.SOURCE_REMOVE
        # Lighter changes (theme, grid columns, cache budget, NC flags …) just
        # rebuild the toolbar tree in place — no topology change, no deadlock.
        # The disk/network scan only runs when the indexed file set actually
        # changed; otherwise it's a pure re-render (picks up columns/sort/etc).
        self._build_ui()
        self.refresh(scan=getattr(self, "_settings_needs_scan", True))
        # The configured roots may have moved with that change; re-point the
        # watcher so it follows the folders the scanner now indexes.
        self._start_watching()
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
        # From here on this window is being replaced. Flip the guard before we
        # spin up the replacement so any in-flight worker callback that lands
        # during the swap bails instead of mutating our dying widget tree.
        self._closing = True
        # Release the directory monitors with the window they belong to — the
        # replacement builds its own, and a cancelled monitor cannot deliver
        # an event into a widget tree that is about to be destroyed.
        self._stop_watching()
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
                LOGGER.debug("dialog.destroy failed", exc_info=True)
        # Destroy after present + dialog cleanup so the app has a window at
        # all times — Adw quits the main loop when the last window goes
        # away, which would take the new window down with it on certain WMs.
        self.destroy()

    # ------------------------------------------------------------------
    # CSS / theme
    # ------------------------------------------------------------------


    # ──────────────────────────────────────────────────────────────────
    # Pull-to-refresh (drag gesture with rubber-band wobble)
    # ──────────────────────────────────────────────────────────────────

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

    # Before anything reads the per-user directories: pick up the data the
    # app left behind under its previous name.
    migrate_legacy_dirs()

    app = GalleryApplication()
    return app.run()
