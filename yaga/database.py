from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from .config import DB_PATH
from .models import MediaItem

LOGGER = logging.getLogger(__name__)


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

# v3 introduces an FTS5 trigram index over `media.name`. Trigram preserves
# the substring-match UX users expect ("ach" still finds "Bachstrasse"),
# while turning a full-table LIKE scan into an index probe — the dominant
# cost on libraries with >10k items, masked today by the search debounce.
# Stored as an external-content shadow table; AFTER triggers keep it in
# sync with media without code-side bookkeeping.
_FTS_CREATE_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5("
    "name, content='media', content_rowid='id', tokenize='trigram')"
)
_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS media_ai AFTER INSERT ON media BEGIN
    INSERT INTO media_fts(rowid, name) VALUES (new.id, new.name);
END;
CREATE TRIGGER IF NOT EXISTS media_ad AFTER DELETE ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, name) VALUES('delete', old.id, old.name);
END;
CREATE TRIGGER IF NOT EXISTS media_au AFTER UPDATE ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, name) VALUES('delete', old.id, old.name);
    INSERT INTO media_fts(rowid, name) VALUES (new.id, new.name);
END;
"""


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = path
        # Per-thread connection storage. Each thread that touches the DB gets
        # its own sqlite3.Connection (and its own RLock) on first access.
        # WAL mode lets concurrent connections read/write without Python-level
        # serialization, which is what previously caused the main thread to
        # stall during long scanner sync passes: a single shared connection
        # plus a global RLock meant a slow batch write would freeze
        # gallery rendering until the batch finished. busy_timeout handles
        # the rare write-vs-write race at the SQLite layer with a short
        # internal wait-and-retry instead of an exception.
        self._tls = threading.local()
        # Initialise the *current* thread's connection so the schema/migration
        # work below runs on a fully-configured handle.
        self._open_conn()
        with self.lock:
            self.conn.executescript(SCHEMA_V1)
            self._migrate()

    def _open_conn(self) -> "sqlite3.Connection":
        """Open a fresh sqlite3 connection for the calling thread, applying
        the same PRAGMAs as the main connection. Called lazily from the
        :pyattr:`conn` property the first time each thread touches the DB."""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL: readers + one writer in parallel, no SQLite-level blocking.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        # 5 s busy timeout so concurrent writers wait at the SQLite layer
        # instead of raising OperationalError("database is locked").
        conn.execute("PRAGMA busy_timeout=5000")
        self._tls.conn = conn
        return conn

    @property
    def conn(self) -> "sqlite3.Connection":
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = self._open_conn()
        return c

    @property
    def lock(self) -> threading.RLock:
        """Per-thread reentrant lock. Threads acquire *their own* lock — never
        each other's — so the historic ``with self.lock:`` guards still work
        within a single call site (executemany + ON CONFLICT etc.) without
        re-introducing cross-thread serialization that was the actual UI
        block during a long-running Nextcloud sync."""
        lk = getattr(self._tls, "lock", None)
        if lk is None:
            lk = threading.RLock()
            self._tls.lock = lk
        return lk

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
        # FTS5 trigram index for substring search on `name`. Behind a
        # try/except because older SQLite builds (or builds compiled
        # without FTS5/trigram) would otherwise refuse to open the DB.
        # Search falls back to LIKE when the table isn't there.
        self._has_fts = False
        try:
            self.conn.execute(_FTS_CREATE_SQL)
            if version < 3:
                # SQLite-recommended way to (re)populate an external-
                # content FTS5 index from scratch. An equivalent
                # `INSERT … SELECT … WHERE id NOT IN (SELECT rowid
                # FROM media_fts)` looked clever but the subquery
                # against the empty FTS5 table during the same
                # statement actually corrupts the trigram index — MATCH
                # silently returns nothing afterward, even though the
                # rows are visible via `SELECT rowid, name FROM
                # media_fts`. The 'rebuild' command is the documented
                # initial-population path and idempotent on its own.
                self.conn.execute("INSERT INTO media_fts(media_fts) VALUES('rebuild')")
                self.conn.executescript(_FTS_TRIGGERS_SQL)
                self.conn.execute("PRAGMA user_version = 3")
                self.conn.commit()
            self._has_fts = True
        except sqlite3.OperationalError as exc:
            LOGGER.warning(
                "FTS5 trigram index unavailable; search uses LIKE fallback: %s", exc,
            )

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

    def upsert_remote_media_bulk(self, rows: list[dict]) -> None:
        """Batched variant of upsert_remote_media — takes the lock once for the
        whole batch so the main thread can interleave reads between batches
        instead of fighting per-row lock acquisitions."""
        if not rows:
            return
        now = time.time()
        payload = [
            (
                r["path"], r["category"], r["media_type"], r["folder"],
                r["name"], r["mtime"], r["size"], r.get("thumb_path"), now,
            )
            for r in rows
        ]
        with self.lock:
            self.conn.executemany(
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
                payload,
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
    def _build_list_where(category: str, folder: str | None, include_nc: bool,
                          media_filter: str | None = None) -> tuple[str, list]:
        """Return (where_sql, args) for filtering by category (+ optional folder).
        Built-in image categories restrict to media_type='image'; the videos
        category aggregates videos across every source. *media_filter* overrides
        the per-category default for extras: "both" drops the type constraint,
        "videos" flips to videos-only, "images" keeps the image-only default."""
        if category == "videos":
            # Aggregate: every video on disk or NC, regardless of which root holds it.
            return "media_type = 'video'", []
        args: list = [category]
        if media_filter == "videos":
            local = "category = ? AND media_type = 'video'"
        elif media_filter == "both":
            local = "category = ?"
        else:
            local = "category = ? AND media_type = 'image'"
        if folder:
            local += " AND folder = ?"
            args.append(folder)
        if include_nc and category != "nextcloud":
            args.append("nextcloud")
            return f"({local}) OR (category = ? AND media_type = 'image')", args
        return local, args

    def list_media(self, category: str, sort_mode: str = "newest", folder: str | None = None,
                   include_nc: bool = False, media_filter: str | None = None) -> list[MediaItem]:
        order = {
            "newest":      "mtime DESC, name COLLATE NOCASE ASC",
            "oldest":      "mtime ASC, name COLLATE NOCASE ASC",
            "name":        "name COLLATE NOCASE ASC",
            "name_desc":   "name COLLATE NOCASE DESC",
            "folder":      "folder COLLATE NOCASE ASC, mtime DESC",
            "folder_desc": "folder COLLATE NOCASE DESC, mtime DESC",
        }.get(sort_mode, "mtime DESC")
        where, args = self._build_list_where(category, folder, include_nc, media_filter)
        with self.lock:
            rows = self.conn.execute(f"SELECT * FROM media WHERE {where} ORDER BY {order}", args).fetchall()
        return [self._row_to_item(row) for row in rows]

    def count_media(self, category: str, folder: str | None = None, include_nc: bool = False,
                    media_filter: str | None = None) -> int:
        """Return total count of media items (for pagination)."""
        where, args = self._build_list_where(category, folder, include_nc, media_filter)
        with self.lock:
            result = self.conn.execute(f"SELECT COUNT(*) FROM media WHERE {where}", args).fetchone()
        return result[0] if result else 0

    # Month-name → number lookup for search. Covers German + English, both
    # short and long forms. Lower-cased keys.
    _MONTH_LOOKUP: dict[str, int] = {
        "januar": 1, "january": 1, "jan": 1,
        "februar": 2, "february": 2, "feb": 2,
        "märz": 3, "marz": 3, "march": 3, "mar": 3, "mär": 3,
        "april": 4, "apr": 4,
        "mai": 5, "may": 5,
        "juni": 6, "june": 6, "jun": 6,
        "juli": 7, "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "september": 9, "sep": 9, "sept": 9,
        "oktober": 10, "october": 10, "oct": 10, "okt": 10,
        "november": 11, "nov": 11,
        "dezember": 12, "december": 12, "dec": 12, "dez": 12,
    }

    def _build_search_clause(self, query: str) -> tuple[str, list]:
        """Build a SQL OR-clause that matches the query against name, exif
        text, year, year-month or month-name. Returns ('1=1', []) for an
        empty query so the caller can drop it back into a WHERE."""
        import re
        q = (query or "").strip()
        if not q:
            return "1=1", []

        clauses: list[str] = []
        args: list = []
        like = f"%{q}%"
        # Filename. With the FTS5 trigram index this is an index probe
        # instead of a full-table LIKE scan; fall back to LIKE when the
        # index isn't there or the query is too short for trigram (it
        # tokenises as 3-grams and silently returns no rows for 1-2 char
        # queries — substring LIKE preserves the user's mental model in
        # those edge cases).
        if getattr(self, "_has_fts", False) and len(q) >= 3:
            # Wrap in double quotes so FTS5 special syntax (`OR`, `NEAR`,
            # parentheses, …) inside a filename can't be reinterpreted as
            # a query operator. Embedded double-quotes get the FTS5-spec
            # `""` escape.
            phrase = '"' + q.replace('"', '""') + '"'
            clauses.append(
                "id IN (SELECT rowid FROM media_fts WHERE media_fts MATCH ?)"
            )
            args.append(phrase)
        else:
            clauses.append("name LIKE ? COLLATE NOCASE")
            args.append(like)
        # EXIF blob — LIKE on a JSON text column is a full-table scan; only
        # bother when the user has typed enough that a hit is realistic.
        if len(q) >= 3:
            clauses.append("exif_data LIKE ?")
            args.append(like)
        # Year (4-digit number anywhere in the query).
        ym = re.search(r"(\d{4})[-/.](\d{1,2})", q)
        if ym:
            year, month = ym.group(1), int(ym.group(2))
            clauses.append(
                "(strftime('%Y', mtime, 'unixepoch') = ? "
                "AND CAST(strftime('%m', mtime, 'unixepoch') AS INTEGER) = ?)"
            )
            args.extend([year, month])
        else:
            year = re.search(r"\b(\d{4})\b", q)
            if year:
                clauses.append("strftime('%Y', mtime, 'unixepoch') = ?")
                args.append(year.group(1))
        # Month name
        q_low = q.lower()
        for name, num in Database._MONTH_LOOKUP.items():
            if name in q_low:
                clauses.append(
                    "CAST(strftime('%m', mtime, 'unixepoch') AS INTEGER) = ?"
                )
                args.append(num)
                break
        return "(" + " OR ".join(clauses) + ")", args

    def search_media_count(
        self, category: str, query: str, folder: str | None = None,
        include_nc: bool = False, media_filter: str | None = None,
    ) -> int:
        """Total number of items matching the search query in the given
        category/folder context. Mirrors search_media so paginated callers
        can know when to stop fetching."""
        base_where, args = self._build_list_where(category, folder, include_nc, media_filter)
        search_where, search_args = self._build_search_clause(query)
        full_where = f"({base_where}) AND {search_where}"
        args.extend(search_args)
        with self.lock:
            row = self.conn.execute(
                f"SELECT COUNT(*) FROM media WHERE {full_where}", args,
            ).fetchone()
        return row[0] if row else 0

    def search_media(
        self, category: str, query: str, sort_mode: str = "newest",
        folder: str | None = None, include_nc: bool = False,
        limit: int | None = None, offset: int = 0,
        media_filter: str | None = None,
    ) -> list[MediaItem]:
        """Filter media by a free-text query. Matches filename, EXIF text,
        year (4-digit), year-month (YYYY-MM / YYYY/MM / YYYY.MM) and locale
        month names (German + English)."""
        order = {
            "newest":      "mtime DESC, name COLLATE NOCASE ASC",
            "oldest":      "mtime ASC, name COLLATE NOCASE ASC",
            "name":        "name COLLATE NOCASE ASC",
            "name_desc":   "name COLLATE NOCASE DESC",
            "folder":      "folder COLLATE NOCASE ASC, mtime DESC",
            "folder_desc": "folder COLLATE NOCASE DESC, mtime DESC",
        }.get(sort_mode, "mtime DESC")
        base_where, args = self._build_list_where(category, folder, include_nc, media_filter)
        search_where, search_args = self._build_search_clause(query)
        full_where = f"({base_where}) AND {search_where}"
        args.extend(search_args)
        sql = f"SELECT * FROM media WHERE {full_where} ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args.extend([int(limit), int(offset)])
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [self._row_to_item(row) for row in rows]

    def list_media_paginated(
        self, category: str, sort_mode: str = "newest", folder: str | None = None,
        limit: int = 100, offset: int = 0, include_nc: bool = False,
        media_filter: str | None = None,
    ) -> list[MediaItem]:
        """Return paginated media items with LIMIT and OFFSET."""
        order = {
            "newest":      "mtime DESC, name COLLATE NOCASE ASC",
            "oldest":      "mtime ASC, name COLLATE NOCASE ASC",
            "name":        "name COLLATE NOCASE ASC",
            "name_desc":   "name COLLATE NOCASE DESC",
            "folder":      "folder COLLATE NOCASE ASC, mtime DESC",
            "folder_desc": "folder COLLATE NOCASE DESC, mtime DESC",
        }.get(sort_mode, "mtime DESC")
        where, args = self._build_list_where(category, folder, include_nc, media_filter)
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

    def child_folders(self, category: str, parent: str | None,
                      media_filter: str | None = None) -> list[tuple[str, int, list]]:
        if category == "videos":
            sql = "SELECT folder, thumb_path FROM media WHERE media_type = 'video' ORDER BY mtime DESC"
            params: tuple = ()
        elif media_filter == "videos":
            sql = "SELECT folder, thumb_path FROM media WHERE category = ? AND media_type = 'video' ORDER BY mtime DESC"
            params = (category,)
        elif media_filter == "both":
            sql = "SELECT folder, thumb_path FROM media WHERE category = ? ORDER BY mtime DESC"
            params = (category,)
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
            # Skip empty remainders (stray trailing slashes). The previous
            # `"/" not in remainder and folder == parent` clause was dead
            # after the startswith check — parent_prefix is parent + "/",
            # so a remainder ever existing implies folder != parent.
            if not remainder:
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
