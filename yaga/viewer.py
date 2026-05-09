from __future__ import annotations

import dataclasses
import logging
import math
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from .editor import EditorView, PILImage, _PIL_OK
from .models import MediaItem
from .nextcloud import is_nc_path

LOGGER = logging.getLogger(__name__)

def _fmt_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_date(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d  %H:%M")


def _image_dimensions(path: str) -> str | None:
    fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(path)
    if fmt is not None:
        return f"{w} × {h}"
    return None


def _extract_exif(path: str) -> dict[str, str]:
    """Extract camera model and GPS coords from EXIF, if available."""
    exif_info: dict[str, str] = {}
    if not _PIL_OK or PILImage is None:
        return exif_info
    try:
        from PIL.Image import Exif
        img = PILImage.open(path)
        exif_data = img.getexif()
        if not exif_data:
            return exif_info
        # Tag 271: Make, Tag 272: Model
        make = exif_data.get(271, "").strip()
        model = exif_data.get(272, "").strip()
        camera = ""
        if make and model:
            camera = f"{make} {model}"
        elif model:
            camera = model
        elif make:
            camera = make
        if camera:
            exif_info["Camera"] = camera
        # Tag 34853: GPS IFD Pointer → parse GPS tags
        if 34853 in exif_data:
            gps_ifd = exif_data.get_ifd(34853)
            # GPS Latitude (Tag 2), Longitude (Tag 4)
            lat_data = gps_ifd.get(2)
            lon_data = gps_ifd.get(4)
            lat_ref = gps_ifd.get(1, "N")  # N/S
            lon_ref = gps_ifd.get(3, "E")  # E/W
            if lat_data and lon_data:
                try:
                    lat = float(lat_data[0]) + float(lat_data[1]) / 60 + float(lat_data[2]) / 3600
                    lon = float(lon_data[0]) + float(lon_data[1]) / 60 + float(lon_data[2]) / 3600
                    if lat_ref == "S":
                        lat = -lat
                    if lon_ref == "W":
                        lon = -lon
                    exif_info["GPS"] = f"{lat:.4f}, {lon:.4f}"
                except (TypeError, IndexError, ZeroDivisionError):
                    pass
    except Exception:
        pass
    return exif_info


class ViewerWindow(Adw.ApplicationWindow):
    def __init__(self, parent: GalleryWindow, items: list[MediaItem], index: int, external_player: str = "") -> None:
        super().__init__(application=parent.get_application(), transient_for=parent, title=items[index].name)
        self.set_default_size(1000, 720)
        self.parent_window = parent
        self.items = items
        self.index = index
        self.external_player = external_player
        self.last_gesture_nav_at = 0
        self.zoom_scale = 1.0
        self.zoom_start_scale = 1.0
        self.zoom_view: Gtk.Picture | None = None
        self.zoom_scroller: Gtk.ScrolledWindow | None = None
        self._rotation: int = 0
        self._current_display_path: str | None = None
        self._current_is_video: bool = False
        
        # Slideshow state
        self._slideshow_active: bool = False
        self._slideshow_timeout_id: int | None = None
        self._slideshow_interval_ms: int = 3000  # 3 seconds
        self.toolbar = Adw.ToolbarView()
        self.set_content(self.toolbar)

        header = Adw.HeaderBar()
        self.header = header
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        self.close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        self.close_button.set_tooltip_text(parent._("Close"))
        self.close_button.connect("clicked", lambda _button: self.close())
        header.pack_end(self.close_button)

        self.delete_button = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self.delete_button.set_tooltip_text(parent._("Delete"))
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._confirm_delete_current)
        self.delete_button.set_visible(False)
        header.pack_start(self.delete_button)

        self.info_button = Gtk.Button.new_from_icon_name("help-about-symbolic")
        self.info_button.set_tooltip_text(parent._("Info"))
        self.info_button.connect("clicked", self._show_info)
        self.info_button.set_visible(False)
        header.pack_start(self.info_button)

        self.edit_button = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        self.edit_button.set_tooltip_text(parent._("Edit"))
        self.edit_button.connect("clicked", self._enter_edit_mode)
        self.edit_button.set_visible(False)
        header.pack_end(self.edit_button)

        self.rotate_button = Gtk.Button.new_from_icon_name("object-rotate-right-symbolic")
        self.rotate_button.set_tooltip_text(parent._("Rotate clockwise"))
        self.rotate_button.connect("clicked", lambda _b: self._rotate_by_step(1))
        self.rotate_button.set_visible(False)
        header.pack_end(self.rotate_button)

        self.cancel_edit_button = Gtk.Button.new_with_label(parent._("Cancel"))
        self.cancel_edit_button.connect("clicked", self._exit_edit_mode)
        self.cancel_edit_button.set_visible(False)
        header.pack_start(self.cancel_edit_button)

        self.undo_edit_button = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        self.undo_edit_button.set_tooltip_text(parent._("Undo"))
        self.undo_edit_button.connect("clicked", self._undo_edit)
        self.undo_edit_button.set_visible(False)
        self.undo_edit_button.set_sensitive(False)
        header.pack_start(self.undo_edit_button)

        self.redo_edit_button = Gtk.Button.new_from_icon_name("edit-redo-symbolic")
        self.redo_edit_button.set_tooltip_text(parent._("Redo"))
        self.redo_edit_button.connect("clicked", self._redo_edit)
        self.redo_edit_button.set_visible(False)
        self.redo_edit_button.set_sensitive(False)
        header.pack_start(self.redo_edit_button)

        self.save_edit_button = Gtk.Button.new_with_label(parent._("Save"))
        self.save_edit_button.add_css_class("suggested-action")
        self.save_edit_button.connect("clicked", self._save_edit)
        self.save_edit_button.set_visible(False)
        header.pack_end(self.save_edit_button)

        self.slideshow_button = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.slideshow_button.set_tooltip_text(parent._("Start slideshow"))
        self.slideshow_button.connect("clicked", self._toggle_slideshow)
        self.slideshow_button.set_visible(False)
        header.pack_end(self.slideshow_button)

        self._editor: EditorView | None = None
        self.toolbar.add_top_bar(header)

        # Date overlay (modern: "1 Mai" large, "2026" smaller and dim).
        # Floats above the image — does not push it down.
        self.date_day_label = Gtk.Label()
        self.date_day_label.add_css_class("viewer-date-day")
        self.date_year_label = Gtk.Label()
        self.date_year_label.add_css_class("viewer-date-year")
        date_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        date_box.set_halign(Gtk.Align.CENTER)
        date_box.add_css_class("viewer-date")
        date_box.append(self.date_day_label)
        date_box.append(self.date_year_label)
        self.date_revealer = Gtk.Revealer()
        self.date_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.date_revealer.set_transition_duration(150)
        self.date_revealer.set_child(date_box)
        self.date_revealer.set_reveal_child(False)
        self.date_revealer.set_halign(Gtk.Align.CENTER)
        self.date_revealer.set_valign(Gtk.Align.START)
        # Don't catch input events — clicks/swipes pass through to the image.
        self.date_revealer.set_can_target(False)

        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        # Filename pill, floating at the bottom of the viewer with the same
        # black background as the date pill but at normal font size.
        self.filename_label = Gtk.Label()
        self.filename_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.filename_label.set_max_width_chars(60)
        self.filename_label.add_css_class("viewer-filename")
        self.filename_revealer = Gtk.Revealer()
        self.filename_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.filename_revealer.set_transition_duration(150)
        self.filename_revealer.set_child(self.filename_label)
        self.filename_revealer.set_reveal_child(False)
        self.filename_revealer.set_halign(Gtk.Align.CENTER)
        self.filename_revealer.set_valign(Gtk.Align.END)
        self.filename_revealer.set_margin_bottom(20)
        self.filename_revealer.set_can_target(False)

        # Wrap stack + date + filename in an overlay so they float over the
        # image instead of stealing vertical space from the toolbar layout.
        self._content_overlay = Gtk.Overlay()
        self._content_overlay.set_child(self.stack)
        self._content_overlay.add_overlay(self.date_revealer)
        self._content_overlay.add_overlay(self.filename_revealer)
        self.toolbar.set_content(self._content_overlay)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.swipe_gesture = Gtk.GestureSwipe()
        self.swipe_gesture.connect("swipe", self._on_swipe)
        self.stack.add_controller(self.swipe_gesture)
        self.drag_gesture = Gtk.GestureDrag()
        self.drag_gesture.connect("drag-end", self._on_drag_end)
        self.stack.add_controller(self.drag_gesture)
        self.zoom_gesture = Gtk.GestureZoom()
        self.zoom_gesture.connect("begin", self._on_zoom_begin)
        self.zoom_gesture.connect("scale-changed", self._on_zoom_scale_changed)
        self.zoom_gesture.connect("end", lambda *_: setattr(self, "_zoom_committed", False))
        self.stack.add_controller(self.zoom_gesture)
        self._zoom_committed: bool = False
        self.click_gesture = Gtk.GestureClick()
        # set_exclusive: only fire when exactly one touch/button is involved,
        # so a two-finger pinch never registers as a click.
        self.click_gesture.set_exclusive(True)
        self._click_press_x: float = 0.0
        self._click_press_y: float = 0.0
        self.click_gesture.connect("pressed", self._on_viewer_press_begin)
        self.click_gesture.connect("released", self._on_viewer_pressed)
        self.stack.add_controller(self.click_gesture)
        self._set_view_gestures_enabled(True)
        self.connect("close-request", self._on_close_request)
        # Track viewer orientation so the date overlay can move out of the
        # image's way on landscape screens.
        self._date_landscape: bool | None = None
        self.add_tick_callback(self._on_date_orientation_tick)
        self.fullscreen()
        self.show_item()

    def _on_date_orientation_tick(self, _widget, _clock) -> bool:
        w = self.get_width()
        h = self.get_height()
        if w > 0 and h > 0:
            # Hysteresis to avoid flapping near the threshold.
            if self._date_landscape:
                landscape = w > h * 1.05
            else:
                landscape = w > h * 1.25
            if landscape != self._date_landscape:
                self._date_landscape = landscape
                self._apply_date_alignment(landscape)
        return True  # GLib.SOURCE_CONTINUE

    def _apply_date_alignment(self, landscape: bool) -> None:
        # Always horizontally centered between the title bar and the image,
        # regardless of orientation. (Landscape used to be right-aligned,
        # but the central placement is what the user actually wants.)
        self.date_revealer.set_halign(Gtk.Align.CENTER)
        self.date_revealer.set_margin_end(0)
        self.date_revealer.set_margin_start(0)

    def show_item(self) -> None:
        self._set_view_gestures_enabled(True)
        child = self.stack.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.stack.remove(child)
            child = next_child
        item = self.items[self.index]
        # Title bar stays empty; the filename floats at the bottom of the image.
        self.set_title("")
        self._update_filename_label(item)
        self._update_date_label(item)
        self._reset_zoom()
        self.zoom_view = None
        self.zoom_scroller = None
        self._rotation = 0
        self._current_display_path = None
        self._current_is_video = False
        self.header.set_visible(True)
        self._set_view_actions_visible(False)

        from .nextcloud import is_nc_path
        if is_nc_path(item.path):
            if not self.parent_window.is_nc_active():
                self._show_nc_blocked(item)
                return
            self.info_button.set_visible(True)
            spinner = Gtk.Spinner()
            spinner.start()
            spinner.set_size_request(32, 32)
            spinner_box = Gtk.Box()
            spinner_box.set_hexpand(True)
            spinner_box.set_vexpand(True)
            spinner_box.set_halign(Gtk.Align.CENTER)
            spinner_box.set_valign(Gtk.Align.CENTER)
            spinner_box.append(spinner)
            self.stack.add_child(spinner_box)
            threading.Thread(target=self._nc_download_worker, args=(item,), daemon=True).start()
            return

        if item.is_video:
            self._current_is_video = True
            self.delete_button.set_visible(True)
            self.info_button.set_visible(True)
            video = Gtk.Video.new_for_file(Gio.File.new_for_path(item.path))
            video.set_autoplay(True)
            self.stack.add_child(video)
            media = video.get_media_stream()
            if media is not None:
                media.connect("notify::prepared", self._on_media_prepared)
        else:
            self.delete_button.set_visible(True)
            self.info_button.set_visible(True)
            self.edit_button.set_visible(_PIL_OK)
            self.rotate_button.set_visible(True)
            self._current_display_path = item.path
            self._show_local_image(item.path)

    def _set_view_actions_visible(self, visible: bool) -> None:
        self.delete_button.set_visible(visible)
        self.info_button.set_visible(visible)
        self.edit_button.set_visible(visible and _PIL_OK and not self._current_is_video)
        self.rotate_button.set_visible(visible and not self._current_is_video)
        self.slideshow_button.set_visible(visible and not self._current_is_video)  # Slideshow only for images

    def _set_view_gestures_enabled(self, enabled: bool) -> None:
        phase = Gtk.PropagationPhase.CAPTURE if enabled else Gtk.PropagationPhase.NONE
        self.swipe_gesture.set_propagation_phase(phase)
        self.drag_gesture.set_propagation_phase(phase)
        self.zoom_gesture.set_propagation_phase(phase)
        self.click_gesture.set_propagation_phase(phase)

    def _show_nc_blocked(self, item: MediaItem) -> None:
        """Render a placeholder for an NC item when the NC connection is
        deactivated, then offer the user a one-shot or permanent reconnect."""
        msg = self.parent_window._(
            "This image is stored in your Nextcloud.\n"
            "The connection is currently disabled."
        )
        lbl = Gtk.Label(label=msg)
        lbl.set_justify(Gtk.Justification.CENTER)
        lbl.set_wrap(True)
        lbl.set_margin_top(24)
        lbl.set_margin_bottom(24)
        lbl.set_margin_start(24)
        lbl.set_margin_end(24)
        lbl.add_css_class("title-2")
        self.stack.add_child(lbl)
        self._set_view_actions_visible(False)
        # Defer the dialog until the placeholder is on screen so the user has
        # something to look at while choosing.
        GLib.idle_add(lambda: (self._prompt_nc_reconnect(item), GLib.SOURCE_REMOVE)[1])

    def _prompt_nc_reconnect(self, item: MediaItem) -> None:
        dialog = Adw.AlertDialog(
            heading=self.parent_window._("Nextcloud connection disabled"),
            body=self.parent_window._(
                "Enable the connection now so the image can be loaded."
            ),
        )
        dialog.add_response("cancel", self.parent_window._("Cancel"))
        dialog.add_response("once", self.parent_window._("Connect once"))
        dialog.add_response("permanent", self.parent_window._("Connect permanently"))
        dialog.set_default_response("permanent")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("permanent", Adw.ResponseAppearance.SUGGESTED)
        dialog.choose(self, None, self._on_nc_reconnect_done, item)

    def _on_nc_reconnect_done(self, dialog: Adw.AlertDialog, result, item: MediaItem) -> None:
        try:
            response = dialog.choose_finish(result)
        except Exception:
            response = "cancel"
        if response == "cancel":
            return
        # Snapshot the previous gate state so "Einmalig" can revert exactly to
        # what was there before we opened the connection for this one image.
        prev_session = self.parent_window._nc_session_active
        prev_enabled = self.parent_window.settings.nextcloud_enabled
        prev_session_setting = getattr(
            self.parent_window.settings, "nextcloud_session_active", True,
        )

        # Open the gate and make NC visible. "Dauerhaft" persists everything;
        # "Einmalig" only flips things in memory and reverts after the load.
        self.parent_window._nc_session_active = True
        self.parent_window.settings.nextcloud_enabled = True
        if response == "permanent":
            self.parent_window.settings.nextcloud_session_active = True
            self.parent_window.settings.save()
            self._nc_einmalig_revert = None
        else:
            # Remember to flip every flag back as soon as this one image has
            # finished loading. A second NC click then triggers the dialog again.
            self._nc_einmalig_revert = (prev_session, prev_enabled, prev_session_setting)

        # Reset the shared NC client so workers reconnect with current creds.
        old_client = self.parent_window._nc_thumb_shared_client
        self.parent_window._nc_thumb_shared_client = None
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass
        # The gallery's category nav was built without the Nextcloud entry —
        # add it back now that NC is active again.
        self.parent_window._rebuild_categories()
        # Re-render the current item — it'll go through the active NC path now.
        self.show_item()

    def _revert_einmalig_session(self) -> None:
        """If the user had only granted Einmalig consent, close the gate again
        once the one-shot download has finished."""
        revert = getattr(self, "_nc_einmalig_revert", None)
        if revert is None:
            return
        prev_session, prev_enabled, prev_session_setting = revert
        self._nc_einmalig_revert = None
        self.parent_window._nc_session_active = prev_session
        # Only roll back nextcloud_enabled if we actually flipped it (i.e. the
        # user hadn't toggled it on permanently before).
        if not prev_enabled:
            self.parent_window.settings.nextcloud_enabled = False
        # Persistent session-active stays exactly where the user left it before
        # the einmalig blip — Einmalig is a transient session grant only.
        self.parent_window.settings.nextcloud_session_active = prev_session_setting
        # Drop the shared NC client so background workers stop using it.
        old_client = self.parent_window._nc_thumb_shared_client
        self.parent_window._nc_thumb_shared_client = None
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass
        # If NC vanished from the gallery (master toggle was off before), the
        # nav has to be rebuilt to reflect that.
        self.parent_window._rebuild_categories()

    def _nc_download_worker(self, item) -> None:
        from .nextcloud import NextcloudClient, dav_path_from_nc
        settings = self.parent_window.settings
        pwd = settings.load_app_password()
        local = None
        if pwd:
            try:
                client = NextcloudClient(settings.nextcloud_url, settings.nextcloud_user, pwd)
                local = client.download_file(dav_path_from_nc(item.path))
            except Exception:
                pass
        GLib.idle_add(self._nc_show_loaded, item, local)

    def _nc_show_loaded(self, item, local_path: str | None) -> None:
        child = self.stack.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.stack.remove(child)
            child = next_child
        try:
            if local_path is None:
                lbl = Gtk.Label(label=self.parent_window._("Could not load file"))
                lbl.set_hexpand(True)
                lbl.set_vexpand(True)
                self.stack.add_child(lbl)
                return
            if item.is_video:
                self._current_is_video = True
                self.info_button.set_visible(True)
                video = Gtk.Video.new_for_file(Gio.File.new_for_path(local_path))
                video.set_autoplay(True)
                self.stack.add_child(video)
                media = video.get_media_stream()
                if media is not None:
                    media.connect("notify::prepared", self._on_media_prepared)
            else:
                self._current_display_path = local_path
                self._show_local_image(local_path)
                self.delete_button.set_visible(False)
                self.info_button.set_visible(True)
                self.edit_button.set_visible(_PIL_OK)
                self.rotate_button.set_visible(True)
        finally:
            # The one-shot consent expires now — next NC interaction re-asks.
            self._revert_einmalig_session()

    def _show_local_image(self, path: str) -> None:
        # Honor the EXIF Orientation tag so portrait phone shots aren't sideways.
        # GdkPixbuf.apply_embedded_orientation does this in one call. If the loader
        # can't handle the format (rare), fall back to the lazy filename-based path.
        picture: Gtk.Picture
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
            pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
            picture = Gtk.Picture.new_for_pixbuf(pixbuf)
        except Exception:
            picture = Gtk.Picture.new_for_filename(path)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_can_shrink(True)
        picture.set_hexpand(True)
        picture.set_vexpand(True)
        picture.set_size_request(0, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_width(False)
        scroller.set_propagate_natural_height(False)
        scroller.set_child(picture)
        self.zoom_view = picture
        self.zoom_scroller = scroller
        self.stack.add_child(scroller)

    def _rotate_by_step(self, steps: int) -> None:
        """Rotate the displayed image by *steps* * 90° (positive = clockwise)."""
        if self._current_display_path is None or steps == 0:
            return
        self._rotation = (self._rotation + 90 * steps) % 360
        child = self.stack.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.stack.remove(child)
            child = nxt
        self._reset_zoom()
        self.zoom_view = None
        self.zoom_scroller = None
        spinner = Gtk.Spinner()
        spinner.start()
        spinner.set_size_request(32, 32)
        box = Gtk.Box()
        box.set_hexpand(True)
        box.set_vexpand(True)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.append(spinner)
        self.stack.add_child(box)
        path = self._current_display_path
        rotation = self._rotation
        threading.Thread(
            target=lambda: self._rotate_worker(path, rotation),
            daemon=True,
        ).start()

    def _rotate_worker(self, path: str, rotation: int) -> None:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
            # Apply EXIF orientation first so the user's rotation stacks on top
            # of the already-corrected display (matches _show_local_image).
            pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
            rot_map = {
                90: GdkPixbuf.PixbufRotation.CLOCKWISE,
                180: GdkPixbuf.PixbufRotation.UPSIDEDOWN,
                270: GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE,
            }
            if rotation in rot_map:
                pixbuf = pixbuf.rotate_simple(rot_map[rotation])
            GLib.idle_add(self._show_rotated_pixbuf, pixbuf)
        except Exception as e:
            LOGGER.exception("Could not rotate image: %s", e)
            GLib.idle_add(self._show_rotated_pixbuf, None)

    def _show_rotated_pixbuf(self, pixbuf) -> None:
        child = self.stack.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.stack.remove(child)
            child = nxt
        self._reset_zoom()
        self.zoom_view = None
        self.zoom_scroller = None
        if pixbuf is not None:
            picture = Gtk.Picture.new_for_pixbuf(pixbuf)
        else:
            picture = Gtk.Picture.new_for_filename(self._current_display_path or "")
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_can_shrink(True)
        picture.set_hexpand(True)
        picture.set_vexpand(True)
        # Don't let the rotated pixbuf's natural dimensions push the layout
        # past the screen — explicitly request 0,0 so the picture only takes
        # what its parent allocation gives it.
        picture.set_size_request(0, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        # Critical: never let the scroller propagate the picture's natural size
        # upward, otherwise after a portrait → landscape rotation the toolbar
        # tries to grow wider than the (already fullscreened) window.
        scroller.set_propagate_natural_width(False)
        scroller.set_propagate_natural_height(False)
        scroller.set_child(picture)
        self.zoom_view = picture
        self.zoom_scroller = scroller
        self.stack.add_child(scroller)
        # Force a resize pass: removing + adding stack children doesn't always
        # reset cached size negotiation, so we nudge the toolbar/window once.
        self.queue_resize()
        if not self.props.fullscreened:
            self.fullscreen()

    def _on_media_prepared(self, media_stream, _param=None) -> None:
        w = media_stream.get_intrinsic_width()
        h = media_stream.get_intrinsic_height()
        if w > 0 and h > 0 and w > h:
            def _hide():
                self.header.set_visible(False)
                self.date_revealer.set_reveal_child(False)
                self.filename_revealer.set_reveal_child(False)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(_hide)

    def _update_date_label(self, item: MediaItem) -> None:
        try:
            dt = datetime.fromtimestamp(item.mtime)
        except (OverflowError, OSError, ValueError):
            self.date_revealer.set_reveal_child(False)
            return
        # Locale-aware day-month, e.g. "1 Mai" with LC_TIME=de_DE
        self.date_day_label.set_label(dt.strftime("%-d %B"))
        self.date_year_label.set_label(dt.strftime("%Y"))
        self.date_revealer.set_reveal_child(not self._current_is_video)

    def _update_filename_label(self, item: MediaItem) -> None:
        self.filename_label.set_label(item.name or "")
        self.filename_revealer.set_reveal_child(bool(item.name) and not self._current_is_video)

    def _on_close_request(self, _window) -> bool:
        # Stop slideshow before closing
        if self._slideshow_active:
            self._stop_slideshow()

        if self._rotation != 0:
            self._check_rotation_before_action(self.destroy)
            return True
        if self.props.fullscreened:
            self.unfullscreen()
        parent = self.parent_window
        GLib.idle_add(lambda: (parent.present(), GLib.SOURCE_REMOVE)[1])
        return False

    def _check_rotation_before_action(self, action) -> None:
        if self._rotation == 0:
            action()
            return
        _ = self.parent_window._
        dialog = Adw.AlertDialog(
            heading=_("Save rotation?"),
            body=_("The image has been rotated. Save the change?"),
        )
        dialog.add_response("discard", _("Discard"))
        dialog.add_response("save", _("Save"))
        dialog.add_response("cancel", _("Cancel"))
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.choose(self, None, self._rotation_dialog_done, action)

    def _rotation_dialog_done(self, dialog, result, action) -> None:
        response = dialog.choose_finish(result)
        if response == "cancel":
            return
        if response == "save":
            self._save_rotation()
        self._rotation = 0
        action()

    def _save_rotation(self) -> None:
        path = self._current_display_path
        if not path or self._rotation == 0:
            return
        if _PIL_OK:
            try:
                from PIL import ImageOps
                img = PILImage.open(path)
                # Bake EXIF orientation into the pixels first so the saved file is
                # standalone-correct (no orientation tag needed), then layer the
                # user's rotation on top.
                img = ImageOps.exif_transpose(img)
                img = img.rotate(-self._rotation, expand=True)
                ext = Path(path).suffix.lower()
                if ext in (".jpg", ".jpeg"):
                    img.save(path, quality=95)
                else:
                    img.save(path)
                return
            except Exception:
                LOGGER.exception("PIL save_rotation failed for %s", path)
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
            pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
            rot_map = {
                90: GdkPixbuf.PixbufRotation.CLOCKWISE,
                180: GdkPixbuf.PixbufRotation.UPSIDEDOWN,
                270: GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE,
            }
            rotated = pixbuf.rotate_simple(rot_map[self._rotation])
            ext = Path(path).suffix.lower()
            fmt = "jpeg" if ext in (".jpg", ".jpeg") else "png"
            rotated.savev(path, fmt, [], [])
        except Exception:
            pass

    def _do_previous(self) -> None:
        self._step(-1)

    def _do_next(self) -> None:
        self._step(1)

    def _step(self, direction: int) -> None:
        """Advance by *direction* (+1 / -1), skipping videos when the current
        item is an image so picture browsing is not interrupted by video clips."""
        if not self.items:
            return
        n = len(self.items)
        skip_videos = not self.items[self.index].is_video
        new_index = (self.index + direction) % n
        if skip_videos:
            for _ in range(n):
                if not self.items[new_index].is_video:
                    break
                new_index = (new_index + direction) % n
            else:
                # Only videos in the list — keep current item.
                return
        self.index = new_index
        self.show_item()

    def previous(self) -> None:
        self._check_rotation_before_action(self._do_previous)

    def next(self) -> None:
        self._check_rotation_before_action(self._do_next)

    def _on_key(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType) -> bool:
        if self._editor is not None:
            if keyval == Gdk.KEY_Escape:
                self._exit_edit_mode()
                return True
            return False
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up):
            self.previous()
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_space):
            self.next()
            return True
        if keyval == Gdk.KEY_F11:
            self._toggle_fullscreen()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.props.fullscreened:
                self._toggle_fullscreen()
            else:
                self.close()
            return True
        return False

    def _on_swipe(self, _gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        if self._editor is not None:
            return
        self._navigate_from_horizontal_motion(velocity_x, velocity_y)

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        if self._editor is not None:
            return
        self._navigate_from_horizontal_motion(offset_x, offset_y)

    def _navigate_from_horizontal_motion(self, x: float, y: float) -> None:
        if self.zoom_scale > 1.05:
            return
        # Don't navigate if a pinch-zoom gesture is the real intent
        if self._zoom_committed:
            return
        if abs(x) < 90 or abs(x) <= abs(y) * 1.8:
            return
        now = GLib.get_monotonic_time()
        if now - self.last_gesture_nav_at < 300_000:
            return
        self.last_gesture_nav_at = now
        if x > 0:
            self.previous()
        else:
            self.next()

    def _on_zoom_begin(self, gesture: Gtk.GestureZoom, _sequence) -> None:
        self.zoom_start_scale = self.zoom_scale
        self._zoom_committed = False
        self._zoom_anchor: tuple[float, float, float, float] | None = None
        if self.zoom_scroller:
            ok, bx, by = gesture.get_bounding_box_center()
            if ok:
                hadj = self.zoom_scroller.get_hadjustment()
                vadj = self.zoom_scroller.get_vadjustment()
                s = max(self.zoom_scale, 0.01)
                self._zoom_anchor = (bx, by, (hadj.get_value() + bx) / s, (vadj.get_value() + by) / s)

    def _on_zoom_scale_changed(self, _gesture: Gtk.GestureZoom, scale_delta: float) -> None:
        if not self._zoom_committed:
            # 1% pinch is enough to commit zoom; below that the gesture is
            # treated as a stray two-finger touch.
            if 0.99 <= scale_delta <= 1.01:
                return
            self._zoom_committed = True
        self._set_zoom(self.zoom_start_scale * scale_delta)
        anchor = getattr(self, "_zoom_anchor", None)
        if self.zoom_scroller and anchor and self.zoom_scale > 1.01:
            vp_x, vp_y, cx, cy = anchor
            scale = self.zoom_scale
            scroller = self.zoom_scroller

            def _apply() -> bool:
                self._set_adjustment_for_focus(scroller.get_hadjustment(), cx, scale, vp_x)
                self._set_adjustment_for_focus(scroller.get_vadjustment(), cy, scale, vp_y)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_apply)

    def _on_viewer_press_begin(self, _gesture, _n_press: int, x: float, y: float) -> None:
        """Stash press coordinates so the released handler can tell a real
        tap apart from a swipe."""
        self._click_press_x = x
        self._click_press_y = y

    def _on_viewer_pressed(self, _gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        # Skip if the click was actually a pinch — set_exclusive normally
        # filters this, but on some hardware the click still fires before
        # the second touch arrives.
        if self._zoom_committed:
            return
        # Treat anything with > ~12 px movement as a swipe/drag, not a tap.
        dx = abs(x - self._click_press_x)
        dy = abs(y - self._click_press_y)
        if dx > 12 or dy > 12:
            return
        if n_press == 1:
            # Single tap toggles the floating overlays (date pill, filename
            # pill, header) — slides them out so the user can see the image
            # uncluttered, slides them back in on the next tap.
            visible = not self.header.get_visible()
            self.header.set_visible(visible)
            if not self._current_is_video:
                self.date_revealer.set_reveal_child(visible)
                self.filename_revealer.set_reveal_child(visible)
            else:
                # On videos the pills aren't shown anyway; only the header
                # toggles so the user can hide playback chrome.
                self.date_revealer.set_reveal_child(False)
                self.filename_revealer.set_reveal_child(False)
        elif not self._current_is_video and n_press == 2:
            self._reset_zoom()

    def _set_zoom(self, scale: float) -> None:
        self.zoom_scale = min(max(scale, 1.0), 6.0)
        self._apply_zoom()

    def _reset_zoom(self) -> None:
        self.zoom_scale = 1.0
        self.zoom_start_scale = 1.0
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        if not self.zoom_view or not self.zoom_scroller:
            return
        if self.zoom_scale <= 1.01:
            self.zoom_view.set_size_request(-1, -1)
            return
        width = max(self.zoom_scroller.get_width(), self.get_width(), 1)
        height = max(self.zoom_scroller.get_height(), self.get_height(), 1)
        self.zoom_view.set_size_request(int(width * self.zoom_scale), int(height * self.zoom_scale))

    def _set_adjustment_for_focus(self, adjustment: Gtk.Adjustment, content_pos: float, scale: float, focus_pos: float) -> None:
        target = content_pos * scale - focus_pos
        lower = adjustment.get_lower()
        upper = max(lower, adjustment.get_upper() - adjustment.get_page_size())
        adjustment.set_value(min(max(target, lower), upper))

    def _enter_edit_mode(self, _button=None) -> None:
        if not _PIL_OK:
            self.parent_window._set_status(self.parent_window._("Could not open editor"))
            return
        
        # Stop slideshow when entering edit mode
        if self._slideshow_active:
            self._stop_slideshow()
        
        item = self.items[self.index]
        if item.is_video:
            return
        
        # Check if it's a RAW image (not editable with PIL)
        from .models import RAW_EXTENSIONS
        if Path(item.path).suffix.lower() in RAW_EXTENSIONS:
            self.parent_window._set_status(self.parent_window._("RAW images cannot be edited with the built-in editor"))
            return
        
        edit_path = self._current_display_path or item.path
        if is_nc_path(edit_path) or not Path(edit_path).exists():
            self.parent_window._set_status(self.parent_window._("Could not open editor"))
            return
        edit_item = dataclasses.replace(item, path=edit_path)
        self._set_view_gestures_enabled(False)
        self.header.set_show_end_title_buttons(False)
        self.header.set_show_start_title_buttons(False)
        self.header.set_visible(True)
        self.close_button.set_visible(False)
        self.delete_button.set_visible(False)
        self.info_button.set_visible(False)
        self.edit_button.set_visible(False)
        self.rotate_button.set_visible(False)
        self.cancel_edit_button.set_visible(True)
        self.save_edit_button.set_visible(True)
        self.undo_edit_button.set_visible(True)
        self.redo_edit_button.set_visible(True)
        # In landscape, fold the filename + date into the title bar so the
        # editor gets every available pixel. In portrait the title bar would
        # truncate aggressively, so we instead float the filename at the *top*
        # of the editor (with the same black-pill look as before).
        try:
            dt = datetime.fromtimestamp(item.mtime)
            date_str = dt.strftime('%-d %B %Y')
        except (OverflowError, OSError, ValueError):
            date_str = ""
        self.date_revealer.set_reveal_child(False)
        if self._date_landscape:
            self.set_title(
                f"{item.name}  ·  {date_str}" if date_str else item.name
            )
            self.filename_revealer.set_reveal_child(False)
        else:
            self.set_title("")
            self.filename_label.set_label(item.name or "")
            self.filename_revealer.set_valign(Gtk.Align.START)
            self.filename_revealer.set_margin_top(20)
            self.filename_revealer.set_margin_bottom(0)
            self.filename_revealer.set_reveal_child(bool(item.name))
        child = self.stack.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.stack.remove(child)
            child = nxt
        try:
            self._editor = EditorView(edit_item, self.parent_window._)
        except Exception as exc:
            LOGGER.exception("Could not open editor: %s", exc)
            # Show informative error dialog
            dialog = Adw.AlertDialog(
                heading=self.parent_window._("Could not open editor"),
                body=self.parent_window._("The image editor could not start. This may be due to insufficient memory or unsupported image format."),
            )
            dialog.add_response("close", self.parent_window._("Close"))
            dialog.present(self.get_root())
            self._set_view_gestures_enabled(True)
            self.show_item()
            return
        self.stack.add_child(self._editor)
        self.stack.set_visible_child(self._editor)
        self._update_edit_buttons()  # Update undo/redo button states

    def _exit_edit_mode(self, _button=None) -> None:
        self._editor = None
        self._set_view_gestures_enabled(True)
        self.header.set_show_end_title_buttons(False)
        self.header.set_show_start_title_buttons(False)
        self.close_button.set_visible(True)
        self.cancel_edit_button.set_visible(False)
        self.save_edit_button.set_visible(False)
        self.undo_edit_button.set_visible(False)
        self.redo_edit_button.set_visible(False)
        # Restore the filename pill back to its default bottom position; the
        # editor may have moved it to the top in portrait mode.
        self.filename_revealer.set_valign(Gtk.Align.END)
        self.filename_revealer.set_margin_top(0)
        self.filename_revealer.set_margin_bottom(20)
        # Title + date overlay are restored by show_item() below.
        self.show_item()

    def _save_edit(self, _button: Gtk.Button) -> None:
        if self._editor is None:
            return
        self.save_edit_button.set_sensitive(False)
        self.cancel_edit_button.set_sensitive(False)
        editor = self._editor
        threading.Thread(target=self._save_edit_worker, args=(editor,), daemon=True).start()

    def _save_edit_worker(self, editor) -> None:
        try:
            local_path = editor.save_as_new()
            
            # Check if original file is from Nextcloud and upload if needed
            if is_nc_path(editor._item.path):
                self._upload_to_nextcloud(local_path)
            
            GLib.idle_add(self._save_edit_done, True)
        except Exception as exc:
            LOGGER.exception("Could not save edited image: %s", exc)
            GLib.idle_add(self._save_edit_done, False)

    def _save_edit_done(self, success: bool) -> None:
        self.save_edit_button.set_sensitive(True)
        self.cancel_edit_button.set_sensitive(True)
        if not success:
            self.parent_window._set_status(self.parent_window._("Could not save edited image"))
            return
        self.parent_window.refresh(scan=True)
        self._exit_edit_mode()

    def _undo_edit(self, _button: Gtk.Button) -> None:
        """Undo last edit in the editor."""
        if self._editor is None:
            return
        self._editor.undo()
        self._update_edit_buttons()

    def _redo_edit(self, _button: Gtk.Button) -> None:
        """Redo last undone edit in the editor."""
        if self._editor is None:
            return
        self._editor.redo()
        self._update_edit_buttons()

    def _update_edit_buttons(self) -> None:
        """Update undo/redo button sensitivity based on editor state."""
        if self._editor is None:
            return
        self.undo_edit_button.set_sensitive(self._editor.can_undo())
        self.redo_edit_button.set_sensitive(self._editor.can_redo())

    def _upload_to_nextcloud(self, local_edited_path: str) -> None:
        """Upload edited image back to Nextcloud."""
        from .nextcloud import NextcloudClient, dav_path_from_nc, NC_PATH_PREFIX
        
        if self._editor is None:
            return
        
        # Get Nextcloud credentials from settings
        settings = self.parent_window.settings
        if not settings.nextcloud_url or not settings.nextcloud_user:
            LOGGER.warning("Nextcloud credentials not configured")
            return
        
        # Get Nextcloud password from system keyring
        try:
            pwd = settings.load_app_password()
            if not pwd:
                LOGGER.warning("Nextcloud password not available")
                return
        except Exception as exc:
            LOGGER.warning("Could not retrieve Nextcloud password: %s", exc)
            return
        
        try:
            client = NextcloudClient(settings.nextcloud_url, settings.nextcloud_user, pwd)
            
            # Get the original DAV path
            original_dav_path = dav_path_from_nc(self._editor._item.path)
            
            # Upload to the same location
            success = client.upload_file(local_edited_path, original_dav_path)
            
            if success:
                LOGGER.info("Successfully uploaded edited image to Nextcloud")
                # Optionally update UI to show success
            else:
                LOGGER.warning("Failed to upload edited image to Nextcloud")
        except Exception as exc:
            LOGGER.exception("Error uploading to Nextcloud: %s", exc)


    def _toggle_fullscreen(self, _btn=None) -> None:
        if self.props.fullscreened:
            self.unfullscreen()
        else:
            self.fullscreen()

    def _confirm_delete_current(self, _button: Gtk.Button) -> None:
        dialog = Adw.AlertDialog(
            heading=self.parent_window._("Delete media?"),
            body=self.parent_window._("Delete this item from the gallery?"),
        )
        dialog.add_response("cancel", self.parent_window._("Cancel"))
        dialog.add_response("delete", self.parent_window._("Delete"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.choose(self, None, self._delete_dialog_finished, None)

    def _delete_dialog_finished(self, dialog: Adw.AlertDialog, result: Gio.AsyncResult, _data) -> None:
        if dialog.choose_finish(result) == "delete":
            self._delete_current_item()

    def _delete_current_item(self) -> None:
        if not self.items:
            self.close()
            return
        item = self.items[self.index]
        try:
            Gio.File.new_for_path(item.path).trash(None)
        except GLib.Error:
            self.parent_window._set_status(self.parent_window._("Could not complete action"))
            return
        if item.thumb_path:
            try:
                Path(item.thumb_path).unlink(missing_ok=True)
            except OSError:
                pass
        self.parent_window.database.delete_path(item.path, item.category)
        self.items.pop(self.index)
        self.parent_window.refresh(scan=False)
        if not self.items:
            self.close()
            return
        self.index = min(self.index, len(self.items) - 1)
        self.show_item()

    def _get_cached_exif(self, item: MediaItem) -> dict[str, str]:
        """Get EXIF data from cache (DB) or parse and cache if missing."""
        import json
        # Try to get cached EXIF from database
        cached_json = self.parent_window.database.get_exif_data(item.path, item.category)
        if cached_json:
            try:
                return json.loads(cached_json)
            except (json.JSONDecodeError, TypeError):
                pass
        
        # If not cached or corrupted, parse and cache
        exif = _extract_exif(item.path)
        if exif:
            try:
                exif_json = json.dumps(exif)
                self.parent_window.database.set_exif_data(item.path, exif_json, item.category)
                self.parent_window.database.commit()
            except Exception:
                # If caching fails, still return the parsed EXIF
                pass
        return exif

    def _show_info(self, _button: Gtk.Button) -> None:
        item = self.items[self.index]
        _ = self.parent_window._

        rows: list[tuple[str, str]] = [
            (_("Name"), item.name),
            (_("Folder"), item.folder),
            (_("Size"), _fmt_size(item.size)),
            (_("Modified"), _fmt_date(item.mtime)),
        ]
        if not item.is_video:
            dims = _image_dimensions(item.path)
            if dims:
                rows.append((_("Dimensions"), dims))
            exif = self._get_cached_exif(item)
            for key in ("Camera", "GPS"):
                if key in exif:
                    rows.append((_(key), exif[key]))

        grid = Gtk.Grid()
        grid.set_column_spacing(20)
        grid.set_row_spacing(8)
        grid.set_margin_top(14)
        grid.set_margin_bottom(14)
        grid.set_margin_start(16)
        grid.set_margin_end(16)
        for i, (key, value) in enumerate(rows):
            key_lbl = Gtk.Label(label=key, xalign=1.0)
            key_lbl.add_css_class("dim-label")
            val_lbl = Gtk.Label(label=value, xalign=0.0)
            is_name = (i == 0)
            val_lbl.set_selectable(not is_name)
            val_lbl.set_wrap(True)
            val_lbl.set_max_width_chars(32)
            grid.attach(key_lbl, 0, i, 1, 1)
            grid.attach(val_lbl, 1, i, 1, 1)

        popover = Gtk.Popover()
        popover.set_parent(self.info_button)
        popover.set_child(grid)
        popover.popup()

    def _toggle_slideshow(self, _button: Gtk.Button) -> None:
        """Toggle slideshow mode on/off."""
        if self._slideshow_active:
            self._stop_slideshow()
        else:
            self._start_slideshow()

    def _start_slideshow(self) -> None:
        """Start automatic slideshow."""
        self._slideshow_active = True
        self.slideshow_button.set_icon_name("media-playback-pause-symbolic")
        self.slideshow_button.set_tooltip_text(self.parent_window._("Stop slideshow"))
        self._schedule_next_slide()

    def _stop_slideshow(self) -> None:
        """Stop automatic slideshow."""
        self._slideshow_active = False
        self.slideshow_button.set_icon_name("media-playback-start-symbolic")
        self.slideshow_button.set_tooltip_text(self.parent_window._("Start slideshow"))
        if self._slideshow_timeout_id is not None:
            GLib.source_remove(self._slideshow_timeout_id)
            self._slideshow_timeout_id = None

    def _schedule_next_slide(self) -> None:
        """Schedule the next slide transition."""
        if not self._slideshow_active:
            return
        self._slideshow_timeout_id = GLib.timeout_add(
            self._slideshow_interval_ms,
            self._on_slideshow_tick,
        )

    def _on_slideshow_tick(self) -> bool:
        """Called on slideshow timer tick. Advance to next image or loop."""
        if not self._slideshow_active:
            return GLib.SOURCE_REMOVE
        
        # If current item is video, skip it (show next static image)
        if self._current_is_video:
            self.index = (self.index + 1) % len(self.items)
        else:
            # Advance to next
            self.index = (self.index + 1) % len(self.items)
        
        self.show_item()
        self._schedule_next_slide()
        return GLib.SOURCE_REMOVE
