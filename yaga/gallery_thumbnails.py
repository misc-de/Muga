"""Thumbnail production and the on-disk cache budget for the gallery window.

Split out of ``GalleryWindow`` — which had grown past 110 methods — as a mixin
rather than a free-standing service: every method here still drives the same
window (grid tiles, status label, settings) and reaches it through ``self``.
The class boundary is the seam; it is not pretending to be a new abstraction.

Three concerns live here:

* the disk-cache budget — measure it, evict least-recently-used, clear it
* local thumbnails, decoded on a worker thread
* Nextcloud thumbnails, fetched through one shared client and coalesced into
  the grid in batches

Every attribute this touches is created in ``GalleryWindow.__init__``.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib

from .config import THUMB_DIR

if TYPE_CHECKING:
    from .config import Settings
    from .database import Database
    from .gallery_grid import GalleryGrid
    from .nextcloud import NextcloudClient
    from .thumbnails import Thumbnailer

LOGGER = logging.getLogger(__name__)


class GalleryWindowThumbnailsMixin:
    """Cache budget and thumbnail workers. Mixed into GalleryWindow.

    The block below is the contract with the host class: every name is created
    in ``GalleryWindow.__init__`` (or defined on it) and only annotated here,
    so the type checker sees one consistent picture across the split and a
    reader can tell at a glance what this mixin reaches into.
    """

    # Set up by GalleryWindow.__init__.
    settings: Settings
    database: Database
    thumbnailer: Thumbnailer
    gallery_grid: GalleryGrid
    category: str
    _closing: bool

    _local_thumb_lock: threading.Lock
    _local_thumb_pending: set[str]
    _local_thumb_failed: set[str]
    _local_thumb_pool: ThreadPoolExecutor | None

    _nc_thumb_lock: threading.Lock
    _nc_thumb_event: threading.Event
    _nc_thumb_queue: list[str]
    _nc_thumb_pending: set[str]
    _nc_thumb_active_workers: int
    _nc_thumb_worker_target: int
    _nc_thumb_shared_client: NextcloudClient | None
    _nc_unreachable: bool

    _pending_thumb_lock: threading.Lock
    _pending_thumb_updates: dict[str, str]
    _pending_thumb_idle: int

    # Coalesces multiple back-to-back scan-completion calls into a single
    # eviction pass — without these guards a user flipping fast between
    # folders kicks off N parallel rglob walks of THUMB_DIR + _NC_CACHE.
    _EVICT_MIN_INTERVAL_SEC = 60.0
    _evict_lock: threading.Lock
    _evict_in_flight: bool
    _evict_last_finished_at: float

    if TYPE_CHECKING:
        # Provided by GalleryWindow. Declared under TYPE_CHECKING so nothing
        # is defined at runtime — a stub here could otherwise silently shadow
        # the real method if it were ever removed from the host class.
        def is_nc_active(self) -> bool: ...
        def _on_nc_sync_failed(self, error: Exception) -> None: ...
        def _visible_child_folder_for_item(self, item_folder: str) -> str | None: ...

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
        if self._closing:
            return GLib.SOURCE_REMOVE
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
                    # A file that vanished mid-scan just does not count towards
                    # the total. No exc_info: this runs per cache entry.
                    LOGGER.debug("could not stat cache entry %s", f)
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
                    LOGGER.debug("total + assignment failed", exc_info=True)
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
                LOGGER.debug("freed + assignment failed", exc_info=True)
        if freed:
            LOGGER.info("Evicted %.1f MB from disk cache", freed / 1024 / 1024)
        return freed

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
    # ── On-demand local thumbnail loader ──────────────────────────────

    def request_local_thumbnail(self, item_path: str, media_type: str, category: str) -> None:
        """Generate a missing thumbnail for a local item off the UI thread.

        Called from tile-bind when the item has no cached thumb: decoding the
        full-resolution file via Gtk.Picture.set_filename() on the main loop
        stalls scrolling, so we hand the decode to a small pool and update the
        tile when the thumb lands. Idempotent per path; safe to call repeatedly
        as the same tile rebinds during scrolling."""
        with self._local_thumb_lock:
            if item_path in self._local_thumb_pending or item_path in self._local_thumb_failed:
                return
            self._local_thumb_pending.add(item_path)
            if self._local_thumb_pool is None:
                # Two workers hide decode latency while scrolling without
                # competing hard with the scanner's own thumbnail pool.
                self._local_thumb_pool = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="local-thumb",
                )
            pool = self._local_thumb_pool
        pool.submit(self._local_thumb_worker, item_path, media_type, category)

    def _local_thumb_worker(self, item_path: str, media_type: str, category: str) -> None:
        failed = False
        try:
            thumb = self.thumbnailer.ensure_thumbnail(Path(item_path), media_type)
            if thumb:
                try:
                    self.database.set_thumb(item_path, thumb, category)
                except Exception:
                    LOGGER.debug("local thumb DB write failed for %s", item_path, exc_info=True)
                self._enqueue_thumb_update(item_path, thumb)
            else:
                # Decoder ran but produced nothing (unsupported/corrupt file) —
                # remember it so rebinds don't keep re-queuing a doomed decode.
                failed = True
        except Exception:
            failed = True
            LOGGER.debug("local thumb generation failed for %s", item_path, exc_info=True)
        finally:
            with self._local_thumb_lock:
                self._local_thumb_pending.discard(item_path)
                if failed:
                    self._local_thumb_failed.add(item_path)
    # ── On-demand Nextcloud thumbnail loader ──────────────────────────

    def request_nc_thumbnail(self, item_path: str) -> None:
        """Queue a NC thumbnail fetch for *item_path*. Thread-safe; idempotent
        per path. Spawns workers up to the configured pool size on demand.

        Bails out silently when NC isn't actively allowed for this session —
        we never re-establish the connection on our own; that requires explicit
        consent (Settings toggle/Connect button or the viewer's "Einmalig/
        Dauerhaft" prompt)."""
        # Circuit breaker: once a sync/fetch has proven the server unreachable,
        # don't queue more fetches that would each block ~20 s and fail. The
        # next successful sync (or a reconnect) clears the flag.
        if not self.is_nc_active() or self._nc_unreachable:
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
        from .nextcloud import NextcloudConnectionError, dav_path_from_nc
        client = self._ensure_nc_thumb_client()
        if client is None:
            with self._nc_thumb_lock:
                self._nc_thumb_active_workers -= 1
                self._nc_thumb_pending.clear()
                self._nc_thumb_queue.clear()
            return
        # Batch the WAL commits. The UI updates immediately from
        # _enqueue_thumb_update (in-memory), so the DB write is pure
        # persistence — committing after *every* thumbnail (× 4 workers) was a
        # storm of fsyncs and lock churn during a folder sync. Commit every N
        # thumbs or once a second, and flush whatever's pending before idling.
        uncommitted = 0
        last_commit = time.monotonic()

        def flush_commit() -> None:
            nonlocal uncommitted, last_commit
            if uncommitted:
                try:
                    self.database.commit()
                except Exception:
                    LOGGER.debug("NC thumb commit failed", exc_info=True)
                uncommitted = 0
                last_commit = time.monotonic()

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
                    flush_commit()  # persist the batch before we go idle
                    # Wait briefly for new work; exit if queue stays empty so we
                    # don't keep idle threads alive forever.
                    if not self._nc_thumb_event.wait(timeout=15.0):
                        with self._nc_thumb_lock:
                            if not self._nc_thumb_queue:
                                self._nc_thumb_active_workers -= 1
                                return
                    continue
                thumb = None
                server_down_err: Exception | None = None
                try:
                    dav = dav_path_from_nc(path)
                    thumb = client.ensure_thumbnail(dav)
                except NextcloudConnectionError as exc:
                    # The server is unreachable (not just this one preview
                    # missing). Bail instead of grinding through the rest of
                    # the queue, ~20 s per tile, all failing the same way.
                    server_down_err = exc
                    LOGGER.warning("NC thumb fetch hit a dead server: %s", exc)
                except Exception:
                    LOGGER.debug("NC thumb fetch failed for %s", path, exc_info=True)
                finally:
                    with self._nc_thumb_lock:
                        self._nc_thumb_pending.discard(path)
                if server_down_err is not None:
                    # Trip the breaker + drain the queue, then retire this worker.
                    self._on_nc_sync_failed(server_down_err)
                    with self._nc_thumb_lock:
                        self._nc_thumb_active_workers -= 1
                    return
                if thumb:
                    try:
                        self.database.set_thumb(path, thumb, "nextcloud")
                        uncommitted += 1
                        if uncommitted >= 20 or time.monotonic() - last_commit >= 1.0:
                            flush_commit()
                    except Exception:
                        LOGGER.debug("NC thumb DB write failed for %s", path, exc_info=True)
                    self._enqueue_thumb_update(path, thumb)
        except Exception:
            LOGGER.exception("NC thumb worker crashed")
            with self._nc_thumb_lock:
                self._nc_thumb_active_workers = max(0, self._nc_thumb_active_workers - 1)
        finally:
            flush_commit()  # don't strand the last partial batch uncommitted
