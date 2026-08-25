"""A read-only MCP server over the media index.

Muga already knows where every photo and video on the device is, what folder
it sits in, when it was taken and what its EXIF says. This exposes that to an
MCP client ("find the videos I shot last August", "how many screenshots are
in the index"). The read tools are all a SELECT; the write ones (delete, move,
add) exist only while the user has switched write access on, and every path
they take goes through GalleryTools._resolve_for_write first.

Transport is MCP's Streamable HTTP on ``POST /mcp``. Responses are plain
``application/json`` — the server never needs to push, so it does not open an
SSE stream, which the transport explicitly permits. ``GET /mcp`` therefore
answers 405, telling a client not to wait for one.

Authentication is a bearer token from :mod:`muga.mcp_tokens`, and a token has
to exist before the server will start at all — without one there is nothing a
client could authenticate with, and a listening socket that refuses every
request is only a way to be wrong later. Ahead of the token, every request is
checked against :func:`origin_is_allowed`, which is what keeps a page in the
user's browser from reaching this port through DNS rebinding.

How far it listens is the user's choice (``mcp_bind``): loopback only by
default, or the LAN address, a public address, or every interface. Widening
it is a deliberate act; turning the server on is not enough.

Lifecycle is a process-wide singleton (:func:`sync_with_settings`) rather than
something owned by the window. A nav-position change destroys and recreates
GalleryWindow, and a window-owned server would have to release the port before
its replacement could bind it — the new window is built first, so that race
would surface as "MCP silently stopped working after changing a setting".
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import logging
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

from . import APP_ID, VERSION
from .config import DEFAULT_MCP_PORT
from .mcp_tokens import TOKENS_PATH, TokenStore

if TYPE_CHECKING:
    from .config import Settings
    from .database import Database

LOGGER = logging.getLogger(__name__)

# The revision of the MCP spec this implements. A client asking for another
# one still gets served — we answer with what we speak and let it decide.
PROTOCOL_VERSION = "2025-06-18"

MCP_PATH = "/mcp"

# Hard ceiling on a request body. Nothing legitimate here comes close; the
# limit exists so an unauthenticated caller cannot make the server buffer an
# arbitrary amount of memory before the token is even checked.
_MAX_BODY = 1 << 20

# Ceiling on how many items one tool call returns, whatever limit was asked
# for. A library can hold six figures of rows and a single JSON response with
# all of them helps nobody — clients paginate with offset instead.
_MAX_ITEMS = 200

# JSON-RPC 2.0 error codes.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Address helpers
# ---------------------------------------------------------------------------

# How far the server is allowed to listen. The default is deliberately the
# narrowest one: a gallery index should not become reachable from the network
# because a switch got flipped without reading the label.
BIND_LOCAL = "local"
BIND_LAN = "lan"
BIND_PUBLIC = "public"
BIND_ALL = "all"
BIND_SCOPES = (BIND_LOCAL, BIND_LAN, BIND_PUBLIC, BIND_ALL)
DEFAULT_BIND = BIND_LOCAL

LOOPBACK = "127.0.0.1"
ANY_ADDRESS = "0.0.0.0"

# Linux ioctl for "give me this interface's IPv4 address".
_SIOCGIFADDR = 0x8915


def lan_ip() -> str:
    """Best guess at the address a client on the LAN should dial.

    Opens a UDP socket towards a TEST-NET-1 address and asks the kernel which
    local address it would route from. No packet is ever sent — a UDP connect
    only fixes the socket's peer — so this neither touches the network nor
    depends on that address existing. Falls back to loopback when there is no
    route at all (offline, or a phone with the modem down).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return LOOPBACK
    finally:
        sock.close()


def _interface_addresses() -> list[str]:
    """Every IPv4 address currently configured on an interface.

    ``socket.if_nameindex`` plus a SIOCGIFADDR ioctl, because Python has no
    portable getifaddrs and the usual substitute — resolving the hostname —
    answers ``127.0.1.1`` on a Debian box and nothing useful on a phone.
    Interfaces without an IPv4 address (a down modem, a v6-only link) raise
    from the ioctl and are simply skipped.
    """
    addresses: list[str] = []
    try:
        names = [name for _index, name in socket.if_nameindex()]
    except OSError:
        LOGGER.debug("if_nameindex failed", exc_info=True)
        return addresses
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for name in names:
            try:
                packed = fcntl.ioctl(
                    sock.fileno(), _SIOCGIFADDR,
                    struct.pack("256s", name.encode("utf-8")[:15]),
                )
            except OSError:
                continue
            try:
                addresses.append(socket.inet_ntoa(packed[20:24]))
            except OSError:
                LOGGER.debug("Could not decode the address of %s", name, exc_info=True)
    return addresses


def available_addresses() -> dict[str, str]:
    """The concrete address behind each bind scope, right now.

    Returns one entry per scope in :data:`BIND_SCOPES`; a scope with no
    matching interface maps to an empty string, which is what lets Settings
    offer only what this machine actually has. "all" is always available — it
    is a wildcard, not an interface.

    The routing address from :func:`lan_ip` is consulted first for its own
    class, so a machine on several networks reports the one it would actually
    reach the internet through rather than an arbitrary first match.
    """
    found: dict[str, str] = {
        BIND_LOCAL: LOOPBACK,
        BIND_LAN: "",
        BIND_PUBLIC: "",
        BIND_ALL: ANY_ADDRESS,
    }
    candidates = [lan_ip(), *_interface_addresses()]
    for candidate in candidates:
        try:
            parsed = ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if parsed.is_loopback:
            continue
        # is_global excludes the RFC1918 ranges, link-local, CGNAT and the
        # documentation blocks — exactly the split the two scopes need.
        scope = BIND_PUBLIC if parsed.is_global else BIND_LAN
        if not found[scope]:
            found[scope] = candidate
    return found


def resolve_bind(scope: str) -> tuple[str, str]:
    """Map a bind scope to (address to bind, address to hand to a client).

    The two differ for "all": a socket binds ``0.0.0.0``, but telling someone
    to connect there is useless, so the advertised address is the best real
    one the machine has.

    A scope whose interface has since gone away (``lan`` saved, then the wifi
    dropped) falls back to loopback rather than to something wider. Falling
    inward is the only safe direction to guess in.
    """
    scope = scope if scope in BIND_SCOPES else DEFAULT_BIND
    addresses = available_addresses()
    if scope == BIND_ALL:
        advertised = addresses[BIND_LAN] or addresses[BIND_PUBLIC] or LOOPBACK
        return ANY_ADDRESS, advertised
    address = addresses.get(scope) or LOOPBACK
    return address, address


# ---------------------------------------------------------------------------
# Origin validation
# ---------------------------------------------------------------------------

def origin_is_allowed(origin: str) -> bool:
    """Whether a request carrying this ``Origin`` header may be served.

    This is the DNS-rebinding defence the Streamable HTTP transport requires
    of every server ("Servers MUST validate the Origin header on all incoming
    connections"). The attack it stops: a page the user visits resolves its
    own hostname to 127.0.0.1, then POSTs to the MCP port from inside their
    browser, reaching a socket that is otherwise unreachable from the network.
    The browser still labels those requests with the *page's* origin, so
    refusing everything that is not itself loopback breaks the rebind while
    leaving a genuinely local browser client working.

    The bearer token already refuses such a request — but a token is a secret
    that can leak into a client config a page can read, and the transport asks
    for this check on its own account, so it does not lean on that.

    An absent header is allowed: a browser always sends one on a cross-origin
    POST, while the CLI and desktop MCP clients this server is actually for
    send none at all. Rejecting them to catch an attacker who could simply
    omit the header would cost every real client and buy nothing.
    """
    text = (origin or "").strip()
    if not text:
        return True
    # A sandboxed iframe, a file:// page or a redirected form post sends the
    # literal string "null". Nothing legitimate here does.
    if text.lower() == "null":
        return False
    try:
        parsed = urlsplit(text)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip()
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A name, not an address — which is exactly what a rebound host looks
        # like. Names are not resolved here on purpose: resolving one would
        # ask the very DNS the attack controls.
        return False


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

# Category used when a caller wants "everything". "pictures" is Muga's virtual
# aggregator across every local category, and the database de-duplicates it to
# one row per file, so a photo inside two overlapping media folders is listed
# once. Paired with media_filter="both" it covers images and videos alike.
_ALL = "pictures"

_CATEGORY_DESC = (
    "Category key from list_categories. Use \"all\" (the default) to search "
    "the whole library, images and videos together."
)
_SORT_VALUES = ["newest", "oldest", "name", "name_desc", "folder", "folder_desc"]


def _limit_schema(default: int) -> dict:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": _MAX_ITEMS,
        "default": default,
        "description": f"How many items to return (max {_MAX_ITEMS}).",
    }


_OFFSET_SCHEMA = {
    "type": "integer",
    "minimum": 0,
    "default": 0,
    "description": "Items to skip — use with limit to page through results.",
}


class GalleryTools:
    """The tool surface, bound to one database + settings pair.

    Held separately from the HTTP plumbing so the tools can be exercised
    without a socket, and so a window rebuild can swap the database reference
    underneath a running server.
    """

    def __init__(self, database: "Database", settings: "Settings",
                 on_change: Callable[[], None] | None = None) -> None:
        self.database = database
        self.settings = settings
        # Called after a write tool changed something on disk, so the gallery
        # can re-render. Without it a photo deleted over MCP stays on screen
        # until the user refreshes by hand and then opens a tile that is gone.
        self.on_change = on_change

    # -- write gate ------------------------------------------------------

    def writes_allowed(self) -> bool:
        """Whether the write tools exist at all right now.

        Read straight from settings on every call rather than cached: the user
        can flip the switch while a client is connected, and the next request
        has to see it — including turning it *off*, which must take effect
        without restarting the server.
        """
        return bool(getattr(self.settings, "mcp_write_enabled", False))

    def _media_roots(self) -> list[Path]:
        """The local directories a write tool may touch, fully resolved.

        Everything the user configured as a media folder, and nothing else.
        Resolved so the comparison in _inside_roots comes down to plain path
        prefixes with no symlink left to reinterpret them.
        """
        roots: list[Path] = []
        for key, _label, path in self.settings.categories():
            # Overview aggregates the others and its path slot holds a legacy
            # value that is never scanned; Nextcloud is a server-side path, not
            # a local directory.
            if key in (_ALL, "nextcloud") or not path:
                continue
            try:
                resolved = Path(path).expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved.is_dir():
                roots.append(resolved)
        return roots

    def _inside_roots(self, candidate: Path, roots: list[Path]) -> bool:
        return any(candidate == root or root in candidate.parents for root in roots)

    def _resolve_for_write(self, raw: str, *, must_exist: bool = True) -> Path:
        """Turn a client-supplied path into one it is allowed to write to.

        This is the whole security boundary of the write tools, so it is
        deliberately strict:

        * the path is resolved first, so ``photos/link/../../../etc/passwd``
          and a symlink pointing out of the library both collapse to what they
          really are before anything is compared;
        * the result has to sit inside a configured media folder — the same
          directories the gallery already shows;
        * a symlink is refused outright even when its target is inside, because
          deleting one and deleting what it points at are different acts and
          the client cannot tell which it asked for.
        """
        text = (raw or "").strip()
        if not text:
            raise ValueError("path is required")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            raise ValueError("path must be absolute")
        if candidate.is_symlink():
            raise ValueError("refusing to operate on a symlink")
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"path cannot be resolved: {exc}") from exc
        roots = self._media_roots()
        if not roots:
            raise ValueError("no media folders are configured")
        if not self._inside_roots(resolved, roots):
            raise ValueError("path is outside the configured media folders")
        return resolved

    def _notify_change(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:
            LOGGER.debug("MCP change callback failed", exc_info=True)

    # -- schema ---------------------------------------------------------

    def descriptors(self) -> list[dict]:
        """The ``tools/list`` payload.

        The write tools are appended only while the user has switched write
        access on, so a client with read-only access is not shown capabilities
        it will be refused. tools/list is also re-read by clients after a
        change notification, and this is computed fresh each time.
        """
        tools = self._read_descriptors()
        if self.writes_allowed():
            tools.extend(self._write_descriptors())
        return tools

    def _write_descriptors(self) -> list[dict]:
        folder_arg = {
            "type": "string",
            "description": (
                "Absolute path of the destination folder. Must be inside one "
                "of the gallery's media folders."
            ),
        }
        name_arg = {
            "type": "string",
            "description": "New file name. Defaults to the source's own name.",
        }
        return [
            {
                "name": "delete_media",
                "title": "Delete a file",
                "description": (
                    "Move a file to the desktop trash and drop it from the "
                    "index. Recoverable from the trash until it is emptied. "
                    "Only works on files inside the gallery's media folders. "
                    "Fails on filesystems with no trash of their own (FAT "
                    "cards, many network mounts) rather than deleting outright."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path, as returned by the read tools.",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "move_media",
                "title": "Move or rename a file",
                "description": (
                    "Move a file to another folder, optionally under a new "
                    "name. Both the file and the destination must be inside "
                    "the gallery's media folders."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path of the file."},
                        "target_folder": folder_arg,
                        "name": name_arg,
                    },
                    "required": ["path", "target_folder"],
                },
            },
            {
                "name": "add_media",
                "title": "Add a file to the gallery",
                "description": (
                    "Copy an image or video from elsewhere on this device into "
                    "one of the gallery's media folders. The original is left "
                    "alone. The copy shows up in the gallery after the next "
                    "scan."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_path": {
                            "type": "string",
                            "description": (
                                "Absolute path of the file to copy. Must be an "
                                "image or video; may be anywhere readable."
                            ),
                        },
                        "target_folder": folder_arg,
                        "name": name_arg,
                    },
                    "required": ["source_path", "target_folder"],
                },
            },
        ]

    def _read_descriptors(self) -> list[dict]:
        return [
            {
                "name": "list_categories",
                "title": "List categories",
                "description": (
                    "The gallery's categories (Photos, Videos, Screenshots, any "
                    "extra folders, Nextcloud) with their folder path and how "
                    "many items each holds. Start here: every other tool takes "
                    "one of these keys."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "search_media",
                "title": "Search media",
                "description": (
                    "Search the index by filename, EXIF text, a year (2024), a "
                    "year-month (2024-08) or a month name in German or English "
                    "(\"August\"). Returns the newest matches first unless told "
                    "otherwise."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for. Empty matches everything.",
                        },
                        "category": {"type": "string", "default": "all", "description": _CATEGORY_DESC},
                        "folder": {
                            "type": "string",
                            "description": "Restrict to this exact folder path.",
                        },
                        "sort": {"type": "string", "enum": _SORT_VALUES, "default": "newest"},
                        "limit": _limit_schema(50),
                        "offset": _OFFSET_SCHEMA,
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "list_media",
                "title": "List media",
                "description": (
                    "List the media in a category, optionally narrowed to one "
                    "folder. Use search_media when there is something to match on."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "default": "all", "description": _CATEGORY_DESC},
                        "folder": {
                            "type": "string",
                            "description": "Restrict to this exact folder path.",
                        },
                        "sort": {"type": "string", "enum": _SORT_VALUES, "default": "newest"},
                        "limit": _limit_schema(50),
                        "offset": _OFFSET_SCHEMA,
                    },
                },
            },
            {
                "name": "list_folders",
                "title": "List folders",
                "description": "The folders inside a category, each with its item count.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": _CATEGORY_DESC},
                    },
                    "required": ["category"],
                },
            },
            {
                "name": "get_media",
                "title": "Get media details",
                "description": (
                    "Everything the index holds about one file, given its path: "
                    "size, modification time, category, folder, and the EXIF "
                    "block if one was extracted."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path as returned by the other tools.",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "gallery_stats",
                "title": "Gallery statistics",
                "description": (
                    "Totals across the whole index — item counts per category, "
                    "images vs videos, and the size on disk."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    # -- dispatch -------------------------------------------------------

    _WRITE_TOOLS = ("delete_media", "move_media", "add_media")

    def call(self, name: str, arguments: dict) -> Any:
        if name in self._WRITE_TOOLS and not self.writes_allowed():
            # Checked here as well as in descriptors(): hiding a tool from the
            # listing is presentation, refusing to run it is the actual rule.
            # A client that remembers the name from an earlier session, or
            # guesses it, must still be turned away.
            raise PermissionError(name)
        handler: Callable[[dict], Any] | None = {
            "list_categories": self._list_categories,
            "search_media": self._search_media,
            "list_media": self._list_media,
            "list_folders": self._list_folders,
            "get_media": self._get_media,
            "gallery_stats": self._gallery_stats,
            "delete_media": self._delete_media,
            "move_media": self._move_media,
            "add_media": self._add_media,
        }.get(name)
        if handler is None:
            raise LookupError(name)
        return handler(arguments or {})

    # -- write tools ----------------------------------------------------

    def _delete_media(self, args: dict) -> dict:
        path = self._resolve_for_write(args.get("path", ""))
        if not path.is_file():
            raise ValueError("not a file")
        # The trash, not unlink: the gallery's own delete does the same, and a
        # mistaken tool call should be recoverable from the desktop's trash
        # rather than gone. A failure here is reported, never quietly
        # downgraded to a permanent delete.
        from gi.repository import Gio

        try:
            ok = Gio.File.new_for_path(str(path)).trash(None)
        except Exception as exc:
            raise ValueError(f"could not move to trash: {exc}") from exc
        if not ok:
            raise ValueError("could not move to trash")
        self.database.delete_path(str(path))
        self.database.commit()
        self._notify_change()
        return {"deleted": True, "path": str(path), "to": "trash"}

    def _move_media(self, args: dict) -> dict:
        source = self._resolve_for_write(args.get("path", ""))
        if not source.is_file():
            raise ValueError("not a file")
        target_dir = self._resolve_for_write(args.get("target_folder", ""))
        if not target_dir.is_dir():
            raise ValueError("target_folder is not a directory")
        name = str(args.get("name") or source.name).strip()
        if "/" in name or name in ("", ".", ".."):
            raise ValueError("name must be a plain file name")
        destination = target_dir / name
        if destination.exists():
            raise ValueError(f"{name} already exists in the target folder")
        try:
            source.rename(destination)
        except OSError as exc:
            # Across filesystems rename fails with EXDEV; copy+remove is what
            # the gallery's own move does in that case.
            import shutil

            try:
                shutil.move(str(source), str(destination))
            except Exception as inner:
                raise ValueError(f"could not move: {inner}") from exc
        self.database.delete_path(str(source))
        self.database.commit()
        self._notify_change()
        return {"moved": True, "from": str(source), "to": str(destination)}

    def _add_media(self, args: dict) -> dict:
        raw_source = str(args.get("source_path") or "").strip()
        if not raw_source:
            raise ValueError("source_path is required")
        source = Path(raw_source).expanduser()
        if not source.is_absolute():
            raise ValueError("source_path must be absolute")
        try:
            source = source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"source_path cannot be resolved: {exc}") from exc
        if not source.is_file():
            raise ValueError("source_path is not a file")
        # The source may sit anywhere the user can read, but it has to be
        # something this gallery would show. Without that check the tool is a
        # way to copy arbitrary files into the media folders — and then to have
        # their paths listed back out.
        from .models import media_type_for

        if media_type_for(source) is None:
            raise ValueError("source_path is not an image or video")

        target_dir = self._resolve_for_write(args.get("target_folder", ""))
        if not target_dir.is_dir():
            raise ValueError("target_folder is not a directory")
        name = str(args.get("name") or source.name).strip()
        if "/" in name or name in ("", ".", ".."):
            raise ValueError("name must be a plain file name")
        destination = target_dir / name
        if destination.exists():
            raise ValueError(f"{name} already exists in the target folder")

        import shutil

        try:
            # copy2 keeps the mtime, so a photo imported today does not sort to
            # the top of a gallery ordered by date.
            shutil.copy2(str(source), str(destination))
        except Exception as exc:
            raise ValueError(f"could not copy: {exc}") from exc
        self._notify_change()
        return {
            "added": True,
            "path": str(destination),
            "indexed": False,
            "note": "The file is on disk; it appears in the gallery after the next scan.",
        }

    # -- helpers --------------------------------------------------------

    def _resolve_category(self, raw: Any) -> tuple[str, str | None]:
        """Map a tool's *category* argument to (category, media_filter).

        "all" (and an omitted value) means the aggregate view with the type
        filter dropped. A real category keeps whatever filter the user
        configured for it, so an extra folder set to videos-only reads the
        same way over MCP as it does in the gallery.
        """
        category = str(raw or "all").strip() or "all"
        if category in ("all", "*", ""):
            return _ALL, "both"
        return category, self.settings.media_filter_for(category)

    @staticmethod
    def _clamp(value: Any, default: int, low: int, high: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(number, high))

    def _paging(self, args: dict, default_limit: int) -> tuple[int, int]:
        return (
            self._clamp(args.get("limit", default_limit), default_limit, 1, _MAX_ITEMS),
            self._clamp(args.get("offset", 0), 0, 0, 1 << 30),
        )

    @staticmethod
    def _sort(args: dict) -> str:
        sort = str(args.get("sort") or "newest")
        return sort if sort in _SORT_VALUES else "newest"

    @staticmethod
    def _item(item) -> dict:
        from datetime import datetime, timezone

        out = {
            "path": item.path,
            "name": item.name,
            "category": item.category,
            "folder": item.folder,
            "type": item.media_type,
            "size_bytes": item.size,
            "modified": datetime.fromtimestamp(item.mtime, timezone.utc).isoformat(timespec="seconds"),
        }
        # Only present when the file carries a capture date. Local time
        # without an offset, because that is what EXIF records — the camera's
        # wall clock, with no zone to convert from. A client must not read it
        # as UTC, and an absent key says "unknown" more honestly than a
        # timestamp copied from the file's mtime would.
        if getattr(item, "taken_at", None):
            out["taken"] = datetime.fromtimestamp(item.taken_at).isoformat(timespec="seconds")
        return out

    def _include_nc(self) -> bool:
        """Whether Nextcloud rows belong in an aggregate listing — the same
        condition the gallery's Overview uses, so MCP never surfaces items a
        disabled connection has hidden from the UI."""
        return bool(
            self.settings.nextcloud_enabled
            and self.settings.nextcloud_show_in_pictures
        )

    # -- tool bodies ----------------------------------------------------

    def _list_categories(self, _args: dict) -> dict:
        out = []
        for key, label, path in self.settings.categories():
            out.append({
                "key": key,
                "label": label,
                "path": path,
                "count": self.database.count_media(
                    key,
                    include_nc=(key == _ALL and self._include_nc()),
                    media_filter=self.settings.media_filter_for(key),
                ),
            })
        return {"categories": out}

    def _search_media(self, args: dict) -> dict:
        category, media_filter = self._resolve_category(args.get("category"))
        limit, offset = self._paging(args, 50)
        query = str(args.get("query") or "")
        folder = args.get("folder") or None
        include_nc = category == _ALL and self._include_nc()
        items = self.database.search_media(
            category, query, sort_mode=self._sort(args), folder=folder,
            include_nc=include_nc, limit=limit, offset=offset,
            media_filter=media_filter,
        )
        total = self.database.search_media_count(
            category, query, folder=folder, include_nc=include_nc,
            media_filter=media_filter,
        )
        return {
            "query": query,
            "category": category,
            "total": total,
            "offset": offset,
            "items": [self._item(i) for i in items],
        }

    def _list_media(self, args: dict) -> dict:
        category, media_filter = self._resolve_category(args.get("category"))
        limit, offset = self._paging(args, 50)
        folder = args.get("folder") or None
        include_nc = category == _ALL and self._include_nc()
        items = self.database.list_media_paginated(
            category, sort_mode=self._sort(args), folder=folder,
            limit=limit, offset=offset, include_nc=include_nc,
            media_filter=media_filter,
        )
        total = self.database.count_media(
            category, folder=folder, include_nc=include_nc, media_filter=media_filter,
        )
        return {
            "category": category,
            "total": total,
            "offset": offset,
            "items": [self._item(i) for i in items],
        }

    def _list_folders(self, args: dict) -> dict:
        category, _ = self._resolve_category(args.get("category"))
        folders = [
            {"path": path, "count": count}
            for path, count, _thumb in self.database.folders(category)
        ]
        return {"category": category, "folders": folders}

    def _get_media(self, args: dict) -> dict:
        path = str(args.get("path") or "").strip()
        if not path:
            raise ValueError("path is required")
        item = self.database.get_media_by_path(path)
        if item is None:
            return {"found": False, "path": path}
        details = self._item(item)
        details["found"] = True
        raw_exif = self.database.get_exif_data(item.path, item.category)
        if raw_exif:
            try:
                details["exif"] = json.loads(raw_exif)
            except json.JSONDecodeError:
                # The column is written by the scanner as JSON, but a row from
                # an older schema (or a partial write) should degrade to "no
                # EXIF" rather than failing the whole lookup.
                LOGGER.debug("exif_data for %s is not JSON", item.path, exc_info=True)
        return details

    def _gallery_stats(self, _args: dict) -> dict:
        include_nc = self._include_nc()
        per_category = {
            key: self.database.count_media(
                key,
                include_nc=(key == _ALL and include_nc),
                media_filter=self.settings.media_filter_for(key),
            )
            for key, _label, _path in self.settings.categories()
        }
        with self.database.lock:
            row = self.database.conn.execute(
                """
                SELECT
                    COUNT(*) AS files,
                    COALESCE(SUM(size), 0) AS bytes,
                    COALESCE(SUM(media_type = 'image'), 0) AS images,
                    COALESCE(SUM(media_type = 'video'), 0) AS videos
                FROM media
                WHERE id IN (SELECT MIN(id) FROM media GROUP BY path)
                """,
            ).fetchone()
        return {
            "files": row["files"],
            "images": row["images"],
            "videos": row["videos"],
            "size_bytes": row["bytes"],
            "per_category": per_category,
        }


# ---------------------------------------------------------------------------
# JSON-RPC / MCP layer
# ---------------------------------------------------------------------------

class _Protocol:
    """Turns a decoded JSON-RPC message into a reply (or None for a
    notification, which the transport answers with a bare 202)."""

    def __init__(self, tools: GalleryTools) -> None:
        self.tools = tools

    def handle(self, message: Any) -> Any:
        if isinstance(message, list):
            # Batching left the spec in 2025-06-18, but answering one anyway
            # costs three lines and keeps older clients working.
            replies = [r for r in (self.handle(m) for m in message) if r is not None]
            return replies or None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, _INVALID_REQUEST, "Not a JSON-RPC 2.0 request")

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, _INVALID_PARAMS, "params must be an object")

        # No id → a notification. Nothing we implement needs to act on one,
        # and the spec forbids replying to them.
        if request_id is None:
            return None

        try:
            if method == "initialize":
                return _result(request_id, self._initialize(params))
            if method == "ping":
                return _result(request_id, {})
            if method == "tools/list":
                return _result(request_id, {"tools": self.tools.descriptors()})
            if method == "tools/call":
                return _result(request_id, self._call_tool(params))
            if method in ("resources/list", "prompts/list"):
                # Declared as absent in initialize, but some clients probe
                # regardless; an empty list is friendlier than an error.
                key = "resources" if method.startswith("resources") else "prompts"
                return _result(request_id, {key: []})
            return _error(request_id, _METHOD_NOT_FOUND, f"Unknown method: {method}")
        except Exception:
            LOGGER.exception("MCP method %s failed", method)
            return _error(request_id, _INTERNAL_ERROR, "Internal error")

    def _initialize(self, params: dict) -> dict:
        requested = str(params.get("protocolVersion") or "")
        return {
            # Echo the client's version when we can speak it, otherwise state
            # ours and let the client decide whether to continue.
            "protocolVersion": requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": APP_ID, "title": "Muga Gallery", "version": VERSION},
            "instructions": (
                "Read-only access to the Muga photo and video index on this "
                "device. Call list_categories first to learn the category keys, "
                "then search_media or list_media. Paths are absolute and point "
                "at real files; nothing here can modify them."
            ),
        }

    def _call_tool(self, params: dict) -> dict:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            payload = self.tools.call(name, arguments)
        except PermissionError:
            return _tool_error(
                f"{name} is unavailable: write access is switched off in "
                "Muga's settings (Settings \u2192 MCP \u2192 Allow write access). "
                "Only the person at the device can turn it on."
            )
        except LookupError:
            # A tool error is reported inside the result, not as a JSON-RPC
            # error — that is what lets the model see it and retry.
            return _tool_error(f"Unknown tool: {name}")
        except ValueError as exc:
            return _tool_error(str(exc))
        except Exception:
            LOGGER.exception("MCP tool %s failed", name)
            return _tool_error(f"{name} failed — see the Muga log for details.")
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": False,
        }


def _result(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so clients may keep the connection open between calls. The
    # server is threaded, so one idle keep-alive connection does not stall the
    # others — and every response carries an explicit Content-Length, which
    # HTTP/1.1 requires for the connection to stay usable.
    protocol_version = "HTTP/1.1"
    server_version = f"Muga/{VERSION}"
    # Drop an idle connection after 30 s. Each one holds a worker thread for as
    # long as it stays open, and opening a connection needs no token — without
    # a timeout, a handful of sockets that connect and then say nothing would
    # tie up the pool. BaseHTTPRequestHandler turns the resulting socket
    # timeout into a closed connection, not an error.
    timeout = 30
    # Drop the Python version from the Server header — it tells an unauthorised
    # caller which interpreter to look up advisories for and nothing useful.
    sys_version = ""

    # Set by MCPServer before the socket is served.
    server: "_HTTPServer"  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:
        """Route the access log into Muga's logger instead of stderr, at debug
        level. The default writes one line per request straight to the
        terminal, which on a phone goes nowhere and on a desktop buries the
        app's own output."""
        LOGGER.debug("mcp %s - %s", self.address_string(), fmt % args)

    # -- verbs ----------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if not self._check_origin():
            return
        if not self._check_path():
            return
        token = self._authenticate()
        if token is None:
            return
        body = self._read_body()
        if body is None:
            return
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, _error(None, _PARSE_ERROR, "Invalid JSON"))
            return
        reply = self.server.protocol.handle(message)
        if reply is None:
            # A notification (or a batch of them) — acknowledged, no body.
            self._send_bytes(202, b"", "text/plain")
            return
        self._send_json(200, reply)

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_origin():
            return
        if not self._check_path():
            return
        if self._authenticate() is None:
            return
        # No server-initiated stream to offer. 405 is the transport's
        # sanctioned way of saying so, and stops the client from hanging on an
        # SSE connection that will never produce an event.
        self._send_json(405, _error(None, _INVALID_REQUEST, "This server does not stream"))

    def do_DELETE(self) -> None:  # noqa: N802
        # Session termination. State lives entirely in the request, so there
        # is nothing to tear down — but answering keeps well-behaved clients
        # from logging a failure on disconnect.
        if not self._check_origin():
            return
        if not self._check_path():
            return
        if self._authenticate() is None:
            return
        self._send_bytes(200, b"", "text/plain")

    # -- plumbing -------------------------------------------------------

    def _check_origin(self) -> bool:
        """Refuse a cross-origin browser request before anything else runs.

        Ahead of the path and the token deliberately: a rebound page must not
        be able to tell a live MCP port from a closed one by which error it
        gets back, and a token comparison is not something an unvetted origin
        should be able to reach at all. See :func:`origin_is_allowed`.
        """
        origin = self.headers.get("Origin", "")
        if origin_is_allowed(origin):
            return True
        LOGGER.warning(
            "MCP request from %s rejected: cross-origin request from %r",
            self.address_string(), origin,
        )
        self._send_json(
            403, _error(None, _INVALID_REQUEST, "Cross-origin requests are not accepted"),
        )
        return False

    def _check_path(self) -> bool:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == MCP_PATH:
            return True
        self._send_json(404, _error(None, _INVALID_REQUEST, f"Not found — the endpoint is {MCP_PATH}"))
        return False

    def _authenticate(self):
        """Return the matching token, or None after having sent a 401."""
        header = self.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        presented = value.strip() if scheme.lower() == "bearer" else ""
        token = self.server.tokens.verify(presented)
        if token is None:
            # Never log the presented value: a mistyped token is still a
            # secret, and this line would put it in the debug log verbatim.
            LOGGER.info("MCP request from %s rejected: invalid token", self.address_string())
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="Muga MCP"')
            payload = json.dumps(
                _error(None, _INVALID_REQUEST, "A valid bearer token is required"),
            ).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return None
        return token

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._send_json(400, _error(None, _INVALID_REQUEST, "Missing or invalid Content-Length"))
            return None
        if length > _MAX_BODY:
            self._send_json(413, _error(None, _INVALID_REQUEST, "Request body too large"))
            return None
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, payload: Any) -> None:
        self._send_bytes(
            status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json",
        )

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The client hung up mid-response. Nothing to recover, and the
            # default handler would print a traceback for it.
            LOGGER.debug("MCP client disconnected before the response was sent")


class _HTTPServer(ThreadingHTTPServer):
    # Worker threads must not keep the process alive: the app quits from the
    # GTK main loop, and a non-daemon request thread parked on a keep-alive
    # read would hold the interpreter open after the last window closed.
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, tokens: TokenStore, protocol: _Protocol) -> None:
        self.tokens = tokens
        self.protocol = protocol
        super().__init__(address, _Handler)

    def process_request_thread(self, request, client_address) -> None:
        """Run the request, then drop this thread's SQLite handle.

        Every worker thread that reads the index opens its own connection (see
        Database.conn) and the thread is discarded right after. Without this,
        each MCP call leaks a file descriptor plus a 16 MB page-cache
        allocation until the garbage collector happens to reclaim it.
        """
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.protocol.tools.database.close_thread_connection()

    def handle_error(self, request, client_address) -> None:
        LOGGER.debug("MCP connection from %s failed", client_address, exc_info=True)


class MCPServer:
    """Owns the socket and the thread that serves it."""

    def __init__(self, database: "Database", settings: "Settings",
                 tokens_path: Path = TOKENS_PATH,
                 on_change: Callable[[], None] | None = None) -> None:
        self.tools = GalleryTools(database, settings, on_change=on_change)
        # Where the tokens are read from. A parameter rather than the module
        # constant so a test can point one server at its own file without
        # monkeypatching the loader out from under the rest of the process.
        self.tokens_path = tokens_path
        self.tokens = TokenStore.load(tokens_path)
        self._protocol = _Protocol(self.tools)
        self._httpd: _HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port = 0
        # The scope the live socket was opened for, and the address a client
        # should dial to reach it. Both are fixed at bind time: re-deriving
        # them per call would make the address in Settings drift away from
        # what the socket is actually listening on.
        self._scope = DEFAULT_BIND
        self._advertised = LOOPBACK
        # Set when start() failed, so Settings can say why instead of showing
        # a switch that flips back with no explanation.
        self.last_error: str = ""

    # -- state ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        return self._port

    @property
    def scope(self) -> str:
        """The bind scope the live socket was opened for."""
        return self._scope

    def url(self) -> str:
        """The address to hand to a client, or "" while stopped."""
        if not self.running:
            return ""
        return f"http://{self._advertised}:{self._port}{MCP_PATH}"

    def reload_tokens(self) -> None:
        """Pick up token changes made in Settings without a restart. The
        running server reads self.tokens on every request, so replacing the
        object is enough."""
        self.tokens = TokenStore.load(self.tokens_path)
        if self._httpd is not None:
            self._httpd.tokens = self.tokens

    def rebind_sources(self, database: "Database", settings: "Settings",
                       on_change: Callable[[], None] | None = None) -> None:
        """Point the tools at a different database/settings pair — used when a
        layout change replaces GalleryWindow while the server keeps running.

        The change callback goes with them: it closes over the old window, and
        calling it after that window is gone would refresh a widget tree that
        no longer exists.
        """
        self.tools.database = database
        self.tools.settings = settings
        if on_change is not None:
            self.tools.on_change = on_change

    # -- lifecycle ------------------------------------------------------

    def start(self, port: int, scope: str = DEFAULT_BIND) -> bool:
        """Bind and serve. Returns False and fills last_error on failure."""
        scope = scope if scope in BIND_SCOPES else DEFAULT_BIND
        if self.running:
            if port == self._port and scope == self._scope:
                return True
            # A changed scope means a different socket — there is no way to
            # widen or narrow a listening one in place.
            self.stop()
        self.reload_tokens()
        if not self.tokens.tokens:
            self.last_error = "no-tokens"
            LOGGER.warning("Refusing to start the MCP server without any access token")
            return False
        bind_address, advertised = resolve_bind(scope)
        try:
            httpd = _HTTPServer((bind_address, int(port)), self.tokens, self._protocol)
        except OSError as exc:
            self.last_error = "port-in-use" if exc.errno in (98, 13) else "bind-failed"
            LOGGER.warning("Could not bind the MCP server to %s:%s: %s", bind_address, port, exc)
            return False
        self._httpd = httpd
        self._port = httpd.server_address[1]
        self._scope = scope
        self._advertised = advertised
        self.last_error = ""
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="muga-mcp", daemon=True,
        )
        self._thread.start()
        LOGGER.info(
            "MCP server listening on %s:%d%s (scope %s)",
            bind_address, self._port, MCP_PATH, scope,
        )
        return True

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        self._port = 0
        self._advertised = LOOPBACK
        if httpd is None:
            return
        try:
            # shutdown() must not run on the serving thread — it waits for
            # serve_forever to return, which would deadlock.
            httpd.shutdown()
        except Exception:
            LOGGER.debug("MCP shutdown failed", exc_info=True)
        try:
            httpd.server_close()
        except Exception:
            LOGGER.debug("MCP server_close failed", exc_info=True)
        if thread is not None:
            thread.join(timeout=2)
        LOGGER.info("MCP server stopped")


# ---------------------------------------------------------------------------
# Process-wide instance
# ---------------------------------------------------------------------------

_instance: MCPServer | None = None
_instance_lock = threading.Lock()


def instance() -> MCPServer | None:
    """The shared server, or None if one was never created."""
    return _instance


def sync_with_settings(settings: "Settings", database: "Database",
                       on_change: Callable[[], None] | None = None) -> MCPServer:
    """Bring the shared server in line with *settings*.

    Idempotent: safe to call at startup, after every settings change, and
    after a window rebuild. Returns the server so the caller can read
    ``running``/``last_error`` to report what actually happened.
    """
    global _instance
    with _instance_lock:
        server = _instance
        if server is None:
            server = MCPServer(database, settings, on_change=on_change)
            _instance = server
        else:
            server.rebind_sources(database, settings, on_change)
        if getattr(settings, "mcp_enabled", False):
            server.start(
                getattr(settings, "mcp_port", DEFAULT_MCP_PORT),
                getattr(settings, "mcp_bind", DEFAULT_BIND),
            )
        elif server.running:
            server.stop()
        return server


def shutdown() -> None:
    """Stop the shared server, if any. Called when the app quits."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.stop()
