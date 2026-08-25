"""A read-only MCP server over the media index.

Muga already knows where every photo and video on the device is, what folder
it sits in, when it was taken and what its EXIF says. This exposes that to an
MCP client ("find the videos I shot last August", "how many screenshots are
in the index") without giving anything on the network a way to modify, move
or delete a file: every tool here is a SELECT.

Transport is MCP's Streamable HTTP on ``POST /mcp``. Responses are plain
``application/json`` — the server never needs to push, so it does not open an
SSE stream, which the transport explicitly permits. ``GET /mcp`` therefore
answers 405, telling a client not to wait for one.

Authentication is a bearer token from :mod:`muga.mcp_tokens`, and a token has
to exist before the server will start at all — without one there is nothing a
client could authenticate with, and a listening socket that refuses every
request is only a way to be wrong later.

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

    def __init__(self, database: "Database", settings: "Settings") -> None:
        self.database = database
        self.settings = settings

    # -- schema ---------------------------------------------------------

    def descriptors(self) -> list[dict]:
        """The ``tools/list`` payload."""
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

    def call(self, name: str, arguments: dict) -> Any:
        handler: Callable[[dict], Any] | None = {
            "list_categories": self._list_categories,
            "search_media": self._search_media,
            "list_media": self._list_media,
            "list_folders": self._list_folders,
            "get_media": self._get_media,
            "gallery_stats": self._gallery_stats,
        }.get(name)
        if handler is None:
            raise LookupError(name)
        return handler(arguments or {})

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

        return {
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
        if not self._check_path():
            return
        if self._authenticate() is None:
            return
        self._send_bytes(200, b"", "text/plain")

    # -- plumbing -------------------------------------------------------

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
                 tokens_path: Path = TOKENS_PATH) -> None:
        self.tools = GalleryTools(database, settings)
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

    def rebind_sources(self, database: "Database", settings: "Settings") -> None:
        """Point the tools at a different database/settings pair — used when a
        layout change replaces GalleryWindow while the server keeps running."""
        self.tools.database = database
        self.tools.settings = settings

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


def sync_with_settings(settings: "Settings", database: "Database") -> MCPServer:
    """Bring the shared server in line with *settings*.

    Idempotent: safe to call at startup, after every settings change, and
    after a window rebuild. Returns the server so the caller can read
    ``running``/``last_error`` to report what actually happened.
    """
    global _instance
    with _instance_lock:
        server = _instance
        if server is None:
            server = MCPServer(database, settings)
            _instance = server
        else:
            server.rebind_sources(database, settings)
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
