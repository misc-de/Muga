"""Tests for the photo editor.

Two halves, tested differently:

  * coordinate maths and the undo/redo model are plain logic and run headless
    against unbound methods;
  * the editor is a Gtk.Box that builds its whole panel stack in __init__, so
    the construction, save and history tests build a real one and are skipped
    without a display (GTK aborts rather than raises there).

The history model is worth pinning down: a snapshot shares ``_working`` by
reference rather than copying it, which is only safe because nothing mutates a
working image in place. That invariant is what keeps a history step a pointer
copy instead of tens of megabytes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.conftest import requires_display

pytest.importorskip("PIL.Image")

from PIL import Image as PILImage  # noqa: E402

from muga.models import MediaItem  # noqa: E402

view = pytest.importorskip("muga.editor.view")


def _item(tmp_path: Path, size=(200, 150), name="photo.jpg") -> MediaItem:
    path = tmp_path / name
    PILImage.new("RGB", size, (40, 80, 120)).save(path, quality=92)
    return MediaItem(
        id=1, path=str(path), category="photos", media_type="image",
        folder=str(tmp_path), name=name, mtime=1.7e9, size=path.stat().st_size,
        thumb_path=None,
    )


# ---------------------------------------------------------------------------
# Display ↔ image coordinate mapping
# ---------------------------------------------------------------------------

def _coord_win(draw_w, draw_h, img_w, img_h):
    area = MagicMock()
    area.get_width.return_value = draw_w
    area.get_height.return_value = draw_h
    return SimpleNamespace(
        _draw_area=area,
        _working=SimpleNamespace(size=(img_w, img_h)),
    )


def test_display_to_image_maps_a_centred_selection() -> None:
    """A 400x300 image shown in a 800x600 area is scaled 2x with no bars."""
    win = _coord_win(800, 600, 400, 300)
    box = view.EditorView._display_to_image(win, 100, 100, 300, 400)
    assert box == (50, 50, 150, 200)


def test_display_to_image_accounts_for_letterboxing() -> None:
    """A 4:3 image in a 16:9 area gets bars at the sides; a selection at the
    widget's left edge must clamp to the image's left edge, not go negative."""
    win = _coord_win(1600, 900, 400, 300)   # scale 3, 200px bar each side
    box = view.EditorView._display_to_image(win, 0, 0, 1600, 900)
    assert box == (0, 0, 400, 300)


def test_display_to_image_normalises_a_backwards_drag() -> None:
    """Dragging up-left gives x2 < x1; the crop rect still has to come out
    with min/max in the right order."""
    win = _coord_win(800, 600, 400, 300)
    forward = view.EditorView._display_to_image(win, 100, 100, 300, 400)
    backward = view.EditorView._display_to_image(win, 300, 400, 100, 100)
    assert forward == backward


def test_display_to_image_clamps_outside_the_image() -> None:
    win = _coord_win(800, 600, 400, 300)
    box = view.EditorView._display_to_image(win, -500, -500, 5000, 5000)
    assert box == (0, 0, 400, 300)


def test_display_to_image_gives_up_on_an_unrealised_area() -> None:
    """Before the first allocation the drawing area has no size; dividing by
    it would raise inside a gesture handler."""
    assert view.EditorView._display_to_image(_coord_win(0, 0, 400, 300), 0, 0, 10, 10) is None
    assert view.EditorView._display_to_image(_coord_win(800, 0, 400, 300), 0, 0, 10, 10) is None


# ---------------------------------------------------------------------------
# Colour sampling for the obfuscate brush
# ---------------------------------------------------------------------------

def test_sample_color_reads_the_underlying_pixels() -> None:
    img = PILImage.new("RGB", (100, 100), (255, 0, 0))
    r, g, b, a = view.EditorView._sample_color_at(SimpleNamespace(), img, 0.5, 0.5)
    assert r == pytest.approx(1.0, abs=0.01)
    assert g == pytest.approx(0.0, abs=0.01)
    assert 0 < a < 1, "the brush has to stay slightly transparent"


def test_sample_color_averages_a_region() -> None:
    img = PILImage.new("RGB", (100, 100), (0, 0, 0))
    for x in range(50, 100):
        for y in range(100):
            img.putpixel((x, y), (255, 255, 255))
    r, _, _, _ = view.EditorView._sample_color_at(SimpleNamespace(), img, 0.5, 0.5, sample_radius=20)
    assert 0.2 < r < 0.8, f"expected an average, got {r}"


def test_sample_color_handles_a_greyscale_source() -> None:
    img = PILImage.new("L", (50, 50), 128)
    r, g, b, _ = view.EditorView._sample_color_at(SimpleNamespace(), img, 0.5, 0.5)
    assert r == g == b


@pytest.mark.parametrize(("x", "y"), [(-1.0, 0.5), (2.0, 0.5), (0.5, -1.0), (0.5, 2.0)])
def test_sample_color_falls_back_outside_the_image(x, y) -> None:
    img = PILImage.new("RGB", (100, 100), (255, 0, 0))
    result = view.EditorView._sample_color_at(SimpleNamespace(), img, x, y)
    assert len(result) == 4
    assert all(0 <= c <= 1 for c in result)


# ---------------------------------------------------------------------------
# EXIF handling on save
# ---------------------------------------------------------------------------

def test_exif_for_save_normalises_the_orientation() -> None:
    """The rotation was baked into the pixels at load; leaving the tag alone
    makes every viewer rotate the photo a second time."""
    source = PILImage.Exif()
    source[0x0112] = 6           # Orientation: rotate 90° CW
    source[0x0110] = "TestCam"   # Model
    win = SimpleNamespace(_exif_bytes=source.tobytes())

    out = view.EditorView._exif_for_save(win)

    result = PILImage.Exif()
    result.load(out)
    assert result[0x0112] == 1
    assert result[0x0110] == "TestCam", "other tags must survive"


def test_exif_for_save_writes_a_minimal_block_without_a_source() -> None:
    """Some receivers treat a JPEG with no EXIF as a plain file rather than a
    picture, so an edited copy always gets at least Software + Orientation."""
    win = SimpleNamespace(_exif_bytes=None)
    out = view.EditorView._exif_for_save(win)
    assert out
    result = PILImage.Exif()
    result.load(out)
    assert result[0x0112] == 1
    assert result[0x0131] == "Muga"


def test_exif_for_save_survives_a_corrupt_source() -> None:
    win = SimpleNamespace(_exif_bytes=b"not really exif")
    view.EditorView._exif_for_save(win)  # must not raise into the save path


# ---------------------------------------------------------------------------
# Undo / redo model
# ---------------------------------------------------------------------------

def _history_win(**extra):
    defaults = dict(
        _working=PILImage.new("RGB", (10, 10)),
        _filter_mode=None, _brightness=1.0, _contrast=1.0,
        _red=1.0, _green=1.0, _blue=1.0,
        _stickers=[], _active_sticker=None, _obfuscate_strokes=[],
        _frame_theme=None,
        _restoring=False,
        _history_undo=[], _history_redo=[], _history_max_steps=20,
        _emit_history_changed=MagicMock(),
    )
    defaults.update(extra)
    win = SimpleNamespace(**defaults)
    # _snapshot_state calls _capture_state on self; bind the real one so the
    # tests exercise the actual snapshot rather than a stand-in.
    win._capture_state = view.EditorView._capture_state.__get__(win, type(win))
    return win


def test_capture_state_shares_the_working_image() -> None:
    """A copy here would cost tens of megabytes per history step; sharing is
    safe because crop/reset/restore always reassign rather than mutate."""
    win = _history_win()
    state = view.EditorView._capture_state(win)
    assert state["working"] is win._working


def test_capture_state_copies_mutable_collections() -> None:
    """Stickers and strokes *are* mutated in place, so they must be copied."""
    win = _history_win(_stickers=[{"x": 1}], _obfuscate_strokes=[(0, 0, 5, (1, 1, 1, 1))])
    state = view.EditorView._capture_state(win)
    win._stickers[0]["x"] = 999
    win._obfuscate_strokes.append((1, 1, 5, (0, 0, 0, 1)))
    assert state["stickers"][0]["x"] == 1
    assert len(state["obfuscate_strokes"]) == 1


def test_snapshot_pushes_onto_the_undo_stack() -> None:
    win = _history_win()
    view.EditorView._snapshot_state(win)
    assert len(win._history_undo) == 1
    win._emit_history_changed.assert_called_once()


def test_snapshot_clears_the_redo_stack() -> None:
    """A new edit after an undo makes the redo branch unreachable."""
    win = _history_win(_history_redo=[{"stale": True}])
    view.EditorView._snapshot_state(win)
    assert win._history_redo == []


def test_snapshot_is_suppressed_while_restoring() -> None:
    """_restore_state writes to the widgets, whose handlers would otherwise
    snapshot the state being restored and corrupt the stack."""
    win = _history_win(_restoring=True)
    view.EditorView._snapshot_state(win)
    assert win._history_undo == []


def test_snapshot_stack_is_bounded() -> None:
    win = _history_win(_history_max_steps=5)
    for _ in range(12):
        view.EditorView._snapshot_state(win)
    assert len(win._history_undo) == 5


def test_can_undo_and_redo_report_the_stacks() -> None:
    win = _history_win()
    assert view.EditorView.can_undo(win) is False
    assert view.EditorView.can_redo(win) is False
    win._history_undo.append({})
    win._history_redo.append({})
    assert view.EditorView.can_undo(win) is True
    assert view.EditorView.can_redo(win) is True


# ---------------------------------------------------------------------------
# Real editor: construction, edits and saving
# ---------------------------------------------------------------------------

@pytest.fixture
def editor(tmp_path):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    made = []

    def _make(item=None, **kwargs):
        ed = view.EditorView(item if item is not None else _item(tmp_path), **kwargs)
        made.append(ed)
        return ed

    yield _make

    for ed in made:
        try:
            ed.cleanup()
        except Exception:
            pass


@requires_display
def test_editor_builds(editor, tmp_path) -> None:
    ed = editor()
    assert ed._working.size == (200, 150)
    assert ed._original is not None
    assert ed.can_undo() is False


@requires_display
def test_editor_uses_the_translator(editor, tmp_path) -> None:
    ed = editor(translate=lambda s: f"<{s}>")
    assert ed._("Crop") == "<Crop>"


@requires_display
def test_editor_bakes_in_the_exif_orientation(editor, tmp_path) -> None:
    """The editor must show the photo the same way up as the viewer and the
    thumbnailer, both of which call exif_transpose."""
    path = tmp_path / "sideways.jpg"
    exif = PILImage.Exif()
    exif[0x0112] = 6  # rotate 90° CW to display
    PILImage.new("RGB", (200, 100), (10, 20, 30)).save(path, exif=exif.tobytes())
    item = MediaItem(id=1, path=str(path), category="photos", media_type="image",
                     folder=str(tmp_path), name="sideways.jpg", mtime=1.0, size=1,
                     thumb_path=None)
    ed = editor(item)
    assert ed._working.size == (100, 200), "orientation was not baked in"


@requires_display
def test_editor_saves_alongside_the_original(editor, tmp_path) -> None:
    ed = editor()
    dest = Path(ed.save_as_new())
    assert dest.exists()
    assert dest.parent == tmp_path
    assert "_edit_" in dest.name


@requires_display
def test_editor_never_overwrites_an_earlier_edit(editor, tmp_path) -> None:
    ed = editor()
    first = Path(ed.save_as_new())
    second = Path(ed.save_as_new())
    assert first != second
    assert first.exists() and second.exists()


@requires_display
def test_editor_save_carries_the_exif_over(editor, tmp_path) -> None:
    path = tmp_path / "tagged.jpg"
    exif = PILImage.Exif()
    exif[0x0110] = "TestCam"
    exif[0x0112] = 1
    PILImage.new("RGB", (120, 90), (7, 7, 7)).save(path, exif=exif.tobytes())
    item = MediaItem(id=1, path=str(path), category="photos", media_type="image",
                     folder=str(tmp_path), name="tagged.jpg", mtime=1.0, size=1,
                     thumb_path=None)
    ed = editor(item)
    with PILImage.open(ed.save_as_new()) as saved:
        assert saved.getexif()[0x0110] == "TestCam"
        assert saved.getexif()[0x0112] == 1


@requires_display
def test_editor_converts_heic_to_jpeg_on_save(editor, tmp_path) -> None:
    """Pillow cannot write HEIC, so the edited copy has to change container."""
    source = tmp_path / "photo.jpg"
    PILImage.new("RGB", (60, 40)).save(source)
    item = MediaItem(id=1, path=str(tmp_path / "photo.heic"), category="photos",
                     media_type="image", folder=str(tmp_path), name="photo.heic",
                     mtime=1.0, size=1, thumb_path=None)
    source.rename(tmp_path / "photo.heic")
    ed = editor(item)
    assert Path(ed.save_as_new()).suffix == ".jpg"


@requires_display
def test_editor_undo_restores_the_previous_state(editor, tmp_path) -> None:
    ed = editor()
    before = ed._brightness
    ed._snapshot_state()
    ed._brightness = 1.8
    assert ed.can_undo() is True
    ed.undo()
    assert ed._brightness == before
    assert ed.can_redo() is True


@requires_display
def test_editor_redo_reapplies_the_undone_state(editor, tmp_path) -> None:
    ed = editor()
    ed._snapshot_state()
    ed._brightness = 1.8
    ed.undo()
    ed.redo()
    assert ed._brightness == 1.8


@requires_display
def test_editor_cleanup_cancels_pending_work(editor, tmp_path) -> None:
    """A detached editor firing _do_update against a removed preview keeps its
    full-resolution copies alive until the timeout would have fired."""
    ed = editor()
    ed._schedule_update()
    assert ed._update_id is not None
    ed.cleanup()
    assert ed._update_id is None
    assert ed._tick_cb_id is None


@requires_display
def test_editor_cleanup_is_idempotent(editor, tmp_path) -> None:
    ed = editor()
    ed.cleanup()
    ed.cleanup()


# ---------------------------------------------------------------------------
# The edit pipeline
# ---------------------------------------------------------------------------

def _edit_win(**extra):
    """A ``self`` carrying only what _apply_edits reads."""
    defaults = dict(
        _filter_mode=None, _brightness=1.0, _contrast=1.0,
        _red=1.0, _green=1.0, _blue=1.0,
        _stickers=[], _obfuscate_strokes=[], _frame_theme=None,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def _grey(size=(60, 40), level=128):
    return PILImage.new("RGB", size, (level, level, level))


def test_edits_return_an_unchanged_image_by_default() -> None:
    src = _grey()
    out = view.EditorView._apply_edits(_edit_win(), src)
    assert out.size == src.size
    assert out.getpixel((30, 20)) == (128, 128, 128)


def test_edits_convert_a_non_rgb_source() -> None:
    """Screenshots are frequently RGBA or palette images."""
    out = view.EditorView._apply_edits(_edit_win(), PILImage.new("RGBA", (20, 20), (1, 2, 3, 255)))
    assert out.mode == "RGB"


def test_brightness_lightens_and_darkens() -> None:
    brighter = view.EditorView._apply_edits(_edit_win(_brightness=1.6), _grey())
    darker = view.EditorView._apply_edits(_edit_win(_brightness=0.4), _grey())
    assert brighter.getpixel((30, 20))[0] > 128
    assert darker.getpixel((30, 20))[0] < 128


def test_contrast_spreads_the_tonal_range() -> None:
    """Pillow's Contrast works against the image's own mean, so it needs an
    image that actually has light and dark areas — on a flat fill it is a
    no-op by definition."""
    src = PILImage.new("RGB", (20, 20), (100, 100, 100))
    for x in range(10):
        for y in range(20):
            src.putpixel((x, y), (160, 160, 160))

    out = view.EditorView._apply_edits(_edit_win(_contrast=2.0), src)

    dark, light = out.getpixel((15, 10))[0], out.getpixel((5, 10))[0]
    assert light - dark > 160 - 100, "the range was not widened"


def test_contrast_below_one_flattens_the_range() -> None:
    src = PILImage.new("RGB", (20, 20), (60, 60, 60))
    for x in range(10):
        for y in range(20):
            src.putpixel((x, y), (200, 200, 200))
    out = view.EditorView._apply_edits(_edit_win(_contrast=0.3), src)
    assert out.getpixel((5, 10))[0] - out.getpixel((15, 10))[0] < 140


def test_channel_gains_are_applied_per_channel() -> None:
    out = view.EditorView._apply_edits(_edit_win(_red=1.5, _blue=0.5), _grey())
    r, g, b = out.getpixel((30, 20))
    assert r > g > b


def test_channel_gains_are_clamped_at_255() -> None:
    """Without the min() a bright pixel wraps around to dark."""
    src = PILImage.new("RGB", (10, 10), (250, 250, 250))
    r, _g, _b = view.EditorView._apply_edits(_edit_win(_red=3.0), src).getpixel((5, 5))
    assert r == 255


def test_neutral_channel_gains_leave_the_pixels_exact() -> None:
    """All three at 1.0 skips the split/point/merge round-trip; going through
    it anyway would round every channel through int() for nothing."""
    src = PILImage.new("RGB", (20, 20), (33, 77, 199))
    out = view.EditorView._apply_edits(_edit_win(), src)
    assert list(out.get_flattened_data()) == list(src.get_flattened_data())


def test_filters_can_be_skipped() -> None:
    """The preview caches the filtered base separately, so it asks for the
    rest of the pipeline without re-running the slow stage."""
    from muga.editor import _FILTER_DEFS

    mode = next((k for k, _l, f in _FILTER_DEFS if f), None)
    if mode is None:
        pytest.skip("no filter with a callable defined")

    src = _grey()
    filtered = view.EditorView._apply_edits(_edit_win(_filter_mode=mode), src)
    unfiltered = view.EditorView._apply_edits(
        _edit_win(_filter_mode=mode), src, apply_filter=False)
    assert unfiltered.getpixel((30, 20)) == (128, 128, 128)
    assert filtered.size == unfiltered.size


def test_an_unknown_filter_is_a_no_op() -> None:
    out = view.EditorView._apply_edits(_edit_win(_filter_mode="not-a-filter"), _grey())
    assert out.getpixel((30, 20)) == (128, 128, 128)


def test_filter_only_stage_matches_the_full_pipeline() -> None:
    """The preview splits the pipeline in two for caching; the halves have to
    agree with the whole or the preview lies about the result."""
    from muga.editor import _FILTER_DEFS

    mode = next((k for k, _l, f in _FILTER_DEFS if f), None)
    if mode is None:
        pytest.skip("no filter with a callable defined")

    src = PILImage.new("RGB", (30, 20), (90, 140, 200))
    win = _edit_win(_filter_mode=mode)
    whole = view.EditorView._apply_edits(win, src)
    halves = view.EditorView._apply_edits(
        win, view.EditorView._apply_filter_only(win, src), apply_filter=False)
    assert list(whole.get_flattened_data()) == list(halves.get_flattened_data())


def test_obfuscate_blurs_only_where_it_was_drawn() -> None:
    """It is a redaction tool — the rest of the photo has to come through
    untouched."""
    src = PILImage.new("RGB", (100, 100), (0, 0, 0))
    for x in range(40, 60):
        for y in range(40, 60):
            src.putpixel((x, y), (255, 255, 255))

    out = view.EditorView._apply_edits(
        _edit_win(_obfuscate_strokes=[(0.5, 0.5, 0.15, (1, 1, 1, 1))]), src)

    assert out.getpixel((50, 50)) != (255, 255, 255), "the covered area was not blurred"
    assert out.getpixel((5, 5)) == (0, 0, 0), "an untouched corner changed"


def test_multiple_obfuscate_strokes_all_apply() -> None:
    src = PILImage.new("RGB", (100, 100), (255, 255, 255))
    src.putpixel((10, 10), (0, 0, 0))
    src.putpixel((90, 90), (0, 0, 0))
    out = view.EditorView._apply_edits(
        _edit_win(_obfuscate_strokes=[(0.1, 0.1, 0.1, None), (0.9, 0.9, 0.1, None)]), src)
    assert out.getpixel((10, 10)) != (0, 0, 0)
    assert out.getpixel((90, 90)) != (0, 0, 0)


def _a_frame_theme() -> str:
    from muga.editor.frames import _FRAME_THEMES

    return _FRAME_THEMES[0][0]


def test_a_frame_is_composited_over_the_photo() -> None:
    theme = _a_frame_theme()
    src = _grey((200, 150))
    out = view.EditorView._apply_edits(_edit_win(_frame_theme=theme), src)
    assert out.size == src.size
    assert out.mode == "RGB"


def test_an_emoji_sticker_is_pasted() -> None:
    src = _grey((200, 150))
    win = _edit_win(_stickers=[{"source": "🙂", "size": 0.3, "rel": (0.5, 0.5)}])
    out = view.EditorView._apply_edits(win, src)
    assert out.size == src.size
    assert list(out.get_flattened_data()) != list(src.get_flattened_data()), "nothing was pasted"


def test_an_image_sticker_keeps_its_aspect() -> None:
    src = _grey((200, 200))
    sticker = PILImage.new("RGBA", (40, 20), (255, 0, 0, 255))
    win = _edit_win(_stickers=[{"source": sticker, "size": 0.5, "rel": (0.5, 0.5)}])
    out = view.EditorView._apply_edits(win, src)
    assert out.size == (200, 200)


def test_stickers_stack_in_order() -> None:
    src = _grey((200, 200))
    red = PILImage.new("RGBA", (40, 40), (255, 0, 0, 255))
    blue = PILImage.new("RGBA", (40, 40), (0, 0, 255, 255))
    win = _edit_win(_stickers=[
        {"source": red, "size": 0.5, "rel": (0.5, 0.5)},
        {"source": blue, "size": 0.5, "rel": (0.5, 0.5)},
    ])
    r, g, b = view.EditorView._apply_edits(win, src).getpixel((100, 100))
    assert b > r, "the later sticker did not land on top"


def test_every_stage_composes() -> None:
    """Filters, sliders, a sticker, a redaction and a frame at once — the
    order matters and nothing may raise."""
    win = _edit_win(
        _brightness=1.2, _contrast=1.1, _red=1.1, _blue=0.9,
        _stickers=[{"source": "🙂", "size": 0.2, "rel": (0.3, 0.3)}],
        _obfuscate_strokes=[(0.7, 0.7, 0.1, None)],
        _frame_theme=_a_frame_theme(),
    )
    out = view.EditorView._apply_edits(win, _grey((240, 180)))
    assert out.size == (240, 180)
    assert out.mode == "RGB"


def test_edits_do_not_mutate_the_source() -> None:
    """History snapshots share the working image by reference, so an in-place
    edit would silently rewrite every undo step."""
    src = _grey()
    before = list(src.get_flattened_data())
    view.EditorView._apply_edits(_edit_win(_brightness=1.5, _contrast=1.4), src)
    assert list(src.get_flattened_data()) == before
