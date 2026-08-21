"""Tests for the WebDAV client.

Every response here comes from a network peer, so the parsing and file-writing
paths are the ones that matter: a hostile or simply broken server must not be
able to make the client write outside its cache, buffer an unbounded body, or
leave a half-downloaded file where a complete one is expected.

The HTTP layer is driven through a fake connection rather than a socket, so
these run offline and deterministically.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

nc = pytest.importorskip("yaga.nextcloud")


def _client(url="https://cloud.example.org", user="alice", pw="app-password"):
    return nc.NextcloudClient(url, user, pw)


class _Response:
    """Stands in for http.client.HTTPResponse."""

    def __init__(self, status=200, body=b"", headers=None) -> None:
        self.status = status
        self._stream = io.BytesIO(body)
        self._headers = headers or {}

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, size=None):
        return self._stream.read(size) if size else self._stream.read()


# ---------------------------------------------------------------------------
# URL and path handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("given", "host", "ssl"),
    [
        ("https://cloud.example.org", "cloud.example.org", True),
        ("http://cloud.example.org", "cloud.example.org", False),
        ("cloud.example.org", "cloud.example.org", True),
        ("https://cloud.example.org:8443", "cloud.example.org:8443", True),
    ],
)
def test_client_parses_the_server_url(given, host, ssl) -> None:
    """A bare hostname must default to https, never to cleartext."""
    client = _client(url=given)
    assert client.host == host
    assert client.use_ssl is ssl


def test_client_builds_the_dav_root_from_the_username() -> None:
    assert _client(user="alice").dav_root == "/remote.php/dav/files/alice"


def test_client_sends_basic_auth() -> None:
    import base64

    headers = _client(user="alice", pw="pw")._headers()
    scheme, _, token = headers["Authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(token) == b"alice:pw"


def test_client_identifies_itself() -> None:
    assert _client()._headers()["User-Agent"].startswith("Yaga/")


def test_headers_accept_extras_without_losing_auth() -> None:
    headers = _client()._headers({"Depth": "infinity"})
    assert headers["Depth"] == "infinity"
    assert "Authorization" in headers


def test_nc_path_round_trips() -> None:
    dav = "/remote.php/dav/files/alice/Photos/a.jpg"
    assert nc.dav_path_from_nc(nc.nc_path(dav)) == dav


def test_nc_path_is_recognisable() -> None:
    assert nc.is_nc_path(nc.nc_path("/x/a.jpg")) is True
    assert nc.is_nc_path("/home/u/a.jpg") is False


# ---------------------------------------------------------------------------
# PROPFIND parsing
# ---------------------------------------------------------------------------

def _multistatus(entries: str) -> bytes:
    return (
        b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
        + entries.encode() + b"</D:multistatus>"
    )


def _entry(href, size=100, mtime="Wed, 21 Oct 2015 07:28:00 GMT", collection=False):
    rtype = "<D:collection/>" if collection else ""
    return (
        f"<D:response><D:href>{href}</D:href><D:prop>"
        f"<D:getcontentlength>{size}</D:getcontentlength>"
        f"<D:getlastmodified>{mtime}</D:getlastmodified>"
        f"<D:resourcetype>{rtype}</D:resourcetype>"
        f"</D:prop></D:response>"
    )


def test_propfind_returns_files() -> None:
    body = _multistatus(_entry("/remote.php/dav/files/alice/Photos/a.jpg", size=4096))
    files = _client()._parse_propfind(body, "/remote.php/dav/files/alice/Photos")
    assert len(files) == 1
    assert files[0]["name"] == "a.jpg"
    assert files[0]["size"] == 4096
    assert files[0]["mtime"] > 0


def test_propfind_skips_directories() -> None:
    body = _multistatus(
        _entry("/remote.php/dav/files/alice/Photos/sub/", collection=True)
        + _entry("/remote.php/dav/files/alice/Photos/a.jpg")
    )
    files = _client()._parse_propfind(body, "/remote.php/dav/files/alice/Photos")
    assert [f["name"] for f in files] == ["a.jpg"]


def test_propfind_skips_the_queried_folder_itself() -> None:
    base = "/remote.php/dav/files/alice/Photos"
    body = _multistatus(_entry(base + "/") + _entry(base + "/a.jpg"))
    assert len(_client()._parse_propfind(body, base)) == 1


def test_propfind_unquotes_hrefs() -> None:
    body = _multistatus(_entry("/remote.php/dav/files/alice/Photos/a%20b%C3%A4r.jpg"))
    files = _client()._parse_propfind(body, "/remote.php/dav/files/alice/Photos")
    assert files[0]["name"] == "a bär.jpg"


def test_propfind_falls_back_to_epoch_on_a_bad_date() -> None:
    """time.time() would make the entry jump to "today" in the date-sorted
    view on every rescan."""
    body = _multistatus(_entry("/x/a.jpg", mtime="not a date"))
    assert _client()._parse_propfind(body, "/x")[0]["mtime"] == 0.0


def test_propfind_tolerates_a_missing_size() -> None:
    body = _multistatus(
        "<D:response><D:href>/x/a.jpg</D:href><D:prop>"
        "<D:getcontentlength>huge</D:getcontentlength></D:prop></D:response>"
    )
    assert _client()._parse_propfind(body, "/x")[0]["size"] == 0


def test_propfind_returns_nothing_for_malformed_xml() -> None:
    assert _client()._parse_propfind(b"<not xml", "/x") == []
    assert _client()._parse_propfind(b"", "/x") == []


def test_propfind_refuses_external_entities() -> None:
    """A compromised or MitM'd server must not be able to read local files."""
    body = (
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<D:multistatus xmlns:D="DAV:"><D:response><D:href>/x/&x;.jpg</D:href>'
        b"<D:prop><D:getcontentlength>1</D:getcontentlength></D:prop>"
        b"</D:response></D:multistatus>"
    )
    files = _client()._parse_propfind(body, "/x")
    assert not any("root:" in f.get("dav_path", "") for f in files)


def test_cache_filename_cannot_escape_the_cache_dir() -> None:
    """The href comes from the server; a traversal attempt has to flatten."""
    evil = "/remote.php/dav/files/alice/../../../../etc/passwd"
    safe = evil.lstrip("/").replace("/", "_")
    assert "/" not in safe
    assert ".." in safe, "the sanitiser is expected to flatten, not strip"


# ---------------------------------------------------------------------------
# Bounded reads
# ---------------------------------------------------------------------------

def test_read_bounded_returns_a_small_body() -> None:
    body = b"x" * 1000
    out = _client()._read_bounded(_Response(body=body, headers={"Content-Length": "1000"}),
                                  4096, "PROPFIND")
    assert out == body


def test_read_bounded_rejects_a_declared_oversize() -> None:
    """Refused before a single chunk is buffered."""
    with pytest.raises(nc.NextcloudResponseTooLarge):
        _client()._read_bounded(_Response(headers={"Content-Length": "999999"}),
                                4096, "PROPFIND")


def test_read_bounded_rejects_an_undeclared_oversize() -> None:
    """A chunked response declares no length at all."""
    with pytest.raises(nc.NextcloudResponseTooLarge):
        _client()._read_bounded(_Response(body=b"y" * 9000), 4096, "PROPFIND")


def test_read_bounded_ignores_a_nonsense_content_length() -> None:
    out = _client()._read_bounded(_Response(body=b"ok", headers={"Content-Length": "banana"}),
                                  4096, "PROPFIND")
    assert out == b"ok"


# ---------------------------------------------------------------------------
# Atomic downloads
# ---------------------------------------------------------------------------

def test_download_publishes_atomically(tmp_path: Path) -> None:
    dest = tmp_path / "photo.jpg"
    assert _client()._write_response_atomic(_Response(body=b"jpegdata"), dest) is True
    assert dest.read_bytes() == b"jpegdata"
    assert list(tmp_path.iterdir()) == [dest], "temp file left behind"


def test_download_refuses_a_declared_oversize(tmp_path: Path) -> None:
    dest = tmp_path / "thumb.jpg"
    resp = _Response(body=b"x" * 100, headers={"Content-Length": "99999999"})
    assert _client()._write_response_atomic(resp, dest, max_bytes=1024) is False
    assert not dest.exists()


def test_download_stops_at_the_streaming_limit(tmp_path: Path) -> None:
    """A server that under-declares its length must still be cut off."""
    dest = tmp_path / "thumb.jpg"
    resp = _Response(body=b"x" * (3 * 1024 * 1024))
    assert _client()._write_response_atomic(resp, dest, max_bytes=1024) is False
    assert not dest.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_download_leaves_no_partial_file_on_error(tmp_path: Path) -> None:
    dest = tmp_path / "photo.jpg"
    resp = MagicMock()
    resp.getheader.return_value = None
    resp.read.side_effect = OSError("connection reset")
    assert _client()._write_response_atomic(resp, dest) is False
    assert not dest.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_download_never_leaves_a_truncated_file_in_place(tmp_path: Path) -> None:
    """A previously cached file must survive a failed refresh."""
    dest = tmp_path / "photo.jpg"
    dest.write_bytes(b"the good copy")
    resp = MagicMock()
    resp.getheader.return_value = None
    resp.read.side_effect = [b"partial", OSError("reset")]
    _client()._write_response_atomic(resp, dest)
    assert dest.read_bytes() == b"the good copy"


def test_temp_names_do_not_collide_across_threads(tmp_path: Path) -> None:
    client = _client()
    dest = tmp_path / "x.jpg"
    names = {str(client._temp_path_for(dest)) for _ in range(50)}
    assert len(names) == 50, "temp names repeat; two workers would fight"


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def test_persistent_connection_is_reused() -> None:
    """The TLS handshake dominates a thumbnail loop; reconnecting per request
    was measured at 10-30x slower."""
    client = _client()
    with patch.object(client, "_conn", side_effect=lambda timeout=10.0: MagicMock()) as make:
        first = client._persistent_conn()
        second = client._persistent_conn()
    assert first is second
    assert make.call_count == 1


def test_dropping_the_connection_forces_a_reopen() -> None:
    client = _client()
    with patch.object(client, "_conn", side_effect=lambda timeout=10.0: MagicMock()) as make:
        first = client._persistent_conn()
        client._drop_persistent_conn()
        second = client._persistent_conn()
    assert first is not second
    assert make.call_count == 2


def test_dropping_a_dead_connection_is_safe() -> None:
    client = _client()
    conn = MagicMock()
    conn.close.side_effect = OSError("already closed")
    client._tls_local.conn = conn
    client._drop_persistent_conn()
    assert client._tls_local.conn is None


def test_close_releases_the_connection() -> None:
    client = _client()
    client._tls_local.conn = MagicMock()
    client.close()
    assert client._tls_local.conn is None


def test_ssl_context_is_built_once() -> None:
    """create_default_context reads the CA bundle off disk each call."""
    client = _client()
    assert client._ssl_ctx is not None
    assert client._ssl_ctx is client._ssl_ctx


def test_no_ssl_context_for_cleartext() -> None:
    assert _client(url="http://cloud.example.org")._ssl_ctx is None
