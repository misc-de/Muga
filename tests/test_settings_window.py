"""Tests for the settings dialog.

The dialog is almost entirely widget construction, so most of the value here
is in building it for real and walking its pages — that alone catches a
mis-parented row or a renamed attribute, which is otherwise only visible when
a user opens the page.

The credential QR parser and the update-row formatting are pure logic and are
tested headless. The QR parser deserves the attention: it is the one place
where text from a scanned code becomes an account password.
"""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import requires_display

sw = pytest.importorskip("yaga.settings_window")

SettingsWindow = sw.SettingsWindow


# ---------------------------------------------------------------------------
# Nextcloud login QR codes
# ---------------------------------------------------------------------------

def test_qr_parses_a_full_login_url() -> None:
    parsed = SettingsWindow._parse_nc_login_url(
        "nc://login/user:alice&password:s3cret&server:https://cloud.example.org",
    )
    assert parsed == {
        "user": "alice", "password": "s3cret", "server": "https://cloud.example.org",
    }


def test_qr_accepts_a_password_only_code() -> None:
    parsed = SettingsWindow._parse_nc_login_url("nc://login/password:only")
    assert parsed == {"password": "only"}


def test_qr_requires_a_password() -> None:
    """Without one there is nothing to fill in, so the raw text is treated as
    a plain app password instead."""
    assert SettingsWindow._parse_nc_login_url("nc://login/user:alice") is None


@pytest.mark.parametrize(
    "text",
    ["", "https://cloud.example.org", "nc://other/password:x",
     "NC://LOGIN/password:x", "just-an-app-password"],
)
def test_qr_rejects_anything_that_is_not_a_login_url(text) -> None:
    assert SettingsWindow._parse_nc_login_url(text) is None


def test_qr_ignores_unknown_fields() -> None:
    """A future Nextcloud release adding a field must not break the scan."""
    parsed = SettingsWindow._parse_nc_login_url(
        "nc://login/user:alice&password:pw&flavour:vanilla",
    )
    assert parsed == {"user": "alice", "password": "pw"}


def test_qr_keeps_a_password_containing_colons() -> None:
    """partition() splits on the first colon only — an app password with one
    in it must survive intact."""
    parsed = SettingsWindow._parse_nc_login_url("nc://login/password:aa:bb:cc")
    assert parsed["password"] == "aa:bb:cc"


def test_qr_keeps_the_scheme_in_the_server_field() -> None:
    parsed = SettingsWindow._parse_nc_login_url(
        "nc://login/password:pw&server:https://cloud.example.org:8443",
    )
    assert parsed["server"] == "https://cloud.example.org:8443"


# ---------------------------------------------------------------------------
# Update row
# ---------------------------------------------------------------------------

def _update_win(last_check="") -> SimpleNamespace:
    return SimpleNamespace(
        _=lambda s: s,
        parent_window=SimpleNamespace(settings=SimpleNamespace(last_update_check=last_check)),
    )


def test_update_row_shows_the_version() -> None:
    from yaga import updater

    text = SettingsWindow._update_row_subtitle(_update_win())
    assert f"v{updater.get_current_version()}" in text


def test_update_row_shows_the_last_check() -> None:
    iso = datetime(2026, 3, 14, 9, 30).isoformat()
    text = SettingsWindow._update_row_subtitle(_update_win(iso))
    assert "14.03.2026" in text
    assert "09:30" in text


def test_update_row_takes_an_explicit_timestamp() -> None:
    win = _update_win("")
    text = SettingsWindow._update_row_subtitle(win, datetime(2026, 1, 2, 3, 4).isoformat())
    assert "02.01.2026" in text


def test_update_row_ignores_an_unparsable_timestamp() -> None:
    text = SettingsWindow._update_row_subtitle(_update_win("not-a-date"))
    assert "Last checked" not in text


# ---------------------------------------------------------------------------
# Update flow
# ---------------------------------------------------------------------------

def test_check_update_worker_survives_a_raising_updater() -> None:
    """A dead worker never reaches idle_add, and the button stays on
    "Checking…" for the rest of the session."""
    win = SimpleNamespace(_=lambda s: s, _on_check_update_done=MagicMock())
    with patch.object(sw.updater, "check_for_update", side_effect=RuntimeError("no net")), \
         patch.object(sw.GLib, "idle_add") as idle:
        SettingsWindow._do_check_update(win)
    idle.assert_called_once()
    info = idle.call_args[0][1]
    assert info.available is False


def test_apply_update_worker_survives_a_raising_updater() -> None:
    win = SimpleNamespace(_=lambda s: s, _on_apply_update_done=MagicMock())
    with patch.object(sw.updater, "apply_update", side_effect=OSError("disk full")), \
         patch.object(sw.GLib, "idle_add") as idle:
        SettingsWindow._do_apply_update(win)
    idle.assert_called_once_with(win._on_apply_update_done, False)


def test_apply_update_worker_reports_success() -> None:
    win = SimpleNamespace(_=lambda s: s, _on_apply_update_done=MagicMock())
    with patch.object(sw.updater, "apply_update", return_value=True), \
         patch.object(sw.GLib, "idle_add") as idle:
        SettingsWindow._do_apply_update(win)
    assert idle.call_args[0][1] is True


def test_cancel_no_update_reset_clears_the_timer() -> None:
    win = SimpleNamespace(_no_update_reset_src=17)
    with patch.object(sw.GLib, "source_remove") as remove:
        SettingsWindow._cancel_no_update_reset(win)
    remove.assert_called_once_with(17)
    assert win._no_update_reset_src == 0


def test_cancel_no_update_reset_without_a_timer() -> None:
    win = SimpleNamespace(_no_update_reset_src=0)
    with patch.object(sw.GLib, "source_remove") as remove:
        SettingsWindow._cancel_no_update_reset(win)
    remove.assert_not_called()


def test_reset_update_button_skips_a_closing_dialog() -> None:
    """The timeout can outlive the dialog; touching the button then hits a
    destroyed widget."""
    button = MagicMock()
    win = SimpleNamespace(_=lambda s: s, _closing=True, _update_btn=button,
                          _no_update_reset_src=5)
    SettingsWindow._reset_update_btn_idle(win)
    button.set_label.assert_not_called()
    assert win._no_update_reset_src == 0


def test_reset_update_button_restores_the_idle_label() -> None:
    button = MagicMock()
    win = SimpleNamespace(_=lambda s: s, _closing=False, _update_btn=button,
                          _no_update_reset_src=5)
    SettingsWindow._reset_update_btn_idle(win)
    button.set_label.assert_called_once_with("Check for updates")
    button.set_sensitive.assert_called_once_with(True)


# ---------------------------------------------------------------------------
# Cache size formatting
# ---------------------------------------------------------------------------

def test_cache_size_text_is_human_readable() -> None:
    row = MagicMock()
    win = SimpleNamespace(_=lambda s: s, _closing=False, _cache_size_row=row)
    SettingsWindow._set_cache_size_text(win, 5 * 1024 * 1024)
    text = row.set_subtitle.call_args[0][0]
    assert "MB" in text or "MiB" in text


def test_cache_size_text_skips_a_closing_dialog() -> None:
    row = MagicMock()
    win = SimpleNamespace(_=lambda s: s, _closing=True, _cache_size_row=row)
    SettingsWindow._set_cache_size_text(win, 1024)
    row.set_subtitle.assert_not_called()


# ---------------------------------------------------------------------------
# Real settings dialog
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_dialog(gallery_window):
    made = []

    def _make(initial_page=None):
        dialog = SettingsWindow(gallery_window, initial_page)
        made.append(dialog)
        return dialog

    yield _make

    for dialog in made:
        dialog._closing = True


@requires_display
def test_settings_dialog_builds(settings_dialog) -> None:
    dialog = settings_dialog()
    assert dialog.parent_window is not None
    assert dialog.settings is not None


@requires_display
@pytest.mark.parametrize("page", [None, "appearance", "folders", "nextcloud"])
def test_settings_dialog_opens_on_any_page(settings_dialog, page) -> None:
    assert settings_dialog(page) is not None


@requires_display
def test_settings_dialog_shows_the_update_row(settings_dialog) -> None:
    from yaga import updater

    dialog = settings_dialog()
    assert f"v{updater.get_current_version()}" in dialog._update_row.get_subtitle()


@requires_display
def test_diagnostics_text_covers_the_environment(settings_dialog) -> None:
    """This is what a user pastes into a bug report."""
    text = settings_dialog()._diagnostics_text()
    for marker in ("Python", "Platform", "Config", "Cache", "Database",
                   "Media folders", "Nextcloud", "Camera settings",
                   "GStreamer", "Torch sysfs"):
        assert marker in text, f"{marker} missing from the diagnostics dump"


@requires_display
def test_diagnostics_text_names_no_library_versions(settings_dialog) -> None:
    """Documents a gap rather than a behaviour: the dump reports the Python
    and GStreamer versions but not GTK, libadwaita or Pillow — and the Pillow
    version in particular decides whether the image decoders handling
    untrusted photos are current. Worth adding; pinned so the omission is
    noticed if someone reads this looking for it.
    """
    text = settings_dialog()._diagnostics_text()
    assert "Pillow" not in text
    assert "GTK version" not in text


@requires_display
def test_diagnostics_text_does_not_leak_the_password(settings_dialog, gallery_window) -> None:
    """It goes into bug reports verbatim."""
    gallery_window.settings.nextcloud_url = "https://cloud.example.org"
    gallery_window.settings.nextcloud_user = "alice"
    with patch.object(type(gallery_window.settings), "load_app_password",
                      return_value="super-secret-token"):
        text = settings_dialog()._diagnostics_text()
    assert "super-secret-token" not in text
    assert "alice" not in text, "the account name is in bug reports too"


@requires_display
def test_nc_is_configured_reflects_the_settings(settings_dialog, gallery_window) -> None:
    """The dialog copies Settings at construction (so Cancel is possible), so
    the parent has to be set up before it opens."""
    gallery_window.settings.nextcloud_url = ""
    gallery_window.settings.nextcloud_user = ""
    assert settings_dialog()._nc_is_configured() is False

    gallery_window.settings.nextcloud_url = "https://cloud.example.org"
    gallery_window.settings.nextcloud_user = "alice"
    assert settings_dialog()._nc_is_configured() is True


@requires_display
def test_settings_dialog_edits_a_copy(settings_dialog, gallery_window) -> None:
    """Editing the parent's Settings directly would apply every keystroke
    immediately and leave no way back."""
    dialog = settings_dialog()
    assert dialog.settings is not gallery_window.settings
    dialog.settings.grid_columns = 9
    assert gallery_window.settings.grid_columns != 9


@requires_display
def test_nc_status_line_can_be_set(settings_dialog) -> None:
    dialog = settings_dialog()
    dialog._nc_set_status("Connected", ok=True)
    dialog._nc_set_status("Failed", ok=False)


@requires_display
def test_qr_success_fills_the_credential_rows(settings_dialog) -> None:
    dialog = settings_dialog()
    dialog._closing = False
    dialog._nc_qr_success(
        MagicMock(),
        "nc://login/user:alice&password:pw123&server:https://cloud.example.org",
    )
    assert dialog._nc_user_row.get_text() == "alice"
    assert dialog._nc_pass_row.get_text() == "pw123"
    assert dialog._nc_url_row.get_text() == "https://cloud.example.org"


@requires_display
def test_qr_success_treats_plain_text_as_a_password(settings_dialog) -> None:
    """Nextcloud's app-password page also offers a bare-token QR code."""
    dialog = settings_dialog()
    dialog._closing = False
    dialog._nc_qr_success(MagicMock(), "raw-app-password")
    assert dialog._nc_pass_row.get_text() == "raw-app-password"


# ---------------------------------------------------------------------------
# Update flow, main-loop half
# ---------------------------------------------------------------------------

@requires_display
def test_update_check_offers_the_new_version(settings_dialog) -> None:
    from yaga import updater

    dialog = settings_dialog()
    dialog._on_check_update_done(updater.UpdateInfo(True, "0.9.9"), "2026-03-14T09:30:00")
    assert "0.9.9" in dialog._update_btn.get_label()   # the version is not localised
    assert dialog._update_btn.get_sensitive() is True


@requires_display
def test_update_check_reports_being_current(settings_dialog) -> None:
    from yaga import updater

    dialog = settings_dialog()
    with patch.object(sw.GLib, "timeout_add_seconds", return_value=7):
        dialog._on_check_update_done(updater.UpdateInfo(False, None), "2026-03-14T09:30:00")
    # Compared through the dialog's own translator — the label is localised.
    assert dialog._update_btn.get_label() == dialog._("Up to date")
    assert dialog._update_btn.get_sensitive() is False


@requires_display
def test_update_check_rearms_the_button_afterwards(settings_dialog) -> None:
    """Left disabled, "Up to date" would be the last thing the button ever
    says for the rest of the session."""
    from yaga import updater

    dialog = settings_dialog()
    with patch.object(sw.GLib, "timeout_add_seconds", return_value=7) as timeout:
        dialog._on_check_update_done(updater.UpdateInfo(False, None), "")
    timeout.assert_called_once()
    assert dialog._no_update_reset_src == 7


@requires_display
def test_update_check_persists_the_timestamp(settings_dialog, gallery_window) -> None:
    from yaga import updater
    from yaga.config import Settings

    dialog = settings_dialog()
    with patch.object(Settings, "save"):
        dialog._on_check_update_done(updater.UpdateInfo(False, None), "2026-03-14T09:30:00")
    assert gallery_window.settings.last_update_check == "2026-03-14T09:30:00"
    assert "14.03.2026" in dialog._update_row.get_subtitle()


@requires_display
def test_update_check_skips_a_closed_dialog(settings_dialog) -> None:
    """The check runs on a worker; the user can close Settings meanwhile."""
    from yaga import updater

    dialog = settings_dialog()
    dialog._closing = True
    before = dialog._update_btn.get_label()
    dialog._on_check_update_done(updater.UpdateInfo(True, "9.9.9"), "")
    assert dialog._update_btn.get_label() == before


@requires_display
def test_applied_update_asks_for_a_restart(settings_dialog) -> None:
    dialog = settings_dialog()
    with patch.object(type(dialog), "_show_restart_dialog") as restart:
        dialog._on_apply_update_done(True)
    assert dialog._update_btn.get_label() == dialog._("Restart required")
    restart.assert_called_once()


@requires_display
def test_failed_update_says_so_and_stops(settings_dialog) -> None:
    """Leaving it enabled invites a retry into the same failure."""
    dialog = settings_dialog()
    dialog._on_apply_update_done(False)
    assert dialog._update_btn.get_label() == dialog._("Update failed")
    assert dialog._update_btn.get_sensitive() is False


@requires_display
def test_applied_update_skips_a_closed_dialog(settings_dialog) -> None:
    dialog = settings_dialog()
    dialog._closing = True
    with patch.object(type(dialog), "_show_restart_dialog") as restart:
        dialog._on_apply_update_done(True)
    restart.assert_not_called()


@requires_display
def test_update_check_end_to_end(settings_dialog, pump) -> None:
    """Button press to result, through the worker thread and back."""
    from yaga import updater
    from yaga.config import Settings

    dialog = settings_dialog()
    with patch.object(updater, "check_for_update",
                      return_value=updater.UpdateInfo(True, "1.0.0")), \
         patch.object(Settings, "save"):
        expected = dialog._("Update to v%s") % "1.0.0"
        dialog._on_check_update(None)
        for _ in range(50):
            pump()
            if dialog._update_btn.get_label() == expected:
                break
            time.sleep(0.01)
    assert dialog._update_btn.get_label() == expected


# ---------------------------------------------------------------------------
# Nextcloud connect / disconnect
# ---------------------------------------------------------------------------

@requires_display
def test_nc_connect_success_enables_the_account(settings_dialog, gallery_window) -> None:
    from yaga.config import Settings

    dialog = settings_dialog()
    with patch.object(Settings, "save"), \
         patch.object(type(gallery_window), "apply_settings"):
        dialog._nc_connect_done(True, "")
    assert dialog.settings.nextcloud_enabled is True
    assert dialog.settings.nextcloud_session_active is True
    assert gallery_window._nc_session_active is True


@requires_display
def test_nc_connect_failure_reports_the_reason(settings_dialog) -> None:
    dialog = settings_dialog()
    dialog._nc_connect_done(False, "HTTP 401")
    assert dialog._nc_runtime_connected is False
    assert dialog._nc_connect_btn.get_sensitive() is True


@requires_display
def test_nc_connect_failure_does_not_enable_the_account(settings_dialog) -> None:
    """Credentials are written before the test runs; a failed test must not
    leave the account switched on with them."""
    dialog = settings_dialog()
    dialog.settings.nextcloud_enabled = False
    dialog._nc_connect_done(False, "refused")
    assert dialog.settings.nextcloud_enabled is False


@requires_display
def test_nc_connect_skips_a_closed_dialog(settings_dialog) -> None:
    dialog = settings_dialog()
    dialog._closing = True
    dialog._nc_connect_done(True, "")
    assert dialog.settings.nextcloud_enabled is not True


@requires_display
def test_nc_disconnect_closes_the_shared_client(settings_dialog, gallery_window) -> None:
    """Otherwise a scripted call could still reach the server with the
    credentials the user just disconnected from."""
    from yaga.config import Settings

    dialog = settings_dialog()
    client = MagicMock()
    gallery_window._nc_thumb_shared_client = client

    with patch.object(Settings, "save"):
        dialog._nc_disconnect(None)

    client.close.assert_called_once()
    assert gallery_window._nc_thumb_shared_client is None
    assert gallery_window._nc_session_active is False


@requires_display
def test_nc_disconnect_is_persisted(settings_dialog, gallery_window) -> None:
    """The next launch has to come up disconnected too."""
    from yaga.config import Settings

    dialog = settings_dialog()
    with patch.object(Settings, "save") as save:
        dialog._nc_disconnect(None)
    assert gallery_window.settings.nextcloud_session_active is False
    save.assert_called()


@requires_display
def test_nc_disconnect_leaves_the_account_configured(settings_dialog) -> None:
    """It is a soft action — cached thumbnails and the tab stay."""
    dialog = settings_dialog()
    dialog.settings.nextcloud_enabled = True
    from yaga.config import Settings

    with patch.object(Settings, "save"):
        dialog._nc_disconnect(None)
    assert dialog.settings.nextcloud_enabled is True, "the account was switched off"


@requires_display
def test_nc_disconnect_survives_a_dead_client(settings_dialog, gallery_window) -> None:
    from yaga.config import Settings

    dialog = settings_dialog()
    client = MagicMock()
    client.close.side_effect = OSError("socket already gone")
    gallery_window._nc_thumb_shared_client = client
    with patch.object(Settings, "save"):
        dialog._nc_disconnect(None)
    assert gallery_window._nc_session_active is False


@requires_display
def test_nc_connect_clears_the_index_on_an_account_change(settings_dialog, gallery_window) -> None:
    """Rows from the old account would otherwise linger in the gallery."""
    from yaga.config import Settings

    dialog = settings_dialog()
    dialog.settings.nextcloud_url = "https://old.example.org"
    dialog.settings.nextcloud_user = "alice"

    with patch.object(Settings, "save"), patch.object(Settings, "save_app_password"), \
         patch.object(type(gallery_window.database), "clear_category") as clear, \
         patch.object(sw.threading, "Thread"):
        dialog._nc_proceed_connect("https://new.example.org", "bob", "pw")

    clear.assert_called_once_with("nextcloud")


@requires_display
def test_nc_connect_keeps_the_index_for_the_same_account(settings_dialog, gallery_window) -> None:
    """Re-entering the same password must not throw the whole index away."""
    from yaga.config import Settings

    dialog = settings_dialog()
    dialog.settings.nextcloud_url = "https://cloud.example.org"
    dialog.settings.nextcloud_user = "alice"

    with patch.object(Settings, "save"), patch.object(Settings, "save_app_password"), \
         patch.object(type(gallery_window.database), "clear_category") as clear, \
         patch.object(sw.threading, "Thread"):
        dialog._nc_proceed_connect("https://cloud.example.org", "alice", "pw")

    clear.assert_not_called()


@requires_display
def test_nc_connect_stores_credentials_before_testing(settings_dialog) -> None:
    from yaga.config import Settings

    dialog = settings_dialog()
    with patch.object(Settings, "save"), \
         patch.object(Settings, "save_app_password") as store, \
         patch.object(sw.threading, "Thread"):
        dialog._nc_proceed_connect("https://cloud.example.org", "alice", "s3cret")
    store.assert_called_once_with("s3cret")
    assert dialog.settings.nextcloud_enabled is False, (
        "the account must stay off until the connection test passes"
    )


@requires_display
def test_nc_connect_defaults_the_photos_path(settings_dialog) -> None:
    from yaga.config import Settings

    dialog = settings_dialog()
    dialog.settings.nextcloud_photos_path = "   "
    with patch.object(Settings, "save"), patch.object(Settings, "save_app_password"), \
         patch.object(sw.threading, "Thread"):
        dialog._nc_proceed_connect("https://cloud.example.org", "alice", "pw")
    assert dialog.settings.nextcloud_photos_path == "Photos"


# ---------------------------------------------------------------------------
# Extra media locations
# ---------------------------------------------------------------------------

@requires_display
def test_removing_a_location_renumbers_the_order(settings_dialog) -> None:
    """media_folder_order keys carry the list index, so a removal in the
    middle has to shift every later key down or the nav points at the wrong
    folders."""
    dialog = settings_dialog()
    dialog.settings.extra_locations = ["/a", "/b", "/c"]
    dialog.settings.extra_location_names = ["A", "B", "C"]
    dialog.settings.extra_location_no_inherit = [False, True, False]
    dialog.settings.extra_location_media_filter = ["both", "images", "videos"]
    dialog.settings.media_folder_order = [
        "photos", "location:0", "location:1", "location:2", "videos",
    ]

    dialog._remove_extra_at(1)

    assert dialog.settings.extra_locations == ["/a", "/c"]
    assert dialog.settings.extra_location_names == ["A", "C"]
    assert dialog.settings.extra_location_no_inherit == [False, False]
    assert dialog.settings.extra_location_media_filter == ["both", "videos"]
    assert dialog.settings.media_folder_order == [
        "photos", "location:0", "location:1", "videos",
    ]


@requires_display
def test_removing_the_last_location(settings_dialog) -> None:
    dialog = settings_dialog()
    dialog.settings.extra_locations = ["/a", "/b"]
    dialog.settings.extra_location_names = ["A", "B"]
    dialog.settings.extra_location_no_inherit = [False, False]
    dialog.settings.extra_location_media_filter = ["both", "both"]
    dialog.settings.media_folder_order = ["location:0", "location:1"]

    dialog._remove_extra_at(1)

    assert dialog.settings.extra_locations == ["/a"]
    assert dialog.settings.media_folder_order == ["location:0"]


@requires_display
@pytest.mark.parametrize("idx", [-1, 5, 99])
def test_removing_an_out_of_range_location_is_a_noop(settings_dialog, idx) -> None:
    dialog = settings_dialog()
    dialog.settings.extra_locations = ["/a"]
    before = list(dialog.settings.extra_locations)
    dialog._remove_extra_at(idx)
    assert dialog.settings.extra_locations == before


@requires_display
def test_removing_a_location_tolerates_short_parallel_lists(settings_dialog) -> None:
    """Older settings.json files have paths but no names/filters."""
    dialog = settings_dialog()
    dialog.settings.extra_locations = ["/a", "/b"]
    dialog.settings.extra_location_names = []
    dialog.settings.extra_location_no_inherit = []
    dialog.settings.extra_location_media_filter = []
    dialog.settings.media_folder_order = ["location:0", "location:1"]
    dialog._remove_extra_at(0)
    assert dialog.settings.extra_locations == ["/b"]


@requires_display
def test_removing_a_location_keeps_unrelated_order_keys(settings_dialog) -> None:
    dialog = settings_dialog()
    dialog.settings.extra_locations = ["/a", "/b"]
    dialog.settings.extra_location_names = ["A", "B"]
    dialog.settings.extra_location_no_inherit = [False, False]
    dialog.settings.extra_location_media_filter = ["both", "both"]
    dialog.settings.media_folder_order = [
        "pictures", "location:0", "photos", "location:1", "screenshots",
    ]
    dialog._remove_extra_at(0)
    assert dialog.settings.media_folder_order == [
        "pictures", "photos", "location:0", "screenshots",
    ]


@requires_display
def test_removing_a_location_survives_a_malformed_order_key(settings_dialog) -> None:
    dialog = settings_dialog()
    dialog.settings.extra_locations = ["/a", "/b"]
    dialog.settings.extra_location_names = ["A", "B"]
    dialog.settings.extra_location_no_inherit = [False, False]
    dialog.settings.extra_location_media_filter = ["both", "both"]
    dialog.settings.media_folder_order = ["location:zero", "location:0", "location:1"]
    dialog._remove_extra_at(0)
    assert "location:zero" in dialog.settings.media_folder_order
