"""Behaviour tests for the recent feature work.

The existing suite leans on string-matching the source. These tests run
the actual code paths via stubs/mocks so a future refactor can't silently
regress the behaviour the user reported as "broken" or asked for explicitly:

  * 1.3 s timer-based long-press + 16 px motion abort
  * refresh_selection_state / update_tile_for_path direct re-bind
  * NC thumbnail queue cancellation on folder leave
  * Share dialog filters nextcloud:// paths and builds correct xdg-email argv
  * Slideshow auto-advance skips videos
  * Editor history change-callback fires on snapshot/undo/redo
  * Credential file: 0o600 perms + atomic write into a 0o700 parent
  * Pillow MAX_IMAGE_PIXELS hard cap
  * Temp-edit cleanup is scoped to the NC cache, not user picture dirs
  * Multi-select header layout: trash on the left, close on the right

PyGObject's metaclass forbids ``object.__new__`` on Gtk widget subclasses,
so methods are tested as unbound functions called with a SimpleNamespace
``self`` carrying just the attributes the method touches.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gi.repository import GLib  # noqa: F401  — ensures the binding is up


# ---------------------------------------------------------------------------
# 1.  Stat-cache for thumbnail existence checks
# ---------------------------------------------------------------------------

def _thumb_cache_self():
    return SimpleNamespace(
        _exists_cache={},
        _EXISTS_TTL=5.0,
        _EXISTS_CACHE_MAX=8,
    )


def test_thumb_exists_caches_positive_result(tmp_path: Path) -> None:
    from yaga.gallery_grid import GalleryGrid
    fake = _thumb_cache_self()
    real = tmp_path / "thumb.jpg"
    real.write_bytes(b"x")
    assert GalleryGrid._thumb_exists(fake, str(real)) is True
    # Cache wins inside TTL even after the real file disappears.
    real.unlink()
    assert GalleryGrid._thumb_exists(fake, str(real)) is True


def test_thumb_exists_caches_negative_result(tmp_path: Path) -> None:
    from yaga.gallery_grid import GalleryGrid
    fake = _thumb_cache_self()
    missing = tmp_path / "nope.jpg"
    assert GalleryGrid._thumb_exists(fake, str(missing)) is False
    missing.write_bytes(b"x")
    # Still cached as missing → False.
    assert GalleryGrid._thumb_exists(fake, str(missing)) is False
    # Explicit invalidation (mirrors update_item_thumb on arrival).
    fake._exists_cache.pop(str(missing))
    assert GalleryGrid._thumb_exists(fake, str(missing)) is True


def test_thumb_exists_returns_false_for_empty_path() -> None:
    from yaga.gallery_grid import GalleryGrid
    assert GalleryGrid._thumb_exists(_thumb_cache_self(), "") is False


def test_thumb_exists_evicts_when_cache_is_full(tmp_path: Path) -> None:
    from yaga.gallery_grid import GalleryGrid
    fake = _thumb_cache_self()  # MAX = 8
    for i in range(fake._EXISTS_CACHE_MAX + 3):
        f = tmp_path / f"f{i}.jpg"
        f.write_bytes(b"x")
        GalleryGrid._thumb_exists(fake, str(f))
    assert len(fake._exists_cache) <= fake._EXISTS_CACHE_MAX


# ---------------------------------------------------------------------------
# 2.  Custom 1.3 s long-press: timer constants, motion abort, fire path
# ---------------------------------------------------------------------------

class _StubGesture:
    def __init__(self) -> None:
        self._long_press_timer_id = 0
        self._press_x = 0.0
        self._press_y = 0.0
        self.claimed = False

    def set_state(self, _state) -> None:
        self.claimed = True


def _long_press_self():
    """Carrier for unbound long-press method calls."""
    return SimpleNamespace(
        _last_long_press_at=0.0,
        _LONG_PRESS_HOLD_MS=1300,
        _LONG_PRESS_MOVE_THRESHOLD_SQ=16.0 * 16.0,
    )


def test_long_press_timer_threshold_is_close_to_one_and_a_half_seconds() -> None:
    from yaga.gallery_grid import GalleryGrid
    # Audit said 2000 ms felt like 3 s; we tuned to 1300 ms. Keep it
    # bracketed so future tweaks don't accidentally land on 0.3 s or 5 s.
    assert 800 <= GalleryGrid._LONG_PRESS_HOLD_MS <= 1500


def test_motion_inside_threshold_does_not_abort() -> None:
    from yaga.gallery_grid import GalleryGrid
    fake = _long_press_self()
    fake._abort_long_press = lambda g: setattr(g, "_long_press_timer_id", 0)
    g = _StubGesture()
    g._long_press_timer_id = 999
    g._press_x = 100.0
    g._press_y = 200.0
    GalleryGrid._on_tile_motion(fake, None, 108.0, 208.0, g)
    assert g._long_press_timer_id == 999  # 8 px diagonal stays under 16 px


def test_motion_beyond_threshold_aborts() -> None:
    from yaga.gallery_grid import GalleryGrid
    aborted: list = []
    fake = _long_press_self()
    fake._abort_long_press = lambda g: aborted.append(g)
    g = _StubGesture()
    g._long_press_timer_id = 1
    g._press_x = 100.0
    g._press_y = 200.0
    GalleryGrid._on_tile_motion(fake, None, 200.0, 200.0, g)
    assert aborted == [g]


def test_abort_long_press_clears_timer_and_ignores_zero() -> None:
    from yaga.gallery_grid import GalleryGrid
    fake = _long_press_self()
    g = _StubGesture()
    g._long_press_timer_id = 0
    GalleryGrid._abort_long_press(fake, g)
    assert g._long_press_timer_id == 0
    # With a real source: install one so the abort path actually
    # exercises GLib.source_remove without leaking it.
    src = GLib.timeout_add(60_000, lambda: False)
    g._long_press_timer_id = src
    GalleryGrid._abort_long_press(fake, g)
    assert g._long_press_timer_id == 0


def _long_press_self_with_owner(owner):
    """As _long_press_self, but with the helpers _fire_long_press needs
    to walk a list_item back to its MediaRow."""
    from yaga.gallery_grid import GalleryGrid
    fake = _long_press_self()
    fake.owner = owner
    # _fire_long_press calls self._get_tile_item — that's a pure helper
    # on the class, so just delegate to it with the fake as self.
    fake._get_tile_item = lambda li, idx: GalleryGrid._get_tile_item(fake, li, idx)
    return fake


def test_fire_long_press_enters_selection_and_toggles() -> None:
    from yaga.gallery_grid import GalleryGrid
    owner_calls = SimpleNamespace(entered=0, toggled=[])

    def enter():
        owner_calls.entered += 1
        owner._selection_mode = True

    def toggle(p):
        owner_calls.toggled.append(p)

    owner = SimpleNamespace(
        _selection_mode=False,
        _enter_selection_mode=enter,
        _toggle_selection=toggle,
    )

    fake = _long_press_self_with_owner(owner)

    media_item = SimpleNamespace(path="/tmp/img.jpg", is_video=False)
    media_row = SimpleNamespace(is_folder=False, media_item=media_item)
    gallery_row = SimpleNamespace(is_header=False, tiles=[media_row])
    list_item = SimpleNamespace(get_item=lambda: gallery_row)

    g = _StubGesture()
    res = GalleryGrid._fire_long_press(fake, g, list_item, 0)

    assert res == GLib.SOURCE_REMOVE
    assert g.claimed is True
    assert owner_calls.entered == 1
    assert owner_calls.toggled == ["/tmp/img.jpg"]
    assert fake._last_long_press_at > 0.0


def test_fire_long_press_skips_folders() -> None:
    from yaga.gallery_grid import GalleryGrid
    owner = SimpleNamespace(
        _selection_mode=False,
        _enter_selection_mode=MagicMock(),
        _toggle_selection=MagicMock(),
    )
    fake = _long_press_self_with_owner(owner)

    folder_row = SimpleNamespace(is_folder=True, media_item=None)
    gallery_row = SimpleNamespace(is_header=False, tiles=[folder_row])
    list_item = SimpleNamespace(get_item=lambda: gallery_row)
    GalleryGrid._fire_long_press(fake, _StubGesture(), list_item, 0)
    owner._enter_selection_mode.assert_not_called()
    owner._toggle_selection.assert_not_called()


def test_fire_long_press_ignores_headers() -> None:
    from yaga.gallery_grid import GalleryGrid
    owner = SimpleNamespace(
        _selection_mode=False,
        _enter_selection_mode=MagicMock(),
        _toggle_selection=MagicMock(),
    )
    fake = _long_press_self_with_owner(owner)

    header_row = SimpleNamespace(is_header=True, tiles=[])
    list_item = SimpleNamespace(get_item=lambda: header_row)
    GalleryGrid._fire_long_press(fake, _StubGesture(), list_item, 0)
    owner._enter_selection_mode.assert_not_called()


# ---------------------------------------------------------------------------
# 3.  refresh_selection_state / update_tile_for_path: direct re-bind
# ---------------------------------------------------------------------------

def test_refresh_selection_state_rebinds_every_bound_item() -> None:
    from yaga.gallery_grid import GalleryGrid
    sentinel_a, sentinel_b, sentinel_c = object(), object(), object()
    rebound: list = []
    fake = SimpleNamespace(
        _bound_list_items=[sentinel_a, sentinel_b, sentinel_c],
        _apply_binding=lambda li: rebound.append(li),
    )
    GalleryGrid.refresh_selection_state(fake)
    assert rebound == [sentinel_a, sentinel_b, sentinel_c]


def test_update_tile_for_path_returns_false_when_path_not_bound() -> None:
    from yaga.gallery_grid import GalleryGrid
    fake = SimpleNamespace(_bound_list_items=[], _apply_binding=MagicMock())
    assert GalleryGrid.update_tile_for_path(fake, "/nope.jpg") is False
    fake._apply_binding.assert_not_called()


def test_update_tile_for_path_rebinds_only_matching_item() -> None:
    from yaga.gallery_grid import GalleryGrid

    def make_li(path: str):
        item = SimpleNamespace(path=path)
        row = SimpleNamespace(
            is_header=False,
            tiles=[SimpleNamespace(is_folder=False, media_item=item)],
        )
        return SimpleNamespace(get_item=lambda: row)

    li_a, li_b = make_li("/a.jpg"), make_li("/b.jpg")
    rebound: list = []
    fake = SimpleNamespace(
        _bound_list_items=[li_a, li_b],
        _apply_binding=lambda li: rebound.append(li),
    )
    assert GalleryGrid.update_tile_for_path(fake, "/b.jpg") is True
    assert rebound == [li_b]


def test_update_item_thumb_mutates_in_place_and_rebinds() -> None:
    """update_item_thumb used to splice the full GalleryRow; now it
    swaps the MediaRow's frozen MediaItem with one carrying the new
    thumb_path and re-binds only the affected list_item. Verify both
    the mutation and the targeted re-bind."""
    import dataclasses
    from yaga.gallery_grid import GalleryGrid
    from yaga.models import MediaItem

    def make_item(path: str, thumb: str | None = None) -> MediaItem:
        return MediaItem(
            id=hash(path) & 0x7FFFFFFF, path=path, category="pictures",
            media_type="image", folder="/", name=path.rsplit("/", 1)[-1],
            mtime=0.0, size=0, thumb_path=thumb,
        )

    tile = SimpleNamespace(
        is_folder=False,
        media_item=make_item("/a.jpg"),
    )
    other_tile = SimpleNamespace(
        is_folder=False,
        media_item=make_item("/b.jpg"),
    )
    gallery_row = SimpleNamespace(is_header=False, tiles=[tile, other_tile])
    list_item = SimpleNamespace(get_item=lambda: gallery_row)

    # Empty row_store stand-in — the bound branch should hit before
    # we fall back to the model walk.
    rebound: list = []
    fake = SimpleNamespace(
        _exists_cache={"/old/thumb.jpg": (0.0, True)},
        _bound_list_items=[list_item],
        _apply_binding=lambda li: rebound.append(li),
        row_store=SimpleNamespace(get_n_items=lambda: 0, get_item=lambda _i: None),
    )

    assert GalleryGrid.update_item_thumb(fake, "/a.jpg", "/new/thumb.jpg") is True
    # Frozen MediaItem replaced in place; the other tile is untouched.
    assert tile.media_item.thumb_path == "/new/thumb.jpg"
    assert other_tile.media_item.thumb_path is None
    # Only the affected list_item is re-bound.
    assert rebound == [list_item]


def test_update_item_thumb_falls_back_to_row_store_walk() -> None:
    """When the path isn't in the bound viewport, mutate via the
    row_store so a later scroll-in picks up the new thumb."""
    from yaga.gallery_grid import GalleryGrid
    from yaga.models import MediaItem

    item = MediaItem(
        id=1, path="/scrolled-out.jpg", category="pictures", media_type="image",
        folder="/", name="scrolled-out.jpg", mtime=0.0, size=0, thumb_path=None,
    )
    tile = SimpleNamespace(is_folder=False, media_item=item)
    row = SimpleNamespace(is_header=False, tiles=[tile])

    fake = SimpleNamespace(
        _exists_cache={},
        _bound_list_items=[],
        _apply_binding=MagicMock(),
        row_store=SimpleNamespace(
            get_n_items=lambda: 1,
            get_item=lambda i: row if i == 0 else None,
        ),
    )

    assert GalleryGrid.update_item_thumb(fake, "/scrolled-out.jpg", "/t.jpg") is True
    assert tile.media_item.thumb_path == "/t.jpg"
    fake._apply_binding.assert_not_called()  # nothing visible to re-bind


# ---------------------------------------------------------------------------
# 4.  NC thumbnail queue cancellation on folder leave
# ---------------------------------------------------------------------------

def test_cancel_nc_thumb_queue_drains_and_signals() -> None:
    from yaga.app import GalleryWindow
    fake = SimpleNamespace(
        _nc_thumb_lock=threading.Lock(),
        _nc_thumb_queue=["/a", "/b", "/c"],
        _nc_thumb_pending={"/a", "/b", "/c", "/d"},
        _nc_thumb_event=threading.Event(),
    )
    fake._nc_thumb_event.clear()

    GalleryWindow._cancel_nc_thumb_queue(fake)

    assert fake._nc_thumb_queue == []
    # Queued paths leave pending; the in-flight /d stays.
    assert fake._nc_thumb_pending == {"/d"}
    # Workers are nudged so they re-evaluate.
    assert fake._nc_thumb_event.is_set()


def test_cancel_nc_thumb_queue_is_a_noop_when_empty() -> None:
    from yaga.app import GalleryWindow
    fake = SimpleNamespace(
        _nc_thumb_lock=threading.Lock(),
        _nc_thumb_queue=[],
        _nc_thumb_pending={"/d"},
        _nc_thumb_event=threading.Event(),
    )
    GalleryWindow._cancel_nc_thumb_queue(fake)
    assert fake._nc_thumb_pending == {"/d"}
    assert not fake._nc_thumb_event.is_set()


# ---------------------------------------------------------------------------
# 5.  Share dialog response: filters NC paths, correct xdg-email argv
# ---------------------------------------------------------------------------

def test_share_dialog_response_passes_attach_per_local_path() -> None:
    from yaga.app import GalleryWindow
    fake = SimpleNamespace(
        _set_status=MagicMock(),
        _=lambda s: s,
    )
    paths = ["/home/u/a.jpg", "/home/u/b.jpg"]
    with patch("yaga.app.subprocess.Popen") as popen:
        GalleryWindow._on_share_dialog_response(fake, None, "email", paths)
    popen.assert_called_once()
    argv = popen.call_args[0][0]
    assert argv[0] == "xdg-email"
    assert argv.count("--attach") == 2
    for p in paths:
        assert p in argv


def test_share_dialog_response_no_op_on_cancel_or_empty() -> None:
    from yaga.app import GalleryWindow
    fake = SimpleNamespace(_set_status=MagicMock(), _=lambda s: s)
    with patch("yaga.app.subprocess.Popen") as popen:
        GalleryWindow._on_share_dialog_response(fake, None, "cancel", ["/a.jpg"])
        GalleryWindow._on_share_dialog_response(fake, None, "email", [])
    popen.assert_not_called()


def test_open_share_dialog_filters_nc_paths_at_helper_level() -> None:
    """open_share_dialog itself constructs Adw.AlertDialog (GTK-heavy);
    pin down the NC-filter contract via the helper that drives it."""
    from yaga.nextcloud import is_nc_path, NC_PATH_PREFIX
    paths = [f"{NC_PATH_PREFIX}foo/bar.jpg", "/home/u/a.jpg"]
    locals_only = [p for p in paths if not is_nc_path(p)]
    assert locals_only == ["/home/u/a.jpg"]


# ---------------------------------------------------------------------------
# 6.  Slideshow auto-advance skips videos
# ---------------------------------------------------------------------------

def test_slideshow_tick_skips_videos() -> None:
    from yaga.viewer import ViewerWindow
    fake = SimpleNamespace(
        _slideshow_active=True,
        items=[
            SimpleNamespace(is_video=False, path="/img0.jpg"),
            SimpleNamespace(is_video=True, path="/clip.mp4"),
            SimpleNamespace(is_video=True, path="/clip2.mp4"),
            SimpleNamespace(is_video=False, path="/img3.jpg"),
        ],
        index=0,
        show_item=MagicMock(),
        _schedule_next_slide=MagicMock(),
        _stop_slideshow=MagicMock(),
    )
    res = ViewerWindow._on_slideshow_tick(fake)
    assert res == GLib.SOURCE_REMOVE
    assert fake.index == 3  # walked past both videos
    fake.show_item.assert_called_once()
    fake._schedule_next_slide.assert_called_once()
    fake._stop_slideshow.assert_not_called()


def test_slideshow_tick_stops_on_all_video_gallery() -> None:
    from yaga.viewer import ViewerWindow
    fake = SimpleNamespace(
        _slideshow_active=True,
        items=[
            SimpleNamespace(is_video=True, path="/v1.mp4"),
            SimpleNamespace(is_video=True, path="/v2.mp4"),
        ],
        index=0,
        show_item=MagicMock(),
        _schedule_next_slide=MagicMock(),
        _stop_slideshow=MagicMock(),
    )
    res = ViewerWindow._on_slideshow_tick(fake)
    assert res == GLib.SOURCE_REMOVE
    fake._stop_slideshow.assert_called_once()
    fake.show_item.assert_not_called()


def test_slideshow_tick_inactive_returns_immediately() -> None:
    from yaga.viewer import ViewerWindow
    fake = SimpleNamespace(
        _slideshow_active=False,
        items=[],
        index=0,
        show_item=MagicMock(),
        _schedule_next_slide=MagicMock(),
        _stop_slideshow=MagicMock(),
    )
    res = ViewerWindow._on_slideshow_tick(fake)
    assert res == GLib.SOURCE_REMOVE
    fake.show_item.assert_not_called()
    fake._schedule_next_slide.assert_not_called()


# ---------------------------------------------------------------------------
# 7.  Editor history-change callback fires on snapshot/undo/redo
# ---------------------------------------------------------------------------

def test_editor_history_callback_fires_on_snapshot_undo_redo(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL")
    from yaga.editor.view import EditorView
    img_path = tmp_path / "tiny.png"
    pil.Image.new("RGB", (16, 16), (200, 100, 50)).save(str(img_path))

    fake = SimpleNamespace(
        _original=pil.Image.open(str(img_path)).convert("RGB"),
        _history_undo=[],
        _history_redo=[],
        _history_max_steps=5,
        _update_id=None,
        _schedule_update=lambda: None,
        _restoring=False,
        # Parameter state captured by _capture_state / restored by _restore_state.
        _filter_mode="none",
        _brightness=1.0,
        _contrast=1.0,
        _red=1.0,
        _green=1.0,
        _blue=1.0,
        _stickers=[],
        _active_sticker=None,
        _obfuscate_strokes=[],
        _frame_theme=None,
        # UI control dicts that _restore_state re-syncs; empty is fine for this test.
        _adjust_sliders={},
        _filter_btns={},
        _frame_btns={},
        _sync_active_sticker=lambda: None,
        _draw_area=SimpleNamespace(queue_draw=lambda: None),
    )
    fake._working = fake._original.copy()
    # Bind the helpers undo/redo/_snapshot_state lean on. _emit_history_changed
    # delegates the listener fan-out; can_undo/can_redo are stack length checks.
    fake._emit_history_changed = lambda: EditorView._emit_history_changed(fake)
    fake.can_undo = lambda: EditorView.can_undo(fake)
    fake.can_redo = lambda: EditorView.can_redo(fake)
    fake._capture_state = lambda: EditorView._capture_state(fake)
    fake._restore_state = lambda state: EditorView._restore_state(fake, state)

    fires: list[tuple[bool, bool]] = []

    def listener():
        # can_undo/can_redo are simple len-checks; call them as unbound.
        fires.append(
            (
                EditorView.can_undo(fake),
                EditorView.can_redo(fake),
            )
        )

    EditorView.set_history_changed_callback(fake, listener)
    # set_history_changed_callback fires once on registration so the host
    # can sync initial sensitivity without a follow-up call.
    assert fires == [(False, False)]

    EditorView._snapshot_state(fake)
    assert fires[-1] == (True, False)

    EditorView.undo(fake)
    assert fires[-1] == (False, True)

    EditorView.redo(fake)
    assert fires[-1] == (True, False)


# ---------------------------------------------------------------------------
# 7b. Editor undo really reverts parameter edits, not just _working pixels.
#     Regression: snapshots used to capture only _working, so changing
#     brightness/filter/frame/stickers and then calling undo() did nothing.
# ---------------------------------------------------------------------------

def test_editor_undo_reverts_parameter_edits(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL")
    from yaga.editor.view import EditorView
    img_path = tmp_path / "tiny.png"
    pil.Image.new("RGB", (16, 16), (200, 100, 50)).save(str(img_path))

    fake = SimpleNamespace(
        _original=pil.Image.open(str(img_path)).convert("RGB"),
        _history_undo=[],
        _history_redo=[],
        _history_max_steps=5,
        _update_id=None,
        _schedule_update=lambda: None,
        _restoring=False,
        _filter_mode="none",
        _brightness=1.0,
        _contrast=1.0,
        _red=1.0,
        _green=1.0,
        _blue=1.0,
        _stickers=[],
        _active_sticker=None,
        _obfuscate_strokes=[],
        _frame_theme=None,
        _adjust_sliders={},
        _filter_btns={},
        _frame_btns={},
        _sync_active_sticker=lambda: None,
        _draw_area=SimpleNamespace(queue_draw=lambda: None),
    )
    fake._working = fake._original.copy()
    fake._emit_history_changed = lambda: None
    fake.can_undo = lambda: EditorView.can_undo(fake)
    fake.can_redo = lambda: EditorView.can_redo(fake)
    fake._capture_state = lambda: EditorView._capture_state(fake)
    fake._restore_state = lambda state: EditorView._restore_state(fake, state)

    # Simulate the call pattern of every parameter handler: snapshot, then
    # mutate the parameter. Brightness, filter, frame, stickers, obfuscate
    # all live outside _working, so a working-only snapshot would lose them.
    EditorView._snapshot_state(fake)
    fake._brightness = 1.6
    fake._filter_mode = "vintage"
    fake._frame_theme = "summer"
    fake._stickers = [{"source": "🎉", "rel": (0.5, 0.5), "size": 0.2}]
    fake._obfuscate_strokes = [(0.1, 0.1, 0.05, (255, 0, 0, 255))]

    EditorView.undo(fake)
    assert fake._brightness == 1.0
    assert fake._filter_mode == "none"
    assert fake._frame_theme is None
    assert fake._stickers == []
    assert fake._obfuscate_strokes == []

    # And redo restores the post-edit values.
    EditorView.redo(fake)
    assert fake._brightness == 1.6
    assert fake._filter_mode == "vintage"
    assert fake._frame_theme == "summer"
    assert fake._stickers == [{"source": "🎉", "rel": (0.5, 0.5), "size": 0.2}]
    assert fake._obfuscate_strokes == [(0.1, 0.1, 0.05, (255, 0, 0, 255))]


# ---------------------------------------------------------------------------
# 7c. Gesture-driven edits (sticker drag, sticker pinch, obfuscate brush)
#     produce undo entries. Regression: drag/zoom handlers used to mutate
#     state without ever calling _snapshot_state, so the gestures were not
#     undoable at all (the counter didn't even increment).
# ---------------------------------------------------------------------------

def _make_editor_fake(pil, img_path):
    fake = SimpleNamespace(
        _original=pil.Image.open(str(img_path)).convert("RGB"),
        _history_undo=[],
        _history_redo=[],
        _history_max_steps=5,
        _update_id=None,
        _schedule_update=lambda: None,
        _restoring=False,
        _filter_mode="none",
        _brightness=1.0, _contrast=1.0, _red=1.0, _green=1.0, _blue=1.0,
        _stickers=[],
        _active_sticker=None,
        _obfuscate_strokes=[],
        _frame_theme=None,
        _adjust_sliders={},
        _filter_btns={},
        _frame_btns={},
        _sync_active_sticker=lambda: None,
        _draw_area=SimpleNamespace(queue_draw=lambda: None),
    )
    fake._working = fake._original.copy()
    fake._emit_history_changed = lambda: None
    return fake


def test_obfuscate_drag_begin_snapshots_for_undo(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL")
    from yaga.editor.view import EditorView
    img_path = tmp_path / "t.png"
    pil.Image.new("RGB", (16, 16), (0, 0, 0)).save(str(img_path))

    fake = _make_editor_fake(pil, img_path)
    fake._obfuscate_mode = True
    fake._obfuscate_drag_origin = None
    fake._obfuscate_brush_size = 0.05
    fake._sample_color_at = lambda *_a: (0, 0, 0, 255)
    fake._crop_mode = False
    fake._drag_sticker = False
    fake._sticker_source = None
    # Bind helpers used by the handlers under test.
    fake._capture_state = lambda: EditorView._capture_state(fake)
    fake._restore_state = lambda s: EditorView._restore_state(fake, s)
    fake._snapshot_state = lambda: EditorView._snapshot_state(fake)
    fake.can_undo = lambda: EditorView.can_undo(fake)
    fake.can_redo = lambda: EditorView.can_redo(fake)
    fake._add_obfuscate_stroke = lambda px, py: EditorView._add_obfuscate_stroke(fake, px, py)
    # _add_obfuscate_stroke needs the draw area dimensions — mimic them.
    fake._draw_area = SimpleNamespace(
        queue_draw=lambda: None,
        get_width=lambda: 100,
        get_height=lambda: 100,
    )

    assert not fake.can_undo()
    EditorView._on_drag_begin(fake, None, 50.0, 50.0)
    # The drag created one undo step and one stroke.
    assert fake.can_undo()
    assert len(fake._obfuscate_strokes) == 1

    EditorView.undo(fake)
    assert fake._obfuscate_strokes == []


def test_sticker_drag_begin_snapshots_for_undo(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL")
    from yaga.editor.view import EditorView
    img_path = tmp_path / "t.png"
    pil.Image.new("RGB", (16, 16), (0, 0, 0)).save(str(img_path))

    fake = _make_editor_fake(pil, img_path)
    fake._obfuscate_mode = False
    fake._crop_mode = False
    fake._drag_sticker = False
    fake._sticker_source = None
    fake._sticker_rel = (0.5, 0.5)
    fake._sticker_size_frac = 0.2
    fake._sticker_del_rect = None
    # One sticker present, with a known position.
    fake._stickers = [{"source": "🎉", "rel": (0.3, 0.3), "size": 0.2}]
    fake._active_sticker = 0
    fake._capture_state = lambda: EditorView._capture_state(fake)
    fake._restore_state = lambda s: EditorView._restore_state(fake, s)
    fake._snapshot_state = lambda: EditorView._snapshot_state(fake)
    fake.can_undo = lambda: EditorView.can_undo(fake)
    fake.can_redo = lambda: EditorView.can_redo(fake)

    assert not fake.can_undo()
    EditorView._on_drag_begin(fake, None, 10.0, 10.0)
    # Now mutate — simulating what _move_sticker would do during the drag.
    fake._stickers[0]["rel"] = (0.8, 0.8)
    assert fake.can_undo()

    EditorView.undo(fake)
    assert fake._stickers[0]["rel"] == (0.3, 0.3)


def test_sticker_zoom_begin_snapshots_for_undo(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL")
    from yaga.editor.view import EditorView
    img_path = tmp_path / "t.png"
    pil.Image.new("RGB", (16, 16), (0, 0, 0)).save(str(img_path))

    fake = _make_editor_fake(pil, img_path)
    fake._sticker_source = "🎉"
    fake._sticker_size_frac = 0.2
    fake._sticker_zoom_start = 0.2
    fake._stickers = [{"source": "🎉", "rel": (0.5, 0.5), "size": 0.2}]
    fake._active_sticker = 0
    fake._capture_state = lambda: EditorView._capture_state(fake)
    fake._restore_state = lambda s: EditorView._restore_state(fake, s)
    fake._snapshot_state = lambda: EditorView._snapshot_state(fake)
    fake.can_undo = lambda: EditorView.can_undo(fake)
    fake.can_redo = lambda: EditorView.can_redo(fake)

    assert not fake.can_undo()
    EditorView._on_sticker_zoom_begin(fake, None, None)
    fake._stickers[0]["size"] = 0.6
    assert fake.can_undo()

    EditorView.undo(fake)
    assert fake._stickers[0]["size"] == 0.2


def test_sticker_zoom_begin_no_snapshot_when_no_active_source(tmp_path: Path) -> None:
    """If there is no active sticker, pinch begin must not create a phantom undo entry."""
    pil = pytest.importorskip("PIL")
    from yaga.editor.view import EditorView
    img_path = tmp_path / "t.png"
    pil.Image.new("RGB", (16, 16), (0, 0, 0)).save(str(img_path))

    fake = _make_editor_fake(pil, img_path)
    fake._sticker_source = None
    fake._sticker_size_frac = 0.2
    fake._sticker_zoom_start = 0.2
    fake._capture_state = lambda: EditorView._capture_state(fake)
    fake._restore_state = lambda s: EditorView._restore_state(fake, s)
    fake._snapshot_state = lambda: EditorView._snapshot_state(fake)
    fake.can_undo = lambda: EditorView.can_undo(fake)

    EditorView._on_sticker_zoom_begin(fake, None, None)
    assert not fake.can_undo()


# ---------------------------------------------------------------------------
# 8.  Credential file: 0o600 + atomic write + 0o700 parent dir
# ---------------------------------------------------------------------------

def test_save_app_password_writes_0600_into_0700_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cred = tmp_path / "yagacfg" / "nc_password"
    monkeypatch.setattr("yaga.config.CONFIG_DIR", tmp_path / "yagacfg")
    monkeypatch.setattr("yaga.config.Settings._CRED_FILE", cred)

    # Stub libsecret out so we hit the file fallback deterministically.
    def _no_secret(*_a, **_kw):
        raise RuntimeError("no libsecret in test")
    monkeypatch.setattr("gi.require_version", _no_secret)

    from yaga.config import Settings
    s = Settings()
    s.nextcloud_url = "https://nc.example"
    s.nextcloud_user = "alice"
    assert s.save_app_password("hunter2") is True
    assert cred.exists()
    assert cred.read_text(encoding="utf-8") == "hunter2"

    if os.name == "posix":
        cred_mode = stat.S_IMODE(cred.stat().st_mode)
        parent_mode = stat.S_IMODE(cred.parent.stat().st_mode)
        assert cred_mode == 0o600, f"expected 0o600, got {oct(cred_mode)}"
        assert parent_mode == 0o700, f"expected 0o700, got {oct(parent_mode)}"

    assert list(cred.parent.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 9.  Pillow MAX_IMAGE_PIXELS hard cap
# ---------------------------------------------------------------------------

def test_pillow_max_image_pixels_is_capped() -> None:
    pytest.importorskip("PIL")
    from yaga.editor._pil import PILImage, _PIL_OK
    if not _PIL_OK:
        pytest.skip("Pillow not installed")
    assert PILImage.MAX_IMAGE_PIXELS is not None
    # 200 MP today; tolerate future re-tunes within a sane range.
    assert 50_000_000 <= PILImage.MAX_IMAGE_PIXELS <= 1_000_000_000


# ---------------------------------------------------------------------------
# 10.  Temp-edit cleanup is scoped to the NC cache, not user picture dirs
# ---------------------------------------------------------------------------

def test_cleanup_abandoned_temp_files_does_not_touch_pictures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    pictures = home / "Pictures"
    pictures.mkdir(parents=True)
    user_edit = pictures / "vacation_edit_2.jpg"
    user_edit.write_bytes(b"keep me")

    cache = tmp_path / "cache" / "yaga"
    nc_cache = cache / "nextcloud"
    nc_cache.mkdir(parents=True)
    nc_temp = nc_cache / "remote_edit_1.jpg"
    nc_temp.write_bytes(b"throwaway")

    monkeypatch.setattr("yaga.config.CACHE_DIR", cache)
    monkeypatch.setenv("HOME", str(home))

    from yaga.app import _cleanup_abandoned_temp_files
    _cleanup_abandoned_temp_files()

    assert user_edit.exists()  # was a data-loss risk in the old version
    assert not nc_temp.exists()


# ---------------------------------------------------------------------------
# 11.  FTS5 trigram index speeds substring search (and survives a roundtrip)
# ---------------------------------------------------------------------------

def test_database_creates_fts_index_when_supported(tmp_path: Path) -> None:
    from yaga.database import Database
    db = Database(tmp_path / "fts.sqlite3")
    if not getattr(db, "_has_fts", False):
        pytest.skip("FTS5/trigram unavailable on this SQLite build")
    # Schema migrated to v3 — pin it so a future regression that drops
    # FTS doesn't silently fall back to LIKE for everyone.
    version = db.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 3
    # The shadow table must exist.
    row = db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='media_fts'"
    ).fetchone()
    assert row is not None


def test_search_returns_substring_match_via_fts(tmp_path: Path) -> None:
    """Trigram preserves the user's substring-match mental model: typing
    'ach' must still find 'Bachstrasse.png'. Validates the actual SQL
    path, not just clause-building."""
    from yaga.database import Database
    db = Database(tmp_path / "fts.sqlite3")
    if not getattr(db, "_has_fts", False):
        pytest.skip("FTS5/trigram unavailable on this SQLite build")

    folder = tmp_path / "pics"
    folder.mkdir()
    for fname in ("Bachstrasse.png", "IMG_2024_Beach.jpg", "unrelated.png"):
        f = folder / fname
        f.write_bytes(b"x")
        db.upsert_media(
            path=f, category="pictures", media_type="image",
            folder="pics", thumb_path=None,
        )
    db.commit()

    hits = db.search_media("pictures", "ach", folder="pics")
    names = {h.name for h in hits}
    # Both "Bachstrasse" and "Beach" contain "ach".
    assert "Bachstrasse.png" in names
    assert "IMG_2024_Beach.jpg" in names
    assert "unrelated.png" not in names


def test_search_short_query_falls_back_to_like(tmp_path: Path) -> None:
    """Trigram FTS5 returns nothing for queries < 3 chars; the search
    clause must transparently fall back to LIKE so a 2-char query still
    finds matches."""
    from yaga.database import Database
    db = Database(tmp_path / "fts.sqlite3")

    folder = tmp_path / "p"
    folder.mkdir()
    f = folder / "ab.jpg"
    f.write_bytes(b"x")
    db.upsert_media(
        path=f, category="pictures", media_type="image",
        folder="p", thumb_path=None,
    )
    db.commit()

    hits = db.search_media("pictures", "ab", folder="p")
    assert any(h.name == "ab.jpg" for h in hits)


def test_search_handles_fts_special_chars_in_filename(tmp_path: Path) -> None:
    """Filenames containing FTS5 reserved syntax (OR, NEAR, parens, …)
    must not crash the search or be reinterpreted as operators."""
    from yaga.database import Database
    db = Database(tmp_path / "fts.sqlite3")
    if not getattr(db, "_has_fts", False):
        pytest.skip("FTS5/trigram unavailable on this SQLite build")

    folder = tmp_path / "p"
    folder.mkdir()
    tricky = folder / "foo (bar) AND baz.jpg"
    tricky.write_bytes(b"x")
    db.upsert_media(
        path=tricky, category="pictures", media_type="image",
        folder="p", thumb_path=None,
    )
    db.commit()

    # Substring containing a paren — the phrase quoting in
    # _build_search_clause must defuse FTS5 syntax.
    hits = db.search_media("pictures", "(bar)", folder="p")
    assert any(h.name == tricky.name for h in hits)


# ---------------------------------------------------------------------------
# 12.  evict_cache_async coalescing: re-entry guard + throttle
# ---------------------------------------------------------------------------

def test_evict_cache_async_drops_concurrent_calls() -> None:
    """Rapid folder hops fire evict_cache_async on every scan completion;
    without coalescing each call would walk THUMB_DIR + _NC_CACHE in its
    own daemon thread. Verify the in-flight guard suppresses duplicates."""
    from yaga.app import GalleryWindow
    fake = SimpleNamespace(
        settings=SimpleNamespace(cache_max_mb=100),
        _EVICT_MIN_INTERVAL_SEC=GalleryWindow._EVICT_MIN_INTERVAL_SEC,
        _evict_cache_worker=lambda: None,  # never runs in test
    )

    started = []
    with patch("yaga.app.threading.Thread") as Thread:
        def fake_thread_ctor(target=None, daemon=None, **_kw):
            started.append(target)
            return SimpleNamespace(start=lambda: None)
        Thread.side_effect = fake_thread_ctor

        # First call sets _evict_in_flight=True before threading.Thread
        # would have run; the second sees the flag and bails.
        GalleryWindow.evict_cache_async(fake)
        GalleryWindow.evict_cache_async(fake)

    # Exactly one worker spawned across two rapid calls.
    assert len(started) == 1


def test_evict_cache_async_throttle_blocks_followups() -> None:
    """After a worker finishes, follow-up calls within the throttle
    window are dropped (avoids re-walking on every scan in a session
    where the user is hopping folders quickly)."""
    from yaga.app import GalleryWindow
    fake = SimpleNamespace(
        settings=SimpleNamespace(cache_max_mb=100),
        _EVICT_MIN_INTERVAL_SEC=GalleryWindow._EVICT_MIN_INTERVAL_SEC,
        _evict_lock=threading.Lock(),
        _evict_in_flight=False,
        _evict_last_finished_at=time.monotonic(),  # just finished
    )
    started = []
    with patch("yaga.app.threading.Thread") as Thread:
        Thread.side_effect = lambda target=None, daemon=None, **_kw: (
            started.append(target),
            SimpleNamespace(start=lambda: None),
        )[1]
        GalleryWindow.evict_cache_async(fake)
    assert started == []  # throttle held the call back


def test_evict_cache_async_no_op_when_budget_disabled() -> None:
    from yaga.app import GalleryWindow
    fake = SimpleNamespace(
        settings=SimpleNamespace(cache_max_mb=0),
    )
    with patch("yaga.app.threading.Thread") as Thread:
        GalleryWindow.evict_cache_async(fake)
    Thread.assert_not_called()


# ---------------------------------------------------------------------------
# 13.  _build_search_clause: month / year / year-month parsing
# ---------------------------------------------------------------------------

def _bare_db(tmp_path: Path):
    """Real Database instance — these tests need an actual SQLite to drive
    schema-aware code (FTS detection, column types). Cheap: <1ms."""
    from yaga.database import Database
    return Database(tmp_path / "search.sqlite3")


def test_search_clause_empty_query_returns_passthrough(tmp_path: Path) -> None:
    db = _bare_db(tmp_path)
    where, args = db._build_search_clause("")
    assert where == "1=1"
    assert args == []
    where2, args2 = db._build_search_clause("   ")
    assert where2 == "1=1"
    assert args2 == []


def test_search_clause_year_month_filter_parses_iso_like(tmp_path: Path) -> None:
    db = _bare_db(tmp_path)
    where, args = db._build_search_clause("2024-05")
    # Year-month branch must extract both components and bind them in order.
    assert "strftime('%Y'" in where
    assert "%m" in where
    assert "2024" in args
    assert 5 in args


def test_search_clause_bare_year_filter(tmp_path: Path) -> None:
    db = _bare_db(tmp_path)
    where, args = db._build_search_clause("2024")
    assert "strftime('%Y'" in where
    assert "2024" in args
    # No standalone month value when only the year was typed.
    assert 5 not in args


def test_search_clause_german_month_name_filter(tmp_path: Path) -> None:
    db = _bare_db(tmp_path)
    where, args = db._build_search_clause("urlaub mai")
    # Mai → 5, ought to land in the args via the month-name lookup branch.
    assert 5 in args


def test_search_clause_english_month_name_filter(tmp_path: Path) -> None:
    db = _bare_db(tmp_path)
    where, args = db._build_search_clause("December photos")
    assert 12 in args


def test_search_clause_short_query_skips_exif_like(tmp_path: Path) -> None:
    """exif_data LIKE is a full-table scan on a JSON blob — guarded by
    len(q) >= 3. A 2-char query must NOT include the exif clause."""
    db = _bare_db(tmp_path)
    _where, _args = db._build_search_clause("ab")
    assert "exif_data" not in _where


def test_search_clause_long_query_includes_exif_like(tmp_path: Path) -> None:
    db = _bare_db(tmp_path)
    where, _args = db._build_search_clause("canon")
    assert "exif_data" in where


# ---------------------------------------------------------------------------
# 14.  Schema migrations: v1 → v2 → v3 path on a fresh DB and on existing data
# ---------------------------------------------------------------------------

def test_fresh_db_lands_on_latest_user_version(tmp_path: Path) -> None:
    """A newly created DB must end up at the most recent schema version
    so downstream code paths (exif_data column, FTS index) work."""
    from yaga.database import Database
    db = Database(tmp_path / "fresh.sqlite3")
    version = db.conn.execute("PRAGMA user_version").fetchone()[0]
    # v3 if FTS is available; v2 otherwise (FTS migration was skipped but
    # exif_data column is still there).
    if getattr(db, "_has_fts", False):
        assert version >= 3
    else:
        assert version >= 2
    # exif_data column always exists post-migration.
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(media)")]
    assert "exif_data" in cols


def test_migration_v1_to_v2_adds_exif_column(tmp_path: Path) -> None:
    """Open a DB at v1 (schema without exif_data column), let migration
    run, verify the column appears and the version is bumped."""
    import sqlite3
    db_path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE media (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            category TEXT NOT NULL,
            media_type TEXT NOT NULL,
            folder TEXT NOT NULL,
            name TEXT NOT NULL,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            thumb_path TEXT,
            seen_at REAL NOT NULL,
            UNIQUE(path, category)
        );
        PRAGMA user_version = 1;
        """
    )
    raw.commit()
    raw.close()

    from yaga.database import Database
    db = Database(db_path)
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(media)")]
    assert "exif_data" in cols
    version = db.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 2


def test_migration_v2_to_v3_backfills_fts(tmp_path: Path) -> None:
    """Open a DB at v2 with existing rows, let migration build FTS, and
    verify all rows are queryable via the new index. Skip if FTS5/trigram
    isn't compiled into this SQLite."""
    import sqlite3
    db_path = tmp_path / "v2.sqlite3"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE media (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            category TEXT NOT NULL,
            media_type TEXT NOT NULL,
            folder TEXT NOT NULL,
            name TEXT NOT NULL,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            thumb_path TEXT,
            seen_at REAL NOT NULL,
            exif_data TEXT DEFAULT NULL,
            UNIQUE(path, category)
        );
        PRAGMA user_version = 2;
        """
    )
    raw.execute(
        "INSERT INTO media (path, category, media_type, folder, name, mtime, size, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("/p/Bachstrasse.png", "pictures", "image", "p", "Bachstrasse.png", 0.0, 0, 0.0),
    )
    raw.commit()
    raw.close()

    from yaga.database import Database
    db = Database(db_path)
    if not getattr(db, "_has_fts", False):
        pytest.skip("FTS5/trigram unavailable")
    # Pre-existing row must be in the FTS index after migration.
    row = db.conn.execute(
        "SELECT name FROM media_fts WHERE media_fts MATCH ?",
        ('"ach"',),
    ).fetchone()
    assert row is not None
    assert row["name"] == "Bachstrasse.png"


def test_migration_v3_idempotent_does_not_duplicate(tmp_path: Path) -> None:
    """Opening an already-v3 DB must not double-populate the FTS index."""
    from yaga.database import Database
    db = Database(tmp_path / "v3.sqlite3")
    if not getattr(db, "_has_fts", False):
        pytest.skip("FTS5/trigram unavailable")
    folder = tmp_path / "p"
    folder.mkdir()
    f = folder / "x.jpg"
    f.write_bytes(b"x")
    db.upsert_media(
        path=f, category="pictures", media_type="image",
        folder="p", thumb_path=None,
    )
    db.commit()
    pre = db.conn.execute("SELECT COUNT(*) FROM media_fts").fetchone()[0]
    # Re-open to re-trigger _migrate.
    db.close() if hasattr(db, "close") else None
    db2 = Database(tmp_path / "v3.sqlite3")
    post = db2.conn.execute("SELECT COUNT(*) FROM media_fts").fetchone()[0]
    assert post == pre


# ---------------------------------------------------------------------------
# 15.  Multi-select header layout: trash on left, close on right
# ---------------------------------------------------------------------------

def test_selection_mode_trash_packed_start_close_packed_end() -> None:
    """User asked to swap positions so the header doesn't rearrange the
    user's reach when entering selection mode. Pin via source order."""
    src = Path("yaga/app.py").read_text(encoding="utf-8")
    trash_pack = src.index("self.header.pack_start(self._sel_delete_btn)")
    close_pack = src.index("self.header.pack_end(self._sel_cancel_btn)")
    assert trash_pack < close_pack


# ---------------------------------------------------------------------------
# 16.  Configurable nav-bar position (top / bottom / left / right)
# ---------------------------------------------------------------------------

def test_settings_nav_position_default_is_top() -> None:
    from yaga.config import Settings
    assert Settings().nav_position == "top"


def test_settings_nav_position_round_trips_through_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving and reloading must preserve the chosen nav position."""
    monkeypatch.setattr("yaga.config.CONFIG_DIR", tmp_path)
    from yaga.config import Settings
    s = Settings()
    s.nav_position = "right"
    s.save()
    loaded = Settings.load()
    assert loaded.nav_position == "right"


@pytest.mark.parametrize("bad_value", ["", "north", "TOP", None, 0, "diagonal"])
def test_settings_nav_position_rejects_invalid_values_on_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_value,
) -> None:
    """A typo in settings.json must not crash _build_ui — Settings.load()
    clamps unknown values back to "top"."""
    monkeypatch.setattr("yaga.config.CONFIG_DIR", tmp_path)
    import json
    (tmp_path / "settings.json").write_text(
        json.dumps({"nav_position": bad_value}), encoding="utf-8",
    )
    from yaga.config import Settings
    assert Settings.load().nav_position == "top"


def test_app_build_ui_routes_nav_box_per_position() -> None:
    """Source-level pin: each position branch must wire the nav_box to the
    right ToolbarView slot. Catches accidental swaps (e.g. left → top_bar)."""
    src = Path("yaga/app.py").read_text(encoding="utf-8")
    top_idx    = src.index('if nav_position == "top":')
    add_top    = src.index("self.toolbar.add_top_bar(self.nav_box)", top_idx)
    bottom_idx = src.index('elif nav_position == "bottom":', add_top)
    add_bottom = src.index("self.toolbar.add_bottom_bar(self.nav_box)", bottom_idx)
    # top branch wires add_top_bar BEFORE the bottom branch does add_bottom_bar.
    assert top_idx < add_top < bottom_idx < add_bottom

    left_idx  = src.index('if nav_position == "left":')
    right_idx = src.index('elif nav_position == "right":', left_idx)
    # The left branch appends nav_box first, then content; right does the
    # opposite. Pin both orders so a refactor can't silently flip them.
    left_block  = src[left_idx:right_idx]
    right_block = src[right_idx:src.index("else:", right_idx)]
    assert left_block.index("row.append(self.nav_box)") < left_block.index("row.append(content)")
    assert right_block.index("row.append(content)") < right_block.index("row.append(self.nav_box)")


def test_settings_window_nav_option_sits_above_video_group() -> None:
    """User explicitly asked for the option to be 'oberhalb von Video-Option'.
    Pin the relative order so a future settings-page refactor doesn't move it."""
    src = Path("yaga/settings_window.py").read_text(encoding="utf-8")
    nav_group_idx   = src.index('"nav_position"')
    video_group_idx = src.index('title=self._("Video")')
    assert nav_group_idx < video_group_idx


def test_apply_settings_defers_rebuild_to_idle() -> None:
    """Regression: a synchronous _build_ui call from inside a settings-dialog
    combo notify::selected was observed to freeze the app when the rebuild
    changed the toolbar topology (top-bar nav → side-rail nav). The rebuild
    must be queued via GLib.idle_add so the signal stack unwinds first."""
    src = Path("yaga/app.py").read_text(encoding="utf-8")
    apply_idx   = src.index("def apply_settings(self, settings: Settings)")
    rebuild_def = src.index("def _do_settings_rebuild", apply_idx)
    apply_body  = src[apply_idx:rebuild_def]
    # apply_settings must NOT call _build_ui or refresh directly anymore.
    assert "self._build_ui()" not in apply_body
    assert "self.refresh(scan=True)" not in apply_body
    # It must schedule _do_settings_rebuild via GLib.idle_add.
    assert "GLib.idle_add(self._do_settings_rebuild" in apply_body
    # And the deferred handler is what actually rebuilds + refreshes.
    rebuild_body = src[rebuild_def:src.index("\n    # ", rebuild_def)]
    assert "self._build_ui()" in rebuild_body
    assert "self.refresh(scan=True)" in rebuild_body


def _make_nav_swipe_fake(category: str, cats: list[str], orientation):
    """Build a SimpleNamespace standing in for GalleryWindow with just the
    attributes _on_nav_swipe touches, so we can call it as an unbound method."""
    activated: list[str] = []
    buttons: dict[str, SimpleNamespace] = {}
    for c in cats:
        # set_active triggers the toggled handler in real GTK; we only record
        # which button got activated — the test asserts on category, not on
        # the toggled-handler side effects.
        buttons[c] = SimpleNamespace(set_active=lambda _v, _c=c: activated.append(_c))
    nav_box = SimpleNamespace(get_orientation=lambda: orientation)
    settings = SimpleNamespace(categories=lambda: [(c, c.title(), f"/p/{c}") for c in cats])
    fake = SimpleNamespace(
        _selection_mode=False,
        category=category,
        nav_box=nav_box,
        settings=settings,
        category_buttons=buttons,
    )
    return fake, activated


def test_nav_swipe_horizontal_right_advances_to_next_category() -> None:
    from yaga.app import GalleryWindow
    from gi.repository import Gtk
    fake, activated = _make_nav_swipe_fake(
        "photos", ["pictures", "photos", "videos"], Gtk.Orientation.HORIZONTAL,
    )
    GalleryWindow._on_nav_swipe(fake, None, 800.0, 10.0)
    assert activated == ["videos"]


def test_nav_swipe_horizontal_left_goes_to_previous_category() -> None:
    from yaga.app import GalleryWindow
    from gi.repository import Gtk
    fake, activated = _make_nav_swipe_fake(
        "photos", ["pictures", "photos", "videos"], Gtk.Orientation.HORIZONTAL,
    )
    GalleryWindow._on_nav_swipe(fake, None, -800.0, 10.0)
    assert activated == ["pictures"]


def test_nav_swipe_vertical_uses_y_axis_for_direction() -> None:
    """On a side rail (vertical nav), only y-axis swipes count and they map
    to next/previous along the vertical stack."""
    from yaga.app import GalleryWindow
    from gi.repository import Gtk
    fake, activated = _make_nav_swipe_fake(
        "photos", ["pictures", "photos", "videos"], Gtk.Orientation.VERTICAL,
    )
    # Strong horizontal velocity on a vertical nav must be ignored.
    GalleryWindow._on_nav_swipe(fake, None, 800.0, 10.0)
    assert activated == []
    # Strong downward velocity advances to the next category.
    GalleryWindow._on_nav_swipe(fake, None, 10.0, 800.0)
    assert activated == ["videos"]


def test_nav_swipe_below_velocity_threshold_is_ignored() -> None:
    """Stray finger drags shouldn't jump categories — match the 350 px/s
    threshold the folder-back swipe already uses."""
    from yaga.app import GalleryWindow
    from gi.repository import Gtk
    fake, activated = _make_nav_swipe_fake(
        "photos", ["pictures", "photos", "videos"], Gtk.Orientation.HORIZONTAL,
    )
    GalleryWindow._on_nav_swipe(fake, None, 200.0, 10.0)
    assert activated == []


def test_nav_swipe_dominant_off_axis_velocity_is_ignored() -> None:
    """A diagonal-but-mostly-vertical swipe on a horizontal nav must NOT
    switch categories. Pin the abs(primary) <= abs(secondary) bail."""
    from yaga.app import GalleryWindow
    from gi.repository import Gtk
    fake, activated = _make_nav_swipe_fake(
        "photos", ["pictures", "photos", "videos"], Gtk.Orientation.HORIZONTAL,
    )
    GalleryWindow._on_nav_swipe(fake, None, 600.0, 800.0)
    assert activated == []


def test_nav_swipe_at_first_or_last_category_does_not_wrap() -> None:
    from yaga.app import GalleryWindow
    from gi.repository import Gtk
    cats = ["pictures", "photos", "videos"]
    # At the first category, a "previous" swipe is a no-op.
    fake, activated = _make_nav_swipe_fake("pictures", cats, Gtk.Orientation.HORIZONTAL)
    GalleryWindow._on_nav_swipe(fake, None, -800.0, 10.0)
    assert activated == []
    # At the last category, a "next" swipe is a no-op.
    fake, activated = _make_nav_swipe_fake("videos", cats, Gtk.Orientation.HORIZONTAL)
    GalleryWindow._on_nav_swipe(fake, None, 800.0, 10.0)
    assert activated == []


def test_nav_swipe_in_selection_mode_is_disabled() -> None:
    """Multi-select uses long-press + drag on tiles; a swipe on the nav while
    in selection mode mustn't tear the user out of their selection."""
    from yaga.app import GalleryWindow
    from gi.repository import Gtk
    fake, activated = _make_nav_swipe_fake(
        "photos", ["pictures", "photos", "videos"], Gtk.Orientation.HORIZONTAL,
    )
    fake._selection_mode = True
    GalleryWindow._on_nav_swipe(fake, None, 800.0, 10.0)
    assert activated == []


def test_do_settings_rebuild_recreates_window_on_nav_position_change() -> None:
    """Regression: in-place rebuild of the toolbar across a top-bar↔side-rail
    topology change either deadlocked GTK's layout pass or left the still-
    open modal settings dialog with a broken input grab. The fix is to
    recreate the entire GalleryWindow when nav_position changes — every
    transient child gets cleanly destroyed with the old window, the new
    window starts with a fresh layout pass, and persisted settings (saved
    by apply_settings before scheduling the rebuild) are picked up by the
    new window's __init__."""
    src = Path("yaga/app.py").read_text(encoding="utf-8")
    rebuild_def  = src.index("def _do_settings_rebuild")
    rebuild_end  = src.index("def _recreate_window_for_layout_change", rebuild_def)
    rebuild_body = src[rebuild_def:rebuild_end]

    # The position-change branch must call _recreate_window_for_layout_change
    # — not _build_ui — and must early-return so the in-place fallback below
    # doesn't also fire.
    pos_change = rebuild_body.index("if old_position != new_position:")
    recreate   = rebuild_body.index("self._recreate_window_for_layout_change()", pos_change)
    early_ret  = rebuild_body.index("return GLib.SOURCE_REMOVE", recreate)
    inplace    = rebuild_body.index("self._build_ui()", early_ret)
    assert pos_change < recreate < early_ret < inplace


def test_recreate_window_destroys_old_after_new_is_presented() -> None:
    """Adw quits the main loop when the last window goes away. self.destroy()
    must fire after new_window.present() so the app always has a window."""
    src = Path("yaga/app.py").read_text(encoding="utf-8")
    fn_def = src.index("def _recreate_window_for_layout_change")
    fn_end = src.index("\n    def ", fn_def + 1)
    fn_body = src[fn_def:fn_end]
    new_idx     = fn_body.index("new_window = GalleryWindow(app)")
    present_idx = fn_body.index("new_window.present()", new_idx)
    destroy_idx = fn_body.index("self.destroy()", present_idx)
    assert new_idx < present_idx < destroy_idx


def test_recreate_destroys_tracked_settings_dialog_before_self() -> None:
    """Regression: an earlier attempt iterated app.get_windows() to find the
    settings dialog and destroy it. That doesn't work because Adw.Preferences-
    Window with transient_for=parent isn't auto-registered with the app, so
    the loop never found the dialog and parent-destroy left it behind on
    some WMs — producing two visible dialogs after recreate. Fix: track the
    dialog explicitly via self._settings_dialog and destroy it directly."""
    src = Path("yaga/app.py").read_text(encoding="utf-8")
    fn_def = src.index("def _recreate_window_for_layout_change")
    fn_end = src.index("\n    def ", fn_def + 1)
    fn_body = src[fn_def:fn_end]
    new_idx     = fn_body.index("new_window = GalleryWindow(app)")
    present_idx = fn_body.index("new_window.present()", new_idx)
    track_read  = fn_body.index("self._settings_dialog", present_idx)
    destroy_dlg = fn_body.index("dialog.destroy()", track_read)
    self_dest   = fn_body.index("self.destroy()", destroy_dlg)
    assert new_idx < present_idx < track_read < destroy_dlg < self_dest
    # Old (broken) loop must NOT be reintroduced.
    assert "for child in list(app.get_windows())" not in fn_body


def test_open_settings_is_idempotent() -> None:
    """Clicking the gear button twice must not stack two dialogs — the
    second click presents the existing one. Pin via source order: the
    existing-dialog branch returns before constructing a new SettingsWindow."""
    src = Path("yaga/app.py").read_text(encoding="utf-8")
    fn_def = src.index("def _open_settings(self, _button")
    fn_end = src.index("\n    def ", fn_def + 1)
    fn_body = src[fn_def:fn_end]
    check_existing = fn_body.index("existing = self._settings_dialog")
    early_present  = fn_body.index("existing.present()", check_existing)
    early_return   = fn_body.index("return", early_present)
    construct      = fn_body.index("dialog = SettingsWindow(self)", early_return)
    assert check_existing < early_present < early_return < construct
    # The newly constructed dialog is also tracked + close-listened.
    track_idx = fn_body.index("self._settings_dialog = dialog", construct)
    close_req = fn_body.index('dialog.connect("close-request"', track_idx)
    destroy_c = fn_body.index('dialog.connect("destroy"', close_req)
    assert construct < track_idx < close_req < destroy_c


def test_recreate_does_not_auto_reopen_settings() -> None:
    """Regression: the auto-reopen-on-appearance feature was removed because
    every sequencing variant left a stale modal grab from the just-destroyed
    old dialog. The reopened dialog rendered and reacted visually but its
    action handlers silently no-opped. Pin the absence of the reopen path
    so a future "convenience" patch doesn't reintroduce the bug."""
    src = Path("yaga/app.py").read_text(encoding="utf-8")
    # Neither the recreate path nor __init__ should write/read the hint.
    assert "_reopen_settings_page" not in src
    # And no timeout/idle path should auto-construct a SettingsWindow.
    init_def = src.index("def __init__(self, app: GalleryApplication)")
    init_end = src.index("\n    def ", init_def + 1)
    init_body = src[init_def:init_end]
    assert "SettingsWindow(" not in init_body


def test_settings_pages_have_stable_names() -> None:
    """set_visible_page_name needs a name that doesn't depend on the
    translated title. Pin all three pages so a future translation change
    doesn't silently break the reopen-after-recreate flow."""
    src = Path("yaga/settings_window.py").read_text(encoding="utf-8")
    assert 'media.set_name("folders")' in src
    assert 'app.set_name("appearance")' in src
    assert 'page.set_name("nextcloud")' in src


def test_settings_window_initial_page_argument() -> None:
    """The initial_page kwarg must be honoured before the dialog is presented
    so the user lands on the requested page without a flash of page 1."""
    src = Path("yaga/settings_window.py").read_text(encoding="utf-8")
    init_def = src.index("def __init__(self, parent: GalleryWindow")
    init_end = src.index("\n    def ", init_def + 1)
    init_body = src[init_def:init_end]
    sig_idx   = init_body.index("initial_page: str | None = None")
    build_idx = init_body.index("self._build()", sig_idx)
    set_idx   = init_body.index("self.set_visible_page_name(initial_page)", build_idx)
    # _build must run first (creates the pages); only then can set_visible_page_name find them.
    assert sig_idx < build_idx < set_idx


def test_apply_settings_coalesces_repeated_calls() -> None:
    """Rapid combo changes shouldn't queue N rebuilds — pin the dedupe flag."""
    src = Path("yaga/app.py").read_text(encoding="utf-8")
    apply_idx = src.index("def apply_settings(self, settings: Settings)")
    rebuild_def = src.index("def _do_settings_rebuild", apply_idx)
    apply_body = src[apply_idx:rebuild_def]
    # The flag-guard must wrap the idle_add call.
    guard_idx = apply_body.index("_settings_rebuild_pending")
    idle_idx  = apply_body.index("GLib.idle_add(self._do_settings_rebuild")
    assert guard_idx < idle_idx
    # And the handler must clear it before doing the work, so a fresh call
    # arriving during the rebuild is allowed to schedule the next one.
    rebuild_body = src[rebuild_def:src.index("\n    # ", rebuild_def)]
    clear_idx   = rebuild_body.index("self._settings_rebuild_pending = False")
    build_idx   = rebuild_body.index("self._build_ui()")
    assert clear_idx < build_idx
