"""Tests for the built-in MCP server.

Three layers, each worth covering for a different reason:

* the token store, because it is the only thing standing between the network
  and the media index — a lost 0600, a rename that silently drops the secret,
  or a `==` comparison would all be invisible in the UI;
* the protocol, because a malformed reply shows up as "the client just doesn't
  see any tools", with nothing in the log;
* the transport end to end over a real socket, because the auth check, the
  JSON-RPC layer and the HTTP verbs only meet there.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from muga.config import Settings
from muga.database import Database
from muga.mcp_tokens import Token, TokenStore
from muga import mcp_server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path) -> TokenStore:
    return TokenStore.load(tmp_path / "mcp_tokens.json")


@pytest.fixture
def indexed(tmp_path):
    """A database with a handful of real files indexed, plus matching settings."""
    photos = tmp_path / "Photos"
    photos.mkdir()
    db = Database(tmp_path / "index.sqlite3")
    for i in range(4):
        f = photos / f"IMG_{i}.jpg"
        f.write_bytes(b"x" * (10 + i))
        db.upsert_media(path=f, category="photos", media_type="image",
                        folder=str(photos), thumb_path=None)
    clip = photos / "holiday.mp4"
    clip.write_bytes(b"v" * 40)
    db.upsert_media(path=clip, category="videos", media_type="video",
                    folder=str(photos), thumb_path=None)
    db.commit()

    settings = Settings()
    settings.photos_dir = str(photos)
    settings.videos_dir = str(photos)
    settings.pictures_hidden = False
    settings.screenshots_dir = ""
    return db, settings, photos


@pytest.fixture
def tools(indexed):
    db, settings, _photos = indexed
    return mcp_server.GalleryTools(db, settings)


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------

def test_a_new_token_is_generated_prefixed_and_persisted(store) -> None:
    token = store.add("Laptop")
    assert token.name == "Laptop"
    assert token.token.startswith("muga_")
    # 32 bytes of entropy, URL-safe encoded.
    assert len(token.token) > 40
    assert TokenStore.load(store.path).tokens[0].token == token.token


def test_the_token_file_is_not_readable_by_anyone_else(store) -> None:
    store.add("Laptop")
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_two_tokens_never_collide(store) -> None:
    values = {store.add(f"client {i}").token for i in range(20)}
    ids = {t.id for t in store.tokens}
    assert len(values) == 20
    assert len(ids) == 20


def test_an_unnamed_token_still_gets_a_name(store) -> None:
    assert store.add("   ").name == "Token"


def test_rename_keeps_the_secret(store) -> None:
    token = store.add("Old")
    assert store.rename(token.id, "New")
    reloaded = TokenStore.load(store.path).tokens[0]
    assert reloaded.name == "New"
    assert reloaded.token == token.token


def test_rename_to_blank_is_ignored(store) -> None:
    token = store.add("Keep me")
    store.rename(token.id, "   ")
    assert TokenStore.load(store.path).tokens[0].name == "Keep me"


def test_remove_deletes_only_the_named_token(store) -> None:
    a, b = store.add("A"), store.add("B")
    assert store.remove(a.id)
    remaining = TokenStore.load(store.path).tokens
    assert [t.id for t in remaining] == [b.id]
    # Removing something that is already gone reports it rather than raising.
    assert not store.remove(a.id)


def test_verify_matches_only_the_exact_secret(store) -> None:
    token = store.add("Laptop")
    assert store.verify(token.token) is not None
    assert store.verify(token.token[:-1]) is None
    assert store.verify(token.token + "x") is None
    assert store.verify("") is None
    assert store.verify("muga_" + "a" * 43) is None


def test_a_damaged_token_file_degrades_to_an_empty_store(tmp_path) -> None:
    """An unreadable file must not stop the MCP page from opening — the user
    can always add a fresh token."""
    path = tmp_path / "mcp_tokens.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert TokenStore.load(path).tokens == []


@pytest.mark.parametrize("payload", ['[]', '{"tokens": "nope"}', 'null', '{}'])
def test_unexpected_json_shapes_yield_no_tokens(tmp_path, payload) -> None:
    path = tmp_path / "mcp_tokens.json"
    path.write_text(payload, encoding="utf-8")
    assert TokenStore.load(path).tokens == []


def test_an_entry_without_a_secret_is_dropped(tmp_path) -> None:
    """It could never authenticate anything, so keeping it would only put a
    phantom row in Settings."""
    path = tmp_path / "mcp_tokens.json"
    path.write_text(json.dumps({"tokens": [
        {"id": "1", "name": "ghost"},
        {"id": "2", "name": "real", "token": "muga_abc"},
    ]}), encoding="utf-8")
    assert [t.name for t in TokenStore.load(path).tokens] == ["real"]


def test_masking_hides_the_middle_of_the_secret() -> None:
    token = Token(id="1", name="n", token="muga_" + "abcdefgh" * 4)
    masked = token.masked()
    assert masked.startswith("muga_abcd")
    assert masked.endswith("efgh")
    assert token.token not in masked


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def test_every_advertised_tool_is_callable(tools) -> None:
    """A descriptor without an implementation is a tool the client can see and
    never use — the failure only shows up as an error at call time."""
    for descriptor in tools.descriptors():
        assert descriptor["inputSchema"]["type"] == "object"
        required = descriptor["inputSchema"].get("required", [])
        args = {name: "" for name in required}
        if "path" in args:
            args["path"] = "/nonexistent"
        tools.call(descriptor["name"], args)


def test_an_unknown_tool_raises_lookup_error(tools) -> None:
    with pytest.raises(LookupError):
        tools.call("delete_everything", {})


def test_list_categories_counts_what_the_gallery_would_show(tools) -> None:
    by_key = {c["key"]: c for c in tools.call("list_categories", {})["categories"]}
    assert by_key["photos"]["count"] == 4
    assert by_key["videos"]["count"] == 1


def test_all_covers_images_and_videos_together(tools) -> None:
    result = tools.call("list_media", {"category": "all"})
    assert result["total"] == 5
    assert {i["type"] for i in result["items"]} == {"image", "video"}


def test_a_named_category_keeps_its_own_media_filter(tools) -> None:
    """Photos is an image category — asking for it must not pull in the video
    that shares the folder."""
    result = tools.call("list_media", {"category": "photos"})
    assert {i["type"] for i in result["items"]} == {"image"}


def test_paging_returns_a_window_and_the_full_total(tools) -> None:
    first = tools.call("list_media", {"category": "all", "limit": 2, "offset": 0})
    second = tools.call("list_media", {"category": "all", "limit": 2, "offset": 2})
    assert first["total"] == second["total"] == 5
    assert len(first["items"]) == len(second["items"]) == 2
    assert {i["path"] for i in first["items"]}.isdisjoint({i["path"] for i in second["items"]})


def test_a_limit_beyond_the_ceiling_is_clamped(tools) -> None:
    """Otherwise one call could serialise a six-figure library into a single
    JSON response."""
    schema = next(d for d in tools.descriptors() if d["name"] == "list_media")
    assert schema["inputSchema"]["properties"]["limit"]["maximum"] == mcp_server._MAX_ITEMS
    result = tools.call("list_media", {"category": "all", "limit": 10_000})
    assert len(result["items"]) <= mcp_server._MAX_ITEMS


@pytest.mark.parametrize("bad", ["nope", None, -5, 0])
def test_a_nonsense_limit_falls_back_to_the_default(tools, bad) -> None:
    assert tools.call("list_media", {"category": "all", "limit": bad})["total"] == 5


def test_search_matches_a_filename(tools) -> None:
    result = tools.call("search_media", {"query": "holiday"})
    assert result["total"] == 1
    assert result["items"][0]["name"] == "holiday.mp4"


def test_an_empty_search_returns_everything(tools) -> None:
    assert tools.call("search_media", {"query": ""})["total"] == 5


def test_get_media_reports_a_path_it_does_not_know(tools) -> None:
    assert tools.call("get_media", {"path": "/no/such/file.jpg"})["found"] is False


def test_get_media_returns_the_indexed_facts(tools, indexed) -> None:
    _db, _settings, photos = indexed
    detail = tools.call("get_media", {"path": str(photos / "holiday.mp4")})
    assert detail["found"] is True
    assert detail["type"] == "video"
    assert detail["size_bytes"] == 40
    # ISO 8601, so a client can parse it without knowing our epoch convention.
    assert detail["modified"].count("-") >= 2 and "T" in detail["modified"]


def test_get_media_requires_a_path(tools) -> None:
    with pytest.raises(ValueError):
        tools.call("get_media", {"path": "   "})


def test_stats_count_each_file_once(tools) -> None:
    stats = tools.call("gallery_stats", {})
    assert stats["files"] == 5
    assert stats["images"] == 4
    assert stats["videos"] == 1
    assert stats["size_bytes"] > 0


def test_nextcloud_items_stay_out_unless_the_gallery_shows_them(indexed) -> None:
    """MCP must not surface what a disabled connection has hidden from the UI."""
    db, settings, _photos = indexed
    db.upsert_remote_media(path="nc://remote/pic.jpg", category="nextcloud",
                           media_type="image", folder="Photos", name="pic.jpg",
                           mtime=1.0, size=1, thumb_path=None)
    db.commit()
    tools = mcp_server.GalleryTools(db, settings)

    settings.nextcloud_enabled = False
    settings.nextcloud_show_in_pictures = True
    assert tools.call("list_media", {"category": "all"})["total"] == 5

    settings.nextcloud_enabled = True
    assert tools.call("list_media", {"category": "all"})["total"] == 6


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@pytest.fixture
def protocol(tools):
    return mcp_server._Protocol(tools)


def test_initialize_answers_with_a_protocol_version_and_tool_capability(protocol) -> None:
    reply = protocol.handle({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": mcp_server.PROTOCOL_VERSION, "capabilities": {}},
    })
    result = reply["result"]
    assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"]


def test_initialize_states_our_version_when_the_client_asks_for_another(protocol) -> None:
    reply = protocol.handle({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "1999-01-01"},
    })
    assert reply["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_a_notification_gets_no_reply(protocol) -> None:
    """Replying to one is a protocol violation, and some clients drop the
    connection over it."""
    assert protocol.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_a_non_jsonrpc_message_is_rejected(protocol) -> None:
    assert protocol.handle({"hello": "there"})["error"]["code"] == -32600
    assert protocol.handle("just a string")["error"]["code"] == -32600


def test_an_unknown_method_is_a_jsonrpc_error(protocol) -> None:
    reply = protocol.handle({"jsonrpc": "2.0", "id": 3, "method": "files/delete"})
    assert reply["error"]["code"] == -32601


def test_non_object_params_are_rejected(protocol) -> None:
    reply = protocol.handle({"jsonrpc": "2.0", "id": 4, "method": "ping", "params": [1, 2]})
    assert reply["error"]["code"] == -32602


def test_tools_list_carries_a_schema_for_every_tool(protocol) -> None:
    tools = protocol.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})["result"]["tools"]
    assert tools
    for tool in tools:
        assert tool["name"] and tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_a_tool_call_returns_both_text_and_structured_content(protocol) -> None:
    reply = protocol.handle({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "gallery_stats", "arguments": {}},
    })
    result = reply["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_an_unknown_tool_is_reported_inside_the_result(protocol) -> None:
    """As a tool error, not a JSON-RPC one — that is what lets the model see
    it and pick a different tool."""
    reply = protocol.handle({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "rm_rf", "arguments": {}},
    })
    assert reply["result"]["isError"] is True
    assert "error" not in reply


def test_a_failing_tool_does_not_leak_a_traceback(protocol, tools) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("secret internal detail")

    tools.database.count_media = _boom
    reply = protocol.handle({
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "list_categories", "arguments": {}},
    })
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is True
    assert "secret internal detail" not in text


def test_resource_and_prompt_probes_get_an_empty_list(protocol) -> None:
    assert protocol.handle(
        {"jsonrpc": "2.0", "id": 9, "method": "resources/list"},
    )["result"]["resources"] == []
    assert protocol.handle(
        {"jsonrpc": "2.0", "id": 10, "method": "prompts/list"},
    )["result"]["prompts"] == []


# ---------------------------------------------------------------------------
# Transport, over a real socket
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(indexed, tmp_path):
    """A running server, its token, and a helper that speaks to it."""
    db, settings, _photos = indexed
    settings.mcp_port = _free_port()
    settings.mcp_enabled = True

    # Its own token file: start() re-reads from disk, so a server pointed at
    # the shared default would drop whatever the test just put in place.
    server = mcp_server.MCPServer(db, settings, tokens_path=tmp_path / "mcp_tokens.json")
    token = server.tokens.add("test client")
    assert server.start(settings.mcp_port), server.last_error
    try:
        yield server, token
    finally:
        server.stop()


def _post(server, payload, token=None, path="/mcp", expect=200, origin=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
    )
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if origin is not None:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == expect
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        assert exc.code == expect, (exc.code, expect)
        body = exc.read()
        return json.loads(body) if body else None


def test_a_request_without_a_token_is_refused(live) -> None:
    server, _token = live
    _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, expect=401)


def test_a_wrong_token_is_refused(live) -> None:
    server, _token = live
    _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
          token="muga_not_the_one", expect=401)


def test_a_valid_token_gets_through(live) -> None:
    server, token = live
    reply = _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=token.token)
    assert reply["result"] == {}


def test_a_full_tool_call_round_trip(live) -> None:
    server, token = live
    reply = _post(server, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "search_media", "arguments": {"query": "holiday"}},
    }, token=token.token)
    assert reply["result"]["structuredContent"]["total"] == 1


def test_a_notification_is_acknowledged_with_no_body(live) -> None:
    server, token = live
    assert _post(server, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 token=token.token, expect=202) is None


def test_malformed_json_is_a_parse_error(live) -> None:
    server, token = live
    url = f"http://127.0.0.1:{server.port}/mcp"
    request = urllib.request.Request(url, data=b"{{{", method="POST")
    request.add_header("Authorization", f"Bearer {token.token}")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 400
    assert json.loads(exc.value.read())["error"]["code"] == -32700


def test_another_path_is_a_404(live) -> None:
    server, token = live
    _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
          token=token.token, path="/admin", expect=404)


def test_get_says_there_is_no_stream(live) -> None:
    """405 rather than an open SSE connection the server will never write to."""
    server, token = live
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}/mcp", method="GET")
    request.add_header("Authorization", f"Bearer {token.token}")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 405


def test_an_oversized_body_is_rejected(live) -> None:
    server, token = live
    url = f"http://127.0.0.1:{server.port}/mcp"
    request = urllib.request.Request(
        url, data=b"x" * (mcp_server._MAX_BODY + 1), method="POST",
    )
    request.add_header("Authorization", f"Bearer {token.token}")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 413


def test_revoking_a_token_takes_effect_without_a_restart(live) -> None:
    server, token = live
    server.tokens.remove(token.id)
    server.reload_tokens()
    _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
          token=token.token, expect=401)


def test_the_url_names_the_live_port_and_the_mcp_path(live) -> None:
    server, _token = live
    url = server.url()
    assert url.endswith(f":{server.port}/mcp")
    assert server.running


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_the_server_refuses_to_start_without_a_token(indexed, tmp_path) -> None:
    """It binds to every interface, so an unauthenticated start would expose
    the index to anything that can reach the port."""
    db, settings, _photos = indexed
    server = mcp_server.MCPServer(db, settings, tokens_path=tmp_path / "empty.json")
    assert not server.start(_free_port())
    assert server.last_error == "no-tokens"
    assert not server.running
    assert server.url() == ""


def test_a_port_already_in_use_fails_with_a_reason(indexed, tmp_path) -> None:
    db, settings, _photos = indexed
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]

        server = mcp_server.MCPServer(db, settings, tokens_path=tmp_path / "mcp_tokens.json")
        server.tokens.add("client")
        assert not server.start(port)
        assert server.last_error == "port-in-use"


def test_stop_is_safe_to_call_on_a_server_that_never_started(indexed) -> None:
    db, settings, _photos = indexed
    mcp_server.MCPServer(db, settings).stop()


def test_sync_starts_stops_and_rebinds_the_shared_instance(indexed, tmp_path, monkeypatch) -> None:
    db, settings, _photos = indexed
    monkeypatch.setattr(mcp_server, "_instance", None)
    # sync_with_settings builds the server itself, so this seeds the default
    # token file — conftest already points the XDG roots at a sandbox.
    default_store = TokenStore.load()
    default_store.add("client")

    settings.mcp_enabled = False
    server = mcp_server.sync_with_settings(settings, db)
    assert not server.running

    settings.mcp_enabled = True
    settings.mcp_port = _free_port()
    assert mcp_server.sync_with_settings(settings, db) is server
    assert server.running

    # A window rebuild hands over a new database without releasing the port.
    other = Database(tmp_path / "other.sqlite3")
    mcp_server.sync_with_settings(settings, other)
    assert server.tools.database is other
    assert server.running

    settings.mcp_enabled = False
    mcp_server.sync_with_settings(settings, db)
    assert not server.running

    mcp_server.shutdown()
    default_store.tokens = []
    default_store.save()


# ---------------------------------------------------------------------------
# Bind scope
# ---------------------------------------------------------------------------

def test_every_scope_resolves_to_a_dialable_address() -> None:
    for scope in mcp_server.BIND_SCOPES:
        bind, advertised = mcp_server.resolve_bind(scope)
        socket.inet_aton(bind)
        socket.inet_aton(advertised)
        # 0.0.0.0 is a wildcard to bind, never something to hand to a client.
        assert advertised != mcp_server.ANY_ADDRESS


def test_local_binds_loopback_and_all_binds_the_wildcard() -> None:
    assert mcp_server.resolve_bind("local") == ("127.0.0.1", "127.0.0.1")
    assert mcp_server.resolve_bind("all")[0] == "0.0.0.0"


def test_an_unknown_scope_falls_back_to_loopback() -> None:
    """Falling inward is the only safe direction to guess in — a typo in
    settings.json must not open the index to the network."""
    assert mcp_server.resolve_bind("everywhere")[0] == "127.0.0.1"
    assert mcp_server.resolve_bind("")[0] == "127.0.0.1"


def test_a_scope_whose_interface_is_gone_falls_back_to_loopback(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "available_addresses", lambda: {
        "local": "127.0.0.1", "lan": "", "public": "", "all": "0.0.0.0",
    })
    assert mcp_server.resolve_bind("lan") == ("127.0.0.1", "127.0.0.1")
    # With no real address anywhere, "all" still binds the wildcard but can
    # only advertise loopback.
    assert mcp_server.resolve_bind("all") == ("0.0.0.0", "127.0.0.1")


def test_available_addresses_covers_every_scope() -> None:
    addresses = mcp_server.available_addresses()
    assert set(addresses) == set(mcp_server.BIND_SCOPES)
    assert addresses["local"] == "127.0.0.1"
    assert addresses["all"] == "0.0.0.0"
    for scope in ("lan", "public"):
        if addresses[scope]:
            socket.inet_aton(addresses[scope])


def test_interface_addresses_include_loopback() -> None:
    """The ioctl sweep has to find at least lo, or the whole listing is
    broken and Settings would offer nothing but the wildcard."""
    assert "127.0.0.1" in mcp_server._interface_addresses()


def test_the_default_scope_is_the_narrowest_one() -> None:
    assert Settings().mcp_bind == mcp_server.BIND_LOCAL == "local"


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------

def test_a_fresh_profile_has_the_server_disabled() -> None:
    assert Settings().mcp_enabled is False


def test_a_settings_file_that_never_mentions_mcp_leaves_it_off(tmp_path, monkeypatch) -> None:
    """An install that predates this feature must not gain a listening socket
    on upgrade."""
    import muga.config as config

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"grid_columns": 5, "theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    loaded = config.Settings.load()
    assert loaded.mcp_enabled is False
    assert loaded.mcp_bind == "local"


def test_sync_opens_no_socket_while_disabled(indexed, tmp_path, monkeypatch) -> None:
    """Even with tokens on file and a port configured: disabled means no
    socket, not a socket that refuses."""
    db, settings, _photos = indexed
    monkeypatch.setattr(mcp_server, "_instance", None)
    store = TokenStore.load()
    store.add("client")
    try:
        settings.mcp_port = _free_port()
        assert settings.mcp_enabled is False
        server = mcp_server.sync_with_settings(settings, db)
        assert not server.running
        assert server.url() == ""
        with socket.socket() as probe:
            probe.settimeout(2)
            with pytest.raises(OSError):
                probe.connect(("127.0.0.1", settings.mcp_port))
    finally:
        mcp_server.shutdown()
        store.tokens = []
        store.save()


def test_the_default_port_is_free_on_a_default_profile(indexed, monkeypatch) -> None:
    """The whole default path end to end: default settings through
    sync_with_settings must leave DEFAULT_MCP_PORT untouched."""
    db, _settings, _photos = indexed
    monkeypatch.setattr(mcp_server, "_instance", None)
    fresh = Settings()
    server = mcp_server.sync_with_settings(fresh, db)
    try:
        assert not server.running
    finally:
        mcp_server.shutdown()


def test_a_local_server_is_not_reachable_from_the_lan(indexed, tmp_path) -> None:
    """The point of the default: the socket exists, but only on loopback."""
    db, settings, _photos = indexed
    lan = mcp_server.available_addresses()["lan"]
    if not lan:
        pytest.skip("no LAN address on this machine to test against")

    server = mcp_server.MCPServer(db, settings, tokens_path=tmp_path / "t.json")
    server.tokens.add("client")
    port = _free_port()
    assert server.start(port, "local")
    try:
        assert server.url().startswith("http://127.0.0.1:")
        with socket.socket() as probe:
            probe.settimeout(3)
            with pytest.raises(OSError):
                probe.connect((lan, port))
    finally:
        server.stop()


def test_widening_the_scope_rebinds_the_socket(indexed, tmp_path) -> None:
    db, settings, _photos = indexed
    lan = mcp_server.available_addresses()["lan"]
    if not lan:
        pytest.skip("no LAN address on this machine to test against")

    server = mcp_server.MCPServer(db, settings, tokens_path=tmp_path / "t.json")
    token = server.tokens.add("client")
    port = _free_port()
    assert server.start(port, "local")
    try:
        assert server.scope == "local"
        assert server.start(port, "all")
        assert server.scope == "all"
        # Now the LAN address answers, and the advertised URL names it.
        assert lan in server.url()
        request = urllib.request.Request(
            f"http://{lan}:{server.port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(),
            method="POST",
        )
        request.add_header("Authorization", f"Bearer {token.token}")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.loads(response.read())["result"] == {}
    finally:
        server.stop()


def test_restarting_with_the_same_port_and_scope_is_a_no_op(indexed, tmp_path) -> None:
    db, settings, _photos = indexed
    server = mcp_server.MCPServer(db, settings, tokens_path=tmp_path / "t.json")
    server.tokens.add("client")
    port = _free_port()
    assert server.start(port, "local")
    try:
        assert server.start(port, "local")
        assert server.port == port
    finally:
        server.stop()


def test_sync_passes_the_saved_scope_through(indexed, tmp_path, monkeypatch) -> None:
    db, settings, _photos = indexed
    monkeypatch.setattr(mcp_server, "_instance", None)
    default_store = TokenStore.load()
    default_store.add("client")

    settings.mcp_enabled = True
    settings.mcp_port = _free_port()
    settings.mcp_bind = "local"
    server = mcp_server.sync_with_settings(settings, db)
    assert server.running and server.scope == "local"

    mcp_server.shutdown()
    default_store.tokens = []
    default_store.save()


def test_lan_ip_returns_something_dialable() -> None:
    address = mcp_server.lan_ip()
    socket.inet_aton(address)  # raises if it is not a dotted quad


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

from tests.conftest import requires_display  # noqa: E402


@requires_display
def test_the_mcp_page_builds_with_no_tokens(gallery_window) -> None:
    from muga.settings_window import SettingsWindow

    dialog = SettingsWindow(gallery_window, "mcp")
    try:
        assert dialog.get_visible_page_name() == "mcp"
        # No tokens on a fresh profile, so the address row stays hidden and
        # the switch is off.
        assert not dialog._mcp_active_row.get_active()
        assert not dialog._mcp_address_row.get_visible()
    finally:
        dialog._closing = True
        dialog.destroy()


@requires_display
def test_turning_the_switch_on_without_a_token_bounces_it_back(gallery_window) -> None:
    from muga.settings_window import SettingsWindow

    dialog = SettingsWindow(gallery_window, "mcp")
    try:
        dialog._mcp_store.tokens = []
        dialog._mcp_active_row.set_active(True)
        assert not dialog._mcp_active_row.get_active()
        assert not dialog.settings.mcp_enabled
    finally:
        dialog._closing = True
        dialog.destroy()


@requires_display
def test_the_interface_combo_starts_on_this_device_only(gallery_window) -> None:
    from muga.settings_window import SettingsWindow

    dialog = SettingsWindow(gallery_window, "mcp")
    try:
        selected = dialog._mcp_bind_values[dialog._mcp_bind_row.get_selected()]
        assert selected == "local"
        # Loopback and the wildcard always exist; the other two depend on the
        # machine, so only their presence-when-available is asserted.
        assert "local" in dialog._mcp_bind_values
        assert "all" in dialog._mcp_bind_values
        for scope, address in mcp_server.available_addresses().items():
            if address:
                assert scope in dialog._mcp_bind_values
    finally:
        dialog._closing = True
        dialog.destroy()


@requires_display
def test_each_interface_choice_names_its_address(gallery_window) -> None:
    from muga.settings_window import SettingsWindow

    dialog = SettingsWindow(gallery_window, "mcp")
    try:
        model = dialog._mcp_bind_row.get_model()
        labels = [model.get_string(i) for i in range(model.get_n_items())]
        addresses = mcp_server.available_addresses()
        for scope, label in zip(dialog._mcp_bind_values, labels):
            assert addresses[scope] in label
    finally:
        dialog._closing = True
        dialog.destroy()


@requires_display
def test_a_saved_scope_with_no_interface_is_still_listed(gallery_window, monkeypatch) -> None:
    """Dropping it silently would move the user to a different scope — a
    narrower one is safe, but they would never be told."""
    from muga.settings_window import SettingsWindow

    monkeypatch.setattr(mcp_server, "available_addresses", lambda: {
        "local": "127.0.0.1", "lan": "", "public": "", "all": "0.0.0.0",
    })
    gallery_window.settings.mcp_bind = "lan"
    dialog = SettingsWindow(gallery_window, "mcp")
    try:
        assert "lan" in dialog._mcp_bind_values
        assert dialog._mcp_bind_values[dialog._mcp_bind_row.get_selected()] == "lan"
    finally:
        gallery_window.settings.mcp_bind = "local"
        dialog._closing = True
        dialog.destroy()


@requires_display
def test_the_popover_marks_the_active_choice(gallery_window) -> None:
    """Replacing AdwComboRow's list factory also drops its tick, so the
    replacement has to carry one — otherwise an open list shows no sign of
    which scope is in force."""
    from muga.settings_window import SettingsWindow

    dialog = SettingsWindow(gallery_window, "mcp")
    try:
        # Realising the popover is what binds the list items.
        dialog._mcp_bind_row.activate()
        assert dialog._mcp_bind_checks, "no list items were bound"
        selected = dialog._mcp_bind_row.get_selected()
        visible = [pos for pos, check in dialog._mcp_bind_checks.items()
                   if check.get_visible()]
        assert visible == [selected]

        target = dialog._mcp_bind_values.index("all")
        dialog._mcp_bind_row.set_selected(target)
        visible = [pos for pos, check in dialog._mcp_bind_checks.items()
                   if check.get_visible()]
        assert visible == [target]
    finally:
        gallery_window.settings.mcp_bind = "local"
        gallery_window.settings.save()
        dialog._closing = True
        dialog.destroy()


@requires_display
def test_the_address_row_shows_a_dialable_host_for_every_scope(gallery_window) -> None:
    """"All interfaces" binds 0.0.0.0, which is not something to type into a
    client — the row has to name a real address instead."""
    from muga.settings_window import SettingsWindow

    dialog = SettingsWindow(gallery_window, "mcp")
    token = None
    try:
        dialog._mcp_store.tokens = []
        token = dialog._mcp_store.add("test")
        dialog._mcp_active_row.set_active(True)
        server = dialog._mcp_server()
        if server is None or not server.running:
            pytest.skip("the shared server could not bind in this environment")
        assert "0.0.0.0" not in dialog._mcp_address_row.get_subtitle()
        assert dialog._mcp_address_row.get_subtitle().endswith("/mcp")

        dialog._mcp_bind_row.set_selected(dialog._mcp_bind_values.index("all"))
        assert "0.0.0.0" not in dialog._mcp_address_row.get_subtitle()
    finally:
        if token is not None:
            dialog._mcp_store.remove(token.id)
            dialog._mcp_tokens_changed()
        gallery_window.settings.mcp_bind = "local"
        gallery_window.settings.mcp_enabled = False
        gallery_window.settings.save()
        mcp_server.shutdown()
        dialog._closing = True
        dialog.destroy()


@requires_display
def test_picking_another_interface_persists_it(gallery_window) -> None:
    from muga.settings_window import SettingsWindow

    dialog = SettingsWindow(gallery_window, "mcp")
    try:
        index = dialog._mcp_bind_values.index("all")
        dialog._mcp_bind_row.set_selected(index)
        assert dialog.settings.mcp_bind == "all"
        assert gallery_window.settings.mcp_bind == "all"
    finally:
        gallery_window.settings.mcp_bind = "local"
        gallery_window.settings.save()
        dialog._closing = True
        dialog.destroy()


@requires_display
def test_the_token_list_renders_one_row_per_token(gallery_window) -> None:
    from muga.settings_window import SettingsWindow

    dialog = SettingsWindow(gallery_window, "mcp")
    try:
        dialog._mcp_store.tokens = [
            Token(id="1", name="Laptop", token="muga_" + "a" * 43),
            Token(id="2", name="Phone", token="muga_" + "b" * 43),
        ]
        dialog._mcp_populate_tokens()
        titles = []
        child = dialog._mcp_token_listbox.get_first_child()
        while child is not None:
            titles.append(child.get_title())
            child = child.get_next_sibling()
        assert titles == ["Laptop", "Phone"]
    finally:
        dialog._closing = True
        dialog.destroy()


# ---------------------------------------------------------------------------
# Origin validation — the transport's DNS-rebinding defence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("origin", "allowed", "note"),
    [
        ("", True, "no header at all — every non-browser client"),
        ("http://localhost:3000", True, "a local browser client"),
        ("http://127.0.0.1:8765", True, "loopback by address"),
        ("http://[::1]:8765", True, "loopback over IPv6"),
        ("https://127.0.0.1", True, "loopback behind a local TLS proxy"),
        ("http://evil.example", False, "the rebinding case: a page's own host"),
        ("https://gallery.example.com", False, "any remote site"),
        ("null", False, "a sandboxed iframe or a file:// page"),
        ("http://127.0.0.1.evil.example", False, "a name that only looks local"),
        ("http://192.168.0.25:8765", False, "the LAN address is not the origin"),
        ("file:///tmp/x.html", False, "a scheme with no authority to check"),
        ("not a url", False, "unparseable"),
    ],
)
def test_which_origins_may_be_served(origin, allowed, note) -> None:
    assert mcp_server.origin_is_allowed(origin) is allowed, note


def test_a_cross_origin_request_is_refused(live) -> None:
    """A page in the user's browser that rebinds its host to loopback reaches
    the socket; the Origin header still names the page, and that is what stops
    it. Before this check the request went straight to the token comparison."""
    server, token = live
    reply = _post(
        server, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        token=token.token, origin="http://evil.example", expect=403,
    )
    assert "origin" in reply["error"]["message"].lower()


def test_a_cross_origin_request_is_refused_before_the_token_is_read(live) -> None:
    """403 for a valid *and* an invalid token: which one it was must not be
    something a rebound page can measure."""
    server, _token = live
    for presented in (None, "muga_not_the_one"):
        _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
              token=presented, origin="http://evil.example", expect=403)


def test_a_cross_origin_request_is_refused_on_an_unknown_path(live) -> None:
    """Ahead of the path check too — otherwise 404 vs 403 tells a page whether
    it found a live MCP server."""
    server, token = live
    _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
          token=token.token, path="/nope", origin="http://evil.example", expect=403)


def test_a_local_origin_still_gets_through(live) -> None:
    server, token = live
    reply = _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                  token=token.token, origin="http://localhost:3000")
    assert reply["result"] == {}


def test_a_client_sending_no_origin_is_unaffected(live) -> None:
    server, token = live
    reply = _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                  token=token.token)
    assert reply["result"] == {}
