from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .config import DB_PATH
from .models import MediaItem


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    category TEXT NOT NULL,
    media_type TEXT NOT NULL,
    folder TEXT NOT NULL,
    name TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    thumb_path TEXT,
    seen_at REAL NOT NULL,
    UNIQUE(path, category)
);
CREATE INDEX IF NOT EXISTS idx_media_category ON media(category);
CREATE INDEX IF NOT EXISTS idx_media_folder ON media(folder);
CREATE INDEX IF NOT EXISTS idx_media_mtime ON media(mtime);
"""

_MIGRATION_V1 = """
CREATE TABLE media_new (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    category TEXT NOT NULL,
    media_type TEXT NOT NULL,
    folder TEXT NOT NULL,
    name TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    thumb_path TEXT,
    seen_at REAL NOT NULL,
    UNIQUE(path, category)
);
INSERT OR IGNORE INTO media_new
    SELECT id, path, category, media_type, folder, name, mtime, size, thumb_path, seen_at FROM media;
DROP TABLE media;
ALTER TABLE media_new RENAME TO media;
CREATE INDEX IF NOT EXISTS idx_media_category ON media(category);
CREATE INDEX IF NOT EXISTS idx_media_folder ON media(folder);
CREATE INDEX IF NOT EXISTS idx_media_mtime ON media(mtime);
PRAGMA user_version = 1;
"""

_MIGRATION_V2 = """
ALTER TABLE media ADD COLUMN exif_data TEXT DEFAULT NULL;
PRAGMA user_version = 2;
"""


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(SCHEMA_V1)
            self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            # Check if old schema (UNIQUE on path alone) is in use
            info = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='media'"
            ).fetchone()
            if info and "UNIQUE(path, category)" not in info["sql"]:
                self.conn.executescript(_MIGRATION_V1)
        if version < 2:
            # Add EXIF cache column
            try:
                self.conn.execute("ALTER TABLE media ADD COLUMN exif_data TEXT DEFAULT NULL")
                self.conn.execute("PRAGMA user_version = 2")
                self.conn.commit()
            except sqlite3.OperationalError:
                # Column already exists
                pass

    def upsert_media(self, *, path: Path, category: str, media_type: str, folder: str, thumb_path: str | None) -> None:
        stat = path.stat()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO media(path, category, media_type, folder, name, mtime, size, thumb_path, seen_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path, category) DO UPDATE SET
                    media_type=excluded.media_type,
                    folder=excluded.folder,
                    name=excluded.name,
                    mtime=excluded.mtime,
                    size=excluded.size,
                    thumb_path=COALESCE(excluded.thumb_path, media.thumb_path),
                    seen_at=excluded.seen_at
                """,
                (str(path), category, media_type, folder, path.name, stat.st_mtime, stat.st_size, thumb_path, time.time()),
            )

    def upsert_remote_media(self, *, path: str, category: str, media_type: str, folder: str,
                             name: str, mtime: float, size: int, thumb_path: str | None) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO media(path, category, media_type, folder, name, mtime, size, thumb_path, seen_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path, category) DO UPDATE SET
                    media_type=excluded.media_type,
                    folder=excluded.folder,
                    name=excluded.name,
                    mtime=excluded.mtime,
                    size=excluded.size,
                    thumb_path=COALESCE(excluded.thumb_path, media.thumb_path),
                    seen_at=excluded.seen_at
                """,
                (path, category, media_type, folder, name, mtime, size, thumb_path, time.time()),
            )

    def prune_missing(self, seen_since: float, categories: list[str]) -> int:
        if not categories:
            return 0
        placeholders = ",".join("?" for _category in categories)
        with self.lock:
            stale = self.conn.execute(
                f"SELECT thumb_path FROM media WHERE seen_at < ? AND category IN ({placeholders})",
                [seen_since, *categories],
            ).fetchall()
            self.conn.execute(
                f"DELETE FROM media WHERE seen_at < ? AND category IN ({placeholders})",
                [seen_since, *categories],
            )
        for row in stale:
            thumb = row["thumb_path"]
            if thumb:
                try:
                    Path(thumb).unlink(missing_ok=True)
                except OSError:
                    pass
        return len(stale)

    def set_thumb(self, path: str, thumb_path: str, category: str | None = None) -> None:
        with self.lock:
            if category is not None:
                self.conn.execute(
                    "UPDATE media SET thumb_path = ? WHERE path = ? AND category = ?",
                    (thumb_path, path, category),
                )
            else:
                self.conn.execute("UPDATE media SET thumb_path = ? WHERE path = ?", (thumb_path, path))

    def set_exif_data(self, path: str, exif_json: str, category: str | None = None) -> None:
        """Store cached EXIF data (JSON) for a media item."""
        with self.lock:
            if category is not None:
                self.conn.execute(
                    "UPDATE media SET exif_data = ? WHERE path = ? AND category = ?",
                    (exif_json, path, category),
                )
            else:
                self.conn.execute("UPDATE media SET exif_data = ? WHERE path = ?", (exif_json, path))

    def get_exif_data(self, path: str, category: str | None = None) -> str | None:
        """Retrieve cached EXIF data (JSON) for a media item."""
        with self.lock:
            if category is not None:
                row = self.conn.execute(
                    "SELECT exif_data FROM media WHERE path = ? AND category = ?", (path, category)
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT exif_data FROM media WHERE path = ?", (path,)
                ).fetchone()
        return row["exif_data"] if row else None

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    @staticmethod
    def _build_list_where(category: str, folder: str | None, include_nc: bool) -> tuple[str, list]:
        """Return (where_sql, args) for filtering by category (+ optional folder).
        Image categories (pictures/photos/screenshots/nextcloud/...) restrict to
        media_type='image'. The videos category aggregates videos across every
        source. When include_nc is True for an image category, NC images are
        merged in regardless of folder."""
        if category == "videos":
            # Aggregate: every video on disk or NC, regardless of which root holds it.
            return "media_type = 'video'", []
        args: list = [category]
        local = "category = ? AND media_type = 'image'"
        if folder:
            local += " AND folder = ?"
            args.append(folder)
        if include_nc and category != "nextcloud":
            args.append("nextcloud")
            return f"({local}) OR (category = ? AND media_type = 'image')", args
        return local, args

    def list_media(self, category: str, sort_mode: str = "newest", folder: str | None = None,
                   include_nc: bool = False) -> list[MediaItem]:
        order = {
            "newest": "mtime DESC, name COLLATE NOCASE ASC",
            "oldest": "mtime ASC, name COLLATE NOCASE ASC",
            "name": "name COLLATE NOCASE ASC",
            "folder": "folder COLLATE NOCASE ASC, mtime DESC",
        }.get(sort_mode, "mtime DESC")
        where, args = self._build_list_where(category, folder, include_nc)
        with self.lock:
            rows = self.conn.execute(f"SELECT * FROM media WHERE {where} ORDER BY {order}", args).fetchall()
        return [self._row_to_item(row) for row in rows]

    def count_media(self, category: str, folder: str | None = None, include_nc: bool = False) -> int:
        """Return total count of media items (for pagination)."""
        where, args = self._build_list_where(category, folder, include_nc)
        with self.lock:
            result = self.conn.execute(f"SELECT COUNT(*) FROM media WHERE {where}", args).fetchone()
        return result[0] if result else 0

    def list_media_paginated(
        self, category: str, sort_mode: str = "newest", folder: str | None = None,
        limit: int = 100, offset: int = 0, include_nc: bool = False,
    ) -> list[MediaItem]:
        """Return paginated media items with LIMIT and OFFSET."""
        order = {
            "newest": "mtime DESC, name COLLATE NOCASE ASC",
            "oldest": "mtime ASC, name COLLATE NOCASE ASC",
            "name": "name COLLATE NOCASE ASC",
            "folder": "folder COLLATE NOCASE ASC, mtime DESC",
        }.get(sort_mode, "mtime DESC")
        where, args = self._build_list_where(category, folder, include_nc)
        args.extend([limit, offset])
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM media WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", args
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get_media_by_path(self, path: str, category: str | None = None) -> MediaItem | None:
        with self.lock:
            if category is not None:
                row = self.conn.execute(
                    "SELECT * FROM media WHERE path = ? AND category = ?", (path, category)
                ).fetchone()
            else:
                row = self.conn.execute("SELECT * FROM media WHERE path = ?", (path,)).fetchone()
        return self._row_to_item(row) if row else None

    def folders(self, category: str) -> list[tuple[str, int, str | None]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT folder, COUNT(*) AS count, MAX(thumb_path) AS thumb
                FROM media
                WHERE category = ?
                GROUP BY folder
                ORDER BY folder COLLATE NOCASE ASC
                """,
                (category,),
            ).fetchall()
        return [(row["folder"], row["count"], row["thumb"]) for row in rows]

    def child_folders(self, category: str, parent: str | None) -> list[tuple[str, int, list]]:
        if category == "videos":
            sql = "SELECT folder, thumb_path FROM media WHERE media_type = 'video' ORDER BY mtime DESC"
            params: tuple = ()
        else:
            sql = "SELECT folder, thumb_path FROM media WHERE category = ? AND media_type = 'image' ORDER BY mtime DESC"
            params = (category,)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        children: dict[str, tuple[int, list]] = {}
        parent_prefix = "" if parent in (None, "/") else f"{parent}/"
        for row in rows:
            folder = row["folder"]
            if folder == "/":
                continue
            if parent in (None, "/"):
                remainder = folder
            elif folder.startswith(parent_prefix):
                remainder = folder[len(parent_prefix):]
            else:
                continue
            if not remainder or "/" not in remainder and folder == parent:
                continue
            child_name = remainder.split("/", 1)[0]
            child_path = child_name if parent in (None, "/") else f"{parent}/{child_name}"
            count, thumbs = children.get(child_path, (0, []))
            t = row["thumb_path"]
            if t and t not in thumbs and len(thumbs) < 4:
                thumbs = thumbs + [t]
            children[child_path] = (count + 1, thumbs)
        return [(f, c, t) for f, (c, t) in sorted(children.items(), key=lambda x: x[0].lower())]

    def delete_path(self, path: str, category: str | None = None) -> None:
        with self.lock:
            if category is not None:
                self.conn.execute(
                    "DELETE FROM media WHERE path = ? AND category = ?", (path, category)
                )
            else:
                self.conn.execute("DELETE FROM media WHERE path = ?", (path,))
            self.conn.commit()

    def clear_category(self, category: str) -> None:
        """Delete all DB rows for a category and remove their thumbnail files."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT thumb_path FROM media WHERE category = ?", (category,)
            ).fetchall()
        for row in rows:
            if row["thumb_path"]:
                try:
                    Path(row["thumb_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
        with self.lock:
            self.conn.execute("DELETE FROM media WHERE category = ?", (category,))
            self.conn.commit()

    def _row_to_item(self, row: sqlite3.Row) -> MediaItem:
        return MediaItem(
            id=row["id"],
            path=row["path"],
            category=row["category"],
            media_type=row["media_type"],
            folder=row["folder"],
            name=row["name"],
            mtime=row["mtime"],
            size=row["size"],
            thumb_path=row["thumb_path"],
        )
