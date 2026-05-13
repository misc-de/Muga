from __future__ import annotations

import os
import platform
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .camera_torch import TORCH_SYSFS_PATHS
from .config import CACHE_DIR, CONFIG_DIR, DATA_DIR, DB_PATH, DEBUG_LOG_PATH, Settings

class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, parent: GalleryWindow, initial_page: str | None = None) -> None:
        super().__init__(transient_for=parent, modal=True, title=parent._("Settings"))
        self.set_search_enabled(False)
        self.parent_window = parent
        self.settings = Settings(**parent.settings.__dict__)
        self._build()
        if initial_page:
            # Switch to the named page (e.g. after a nav-position change
            # recreates the window and reopens settings to where the user was).
            # Falls back to the default first page if the name doesn't exist —
            # set_visible_page_name is forgiving with unknown names.
            try:
                self.set_visible_page_name(initial_page)
            except Exception:
                pass
        # Suppress GTK's default "focus the first focusable widget" so opening
        # settings doesn't pop up the on-screen keyboard on a SpinRow / Entry.
        GLib.idle_add(lambda: (self.set_focus(None), GLib.SOURCE_REMOVE)[1])

    def _(self, text: str) -> str:
        return self.parent_window._(text)

    def _build(self) -> None:
        media = Adw.PreferencesPage(title=self._("Folders"), icon_name="folder-pictures-symbolic")
        # Stable name (independent of the translated title) so callers can
        # jump to a specific page via set_visible_page_name().
        media.set_name("folders")
        self.add(media)
        group = Adw.PreferencesGroup(title=self._("Folders"))
        media.add(group)

        # Built-in folder specs (the same key used in Settings.categories()).
        # Built-in folders use a filesystem chooser; "nextcloud" uses an inline
        # edit dialog because the path is a server-side string, not a local dir.
        # Titles are intentionally without the word "folder" — keeps them
        # consistent with the category labels used in the gallery nav.
        self._media_folder_specs: dict[str, dict] = {
            "pictures":    {"attr": "pictures_dir",    "title": "Overview",    "kind": "local"},
            "photos":      {"attr": "photos_dir",      "title": "Photos",      "kind": "local"},
            "videos":      {"attr": "videos_dir",      "title": "Videos",      "kind": "local"},
            "screenshots": {"attr": "screenshots_dir", "title": "Screenshots", "kind": "local"},
            "nextcloud":   {"attr": "nextcloud_photos_path",
                            "title": "Nextcloud", "kind": "nextcloud"},
        }
        self._media_listbox = Gtk.ListBox()
        self._media_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._media_listbox.add_css_class("boxed-list")
        group.add(self._media_listbox)
        self._populate_media_listbox()

        # "Add location" button replaces the separate "Optional locations"
        # group — extras now live in the same listbox as everything else.
        add_btn = Gtk.Button(
            label=self._("Add location"), icon_name="list-add-symbolic",
        )
        add_btn.set_halign(Gtk.Align.START)
        add_btn.set_margin_top(8)
        add_btn.connect("clicked", self._add_location)
        group.add(add_btn)

        app = Adw.PreferencesPage(title=self._("Appearance"), icon_name="preferences-desktop-appearance-symbolic")
        app.set_name("appearance")
        self.add(app)
        theme_group = Adw.PreferencesGroup(title=self._("Appearance"))
        app.add(theme_group)
        theme_group.add(self._combo_row("theme", "Theme", [("system", "System"), ("light", "Light"), ("dark", "Dark")]))
        theme_group.add(self._combo_row("language", "Language", [("system", "Use system language"), ("en", "English"), ("de", "German")]))

        grid_group = Adw.PreferencesGroup(title=self._("Grid"))
        app.add(grid_group)
        columns = Adw.SpinRow.new_with_range(2, 10, 1)
        columns.set_title(self._("Photos per row"))
        columns.set_value(self.settings.grid_columns)
        columns.connect("notify::value", self._columns_changed)
        grid_group.add(columns)

        nav_group = Adw.PreferencesGroup(
            title=self._("Navigation"),
            description=self._("Where the category bar is shown around the gallery."),
        )
        app.add(nav_group)
        nav_group.add(self._combo_row(
            "nav_position", "Position",
            [
                ("top",    "Top"),
                ("right",  "Right"),
                ("bottom", "Bottom"),
                ("left",   "Left"),
            ],
        ))

        handedness_group = Adw.PreferencesGroup(
            title=self._("Handedness"),
            description=self._(
                "Which side thumb-reachable camera controls sit on."
            ),
        )
        app.add(handedness_group)
        handedness_group.add(self._combo_row(
            "handedness", "Camera buttons",
            [
                ("right",   "Right-handed"),
                ("left",    "Left-handed"),
                ("neutral", "Neutral (centred)"),
            ],
        ))

        video_group = Adw.PreferencesGroup(
            title=self._("Video"),
            description=self._("Leave empty to use built-in playback"),
        )
        app.add(video_group)
        command = Adw.EntryRow(title=self._("External player command"))
        command.set_text(self.settings.external_video_player)
        command.set_show_apply_button(True)
        command.connect("apply", self._entry_apply, "external_video_player")
        video_group.add(command)

        cache_group = Adw.PreferencesGroup(
            title=self._("Cache"),
            description=self._("Disk cache for thumbnails and downloaded Nextcloud files. When the limit is reached, the least-recently-used files are deleted first."),
        )
        app.add(cache_group)

        cache_size_row = Adw.SpinRow.new_with_range(0, 200000, 100)
        cache_size_row.set_title(self._("Maximum cache size (MB)"))
        cache_size_row.set_subtitle(self._("0 = unlimited"))
        cache_size_row.set_value(self.settings.cache_max_mb)
        cache_size_row.connect("notify::value", self._cache_max_mb_changed)
        cache_group.add(cache_size_row)

        self._cache_size_row = Adw.ActionRow(title=self._("Current cache size"))
        clear_btn = Gtk.Button(label=self._("Clear cache"))
        clear_btn.add_css_class("destructive-action")
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_btn.connect("clicked", self._on_clear_cache_clicked)
        self._cache_size_row.add_suffix(clear_btn)
        cache_group.add(self._cache_size_row)
        self._refresh_cache_size_display()

        self._build_nextcloud_page()
        self._build_diagnostics_page()

    def _build_diagnostics_page(self) -> None:
        page = Adw.PreferencesPage(
            title=self._("Diagnostics"),
            icon_name="dialog-information-symbolic",
        )
        page.set_name("diagnostics")
        self.add(page)

        group = Adw.PreferencesGroup(
            title=self._("Diagnostics"),
            description=self._(
                "Copy this when reporting camera, Nextcloud, or media-scan issues."
            ),
        )
        page.add(group)

        copy_row = Adw.ActionRow(
            title=self._("Diagnostic report"),
            subtitle=self._("Includes paths, runtime versions, camera plugins, and status flags."),
        )
        copy_btn = Gtk.Button(label=self._("Copy"))
        copy_btn.set_valign(Gtk.Align.CENTER)
        copy_btn.connect("clicked", self._copy_diagnostics)
        copy_row.add_suffix(copy_btn)
        group.add(copy_row)

        self._diagnostics_view = Gtk.TextView()
        self._diagnostics_view.set_editable(False)
        self._diagnostics_view.set_cursor_visible(False)
        self._diagnostics_view.set_monospace(True)
        self._diagnostics_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._diagnostics_view.set_top_margin(10)
        self._diagnostics_view.set_bottom_margin(10)
        self._diagnostics_view.set_left_margin(10)
        self._diagnostics_view.set_right_margin(10)
        self._diagnostics_view.get_buffer().set_text(self._diagnostics_text())

        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(260)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._diagnostics_view)
        group.add(scroller)

    def _copy_diagnostics(self, btn: Gtk.Button) -> None:
        text = self._diagnostics_text()
        self._diagnostics_view.get_buffer().set_text(text)
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)
        btn.set_label(self._("Copied"))
        GLib.timeout_add_seconds(2, lambda: (btn.set_label(self._("Copy")), False)[1])

    def _diagnostics_text(self) -> str:
        gst_info = self._gst_diagnostics()
        s = self.settings
        parent = self.parent_window
        lines = [
            "Yaga diagnostics",
            "================",
            f"Python: {sys.version.split()[0]}",
            f"Platform: {platform.platform()}",
            f"Executable: {sys.executable}",
            f"PID: {os.getpid()}",
            "",
            "Paths",
            "-----",
            f"Config: {CONFIG_DIR}",
            f"Cache: {CACHE_DIR}",
            f"Data: {DATA_DIR}",
            f"Database: {DB_PATH} ({'exists' if DB_PATH.exists() else 'missing'})",
            f"Debug log: {DEBUG_LOG_PATH} ({'exists' if DEBUG_LOG_PATH.exists() else 'missing'})",
            "",
            "Media folders",
            "-------------",
            f"Overview: hidden={s.pictures_hidden}, filter={s.pictures_media_filter}",
            f"Photos: {s.photos_dir}",
            f"Pictures legacy path: {s.pictures_dir}",
            f"Videos: {s.videos_dir}",
            f"Screenshots: {s.screenshots_dir}",
            f"Extra locations: {len(s.extra_locations)}",
            "",
            "Nextcloud",
            "---------",
            f"Enabled: {s.nextcloud_enabled}",
            f"Session active: {getattr(parent, '_nc_session_active', False)}",
            f"URL set: {bool(s.nextcloud_url)}",
            f"User set: {bool(s.nextcloud_user)}",
            f"Photos path: {s.nextcloud_photos_path}",
            f"Thumbnail-only scan: {s.nextcloud_thumbnail_only}",
            "",
            "Camera settings",
            "---------------",
            f"Handedness: {s.handedness}",
            f"JPEG quality: {s.camera_jpeg_quality}",
            f"Image resolution: {s.camera_image_resolution or 'native/default'}",
            f"Video quality preset: {s.camera_video_bitrate_kbps} kbps",
            f"Geotagging enabled: {s.camera_geo_enabled}",
            f"Flash/video-light enabled: {s.camera_flash_enabled}",
            "",
            "GStreamer",
            "---------",
            *gst_info,
            "",
            "Torch sysfs",
            "-----------",
        ]
        for path in TORCH_SYSFS_PATHS:
            p = Path(path)
            writable = os.access(path, os.W_OK)
            lines.append(f"{path}: {'exists' if p.exists() else 'missing'}, writable={writable}")
        return "\n".join(lines) + "\n"

    def _gst_diagnostics(self) -> list[str]:
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst  # type: ignore
            Gst.init(None)
        except Exception as exc:
            return [f"Unavailable: {exc}"]
        factories = (
            "droidcamsrc", "gtk4paintablesink", "v4l2src", "pipewiresrc",
            "autovideosrc", "appsink", "jpegenc", "jpegdec", "matroskamux",
            "filesink", "zxing",
        )
        out = [f"Version: {Gst.version_string()}"]
        for name in factories:
            out.append(f"{name}: {bool(Gst.ElementFactory.find(name))}")
        return out

    def _build_nextcloud_page(self) -> None:
        page = Adw.PreferencesPage(title="Nextcloud", icon_name="folder-remote-symbolic")
        page.set_name("nextcloud")
        self.add(page)

        # Track whether the user explicitly chose "manual" in the setup dialog —
        # before that, the credentials fields stay hidden so the page just shows
        # the Setup button.
        self._nc_manual_setup_unlocked = False
        # Runtime connection state — mirrors the parent window's session gate
        # (which already accounts for the persistent disconnect flag). Reading
        # from the parent ensures we see the same state across restarts: a
        # saved Disconnect comes up showing "Disconnected" + green Connect btn.
        self._nc_runtime_connected = bool(self.parent_window._nc_session_active)

        # ── Top: Active toggle + Setup button (when not yet configured) ──
        self._nc_top_group = Adw.PreferencesGroup()
        page.add(self._nc_top_group)

        self._nc_active_row = Adw.SwitchRow(
            title=self._("Nextcloud active"),
            subtitle=self._("Enables or disables all Nextcloud functions"),
        )
        self._nc_active_row.set_active(self.settings.nextcloud_enabled)
        self._nc_active_handler = self._nc_active_row.connect(
            "notify::active", self._nc_active_changed,
        )
        self._nc_top_group.add(self._nc_active_row)

        self._nc_setup_row = Adw.ActionRow(
            title=self._("Set up connection"),
            subtitle=self._("Connect to your Nextcloud"),
        )
        setup_btn = Gtk.Button(label=self._("Set up"))
        setup_btn.add_css_class("suggested-action")
        setup_btn.set_valign(Gtk.Align.CENTER)
        setup_btn.connect("clicked", self._nc_show_setup_dialog)
        self._nc_setup_row.add_suffix(setup_btn)
        self._nc_top_group.add(self._nc_setup_row)

        # ── Credentials (only visible after setup or already configured) ──
        self._nc_creds_group = Adw.PreferencesGroup(title=self._("Credentials"))
        page.add(self._nc_creds_group)

        # Status row at the top of credentials with Connect/Disconnect button.
        self._nc_status_row = Adw.ActionRow()
        self._nc_status_icon: Gtk.Image | None = None

        self._nc_connect_btn = Gtk.Button()
        self._nc_connect_btn.set_child(self._make_icon_label("network-transmit-receive-symbolic", self._("Connect")))
        self._nc_connect_btn.add_css_class("suggested-action")
        self._nc_connect_btn.set_valign(Gtk.Align.CENTER)
        self._nc_connect_btn.connect("clicked", self._nc_connect)
        self._nc_status_row.add_suffix(self._nc_connect_btn)

        self._nc_disconnect_btn = Gtk.Button()
        self._nc_disconnect_btn.set_child(self._make_icon_label("network-offline-symbolic", self._("Disconnect")))
        self._nc_disconnect_btn.add_css_class("destructive-action")
        self._nc_disconnect_btn.set_valign(Gtk.Align.CENTER)
        self._nc_disconnect_btn.connect("clicked", self._nc_disconnect)
        self._nc_status_row.add_suffix(self._nc_disconnect_btn)
        self._nc_creds_group.add(self._nc_status_row)

        self._nc_url_row = Adw.EntryRow(title=self._("Server URL"))
        self._nc_url_row.set_text(self.settings.nextcloud_url)
        self._nc_url_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        self._nc_creds_group.add(self._nc_url_row)

        self._nc_user_row = Adw.EntryRow(title=self._("Username"))
        self._nc_user_row.set_text(self.settings.nextcloud_user)
        self._nc_user_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        self._nc_creds_group.add(self._nc_user_row)

        self._nc_pass_row = Adw.PasswordEntryRow(title=self._("App password"))
        self._nc_pass_row.set_text(self.settings.load_app_password())
        qr_btn = Gtk.Button.new_from_icon_name("camera-photo-symbolic")
        qr_btn.set_tooltip_text(self._("Scan QR code"))
        qr_btn.add_css_class("flat")
        qr_btn.set_valign(Gtk.Align.CENTER)
        qr_btn.connect("clicked", self._nc_scan_qr)
        self._nc_pass_row.add_suffix(qr_btn)
        self._nc_creds_group.add(self._nc_pass_row)

        # NB: the Photos folder path lives on the Media folders page now —
        # editable inline there, draggable for ordering.

        # ── Performance ──
        self._nc_perf_group = Adw.PreferencesGroup(title=self._("Performance"))
        page.add(self._nc_perf_group)

        thumb_row = Adw.SwitchRow(
            title=self._("Load thumbnails only"),
            subtitle=self._("Skip downloading full files during sync"),
        )
        thumb_row.set_active(self.settings.nextcloud_thumbnail_only)
        thumb_row.connect("notify::active", self._nc_thumb_only_changed)
        self._nc_perf_group.add(thumb_row)

        merge_row = Adw.SwitchRow(
            title=self._("Show in Overview"),
            subtitle=self._("Merge Nextcloud items into the Overview (thumbnails load on demand)"),
        )
        merge_row.set_active(self.settings.nextcloud_show_in_pictures)
        merge_row.connect("notify::active", self._nc_show_in_pictures_changed)
        self._nc_perf_group.add(merge_row)

        self._nc_refresh_status()
        self._nc_refresh_layout()

    def _nc_is_configured(self) -> bool:
        """True when we have at least URL+user on file (whether or not the
        connection is currently active)."""
        return bool(
            self.settings.nextcloud_url.strip()
            and self.settings.nextcloud_user.strip()
        )

    def _nc_refresh_layout(self) -> None:
        """Show the right page chrome for the current configuration state.
        - Not configured + manual not unlocked: only Setup button visible.
        - Configured (or manual unlocked): full credential + perf groups."""
        configured = self._nc_is_configured() or self._nc_manual_setup_unlocked
        self._nc_setup_row.set_visible(not configured)
        self._nc_creds_group.set_visible(configured)
        self._nc_perf_group.set_visible(configured)
        # The aktiv-toggle is meaningless until something is configured.
        self._nc_active_row.set_sensitive(self._nc_is_configured())

    def _nc_show_setup_dialog(self, _btn: Gtk.Button) -> None:
        dialog = Adw.AlertDialog(
            heading=self._("Set up connection"),
            body=self._(
                "How would you like to connect to your Nextcloud?\n\n"
                "You can find the app-password QR code in your Nextcloud under:\n"
                "Settings → Security → App passwords → \"Create new app password\"."
            ),
        )
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("manual", self._("Manually"))
        dialog.add_response("qr", self._("Scan QR code"))
        dialog.set_default_response("qr")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("qr", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._nc_setup_dialog_response)
        dialog.present(self)

    def _nc_setup_dialog_response(self, _dialog, response: str) -> None:
        if response == "qr":
            self._nc_manual_setup_unlocked = True
            self._nc_refresh_layout()
            self._nc_scan_qr(None)
        elif response == "manual":
            self._nc_manual_setup_unlocked = True
            self._nc_refresh_layout()
        # cancel → nothing happens

    def _nc_active_changed(self, row: Adw.SwitchRow, _param) -> None:
        active = row.get_active()
        if active and not self._nc_is_configured():
            # Can't enable without credentials; bounce back and prompt setup.
            row.handler_block(self._nc_active_handler)
            row.set_active(False)
            row.handler_unblock(self._nc_active_handler)
            self._nc_show_setup_dialog(None)
            return
        self.settings.nextcloud_enabled = active
        # Toggling the master "aktiv" switch always resyncs the
        # session-active flag — the user's intent is "on=fully on".
        self.settings.nextcloud_session_active = active
        self.settings.save()
        self.parent_window.settings.nextcloud_enabled = active
        self.parent_window.settings.nextcloud_session_active = active
        self.parent_window.settings.save()
        # Runtime mirrors the toggle. When deactivating, also drop the shared
        # client so no scripted call can sneak through with stale credentials.
        self._nc_runtime_connected = active
        self.parent_window._nc_session_active = active
        if not active:
            old_client = self.parent_window._nc_thumb_shared_client
            self.parent_window._nc_thumb_shared_client = None
            if old_client is not None:
                try:
                    old_client.close()
                except Exception:
                    pass
        self._nc_refresh_status()
        # Add or remove the Nextcloud entry from the gallery's category nav.
        self.parent_window._rebuild_categories()
        # Same for the Settings → Folders listbox: the NC row must appear/
        # disappear in lock-step with the toggle.
        self._populate_media_listbox()
        # Force a re-render so the merged-Pictures view picks up the change too.
        self.parent_window.refresh(scan=False)

    @staticmethod
    def _make_icon_label(icon_name: str, label: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(Gtk.Image.new_from_icon_name(icon_name))
        box.append(Gtk.Label(label=label))
        return box

    def _nc_update_buttons(self) -> None:
        # Buttons reflect *runtime* connection state so a manual disconnect
        # immediately surfaces a green "Connect" button without touching the
        # persistent "Nextcloud aktiv" toggle.
        connected = self._nc_runtime_connected
        self._nc_connect_btn.set_visible(not connected)
        self._nc_disconnect_btn.set_visible(connected)

    def _nc_set_status(self, text: str, ok: bool = True) -> None:
        # No prefix/suffix icons — status is conveyed by the colour of the
        # text alone (green for connected, red otherwise).
        if self._nc_status_icon is not None:
            try:
                self._nc_status_row.remove(self._nc_status_icon)
            except Exception:
                pass
            self._nc_status_icon = None
        color = "#2ec27e" if ok else "#e01b24"
        try:
            self._nc_status_row.set_title_use_markup(True)
        except AttributeError:
            try:
                self._nc_status_row.set_property("title-use-markup", True)
            except Exception:
                pass
        safe = GLib.markup_escape_text(text)
        self._nc_status_row.set_title(f"<span color='{color}' weight='600'>{safe}</span>")

    def _nc_refresh_status(self, sync_toggle: bool = True) -> None:
        """Sync status row + buttons. The QR-code tip is intentionally
        suppressed here — it only shows up in the initial setup dialog.
        The 'Nextcloud active' toggle is only synced when sync_toggle=True
        (e.g. on connect, but NOT on a manual disconnect)."""
        self._nc_update_buttons()
        if sync_toggle and hasattr(self, "_nc_active_row") \
                and self._nc_active_row.get_active() != self.settings.nextcloud_enabled:
            self._nc_active_row.handler_block(self._nc_active_handler)
            self._nc_active_row.set_active(self.settings.nextcloud_enabled)
            self._nc_active_row.handler_unblock(self._nc_active_handler)
        # Group description is always empty — the QR tip lives in the setup dialog.
        self._nc_creds_group.set_description("")
        if self._nc_runtime_connected:
            self._nc_set_status(self._("Connected"), ok=True)
        else:
            self._nc_set_status(self._("Disconnected"), ok=False)

    def _nc_connect(self, _btn: Gtk.Button) -> None:
        url  = self._nc_url_row.get_text().strip()
        user = self._nc_user_row.get_text().strip()
        pwd  = self._nc_pass_row.get_text()

        if not url or not user or not pwd:
            self._nc_set_status(self._("Please fill in all fields."), ok=False)
            return

        url = self.settings._normalize_url(url)
        self._nc_url_row.set_text(url)

        # Cleartext-only connection: _normalize_url defaults to https://, so
        # http:// here means the user explicitly typed it. Warn + require
        # confirmation before sending the password in the clear.
        if url.startswith("http://"):
            self._nc_warn_cleartext_then_connect(url, user, pwd)
            return

        self._nc_proceed_connect(url, user, pwd)

    def _nc_warn_cleartext_then_connect(self, url: str, user: str, pwd: str) -> None:
        dialog = Adw.AlertDialog(
            heading=self._("Unencrypted connection"),
            body=self._(
                "The server URL starts with http:// — your password and "
                "photos will be transmitted unencrypted. Anyone on the same "
                "network can read them. Use https:// unless you have a "
                "specific reason not to."
            ),
        )
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("connect_anyway", self._("Connect anyway"))
        dialog.set_response_appearance(
            "connect_anyway", Adw.ResponseAppearance.DESTRUCTIVE,
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d, response: str) -> None:
            if response == "connect_anyway":
                self._nc_proceed_connect(url, user, pwd)
            else:
                self._nc_set_status(self._("Cancelled"), ok=False)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _nc_proceed_connect(self, url: str, user: str, pwd: str) -> None:
        # Read the Photos path from settings (edited via the media-folders page).
        path = (self.settings.nextcloud_photos_path or "").strip() or "Photos"

        account_changed = (url != self.settings.nextcloud_url
                           or user != self.settings.nextcloud_user)
        if account_changed:
            self.parent_window.database.clear_category("nextcloud")

        self.settings.nextcloud_url         = url
        self.settings.nextcloud_user        = user
        self.settings.nextcloud_photos_path = path
        self.settings.nextcloud_enabled     = False   # only True after successful test
        self.settings.save()
        self.settings.save_app_password(pwd)

        self._nc_set_status(self._("Connecting…"), ok=True)
        self._nc_connect_btn.set_sensitive(False)

        def _worker():
            from .nextcloud import NextcloudClient
            try:
                client = NextcloudClient(url, user, pwd)
                client.list_files(path)
                GLib.idle_add(self._nc_connect_done, True, "")
            except Exception as exc:
                GLib.idle_add(self._nc_connect_done, False, str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _nc_connect_done(self, ok: bool, error: str) -> None:
        self._nc_connect_btn.set_sensitive(True)
        if ok:
            self.settings.nextcloud_enabled = True
            self.settings.nextcloud_session_active = True
            self.settings.save()
            self._nc_runtime_connected = True
            # User-initiated → runtime gate may open.
            self.parent_window._nc_session_active = True
            self.parent_window.settings.nextcloud_session_active = True
            # Setup button is now obsolete; full UI may also have been hidden if
            # this was the very first connect.
            self._nc_manual_setup_unlocked = True
            self._nc_refresh_layout()
            self._nc_refresh_status()
            self.parent_window.apply_settings(self.settings)
        else:
            self._nc_runtime_connected = False
            self._nc_update_buttons()
            self._nc_set_status(
                f"{self._('Connection failed')}: {error}" if error else self._("Connection failed"),
                ok=False,
            )

    def _nc_disconnect(self, _btn: Gtk.Button) -> None:
        # "Disconnect" is a soft action: it stops every NC *network* operation
        # in this session (workers, scans, thumb fetches) by flipping the
        # runtime gate. The NC tab and cached thumbnails stay visible — only
        # operations that need the server are blocked. The persistent
        # "Nextcloud aktiv" preference is untouched, but the disconnect state
        # itself is persisted so the next launch comes up disconnected too.
        old_client = self.parent_window._nc_thumb_shared_client
        self.parent_window._nc_thumb_shared_client = None
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass
        self.parent_window._nc_session_active = False
        self.settings.nextcloud_session_active = False
        self.parent_window.settings.nextcloud_session_active = False
        self.parent_window.settings.save()
        self._nc_runtime_connected = False
        self._nc_update_buttons()
        self._nc_set_status(self._("Disconnected"), ok=False)

    def _nc_scan_qr(self, _btn: Gtk.Button) -> None:
        from .qr import WebcamQRScanner, scan_supported, QRScanError

        dialog = Adw.Dialog()
        dialog.set_title(self._("Scan QR code"))
        dialog.set_content_width(480)
        dialog.set_content_height(400)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        close_btn = Gtk.Button(label=self._("Cancel"))
        close_btn.connect("clicked", lambda _: dialog.close())
        header.pack_start(close_btn)

        if not scan_supported():
            lbl = Gtk.Label(
                label=self._("GStreamer camera support missing.\n"
                             "Install: apt install gstreamer1.0-plugins-bad\n"
                             "                    python3-gst-1.0"),
                wrap=True, xalign=0.5,
            )
            lbl.set_margin_top(24)
            lbl.set_margin_start(16)
            lbl.set_margin_end(16)
            toolbar.set_content(lbl)
            dialog.set_child(toolbar)
            dialog.present(self)
            return

        scanner = WebcamQRScanner(
            on_success=lambda text: GLib.idle_add(self._nc_qr_success, dialog, text),
            on_error=lambda msg: GLib.idle_add(self._nc_qr_error, msg),
        )
        toolbar.set_content(scanner.build_widget())
        dialog.set_child(toolbar)

        dialog.connect("closed", lambda _: scanner.cancel())
        dialog.present(self)
        scanner.start()

    @staticmethod
    def _parse_nc_login_url(text: str) -> dict[str, str] | None:
        """Parse nc://login/user:U&password:P&server:S → {user, password, server} or None."""
        if not text.startswith("nc://login/"):
            return None
        params: dict[str, str] = {}
        for part in text[len("nc://login/"):].split("&"):
            key, sep, value = part.partition(":")
            if sep and key in ("user", "password", "server"):
                params[key] = value
        if "password" not in params:
            return None
        return params

    def _nc_qr_success(self, dialog: Adw.Dialog, text: str) -> None:
        dialog.close()
        parsed = self._parse_nc_login_url(text)
        if parsed:
            if "server" in parsed:
                self._nc_url_row.set_text(parsed["server"])
            if "user" in parsed:
                self._nc_user_row.set_text(parsed["user"])
            self._nc_pass_row.set_text(parsed["password"])
            self._nc_set_status(self._("QR code scanned – credentials entered ✓"), ok=True)
        else:
            self._nc_pass_row.set_text(text)
            self._nc_set_status(self._("QR code scanned successfully ✓"), ok=True)

    def _nc_qr_error(self, message: str) -> None:
        self._nc_set_status(f"{self._('QR code scan error')}: {message}", ok=False)

    def _nc_thumb_only_changed(self, row: Adw.SwitchRow, _param) -> None:
        value = row.get_active()
        self.settings.nextcloud_thumbnail_only = value
        # Apply immediately so the change is live without closing the dialog.
        self.parent_window.settings.nextcloud_thumbnail_only = value
        self.parent_window.settings.save()

    def _nc_show_in_pictures_changed(self, row: Adw.SwitchRow, _param) -> None:
        value = row.get_active()
        self.settings.nextcloud_show_in_pictures = value
        self.parent_window.settings.nextcloud_show_in_pictures = value
        self.parent_window.settings.save()
        # When enabling, kick off a NC scan so its index is fresh; the soft refresh
        # below would otherwise show nothing if NC was never scanned in this session.
        if self.parent_window.category == "pictures":
            self.parent_window.refresh(scan=value)

    def _folder_row(self, attr: str, title: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=self._(title), subtitle=getattr(self.settings, attr))
        choose = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        choose.set_tooltip_text(self._("Choose folder"))
        choose.connect("clicked", self._choose_folder, attr, row)
        row.add_suffix(choose)
        return row

    # ── Media folder reorder via drag handles ───────────────────────────────
    def _row_spec(self, key: str) -> dict | None:
        """Resolve a media-folder key into a render spec.
        Built-ins are gated by their path being non-empty (clearing the path
        is how the user "deletes" a built-in). Extras are recovered from
        extra_locations by their integer index."""
        if key in self._media_folder_specs:
            base = self._media_folder_specs[key]
            attr = base["attr"]
            value = getattr(self.settings, attr) or ""
            if key == "pictures":
                # Overview is a virtual aggregator — always shown in the
                # listbox so the user can toggle its visibility. It can't
                # be removed and has no path picker.
                return {
                    "key":   key,
                    "title": base["title"],
                    "path":  self._("All other folders combined"),
                    "kind":  "overview",
                    "attr":  attr,
                    "removable": False,
                }
            if not value and base["kind"] != "nextcloud":
                return None
            if base["kind"] == "nextcloud" and not self.settings.nextcloud_enabled:
                return None
            return {
                "key":   key,
                "title": base["title"],
                "path":  value or ("Photos" if base["kind"] == "nextcloud" else ""),
                "kind":  base["kind"],
                "attr":  attr,
                "removable": base["kind"] != "nextcloud",
            }
        if key.startswith("location:"):
            try:
                idx = int(key.split(":", 1)[1])
            except ValueError:
                return None
            if idx < 0 or idx >= len(self.settings.extra_locations):
                return None
            path = self.settings.extra_locations[idx]
            custom_name = ""
            if idx < len(self.settings.extra_location_names):
                custom_name = (self.settings.extra_location_names[idx] or "").strip()
            title = custom_name or Path(path).name or path
            return {
                "key":       key,
                "title":     title,
                "path":      path,
                "kind":      "extra",
                "attr":      None,
                "extra_idx": idx,
                "removable": True,
            }
        return None

    def _available_media_keys(self) -> list[str]:
        keys: list[str] = []
        # Overview is virtual — listed regardless of any path setting so the
        # user can always toggle its visibility from the row.
        keys.append("pictures")
        for k in ("photos", "videos", "screenshots"):
            if getattr(self.settings, self._media_folder_specs[k]["attr"]):
                keys.append(k)
        if self.settings.nextcloud_enabled:
            keys.append("nextcloud")
        keys.extend(f"location:{i}" for i in range(len(self.settings.extra_locations)))
        return keys

    def _media_order(self) -> list[str]:
        """Saved order filtered to currently available keys, with any newly
        appearing keys appended at the end."""
        available = self._available_media_keys()
        saved = list(self.settings.media_folder_order or [])
        order = [k for k in saved if k in available]
        for k in available:
            if k not in order:
                order.append(k)
        return order

    def _populate_media_listbox(self) -> None:
        child = self._media_listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._media_listbox.remove(child)
            child = nxt
        for key in self._media_order():
            row = self._build_media_row(key)
            if row is not None:
                self._media_listbox.append(row)

    def _build_media_row(self, key: str) -> Gtk.ListBoxRow | None:
        spec = self._row_spec(key)
        if spec is None:
            return None

        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        # Stash the category key on the widget so the drop handler can
        # reorder by key without an extra dict lookup.
        row.media_key = key  # type: ignore[attr-defined]

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        grip = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        grip.add_css_class("dim-label")
        grip.set_tooltip_text(self._("Drag to reorder"))
        grip.set_cursor(Gdk.Cursor.new_from_name("grab", None))
        box.append(grip)

        title_lbl = Gtk.Label(label=self._(spec["title"]), xalign=0)
        title_lbl.add_css_class("body")
        path_lbl = Gtk.Label(label=spec["path"], xalign=0)
        path_lbl.add_css_class("caption")
        path_lbl.add_css_class("dim-label")
        path_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        path_lbl.set_max_width_chars(30)
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.append(title_lbl)
        text_box.append(path_lbl)
        box.append(text_box)

        # Overview takes a visibility toggle in place of the trash + chooser
        # combo: it is a virtual aggregator that the user can hide but never
        # remove, and there's no underlying path to pick.
        if spec["kind"] == "overview":
            hidden = bool(self.settings.pictures_hidden)
            edit = Gtk.Button.new_from_icon_name("document-edit-symbolic")
            edit.set_tooltip_text(self._("Edit"))
            edit.add_css_class("flat")
            edit.set_valign(Gtk.Align.CENTER)
            edit.connect("clicked", self._edit_overview)
            box.append(edit)
            toggle = Gtk.Button.new_from_icon_name(
                "view-reveal-symbolic" if hidden else "view-conceal-symbolic"
            )
            toggle.set_tooltip_text(
                self._("Show") if hidden else self._("Hide")
            )
            toggle.add_css_class("flat")
            toggle.set_valign(Gtk.Align.CENTER)
            toggle.connect("clicked", self._toggle_pictures_hidden)
            if hidden:
                title_lbl.add_css_class("dim-label")
            box.append(toggle)
        else:
            # Trash icon — left of the chooser/edit button. Hidden for Nextcloud,
            # which is structurally not removable in this UI.
            if spec["removable"]:
                trash = Gtk.Button.new_from_icon_name("user-trash-symbolic")
                trash.set_tooltip_text(self._("Remove"))
                trash.add_css_class("flat")
                trash.set_valign(Gtk.Align.CENTER)
                trash.connect("clicked", self._confirm_remove_media, key)
                box.append(trash)

            if spec["kind"] == "local":
                choose = Gtk.Button.new_from_icon_name("folder-open-symbolic")
                choose.set_tooltip_text(self._("Choose folder"))
                choose.connect("clicked", self._choose_folder_for_key, spec["attr"], path_lbl)
            elif spec["kind"] == "extra":
                # Pencil — opens a dialog with both Name and Path fields, since the
                # custom name is what shows up in the gallery navigation.
                choose = Gtk.Button.new_from_icon_name("document-edit-symbolic")
                choose.set_tooltip_text(self._("Edit"))
                choose.connect("clicked", self._edit_extra_location, spec["extra_idx"], title_lbl, path_lbl)
            else:  # nextcloud
                choose = Gtk.Button.new_from_icon_name("document-edit-symbolic")
                choose.set_tooltip_text(self._("Edit"))
                choose.connect("clicked", self._edit_nc_path, path_lbl)
            choose.add_css_class("flat")
            choose.set_valign(Gtk.Align.CENTER)
            box.append(choose)

        row.set_child(box)

        # Drag source attached to the grip widget so dragging only initiates
        # when the user grabs the handle, not when interacting with the chooser.
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.MOVE)
        drag.connect("prepare", self._on_media_drag_prepare, row)
        drag.connect("drag-begin", self._on_media_drag_begin, row)
        grip.add_controller(drag)

        # Drop target on each row so the drop position is unambiguous.
        drop = Gtk.DropTarget.new(Gtk.ListBoxRow, Gdk.DragAction.MOVE)
        drop.connect("drop", self._on_media_drop, row)
        row.add_controller(drop)

        return row

    def _on_media_drag_prepare(self, _src, _x, _y, row):
        return Gdk.ContentProvider.new_for_value(row)

    def _on_media_drag_begin(self, src, _drag, row):
        # Use the row's own snapshot as the drag image so it's visually obvious
        # which element is moving.
        try:
            paintable = Gtk.WidgetPaintable.new(row)
            src.set_icon(paintable, 0, 0)
        except Exception:
            pass

    def _on_media_drop(self, _target, value, _x, _y, target_row):
        if not isinstance(value, Gtk.ListBoxRow) or value is target_row:
            return False
        src_key = getattr(value, "media_key", None)
        dst_key = getattr(target_row, "media_key", None)
        if src_key is None or dst_key is None or src_key == dst_key:
            return False
        order = self._media_order()
        try:
            order.remove(src_key)
            insert_at = order.index(dst_key)
        except ValueError:
            return False
        order.insert(insert_at, src_key)
        # Persist + propagate
        self.settings.media_folder_order = order
        self.parent_window.settings.media_folder_order = order
        self.parent_window.settings.save()
        # Rebuild the listbox so visual order matches state.
        self._populate_media_listbox()
        # Mirror to the gallery's category nav.
        self.parent_window._rebuild_categories()
        return True

    def _choose_folder_for_key(self, _btn: Gtk.Button, attr: str, path_lbl: Gtk.Label) -> None:
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._media_folder_chosen, attr, path_lbl)
        chooser.show()

    def _media_folder_chosen(
        self, chooser: Gtk.FileChooserNative, response: int,
        attr: str, path_lbl: Gtk.Label,
    ) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            file = chooser.get_file()
            if file is not None:
                path = file.get_path() or ""
                setattr(self.settings, attr, path)
                setattr(self.parent_window.settings, attr, path)
                path_lbl.set_label(path)
                self.parent_window.settings.save()
                self.parent_window._rebuild_categories()
                self.parent_window.refresh(scan=True)
        chooser.destroy()

    def _combo_row(self, attr: str, title: str, values: list[tuple[str, str]]) -> Adw.ComboRow:
        store = Gtk.StringList()
        active = 0
        current = getattr(self.settings, attr)
        for i, (value, label) in enumerate(values):
            store.append(self._(label))
            if value == current:
                active = i
        row = Adw.ComboRow(title=self._(title), model=store, selected=active)
        row.values = [value for value, _label in values]
        row.connect("notify::selected", self._combo_changed, attr)
        return row

    def _combo_changed(self, row: Adw.ComboRow, _param, attr: str) -> None:
        setattr(self.settings, attr, row.values[row.get_selected()])
        self.parent_window.apply_settings(self.settings)

    def _entry_apply(self, row: Adw.EntryRow, attr: str) -> None:
        setattr(self.settings, attr, row.get_text())
        self.parent_window.apply_settings(self.settings)

    def _edit_nc_path(self, _btn: Gtk.Button, path_lbl: Gtk.Label) -> None:
        """Inline editor for the Nextcloud Photos folder, opened from the
        media-folders listbox row."""
        dialog = Adw.AlertDialog(
            heading=self._("Photos folder on Nextcloud"),
            body=self._("Path of the Photos folder on your Nextcloud server."),
        )
        entry = Gtk.Entry()
        entry.set_text(self.settings.nextcloud_photos_path or "Photos")
        entry.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        entry.set_activates_default(True)
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        wrapper.set_margin_top(6)
        wrapper.append(entry)
        dialog.set_extra_child(wrapper)
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("ok", self._("Save"))
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def _done(_dialog, response):
            if response != "ok":
                return
            value = entry.get_text().strip() or "Photos"
            self.settings.nextcloud_photos_path = value
            self.parent_window.settings.nextcloud_photos_path = value
            self.parent_window.settings.save()
            path_lbl.set_label(value)
            self.parent_window._rebuild_categories()

        dialog.connect("response", _done)
        dialog.present(self)
        GLib.idle_add(lambda: (entry.grab_focus(), GLib.SOURCE_REMOVE)[1])

    def _columns_changed(self, row: Adw.SpinRow, _param) -> None:
        self.settings.grid_columns = int(row.get_value())
        self.parent_window.apply_settings(self.settings)

    def _cache_max_mb_changed(self, row: Adw.SpinRow, _param) -> None:
        value = int(row.get_value())
        self.settings.cache_max_mb = value
        self.parent_window.settings.cache_max_mb = value
        self.parent_window.settings.save()
        # Trigger eviction in the background — won't block the UI even on
        # huge caches, since the file walk runs off the main loop.
        self.parent_window.evict_cache_async()
        # Schedule a delayed display refresh so the user sees the new size.
        GLib.timeout_add(800, self._refresh_cache_size_display_once)

    def _on_clear_cache_clicked(self, _btn: Gtk.Button) -> None:
        # Run the (potentially slow) wipe off the main thread.
        def _worker():
            self.parent_window.clear_cache()
            GLib.idle_add(self._refresh_cache_size_display_once)
        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_cache_size_display(self) -> None:
        """Compute current cache size off the main thread, then update the row."""
        def _worker():
            try:
                size = self.parent_window.cache_size_bytes()
            except Exception:
                size = 0
            GLib.idle_add(self._set_cache_size_text, size)
        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_cache_size_display_once(self) -> bool:
        self._refresh_cache_size_display()
        return GLib.SOURCE_REMOVE

    def _set_cache_size_text(self, size_bytes: int) -> bool:
        if size_bytes < 1024 * 1024:
            text = f"{size_bytes / 1024:.0f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            text = f"{size_bytes / 1024 / 1024:.1f} MB"
        else:
            text = f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"
        self._cache_size_row.set_subtitle(text)
        return GLib.SOURCE_REMOVE

    def _choose_folder(self, _button: Gtk.Button, attr: str, row: Adw.ActionRow) -> None:
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._folder_response, attr, row)
        chooser.show()

    def _folder_response(self, chooser: Gtk.FileChooserNative, response: int, attr: str, row: Adw.ActionRow) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            setattr(self.settings, attr, path)
            row.set_subtitle(path)
            self.parent_window.apply_settings(self.settings)
        chooser.destroy()

    def _add_location(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserNative(
            title=self._("Choose folder"), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        # Start at the user's home directory rather than wherever the chooser
        # last landed (typically inside Pictures from a previous interaction).
        try:
            home = Gio.File.new_for_path(str(Path.home()))
            chooser.set_current_folder(home)
        except Exception:
            pass
        chooser.connect("response", self._add_location_response)
        chooser.show()

    def _add_location_response(self, chooser: Gtk.FileChooserNative, response: int) -> None:
        try:
            if response != Gtk.ResponseType.ACCEPT:
                return
            file = chooser.get_file()
            if file is None:
                return
            path = file.get_path()
            if not path or path in self.settings.extra_locations:
                return
            # self.settings was built via `Settings(**parent.settings.__dict__)`,
            # so its list attributes are *the same* objects as parent's — one
            # append per list reaches both. A second append on the parent ref
            # was the cause of every new folder showing up twice.
            self.settings.extra_locations.append(path)
            self.settings.extra_location_names.append("")
            self.settings.extra_location_no_inherit.append(False)
            self.settings.extra_location_media_filter.append("both")
            new_idx = len(self.settings.extra_locations) - 1
            order = list(self.settings.media_folder_order or [])
            order.append(f"location:{new_idx}")
            self.settings.media_folder_order = order
            self.parent_window.settings.media_folder_order = order
            self.parent_window.settings.save()
            self._populate_media_listbox()
            self.parent_window._rebuild_categories()
            self.parent_window.refresh(scan=True)
        finally:
            chooser.destroy()

    def _edit_overview(self, _btn: Gtk.Button) -> None:
        """Overview has no path or name to edit — the only editable knob
        is the media-type filter (Images / Videos / Both). Mirrors the
        extras edit dialog so the UX stays consistent."""
        current = self.settings.pictures_media_filter
        if current not in ("both", "images", "videos"):
            current = "images"

        dialog = Adw.AlertDialog(
            heading=self._("Edit folder"),
            body=self._("Overview shows the combined content of every other folder."),
        )

        filter_values = ["both", "images", "videos"]
        filter_labels = [self._("Both"), self._("Images only"), self._("Videos only")]
        filter_store = Gtk.StringList()
        for lbl in filter_labels:
            filter_store.append(lbl)
        filter_row = Adw.ComboRow(
            title=self._("Show"),
            subtitle=self._("Which media types appear when this folder is opened."),
            model=filter_store,
            selected=filter_values.index(current),
        )

        group = Adw.PreferencesGroup()
        group.add(filter_row)
        dialog.set_extra_child(group)

        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("save", self._("Save"))
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def _done(_d, response):
            if response != "save":
                return
            new_val = filter_values[filter_row.get_selected()]
            self.settings.pictures_media_filter = new_val
            self.parent_window.settings.pictures_media_filter = new_val
            self.parent_window.settings.save()
            # Re-render only if Overview is currently visible — otherwise
            # the change applies the next time the user shows it.
            if self.parent_window.category == "pictures":
                self.parent_window.refresh(scan=False)

        dialog.connect("response", _done)
        dialog.present(self)

    def _toggle_pictures_hidden(self, _btn: Gtk.Button) -> None:
        new_value = not bool(self.settings.pictures_hidden)
        self.settings.pictures_hidden = new_value
        self.parent_window.settings.pictures_hidden = new_value
        self.parent_window.settings.save()
        self._populate_media_listbox()
        # _rebuild_categories drops the Overview button when hidden. If
        # Overview was the active tab, activate the first remaining
        # button so the gallery doesn't stay pointed at a vanished tab.
        self.parent_window._rebuild_categories()
        if new_value and self.parent_window.category == "pictures":
            remaining = list(self.parent_window.category_buttons.items())
            if remaining:
                next_cat, next_btn = remaining[0]
                # set_active(True) fires _on_category_toggled, which handles
                # the self.category update, last_category persistence and
                # the re-render in one path.
                next_btn.set_active(True)
                return
        self.parent_window.refresh(scan=False)

    def _confirm_remove_media(self, _btn: Gtk.Button, key: str) -> None:
        spec = self._row_spec(key)
        if spec is None or not spec["removable"]:
            return
        dialog = Adw.AlertDialog(
            heading=self._("Remove this folder?"),
            body=self._("It will disappear from the gallery navigation. The files on disk are not deleted."),
        )
        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("remove", self._("Remove"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r, k=key: self._remove_media_done(r, k))
        dialog.present(self)

    def _remove_media_done(self, response: str, key: str) -> None:
        if response != "remove":
            return
        spec = self._row_spec(key)
        if spec is None:
            return
        if spec["kind"] == "extra":
            self._remove_extra_at(spec["extra_idx"])
        else:
            # Built-in: clear the path; the categories() filter will then drop it.
            setattr(self.settings, spec["attr"], "")
            setattr(self.parent_window.settings, spec["attr"], "")
            order = [k for k in (self.settings.media_folder_order or []) if k != key]
            self.settings.media_folder_order = order
            self.parent_window.settings.media_folder_order = order
        self.parent_window.settings.save()
        self._populate_media_listbox()
        self.parent_window._rebuild_categories()
        self.parent_window.refresh(scan=True)

    def _remove_extra_at(self, idx: int) -> None:
        """Drop extra_locations[idx] and renumber every later location:N key
        in media_folder_order so indices stay aligned with the list."""
        if idx < 0 or idx >= len(self.settings.extra_locations):
            return
        new_extras = list(self.settings.extra_locations)
        new_extras.pop(idx)
        self.settings.extra_locations = new_extras
        self.parent_window.settings.extra_locations = list(new_extras)
        # Names + no-inherit lists run in lock-step with the paths. They are
        # shared with parent.settings (shallow-copy in __init__), so one
        # pop per list is enough — popping twice removed the next entry too.
        if idx < len(self.settings.extra_location_names):
            self.settings.extra_location_names.pop(idx)
        if idx < len(self.settings.extra_location_no_inherit):
            self.settings.extra_location_no_inherit.pop(idx)
        if idx < len(self.settings.extra_location_media_filter):
            self.settings.extra_location_media_filter.pop(idx)

        renumbered: list[str] = []
        for k in (self.settings.media_folder_order or []):
            if k == f"location:{idx}":
                continue  # the deleted one
            if k.startswith("location:"):
                try:
                    n = int(k.split(":", 1)[1])
                except ValueError:
                    renumbered.append(k)
                    continue
                if n > idx:
                    renumbered.append(f"location:{n - 1}")
                else:
                    renumbered.append(k)
            else:
                renumbered.append(k)
        self.settings.media_folder_order = renumbered
        self.parent_window.settings.media_folder_order = renumbered

    def _edit_extra_location(
        self, _btn: Gtk.Button, idx: int,
        title_lbl: Gtk.Label, path_lbl: Gtk.Label,
    ) -> None:
        if idx < 0 or idx >= len(self.settings.extra_locations):
            return
        current_path = self.settings.extra_locations[idx]
        custom_name = (
            self.settings.extra_location_names[idx]
            if idx < len(self.settings.extra_location_names)
            else ""
        )
        current_no_inherit = (
            self.settings.extra_location_no_inherit[idx]
            if idx < len(self.settings.extra_location_no_inherit)
            else False
        )
        current_media_filter = (
            self.settings.extra_location_media_filter[idx]
            if idx < len(self.settings.extra_location_media_filter)
            else "both"
        )
        if current_media_filter not in ("both", "images", "videos"):
            current_media_filter = "both"
        # Pre-fill with whatever is currently shown as the entry label so the
        # user starts editing from the value they actually see.
        display_name = custom_name or Path(current_path).name or current_path

        dialog = Adw.AlertDialog(
            heading=self._("Edit folder"),
            body=self._("The name appears as the entry label in the gallery navigation."),
        )
        # Two stacked entry rows for Name + Path.
        name_row = Adw.EntryRow(title=self._("Name"))
        name_row.set_text(display_name)
        path_row = Adw.EntryRow(title=self._("Path"))
        path_row.set_text(current_path)
        path_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        # "Browse" button as suffix on the path row → file picker.
        browse = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        browse.set_valign(Gtk.Align.CENTER)
        browse.add_css_class("flat")

        def _open_picker(_btn):
            chooser = Gtk.FileChooserNative(
                title=self._("Choose folder"), transient_for=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            try:
                home = Gio.File.new_for_path(str(Path.home()))
                chooser.set_current_folder(home)
            except Exception:
                pass

            def _picked(c, response):
                try:
                    if response == Gtk.ResponseType.ACCEPT:
                        f = c.get_file()
                        if f is not None:
                            picked_path = f.get_path() or ""
                            if picked_path:
                                path_row.set_text(picked_path)
                finally:
                    c.destroy()

            chooser.connect("response", _picked)
            chooser.show()

        browse.connect("clicked", _open_picker)
        path_row.add_suffix(browse)

        inherit_row = Adw.SwitchRow(
            title=self._("Don't inherit"),
            subtitle=self._(
                "Parent folders won't include this folder's content during scans."
            ),
        )
        inherit_row.set_active(bool(current_no_inherit))

        # Media-type filter: the order of `filter_values` must stay in lock-step
        # with the labels appended to `filter_store` so the resolved selection
        # maps back to the right enum value.
        filter_values = ["both", "images", "videos"]
        filter_labels = [self._("Both"), self._("Images only"), self._("Videos only")]
        filter_store = Gtk.StringList()
        for lbl in filter_labels:
            filter_store.append(lbl)
        filter_row = Adw.ComboRow(
            title=self._("Show"),
            subtitle=self._("Which media types appear when this folder is opened."),
            model=filter_store,
            selected=filter_values.index(current_media_filter),
        )

        group = Adw.PreferencesGroup()
        group.add(name_row)
        group.add(path_row)
        group.add(inherit_row)
        group.add(filter_row)
        dialog.set_extra_child(group)

        dialog.add_response("cancel", self._("Cancel"))
        dialog.add_response("save", self._("Save"))
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def _done(_d, response):
            if response != "save":
                return
            new_name = name_row.get_text().strip()
            new_path = path_row.get_text().strip() or current_path
            new_no_inherit = bool(inherit_row.get_active())
            new_media_filter = filter_values[filter_row.get_selected()]
            # The list fields are shared with parent.settings (shallow-copy in
            # __init__), so one write per list reaches both.
            self.settings.extra_locations[idx] = new_path
            while len(self.settings.extra_location_names) <= idx:
                self.settings.extra_location_names.append("")
            self.settings.extra_location_names[idx] = new_name
            while len(self.settings.extra_location_no_inherit) <= idx:
                self.settings.extra_location_no_inherit.append(False)
            self.settings.extra_location_no_inherit[idx] = new_no_inherit
            while len(self.settings.extra_location_media_filter) <= idx:
                self.settings.extra_location_media_filter.append("both")
            self.settings.extra_location_media_filter[idx] = new_media_filter
            self.parent_window.settings.save()
            # Refresh the row (title falls back to basename if name is empty)
            display_title = new_name or Path(new_path).name or new_path
            title_lbl.set_label(display_title)
            path_lbl.set_label(new_path)
            self.parent_window._rebuild_categories()
            self.parent_window.refresh(scan=True)

        dialog.connect("response", _done)
        dialog.present(self)



# ---------------------------------------------------------------------------
# Viewer window
# ---------------------------------------------------------------------------
