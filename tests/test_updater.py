"""Unit tests for the in-app updater's check/parse logic (no real network or
git — the subprocess/HTTP helpers are monkeypatched)."""
from __future__ import annotations

from yaga import updater
from yaga.updater import UpdateInfo


def test_parse_version_extracts_from_init_text() -> None:
    assert updater._parse_version('VERSION = "1.2.3"') == "1.2.3"
    assert updater._parse_version("VERSION='0.9.0'") == "0.9.0"
    assert updater._parse_version('APP_ID = "x"\nVERSION = "4.5.6"\n') == "4.5.6"
    assert updater._parse_version("no version here") is None
    assert updater._parse_version(None) is None


def test_get_current_version_matches_package() -> None:
    from yaga import VERSION
    assert updater.get_current_version() == VERSION


def test_check_git_reports_update_when_behind(monkeypatch) -> None:
    monkeypatch.setattr(updater, "_is_git_repo", lambda: True)

    def fake_git(*args, timeout=30):
        if args[0] == "rev-parse":
            return 0, "main"
        if args[0] == "fetch":
            return 0, ""
        if args[0] == "rev-list":
            return 0, "3"  # local HEAD is 3 commits behind origin/main
        if args[0] == "show":
            return 0, 'VERSION = "9.9.9"'
        return 0, ""

    monkeypatch.setattr(updater, "_git", fake_git)
    assert updater.check_for_update() == UpdateInfo(True, "9.9.9")


def test_check_git_reports_no_update_when_current(monkeypatch) -> None:
    monkeypatch.setattr(updater, "_is_git_repo", lambda: True)

    def fake_git(*args, timeout=30):
        if args[0] == "rev-parse":
            return 0, "main"
        if args[0] == "rev-list":
            return 0, "0"  # up to date
        return 0, ""

    monkeypatch.setattr(updater, "_git", fake_git)
    assert updater.check_for_update() == UpdateInfo(False, None)


def test_check_git_handles_failure_gracefully(monkeypatch) -> None:
    """A missing upstream (rev-list errors) must not raise — just 'no update'."""
    monkeypatch.setattr(updater, "_is_git_repo", lambda: True)

    def fake_git(*args, timeout=30):
        if args[0] == "rev-parse":
            return 0, "speed"
        if args[0] == "rev-list":
            return 128, "fatal: bad revision"
        return 0, ""

    monkeypatch.setattr(updater, "_git", fake_git)
    assert updater.check_for_update() == UpdateInfo(False, None)


def test_check_zip_compares_remote_version(monkeypatch) -> None:
    monkeypatch.setattr(updater, "_is_git_repo", lambda: False)

    # Remote newer than us → update available.
    monkeypatch.setattr(updater, "_http_get_text", lambda *a, **k: 'VERSION = "99.0.0"')
    assert updater.check_for_update() == UpdateInfo(True, "99.0.0")

    # Remote identical to the running version → no update.
    monkeypatch.setattr(
        updater, "_http_get_text",
        lambda *a, **k: f'VERSION = "{updater.get_current_version()}"',
    )
    assert updater.check_for_update() == UpdateInfo(False, None)

    # Network failure (None) → no update, no crash.
    monkeypatch.setattr(updater, "_http_get_text", lambda *a, **k: None)
    assert updater.check_for_update() == UpdateInfo(False, None)
