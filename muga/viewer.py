from __future__ import annotations

import collections
import dataclasses
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from .camera_orientation import (
    ORIENT_BOTTOM_UP,
    ORIENT_LEFT_UP,
    ORIENT_NORMAL,
    ORIENT_RIGHT_UP,
    OrientationClient,
)
from . import exif as exif_module
from .editor import EditorView, PILImage, _PIL_OK
from .models import MediaItem
from .nextcloud import is_nc_path
from .rotated_container import RotatedContainer
from .gtk_util import idle_once, texture_from_pixbuf
from .thumbnails import image_within_pixel_budget

if TYPE_CHECKING:
    from .app import GalleryWindow

LOGGER = logging.getLogger(__name__)


def _write_in_place_atomic(path: str, write) -> None:
    """Run ``write(tmp_path)`` and only then replace *path* with the result.

    Rotation is the one place where we overwrite a user's original photo, so
    it must never be a partial write: a save that dies mid-stream (ENOSPC, a
    flat battery, an OOM kill) would otherwise leave a truncated file where
    an irreplaceable photo used to be — and the GdkPixbuf fallback below
    would then be handed that corpse to re-encode. Writing beside the target
    and swapping via os.replace keeps the original intact until a *complete*
    new file exists. Mirrors thumbnails._save_atomic, plus the mode/owner
    copy the original file deserves.
    """
    target = Path(path)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        write(str(tmp))
        # os.replace takes the tmp file's permissions with it, so carry the
        # original's mode over first — otherwise a 0600 photo silently
        # widens to the process umask on every rotation.
        try:
            st = target.stat()
            os.chmod(tmp, st.st_mode & 0o7777)
            if hasattr(os, "chown"):
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except (OSError, PermissionError):
                    LOGGER.debug("os.chown failed", exc_info=True)
        except OSError:
            # Best effort: the replacement file keeps default ownership/mode.
            LOGGER.debug("could not copy owner/mode onto %s", tmp, exc_info=True)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("tmp.unlink failed", exc_info=True)
        raise

# Maps the 4-state device orientation reported by the accelerometer to the
# rotation (degrees) that keeps the photo upright in the user's view. Mirrors
# the camera viewfinder's mapping (camera_orientation._ICON_ROTATION_DEG) so a phone held
# in landscape shows the picture the same way the camera previews it — the X
# axis is inverted relative to the Android convention to match this HAL.
_SENSOR_ROTATION_DEG = {
    ORIENT_NORMAL: 0,
    ORIENT_BOTTOM_UP: 180,
    ORIENT_LEFT_UP: 270,
    ORIENT_RIGHT_UP: 90,
}


def _motion_in_view_space(x: float, y: float, sensor_angle: int) -> tuple[float, float]:
    """Map a gesture delta from window space into the space the user sees.

    The viewer's swipe/drag controllers sit on the stack, so their deltas come
    in window coordinates — but `RotatedContainer` turns the picture inside
    that window when the device is held sideways with auto-rotate off. Without
    undoing that rotation, "next photo" follows the window's x axis, which the
    user experiences as up/down. Inverse of the transforms in
    `RotatedContainer.do_size_allocate`.
    """
    if sensor_angle == 90:
        return y, -x
    if sensor_angle == 180:
        return -x, -y
    if sensor_angle == 270:
        return -y, x
    return x, y


def _fmt_size(size: int) -> str:
    scaled = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if scaled < 1024:
            return f"{scaled:.0f} {unit}"
        scaled /= 1024
    return f"{scaled:.1f} TB"


def _fmt_date(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d  %H:%M")


def _image_dimensions(path: str) -> str | None:
    fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(path)
    if fmt is not None:
        return f"{w} × {h}"
    return None


def _exif_for_upright_save(img) -> bytes | None:
    """Return `img`'s EXIF block with Orientation normalised to 1, ready to be
    written back after a rotation was baked into the pixels.

    Saving without this drops the entire metadata block — capture date, camera
    model, GPS. It also costs the photo a protection it silently relies on:
    Delta Chat's core (src/blob.rs) only demotes an attachment from image to
    plain file when recoding fails *and* the file carries no EXIF at all, so a
    JPEG that lost its metadata gets sent as a file attachment instead of a
    picture.

    When the source has no EXIF of its own (an older Muga photo that already
    went through this path, back when it stripped them) a minimal block is
    written instead. Nothing is invented: Orientation=1 states that the pixels
    are upright, and Software names the app that wrote them."""
    if not _PIL_OK or PILImage is None:
        return None
    try:
        exif = PILImage.Exif()
        data = img.info.get("exif")
        if data:
            exif.load(data)
        else:
            exif[0x0131] = "Muga"  # Software
        exif[0x0112] = 1  # Orientation: pixels are already the right way up
        return exif.tobytes()
    except Exception:
        LOGGER.debug("Could not rebuild EXIF for rotated save", exc_info=True)
        return None


def _extract_exif(path: str) -> dict[str, str]:
    """The displayable EXIF fields for the info popover.

    The parser itself lives in muga.exif now: the scanner needs the same one
    to fill the index for every file, not just the ones opened here. Kept as a
    named function because the popover and its tests reach for it directly.
    """
    return exif_module.extract_fields(path)


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
        # Deferred close/navigate action handed to the rotation worker.
        self._pending_rotation_action = None
        self._current_display_path: str | None = None
        self._current_is_video: bool = False

        # Sensor-driven *display* rotation: when the phone is physically turned
        # but its orientation is locked (auto-rotate off), the window stays put,
        # so we rotate the picture ourselves to keep it upright for the user.
        # This is purely visual and never saved — distinct from the manual
        # `_rotation` above, which is a persisted edit. `_sensor_rotator` wraps
        # the on-screen scroller and is rebuilt on every show_item()/mount.
        self._sensor_angle: int = 0
        self._sensor_rotator: RotatedContainer | None = None
        self._orientation_client = OrientationClient()

        # Off-thread image decoding. new_from_file() on a 48 MP photo blocks the
        # UI thread for hundreds of ms on every swipe; we decode (downscaled,
        # EXIF-oriented) on a small pool and apply the result via idle_add.
        # _show_token tags each show_item() so a decode that lands after the
        # user has already navigated away is discarded. A tiny LRU keeps the
        # current image and its two neighbours decoded, so back-and-forth
        # swiping is instant; neighbours are prefetched after each show.
        self._image_cache: collections.OrderedDict[str, GdkPixbuf.Pixbuf] = collections.OrderedDict()
        self._IMAGE_CACHE_MAX = 3
        # Cap the long edge so a huge source doesn't pin hundreds of MB per
        # cached image; still far above screen resolution for crisp zoom.
        self._DECODE_MAX_DIM = 4096
        self._decode_pool: ThreadPoolExecutor | None = None
        self._show_token = 0
        # Chrome (header + date pill + filename pill) visibility persists
        # across swipe-to-next so the user's tap-to-hide choice carries
        # over to the next image instead of reverting on every show_item().
        self._chrome_visible: bool = True

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

        # Edit-mode-only undo/redo. Sensitivity is driven by the editor's
        # history-changed callback (see _enter_edit_mode), so the buttons
        # grey out the moment the corresponding stack empties.
        self.undo_button = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        self.undo_button.set_tooltip_text(parent._("Undo"))
        self.undo_button.connect("clicked", self._on_editor_undo)
        self.undo_button.set_visible(False)
        self.undo_button.set_sensitive(False)
        header.pack_start(self.undo_button)

        self.redo_button = Gtk.Button.new_from_icon_name("edit-redo-symbolic")
        self.redo_button.set_tooltip_text(parent._("Redo"))
        self.redo_button.connect("clicked", self._on_editor_redo)
        self.redo_button.set_visible(False)
        self.redo_button.set_sensitive(False)
        header.pack_start(self.redo_button)

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

        self.share_button = Gtk.Button.new_from_icon_name("folder-publicshare-symbolic")
        self.share_button.set_tooltip_text(parent._("Share"))
        self.share_button.connect("clicked", self._on_share_clicked)
        self.share_button.set_visible(False)
        header.pack_end(self.share_button)

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
        # Set once the window is torn down. Background workers (NC download,
        # rotation, edit-save) finish on their own thread and bounce results
        # back via GLib.idle_add; if the user closed the viewer meanwhile, the
        # callbacks must not touch the now-defunct widget tree.
        self._closing = False
        self.connect("destroy", self._on_destroy)
        # Track viewer orientation so the date overlay can move out of the
        # image's way on landscape screens.
        self._date_landscape: bool | None = None
        self.add_tick_callback(self._on_date_orientation_tick)
        self.fullscreen()
        self.show_item()
        # Follow the physical device orientation so a held-sideways phone shows
        # the photo upright even with auto-rotate off. Callbacks arrive on the
        # GLib main loop (D-Bus / socket watch), so touching widgets is safe.
        # If no accelerometer is available start() is a no-op and we stay at 0°.
        self._orientation_client.start(on_change=self._on_device_orientation_changed)

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

    def _on_device_orientation_changed(self, orientation: str) -> None:
        """Accelerometer reported a new device orientation — rotate the picture
        to match so it stays upright in the user's hand. Always active in the
        viewer; no setting gates it."""
        angle = _SENSOR_ROTATION_DEG.get(orientation, 0)
        if angle == self._sensor_angle:
            return
        self._sensor_angle = angle
        # A pinch-zoom anchored to the old orientation no longer maps cleanly
        # once the axes swap — reset to a clean fit before rotating.
        self._reset_zoom()
        if self._sensor_rotator is not None:
            self._sensor_rotator.set_rotation(angle)

    def _mount_rotatable(self, scroller: Gtk.ScrolledWindow) -> None:
        """Place *scroller* into the stack wrapped in a RotatedContainer that
        tracks the current device orientation. Centralises the wrap so every
        picture-mount path (initial, rotate, …) follows the sensor."""
        rotator = RotatedContainer()
        rotator.set_hexpand(True)
        rotator.set_vexpand(True)
        rotator.set_child(scroller)
        rotator.set_rotation(self._sensor_angle)
        self._sensor_rotator = rotator
        self.stack.add_child(rotator)

    def show_item(self) -> None:
        # Bump the token so any in-flight decode for the previous item is
        # discarded when it lands instead of painting over the new one.
        self._show_token += 1
        self._set_view_gestures_enabled(True)
        child = self.stack.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.stack.remove(child)
            child = next_child
        item = self.items[self.index]
        # Title bar stays empty; the filename floats at the bottom of the image.
        self.set_title("")
        self._reset_zoom()
        self.zoom_view = None
        self.zoom_scroller = None
        self._sensor_rotator = None
        self._rotation = 0
        self._current_display_path = None
        self._current_is_video = False
        # Reapply the user's last chrome choice — show_item() runs on every
        # swipe and on hard reloads (rotate save, edit exit, …); without
        # this the header + pills snap back into view on every navigation.
        self._apply_chrome_visibility()
        self._update_filename_label(item)
        self._update_date_label(item)
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
        # Share is image-only — videos can't be sent as e-mail attachments
        # in any sane size, and the dialog uses xdg-email which only knows
        # how to attach files (not stream videos).
        self.share_button.set_visible(visible and not self._current_is_video)

    def _on_share_clicked(self, _btn: Gtk.Button) -> None:
        if not self.items:
            return
        item = self.items[self.index]
        # Route through the gallery's shared dialog so the viewer share
        # button and the selection-mode share button look identical and
        # gain new methods (cloud, social, …) in lockstep later.
        self.parent_window.open_share_dialog([item.path])

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
        idle_once(self._prompt_nc_reconnect, item)

    def _prompt_nc_reconnect(self, item: MediaItem) -> None:
        if self._closing:
            return
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
                LOGGER.debug("old_client.close failed", exc_info=True)
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
                LOGGER.debug("old_client.close failed", exc_info=True)
        # If NC vanished from the gallery (master toggle was off before), the
        # nav has to be rebuilt to reflect that.
        self.parent_window._rebuild_categories()

    def _nc_download_worker(self, item) -> None:
        from .nextcloud import NextcloudClient, dav_path_from_nc
        local = None
        try:
            settings = self.parent_window.settings
            pwd = settings.load_app_password()
            if pwd:
                try:
                    client = NextcloudClient(settings.nextcloud_url, settings.nextcloud_user, pwd)
                    local = client.download_file(
                        dav_path_from_nc(item.path), remote_mtime=item.mtime,
                    )
                except Exception:
                    LOGGER.debug("NC download failed for %s", item.path, exc_info=True)
            if local:
                # Each viewed Nextcloud photo lands in the cache full-size and
                # never expires on its own; give the (throttled, no-op when
                # unbudgeted) evictor a chance to keep it in bounds.
                GLib.idle_add(self.parent_window.evict_cache_async)
        except Exception:
            LOGGER.exception("NC download worker crashed for %s", getattr(item, "path", "?"))
        finally:
            # _nc_show_loaded owns the spinner/placeholder teardown; skipping it
            # would leave the viewer on a permanent loading state.
            GLib.idle_add(self._nc_show_loaded, item, local)

    def _nc_show_loaded(self, item, local_path: str | None) -> None:
        if self._closing:
            return GLib.SOURCE_REMOVE
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
        cached = self._image_cache.get(path)
        if cached is not None:
            self._image_cache.move_to_end(path)
            self._mount_picture(cached, path)
            self._preload_neighbours()
            return
        # Cache miss: decode off the UI thread so a big photo doesn't freeze the
        # swipe. Show a spinner meanwhile; the decode result is applied (if the
        # user is still on this item) by _on_image_decoded.
        token = self._show_token
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
        self._ensure_decode_pool().submit(self._decode_image_worker, path, token)
        self._preload_neighbours()

    def _ensure_decode_pool(self) -> ThreadPoolExecutor:
        if self._decode_pool is None:
            self._decode_pool = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="muga-viewer-decode",
            )
        return self._decode_pool

    def _decode_display_pixbuf(self, path: str) -> "GdkPixbuf.Pixbuf | None":
        """Decode *path* to an EXIF-oriented pixbuf, downscaled so its long
        edge never exceeds _DECODE_MAX_DIM. Runs on a worker thread."""
        try:
            # GdkPixbuf has no decompression-bomb guard of its own, and the
            # branch below falls through to a full-size decode whenever the
            # header didn't yield dimensions.
            if not image_within_pixel_budget(path):
                return None
            info = GdkPixbuf.Pixbuf.get_file_info(path)
            w, h = (info[1], info[2]) if info else (0, 0)
            cap = self._DECODE_MAX_DIM
            if w and h and max(w, h) > cap:
                # Scale down at decode time — never up (small images stay native).
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, cap, cap, True)
            else:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
            return pixbuf.apply_embedded_orientation() or pixbuf
        except Exception:
            LOGGER.debug("image decode failed for %s", path, exc_info=True)
            return None

    def _decode_image_worker(self, path: str, token: int) -> None:
        pixbuf = self._decode_display_pixbuf(path)
        GLib.idle_add(self._on_image_decoded, path, token, pixbuf)

    def _on_image_decoded(self, path: str, token: int, pixbuf) -> bool:
        if pixbuf is not None:
            self._image_cache[path] = pixbuf
            self._image_cache.move_to_end(path)
            while len(self._image_cache) > self._IMAGE_CACHE_MAX:
                self._image_cache.popitem(last=False)
        # Only paint if the viewer is still showing this very item.
        if self._closing or token != self._show_token:
            return GLib.SOURCE_REMOVE
        # Clear the spinner placeholder, then mount the image (or a lazy
        # filename fallback if the decode failed).
        child = self.stack.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.stack.remove(child)
            child = nxt
        self._mount_picture(pixbuf, path)
        return GLib.SOURCE_REMOVE

    def _mount_picture(self, pixbuf, path: str) -> None:
        if pixbuf is not None:
            # Gtk.Picture.new_for_pixbuf is deprecated; wrapping the pixbuf in
            # a texture is what it does internally and is what gallery_grid
            # already does for thumbnails.
            picture = Gtk.Picture.new_for_paintable(texture_from_pixbuf(pixbuf))
        else:
            # Decode failed — fall back to GTK's lazy filename loader.
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
        self._mount_rotatable(scroller)

    def _preload_neighbours(self) -> None:
        """Warm the decode cache for the items on either side of the current
        one so the next swipe hits the cache instead of decoding live."""
        if not self.items:
            return
        n = len(self.items)
        for direction in (1, -1):
            neighbour = self.items[(self.index + direction) % n]
            if neighbour.is_video or is_nc_path(neighbour.path):
                continue
            path = neighbour.path
            if path in self._image_cache:
                continue
            self._ensure_decode_pool().submit(self._preload_worker, path)

    def _preload_worker(self, path: str) -> None:
        if path in self._image_cache:
            return
        pixbuf = self._decode_display_pixbuf(path)
        if pixbuf is not None:
            GLib.idle_add(self._cache_preloaded, path, pixbuf)

    def _cache_preloaded(self, path: str, pixbuf) -> bool:
        if not self._closing and path not in self._image_cache:
            self._image_cache[path] = pixbuf
            self._image_cache.move_to_end(path)
            while len(self._image_cache) > self._IMAGE_CACHE_MAX:
                self._image_cache.popitem(last=False)
        return GLib.SOURCE_REMOVE

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
        self._sensor_rotator = None
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
            if not image_within_pixel_budget(path):
                return
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
        if self._closing:
            return GLib.SOURCE_REMOVE
        child = self.stack.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.stack.remove(child)
            child = nxt
        self._reset_zoom()
        self.zoom_view = None
        self.zoom_scroller = None
        self._sensor_rotator = None
        if pixbuf is not None:
            picture = Gtk.Picture.new_for_paintable(texture_from_pixbuf(pixbuf))
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
        self._mount_rotatable(scroller)
        # Force a resize pass: removing + adding stack children doesn't always
        # reset cached size negotiation, so we nudge the toolbar/window once.
        self.queue_resize()
        if not self.props.fullscreened:
            self.fullscreen()

    def _on_media_prepared(self, media_stream, _param=None) -> None:
        w = media_stream.get_intrinsic_width()
        h = media_stream.get_intrinsic_height()
        if w > 0 and h > 0 and w > h:
            # Landscape video: auto-hide chrome, and route through the same
            # helper so a subsequent swipe keeps the chrome hidden.
            idle_once(self._set_chrome_visible, False)

    def _update_date_label(self, item: MediaItem) -> None:
        try:
            dt = datetime.fromtimestamp(item.display_time)
        except (OverflowError, OSError, ValueError):
            self._date_label_has_value = False
            self.date_revealer.set_reveal_child(False)
            return
        # Locale-aware day-month, e.g. "1 Mai" with LC_TIME=de_DE
        self.date_day_label.set_label(dt.strftime("%-d %B"))
        self.date_year_label.set_label(dt.strftime("%Y"))
        self._date_label_has_value = True
        self.date_revealer.set_reveal_child(
            self._chrome_visible and not self._current_is_video
        )

    def _update_filename_label(self, item: MediaItem) -> None:
        self.filename_label.set_label(item.name or "")
        self.filename_revealer.set_reveal_child(
            self._chrome_visible
            and bool(item.name)
            and not self._current_is_video
        )

    def _apply_chrome_visibility(self) -> None:
        """Push the current ``_chrome_visible`` flag onto the actual widgets.
        Single source of truth so swipe → show_item → re-apply keeps the
        user's "hide chrome" choice across image navigation."""
        visible = self._chrome_visible
        self.header.set_visible(visible)
        # Pills follow chrome but only if there's something meaningful to
        # show — videos don't get pills, and an item without a parsable
        # mtime/name shouldn't pop a blank revealer either.
        if self._current_is_video:
            self.date_revealer.set_reveal_child(False)
            self.filename_revealer.set_reveal_child(False)
            return
        if visible and getattr(self, "_date_label_has_value", False):
            self.date_revealer.set_reveal_child(True)
        else:
            self.date_revealer.set_reveal_child(False)
        item = self.items[self.index] if self.items else None
        has_name = item is not None and bool(item.name)
        self.filename_revealer.set_reveal_child(visible and has_name)

    def _set_chrome_visible(self, visible: bool) -> None:
        self._chrome_visible = visible
        self._apply_chrome_visibility()

    def _on_destroy(self, _window) -> None:
        self._closing = True
        self._orientation_client.stop()
        pool, self._decode_pool = self._decode_pool, None
        if pool is not None:
            pool.shutdown(wait=False)
        self._image_cache.clear()

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
        idle_once(parent.present)
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
            path = self._current_display_path
            rotation = self._rotation
            self._rotation = 0
            if path and rotation:
                # Re-encoding a full-resolution photo blocks the UI for hundreds
                # of ms — do it off-thread, then run the follow-up action (close
                # / navigate) once the file is on disk. The cached display pixbuf
                # is now stale, so drop it.
                self._image_cache.pop(path, None)
                # Grab the item now (on the main loop) so the worker doesn't read
                # self.index while a follow-up navigation is mutating it.
                item = (
                    self.items[self.index]
                    if self.items and 0 <= self.index < len(self.items)
                    else None
                )

                def worker() -> None:
                    ok = False
                    try:
                        ok = self._save_rotation_to_disk(path, rotation)
                        if ok:
                            self._refresh_thumbnail_after_rotation(path, item)
                    except Exception:
                        LOGGER.exception("Rotation worker crashed for %s", path)
                    finally:
                        # The follow-up action (close / navigate) has to run
                        # even when the save failed, or the viewer stays stuck
                        # on a photo the user already asked to leave.
                        GLib.idle_add(self._on_rotation_saved, ok)

                self._pending_rotation_action = action
                threading.Thread(target=worker, daemon=True).start()
                return
        self._rotation = 0
        action()

    def _on_rotation_saved(self, ok: bool) -> bool:
        """Main-loop tail of the rotation worker: run the deferred action and,
        if the file could not be written, tell the user instead of failing
        silently — the photo on disk is unchanged in that case."""
        action = getattr(self, "_pending_rotation_action", None)
        self._pending_rotation_action = None
        if not ok:
            try:
                _ = self.parent_window._
                self.parent_window._show_error_dialog(
                    _("Save failed"),
                    _("The rotation could not be saved. The photo on disk is unchanged."),
                )
            except Exception:
                LOGGER.debug("Could not surface rotation failure", exc_info=True)
        if action is not None:
            action()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _save_rotation_to_disk(path: str, rotation: int) -> bool:
        """Bake *rotation* into the file at *path*. Returns True on success.

        Both encoder paths write beside the original and swap atomically, so a
        failed save leaves the photo exactly as it was — which is also what
        lets the GdkPixbuf fallback read an intact source after the PIL path
        gave up.
        """
        if not path or rotation == 0:
            return True
        if _PIL_OK:
            try:
                from PIL import ImageOps
                with PILImage.open(path) as src:
                    # Read the metadata off the source before any transform — the
                    # save below has to carry it over explicitly or Pillow writes a
                    # file with no EXIF at all.
                    exif_bytes = _exif_for_upright_save(src)
                    # Bake EXIF orientation into the pixels first so the saved file is
                    # standalone-correct (no orientation tag needed), then layer the
                    # user's rotation on top.
                    img = ImageOps.exif_transpose(src)
                    img = img.rotate(-rotation, expand=True)
                ext = Path(path).suffix.lower()
                save_kwargs: dict[str, Any] = {"exif": exif_bytes} if exif_bytes else {}
                if ext in (".jpg", ".jpeg"):
                    save_kwargs["quality"] = 95
                # Pillow infers the output format from the filename, and the
                # temp file ends in ".tmp" — pass the source format explicitly.
                fmt = PILImage.registered_extensions().get(ext)
                if fmt:
                    save_kwargs["format"] = fmt
                _write_in_place_atomic(path, lambda tmp: img.save(tmp, **save_kwargs))
                return True
            except Exception:
                LOGGER.exception("PIL save_rotation failed for %s", path)
        try:
            if not image_within_pixel_budget(path):
                return False
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
            pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
            rot_map = {
                90: GdkPixbuf.PixbufRotation.CLOCKWISE,
                180: GdkPixbuf.PixbufRotation.UPSIDEDOWN,
                270: GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE,
            }
            rotated = pixbuf.rotate_simple(rot_map[rotation])
            ext = Path(path).suffix.lower()
            fmt = "jpeg" if ext in (".jpg", ".jpeg") else "png"
            _write_in_place_atomic(path, lambda tmp: rotated.savev(tmp, fmt, [], []))
            return True
        except Exception:
            LOGGER.exception("GdkPixbuf save_rotation failed for %s", path)
            return False

    def _refresh_thumbnail_after_rotation(self, path: str, item: MediaItem | None) -> None:
        """Regenerate the gallery thumbnail after a rotation was baked into the
        file on disk.

        ``ensure_thumbnail`` notices the rewritten source by itself (it compares
        the thumbnail's stamped mtime against the file's), so the explicit
        invalidate below is belt and braces: two rotations saved inside a single
        filesystem timestamp tick would otherwise leave the second one showing
        the first one's thumbnail.

        What this method adds beyond freshness is what the thumbnailer cannot
        do on its own — writing the new thumbnail path back to the index and
        turning the tile in the live grid, without waiting for a full rescan.

        Only for genuine local items whose display path *is* the stored path — an
        NC item's display path is a throwaway download whose rotation never
        persists to the server, so there is nothing to refresh there."""
        if item is None or item.path != path:
            return
        parent = self.parent_window
        try:
            thumbnailer = parent.thumbnailer
            thumbnailer.invalidate(Path(path))
            thumb = thumbnailer.ensure_thumbnail(Path(path), item.media_type)
        except Exception:
            LOGGER.debug("thumbnail refresh after rotation failed for %s", path, exc_info=True)
            return
        if not thumb:
            return
        try:
            parent.database.set_thumb(path, thumb, item.category)
        except Exception:
            LOGGER.debug("thumb DB write after rotation failed for %s", path, exc_info=True)
        idle_once(parent._enqueue_thumb_update, path, thumb)

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

    def _on_key(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        if self._editor is not None:
            if keyval == Gdk.KEY_Escape:
                self._exit_edit_mode()
                return True
            # Ctrl+Z = undo, Ctrl+Shift+Z / Ctrl+Y = redo. Match the
            # GIMP/Inkscape convention rather than the OS default so users
            # don't get a different shortcut depending on the desktop.
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            if ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
                if shift:
                    self._on_editor_redo()
                else:
                    self._on_editor_undo()
                return True
            if ctrl and keyval in (Gdk.KEY_y, Gdk.KEY_Y):
                self._on_editor_redo()
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
        # Deltas arrive in window space; navigation follows the user's eyes.
        x, y = _motion_in_view_space(x, y, self._sensor_angle)
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
            # pill, header) — and the new state persists across swipe-to-
            # next via _chrome_visible so the next image stays uncluttered.
            self._set_chrome_visible(not self._chrome_visible)
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
        self.share_button.set_visible(False)
        self.cancel_edit_button.set_visible(True)
        self.save_edit_button.set_visible(True)
        self.undo_button.set_visible(True)
        self.redo_button.set_visible(True)
        # Initial sensitivity reflects an empty history; the editor's
        # callback (registered below) keeps it in sync after each edit.
        self.undo_button.set_sensitive(False)
        self.redo_button.set_sensitive(False)
        # In landscape, fold the filename + date into the title bar so the
        # editor gets every available pixel. In portrait the title bar would
        # truncate aggressively, so we instead float the filename at the *top*
        # of the editor (with the same black-pill look as before).
        try:
            dt = datetime.fromtimestamp(item.display_time)
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
        # Drive the toolbar buttons' sensitivity from editor history events.
        # The callback fires once on registration so initial state is correct
        # without a follow-up call here.
        self._editor.set_history_changed_callback(self._sync_editor_history_buttons)
        self.stack.add_child(self._editor)
        self.stack.set_visible_child(self._editor)

    def _sync_editor_history_buttons(self) -> None:
        editor = self._editor
        if editor is None:
            return
        self.undo_button.set_sensitive(editor.can_undo())
        self.redo_button.set_sensitive(editor.can_redo())

    def _on_editor_undo(self, _btn: Gtk.Button = None) -> None:
        if self._editor is not None:
            self._editor.undo()

    def _on_editor_redo(self, _btn: Gtk.Button = None) -> None:
        if self._editor is not None:
            self._editor.redo()

    def _exit_edit_mode(self, _button=None) -> None:
        if self._editor is not None:
            self._editor.cleanup()
        self._editor = None
        self._set_view_gestures_enabled(True)
        self.header.set_show_end_title_buttons(False)
        self.header.set_show_start_title_buttons(False)
        self.close_button.set_visible(True)
        self.cancel_edit_button.set_visible(False)
        self.save_edit_button.set_visible(False)
        self.undo_button.set_visible(False)
        self.redo_button.set_visible(False)
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
        if self._closing:
            return GLib.SOURCE_REMOVE
        self.save_edit_button.set_sensitive(True)
        self.cancel_edit_button.set_sensitive(True)
        if not success:
            self.parent_window._set_status(self.parent_window._("Could not save edited image"))
            return
        self.parent_window.refresh(scan=True)
        self._exit_edit_mode()

    def _upload_to_nextcloud(self, local_edited_path: str) -> None:
        """Upload edited image back to Nextcloud."""
        from .nextcloud import NextcloudClient, dav_path_from_nc
        
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
                LOGGER.debug("Path failed", exc_info=True)
        # Category-agnostic: the file is trashed, so every index row for the
        # path is stale (also covers Overview/Videos where item.category is the
        # real category but a same-path row could exist under another category).
        self.parent_window.database.delete_path(item.path)
        self.items.pop(self.index)
        self.parent_window.refresh(scan=False)
        if not self.items:
            self.close()
            return
        self.index = min(self.index, len(self.items) - 1)
        self.show_item()

    def _get_cached_exif_only(self, item: MediaItem) -> dict[str, str] | None:
        """Return cached EXIF from the DB without ever parsing the file.
        None means cache miss — caller decides whether to parse off-thread."""
        import json
        cached_json = self.parent_window.database.get_exif_data(item.path, item.category)
        if not cached_json:
            return None
        try:
            return json.loads(cached_json)
        except (json.JSONDecodeError, TypeError):
            return None

    def _parse_and_cache_exif(self, item: MediaItem) -> dict[str, str]:
        """Parse EXIF off the main loop and persist to the DB cache.
        Always called from a background thread."""
        import json
        exif = _extract_exif(item.path)
        if exif:
            try:
                exif_json = json.dumps(exif)
                self.parent_window.database.set_exif_data(
                    item.path, exif_json, item.category,
                )
                self.parent_window.database.commit()
            except Exception:
                # If caching fails, still return the parsed EXIF
                pass
        return exif

    def _show_info(self, _button: Gtk.Button) -> None:
        item = self.items[self.index]
        _ = self.parent_window._

        # Cheap fields go in the popover synchronously so the user sees
        # something immediately; dimensions + EXIF can take hundreds of ms
        # on a 40 MP RAW/HEIC and used to freeze the main loop. Slot them
        # in via idle_add when the worker thread finishes.
        rows: list[tuple[str, str]] = [
            (_("Name"), item.name),
            (_("Folder"), item.folder),
            (_("Size"), _fmt_size(item.size)),
            (_("Modified"), _fmt_date(item.mtime)),
        ]

        cached_exif: dict[str, str] | None = None
        if not item.is_video:
            cached_exif = self._get_cached_exif_only(item)

        loading_label = _("Loading…")
        # Track which rows are placeholders so the worker can update them
        # in place without rebuilding the grid.
        placeholders: dict[str, Gtk.Label] = {}

        def add_row(key: str, value: str) -> Gtk.Label:
            i = grid_state["next_row"]
            grid_state["next_row"] += 1
            key_lbl = Gtk.Label(label=key, xalign=1.0)
            key_lbl.add_css_class("dim-label")
            val_lbl = Gtk.Label(label=value, xalign=0.0)
            val_lbl.set_selectable(i != 0)
            val_lbl.set_wrap(True)
            val_lbl.set_max_width_chars(32)
            grid.attach(key_lbl, 0, i, 1, 1)
            grid.attach(val_lbl, 1, i, 1, 1)
            return val_lbl

        grid = Gtk.Grid()
        grid.set_column_spacing(20)
        grid.set_row_spacing(8)
        grid.set_margin_top(14)
        grid.set_margin_bottom(14)
        grid.set_margin_start(16)
        grid.set_margin_end(16)
        grid_state = {"next_row": 0}

        for key, value in rows:
            add_row(key, value)

        if not item.is_video:
            placeholders["Dimensions"] = add_row(_("Dimensions"), loading_label)
            if cached_exif is not None:
                # Cached: drop the placeholders and inline the values.
                if "Camera" in cached_exif:
                    add_row(_("Camera"), cached_exif["Camera"])
                if "GPS" in cached_exif:
                    add_row(_("GPS"), cached_exif["GPS"])
            else:
                # Reserve placeholders for the worker to fill in.
                placeholders["Camera"] = add_row(_("Camera"), loading_label)
                placeholders["GPS"] = add_row(_("GPS"), loading_label)

        popover = Gtk.Popover()
        popover.set_parent(self.info_button)
        # Same as the gallery's context menu: dismiss on an outside click, and
        # unparent on close so repeated opens do not pile invisible popovers
        # onto the info button.
        popover.set_autohide(True)
        popover.connect("closed", lambda pop: pop.unparent())
        popover.set_child(grid)
        popover.popup()

        if item.is_video or not placeholders:
            return

        # Snapshot what the worker needs to know — popover may close (and
        # placeholders disappear) before the result lands.
        target_path = item.path
        item_for_parse = item
        skip_exif = cached_exif is not None

        def _worker() -> None:
            try:
                dims = _image_dimensions(target_path)
            except Exception:
                LOGGER.debug("dimension probe failed for %s", target_path, exc_info=True)
                dims = None
            exif: dict[str, str] = {}
            if not skip_exif:
                try:
                    exif = self._parse_and_cache_exif(item_for_parse)
                except Exception:
                    LOGGER.debug("exif parse failed for %s", target_path, exc_info=True)
                    exif = {}
            GLib.idle_add(_apply_result, dims, exif)

        def _apply_result(dims, exif) -> bool:
            # If the popover was already dismissed and the labels reparented,
            # the set_label calls are no-ops on detached widgets — safe.
            dim_label = placeholders.get("Dimensions")
            if dim_label is not None:
                dim_label.set_label(dims if dims else "—")
            for key in ("Camera", "GPS"):
                lbl = placeholders.get(key)
                if lbl is None:
                    continue
                lbl.set_label(exif.get(key, "—"))
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_worker, daemon=True, name="info-exif").start()

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
        """Called on slideshow timer tick. Advance to the next *image*,
        skipping video clips so the slideshow doesn't grind to a halt on a
        long video. Stops the slideshow gracefully if every remaining item
        is a video (nothing to play)."""
        if not self._slideshow_active or not self.items:
            return GLib.SOURCE_REMOVE
        n = len(self.items)
        new_index = (self.index + 1) % n
        for _ in range(n):
            if not self.items[new_index].is_video:
                break
            new_index = (new_index + 1) % n
        else:
            # All-video gallery — stop instead of looping forever on a
            # placeholder. The user can re-trigger when they navigate to a
            # mixed list.
            self._stop_slideshow()
            return GLib.SOURCE_REMOVE
        self.index = new_index
        self.show_item()
        self._schedule_next_slide()
        return GLib.SOURCE_REMOVE
