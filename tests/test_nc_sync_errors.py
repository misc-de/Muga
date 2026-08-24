"""
Regression tests for fast, visible failure on a broken Nextcloud connection.

Background: `scan_nc_structure` used to swallow *every* exception and return
False, so a timeout / refused / 401 during sync was indistinguishable from
"nothing changed". The UI got no error and on-demand thumbnail fetches kept
hammering the dead server — the "endless update" the user reported.

These tests pin the new contract:
  * a transport/auth failure during the structure scan propagates (so the app
    can flag it broken and tell the user),
  * a vanished Photos folder (404) is still handled gracefully (prune), and
  * `ensure_thumbnail` distinguishes a dead server (raises) from a single
    missing preview (returns None).
"""
import pytest

from muga import nextcloud
from muga.database import Database
from muga.nextcloud import NextcloudClient, NextcloudConnectionError
from muga.scanner import MediaScanner


def _scanner(tmp_path) -> MediaScanner:
    db = Database(tmp_path / "test.sqlite3")
    # scan_nc_structure never touches the thumbnailer, so a stub is fine.
    return MediaScanner(db, thumbnailer=None)


class _RaisingClient:
    """Minimal NextcloudClient stand-in whose list_files always fails."""

    dav_root = "/remote.php/dav/files/user"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def list_files(self, photos_path: str):
        raise self._exc


# --------------------------------------------------------------------------
# scan_nc_structure error contract
# --------------------------------------------------------------------------

def test_scan_nc_structure_reraises_timeout(tmp_path):
    scanner = _scanner(tmp_path)
    with pytest.raises(TimeoutError):
        scanner.scan_nc_structure(_RaisingClient(TimeoutError("timed out")), "Photos")


def test_scan_nc_structure_reraises_refused(tmp_path):
    scanner = _scanner(tmp_path)
    with pytest.raises(ConnectionRefusedError):
        scanner.scan_nc_structure(
            _RaisingClient(ConnectionRefusedError("refused")), "Photos"
        )


def test_scan_nc_structure_reraises_auth_failure(tmp_path):
    scanner = _scanner(tmp_path)
    with pytest.raises(PermissionError):
        scanner.scan_nc_structure(_RaisingClient(PermissionError("HTTP 401")), "Photos")


def test_scan_nc_structure_prunes_on_missing_folder(tmp_path):
    """A 404 (folder deleted on the server) is a legitimate state, not a
    connection error: it is handled internally and must NOT propagate."""
    scanner = _scanner(tmp_path)
    result = scanner.scan_nc_structure(
        _RaisingClient(FileNotFoundError("HTTP 404")), "Photos"
    )
    assert result is False  # nothing was indexed, so nothing got pruned
    assert scanner.missing_root.get("nextcloud") == "Photos"


# --------------------------------------------------------------------------
# ensure_thumbnail: dead server vs. single missing preview
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status

    def read(self, *_a):
        return b""

    def getheader(self, *_a):
        return None


class _FakeConn:
    def __init__(self, status: int | None = None, exc: Exception | None = None) -> None:
        self._status = status
        self._exc = exc

    def request(self, *_a, **_k):
        if self._exc:
            raise self._exc

    def getresponse(self):
        if self._exc:
            raise self._exc
        return _FakeResp(self._status)

    def close(self):
        pass


def _client(tmp_path, monkeypatch) -> NextcloudClient:
    # Keep the test hermetic: don't write into the real thumbnail cache dir.
    monkeypatch.setattr(nextcloud, "_NC_THUMB", tmp_path / "thumbs")
    return NextcloudClient("https://nc.example.invalid", "user", "pw")


def test_ensure_thumbnail_raises_on_dead_server(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        client, "_persistent_conn",
        lambda timeout=10.0: _FakeConn(exc=ConnectionRefusedError("refused")),
    )
    monkeypatch.setattr(client, "_drop_persistent_conn", lambda: None)
    with pytest.raises(NextcloudConnectionError):
        client.ensure_thumbnail("/remote.php/dav/files/user/Photos/x.jpg")


def test_ensure_thumbnail_returns_none_on_http_miss(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        client, "_persistent_conn", lambda timeout=10.0: _FakeConn(status=404),
    )
    monkeypatch.setattr(client, "_drop_persistent_conn", lambda: None)
    assert client.ensure_thumbnail(
        "/remote.php/dav/files/user/Photos/missing.jpg"
    ) is None


def test_connection_error_is_a_connection_error_subclass():
    """The app's generic NC handler keys off isinstance(..., ConnectionError);
    keep that mapping intact."""
    assert issubclass(NextcloudConnectionError, ConnectionError)
