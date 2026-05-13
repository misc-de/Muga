from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "yaga"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "yaga"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "yaga"
THUMB_DIR = CACHE_DIR / "thumbnails"
DB_PATH = DATA_DIR / "yaga.sqlite3"
DEBUG_LOG_PATH = CACHE_DIR / "debug.log"
TRACE_LOG_PATH = CACHE_DIR / "trace.log"


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
    language: str = "system"
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
    cache_max_mb: int = 0

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

    @classmethod
    def load(cls) -> "Settings":
        path = CONFIG_DIR / "settings.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}
        settings = cls(**{k: v for k, v in data.items() if k in known})
        settings.grid_columns = min(max(int(settings.grid_columns), 2), 10)
        # Clamp legacy / hand-edited values to the four supported positions so a
        # typo in settings.json doesn't crash the layout logic in _build_ui.
        if settings.nav_position not in ("top", "bottom", "left", "right"):
            settings.nav_position = "top"
        if settings.handedness not in ("left", "right"):
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

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_DIR / "settings.json"
        path.write_text(json.dumps(self.__dict__, indent=2, ensure_ascii=False), encoding="utf-8")

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

    _KEYRING_SCHEMA = "de.furilabs.yaga.nextcloud"
    _CRED_FILE = CONFIG_DIR / "nc_password"

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
                "Yaga – Nextcloud App-Passwort",
                password,
                None,
            )
            if ok:
                return True
        except Exception:
            pass
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
                pass
            tmp = self._CRED_FILE.with_suffix(".tmp")
            try:
                tmp.write_text(password, encoding="utf-8")
                tmp.chmod(0o600)
                os.replace(tmp, self._CRED_FILE)
            finally:
                # If os.replace already moved tmp into place this is a
                # no-op; if anything else failed we don't want a partial
                # password sitting around in cleartext.
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            return True
        except Exception:
            return False

    def load_app_password(self) -> str:
        """Retrieve app-password. Tries system keyring first, falls back to file."""
        try:
            import gi; gi.require_version("Secret", "1")
            from gi.repository import Secret
            schema = Secret.Schema.new(
                self._KEYRING_SCHEMA, Secret.SchemaFlags.NONE,
                {"server": Secret.SchemaAttributeType.STRING,
                 "user":   Secret.SchemaAttributeType.STRING},
            )
            result = Secret.password_lookup_sync(
                schema,
                {"server": self.nextcloud_url, "user": self.nextcloud_user},
                None,
            )
            if result:
                return result
        except Exception:
            pass
        # Fallback: file
        try:
            return self._CRED_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def clear_app_password(self) -> None:
        try:
            import gi; gi.require_version("Secret", "1")
            from gi.repository import Secret
            schema = Secret.Schema.new(
                self._KEYRING_SCHEMA, Secret.SchemaFlags.NONE,
                {"server": Secret.SchemaAttributeType.STRING,
                 "user":   Secret.SchemaAttributeType.STRING},
            )
            Secret.password_clear_sync(
                schema,
                {"server": self.nextcloud_url, "user": self.nextcloud_user},
                None,
            )
        except Exception:
            pass
        try:
            self._CRED_FILE.unlink(missing_ok=True)
        except OSError:
            pass
