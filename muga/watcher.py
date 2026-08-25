"""Live filesystem watching for the folders the scanner indexes.

The gallery used to learn about a new picture only when something asked it to
scan: startup, the refresh button, pull-to-refresh, or the built-in camera
handing back a capture. Everything else that writes a photo — the system
camera, a screenshot tool, a file manager copy, a sync client — left the grid
showing the library as it was when the window opened, until the user refreshed
by hand.

This watches those folders with Gio.FileMonitor and reports what changed, so
the new file can be folded into the index (and onto the screen) on its own.
Events arrive on the main loop, so the reporting callback does too.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Iterable

from gi.repository import Gio, GLib

LOGGER = logging.getLogger(__name__)

# How long to sit on incoming events before reporting them. A single photo
# lands as a burst (created → changed → changed → changes-done-hint), and a
# copy of a folder full of them arrives as one burst per file. Coalescing over
# a short window turns that into one index pass and one re-render instead of
# one per event.
_DEBOUNCE_MS = 700

# Upper bound on directory monitors. Each one costs an inotify watch, and a
# library that nests thousands of folders would otherwise eat the user's
# per-process quota — at which point monitor_directory starts failing and the
# watches we *do* care about are whichever ones happened to be created first.
# Beyond the cap the tree simply isn't watched; the manual refresh and the
# startup scan still cover it, which is exactly the behaviour before this
# existed.
_MAX_WATCHES = 2048

# The events worth reacting to. CHANGED is deliberately absent: it fires
# repeatedly while a file is still being written, and CHANGES_DONE_HINT is the
# same edit reported once the writer let go.
_INTERESTING = {
    Gio.FileMonitorEvent.CREATED,
    Gio.FileMonitorEvent.CHANGES_DONE_HINT,
    Gio.FileMonitorEvent.DELETED,
    Gio.FileMonitorEvent.MOVED_IN,
    Gio.FileMonitorEvent.MOVED_OUT,
    Gio.FileMonitorEvent.RENAMED,
}


class MediaWatcher:
    """Watches a set of roots and reports changed paths in coalesced batches.

    *on_changed* is called with a set of path strings. They may have been
    created, modified or removed — the watcher does not try to tell those
    apart, because by the time the batch is reported the only answer that
    matters is what is on disk now. The caller is expected to look.
    """

    def __init__(
        self,
        on_changed: Callable[[set[str]], None],
        debounce_ms: int = _DEBOUNCE_MS,
        max_watches: int = _MAX_WATCHES,
    ) -> None:
        self._on_changed = on_changed
        self._debounce_ms = debounce_ms
        self._max_watches = max_watches
        self._monitors: dict[str, Gio.FileMonitor] = {}
        self._pending: set[str] = set()
        self._flush_id: int | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def watch(self, roots: Iterable[Path]) -> None:
        """Point the watcher at *roots*, replacing whatever it watched before.

        Called again whenever the configured folders change, so a root the
        user just removed stops firing and one they just added starts.
        """
        self.stop()
        for root in roots:
            self._add_tree(Path(root).expanduser())
        LOGGER.info("Watching %d directories for new media", len(self._monitors))

    def stop(self) -> None:
        """Drop every monitor and any batch still waiting. Idempotent."""
        if self._flush_id is not None:
            try:
                GLib.source_remove(self._flush_id)
            except Exception:
                LOGGER.debug("flush timeout removal failed", exc_info=True)
            self._flush_id = None
        self._pending.clear()
        for monitor in self._monitors.values():
            try:
                monitor.cancel()
            except Exception:
                LOGGER.debug("monitor.cancel failed", exc_info=True)
        self._monitors.clear()

    @property
    def watched_count(self) -> int:
        return len(self._monitors)

    # ------------------------------------------------------------------
    # Building the watch set
    # ------------------------------------------------------------------

    def _add_tree(self, root: Path) -> None:
        """Monitor *root* and every directory beneath it.

        Gio watches one directory at a time, so a recursive view means one
        monitor per folder. The walk mirrors the scanner's: hidden directories
        hold tooling rather than photos and are never descended into, and
        symlinked directories are skipped so a loop can't be followed.
        """
        if root.is_symlink() or not root.is_dir():
            return
        stack = [str(root)]
        while stack:
            current = stack.pop()
            if not self._add_dir(current):
                # Cap reached (or this directory can't be watched) — no point
                # descending, the children would not fit either.
                if len(self._monitors) >= self._max_watches:
                    LOGGER.info(
                        "Watch limit of %d directories reached — %s and below "
                        "will only update on a manual refresh",
                        self._max_watches, current,
                    )
                    return
                continue
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir() or entry.is_symlink():
                        continue
                except OSError:
                    continue
                if entry.name.startswith("."):
                    continue
                stack.append(entry.path)

    def _add_dir(self, path: str) -> bool:
        if path in self._monitors:
            # Already watched: two configured roots overlapping (Photos inside
            # Pictures, say) must not cost two monitors and two events per file.
            return True
        if len(self._monitors) >= self._max_watches:
            return False
        try:
            monitor = Gio.File.new_for_path(path).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None
            )
        except GLib.Error:
            LOGGER.debug("Could not watch %s", path, exc_info=True)
            return False
        monitor.connect("changed", self._on_event)
        self._monitors[path] = monitor
        return True

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _on_event(
        self,
        _monitor: Gio.FileMonitor,
        gfile: Gio.File,
        other_file: Gio.File | None,
        event: Gio.FileMonitorEvent,
    ) -> None:
        if event not in _INTERESTING:
            return
        # RENAMED carries both ends: the old name has to leave the index and
        # the new one has to enter it.
        for candidate in (gfile, other_file):
            if candidate is None:
                continue
            path = candidate.get_path()
            if not path:
                continue
            # A directory appearing under a watched root — a camera app
            # creating this month's folder, an unpacked archive — needs its own
            # monitor, or everything written into it afterwards is invisible.
            if event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN):
                self._maybe_watch_new_dir(path)
            self._pending.add(path)
        self._schedule_flush()

    def _maybe_watch_new_dir(self, path: str) -> None:
        try:
            entry = Path(path)
            if entry.is_symlink() or not entry.is_dir():
                return
        except OSError:
            return
        if entry.name.startswith("."):
            return
        self._add_tree(entry)

    def _schedule_flush(self) -> None:
        # Restart the timer on every event so a burst is reported once, after
        # it stops — not once per _debounce_ms while a folder copy is running.
        if self._flush_id is not None:
            try:
                GLib.source_remove(self._flush_id)
            except Exception:
                LOGGER.debug("flush timeout removal failed", exc_info=True)
        self._flush_id = GLib.timeout_add(self._debounce_ms, self._flush)

    def _flush(self) -> bool:
        self._flush_id = None
        batch = self._pending
        self._pending = set()
        if batch:
            try:
                self._on_changed(batch)
            except Exception:
                LOGGER.exception("Media watcher callback failed")
        return GLib.SOURCE_REMOVE
