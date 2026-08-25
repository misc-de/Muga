from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from .config import CACHE_DIR, DB_PATH, LEGACY_CACHE_DIR
from .models import MediaItem

LOGGER = logging.getLogger(__name__)

# Attempts for a write that finds the database busy (exponential backoff,
# 0.05 s doubling — ~0.75 s of patience in total).
_WRITE_RETRIES = 5


_MIGRATION_V10 = """
CREATE INDEX IF NOT EXISTS idx_media_cat_type_folder_taken_name
    ON media(category, media_type, folder, COALESCE(taken_at, mtime) DESC, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_media_cat_type_taken_name
    ON media(category, media_type, COALESCE(taken_at, mtime) DESC, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_media_type_taken_name
    ON media(media_type, COALESCE(taken_at, mtime) DESC, name COLLATE NOCASE);
"""

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
    -- EXIF capture time, NULL when the file has none. Kept beside mtime
    -- rather than replacing it: mtime always exists and still drives the
    -- gallery's sort order, while date search prefers this when present.
    taken_at REAL DEFAULT NULL,
    UNIQUE(path, category)
);
CREATE INDEX IF NOT EXISTS idx_media_category ON media(category);
CREATE INDEX IF NOT EXISTS idx_media_folder ON media(folder);
CREATE INDEX IF NOT EXISTS idx_media_mtime ON media(mtime);
CREATE INDEX IF NOT EXISTS idx_media_cat_type_folder_mtime_name
    ON media(category, media_type, folder, mtime DESC, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_media_cat_type_mtime_name
    ON media(category, media_type, mtime DESC, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_media_type_mtime_name
    ON media(media_type, mtime DESC, name COLLATE NOCASE);
-- Serves the one-row-per-file subquery the aggregate views use. Without a
-- (media_type, path) index SQLite filters on media_type and then sorts the
-- whole result into a temp B-tree to GROUP BY path; on a 12k-row index that
-- was the difference between 42 ms and 24 ms per Overview count.
CREATE INDEX IF NOT EXISTS idx_media_type_path ON media(media_type, path);
-- The capture-date indexes are deliberately NOT here. This script runs with
-- CREATE TABLE IF NOT EXISTS against databases whose media table predates
-- taken_at, and an index naming a column that does not exist yet raises
-- "no such column" — which the constructor treats as an unusable database and
-- answers by discarding the whole index. They live in _MIGRATION_V10, which
-- runs after the column has been added and covers fresh databases too
-- (a new file starts at user_version 0 and walks every step).
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

# v3 introduced an FTS5 trigram index over `media.name`; v6 widened it to
# also cover `media.exif_data` so an EXIF substring search is an index probe
# instead of the full-table `LIKE '%q%'` scan over a JSON blob it used to be
# (the dominant search cost on libraries with EXIF cached for many items).
# Trigram preserves the substring-match UX users expect ("ach" still finds
# "Bachstrasse"). Stored as an external-content shadow table; AFTER triggers
# keep it in sync with media without code-side bookkeeping. The UPDATE
# trigger fires only when an indexed column (name / exif_data) is assigned,
# so the scanner's frequent seen_at/thumb_path/mtime touch-ups don't churn
# the index.
_FTS_CREATE_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5("
    "name, exif_data, content='media', content_rowid='id', tokenize='trigram')"
)
_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS media_ai AFTER INSERT ON media BEGIN
    INSERT INTO media_fts(rowid, name, exif_data) VALUES (new.id, new.name, new.exif_data);
END;
CREATE TRIGGER IF NOT EXISTS media_ad AFTER DELETE ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, name, exif_data) VALUES('delete', old.id, old.name, old.exif_data);
END;
CREATE TRIGGER IF NOT EXISTS media_au AFTER UPDATE OF name, exif_data ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, name, exif_data) VALUES('delete', old.id, old.name, old.exif_data);
    INSERT INTO media_fts(rowid, name, exif_data) VALUES (new.id, new.name, new.exif_data);
END;
"""

_MIGRATION_V5 = """
CREATE INDEX IF NOT EXISTS idx_media_cat_type_folder_mtime_name
    ON media(category, media_type, folder, mtime DESC, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_media_cat_type_mtime_name
    ON media(category, media_type, mtime DESC, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_media_type_mtime_name
    ON media(media_type, mtime DESC, name COLLATE NOCASE);
PRAGMA user_version = 5;
"""


class Database:
    # Columns _row_to_item actually reads. Listing them explicitly keeps the
    # big `exif_data` TEXT blob out of every list/search/lookup result set —
    # it's never used to build a MediaItem and only fetched on demand via
    # get_exif_data, so `SELECT *` was hauling it across the lock for nothing.
    _ITEM_COLS = (
        "id, path, category, media_type, folder, name, mtime, size, thumb_path, taken_at"
    )

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
        # Shared by every thread — see the wlock property.
        self._write_lock = threading.RLock()
        # Initialise the *current* thread's connection so the schema/migration
        # work below runs on a fully-configured handle.
        try:
            # _open_conn already runs PRAGMAs, so an unreadable file fails here
            # rather than at the first query — keep it inside the guard.
            self._open_conn()
            with self.lock:
                self.wconn.executescript(SCHEMA_V1)
                self._migrate()
        except sqlite3.DatabaseError:
            # The file at DB_PATH is not a usable database — a truncated write,
            # a bad block on an SD card, or something else entirely landing on
            # the path. Every row in here is derived from files on disk, so the
            # cheapest correct answer is to throw it away and let the next scan
            # rebuild it. Crashing instead would leave the app unable to start
            # at all, with nothing but a traceback on a device that has no
            # terminal.
            LOGGER.warning("Media index at %s is unusable — rebuilding", path, exc_info=True)
            self._discard_and_reopen()
            with self.lock:
                self.wconn.executescript(SCHEMA_V1)
                self._migrate()

    def _discard_and_reopen(self) -> None:
        """Move the unusable database file aside and open a fresh one."""
        for existing in (getattr(self._tls, "conn", None), getattr(self, "_wconn", None)):
            if existing is not None:
                try:
                    existing.close()
                except Exception:
                    LOGGER.debug("existing.close failed", exc_info=True)
        self._tls.conn = None
        self._wconn: "sqlite3.Connection | None" = None
        for suffix in ("", "-wal", "-shm"):
            broken = Path(str(self._db_path) + suffix)
            try:
                if broken.exists():
                    broken.replace(Path(str(broken) + ".corrupt"))
            except OSError:
                try:
                    broken.unlink(missing_ok=True)
                except OSError:
                    LOGGER.debug("Could not clear %s", broken, exc_info=True)
        self._open_conn()

    def _new_conn(self) -> "sqlite3.Connection":
        """Open a connection with the app's standard PRAGMAs applied."""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL: readers + one writer in parallel, no SQLite-level blocking.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        # ~16 MB page cache (negative = KiB). The default ~2 MB forces the big
        # ORDER BY mtime sorts (folder navigation, library-wide listings) to
        # spill to disk; 16 MB keeps the working set resident on phone-sized
        # libraries for a noticeable win on those queries.
        conn.execute("PRAGMA cache_size=-16000")
        # 5 s busy timeout so a writer waiting on *another process* waits at
        # the SQLite layer instead of raising straight away.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _open_conn(self) -> "sqlite3.Connection":
        """Open this thread's read connection. Called lazily from the
        :pyattr:`conn` property the first time each thread touches the DB."""
        conn = self._new_conn()
        self._tls.conn = conn
        return conn

    def close_thread_connection(self) -> None:
        """Close and forget the calling thread's read connection.

        Long-lived threads never need this — they keep their handle for the
        life of the app. Short-lived ones do: a pool worker that reads once
        and exits leaves behind an open file descriptor and the 16 MB page
        cache its PRAGMA asked for, reclaimed only whenever the garbage
        collector next gets to the object. The MCP server serves each request
        on a fresh thread, so it calls this when the request is done.
        """
        conn = getattr(self._tls, "conn", None)
        self._tls.conn = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            LOGGER.debug("close_thread_connection failed", exc_info=True)

    @property
    def wconn(self) -> "sqlite3.Connection":
        """The single connection every write goes through.

        Writes used to run on the calling thread's own connection, and most
        write methods deliberately leave the transaction open so the scanner
        can batch a whole sweep into one commit. Those two facts together mean
        a scan thread holds SQLite's write lock for as long as its batch
        lasts, and every other thread's write hits SQLITE_BUSY and is lost —
        busy_timeout cannot help, because the holder is not going to finish
        within any timeout. Funnelling writes through one connection, guarded
        by :pyattr:`wlock`, means there is only ever one write transaction in
        this process. Readers keep their own connections and still run
        concurrently, which is what WAL is for.
        """
        conn: sqlite3.Connection | None = getattr(self, "_wconn", None)
        if conn is None:
            conn = self._new_conn()
            self._wconn = conn
        return conn

    @property
    def conn(self) -> "sqlite3.Connection":
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = self._open_conn()
        return c

    @property
    def wlock(self) -> threading.RLock:
        """Process-wide lock held across every write.

        ``lock`` below is deliberately *per thread* and therefore serialises
        nothing between threads — WAL plus busy_timeout were meant to absorb
        write-vs-write contention at the SQLite layer. Under load they don't
        quite: a scan thread, the NC sync and a delete running together still
        produced ``OperationalError: database is locked`` a handful of times
        per 70k writes, and nothing retried them, so the write was simply lost.

        Serialising *writers* fixes that at the source without bringing back
        the UI stalls that killed the old global lock: readers never take this,
        so gallery rendering still runs concurrently with a long scan.
        """
        return self._write_lock

    def _run_write(self, fn):
        """Execute *fn* under the writer lock, retrying a busy database.

        The lock removes contention between Muga's own threads; the retry
        covers the remaining case of a second process (a second window, or a
        stray instance) holding the write lock when we arrive.
        """
        with self._write_lock:
            delay = 0.05
            for attempt in range(_WRITE_RETRIES):
                try:
                    return fn()
                except sqlite3.OperationalError as exc:
                    message = str(exc).lower()
                    if "locked" not in message and "busy" not in message:
                        raise
                    if attempt == _WRITE_RETRIES - 1:
                        LOGGER.warning("Write gave up after %d attempts: %s", _WRITE_RETRIES, exc)
                        raise
                    time.sleep(delay)
                    delay *= 2

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
        version = self.wconn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            # Check if old schema (UNIQUE on path alone) is in use
            info = self.wconn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='media'"
            ).fetchone()
            if info and "UNIQUE(path, category)" not in info["sql"]:
                self.wconn.executescript(_MIGRATION_V1)
        if version < 2:
            # Add EXIF cache column
            try:
                self.wconn.execute("ALTER TABLE media ADD COLUMN exif_data TEXT DEFAULT NULL")
                self.wconn.execute("PRAGMA user_version = 2")
                self.wconn.commit()
            except sqlite3.OperationalError:
                # Column already exists
                pass
        # v6 widens the FTS index from (name) to (name, exif_data). A DB built
        # before v6 has the old single-column shadow table + triggers; drop
        # them so the setup block below rebuilds the two-column index from
        # scratch. (Fresh DBs never enter here — they build the new schema
        # directly.) Idempotent: gated on user_version, runs once.
        if version < 6:
            try:
                self.wconn.executescript(
                    "DROP TRIGGER IF EXISTS media_ai;"
                    "DROP TRIGGER IF EXISTS media_ad;"
                    "DROP TRIGGER IF EXISTS media_au;"
                    "DROP TABLE IF EXISTS media_fts;"
                )
                self.wconn.commit()
            except sqlite3.OperationalError:
                LOGGER.debug("wconn.commit failed", exc_info=True)
        # FTS5 trigram index for substring search on `name` + `exif_data`.
        # Behind a try/except because older SQLite builds (or builds compiled
        # without FTS5/trigram) would otherwise refuse to open the DB.
        # Search falls back to LIKE when the table isn't there.
        self._has_fts = False
        try:
            self.wconn.execute(_FTS_CREATE_SQL)
            # Whether the index is populated and its sync triggers are
            # installed cannot be inferred from user_version alone: if FTS5 was
            # unavailable on an earlier open, user_version advanced past 3 via
            # later migrations while the index was never built. Probe the
            # triggers directly so a later FTS-capable open self-heals instead
            # of silently returning zero matches forever.
            have_triggers = self.wconn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name IN ('media_ai', 'media_ad', 'media_au')"
            ).fetchone()[0] == 3
            if not have_triggers:
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
                self.wconn.execute("INSERT INTO media_fts(media_fts) VALUES('rebuild')")
                self.wconn.executescript(_FTS_TRIGGERS_SQL)
                if version < 3:
                    self.wconn.execute("PRAGMA user_version = 3")
                self.wconn.commit()
            self._has_fts = True
        except sqlite3.OperationalError as exc:
            LOGGER.warning(
                "FTS5 trigram index unavailable; search uses LIKE fallback: %s", exc,
            )
        if version < 4:
            # Overview became a virtual aggregator: any rows previously
            # indexed under category='pictures' are now duplicates of what
            # other categories (photos/videos/screenshots/extras) provide.
            # Drop them once so the aggregator query doesn't double-list
            # files that happened to live under both ~/Pictures and
            # another scanned root.
            try:
                self.wconn.execute("DELETE FROM media WHERE category = 'pictures'")
                self.wconn.execute("PRAGMA user_version = 4")
                self.wconn.commit()
            except sqlite3.OperationalError:
                LOGGER.debug("wconn.commit failed", exc_info=True)
        if version < 5:
            try:
                self.wconn.executescript(_MIGRATION_V5)
                self.wconn.commit()
            except sqlite3.OperationalError as exc:
                LOGGER.warning("Could not create media performance indexes: %s", exc)
        if version < 6:
            # The two-column FTS index (built above, if available) is now in
            # place; pin the schema version so the drop/rebuild above runs at
            # most once. Done even when FTS is unavailable — the version only
            # records that the migration step was reached.
            self.wconn.execute("PRAGMA user_version = 6")
            self.wconn.commit()
        if version < 7:
            self._repoint_legacy_thumb_paths()
            self.wconn.execute("PRAGMA user_version = 7")
            self.wconn.commit()
        if version < 8:
            # Index for the one-row-per-file subquery in _one_row_per_file.
            try:
                self.wconn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_type_path ON media(media_type, path)"
                )
                self.wconn.execute("PRAGMA user_version = 8")
                self.wconn.commit()
            except sqlite3.OperationalError as exc:
                LOGGER.warning("Could not create the media_type/path index: %s", exc)
        if version < 9:
            # EXIF capture time. Existing rows get NULL and fall back to mtime
            # in every query, so the column is useful from the moment it exists
            # and fills in as the scanner re-reads files.
            try:
                self.wconn.execute("ALTER TABLE media ADD COLUMN taken_at REAL DEFAULT NULL")
            except sqlite3.OperationalError as exc:
                # Already there when the table was created from SCHEMA_V1 in
                # this same run — a fresh database goes through _migrate too.
                if "duplicate column" not in str(exc).lower():
                    LOGGER.warning("Could not add the taken_at column: %s", exc)
            try:
                self.wconn.execute("PRAGMA user_version = 9")
                self.wconn.commit()
            except sqlite3.OperationalError as exc:
                LOGGER.warning("Could not pin schema version 9: %s", exc)
        if version < 10:
            # Indexes for the capture-date sort. Expression indexes need
            # SQLite 3.9; on anything older this fails and the sort still
            # works, just by scanning — correctness never depends on them.
            try:
                self.wconn.executescript(_MIGRATION_V10)
                self.wconn.execute("PRAGMA user_version = 10")
                self.wconn.commit()
            except sqlite3.OperationalError as exc:
                LOGGER.warning("Could not create the capture-date indexes: %s", exc)

    def _repoint_legacy_thumb_paths(self) -> None:
        """Point thumb_path at the renamed cache directory.

        migrate_legacy_dirs() renames ~/.cache/yaga to ~/.cache/muga, and every
        cached thumbnail travels with it. thumb_path, however, stores an
        *absolute* path, and those rows kept naming a directory that no longer
        exists — so the grid asks for a file that is not there and draws the
        placeholder instead, while the real thumbnail sits next door under the
        new name.

        Local items recover on their own: the scanner recomputes the path, finds
        no file, re-decodes and writes the row back. Nextcloud items never do —
        the folder sync skips anything that already carries a thumb_path, so a
        whole remote library stays on placeholder tiles for good. Rewriting the
        prefix restores them at once, instead of re-fetching thousands of
        previews over the network.

        Prefix-matched with substr rather than LIKE on purpose: a home directory
        containing "_" would make LIKE match paths of the same length that are
        not under the old directory at all, and the rewrite would then corrupt
        them.
        """
        old_prefix = f"{LEGACY_CACHE_DIR}/"
        new_prefix = f"{CACHE_DIR}/"
        try:
            cur = self.wconn.execute(
                "UPDATE media SET thumb_path = ? || substr(thumb_path, ?) "
                "WHERE substr(thumb_path, 1, ?) = ?",
                (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
            )
        except sqlite3.OperationalError:
            LOGGER.debug("legacy thumb_path migration failed", exc_info=True)
            return
        if cur.rowcount:
            LOGGER.info(
                "Repointed %s thumbnail path(s) from %s to %s",
                cur.rowcount, LEGACY_CACHE_DIR, CACHE_DIR,
            )

    def load_scan_index(self, category: str) -> dict[str, tuple[float, int, bool]]:
        """Return ``{path: (mtime, size, exif_read)}`` for every row in *category*.

        The scanner uses this to skip files whose mtime+size are unchanged
        since the last sweep: an unchanged file needs neither a re-decode nor
        a row rewrite (the latter would also pointlessly fire the FTS update
        trigger). Keyed by path because that's the scanner's per-file lookup.

        ``exif_read`` is what keeps an existing library from staying without
        capture dates forever. Every file in it is "unchanged", so the skip
        would never let the scanner near it and taken_at would only ever fill
        in for photos added after the upgrade. A non-NULL ``exif_data`` means
        the parse has already happened — including the ``{}`` written for a
        file that turned out to have no EXIF, so a screenshot is not re-parsed
        on every single scan.
        """
        with self.lock:
            rows = self.conn.execute(
                "SELECT path, mtime, size, exif_data FROM media WHERE category = ?",
                (category,),
            ).fetchall()
        return {
            row["path"]: (row["mtime"], row["size"], row["exif_data"] is not None)
            for row in rows
        }

    def touch_seen(self, paths: list[str], category: str) -> None:
        """Bump ``seen_at`` for *paths* without rewriting any indexed column.

        Unchanged files still have to survive ``prune_missing`` (which deletes
        rows with ``seen_at < scan_start``), but they don't need a full upsert.
        This batched UPDATE touches only ``seen_at`` so the ``AFTER UPDATE OF
        name, exif_data`` FTS trigger stays dormant — the whole point of the
        scanner's skip path is to avoid that churn."""
        if not paths:
            return
        now = time.time()
        with self.wlock:
            self.wconn.executemany(
                "UPDATE media SET seen_at = ? WHERE path = ? AND category = ?",
                [(now, p, category) for p in paths],
            )

    def upsert_media(self, *, path: Path, category: str, media_type: str, folder: str,
                     thumb_path: str | None, stat: os.stat_result | None = None,
                     taken_at: float | None = None,
                     exif_json: str | None = None) -> None:
        if stat is None:
            stat = path.stat()
        with self.wlock:
            self.wconn.execute(
                """
                INSERT INTO media(path, category, media_type, folder, name, mtime, size,
                                  thumb_path, seen_at, taken_at, exif_data)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path, category) DO UPDATE SET
                    media_type=excluded.media_type,
                    folder=excluded.folder,
                    name=excluded.name,
                    mtime=excluded.mtime,
                    size=excluded.size,
                    thumb_path=COALESCE(excluded.thumb_path, media.thumb_path),
                    seen_at=excluded.seen_at,
                    -- COALESCE both ways round: a caller that did not parse
                    -- EXIF (the Nextcloud path, a re-index that skipped it)
                    -- must not wipe what an earlier pass already found.
                    taken_at=COALESCE(excluded.taken_at, media.taken_at),
                    exif_data=COALESCE(excluded.exif_data, media.exif_data)
                """,
                (str(path), category, media_type, folder, path.name, stat.st_mtime,
                 stat.st_size, thumb_path, time.time(), taken_at, exif_json),
            )

    def upsert_remote_media(self, *, path: str, category: str, media_type: str, folder: str,
                             name: str, mtime: float, size: int, thumb_path: str | None) -> None:
        with self.wlock:
            self.wconn.execute(
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
        with self.wlock:
            self.wconn.executemany(
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
        with self.wlock:
            # DELETE … RETURNING evaluates the (un-indexed) seen_at predicate
            # once instead of twice — the old SELECT-then-DELETE ran it back to
            # back. SQLite ≥ 3.35 (we require far newer) supports RETURNING.
            stale = self.wconn.execute(
                f"DELETE FROM media WHERE seen_at < ? AND category IN ({placeholders}) "
                "RETURNING thumb_path",
                [seen_since, *categories],
            ).fetchall()
        for row in stale:
            thumb = row["thumb_path"]
            if thumb:
                try:
                    Path(thumb).unlink(missing_ok=True)
                except OSError:
                    LOGGER.debug("Path failed", exc_info=True)
        return len(stale)

    def set_thumb(self, path: str, thumb_path: str, category: str | None = None) -> None:
        with self.wlock:
            if category is not None:
                self.wconn.execute(
                    "UPDATE media SET thumb_path = ? WHERE path = ? AND category = ?",
                    (thumb_path, path, category),
                )
            else:
                self.wconn.execute("UPDATE media SET thumb_path = ? WHERE path = ?", (thumb_path, path))

    def set_exif_data(self, path: str, exif_json: str, category: str | None = None) -> None:
        """Store cached EXIF data (JSON) for a media item."""
        with self.wlock:
            if category is not None:
                self.wconn.execute(
                    "UPDATE media SET exif_data = ? WHERE path = ? AND category = ?",
                    (exif_json, path, category),
                )
            else:
                self.wconn.execute("UPDATE media SET exif_data = ? WHERE path = ?", (exif_json, path))

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
        # _run_write, not a bare wlock: this is where the batched transaction
        # actually reaches disk, so it is the moment another *process* (a
        # second window, a stray instance) can be holding the file's write
        # lock. Losing here would discard a whole scan batch.
        self._run_write(self.wconn.commit)

    @staticmethod
    def _one_row_per_file(where: str, args: list) -> tuple[str, list]:
        """Narrow *where* to a single row per file.

        The same photo legitimately sits in the index more than once: `media` is
        unique on (path, category), so a file inside two overlapping media
        folders gets one row per category — which is what makes each folder's
        own tab complete on its own.

        The aggregate views select *across* categories, and there the second row
        is the same picture again: a duplicate tile in the grid, and a folder
        whose count is inflated by the copies. Restricting to the lowest id per
        path shows each file once. Lowest id is arbitrary but stable, so a photo
        does not drift between the folders it is attributed to from one render
        to the next — and because grid, count and folder listing all apply this
        same predicate, the number on a folder tile matches what opening it
        shows.

        The where clause is repeated inside the subquery rather than deduping
        over the whole table: a file that only collides *outside* the current
        filter must not lose its row here.
        """
        return (
            f"({where}) AND id IN (SELECT MIN(id) FROM media WHERE ({where}) GROUP BY path)",
            args + args,
        )

    @staticmethod
    def _build_list_where(category: str, folder: str | None, include_nc: bool,
                          media_filter: str | None = None) -> tuple[str, list]:
        """Return (where_sql, args) for filtering by category (+ optional folder).
        Built-in image categories restrict to media_type='image'; the videos
        and pictures (Overview) categories aggregate across every source.
        *media_filter* overrides the per-category default for extras:
        "both" drops the type constraint, "videos" flips to videos-only,
        "images" keeps the image-only default."""
        if category == "videos":
            # Aggregate: every video on disk or NC, regardless of which root
            # holds it — so the same clip under two overlapping folders needs
            # the same one-row-per-file treatment as Overview below.
            return Database._one_row_per_file("media_type = 'video'", [])
        if category == "pictures":
            # Overview is a virtual aggregator across every local category.
            # `category != 'pictures'` excludes any stale rows the migration
            # missed; NC is folded in only when the caller explicitly opted
            # into it via include_nc, matching the historic pictures view.
            args_pic: list = []
            base_pic = "category NOT IN ('pictures', 'nextcloud')"
            if media_filter == "videos":
                base_pic += " AND media_type = 'video'"
            elif media_filter != "both":
                base_pic += " AND media_type = 'image'"
            if folder:
                base_pic += " AND folder = ?"
                args_pic.append(folder)
            if include_nc:
                base_pic = (
                    f"({base_pic}) OR (category = 'nextcloud' AND media_type = 'image')"
                )
            return Database._one_row_per_file(base_pic, args_pic)
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
            "newest":      f"{Database._SORT_DATE} DESC, name COLLATE NOCASE ASC",
            "oldest":      f"{Database._SORT_DATE} ASC, name COLLATE NOCASE ASC",
            # The file's own timestamp, for the "Date (file)" sort. Kept apart
            # from newest/oldest rather than folded in: those answer "when is
            # this photo from", this one answers "when did this file change",
            # and for anything copied off a camera the two differ by years.
            "file_newest": "mtime DESC, name COLLATE NOCASE ASC",
            "file_oldest": "mtime ASC, name COLLATE NOCASE ASC",
            "name":        "name COLLATE NOCASE ASC",
            "name_desc":   "name COLLATE NOCASE DESC",
            "folder":      f"folder COLLATE NOCASE ASC, {Database._SORT_DATE} DESC",
            "folder_desc": f"folder COLLATE NOCASE DESC, {Database._SORT_DATE} DESC",
        }.get(sort_mode, "mtime DESC")
        where, args = self._build_list_where(category, folder, include_nc, media_filter)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {self._ITEM_COLS} FROM media WHERE {where} ORDER BY {order}", args
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def count_media(self, category: str, folder: str | None = None, include_nc: bool = False,
                    media_filter: str | None = None) -> int:
        """Return total count of media items (for pagination)."""
        where, args = self._build_list_where(category, folder, include_nc, media_filter)
        with self.lock:
            result = self.conn.execute(f"SELECT COUNT(*) FROM media WHERE {where}", args).fetchone()
        return result[0] if result else 0

    # What a date search matches against.
    #
    # taken_at first: "photos from 2019" means the year they were shot, not the
    # year the file happened to be written. A photo pulled off a camera today
    # has today's mtime, so before this the search answered with whatever the
    # copy date was. mtime remains the fallback for everything with no EXIF
    # date — videos, screenshots, downloads.
    #
    # 'localtime' because both are stored as real epoch seconds while the user
    # thinks in wall-clock dates. Without it SQLite formats in UTC, and a photo
    # taken at 00:30 on New Year's Day was found under the previous year.
    # The date a row is filed under, mirroring MediaItem.display_time: the
    # capture date when there is one, the file's mtime otherwise. Used for
    # ordering as well as for the year/month extraction below, so the grid's
    # sort and its month headers can never disagree.
    _SORT_DATE = "COALESCE(taken_at, mtime)"
    _DATE_EXPR = f"{_SORT_DATE}, 'unixepoch', 'localtime'"

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
        "september": 9, "sep": 9, "sept": 9,
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
        # Filename + EXIF. With the FTS5 trigram index a single MATCH probes
        # both the `name` and `exif_data` columns, so it replaces *both* the
        # old full-table `name LIKE` scan and the even more expensive
        # `exif_data LIKE '%q%'` blob scan. Fall back to LIKE when the index
        # isn't there or the query is too short for trigram (it tokenises as
        # 3-grams and silently returns no rows for 1-2 char queries —
        # substring LIKE preserves the user's mental model in those edges).
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
            # No FTS index to lean on: keep EXIF searchable via a (full-scan)
            # LIKE, but only once the query is long enough for a hit to be
            # realistic. With FTS present this is unnecessary — the MATCH
            # above already covers exif_data.
            if len(q) >= 3:
                clauses.append("exif_data LIKE ?")
                args.append(like)
        # Year (4-digit number anywhere in the query).
        ym = re.search(r"(\d{4})[-/.](\d{1,2})", q)
        if ym:
            year, month = ym.group(1), int(ym.group(2))
            clauses.append(
                f"(strftime('%Y', {self._DATE_EXPR}) = ? "
                f"AND CAST(strftime('%m', {self._DATE_EXPR}) AS INTEGER) = ?)"
            )
            args.extend([year, month])
        else:
            year = re.search(r"\b(\d{4})\b", q)
            if year:
                clauses.append(f"strftime('%Y', {self._DATE_EXPR}) = ?")
                args.append(year.group(1))
        # Month name
        q_low = q.lower()
        for name, num in Database._MONTH_LOOKUP.items():
            if name in q_low:
                clauses.append(
                    f"CAST(strftime('%m', {self._DATE_EXPR}) AS INTEGER) = ?"
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
            "newest":      f"{Database._SORT_DATE} DESC, name COLLATE NOCASE ASC",
            "oldest":      f"{Database._SORT_DATE} ASC, name COLLATE NOCASE ASC",
            # The file's own timestamp, for the "Date (file)" sort. Kept apart
            # from newest/oldest rather than folded in: those answer "when is
            # this photo from", this one answers "when did this file change",
            # and for anything copied off a camera the two differ by years.
            "file_newest": "mtime DESC, name COLLATE NOCASE ASC",
            "file_oldest": "mtime ASC, name COLLATE NOCASE ASC",
            "name":        "name COLLATE NOCASE ASC",
            "name_desc":   "name COLLATE NOCASE DESC",
            "folder":      f"folder COLLATE NOCASE ASC, {Database._SORT_DATE} DESC",
            "folder_desc": f"folder COLLATE NOCASE DESC, {Database._SORT_DATE} DESC",
        }.get(sort_mode, "mtime DESC")
        base_where, args = self._build_list_where(category, folder, include_nc, media_filter)
        search_where, search_args = self._build_search_clause(query)
        full_where = f"({base_where}) AND {search_where}"
        args.extend(search_args)
        sql = f"SELECT {self._ITEM_COLS} FROM media WHERE {full_where} ORDER BY {order}"
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
            "newest":      f"{Database._SORT_DATE} DESC, name COLLATE NOCASE ASC",
            "oldest":      f"{Database._SORT_DATE} ASC, name COLLATE NOCASE ASC",
            # The file's own timestamp, for the "Date (file)" sort. Kept apart
            # from newest/oldest rather than folded in: those answer "when is
            # this photo from", this one answers "when did this file change",
            # and for anything copied off a camera the two differ by years.
            "file_newest": "mtime DESC, name COLLATE NOCASE ASC",
            "file_oldest": "mtime ASC, name COLLATE NOCASE ASC",
            "name":        "name COLLATE NOCASE ASC",
            "name_desc":   "name COLLATE NOCASE DESC",
            "folder":      f"folder COLLATE NOCASE ASC, {Database._SORT_DATE} DESC",
            "folder_desc": f"folder COLLATE NOCASE DESC, {Database._SORT_DATE} DESC",
        }.get(sort_mode, "mtime DESC")
        where, args = self._build_list_where(category, folder, include_nc, media_filter)
        args.extend([limit, offset])
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {self._ITEM_COLS} FROM media WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", args
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get_media_by_path(self, path: str, category: str | None = None) -> MediaItem | None:
        with self.lock:
            if category is not None:
                row = self.conn.execute(
                    f"SELECT {self._ITEM_COLS} FROM media WHERE path = ? AND category = ?", (path, category)
                ).fetchone()
            else:
                row = self.conn.execute(
                    f"SELECT {self._ITEM_COLS} FROM media WHERE path = ?", (path,)
                ).fetchone()
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

    @staticmethod
    def _child_segment(folder: str, parent: str | None, parent_prefix: str) -> str | None:
        """Map a stored *folder* to the direct child of *parent* it falls under
        (e.g. parent ``Trips`` + folder ``Trips/Berlin/Mitte`` → ``Trips/Berlin``),
        or ``None`` when *folder* isn't a descendant of *parent*."""
        if folder == "/":
            return None
        if parent in (None, "/"):
            remainder = folder
        elif folder.startswith(parent_prefix):
            remainder = folder[len(parent_prefix):]
        else:
            return None
        # Empty remainders (stray trailing slashes) have no child segment.
        if not remainder:
            return None
        child_name = remainder.split("/", 1)[0]
        return child_name if parent in (None, "/") else f"{parent}/{child_name}"

    def child_folders(self, category: str, parent: str | None,
                      media_filter: str | None = None) -> list[tuple[str, int, list]]:
        params: tuple[str, ...]
        if category == "videos":
            # Aggregates get the same one-row-per-file treatment the grid uses,
            # so a folder's count matches what opening it actually shows.
            where, args = self._one_row_per_file("media_type = 'video'", [])
            params = tuple(args)
        elif category == "pictures":
            # Overview aggregator — union across every local category.
            base = "category NOT IN ('pictures', 'nextcloud')"
            if media_filter == "videos":
                where = f"{base} AND media_type = 'video'"
            elif media_filter == "both":
                where = base
            else:
                where = f"{base} AND media_type = 'image'"
            where, args = self._one_row_per_file(where, [])
            params = tuple(args)
        elif media_filter == "videos":
            where, params = "category = ? AND media_type = 'video'", (category,)
        elif media_filter == "both":
            where, params = "category = ?", (category,)
        else:
            where, params = "category = ? AND media_type = 'image'", (category,)

        parent_prefix = "" if parent in (None, "/") else f"{parent}/"
        # Counts come from an aggregate GROUP BY — one row per folder instead
        # of one Python object per *file*, which is what the old single-query
        # form materialised across the whole category.
        with self.lock:
            count_rows = self.conn.execute(
                f"SELECT folder, COUNT(*) AS n FROM media WHERE {where} GROUP BY folder", params
            ).fetchall()
        counts: dict[str, int] = {}
        for row in count_rows:
            child = self._child_segment(row["folder"], parent, parent_prefix)
            if child is not None:
                counts[child] = counts.get(child, 0) + row["n"]
        if not counts:
            return []

        # Up to 4 preview thumbs per child, newest first. Only non-null thumbs
        # matter, and we can stop the moment every child has its four — on a
        # library where folders are well-populated that bails out long before
        # scanning the whole category.
        thumbs: dict[str, list] = {child: [] for child in counts}
        satisfied = 0
        with self.lock:
            cur = self.conn.execute(
                f"SELECT folder, thumb_path FROM media "
                f"WHERE ({where}) AND thumb_path IS NOT NULL ORDER BY mtime DESC", params
            )
            for row in cur:
                child = self._child_segment(row["folder"], parent, parent_prefix)
                if child is None:
                    continue
                bucket = thumbs.get(child)
                if bucket is None or len(bucket) >= 4:
                    continue
                t = row["thumb_path"]
                if t not in bucket:
                    bucket.append(t)
                    if len(bucket) == 4:
                        satisfied += 1
                        if satisfied == len(counts):
                            break
        return [
            (child, counts[child], thumbs[child])
            for child in sorted(counts, key=str.lower)
        ]

    def delete_path(self, path: str, category: str | None = None) -> None:
        def _delete() -> None:
            if category is not None:
                self.wconn.execute(
                    "DELETE FROM media WHERE path = ? AND category = ?", (path, category)
                )
            else:
                self.wconn.execute("DELETE FROM media WHERE path = ?", (path,))
            self.wconn.commit()

        # Deletes commit immediately and follow a file that is already gone
        # from disk; a dropped one leaves a tile pointing at nothing.
        self._run_write(_delete)

    def clear_category(self, category: str) -> None:
        """Delete all DB rows for a category and remove their thumbnail files."""
        with self.wlock:
            rows = self.wconn.execute(
                "SELECT thumb_path FROM media WHERE category = ?", (category,)
            ).fetchall()
        for row in rows:
            if row["thumb_path"]:
                try:
                    Path(row["thumb_path"]).unlink(missing_ok=True)
                except OSError:
                    LOGGER.debug("Path failed", exc_info=True)
        def _clear() -> None:
            self.wconn.execute("DELETE FROM media WHERE category = ?", (category,))
            self.wconn.commit()

        self._run_write(_clear)

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
            taken_at=row["taken_at"],
        )
