from __future__ import annotations

import logging
import time
from pathlib import Path

from .database import Database
from .models import media_type_for
from .thumbnails import Thumbnailer

LOGGER = logging.getLogger(__name__)


class MediaScanner:
    def __init__(self, database: Database, thumbnailer: Thumbnailer) -> None:
        self.database = database
        self.thumbnailer = thumbnailer
        self._visited_inodes: set[tuple] = set()  # Track (device, inode) to prevent symlink loops
        # Categories whose root location is gone (missing on disk / 404 on server).
        # Maps category → human-readable folder name shown in the empty state.
        self.missing_root: dict[str, str] = {}

    def scan(self, categories: list[tuple[str, str, str]], nc_client=None,
             nc_thumbnail_only: bool = True) -> None:
        started = time.time()
        scanned_categories: list[str] = []
        self._visited_inodes.clear()  # Reset for fresh scan
        
        for category, _label, root_text in categories:
            if category == "nextcloud":
                if nc_client is not None:
                    self._scan_nextcloud(nc_client, root_text, thumbnail_only=nc_thumbnail_only)
                    scanned_categories.append(category)
                continue
            root = Path(root_text).expanduser()
            if not root.exists():
                # Mark the category as missing AND prune so the gallery doesn't keep
                # showing stale entries for a folder the user has deleted. (If a user
                # ever wants to preserve the index across an unmount, they can simply
                # not refresh while the drive is offline.)
                self.missing_root[category] = str(root)
                scanned_categories.append(category)
                continue
            self.missing_root.pop(category, None)
            scanned_categories.append(category)
            if root.is_symlink():
                LOGGER.warning("Skipping symlink root folder: %s", root)
                continue
            
            for path in root.rglob("*"):
                # Skip symlinks to prevent infinite loops and unexpected behavior
                if path.is_symlink():
                    LOGGER.debug("Skipping symlink: %s", path)
                    continue
                
                if not path.is_file():
                    continue
                
                # Optional: Track visited directories (inode) to detect symlink loops
                # This provides defense-in-depth if rglob() encounters symlinks
                try:
                    stat = path.stat()
                    inode_key = (stat.st_dev, stat.st_ino)
                    if inode_key in self._visited_inodes and path.is_dir():
                        LOGGER.debug("Skipping already-visited directory (potential symlink loop): %s", path)
                        continue
                    if path.is_dir():
                        self._visited_inodes.add(inode_key)
                except (OSError, ValueError):
                    # If we can't stat the path, skip it (broken symlink, permission denied, etc.)
                    LOGGER.debug("Skipping path that cannot be stat'd: %s", path)
                    continue
                
                media_type = media_type_for(path)
                if not media_type:
                    continue
                folder = self._relative_folder(root, path.parent)
                thumb = self.thumbnailer.ensure_thumbnail(path, media_type)
                self.database.upsert_media(path=path, category=category, media_type=media_type, folder=folder, thumb_path=thumb)
        
        self.database.prune_missing(started, scanned_categories)
        self.database.commit()

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

    def _relative_folder(self, root: Path, folder: Path) -> str:
        try:
            rel = folder.relative_to(root)
        except ValueError:
            return str(folder)
        if str(rel) == ".":
            return "/"
        return str(rel)

    def scan_nc_structure(self, client, photos_path: str) -> None:
        """Scan NC folder structure and store metadata without downloading thumbnails."""
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
            return
        except Exception as e:
            LOGGER.exception("Nextcloud structure scan failed: %s", e)
            return
        self.missing_root.pop("nextcloud", None)
        dav_root = client.dav_root + "/"
        upserted = 0
        # Batched upserts: holding the DB lock once per batch (instead of per
        # row) lets the main thread interleave its render/scroll reads, which
        # is the difference between visible UI stutter and a smooth sync.
        BATCH_SIZE = 100
        batch: list[dict] = []
        for info in files:
            dav = info["dav_path"]
            media_type = media_type_for(Path(info["name"]))
            if not media_type:
                continue
            folder = self._nc_folder(dav, dav_root, photos_path)
            batch.append({
                "path": nc_path(dav),
                "category": "nextcloud",
                "media_type": media_type,
                "folder": folder,
                "name": info["name"],
                "mtime": info["mtime"],
                "size": info["size"],
                "thumb_path": None,
            })
            upserted += 1
            if len(batch) >= BATCH_SIZE:
                self.database.upsert_remote_media_bulk(batch)
                batch = []
                # Real preemption window — gives the main loop ~10ms to satisfy
                # any pending DB reads before we grab the lock again.
                time.sleep(0.01)
        if batch:
            self.database.upsert_remote_media_bulk(batch)
        removed = self.database.prune_missing(started, ["nextcloud"])
        self.database.commit()
        LOGGER.info(
            "Nextcloud structure scan indexed %s file(s), pruned %d stale, in %.2fs",
            upserted, removed, time.time() - started,
        )

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
