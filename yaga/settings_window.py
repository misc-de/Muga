from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from .config import Settings

class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, parent: GalleryWindow) -> None:
        super().__init__(transient_for=parent, modal=True, title=parent._("Settings"))
        self.set_search_enabled(False)
        self.parent_window = parent
        self.settings = Settings(**parent.settings.__dict__)
        self._build()
        # Suppress GTK's default "focus the first focusable widget" so opening
        # settings doesn't pop up the on-screen keyboard on a SpinRow / Entry.
        GLib.idle_add(lambda: (self.set_focus(None), GLib.SOURCE_REMOVE)[1])

    def _(self, text: str) -> str:
        return self.parent_window._(text)

    def _build(self) -> None:
        media = Adw.PreferencesPage(title=self._("Media folders"), icon_name="folder-pictures-symbolic")
        self.add(media)
        group = Adw.PreferencesGroup(title=self._("Media folders"))
        media.add(group)

        for attr, title in [
            ("photos_dir", "Photos folder"),
            ("pictures_dir", "Pictures folder"),
            ("videos_dir", "Videos folder"),
            ("screenshots_dir", "Screenshots folder"),
        ]:
            group.add(self._folder_row(attr, title))

        extra = Adw.PreferencesGroup(title=self._("Optional locations"))
        media.add(extra)
        add = Gtk.Button(label=self._("Add location"), icon_name="list-add-symbolic")
        add.connect("clicked", self._add_location)
        extra.add(add)
        for path in self.settings.extra_locations:
            row = Adw.ActionRow(title=Path(path).name or path, subtitle=path)
            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            remove.connect("clicked", self._remove_location, path)
            row.add_suffix(remove)
            extra.add(row)
        self.extra_group = extra

        app = Adw.PreferencesPage(title=self._("Appearance"), icon_name="preferences-desktop-appearance-symbolic")
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
        clear_btn = Gtk.Button(label=self._("Cache löschen"))
        clear_btn.add_css_class("destructive-action")
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_btn.connect("clicked", self._on_clear_cache_clicked)
        self._cache_size_row.add_suffix(clear_btn)
        cache_group.add(self._cache_size_row)
        self._refresh_cache_size_display()

        self._build_nextcloud_page()

    def _build_nextcloud_page(self) -> None:
        page = Adw.PreferencesPage(title="Nextcloud", icon_name="folder-remote-symbolic")
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
            title=self._("Nextcloud aktiv"),
            subtitle=self._("Aktiviert oder deaktiviert alle Nextcloud-Funktionen"),
        )
        self._nc_active_row.set_active(self.settings.nextcloud_enabled)
        self._nc_active_handler = self._nc_active_row.connect(
            "notify::active", self._nc_active_changed,
        )
        self._nc_top_group.add(self._nc_active_row)

        self._nc_setup_row = Adw.ActionRow(
            title=self._("Verbindung einrichten"),
            subtitle=self._("Mit deiner Nextcloud verbinden"),
        )
        setup_btn = Gtk.Button(label=self._("Einrichten"))
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

        self._nc_path_row = Adw.EntryRow(title=self._("Photos folder on Nextcloud"))
        self._nc_path_row.set_text(self.settings.nextcloud_photos_path or "Photos")
        self._nc_path_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        self._nc_creds_group.add(self._nc_path_row)

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
            title=self._("Show in Pictures"),
            subtitle=self._("Merge Nextcloud items into the Pictures view (thumbnails load on demand)"),
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
            heading=self._("Verbindung einrichten"),
            body=self._(
                "Wie möchtest du deine Nextcloud verbinden?\n\n"
                "Den App-Passwort-QR-Code findest du in deiner Nextcloud unter:\n"
                "Einstellungen → Sicherheit → App-Passwörter → „Neues App-Passwort erstellen“."
            ),
        )
        dialog.add_response("cancel", self._("Abbrechen"))
        dialog.add_response("manual", self._("Manuell"))
        dialog.add_response("qr", self._("QR-Code scannen"))
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
        The 'Nextcloud aktiv' toggle is only synced when sync_toggle=True
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
        path = self._nc_path_row.get_text().strip() or "Photos"

        if not url or not user or not pwd:
            self._nc_set_status(self._("Please fill in all fields."), ok=False)
            return

        url = self.settings._normalize_url(url)
        self._nc_url_row.set_text(url)

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
        chooser.connect("response", self._add_location_response)
        chooser.show()

    def _add_location_response(self, chooser: Gtk.FileChooserNative, response: int) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            if path and path not in self.settings.extra_locations:
                self.settings.extra_locations.append(path)
                self.parent_window.apply_settings(self.settings)
                self.close()
                SettingsWindow(self.parent_window).present()
        chooser.destroy()

    def _remove_location(self, _button: Gtk.Button, path: str) -> None:
        self.settings.extra_locations = [item for item in self.settings.extra_locations if item != path]
        self.parent_window.apply_settings(self.settings)
        self.close()
        SettingsWindow(self.parent_window).present()



# ---------------------------------------------------------------------------
# Viewer window
# ---------------------------------------------------------------------------
