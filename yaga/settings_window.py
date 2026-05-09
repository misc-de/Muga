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

        self._build_nextcloud_page()

    def _build_nextcloud_page(self) -> None:
        page = Adw.PreferencesPage(title="Nextcloud", icon_name="folder-remote-symbolic")
        self.add(page)

        # ── Credentials ──
        self._nc_creds_group = Adw.PreferencesGroup(title=self._("Credentials"))
        page.add(self._nc_creds_group)

        # Top row: status text + Connect/Disconnect button on the right
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
        perf = Adw.PreferencesGroup(title=self._("Performance"))
        page.add(perf)

        thumb_row = Adw.SwitchRow(
            title=self._("Load thumbnails only"),
            subtitle=self._("Skip downloading full files during sync"),
        )
        thumb_row.set_active(self.settings.nextcloud_thumbnail_only)
        thumb_row.connect("notify::active", self._nc_thumb_only_changed)
        perf.add(thumb_row)

        merge_row = Adw.SwitchRow(
            title=self._("Show in Pictures"),
            subtitle=self._("Merge Nextcloud items into the Pictures view (thumbnails load on demand)"),
        )
        merge_row.set_active(self.settings.nextcloud_show_in_pictures)
        merge_row.connect("notify::active", self._nc_show_in_pictures_changed)
        perf.add(merge_row)

        self._nc_refresh_status()

    @staticmethod
    def _make_icon_label(icon_name: str, label: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(Gtk.Image.new_from_icon_name(icon_name))
        box.append(Gtk.Label(label=label))
        return box

    def _nc_update_buttons(self) -> None:
        connected = self.settings.nextcloud_enabled
        self._nc_connect_btn.set_visible(not connected)
        self._nc_disconnect_btn.set_visible(connected)

    def _nc_set_status(self, text: str, ok: bool = True) -> None:
        self._nc_status_row.set_title(text)
        icon = "emblem-ok-symbolic" if ok else "dialog-warning-symbolic"
        if self._nc_status_icon is not None:
            self._nc_status_row.remove(self._nc_status_icon)
        self._nc_status_icon = Gtk.Image.new_from_icon_name(icon)
        self._nc_status_row.add_prefix(self._nc_status_icon)

    def _nc_refresh_status(self) -> None:
        """Sync status row + buttons + group description with the current
        nextcloud_enabled state. Called whenever the connection state changes."""
        connected = self.settings.nextcloud_enabled
        self._nc_update_buttons()
        if connected:
            self._nc_set_status(self._("Connected ✓"), ok=True)
            # Tip is no longer needed once the user has set things up.
            self._nc_creds_group.set_description("")
        else:
            self._nc_set_status(self._("Disconnected"), ok=False)
            self._nc_creds_group.set_description(
                self._("Create app password: Nextcloud → Settings → Security → App passwords")
            )

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
            self.settings.save()
            self._nc_refresh_status()
            self.parent_window.apply_settings(self.settings)
        else:
            self._nc_set_status(f"{self._('Connection failed')}: {error}" if error else self._("Connection failed"), ok=False)

    def _nc_disconnect(self, _btn: Gtk.Button) -> None:
        self.settings.nextcloud_enabled = False
        self.settings.save()
        self._nc_refresh_status()
        self.parent_window.apply_settings(self.settings)

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
