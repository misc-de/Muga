from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .database import Database
from .models import media_type_for
from .thumbnails import Thumbnailer

LOGGER = logging.getLogger(__name__)

# Files processed per thumbnail batch. Big enough to amortise pool startup over
# real decode work, small enough to bound memory and keep the UI updating.
_THUMB_CHUNK = 64
# Cap pool width so a phone with many cores doesn't thrash on RAM/IO.
_THUMB_WORKERS = min(os.cpu_count() or 4, 8)
# Commit roughly every this many thumbnail chunks during a long local scan.
# Bounds WAL growth (a single end-of-scan commit lets it balloon on a 50k-file
# first scan) and gives the main thread a clean read window between commits.
_COMMIT_EVERY_CHUNKS = 16
# How many unchanged files to accumulate before flushing a seen_at touch.
_TOUCH_BATCH = 512
# An mtime read back from SQLite (REAL) round-trips a Python float losslessly,
# but allow a hair of slack so a sub-microsecond representation wobble never
# forces a needless re-decode.
_MTIME_EPS = 1e-6


class MediaScanner:
    def __init__(self, database: Database, thumbnailer: Thumbnailer) -> None:
        self.database = database
        self.thumbnailer = thumbnailer
        # Categories whose root location is gone (missing on disk / 404 on server).
        # Maps category → human-readable folder name shown in the empty state.
        self.missing_root: dict[str, str] = {}

    def scan(self, categories: list[tuple[str, str, str]], nc_client=None,
             nc_thumbnail_only: bool = True,
             excluded_subtrees: list[str] | None = None) -> bool:
        """Scan *categories* into the index. Returns True iff anything actually
        changed (a file was added/updated, or a stale row pruned) so the caller
        can skip re-rendering the gallery when nothing did."""
        started = time.time()
        scanned_categories: list[str] = []
        excluded_paths = [Path(p).expanduser() for p in (excluded_subtrees or [])]
        changed_rows = 0

        for category, _label, root_text in categories:
            if category == "nextcloud":
                if nc_client is not None:
                    self._scan_nextcloud(nc_client, root_text, thumbnail_only=nc_thumbnail_only)
                    scanned_categories.append(category)
                continue
            root = Path(root_text).expanduser()
            if not root.exists():
                # Root is gone (typical case: unmounted USB / SD-card). Preserve
                # the cached index — the user wants thumbnails to stay visible
                # until the drive is reconnected. We deliberately do NOT add
                # this category to scanned_categories so prune_missing leaves
                # its rows alone. Truly-removed folders can be cleaned up via
                # the Settings → Folders UI.
                self.missing_root[category] = str(root)
                LOGGER.info("%s: Ordner %s nicht gefunden, übersprungen", category, root)
                continue
            self.missing_root.pop(category, None)
            scanned_categories.append(category)
            if root.is_symlink():
                LOGGER.warning("Skipping symlink root folder: %s", root)
                continue

            # Subtrees flagged "do not inherit" that live *strictly* inside the
            # current root: their content belongs to their own category only.
            # `is_relative_to(root)` would also be True for root itself; that
            # case is excluded so a no-inherit folder still gets scanned as
            # its own category.
            skip_prefixes: list[Path] = []
            for ex in excluded_paths:
                try:
                    if ex != root and ex.is_relative_to(root):
                        skip_prefixes.append(ex)
                except ValueError:
                    continue

            cat_start = time.time()
            LOGGER.info("%s: durchsuche %s …", category, root)
            file_count = 0
            thumb_new = 0
            skipped = 0
            # Snapshot of what's already indexed for this category: lets us
            # skip files whose mtime+size are unchanged — no re-decode, no row
            # rewrite, just a cheap seen_at touch so prune_missing spares them.
            index = self.database.load_scan_index(category)
            touch_batch: list[str] = []
            # Decoding thumbnails is the dominant cost of a first scan and is
            # independent per file, so we generate them a chunk at a time on a
            # thread pool. DB writes stay on this thread; chunking bounds memory
            # and keeps results landing progressively for the UI.
            chunk: list[tuple[Path, str, str, bool, os.stat_result]] = []
            chunks_since_commit = 0
            for path, st in self._iter_media_files(root, skip_prefixes):
                media_type = media_type_for(path)
                if not media_type:
                    continue
                file_count += 1
                prev = index.get(str(path))
                thumb_file = self.thumbnailer.thumb_path_for(path)
                thumb_exists = thumb_file.exists()
                unchanged = (
                    prev is not None
                    and abs(prev[0] - st.st_mtime) <= _MTIME_EPS
                    and prev[1] == st.st_size
                )
                if unchanged and thumb_exists:
                    # Nothing to do but keep the row alive past prune_missing.
                    skipped += 1
                    touch_batch.append(str(path))
                    if len(touch_batch) >= _TOUCH_BATCH:
                        self.database.touch_seen(touch_batch, category)
                        touch_batch = []
                    continue
                if prev is not None and not unchanged and thumb_exists:
                    # File changed in place. thumb_path_for keys on the *path*,
                    # so the cached thumbnail is now stale — drop it so the
                    # decode below regenerates it instead of serving the old one.
                    try:
                        thumb_file.unlink()
                    except OSError:
                        pass
                    thumb_exists = False
                folder = self._relative_folder(root, path.parent)
                chunk.append((path, media_type, folder, not thumb_exists, st))
                if len(chunk) >= _THUMB_CHUNK:
                    thumb_new += self._flush_thumb_chunk(chunk, category)
                    chunk = []
                    chunks_since_commit += 1
                    if chunks_since_commit >= _COMMIT_EVERY_CHUNKS:
                        self.database.commit()
                        chunks_since_commit = 0
            thumb_new += self._flush_thumb_chunk(chunk, category)
            if touch_batch:
                self.database.touch_seen(touch_batch, category)
            # Non-skipped files are exactly the new/changed ones we upserted.
            changed_rows += file_count - skipped
            LOGGER.info(
                "%s: %d Dateien (%d unverändert übersprungen), %d neue Thumbnails (%.1fs)",
                category, file_count, skipped, thumb_new, time.time() - cat_start,
            )

        db_start = time.time()
        pruned = self.database.prune_missing(started, scanned_categories)
        self.database.commit()
        LOGGER.info(
            "Datenbank bereinigt & gespeichert (%.1fs) — Gesamtzeit: %.1fs",
            time.time() - db_start, time.time() - started,
        )
        return changed_rows > 0 or pruned > 0

    def _scan_nextcloud(self, client, photos_path: str, thumbnail_only: bool = True) -> None:
        from .nextcloud import nc_path
        LOGGER.info("Scanning Nextcloud folder %r", photos_path)
        files = client.list_files(photos_path)
        LOGGER.info("Found %s Nextcloud file(s)", len(files))
        dav_root = client.dav_root + "/"
        for info in files:
            dav = info["dav_path"]
            media_type = media_type_for(Path(info["name"]))
            if not media_type:
                continue
            thumb = client.ensure_thumbnail(dav)
            if not thumbnail_only:
                client.download_file(dav)
            folder = self._nc_folder(dav, dav_root, photos_path)
            self.database.upsert_remote_media(
                path=nc_path(dav),
                category="nextcloud",
                media_type=media_type,
                folder=folder,
                name=info["name"],
                mtime=info["mtime"],
                size=info["size"],
                thumb_path=thumb,
            )

    def _nc_folder(self, dav_path: str, dav_root: str, photos_path: str) -> str:
        """Return a relative folder path for an NC file, rooted at photos_path."""
        # Strip dav_root prefix to get the user-relative path
        rel = dav_path[len(dav_root):] if dav_path.startswith(dav_root) else dav_path.lstrip("/")
        # Strip the photos_path prefix
        photos_prefix = photos_path.strip("/") + "/"
        if rel.startswith(photos_prefix):
            rel = rel[len(photos_prefix):]
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        return parent if parent else "/"

    def _iter_media_files(self, root: Path, skip_prefixes: list[Path]):
        """Yield ``(Path, os.stat_result)`` for every non-symlink file under
        *root*, depth-first.

        Uses ``os.scandir`` so each entry's type and stat come from a single
        directory read: on Linux ``d_type`` answers is_symlink/is_dir/is_file
        without a syscall, and ``entry.stat()`` is the one stat we then hand to
        ``upsert_media`` so it doesn't stat the same file a second time.
        ``Path.rglob`` plus the old per-file is_symlink/is_file/stat calls cost
        three-to-four syscalls per file — a real tax on SD-card libraries."""
        stack = [str(root)]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        LOGGER.debug("Skipping symlink: %s", entry.path)
                        continue
                    if entry.is_dir():
                        dpath = Path(entry.path)
                        # Don't descend into a no-inherit subtree — it's scanned
                        # under its own category elsewhere.
                        if skip_prefixes and any(
                            dpath == sp or sp in dpath.parents for sp in skip_prefixes
                        ):
                            continue
                        stack.append(entry.path)
                        continue
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    # Permission denied, vanished mid-scan, broken entry, …
                    LOGGER.debug("Skipping path that cannot be stat'd: %s", entry.path)
                    continue
                yield Path(entry.path), st

    def _flush_thumb_chunk(
        self, chunk: list[tuple[Path, str, str, bool, os.stat_result]], category: str,
    ) -> int:
        """Generate thumbnails for *chunk*, then upsert each row. Returns the
        number of newly-created thumbnails.

        Thumbnail decode runs on a thread pool, but only when at least two
        files in the chunk actually need a new thumbnail — on a re-scan
        ``ensure_thumbnail`` just returns the cached path, so a serial pass is
        cheaper than spinning up workers. The DB writes always stay on this
        (the scanner) thread, preserving the existing single-writer model and
        discovery order. The stat captured during the walk is threaded through
        to upsert_media so the row write doesn't re-stat the file."""
        if not chunk:
            return 0
        new_needed = sum(1 for row in chunk if row[3])
        if new_needed >= 2:
            workers = min(new_needed, _THUMB_WORKERS)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                thumbs = list(pool.map(
                    lambda row: self.thumbnailer.ensure_thumbnail(row[0], row[1]),
                    chunk,
                ))
        else:
            thumbs = [
                self.thumbnailer.ensure_thumbnail(path, media_type)
                for path, media_type, _folder, _need, _st in chunk
            ]
        thumb_new = 0
        for (path, media_type, folder, need_thumb, st), thumb in zip(chunk, thumbs):
            if need_thumb and thumb:
                thumb_new += 1
            self.database.upsert_media(
                path=path, category=category, media_type=media_type,
                folder=folder, thumb_path=thumb, stat=st,
            )
        return thumb_new

    def _relative_folder(self, root: Path, folder: Path) -> str:
        try:
            rel = folder.relative_to(root)
        except ValueError:
            return str(folder)
        if str(rel) == ".":
            return "/"
        return str(rel)

    def scan_nc_structure(self, client, photos_path: str) -> bool:
        """Scan NC folder structure and store metadata without downloading
        thumbnails. Returns True iff the NC index actually changed (a file was
        added/updated, or a stale row pruned) so the caller can skip a
        re-render when the remote is unchanged."""
        from .nextcloud import nc_path
        started = time.time()
        LOGGER.info("Nextcloud structure scan started for %r", photos_path)
        try:
            files = client.list_files(photos_path)
        except FileNotFoundError:
            # Photos folder no longer exists on the server — drop all NC entries.
            LOGGER.warning("Nextcloud Photos folder %r is gone — pruning all NC entries", photos_path)
            self.missing_root["nextcloud"] = photos_path
            removed = self.database.prune_missing(started, ["nextcloud"])
            self.database.commit()
            LOGGER.info("Pruned %d stale NC entries (folder vanished)", removed)
            return removed > 0
        except Exception as e:
            # A timeout / refused / auth (401) / TLS failure is NOT "nothing
            # changed" — swallowing it as `return False` left the UI spinning
            # with no feedback while the user had no idea the connection broke.
            # Bubble it up so the caller can mark the connection broken, tell
            # the user, and stop retrying against a dead server.
            LOGGER.warning("Nextcloud structure scan failed: %s", e)
            raise
        LOGGER.info("Nextcloud: %d Dateien empfangen, schreibe in DB …", len(files))
        self.missing_root.pop("nextcloud", None)
        dav_root = client.dav_root + "/"
        # Same unchanged-file skip as the local scan: re-upserting every NC row
        # on each sync (and firing the FTS triggers) is wasted work when the
        # remote hasn't changed. Compare mtime+size against the stored index;
        # unchanged entries just get a seen_at touch so prune spares them.
        index = self.database.load_scan_index("nextcloud")
        changed = 0
        skipped = 0
        # Batched upserts: holding the DB lock once per batch (instead of per
        # row) lets the main thread interleave its render/scroll reads, which
        # is the difference between visible UI stutter and a smooth sync.
        BATCH_SIZE = 100
        batch: list[dict] = []
        touch_batch: list[str] = []
        for info in files:
            dav = info["dav_path"]
            media_type = media_type_for(Path(info["name"]))
            if not media_type:
                continue
            p = nc_path(dav)
            prev = index.get(p)
            if (prev is not None
                    and abs(prev[0] - info["mtime"]) <= _MTIME_EPS
                    and prev[1] == info["size"]):
                skipped += 1
                touch_batch.append(p)
                if len(touch_batch) >= _TOUCH_BATCH:
                    self.database.touch_seen(touch_batch, "nextcloud")
                    touch_batch = []
                continue
            folder = self._nc_folder(dav, dav_root, photos_path)
            changed += 1
            batch.append({
                "path": p,
                "category": "nextcloud",
                "media_type": media_type,
                "folder": folder,
                "name": info["name"],
                "mtime": info["mtime"],
                "size": info["size"],
                "thumb_path": None,
            })
            if len(batch) >= BATCH_SIZE:
                self.database.upsert_remote_media_bulk(batch)
                batch = []
                # Real preemption window — gives the main loop ~10ms to satisfy
                # any pending DB reads before we grab the lock again.
                time.sleep(0.01)
        if batch:
            self.database.upsert_remote_media_bulk(batch)
        if touch_batch:
            self.database.touch_seen(touch_batch, "nextcloud")
        removed = self.database.prune_missing(started, ["nextcloud"])
        self.database.commit()
        LOGGER.info(
            "Nextcloud structure scan: %d changed, %d unchanged, %d pruned, in %.2fs",
            changed, skipped, removed, time.time() - started,
        )
        return changed > 0 or removed > 0

    def load_nc_folder_thumbs(self, client, folder: str, on_thumb_loaded) -> None:
        """Download thumbnails only for NC items in *folder* that don't have one yet."""
        from .nextcloud import dav_path_from_nc
        started = time.time()
        items = self.database.list_media("nextcloud", "newest", folder)
        missing = 0
        loaded = 0
        for item in items:
            if item.thumb_path:
                continue
            missing += 1
            dav = dav_path_from_nc(item.path)
            thumb = client.ensure_thumbnail(dav)
            if thumb:
                loaded += 1
                self.database.set_thumb(item.path, thumb, "nextcloud")
                on_thumb_loaded(item.path, thumb)
        self.database.commit()
        LOGGER.info(
            "Nextcloud thumbnail sync for folder %r loaded %s/%s thumbnail(s) in %.2fs",
            folder,
            loaded,
            missing,
            time.time() - started,
        )
