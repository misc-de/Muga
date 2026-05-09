"""Direct WebDAV client for Nextcloud — no GVFS/FUSE required."""
from __future__ import annotations

import base64
import email.utils
import http.client
import logging
import ssl
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree

from . import VERSION
from .config import CACHE_DIR, THUMB_DIR

USER_AGENT = f"Yaga/{VERSION}"
LOGGER = logging.getLogger(__name__)

# Local directories for cached full-res files and thumbnails
_NC_CACHE = CACHE_DIR / "nextcloud"
_NC_THUMB = THUMB_DIR / "nextcloud"

# Prefix stored in DB to identify nextcloud paths
NC_PATH_PREFIX = "nextcloud://"


def nc_path(dav_path: str) -> str:
    """Encode a WebDAV path as a DB-safe string."""
    return NC_PATH_PREFIX + dav_path.lstrip("/")


def dav_path_from_nc(nc: str) -> str:
    """Reverse nc_path()."""
    return "/" + nc.removeprefix(NC_PATH_PREFIX)


def is_nc_path(path: str) -> bool:
    return path.startswith(NC_PATH_PREFIX)


class NextcloudClient:
    def __init__(self, server_url: str, username: str, app_password: str) -> None:
        url = server_url.strip()
        if not url.startswith("http"):
            url = "https://" + url
        parsed = urlparse(url)
        self.host: str = parsed.netloc or parsed.path.split("/")[0]
        self.use_ssl: bool = parsed.scheme != "http"
        self.username = username
        self._auth = base64.b64encode(f"{username}:{app_password}".encode()).decode()
        self.dav_root = f"/remote.php/dav/files/{username}"

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _conn(self) -> http.client.HTTPConnection:
        if self.use_ssl:
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(self.host, context=ctx, timeout=30)
        return http.client.HTTPConnection(self.host, timeout=30)

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Basic {self._auth}", "User-Agent": USER_AGENT}
        if extra:
            h.update(extra)
        return h

    # ------------------------------------------------------------------
    # WebDAV PROPFIND
    # ------------------------------------------------------------------

    def list_files(self, remote_folder: str) -> list[dict]:
        """
        Return list of dicts for all files under *remote_folder*.
        Keys: dav_path, size, mtime, name
        """
        folder_path = f"{self.dav_root}/{remote_folder.strip('/')}"
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<D:propfind xmlns:D="DAV:">'
            "<D:prop><D:displayname/><D:getcontentlength/>"
            "<D:getlastmodified/><D:resourcetype/></D:prop>"
            "</D:propfind>"
        )
        started = time.monotonic()
        conn = self._conn()
        try:
            LOGGER.info("Nextcloud PROPFIND started for %r", remote_folder)
            conn.request(
                "PROPFIND",
                quote(folder_path, safe="/:@!$&'()*+,;="),
                body,
                self._headers({"Depth": "infinity", "Content-Type": "application/xml"}),
            )
            resp = conn.getresponse()
            if resp.status == 401:
                raise PermissionError(f"Authentication failed (HTTP 401) – check app password")
            if resp.status == 404:
                raise FileNotFoundError(f"Folder not found: {remote_folder!r} (HTTP 404)")
            if resp.status not in (207,):
                raise OSError(f"Unexpected HTTP status {resp.status} from {self.host}")
            data = resp.read()
        finally:
            conn.close()
        results = self._parse_propfind(data, folder_path)
        LOGGER.info(
            "Nextcloud PROPFIND finished for %r: %s file(s) in %.2fs",
            remote_folder,
            len(results),
            time.monotonic() - started,
        )
        return results

    def _parse_propfind(self, data: bytes, base_path: str) -> list[dict]:
        ns = {"D": "DAV:"}
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            return []
        results: list[dict] = []
        for response in root.findall("D:response", ns):
            href = unquote(response.findtext("D:href", "", ns))
            prop = response.find(".//D:prop", ns)
            if prop is None:
                continue
            is_dir = prop.find("D:resourcetype/D:collection", ns) is not None
            if is_dir:
                continue
            # Skip the root itself
            if href.rstrip("/") == base_path.rstrip("/"):
                continue
            size_text = prop.findtext("D:getcontentlength", "0", ns)
            mtime_text = prop.findtext("D:getlastmodified", "", ns)
            try:
                size = int(size_text)
            except ValueError:
                size = 0
            try:
                mtime = email.utils.parsedate_to_datetime(mtime_text).timestamp()
            except Exception:
                mtime = time.time()
            name = href.rstrip("/").rsplit("/", 1)[-1]
            results.append({"dav_path": href, "size": size, "mtime": mtime, "name": name})
        return results

    # ------------------------------------------------------------------
    # Thumbnail via Nextcloud preview API
    # ------------------------------------------------------------------

    def ensure_thumbnail(self, dav_path: str, size: int = 256) -> str | None:
        """
        Download a thumbnail via the Nextcloud preview API.
        Returns local path on success, None on failure.
        """
        # Derive a stable local filename from the dav_path
        safe = dav_path.lstrip("/").replace("/", "_")
        dest = _NC_THUMB / f"{safe}.jpg"
        if dest.exists():
            return str(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Strip /remote.php/dav/files/<user>/ prefix to get plain file path
        prefix = self.dav_root + "/"
        file_path = dav_path[len(prefix):] if dav_path.startswith(prefix) else dav_path.lstrip("/")
        thumb_url = (
            f"/index.php/apps/files/api/v1/thumbnail/{size}/{size}/"
            + quote(file_path, safe="/")
        )
        conn = self._conn()
        try:
            conn.request("GET", thumb_url, headers=self._headers())
            resp = conn.getresponse()
            if resp.status == 200:
                dest.write_bytes(resp.read())
                return str(dest)
            LOGGER.debug("Nextcloud thumbnail HTTP %s for %s", resp.status, dav_path)
        except Exception:
            LOGGER.debug("Nextcloud thumbnail download failed for %s", dav_path, exc_info=True)
        finally:
            conn.close()
        return None

    # ------------------------------------------------------------------
    # Download full file for viewing/editing
    # ------------------------------------------------------------------

    def download_file(self, dav_path: str) -> str | None:
        """
        Download a file to local cache.
        Returns local path on success, None on failure.
        """
        safe = dav_path.lstrip("/").replace("/", "_")
        dest = _NC_CACHE / safe
        if dest.exists():
            return str(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        try:
            conn.request(
                "GET",
                quote(dav_path, safe="/:@!$&'()*+,;="),
                headers=self._headers(),
            )
            resp = conn.getresponse()
            if resp.status == 200:
                dest.write_bytes(resp.read())
                return str(dest)
            LOGGER.debug("Nextcloud file HTTP %s for %s", resp.status, dav_path)
        except Exception:
            LOGGER.debug("Nextcloud file download failed for %s", dav_path, exc_info=True)
        finally:
            conn.close()
        return None

    def upload_file(self, local_path: Path | str, dav_path: str) -> bool:
        """
        Upload a local file to Nextcloud.
        Returns True on success, False on failure.
        """
        local_path = Path(local_path)
        if not local_path.exists():
            LOGGER.error("Local file does not exist: %s", local_path)
            return False
        
        try:
            file_content = local_path.read_bytes()
        except Exception as exc:
            LOGGER.error("Failed to read local file %s: %s", local_path, exc)
            return False
        
        conn = self._conn()
        try:
            headers = self._headers()
            headers["Content-Type"] = "application/octet-stream"
            conn.request(
                "PUT",
                quote(dav_path, safe="/:@!$&'()*+,;="),
                body=file_content,
                headers=headers,
            )
            resp = conn.getresponse()
            resp.read()  # consume response body
            
            if resp.status in (201, 204):  # Created or No Content
                LOGGER.info("Successfully uploaded %s to %s", local_path, dav_path)
                return True

            LOGGER.warning("Nextcloud upload HTTP %s for %s", resp.status, dav_path)
            return False
        except Exception as exc:
            LOGGER.error("Nextcloud upload failed for %s: %s", dav_path, exc)
            return False
        finally:
            conn.close()

    def mkcol(self, dav_path: str) -> bool:
        """Create a remote collection (folder) at *dav_path* via WebDAV MKCOL."""
        conn = self._conn()
        try:
            conn.request(
                "MKCOL",
                quote(dav_path, safe="/:@!$&'()*+,;="),
                headers=self._headers(),
            )
            resp = conn.getresponse()
            resp.read()
            if resp.status in (201, 204):
                return True
            LOGGER.warning("Nextcloud MKCOL HTTP %s for %s", resp.status, dav_path)
            return False
        except Exception as exc:
            LOGGER.error("Nextcloud MKCOL failed for %s: %s", dav_path, exc)
            return False
        finally:
            conn.close()

