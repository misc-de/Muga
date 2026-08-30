"""Selection mode and the file actions it drives.

Split out of ``GalleryWindow``. Everything here answers the same question —
what happens to the photos the user picked — from the long-press that opens
selection mode to the delete, move, share and open-with that follow.

Batch operations run on a worker thread and report back through
``_set_sel_busy`` / ``GLib.idle_add``; the gallery stays interactive while a
few hundred files move.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_NAME
from .models import MediaItem

if TYPE_CHECKING:
    from .config import Settings
    from .database import Database
    from .gallery_grid import GalleryGrid

LOGGER = logging.getLogger(__name__)


def _move_file_no_clobber(src: Path, folder: Path) -> Path:
    """Move *src* into *folder* and return where it landed.

    Replaces the plain ``Path.rename`` this used to be, which had two ways of
    losing a user's photos:

    * it silently destroyed a same-named file already in the destination —
      two cameras both numbering from ``IMG_0001.jpg`` cost you one of them;
    * it cannot cross filesystems, so moving anything onto an SD card or USB
      stick failed with EXDEV for every single file.

    A colliding name gets a ``" (2)"`` suffix instead. The name is claimed
    with an atomic exclusive create — ``os.link`` within one filesystem,
    ``O_CREAT|O_EXCL`` across two — so a second process racing for the same
    name loses the race rather than both writing it. The source is only
    unlinked once the destination is complete, so an interrupted cross-device
    move leaves the original in place.
    """
    stem, suffix = Path(src.name).stem, Path(src.name).suffix
    for attempt in range(1, 1000):
        name = src.name if attempt == 1 else f"{stem} ({attempt}){suffix}"
        target = folder / name
        try:
            os.link(src, target)
        except FileExistsError:
            continue
        except OSError as exc:
            import errno
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOSYS):
                raise
            # Different filesystem (or one that refuses hard links): claim the
            # name exclusively, then stream the bytes over.
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            os.close(fd)
            try:
                shutil.copyfile(src, target)
                shutil.copystat(src, target)
            except BaseException:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    LOGGER.debug("target.unlink failed", exc_info=True)
                raise
            src.unlink()
            return target
        else:
            # Hard link placed: the destination now has the data, so dropping
            # the source completes the move without ever copying bytes.
            src.unlink()
            return target
    raise FileExistsError(f"No free filename for {src.name} in {folder}")


class GallerySelectionMixin:
    """Selection mode, batch operations and per-item file actions.

    The block below is the contract with the host class: every name is created
    in ``GalleryWindow.__init__`` (or defined on it) and only annotated here,
    so the type checker sees one consistent picture across the split.
    """

    # State.
    _selection_mode: bool
    _selected_paths: set[str]
    _sel_busy: bool
    _closing: bool
    current_folder: str | None
    database: Database
    settings: Settings
    gallery_grid: GalleryGrid

    # Header chrome the selection bar swaps in and out.
    header: Adw.HeaderBar
    _sel_title: Adw.WindowTitle
    _sel_cancel_btn: Gtk.Button
    _sel_delete_btn: Gtk.Button
    _sel_move_btn: Gtk.Button
    _sel_share_btn: Gtk.Button
    back_button: Gtk.Button
    new_folder_button: Gtk.Button
    refresh_button: Gtk.Button
    search_button: Gtk.ToggleButton
    settings_button: Gtk.Button
    sort_button: Gtk.MenuButton

    if TYPE_CHECKING:
        # Provided by GalleryWindow. Declared under TYPE_CHECKING so nothing
        # exists at runtime that could shadow the real method.
        def _(self, text: str) -> str: ...
        def _set_status(self, text: str) -> None: ...
        def _show_error_dialog(self, title: str, message: str, details: str = "") -> None: ...
        def _handle_file_error(self, error: Exception, file_path: str = "") -> None: ...
        def refresh(self, scan: bool = False, scope: str | None = None,
                    reset_scroll: bool = False) -> None: ...
        def _render(self, reset_scroll: bool = False) -> None: ...

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
        # Dismiss on a click or tap anywhere outside. autohide defaults to
        # True, but the popover also has to let go of its parent when it
        # closes: set_parent() keeps it alive, so every long-press was leaving
        # another invisible popover attached to the same tile, and those stack
        # up in front of the live one and swallow the very clicks that are
        # supposed to dismiss it.
        popover.set_autohide(True)
        popover.connect("closed", lambda pop: pop.unparent())
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
        self._set_sel_busy(True, self._("Deleting %d items…") % n)

        def _worker() -> None:
            errors: list[tuple[str, Exception]] = []
            try:
                for path in paths:
                    try:
                        Gio.File.new_for_path(path).trash(None)
                        # Drop ALL index rows for the trashed path, not just the
                        # current view's category: in the Overview/Videos
                        # aggregators self.category ("pictures"/"videos") matches
                        # no real row, so a category-scoped delete left the tile
                        # behind. The file is gone from disk → every row is stale.
                        self.database.delete_path(path)
                    except Exception as e:
                        errors.append((path, e))
            except Exception:
                LOGGER.exception("Bulk delete worker crashed")
            finally:
                # Always unfreeze the toolbar via the done-handler.
                GLib.idle_add(self._on_sel_delete_done, n, errors)

        threading.Thread(target=_worker, daemon=True, name="sel-delete").start()

    def _on_sel_delete_done(self, total: int, errors: list[tuple[str, Exception]]) -> bool:
        if self._closing:
            return GLib.SOURCE_REMOVE
        self._set_sel_busy(False, "")
        self._exit_selection_mode()
        # Re-render so the deleted tiles disappear from the grid. No
        # success toast — the visible absence of the items is the
        # confirmation. Partial / total failures still surface a
        # status or error dialog because the user needs to know what
        # didn't get deleted.
        self._render()
        if errors and len(errors) == total:
            self._show_error_dialog(
                self._("Delete failed"),
                self._("Could not delete all files. Check file permissions or disk state."),
                f"{len(errors)}/{total} items",
            )
        elif errors:
            succeeded = total - len(errors)
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
                        _move_file_no_clobber(Path(path), Path(folder))
                        # The file left this path → remove all its index rows
                        # (category-agnostic, same reason as the delete path).
                        self.database.delete_path(path)
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
        if self._closing:
            return GLib.SOURCE_REMOVE
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
        line so the user sees the operation is making progress.

        Status is always written — including when *status* is empty —
        so the "Deleting N items…" message reliably disappears when
        the worker hands control back via _set_sel_busy(False, '')."""
        self._sel_busy = busy
        for btn in (
            self._sel_cancel_btn,
            self._sel_delete_btn,
            self._sel_move_btn,
            self._sel_share_btn,
        ):
            btn.set_sensitive(not busy)
        self._set_status(status)

    def _delete_item(self, item: MediaItem) -> None:
        try:
            Gio.File.new_for_path(item.path).trash(None)
            if item.thumb_path:
                try:
                    Path(item.thumb_path).unlink(missing_ok=True)
                except OSError:
                    LOGGER.debug("Path failed", exc_info=True)
            # Trashing removes the file outright → drop every index row for the
            # path (category-agnostic), so it also disappears from the Overview
            # / Videos aggregators where item.category wouldn't match the view.
            self.database.delete_path(item.path)
            # No status toast — the re-render is the visual
            # confirmation (per user spec: "Bitte den Hinweis
            # entfernen").
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
            try:
                _move_file_no_clobber(Path(item.path), Path(folder))
                self.database.delete_path(item.path)
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
