from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "muga"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "muga"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "muga"
THUMB_DIR = CACHE_DIR / "thumbnails"
DB_PATH = DATA_DIR / "muga.sqlite3"
DEBUG_LOG_PATH = CACHE_DIR / "debug.log"
TRACE_LOG_PATH = CACHE_DIR / "trace.log"

# Muga used to be called Yaga, and the per-user directories carried that name.
# Everything a user owns lives in them: settings, the media index, the whole
# thumbnail cache.
_LEGACY_DIR_NAME = "yaga"
_LEGACY_DB_NAME = "yaga.sqlite3"

# Where the cache lived before the rename. Public because the database has to
# repoint the absolute thumb_path values it stored back then — moving the
# directory (below) does not rewrite what is inside the index.
LEGACY_CACHE_DIR = CACHE_DIR.parent / _LEGACY_DIR_NAME


def migrate_legacy_dirs() -> None:
    """Move the pre-rename directories into place. Runs once, at startup.

    Without this, the rename would look like data loss to every existing
    install: Muga would come up with no settings, an empty gallery and a cold
    cache, while the old data sat next door under the previous name.

    A rename rather than a copy — it is atomic and instant even for a cache of
    several GB, and it cannot half-succeed and leave two diverging copies. It
    only ever fires when the new directory does not exist yet, so a fresh
    install (and a second start) never touches anything.
    """
    for new in (CONFIG_DIR, CACHE_DIR, DATA_DIR):
        old = new.parent / _LEGACY_DIR_NAME
        if new.exists() or not old.is_dir():
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
            LOGGER.info("Migrated %s to %s after the rename", old.name, new.name)
        except OSError:
            # A cross-device layout or a stale permission is not worth failing
            # the launch over: Muga then simply starts fresh, and the old
            # directory stays where it is for the user to move by hand.
            LOGGER.warning("Could not migrate %s", old, exc_info=True)

    # The database file carries the app name as well, and DB_PATH now points at
    # the new one — moving the directory alone would leave a full media index
    # sitting there unread. The -wal/-shm siblings travel with it: leaving a
    # write-ahead log behind loses whatever it still holds.
    legacy_db = DB_PATH.parent / _LEGACY_DB_NAME
    if legacy_db.is_file() and not DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            src = legacy_db.parent / f"{_LEGACY_DB_NAME}{suffix}"
            if not src.exists():
                continue
            try:
                src.rename(DB_PATH.parent / f"{DB_PATH.name}{suffix}")
            except OSError:
                LOGGER.warning("Could not migrate %s", src, exc_info=True)


def default_path(name: str) -> str:
    candidates = {
        "photos": [Path.home() / "Photos", Path.home() / "Bilder", Path.home() / "Pictures"],
        "pictures": [Path.home() / "Pictures", Path.home() / "Bilder"],
        "videos": [Path.home() / "Videos"],
        "screenshots": [Path.home() / "Pictures" / "Screenshots", Path.home() / "Bilder" / "Bildschirmfotos"],
    }
    for path in candidates[name]:
        if path.exists():
            return str(path)
    return str(candidates[name][0])


# Thumbnail/preview disk-cache ceiling in MB. Named rather than inlined so
# the migration in load() can restore it without reaching into
# __dataclass_fields__ for the literal.
DEFAULT_CACHE_MAX_MB = 2048

# Default listening port for the built-in MCP server. Named rather than
# inlined so load()'s range clamp can fall back to it without reaching into
# the dataclass fields for the literal.
DEFAULT_MCP_PORT = 8765


@dataclass
class Settings:
    photos_dir: str = field(default_factory=lambda: default_path("photos"))
    pictures_dir: str = field(default_factory=lambda: default_path("pictures"))
    videos_dir: str = field(default_factory=lambda: default_path("videos"))
    screenshots_dir: str = field(default_factory=lambda: default_path("screenshots"))
    extra_locations: list[str] = field(default_factory=list)
    # Display names for the entries in extra_locations, index-aligned. An empty
    # string falls back to Path(path).name. Stored as a parallel list (not as
    # tuples) to keep settings.json human-editable and JSON-serialisable.
    extra_location_names: list[str] = field(default_factory=list)
    # "Do not inherit" flag per extra location, index-aligned. When True, any
    # *other* category whose root is a parent of this folder will not include
    # its content during scans — useful when a subfolder is exposed as its
    # own category and shouldn't be listed twice.
    extra_location_no_inherit: list[bool] = field(default_factory=list)
    # Media-type filter per extra location, index-aligned. Allowed values:
    # "both" (default), "images", "videos". Drives which rows show up when
    # the user opens this folder in the gallery.
    extra_location_media_filter: list[str] = field(default_factory=list)
    sort_mode: str = "newest"
    sort_modes: dict = field(default_factory=dict)
    theme: str = "system"
    # English is the source language (the msgids themselves), so it is what
    # Muga shows unless a translation is picked. "system" stays selectable in
    # Settings — it is just no longer the default, see language_default_migrated.
    language: str = "en"
    # One-shot marker for the "system" → "en" default change. Installs that
    # predate it have "system" in settings.json that was never a real choice:
    # it silently started Muga in German on a German desktop. Picking a
    # language in Settings sets this, so a deliberate "system" survives.
    language_default_migrated: bool = False
    external_video_player: str = ""
    grid_columns: int = 4
    last_category: str = ""
    # Where to place the category nav bar relative to the gallery content.
    # Valid values: "top" (default, preserves legacy layout), "bottom", "left", "right".
    nav_position: str = "top"
    # Which side the camera record button (and any other thumb-reachable
    # camera controls) should sit on. "right" or "left".
    handedness: str = "right"
    # Camera capture settings — persisted across sessions.
    # jpeg quality (0-100) used by the gst-jpegenc element when we
    # encode in-pipeline, and by Pillow when we re-encode after a
    # post-capture downscale.
    camera_jpeg_quality: int = 92
    # Photo target size (w, h). null/None = save at HAL-native resolution.
    # Stored as a list because tuples don't survive JSON round-trips.
    camera_image_resolution: list | None = None
    # Video record bitrate (kbps) — applied when the record path lands.
    camera_video_bitrate_kbps: int = 4000
    # Camera geotagging: user intent. Boolean on/off. When on, the camera
    # tries to acquire a GPS fix via GeoClue; if unavailable, silently
    # no-op (no error toast).
    camera_geo_enabled: bool = False

    # Flash / torch toggle. Single boolean covers both:
    #   - photo mode → flash-mode=ON (fires once at capture)
    #   - video mode → direct sysfs torch (continuous light while active)
    # Only meaningful on Halium / gst-droid devices; v4l2 cameras
    # silently ignore it.
    camera_flash_enabled: bool = False

    # User-defined ordering of the four built-in media folders. Items not in
    # the list (e.g. legacy upgrades that didn't write the field) fall back to
    # the natural order.
    media_folder_order: list = field(default_factory=lambda: [
        "pictures", "photos", "videos", "screenshots",
    ])

    # The "Overview" category is a virtual aggregator across every other
    # local category. It can be hidden from the gallery navigation but
    # never deleted — pictures_dir is preserved purely for legacy load()
    # compatibility and is no longer scanned.
    pictures_hidden: bool = False
    # Media-type filter for Overview. Defaults to "images" so the historic
    # Pictures view (images-only) keeps its semantics on upgrade. Allowed:
    # "both", "images", "videos" — same vocabulary as extras.
    pictures_media_filter: str = "images"

    # Disk cache budget for thumbnails + downloaded NC originals (MB).
    # 0 means "unlimited"; any positive value triggers LRU eviction.
    #
    # Defaults to 2 GB rather than unlimited: every Nextcloud photo opened is
    # cached full-size and never expires on its own, so the old default let
    # ~/.cache grow until the partition filled — on a phone that takes the
    # camera down with it. The eviction machinery was already there; nothing
    # ever switched it on.
    cache_max_mb: int = DEFAULT_CACHE_MAX_MB
    # One-shot marker for the 0 → 2048 default change. Installs that predate
    # it have an explicit 0 in settings.json that was never a real choice;
    # this lifts them to the new default exactly once, while still honouring
    # a 0 the user picks deliberately afterwards.
    cache_budget_migrated: bool = False

    # ISO timestamp of the last in-app update check (shown in Settings).
    last_update_check: str = ""

    # MCP server — the access tokens live in mcp_tokens.json (0600), never
    # here: settings.json is documented as hand-editable and written on almost
    # every UI interaction. Only the on/off state and the port belong in it.
    mcp_enabled: bool = False
    mcp_port: int = DEFAULT_MCP_PORT
    # How far the MCP server listens: "local" (loopback only), "lan", "public"
    # or "all". Defaults to the narrowest — reaching the index from another
    # machine has to be a deliberate choice, not the consequence of turning
    # the server on. Validated in mcp_server.resolve_bind, which falls back to
    # loopback for an unknown or currently unavailable value.
    mcp_bind: str = "local"
    # Whether the MCP server also offers the tools that add, move and delete
    # files. Off by default and independent of mcp_bind: turning the server on,
    # or widening how far it listens, must never be what grants write access.
    # The tools are hidden from tools/list entirely while this is False, so a
    # client cannot even see that they exist.
    mcp_write_enabled: bool = False

    # Nextcloud — stored in keyring; only URL/user saved to settings.json
    nextcloud_url: str = ""
    nextcloud_user: str = ""
    nextcloud_photos_path: str = "Photos"
    nextcloud_enabled: bool = False  # set to True after successful connect
    nextcloud_thumbnail_only: bool = True  # skip full-file download during scan
    nextcloud_show_in_pictures: bool = False  # merge NC items into the Pictures view
    # Persistent counterpart of the runtime "session active" flag. Defaults to
    # True so a fresh nextcloud_enabled → True actually activates the connection;
    # a manual Disconnect saves False here so the next app launch comes up
    # disconnected (cached items still visible, no network until user reconnects).
    nextcloud_session_active: bool = True

    @staticmethod
    def _quarantine(path: Path, reason: str) -> None:
        """Move an unusable settings file aside instead of silently dropping the
        user's configuration. They keep a copy they can fix by hand, and the
        next save() starts from a clean file rather than failing forever."""
        try:
            backup = path.with_suffix(".json.corrupt")
            os.replace(path, backup)
            LOGGER.warning("settings.json unusable (%s) — moved to %s", reason, backup)
        except OSError:
            LOGGER.warning("settings.json unusable (%s) and could not be moved aside", reason)

    @classmethod
    def _accepted_fields(cls, data: dict) -> dict:
        """Keep only known keys whose value type matches the field's default.

        settings.json is explicitly documented as hand-editable, so a typo has
        to degrade to "that one setting reverts to its default", never to a
        crash on startup. A bare ``cls(**data)`` accepted anything JSON could
        express: ``"grid_columns": "vier"`` blew up in the int() clamp below,
        and ``"extra_locations": "text"`` sailed through as a str that later
        code iterated character by character.
        """
        known = {f.name for f in cls.__dataclass_fields__.values()}
        reference = cls()
        accepted: dict = {}
        for key, value in data.items():
            if key not in known:
                continue
            default = getattr(reference, key, None)
            # Fields whose default is None carry their own validation further
            # down (camera_image_resolution); accept None or a list here.
            if default is None:
                if value is None or isinstance(value, list):
                    accepted[key] = value
                else:
                    LOGGER.warning("settings.json: ignoring %r (unexpected type)", key)
                continue
            # bool before int — bool IS an int in Python, and letting True
            # through as grid_columns=1 would be a silent surprise.
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif isinstance(default, int):
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
                if ok:
                    value = int(value)
            else:
                ok = isinstance(value, type(default))
            if ok:
                accepted[key] = value
            else:
                LOGGER.warning(
                    "settings.json: ignoring %r (expected %s, got %s)",
                    key, type(default).__name__, type(value).__name__,
                )
        return accepted

    @classmethod
    def load(cls) -> "Settings":
        path = CONFIG_DIR / "settings.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            # Valid JSON, but not an object (null, a list, a bare string …).
            cls._quarantine(path, f"top level is {type(data).__name__}, not an object")
            return cls()
        try:
            settings = cls(**cls._accepted_fields(data))
        except Exception:
            LOGGER.warning("settings.json could not be applied", exc_info=True)
            cls._quarantine(path, "could not be applied")
            return cls()
        if not settings.cache_budget_migrated and settings.cache_max_mb <= 0:
            # Only rewrite an unlimited budget that was never a real choice.
            # Deliberately choosing 0 in Settings sets the flag there, so this
            # never second-guesses the user twice.
            settings.cache_max_mb = DEFAULT_CACHE_MAX_MB
            settings.cache_budget_migrated = True
            LOGGER.info(
                "Cache budget was unlimited — defaulting to %d MB", settings.cache_max_mb,
            )
            settings.save()
        if not settings.language_default_migrated and settings.language == "system":
            from .i18n import SOURCE_LANGUAGE

            # Same reasoning as the cache budget above: "system" was the old
            # default rather than a choice. Selecting it in Settings sets the
            # flag there, so this never overrides the user twice.
            settings.language = SOURCE_LANGUAGE
            settings.language_default_migrated = True
            LOGGER.info(
                "Language was following the system by default — defaulting to %r",
                settings.language,
            )
            settings.save()
        settings.grid_columns = min(max(int(settings.grid_columns), 2), 10)
        # Ports below 1024 need root on Linux and would fail to bind; above
        # 65535 there is nothing to bind to at all. A hand-edited value out of
        # range falls back to the default rather than leaving the MCP page
        # showing a port that can never come up.
        if not 1024 <= int(settings.mcp_port) <= 65535:
            settings.mcp_port = DEFAULT_MCP_PORT
        # Clamp legacy / hand-edited values to the four supported positions so a
        # typo in settings.json doesn't crash the layout logic in _build_ui.
        if settings.nav_position not in ("top", "bottom", "left", "right"):
            settings.nav_position = "top"
        if settings.handedness not in ("left", "right", "neutral"):
            settings.handedness = "right"
        # Clamp / sanitise camera fields against hand-edited values.
        settings.camera_jpeg_quality = min(
            max(int(settings.camera_jpeg_quality), 1), 100
        )
        settings.camera_video_bitrate_kbps = max(
            int(settings.camera_video_bitrate_kbps), 200
        )
        if settings.camera_image_resolution is not None:
            try:
                w, h = (
                    int(settings.camera_image_resolution[0]),
                    int(settings.camera_image_resolution[1]),
                )
                if w <= 0 or h <= 0:
                    raise ValueError
                settings.camera_image_resolution = [w, h]
            except Exception:
                settings.camera_image_resolution = None
        return settings

    def save(self) -> bool:
        """Persist the settings. Returns False if they could not be written.

        Atomic write: serialise to a sibling tmp file, fsync, rename into
        place. Without this, a crash mid-write produces a truncated JSON file
        that load() can't parse — and load() silently falls back to defaults,
        losing every user setting.

        Failures are reported, not raised: this is called from ~24 UI
        callbacks (every toggle, every folder edit), and a full disk or a
        read-only config dir would otherwise propagate out of a signal handler
        and abort whatever the user was doing mid-way.
        """
        path = CONFIG_DIR / "settings.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = json.dumps(self.__dict__, indent=2, ensure_ascii=False)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    LOGGER.debug("os.fsync failed", exc_info=True)
            os.replace(tmp, path)
            return True
        except (OSError, TypeError, ValueError):
            LOGGER.exception("Could not write %s", path)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                LOGGER.debug("tmp.unlink failed", exc_info=True)
            return False

    def get_sort_mode(self, category: str, folder: str | None = None) -> str:
        default = "folder" if category == "nextcloud" else self.sort_mode
        if folder is not None:
            folder_key = f"{category}\x00{folder}"
            if folder_key in self.sort_modes:
                return self.sort_modes[folder_key]
        return self.sort_modes.get(category, default)

    def categories(self) -> list[tuple[str, str, str]]:
        cat_map: dict[str, tuple[str, str]] = {}
        # Overview is a virtual aggregator. Its path slot carries the legacy
        # pictures_dir value so existing 3-tuple consumers stay happy, but the
        # DB query for category="pictures" unions the other categories — the
        # path itself is never scanned. The user can hide Overview but not
        # remove it; clearing pictures_dir does not delete it anymore.
        if not self.pictures_hidden:
            cat_map["pictures"] = ("Overview", self.pictures_dir or "(overview)")
        if self.photos_dir:
            cat_map["photos"] = ("Photos", self.photos_dir)
        if self.videos_dir:
            cat_map["videos"] = ("Videos", self.videos_dir)
        if self.screenshots_dir:
            cat_map["screenshots"] = ("Screenshots", self.screenshots_dir)
        if self.nextcloud_enabled and self.nextcloud_url and self.nextcloud_user:
            cat_map["nextcloud"] = (
                "Nextcloud", self.nextcloud_photos_path or "Photos",
            )
        for i, p in enumerate(self.extra_locations):
            custom_name = ""
            if i < len(self.extra_location_names):
                custom_name = (self.extra_location_names[i] or "").strip()
            label = custom_name or Path(p).name or "Locations"
            cat_map[f"location:{i}"] = (label, p)

        order = list(self.media_folder_order or [])
        for key in cat_map:
            if key not in order:
                order.append(key)
        cats: list[tuple[str, str, str]] = []
        for key in order:
            spec = cat_map.get(key)
            if spec is None:
                continue
            label, path = spec
            cats.append((key, label, path))
        return cats

    def media_filter_for(self, category: str) -> str | None:
        """Resolve the per-folder media-type filter for *category*. Returns
        one of "both"/"images"/"videos" for Overview and extra locations
        that have it explicitly set, or None to mean "use the DB's
        category default" (built-ins keep their historic image/video
        split)."""
        if category == "pictures":
            val = self.pictures_media_filter
            return val if val in ("both", "images", "videos") else "images"
        if not category.startswith("location:"):
            return None
        try:
            idx = int(category.split(":", 1)[1])
        except ValueError:
            return None
        if idx < 0 or idx >= len(self.extra_location_media_filter):
            return None
        val = self.extra_location_media_filter[idx]
        if val in ("both", "images", "videos"):
            return val
        return None

    def excluded_subtrees(self) -> list[str]:
        """Absolute paths of extra locations flagged "do not inherit". The
        scanner subtracts these from any parent root's recursive walk so a
        single folder is never listed under both its own category and a
        containing one."""
        out: list[str] = []
        for i, p in enumerate(self.extra_locations):
            if i >= len(self.extra_location_no_inherit):
                break
            if self.extra_location_no_inherit[i] and p:
                out.append(str(Path(p).expanduser()))
        return out

    # ------------------------------------------------------------------
    # Nextcloud helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Ensure URL has a scheme; default to https:// when missing."""
        url = url.strip().rstrip("/")
        if not url:
            return ""
        if not re.match(r"^https?://", url):
            url = "https://" + url
        return url

    # The GVFS-era helpers (nextcloud_webdav_url, nextcloud_local_path,
    # nextcloud_available_folders) used to live here. They were leftovers
    # from a discontinued gio-mount path; the direct WebDAV client
    # (NextcloudClient in nextcloud.py) replaced all of them. Removed so a
    # future caller can't accidentally bring them back into use.

    # ------------------------------------------------------------------
    # App-password keyring helpers (libsecret, falls back to nothing)
    # ------------------------------------------------------------------

    _KEYRING_SCHEMA = "de.cais.Muga.nextcloud"
    # The schema used to read de.furilabs.muga.nextcloud — the namespace of
    # FuriLabs, who make the FuriPhone, not ours. It is only a lookup key, so
    # the old entries stay readable; load_app_password moves them across.
    _LEGACY_KEYRING_SCHEMA = "de.furilabs.muga.nextcloud"
    _CRED_FILE = CONFIG_DIR / "nc_password"

    def _secret_schema(self, name: str):
        """Build a libsecret schema. Raises if libsecret is unavailable."""
        import gi; gi.require_version("Secret", "1")
        from gi.repository import Secret
        return Secret.Schema.new(
            name, Secret.SchemaFlags.NONE,
            {"server": Secret.SchemaAttributeType.STRING,
             "user":   Secret.SchemaAttributeType.STRING},
        )

    def save_app_password(self, password: str) -> bool:
        """Store app-password. Tries system keyring first, falls back to a 0600 file."""
        try:
            import gi; gi.require_version("Secret", "1")
            from gi.repository import Secret
            schema = Secret.Schema.new(
                self._KEYRING_SCHEMA, Secret.SchemaFlags.NONE,
                {"server": Secret.SchemaAttributeType.STRING,
                 "user":   Secret.SchemaAttributeType.STRING},
            )
            ok = Secret.password_store_sync(
                schema,
                {"server": self.nextcloud_url, "user": self.nextcloud_user},
                Secret.COLLECTION_DEFAULT,
                "Muga – Nextcloud App-Passwort",
                password,
                None,
            )
            if ok:
                return True
        except Exception:
            LOGGER.debug("Keyring store failed, falling back to file", exc_info=True)
        # Fallback: plain file with restricted permissions.
        # mkdir(mode=…) only applies on first create; for pre-existing 0755
        # dirs we follow up with an explicit chmod so the secret's parent
        # directory matches the secret's own 0600 file mode.
        # Atomic write (tmp + os.replace) keeps a crash mid-write from
        # truncating an existing password file to zero bytes.
        try:
            parent = self._CRED_FILE.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                parent.chmod(0o700)
            except OSError:
                LOGGER.debug("parent.chmod failed", exc_info=True)
            tmp = self._CRED_FILE.with_suffix(".tmp")
            # Create the tmp file 0600 BEFORE writing the password.
            # path.write_text() creates the file with umask-default
            # permissions (usually 0644) and only chmod-s it after the
            # write — for that window the password is world-readable on
            # multi-user systems. os.open with O_CREAT|O_EXCL|0o600
            # establishes the right mode atomically before any bytes
            # land.
            try:
                fd = os.open(
                    tmp,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC,
                    0o600,
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(password)
                        fh.flush()
                        try:
                            os.fsync(fh.fileno())
                        except OSError:
                            LOGGER.debug("os.fsync failed", exc_info=True)
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        LOGGER.debug("os.close failed", exc_info=True)
                    raise
                os.replace(tmp, self._CRED_FILE)
            finally:
                # If os.replace already moved tmp into place this is a
                # no-op; if anything else failed we don't want a partial
                # password sitting around in cleartext.
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        LOGGER.debug("tmp.unlink failed", exc_info=True)
            return True
        except Exception:
            return False

    def load_app_password(self) -> str:
        """Retrieve app-password. Tries system keyring first, falls back to file.

        A password stored under the old FuriLabs schema is moved across on the
        way out, so the schema rename does not read as a lost connection: the
        user would otherwise be asked to fetch a fresh app password from
        Nextcloud for an account that was already set up.
        """
        try:
            import gi; gi.require_version("Secret", "1")
            from gi.repository import Secret
            attrs = {"server": self.nextcloud_url, "user": self.nextcloud_user}
            result = Secret.password_lookup_sync(
                self._secret_schema(self._KEYRING_SCHEMA), attrs, None,
            )
            if result:
                return result
            legacy = Secret.password_lookup_sync(
                self._secret_schema(self._LEGACY_KEYRING_SCHEMA), attrs, None,
            )
            if legacy:
                self._migrate_keyring_entry(legacy)
                return legacy
        except Exception:
            LOGGER.debug("Keyring lookup failed, falling back to file", exc_info=True)
        # Fallback: file
        try:
            return self._CRED_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _migrate_keyring_entry(self, password: str) -> None:
        """Re-store a password found under the legacy schema, then drop the old
        entry. Best-effort: if the store fails the old entry is kept, so the
        password is never dropped on the floor between the two schemas.
        """
        try:
            import gi; gi.require_version("Secret", "1")
            from gi.repository import Secret
            attrs = {"server": self.nextcloud_url, "user": self.nextcloud_user}
            stored = Secret.password_store_sync(
                self._secret_schema(self._KEYRING_SCHEMA), attrs,
                Secret.COLLECTION_DEFAULT,
                "Muga – Nextcloud App-Passwort", password, None,
            )
            if not stored:
                return
            Secret.password_clear_sync(
                self._secret_schema(self._LEGACY_KEYRING_SCHEMA), attrs, None,
            )
            LOGGER.info("Moved the Nextcloud password to the %s schema",
                        self._KEYRING_SCHEMA)
        except Exception:
            LOGGER.debug("Keyring migration failed", exc_info=True)

    def clear_app_password(self) -> None:
        try:
            import gi; gi.require_version("Secret", "1")
            from gi.repository import Secret
            schema = Secret.Schema.new(
                self._KEYRING_SCHEMA, Secret.SchemaFlags.NONE,
                {"server": Secret.SchemaAttributeType.STRING,
                 "user":   Secret.SchemaAttributeType.STRING},
            )
            attrs = {"server": self.nextcloud_url, "user": self.nextcloud_user}
            Secret.password_clear_sync(schema, attrs, None)
            # Also clear the legacy schema: disconnecting must not leave the
            # password behind under the old name for an install that never
            # went through the migration in load_app_password.
            Secret.password_clear_sync(
                self._secret_schema(self._LEGACY_KEYRING_SCHEMA), attrs, None,
            )
        except Exception:
            LOGGER.debug("Keyring clear failed", exc_info=True)
        try:
            self._CRED_FILE.unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("_CRED_FILE.unlink failed", exc_info=True)
