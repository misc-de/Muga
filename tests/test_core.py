from pathlib import Path
import threading

import yaga.config as config
import yaga.thumbnails as thumbnails
from yaga.database import Database
from yaga.i18n import Translator
from yaga.models import media_type_for
from yaga.scanner import MediaScanner
from yaga.config import Settings
from yaga.thumbnails import Thumbnailer


class FakeThumbnailer:
    def ensure_thumbnail(self, path: Path, media_type: str) -> str:
        return f"thumb://{media_type}/{path.name}"


def test_media_type_detection() -> None:
    assert media_type_for(Path("photo.jpg")) == "image"
    assert media_type_for(Path("movie.webm")) == "video"
    assert media_type_for(Path("notes.txt")) is None


def test_translator_falls_back_to_english() -> None:
    assert Translator("de").gettext("Settings") == "Einstellungen"
    assert Translator("unknown").gettext("Settings") == "Settings"


def test_database_preserves_folder_hierarchy(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    root = tmp_path / "photos"
    nested = root / "Trips" / "Berlin"
    nested.mkdir(parents=True)
    direct = root / "Trips" / "cover.jpg"
    deep = nested / "street.jpg"
    direct.write_bytes(b"direct")
    deep.write_bytes(b"deep")

    db.upsert_media(path=direct, category="photos", media_type="image", folder="Trips", thumb_path=None)
    db.upsert_media(path=deep, category="photos", media_type="image", folder="Trips/Berlin", thumb_path=None)
    db.commit()

    assert db.child_folders("photos", None)[0][0] == "Trips"
    assert db.child_folders("photos", "Trips")[0][0] == "Trips/Berlin"
    assert [item.name for item in db.list_media("photos", "folder", "Trips")] == ["cover.jpg"]


def test_database_sort_modes(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    newer = tmp_path / "b-newer.jpg"
    older = tmp_path / "a-older.jpg"
    newer.write_bytes(b"newer")
    older.write_bytes(b"older")

    db.upsert_media(path=older, category="photos", media_type="image", folder="/", thumb_path=None)
    db.upsert_media(path=newer, category="photos", media_type="image", folder="/", thumb_path=None)
    db.conn.execute("UPDATE media SET mtime = 100 WHERE path = ?", (str(older),))
    db.conn.execute("UPDATE media SET mtime = 200 WHERE path = ?", (str(newer),))
    db.commit()

    assert [item.name for item in db.list_media("photos", "newest")] == ["b-newer.jpg", "a-older.jpg"]
    assert [item.name for item in db.list_media("photos", "oldest")] == ["a-older.jpg", "b-newer.jpg"]
    assert [item.name for item in db.list_media("photos", "name")] == ["a-older.jpg", "b-newer.jpg"]


def test_database_delete_path_is_category_scoped(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    shared = tmp_path / "shared.jpg"
    shared.write_bytes(b"img")

    # "pictures" is now a virtual aggregator (Overview) and rejects writes,
    # so the second insertion uses another real category. The test is about
    # (path, category) compound-key behaviour — the specific pair doesn't
    # matter as long as both are real, indexable categories.
    db.upsert_media(path=shared, category="photos", media_type="image", folder="/", thumb_path=None)
    db.upsert_media(path=shared, category="screenshots", media_type="image", folder="/", thumb_path=None)
    db.commit()

    db.delete_path(str(shared), category="photos")

    assert db.list_media("photos") == []
    assert [item.name for item in db.list_media("screenshots")] == ["shared.jpg"]


def test_database_get_media_by_path_is_category_scoped(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    shared = tmp_path / "shared.jpg"
    shared.write_bytes(b"img")

    db.upsert_media(path=shared, category="photos", media_type="image", folder="/", thumb_path="t1.jpg")
    db.upsert_media(path=shared, category="screenshots", media_type="image", folder="/", thumb_path="t2.jpg")
    db.commit()

    item = db.get_media_by_path(str(shared), category="screenshots")
    assert item is not None
    assert item.thumb_path == "t2.jpg"
    assert item.category == "screenshots"


def test_scanner_indexes_media_recursively_and_ignores_non_media(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    root = tmp_path / "Pictures"
    nested = root / "Camera" / "May"
    nested.mkdir(parents=True)
    (root / "cover.jpg").write_bytes(b"image")
    (nested / "clip.mp4").write_bytes(b"video")
    (nested / "notes.txt").write_bytes(b"text")

    scanner = MediaScanner(db, FakeThumbnailer())
    # Use "screenshots" rather than "pictures": the latter is now a virtual
    # aggregator and is intentionally not scannable as its own category.
    scanner.scan([("screenshots", "Screenshots", str(root))])

    # Image categories show only images — videos surface via the dedicated
    # "videos" aggregate category, regardless of which root indexed them.
    images = db.list_media("screenshots", "name")
    assert [item.name for item in images] == ["cover.jpg"]
    assert images[0].folder == "/"
    assert images[0].media_type == "image"

    videos = db.list_media("videos", "name")
    assert [item.name for item in videos] == ["clip.mp4"]
    assert videos[0].folder == "Camera/May"
    assert videos[0].media_type == "video"
    assert videos[0].thumb_path == "thumb://video/clip.mp4"

    # notes.txt was correctly ignored — neither category sees it.
    assert "notes.txt" not in {item.name for item in images + videos}


def test_scanner_can_use_database_from_background_thread(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    root = tmp_path / "Pictures"
    root.mkdir()
    (root / "background.jpg").write_bytes(b"image")
    scanner = MediaScanner(db, FakeThumbnailer())
    error: list[BaseException] = []

    def run_scan() -> None:
        try:
            # Avoid "pictures" — that key now resolves to the Overview
            # aggregator and is not scannable as its own category.
            scanner.scan([("screenshots", "Screenshots", str(root))])
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=run_scan)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert error == []
    assert [item.name for item in db.list_media("screenshots")] == ["background.jpg"]


def test_scanner_prunes_removed_files_only_for_scanned_existing_categories(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite3")
    photos_root = tmp_path / "Photos"
    missing_root = tmp_path / "Missing"
    photos_root.mkdir()
    stale_photo = photos_root / "stale.jpg"
    mounted_later = tmp_path / "sd-card.jpg"
    stale_photo.write_bytes(b"stale")
    mounted_later.write_bytes(b"mounted")

    db.upsert_media(path=stale_photo, category="photos", media_type="image", folder="/", thumb_path=None)
    db.upsert_media(path=mounted_later, category="location:0", media_type="image", folder="/", thumb_path=None)
    db.commit()
    stale_photo.unlink()

    scanner = MediaScanner(db, FakeThumbnailer())
    scanner.scan([("photos", "Photos", str(photos_root)), ("location:0", "SD", str(missing_root))])

    assert db.list_media("photos") == []
    assert [item.name for item in db.list_media("location:0")] == ["sd-card.jpg"]


def test_settings_save_and_load_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    settings = Settings(
        photos_dir="/photos",
        pictures_dir="/pictures",
        videos_dir="/videos",
        screenshots_dir="/screenshots",
        extra_locations=["/run/media/card", "/home/user/Nextcloud"],
        sort_mode="folder",
        theme="dark",
        language="de",
        external_video_player="vlc",
        grid_columns=6,
    )

    settings.save()
    loaded = Settings.load()

    assert loaded == settings
    assert loaded.categories()[-2:] == [
        ("location:0", "card", "/run/media/card"),
        ("location:1", "Nextcloud", "/home/user/Nextcloud"),
    ]
    assert loaded.grid_columns == 6


def test_settings_load_ignores_unknown_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    config.CONFIG_DIR.mkdir(parents=True)
    (config.CONFIG_DIR / "settings.json").write_text('{"theme": "light", "future": true}', encoding="utf-8")

    loaded = Settings.load()

    assert loaded.theme == "light"
    assert not hasattr(loaded, "future")


def test_thumbnailer_uses_stable_hash_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(thumbnails, "THUMB_DIR", tmp_path / "thumbs")
    thumbnailer = Thumbnailer()
    image = tmp_path / "photo.jpg"

    first = thumbnailer.thumb_path_for(image)
    second = thumbnailer.thumb_path_for(image)

    assert first == second
    assert first.parent == tmp_path / "thumbs"
    assert first.suffix == ".jpg"


def test_thumbnailer_clear_recreates_cache_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(thumbnails, "THUMB_DIR", tmp_path / "thumbs")
    thumbnailer = Thumbnailer()
    old_file = thumbnails.THUMB_DIR / "old.jpg"
    old_file.write_bytes(b"old")

    thumbnailer.clear()

    assert thumbnails.THUMB_DIR.exists()
    assert not old_file.exists()


def test_gallery_media_area_is_configured_to_expand() -> None:
    app_source = Path("yaga/app.py").read_text(encoding="utf-8")
    grid_source = Path("yaga/gallery_grid.py").read_text(encoding="utf-8")

    assert "self.grid_view.set_vexpand(True)" in grid_source
    assert "self.scroller.set_vexpand(True)" in grid_source
    assert "content.set_vexpand(True)" in app_source


def test_gallery_grid_defaults_to_four_compact_media_columns() -> None:
    settings = Settings()
    app_source = Path("yaga/app.py").read_text(encoding="utf-8")
    grid_source = Path("yaga/gallery_grid.py").read_text(encoding="utf-8")
    settings_source = Path("yaga/settings_window.py").read_text(encoding="utf-8")

    assert settings.grid_columns == 4
    assert "from .gallery_grid import GalleryGrid" in app_source
    assert "self.grid_view.set_min_columns(columns)" in grid_source
    assert "self.grid_view.set_max_columns(columns)" in grid_source
    assert "gridview.gallery-grid > child" in app_source
    assert "padding: 1px;" in app_source
    assert "Photos per row" in settings_source


def test_gallery_tiles_are_sized_from_available_width() -> None:
    source = Path("yaga/app.py").read_text(encoding="utf-8")

    assert "def _update_tile_size" in source
    assert "cell_size = max(32, scroller_width // columns)" in source  # height only; width handled by homogeneous Box
    assert "min-height: {cell_size}px" in source
    assert "self.gallery_grid.scroller.add_tick_callback(self._on_grid_tick)" in source


def test_gallery_folder_navigation_uses_swipe_without_visible_back_button() -> None:
    source = Path("yaga/app.py").read_text(encoding="utf-8")
    grid_source = Path("yaga/gallery_grid.py").read_text(encoding="utf-8")

    assert "folder_swipe = Gtk.GestureSwipe()" in source
    assert 'folder_swipe.connect("swipe", self._on_folder_swipe)' in source
    assert "swipe = Gtk.GestureSwipe()" in grid_source
    assert 'swipe.connect("swipe", self._on_tile_swipe)' in grid_source
    assert "def _on_tile_swipe" in grid_source
    assert "def _on_folder_swipe" in source
    assert "if velocity_x > 0:" in source
    assert "self._go_back_folder()" in source
    assert "self.back_button.set_visible(False)" in source


def test_nextcloud_folder_open_uses_on_demand_thumbnails() -> None:
    """Opening an NC folder must not trigger a bulk thumbnail download —
    tiles request their own thumbnail lazily when they scroll into view.
    Bulk sync (scan_nc_structure) is reserved for the initial structure scan,
    not per-folder navigation."""
    source = Path("yaga/app.py").read_text(encoding="utf-8")
    grid_source = Path("yaga/gallery_grid.py").read_text(encoding="utf-8")

    # Public entry point the grid calls when it needs a tile's thumbnail.
    assert "def request_nc_thumbnail" in source
    # Worker pool fans out HTTPS thumbnail fetches in parallel.
    assert "_nc_thumb_worker" in source
    # Grid forwards tile-bind events to the requester rather than bulk-loading.
    assert 'getattr(self.owner, "request_nc_thumbnail"' in grid_source
    # When a fetched thumbnail comes back, it updates the row in place.
    assert "update_folder_thumb" in grid_source


def test_gallery_grid_is_split_out_of_app_module() -> None:
    app_source = Path("yaga/app.py").read_text(encoding="utf-8")
    grid_source = Path("yaga/gallery_grid.py").read_text(encoding="utf-8")

    assert "from .gallery_grid import GalleryGrid" in app_source
    assert "class MediaRow" not in app_source
    assert "class GalleryGrid" in grid_source
    assert "class MediaRow" in grid_source


def test_viewer_has_close_header_but_no_navigation_buttons() -> None:
    viewer_source = Path("yaga/viewer.py").read_text(encoding="utf-8")

    assert "Adw.HeaderBar()" in viewer_source
    assert "header.set_show_start_title_buttons(False)" in viewer_source
    assert "header.set_show_end_title_buttons(False)" in viewer_source
    assert "window-close-symbolic" in viewer_source
    assert "header.pack_start(self.delete_button)" in viewer_source
    assert "header.pack_start(self.info_button)" in viewer_source
    assert "header.pack_end(self.rotate_button)" in viewer_source
    assert "header.pack_end(self.edit_button)" in viewer_source
    assert "header.pack_end(self.close_button)" in viewer_source
    assert "go-previous-symbolic" not in viewer_source
    assert "go-next-symbolic" not in viewer_source
    assert "Gtk.GestureSwipe()" in viewer_source
    assert "Gtk.GestureDrag()" in viewer_source
    assert "Gtk.GestureZoom()" in viewer_source
    assert "Gtk.GestureClick()" in viewer_source
    assert "Gtk.PropagationPhase.CAPTURE" in viewer_source
    assert 'self.swipe_gesture.connect("swipe", self._on_swipe)' in viewer_source
    assert 'connect("end", self._on_swipe)' not in viewer_source
    assert 'self.drag_gesture.connect("drag-end", self._on_drag_end)' in viewer_source
    assert 'self.zoom_gesture.connect("scale-changed", self._on_zoom_scale_changed)' in viewer_source
    # The click gesture splits into two phases: "pressed" stamps the start
    # position so a tap-vs-drag decision can be made later, and "released"
    # is the actual click handler that dispatches single-/double-click intent.
    assert 'self.click_gesture.connect("pressed", self._on_viewer_press_begin)' in viewer_source
    assert 'self.click_gesture.connect("released", self._on_viewer_pressed)' in viewer_source
    assert "def _navigate_from_horizontal_motion" in viewer_source
    assert "abs(x) <= abs(y) * 1.8" in viewer_source


def test_viewer_exposes_info_edit_and_delete_actions() -> None:
    viewer_source = Path("yaga/viewer.py").read_text(encoding="utf-8")

    assert 'Gtk.Button.new_from_icon_name("help-about-symbolic")' in viewer_source
    assert 'Gtk.Button.new_from_icon_name("document-edit-symbolic")' in viewer_source
    assert 'Gtk.Button.new_from_icon_name("user-trash-symbolic")' in viewer_source
    assert "self.info_button.set_visible(True)" in viewer_source
    assert "self.edit_button.set_visible(_PIL_OK)" in viewer_source
    assert "self.delete_button.set_visible(True)" in viewer_source
    assert "def _show_info" in viewer_source
    assert "def _enter_edit_mode" in viewer_source
    assert "edit_path = self._current_display_path or item.path" in viewer_source
    assert "edit_item = dataclasses.replace(item, path=edit_path)" in viewer_source
    assert "EditorView(edit_item, self.parent_window._)" in viewer_source
    assert "self.stack.set_visible_child(self._editor)" in viewer_source
    assert 'LOGGER.exception("Could not open editor: %s", exc)' in viewer_source


def test_viewer_disables_own_gestures_in_editor_mode() -> None:
    viewer_source = Path("yaga/viewer.py").read_text(encoding="utf-8")

    assert "def _set_view_gestures_enabled" in viewer_source
    assert "Gtk.PropagationPhase.NONE" in viewer_source
    assert "self._set_view_gestures_enabled(False)" in viewer_source
    assert "self._set_view_gestures_enabled(True)" in viewer_source


def test_navigation_uses_spinner_broken_icon_and_pull_refresh() -> None:
    app_source = Path("yaga/app.py").read_text(encoding="utf-8")
    config_source = Path("yaga/config.py").read_text(encoding="utf-8")

    assert "DEBUG_LOG_PATH" in config_source
    assert "RotatingFileHandler(DEBUG_LOG_PATH" in app_source
    # Refresh icon now lives in the titlebar (top-left) so desktop users
    # have a discoverable refresh action; mobile keeps the gesture below.
    assert "header.pack_start(self.refresh_button)" in app_source
    # Pull-to-refresh moved from "any over-pull" (EventControllerScroll +
    # edge-overshot) to a proper GestureDrag with a release-threshold so
    # the list visibly wobbles before firing the refresh.
    assert "Gtk.GestureDrag.new()" in app_source
    assert "def _on_pull_drag_update" in app_source
    assert "def _on_pull_drag_end" in app_source
    assert "Gtk.Spinner()" in app_source
    assert "network-error-symbolic" in app_source
    assert "set_pixel_size(22)" in app_source
    assert "spinner.set_valign(Gtk.Align.START)" in app_source
    assert "self._nc_broken_img.set_visible(active)" in app_source


def test_settings_search_is_disabled() -> None:
    settings_source = Path("yaga/settings_window.py").read_text(encoding="utf-8")

    assert "self.set_search_enabled(False)" in settings_source


def test_gallery_supports_date_group_sorting_headers() -> None:
    app_source = Path("yaga/app.py").read_text(encoding="utf-8")
    grid_source = Path("yaga/gallery_grid.py").read_text(encoding="utf-8")

    # "date" is a first-class sort key in the dropdown, paired with the
    # ascending/descending direction button via _SORT_TO_INTERNAL.
    assert '"date"' in app_source
    assert '"Date"' in app_source
    assert '_SORT_KEYS' in app_source
    # Date sort produces grouped headers, emitted month-by-month.
    assert "def _render_date_groups" in app_source
    assert "def _append_date_grouped" in app_source
    assert "def _month_header_markup" in app_source
    # Headers flow through the gallery grid's append_header path.
    assert "self.gallery_grid.append_header(" in app_source
    assert "def append_header" in grid_source
    assert "class MediaRow" in grid_source
    assert "header_text" in grid_source
    assert "date-header" in app_source


def test_viewer_supports_pinch_zoom_and_double_tap_reset() -> None:
    viewer_source = Path("yaga/viewer.py").read_text(encoding="utf-8")

    assert "self.zoom_scale = 1.0" in viewer_source
    assert "self.zoom_start_scale = 1.0" in viewer_source
    assert "self.zoom_view.set_size_request(int(width * self.zoom_scale), int(height * self.zoom_scale))" in viewer_source
    assert "self.zoom_view.set_size_request(-1, -1)" in viewer_source
    assert "self._set_zoom(self.zoom_start_scale * scale_delta)" in viewer_source
    assert "gesture.get_bounding_box_center()" in viewer_source
    assert "self._zoom_anchor" in viewer_source
    assert "self._set_adjustment_for_focus(scroller.get_hadjustment(), cx, scale, vp_x)" in viewer_source
    assert "n_press == 2" in viewer_source
    assert "self._reset_zoom()" in viewer_source
    assert "if self.zoom_scale > 1.05:" in viewer_source


def test_viewer_delete_uses_confirmation_and_cleans_index_and_thumbnail() -> None:
    viewer_source = Path("yaga/viewer.py").read_text(encoding="utf-8")

    assert "Adw.AlertDialog" in viewer_source
    assert "Adw.ResponseAppearance.DESTRUCTIVE" in viewer_source
    assert "Gio.File.new_for_path(item.path).trash(None)" in viewer_source
    assert "Path(item.thumb_path).unlink(missing_ok=True)" in viewer_source
    assert "self.parent_window.database.delete_path(item.path, item.category)" in viewer_source
    assert "self.parent_window.refresh(scan=False)" in viewer_source


def test_editor_is_split_out_of_app_module() -> None:
    app_source = Path("yaga/app.py").read_text(encoding="utf-8")
    # The editor became a package; the GTK widget lives in editor/view.py.
    view_source = Path("yaga/editor/view.py").read_text(encoding="utf-8")

    assert "from .editor import EditorView, PILImage, _PIL_OK" not in app_source
    assert "class EditorView" not in app_source
    assert "class EditorView" in view_source
    assert "def __init__(self, item: MediaItem, translate=None)" in view_source
    assert "def _(self, text: str) -> str" in view_source


def test_editor_frames_are_decorative_not_plain_color_bands() -> None:
    from yaga.editor import _FRAME_THEMES, _frame_pil

    frame = _frame_pil(240, 180, "christmas")
    assert frame is not None
    assert frame.getpixel((120, 90))[3] == 0

    edge_pixels = [
        frame.getpixel((x, y))
        for x in range(6, 234, 8)
        for y in list(range(6, 42, 6)) + list(range(138, 174, 6))
        if frame.getpixel((x, y))[3] > 0
    ]
    assert len(set(edge_pixels)) > 4

    # Frame decorators live in editor/frames.py after the split.
    source = Path("yaga/editor/frames.py").read_text(encoding="utf-8")
    assert "_decorate_christmas" in source
    assert "_decorate_winter" in source
    assert len(_FRAME_THEMES) >= 8


def test_editor_has_resettable_sliders_color_picker_and_multiple_stickers() -> None:
    # Editor UI now lives in editor/view.py.
    source = Path("yaga/editor/view.py").read_text(encoding="utf-8")

    assert 'Gtk.Button.new_from_icon_name("edit-undo-symbolic")' in source
    assert "def _reset_slider" in source
    assert "Gtk.ColorButton.new_with_rgba" in source
    assert "def _on_text_color_set" in source
    assert "self._stickers: list[dict]" in source
    assert "self._stickers.append" in source
    assert "for sticker in self._stickers" in source


def test_settings_window_is_split_out_of_app_module() -> None:
    app_source = Path("yaga/app.py").read_text(encoding="utf-8")
    settings_source = Path("yaga/settings_window.py").read_text(encoding="utf-8")

    assert "from .settings_window import SettingsWindow" in app_source
    assert "class SettingsWindow" not in app_source
    assert "class SettingsWindow" in settings_source


def test_viewer_is_split_out_of_app_module() -> None:
    app_source = Path("yaga/app.py").read_text(encoding="utf-8")
    viewer_source = Path("yaga/viewer.py").read_text(encoding="utf-8")

    assert "from .viewer import ViewerWindow" in app_source
    assert "class ViewerWindow" not in app_source
    assert "class ViewerWindow" in viewer_source
    assert "from .editor import EditorView, PILImage, _PIL_OK" in viewer_source
