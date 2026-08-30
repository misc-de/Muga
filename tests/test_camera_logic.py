"""Unit tests for camera.py's pure logic.

camera.py is a 4600-line GTK window, and GTK segfaults without a display, so
nothing here constructs a widget. Following the convention established in
test_recent_changes.py, methods are called unbound with a SimpleNamespace
``self`` carrying only the attributes the method under test actually touches.
That keeps the tests honest about each method's real dependencies and lets
them run headless.
"""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

camera = pytest.importorskip("muga.camera")
# Frame->file, EXIF and the rotation tables moved into their own module.
capture_io = pytest.importorskip("muga.camera_capture_io")

from muga.camera_orientation import (  # noqa: E402
    ORIENT_BOTTOM_UP,
    ORIENT_LEFT_UP,
    ORIENT_NORMAL,
    ORIENT_RIGHT_UP,
)


def _win(**attrs) -> SimpleNamespace:
    """A stand-in ``self`` with a pass-through translator."""
    attrs.setdefault("_", lambda s: s)
    return SimpleNamespace(**attrs)


# ---------------------------------------------------------------------------
# Device classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("device", "expected"),
    [
        (None, False),
        ({}, False),
        ({"source_factory": "v4l2src"}, False),
        ({"source_factory": "droidcamsrc"}, True),
        ({"source_factory": "pipewiresrc"}, False),
    ],
)
def test_is_halium_device(device, expected) -> None:
    assert camera._is_halium_device(device) is expected


# ---------------------------------------------------------------------------
# Aspect-ratio labels
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("w", "h", "expected"),
    [
        (1920, 1080, "16:9"),
        (1280, 720, "16:9"),
        (640, 480, "4:3"),
        (4032, 3024, "4:3"),
        (3000, 2000, "3:2"),
        (1000, 1000, "1:1"),
        (2560, 2048, "5:4"),
        (0, 480, ""),
        (640, 0, ""),
    ],
)
def test_aspect_label(w, h, expected) -> None:
    assert camera.CameraWindow._aspect_label(w, h) == expected


def test_aspect_label_snaps_near_misses_to_the_familiar_name() -> None:
    """1013:757 is 1.338 — within 2% of 4:3, so the picker says "4:3" rather
    than printing the exact but useless reduced fraction."""
    assert camera.CameraWindow._aspect_label(1013, 757) == "4:3"


@pytest.mark.parametrize(("w", "h"), [(1000, 333), (2000, 1001), (2560, 1080)])
def test_aspect_label_gives_up_on_unrecognised_ratios(w, h) -> None:
    """No candidate within 2% and a reduced fraction with big terms: a label
    like "64:27" is noise in a resolution picker, so it shows nothing.

    Note 2560x1080 is marketed as 21:9 but is really 64:27 (2.370 vs 2.333),
    outside the tolerance. No camera sensor offers it, so this only matters
    if the picker is ever pointed at display modes.
    """
    assert camera.CameraWindow._aspect_label(w, h) == ""


# ---------------------------------------------------------------------------
# v4l2 error interpretation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("message", "fragment"),
    [
        ("Inappropriate ioctl for device", "isn't a v4l2 capture device"),
        ("ENOTTY", "isn't a v4l2 capture device"),
        ("Device is busy", "in use by another app"),
        ("EBUSY", "in use by another app"),
        ("resource busy", "in use by another app"),
        ("Permission denied", "video' group"),
        ("EACCES", "video' group"),
        ("streaming stopped, reason not-negotiated", "couldn't agree on a format"),
        ("no common format", "couldn't agree on a format"),
    ],
)
def test_interpret_v4l2_error_recognises_the_common_failures(message, fragment) -> None:
    win = _win(_current_device=lambda: {"path": "/dev/video0"})
    out = camera.CameraWindow._interpret_v4l2_error(win, message, "")
    assert out is not None and fragment in out


def test_interpret_v4l2_error_passes_through_the_device_path() -> None:
    win = _win(_current_device=lambda: {"path": "/dev/video2"})
    out = camera.CameraWindow._interpret_v4l2_error(win, "Device is busy", "")
    assert "/dev/video2" in out


def test_interpret_v4l2_error_copes_with_no_device() -> None:
    win = _win(_current_device=lambda: None)
    out = camera.CameraWindow._interpret_v4l2_error(win, "Device is busy", "")
    assert out is not None and "()" not in out


def test_interpret_v4l2_error_reads_the_debug_string_too() -> None:
    """GStreamer often puts the real cause in the debug field, not the message."""
    win = _win(_current_device=lambda: None)
    out = camera.CameraWindow._interpret_v4l2_error(
        win, "Internal data stream error", "v4l2src: EACCES opening device",
    )
    assert out is not None and "video' group" in out


def test_interpret_v4l2_error_returns_none_for_unknown_faults() -> None:
    win = _win(_current_device=lambda: None)
    assert camera.CameraWindow._interpret_v4l2_error(win, "kaboom", "") is None


# ---------------------------------------------------------------------------
# Exposure menu: which value is "manual", which is "auto"
# ---------------------------------------------------------------------------

def _auto_manual(menu):
    return camera.CameraWindow._auto_manual_values(_win(), menu)


def test_auto_manual_picks_the_obvious_pair() -> None:
    assert _auto_manual({1: "Manual Mode", 3: "Auto Mode"}) == (1, 3)


def test_auto_manual_falls_back_to_aperture_priority() -> None:
    """UVC cameras frequently expose no "Auto" but do expose aperture priority."""
    assert _auto_manual({1: "Manual Mode", 8: "Aperture Priority Mode"}) == (1, 8)


def test_auto_manual_takes_anything_else_as_auto() -> None:
    manual, auto = _auto_manual({1: "Manual Mode", 4: "Shutter Priority"})
    assert (manual, auto) == (1, 4)


def test_auto_manual_handles_a_menu_with_no_manual_entry() -> None:
    manual, auto = _auto_manual({2: "Auto Mode", 5: "Something"})
    assert manual is None
    assert auto == 2


def test_auto_manual_handles_an_empty_menu() -> None:
    assert _auto_manual({}) == (None, None)


def test_auto_manual_never_returns_the_same_value_twice() -> None:
    manual, auto = _auto_manual({1: "Manual Mode"})
    assert manual == 1
    assert auto is None, "the single entry must not be both manual and auto"


# ---------------------------------------------------------------------------
# Orientation-driven child ordering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("orientation", "reversed_"),
    [
        (ORIENT_NORMAL, False),
        (ORIENT_LEFT_UP, False),
        (ORIENT_BOTTOM_UP, True),
        (ORIENT_RIGHT_UP, True),
    ],
)
def test_orient_seq(orientation, reversed_) -> None:
    win = _win(_device_orientation=orientation)
    items = ["a", "b", "c"]
    out = camera.CameraWindow._orient_seq(win, items)
    assert out == (list(reversed(items)) if reversed_ else items)
    assert items == ["a", "b", "c"], "input list was mutated"


# ---------------------------------------------------------------------------
# Rotation tables
# ---------------------------------------------------------------------------

def test_capture_rotation_and_exif_tables_agree() -> None:
    """Every rotation the capture path can produce needs an EXIF equivalent
    for the no-Pillow fallback, or the photo is saved with no orientation."""
    for deg in capture_io._CAPTURE_ROTATION_CW.values():
        assert deg in capture_io._CW_TO_EXIF_ORIENTATION


def test_icon_rotation_covers_every_orientation() -> None:
    for orientation in (ORIENT_NORMAL, ORIENT_BOTTOM_UP, ORIENT_LEFT_UP, ORIENT_RIGHT_UP):
        assert orientation in camera._ICON_ROTATION_DEG


# ---------------------------------------------------------------------------
# Self-timer
# ---------------------------------------------------------------------------

def test_cycle_timer_wraps_through_the_choices() -> None:
    win = _win(
        _timer_choices=[0, 3, 10], _timer_idx=0, _countdown_source=None,
        _refresh_timer_button=MagicMock(), _cancel_countdown=MagicMock(),
    )
    seen = []
    for _ in range(4):
        camera.CameraWindow._cycle_timer(win)
        seen.append(win._timer_choices[win._timer_idx])
    assert seen == [3, 10, 0, 3]
    assert win._refresh_timer_button.call_count == 4


def test_cycle_timer_cancels_a_running_countdown() -> None:
    """Changing the timer mid-countdown must not leave the old one ticking
    towards a capture the user no longer asked for."""
    win = _win(
        _timer_choices=[0, 3, 10], _timer_idx=1, _countdown_source=42,
        _refresh_timer_button=MagicMock(), _cancel_countdown=MagicMock(),
    )
    camera.CameraWindow._cycle_timer(win)
    win._cancel_countdown.assert_called_once()


def test_countdown_ticks_down_and_fires_the_capture() -> None:
    countdown = MagicMock()
    win = _win(_countdown_value=3, _countdown=countdown,
               _countdown_source=7, _capture=MagicMock())

    assert camera.CameraWindow._tick_countdown(win) is True
    assert win._countdown_value == 2
    countdown.set_text.assert_called_with("2")
    win._capture.assert_not_called()

    assert camera.CameraWindow._tick_countdown(win) is True
    assert camera.CameraWindow._tick_countdown(win) is False, "last tick must stop the source"
    win._capture.assert_called_once()
    assert win._countdown_source is None
    countdown.set_visible.assert_called_with(False)


def test_cancel_countdown_is_safe_when_none_is_running() -> None:
    countdown = MagicMock()
    win = _win(_countdown_source=None, _countdown=countdown)
    camera.CameraWindow._cancel_countdown(win)  # must not call source_remove
    countdown.set_visible.assert_called_once_with(False)


# ---------------------------------------------------------------------------
# Video file naming
# ---------------------------------------------------------------------------

def test_build_video_path_creates_a_unique_reserved_name(tmp_path: Path) -> None:
    win = _win(_video_dir=tmp_path / "Videos")
    first = camera.CameraWindow._build_video_path(win)
    assert first.exists(), "the name must be reserved, not just computed"
    assert first.suffix == ".mkv"

    second = camera.CameraWindow._build_video_path(win)
    assert second != first, "two recordings in the same second collided"
    assert second.exists()


def test_build_video_path_creates_the_directory(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "Videos"
    win = _win(_video_dir=target)
    camera.CameraWindow._build_video_path(win)
    assert target.is_dir()


def test_build_video_path_returns_a_path_even_when_reserving_fails(tmp_path: Path) -> None:
    """A permission error must fall through to the caller, which surfaces the
    filesink failure as a toast — not raise out of the record handler."""
    vdir = tmp_path / "Videos"
    vdir.mkdir()
    win = _win(_video_dir=vdir)
    import os as _os
    _os.chmod(vdir, 0o500)
    try:
        path = camera.CameraWindow._build_video_path(win)
        assert path.suffix == ".mkv"
    finally:
        _os.chmod(vdir, 0o700)


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def test_persist_settings_flush_writes_every_camera_field() -> None:
    settings = SimpleNamespace(
        camera_jpeg_quality=0, camera_video_bitrate_kbps=0,
        camera_image_resolution=[1, 1], handedness="right",
        camera_geo_enabled=False, camera_flash_enabled=False,
        save=MagicMock(),
    )
    win = _win(
        _settings=settings, _settings_persist_source=99,
        _jpeg_quality=88, _video_bitrate_kbps=6000,
        _image_resolution=(4032, 3024), _handedness="left",
        _geo_enabled=True, _flash_enabled=True,
    )
    assert camera.CameraWindow._persist_settings_flush(win) is False
    assert win._settings_persist_source is None
    assert settings.camera_jpeg_quality == 88
    assert settings.camera_video_bitrate_kbps == 6000
    assert settings.camera_image_resolution == [4032, 3024]
    assert settings.handedness == "left"
    assert settings.camera_geo_enabled is True
    assert settings.camera_flash_enabled is True
    settings.save.assert_called_once()


def test_persist_settings_flush_stores_native_resolution_as_none() -> None:
    settings = SimpleNamespace(
        camera_jpeg_quality=0, camera_video_bitrate_kbps=0,
        camera_image_resolution=[1, 1], handedness="right",
        camera_geo_enabled=False, camera_flash_enabled=False, save=MagicMock(),
    )
    win = _win(_settings=settings, _settings_persist_source=None,
               _jpeg_quality=92, _video_bitrate_kbps=4000, _image_resolution=None,
               _handedness="right", _geo_enabled=False, _flash_enabled=False)
    camera.CameraWindow._persist_settings_flush(win)
    assert settings.camera_image_resolution is None


def test_persist_settings_flush_survives_a_failing_save() -> None:
    """It runs from a GLib timeout; an exception there is unhandled."""
    settings = SimpleNamespace(
        camera_jpeg_quality=0, camera_video_bitrate_kbps=0,
        camera_image_resolution=None, handedness="right",
        camera_geo_enabled=False, camera_flash_enabled=False,
        save=MagicMock(side_effect=OSError("disk full")),
    )
    win = _win(_settings=settings, _settings_persist_source=None,
               _jpeg_quality=92, _video_bitrate_kbps=4000, _image_resolution=None,
               _handedness="right", _geo_enabled=False, _flash_enabled=False)
    assert camera.CameraWindow._persist_settings_flush(win) is False


def test_persist_settings_flush_without_settings_is_a_noop() -> None:
    win = _win(_settings=None, _settings_persist_source=5)
    assert camera.CameraWindow._persist_settings_flush(win) is False


# ---------------------------------------------------------------------------
# Quality / resolution setters
# ---------------------------------------------------------------------------

def test_set_jpeg_quality_updates_a_live_encoder() -> None:
    """Quality changes must not require a pipeline restart."""
    jpeg = MagicMock()
    pipeline = MagicMock()
    pipeline.get_by_name.return_value = jpeg
    button_hi, button_lo = MagicMock(), MagicMock()
    win = _win(_jpeg_quality=92, _pipeline=pipeline,
               _photo_quality_buttons=[(button_hi, 98), (button_lo, 75)],
               _persist_settings=MagicMock())

    camera.CameraWindow._set_jpeg_quality(win, 98)

    assert win._jpeg_quality == 98
    pipeline.get_by_name.assert_called_once_with("snap_jpeg")
    jpeg.set_property.assert_called_once_with("quality", 98)
    button_hi.add_css_class.assert_called_with("suggested-action")
    button_lo.remove_css_class.assert_called_with("suggested-action")
    win._persist_settings.assert_called_once()


def test_set_jpeg_quality_without_a_running_pipeline() -> None:
    win = _win(_jpeg_quality=92, _pipeline=None, _photo_quality_buttons=[],
               _persist_settings=MagicMock())
    camera.CameraWindow._set_jpeg_quality(win, 75)
    assert win._jpeg_quality == 75


def test_set_jpeg_quality_survives_a_refusing_encoder() -> None:
    jpeg = MagicMock()
    jpeg.set_property.side_effect = TypeError("no such property")
    pipeline = MagicMock()
    pipeline.get_by_name.return_value = jpeg
    win = _win(_jpeg_quality=92, _pipeline=pipeline, _photo_quality_buttons=[],
               _persist_settings=MagicMock())
    camera.CameraWindow._set_jpeg_quality(win, 60)
    assert win._jpeg_quality == 60


def test_set_video_bitrate_marks_the_active_button() -> None:
    lo, hi = MagicMock(), MagicMock()
    win = _win(_video_bitrate_kbps=4000, _video_quality_buttons=[(lo, 2000), (hi, 8000)],
               _persist_settings=MagicMock())
    camera.CameraWindow._set_video_bitrate(win, 8000)
    assert win._video_bitrate_kbps == 8000
    hi.add_css_class.assert_called_with("suggested-action")
    lo.remove_css_class.assert_called_with("suggested-action")


def test_set_image_resolution_matches_the_tuple_button() -> None:
    native, small = MagicMock(), MagicMock()
    win = _win(_image_resolution=None,
               _image_size_buttons=[(native, None), (small, (1920, 1080))],
               _persist_settings=MagicMock())
    camera.CameraWindow._set_image_resolution(win, (1920, 1080))
    assert win._image_resolution == (1920, 1080)
    small.add_css_class.assert_called_with("suggested-action")
    native.remove_css_class.assert_called_with("suggested-action")


def test_set_handedness_rejects_unknown_values() -> None:
    win = _win(_handedness="right", _handedness_buttons=[], _applied_layout=None,
               _persist_settings=MagicMock(), _apply_layout_for=MagicMock())
    camera.CameraWindow._set_handedness(win, "sideways")
    assert win._handedness == "right"
    win._persist_settings.assert_not_called()


def test_set_handedness_reapplies_the_layout() -> None:
    """Shutter and options bar have to move to the other side immediately."""
    left_btn, right_btn = MagicMock(), MagicMock()
    win = _win(_handedness="right", _handedness_buttons=[(left_btn, "left"), (right_btn, "right")],
               _applied_layout=ORIENT_NORMAL, _persist_settings=MagicMock(),
               _apply_layout_for=MagicMock())
    camera.CameraWindow._set_handedness(win, "left")
    assert win._handedness == "left"
    left_btn.add_css_class.assert_called_with("suggested-action")
    right_btn.remove_css_class.assert_called_with("suggested-action")
    win._apply_layout_for.assert_called_once_with(ORIENT_NORMAL)


def test_set_handedness_to_the_current_value_is_a_noop() -> None:
    win = _win(_handedness="left", _handedness_buttons=[], _applied_layout=None,
               _persist_settings=MagicMock(), _apply_layout_for=MagicMock())
    camera.CameraWindow._set_handedness(win, "left")
    win._persist_settings.assert_not_called()


# ---------------------------------------------------------------------------
# APP1 / EXIF patching — byte surgery on a real JPEG
# ---------------------------------------------------------------------------

def _minimal_jpeg(segments: bytes = b"") -> bytes:
    """SOI + optional segments + SOS + a byte of "image data" + EOI."""
    return b"\xff\xd8" + segments + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x7f" + b"\xff\xd9"


def _app_segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def test_exif_patch_inserts_an_app1_segment(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    path.write_bytes(_minimal_jpeg())
    capture_io._write_exif_app1_inplace(path, b"II*\x00TIFFDATA")

    out = path.read_bytes()
    assert out[:2] == b"\xff\xd8"
    assert out[2:4] == b"\xff\xe1", "APP1 must come first"
    length = struct.unpack(">H", out[4:6])[0]
    assert out[6:6 + 6] == b"Exif\x00\x00"
    assert out[6 + 6:4 + length] == b"II*\x00TIFFDATA"
    assert out.endswith(b"\xff\xd9"), "image data was truncated"


def test_exif_patch_replaces_an_existing_exif_segment(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    path.write_bytes(_minimal_jpeg(_app_segment(0xE1, b"Exif\x00\x00OLDDATA")))
    capture_io._write_exif_app1_inplace(path, b"NEWDATA")

    out = path.read_bytes()
    assert b"OLDDATA" not in out, "the old EXIF block was kept as well"
    assert out.count(b"Exif\x00\x00") == 1
    assert b"NEWDATA" in out


def test_exif_patch_keeps_xmp(tmp_path: Path) -> None:
    """XMP also lives in APP1 and is not ours to drop."""
    xmp = _app_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta/>")
    path = tmp_path / "photo.jpg"
    path.write_bytes(_minimal_jpeg(xmp))
    capture_io._write_exif_app1_inplace(path, b"NEWDATA")
    assert b"<x:xmpmeta/>" in path.read_bytes()


def test_exif_patch_keeps_other_app_segments(tmp_path: Path) -> None:
    jfif = _app_segment(0xE0, b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00")
    comment = _app_segment(0xFE, b"a comment")
    path = tmp_path / "photo.jpg"
    path.write_bytes(_minimal_jpeg(jfif + comment))
    capture_io._write_exif_app1_inplace(path, b"NEWDATA")
    out = path.read_bytes()
    assert b"JFIF" in out
    assert b"a comment" in out


def test_exif_patch_ignores_a_non_jpeg(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(b"just text")
    capture_io._write_exif_app1_inplace(path, b"NEWDATA")
    assert path.read_bytes() == b"just text"


def test_exif_patch_ignores_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jpg"
    path.write_bytes(b"")
    capture_io._write_exif_app1_inplace(path, b"NEWDATA")
    assert path.read_bytes() == b""


def test_exif_patch_does_not_double_the_exif_header(tmp_path: Path) -> None:
    """Pillow 12 returns the "Exif" header inside tobytes(); older versions
    returned bare TIFF. Prefixing it a second time leaves every TIFF offset
    in the block pointing six bytes past its data. Pillow still reads that
    back — it re-finds the header — so it stayed invisible in Muga's own
    gallery, while exiftool called the segment malformed and dropped it."""
    PILImage = pytest.importorskip("PIL.Image")

    exif = PILImage.Exif()
    exif[0x010F] = "Muga"
    path = tmp_path / "shot.jpg"
    PILImage.new("RGB", (8, 8)).save(path)
    capture_io._write_exif_app1_inplace(path, exif.tobytes())

    raw = path.read_bytes()
    payload = raw[6:4 + int.from_bytes(raw[4:6], "big")]
    header = b"Exif\x00\x00"
    assert payload.startswith(header)
    assert not payload[6:].startswith(header), "the EXIF header was written twice"

    # ...and the offset the block opens with still lands inside it.
    tiff = payload[6:]
    order = "little" if tiff[:2] == b"II" else "big"
    ifd0 = int.from_bytes(tiff[4:8], order)
    assert 0 < ifd0 < len(tiff), f"IFD0 offset {ifd0} outside a {len(tiff)}-byte block"


def test_exif_patch_refuses_an_oversized_payload(tmp_path: Path) -> None:
    """APP1's length field is a uint16; anything bigger needs Extended EXIF."""
    path = tmp_path / "photo.jpg"
    original = _minimal_jpeg()
    path.write_bytes(original)
    capture_io._write_exif_app1_inplace(path, b"x" * 0xFFFF)
    assert path.read_bytes() == original, "an unsupported payload must change nothing"


def test_exif_patch_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    path.write_bytes(_minimal_jpeg())
    capture_io._write_exif_app1_inplace(path, b"DATA")
    assert list(tmp_path.iterdir()) == [path]


def test_exif_patch_result_is_readable_by_pillow(tmp_path: Path) -> None:
    """The real contract: the patched file still parses, and the EXIF is ours."""
    PILImage = pytest.importorskip("PIL.Image")

    path = tmp_path / "photo.jpg"
    PILImage.new("RGB", (32, 24), (10, 20, 30)).save(path, quality=90)

    exif = PILImage.Exif()
    exif[274] = 6           # Orientation
    exif[271] = "Muga"      # Make
    capture_io._write_exif_app1_inplace(path, exif.tobytes())

    with PILImage.open(path) as img:
        assert img.size == (32, 24), "pixel data was disturbed"
        read_back = img.getexif()
        assert read_back[274] == 6
        assert read_back[271] == "Muga"
