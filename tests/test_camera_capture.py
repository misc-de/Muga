"""Tests for camera.py's capture path: frame → file → EXIF.

This is where a captured photo either survives or doesn't, and several of the
behaviours here exist because they previously went wrong: the O_EXCL save loop
closes a TOCTOU race on a shared save dir, and the GPS block is serialised
separately because a single bad rational used to cost the photo its *entire*
EXIF (Pillow 11 raises on tuple rationals, and exif.tobytes() builds the whole
block in one go).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

camera = pytest.importorskip("muga.camera")
# Frame->file, EXIF and the rotation tables moved into their own module.
capture_io = pytest.importorskip("muga.camera_capture_io")


class _MapInfo:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _Buffer:
    """Mimics Gst.Buffer's map/unmap protocol."""

    def __init__(self, data: bytes | None, map_ok: bool = True) -> None:
        self._data = data
        self._map_ok = map_ok
        self.unmapped = False

    def map(self, _flags):
        if not self._map_ok:
            return False, None
        return True, _MapInfo(self._data)

    def unmap(self, _info):
        self.unmapped = True


class _Sample:
    def __init__(self, buffer) -> None:
        self._buffer = buffer

    def get_buffer(self):
        return self._buffer


class _FakeGst:
    class MapFlags:
        READ = "READ"


def _win(tmp_path: Path, **attrs) -> SimpleNamespace:
    defaults = dict(
        _=lambda s: s,
        _Gst=_FakeGst(),
        _save_dir=tmp_path / "Photos",
        _show_toast=MagicMock(),
        _orient_and_resize=lambda data, orientation=None: (data, 1),
        _write_exif=MagicMock(),
        _on_captured=None,
        _geo=None,
        _current_device=lambda: None,
        _capture_orientation="normal",
        _closing=False,
        _save_thread=None,
    )
    defaults.update(attrs)
    win = SimpleNamespace(**defaults)
    # The save path is three methods now — a main-loop prepare, a worker
    # persist and a main-loop report. The tests still drive it through
    # _write_sample as one operation, so bind whichever of the three the
    # test isn't stubbing out itself.
    for name in ("_prepare_capture", "_persist_capture", "_report_capture"):
        if name not in attrs:
            setattr(win, name, MethodType(getattr(camera.CameraWindow, name), win))
    return win


JPEG = b"\xff\xd8\xff\xe0jpegbytes\xff\xd9"


# ---------------------------------------------------------------------------
# Writing the captured frame
# ---------------------------------------------------------------------------

def test_capture_writes_the_frame_and_reports_it(tmp_path: Path) -> None:
    on_captured = MagicMock()
    win = _win(tmp_path, _on_captured=on_captured)
    buf = _Buffer(JPEG)

    camera.CameraWindow._write_sample(win, _Sample(buf))

    files = list((tmp_path / "Photos").glob("*.jpg"))
    assert len(files) == 1
    assert files[0].read_bytes() == JPEG
    assert files[0].stat().st_mode & 0o777 == 0o644
    assert buf.unmapped, "the Gst buffer was never unmapped"
    win._write_exif.assert_called_once_with(files[0], 1)
    on_captured.assert_called_once_with(files[0])


def test_capture_creates_the_save_directory(tmp_path: Path) -> None:
    win = _win(tmp_path, _save_dir=tmp_path / "a" / "b" / "Photos")
    camera.CameraWindow._write_sample(win, _Sample(_Buffer(JPEG)))
    assert (tmp_path / "a" / "b" / "Photos").is_dir()


def test_capture_never_overwrites_an_existing_photo(tmp_path: Path) -> None:
    """Two shutter presses inside the same second must not collide — the
    O_EXCL loop also closes the symlink race on a shared save dir."""
    win = _win(tmp_path)
    for _ in range(3):
        camera.CameraWindow._write_sample(win, _Sample(_Buffer(JPEG)))
    files = sorted(p.name for p in (tmp_path / "Photos").glob("*.jpg"))
    assert len(files) == 3, f"photos overwrote each other: {files}"
    assert len({f for f in files}) == 3


def test_capture_reports_a_sample_without_a_buffer(tmp_path: Path) -> None:
    win = _win(tmp_path)
    camera.CameraWindow._write_sample(win, _Sample(None))
    win._show_toast.assert_called_once_with("No frame available")
    assert not (tmp_path / "Photos").exists()


def test_capture_reports_a_missing_sample(tmp_path: Path) -> None:
    win = _win(tmp_path)
    camera.CameraWindow._write_sample(win, None)
    win._show_toast.assert_called_once_with("No frame available")


def test_capture_reports_an_unmappable_buffer(tmp_path: Path) -> None:
    win = _win(tmp_path)
    camera.CameraWindow._write_sample(win, _Sample(_Buffer(JPEG, map_ok=False)))
    win._show_toast.assert_called_once_with("Could not read frame")


def test_capture_reports_an_unwritable_directory(tmp_path: Path) -> None:
    save = tmp_path / "Photos"
    save.mkdir()
    os.chmod(save, 0o500)
    win = _win(tmp_path, _save_dir=save)
    try:
        camera.CameraWindow._write_sample(win, _Sample(_Buffer(JPEG)))
    finally:
        os.chmod(save, 0o700)
    assert win._show_toast.call_count == 1
    assert "Failed to save" in win._show_toast.call_args[0][0]
    win._write_exif.assert_not_called()


def test_capture_passes_the_orientation_through_to_exif(tmp_path: Path) -> None:
    """When the pixels could not be rotated, the EXIF tag has to carry it."""
    win = _win(
        tmp_path, _orient_and_resize=lambda data, orientation=None: (data, 6)
    )
    camera.CameraWindow._write_sample(win, _Sample(_Buffer(JPEG)))
    assert win._write_exif.call_args[0][1] == 6


# ---------------------------------------------------------------------------
# Writing it off the main loop
# ---------------------------------------------------------------------------

def test_async_capture_writes_on_a_worker_and_reports_back(tmp_path: Path) -> None:
    """Rotating and re-encoding a 20 MP JPEG costs ~530 ms on a phone. It used
    to run in the same main-loop callback that had just queued the preview
    rebuild, so the rebuild could not start until the encode was done."""
    win = _win(tmp_path)
    caller = threading.current_thread()
    saw: dict = {}
    win._orient_and_resize = lambda data, orientation=None: (
        saw.setdefault("thread", threading.current_thread()), (data, 1)
    )[1]

    with patch.object(capture_io.GLib, "idle_add") as idle:
        camera.CameraWindow._write_sample_async(win, _Sample(_Buffer(JPEG)))
        win._save_thread.join(timeout=5)

    assert not win._save_thread.is_alive()
    assert saw["thread"] is not caller, "the encode still ran on the main loop"
    assert list((tmp_path / "Photos").glob("*.jpg"))
    # The toast and the gallery hook are widgets' business — main loop only.
    idle.assert_called_once()
    assert idle.call_args[0][0] == win._report_capture
    win._show_toast.assert_not_called()


def test_async_capture_latches_the_orientation_against_the_next_shot(
    tmp_path: Path,
) -> None:
    """The shutter is re-armed as soon as the frame is in hand, so a second
    press can move _capture_orientation while the first photo is still being
    encoded. The worker has to use the lay the shot was framed at."""
    win = _win(tmp_path, _capture_orientation="left-up")
    seen: list[str | None] = []
    gate = threading.Event()

    def _slow_orient(data, orientation=None):
        gate.wait(timeout=5)
        seen.append(orientation)
        return data, 1

    win._orient_and_resize = _slow_orient
    with patch.object(capture_io.GLib, "idle_add"):
        camera.CameraWindow._write_sample_async(win, _Sample(_Buffer(JPEG)))
        win._capture_orientation = "bottom-up"  # the next shutter press
        gate.set()
        win._save_thread.join(timeout=5)

    assert seen == ["left-up"]


def test_async_capture_reports_a_missing_frame_without_a_worker(
    tmp_path: Path,
) -> None:
    win = _win(tmp_path)
    camera.CameraWindow._write_sample_async(win, _Sample(None))
    win._show_toast.assert_called_once_with("No frame available")
    assert win._save_thread is None


def test_capture_stays_quiet_about_the_save_while_closing(tmp_path: Path) -> None:
    """The toast belongs to a window that is going away; the photo still
    belongs in the gallery."""
    on_captured = MagicMock()
    win = _win(tmp_path, _closing=True, _on_captured=on_captured)
    camera.CameraWindow._write_sample(win, _Sample(_Buffer(JPEG)))
    win._show_toast.assert_not_called()
    on_captured.assert_called_once()


def test_capture_survives_a_raising_callback(tmp_path: Path) -> None:
    """The gallery's refresh hook must not take the capture down with it —
    the photo is already on disk at that point."""
    win = _win(tmp_path, _on_captured=MagicMock(side_effect=RuntimeError("boom")))
    camera.CameraWindow._write_sample(win, _Sample(_Buffer(JPEG)))
    assert list((tmp_path / "Photos").glob("*.jpg"))


# ---------------------------------------------------------------------------
# EXIF basics shared by both backends
# ---------------------------------------------------------------------------

def test_exif_basics_defaults_to_upright(tmp_path: Path) -> None:
    win = _win(tmp_path)
    basics = camera.CameraWindow._current_exif_basics(win)
    assert basics["orientation"] == 1
    assert basics["make"] == "Muga"
    assert basics["software"] == "Muga"
    assert basics["model"] == ""


def test_exif_basics_carries_the_device_name(tmp_path: Path) -> None:
    win = _win(tmp_path, _current_device=lambda: {"name": "Integrated Webcam"})
    assert camera.CameraWindow._current_exif_basics(win)["model"] == "Integrated Webcam"


def test_exif_basics_scrubs_control_characters(tmp_path: Path) -> None:
    """v4l2 device names come straight from the kernel and can hold anything;
    a raw control byte in an EXIF string breaks readers."""
    win = _win(tmp_path, _current_device=lambda: {"name": "Cam\x00\x07\ttest☃"})
    model = camera.CameraWindow._current_exif_basics(win)["model"]
    assert all(0x20 <= ord(c) <= 0x7E for c in model), model
    assert "test" in model


def test_exif_basics_truncates_a_long_model(tmp_path: Path) -> None:
    win = _win(tmp_path, _current_device=lambda: {"name": "X" * 200})
    assert len(camera.CameraWindow._current_exif_basics(win)["model"]) == 64


# ---------------------------------------------------------------------------
# Pillow EXIF writer
# ---------------------------------------------------------------------------

def _exif_win(tmp_path: Path, **attrs) -> SimpleNamespace:
    """A ``self`` for _write_exif_pillow, which calls two sibling methods —
    bind the real ones so the test exercises the whole writer."""
    win = _win(tmp_path, **attrs)
    for name in ("_current_exif_basics", "_pillow_set_gps"):
        setattr(win, name, getattr(camera.CameraWindow, name).__get__(win, type(win)))
    return win


def _photo(tmp_path: Path) -> Path:
    PILImage = pytest.importorskip("PIL.Image")
    path = tmp_path / "shot.jpg"
    PILImage.new("RGB", (48, 32), (5, 10, 15)).save(path, quality=90)
    return path


def test_pillow_exif_writes_the_basic_tags(tmp_path: Path) -> None:
    PILImage = pytest.importorskip("PIL.Image")
    path = _photo(tmp_path)
    win = _exif_win(tmp_path, _current_device=lambda: {"name": "TestCam"})

    camera.CameraWindow._write_exif_pillow(win, path, 1)

    with PILImage.open(path) as img:
        exif = img.getexif()
        assert exif[0x010F] == "Muga"          # Make
        assert exif[0x0110] == "TestCam"       # Model
        assert exif[0x0131] == "Muga"          # Software
        assert exif[0x0112] == 1               # Orientation
        assert exif[0x0132]                    # DateTime
        assert img.size == (48, 32), "the writer re-encoded the pixels"


def test_pillow_exif_records_a_fallback_orientation(tmp_path: Path) -> None:
    PILImage = pytest.importorskip("PIL.Image")
    path = _photo(tmp_path)
    camera.CameraWindow._write_exif_pillow(_exif_win(tmp_path), path, 8)
    with PILImage.open(path) as img:
        assert img.getexif()[0x0112] == 8


def test_pillow_exif_writes_datetime_original(tmp_path: Path) -> None:
    PILImage = pytest.importorskip("PIL.Image")
    path = _photo(tmp_path)
    camera.CameraWindow._write_exif_pillow(_exif_win(tmp_path), path, 1)
    with PILImage.open(path) as img:
        sub = img.getexif().get_ifd(0x8769)
        assert sub[0x9003]                     # DateTimeOriginal
        assert sub[0x9004]                     # DateTimeDigitized


def test_pillow_exif_writes_gps_when_a_fix_is_available(tmp_path: Path) -> None:
    PILImage = pytest.importorskip("PIL.Image")
    path = _photo(tmp_path)
    geo = SimpleNamespace(latest=lambda: {"lat": 52.5200, "lon": 13.4050, "alt": 34.0})
    win = _exif_win(tmp_path, _geo=geo)

    camera.CameraWindow._write_exif_pillow(win, path, 1)

    with PILImage.open(path) as img:
        gps = img.getexif().get_ifd(0x8825)
    assert gps[0x0001] == "N"
    assert gps[0x0003] == "E"
    deg, minute, sec = gps[0x0002]
    recovered = float(deg) + float(minute) / 60 + float(sec) / 3600
    assert abs(recovered - 52.5200) < 1e-6


def test_pillow_exif_marks_southern_and_western_hemispheres(tmp_path: Path) -> None:
    PILImage = pytest.importorskip("PIL.Image")
    path = _photo(tmp_path)
    geo = SimpleNamespace(latest=lambda: {"lat": -33.8688, "lon": -151.2093, "alt": -5.0})
    camera.CameraWindow._write_exif_pillow(_exif_win(tmp_path, _geo=geo), path, 1)
    with PILImage.open(path) as img:
        gps = img.getexif().get_ifd(0x8825)
    assert gps[0x0001] == "S"
    assert gps[0x0003] == "W"
    # AltitudeRef is a BYTE tag, so Pillow reads it back as bytes.
    ref = gps[0x0005]
    assert (ref if isinstance(ref, int) else ref[0]) == 1, "below sea level must set AltitudeRef"


def test_pillow_exif_keeps_the_basics_when_gps_fails(tmp_path: Path) -> None:
    """The whole reason GPS is serialised separately: one bad rational used to
    take the camera model, date and orientation down with it."""
    PILImage = pytest.importorskip("PIL.Image")
    path = _photo(tmp_path)
    geo = SimpleNamespace(latest=lambda: {"lat": 1.0, "lon": 2.0})
    win = _exif_win(tmp_path, _geo=geo, _current_device=lambda: {"name": "TestCam"})

    with patch.object(camera.CameraWindow, "_pillow_set_gps",
                      side_effect=TypeError("bad rational")):
        camera.CameraWindow._write_exif_pillow(win, path, 1)

    with PILImage.open(path) as img:
        exif = img.getexif()
    assert exif[0x0110] == "TestCam", "basic EXIF was lost along with the GPS"
    assert exif[0x0112] == 1


def test_pillow_exif_without_a_geo_fix(tmp_path: Path) -> None:
    PILImage = pytest.importorskip("PIL.Image")
    path = _photo(tmp_path)
    win = _exif_win(tmp_path, _geo=SimpleNamespace(latest=lambda: None))
    camera.CameraWindow._write_exif_pillow(win, path, 1)
    with PILImage.open(path) as img:
        assert not dict(img.getexif().get_ifd(0x8825))


def test_pillow_exif_ignores_an_unwritable_file(tmp_path: Path) -> None:
    """A failure here must not escape into the capture path."""
    win = _exif_win(tmp_path)
    camera.CameraWindow._write_exif_pillow(win, tmp_path / "missing.jpg", 1)


# ---------------------------------------------------------------------------
# GPS degree/minute/second conversion
# ---------------------------------------------------------------------------

def _dms(lat: float, lon: float, alt: float = 0.0) -> dict:
    pytest.importorskip("PIL.Image")
    gps: dict = {}
    camera.CameraWindow._pillow_set_gps(
        SimpleNamespace(), gps, {"lat": lat, "lon": lon, "alt": alt},
    )
    return gps


def _to_decimal(triple) -> float:
    d, m, s = triple
    return float(d) + float(m) / 60 + float(s) / 3600


@pytest.mark.parametrize("value", [0.0, 0.5, 52.52, 89.999999, 45.000001, 12.3456789])
def test_gps_conversion_round_trips(value) -> None:
    gps = _dms(value, value)
    assert abs(_to_decimal(gps[0x0002]) - value) < 1e-6


def test_gps_never_emits_sixty(): 
    """EXIF requires 0 <= minutes, seconds < 60; a naive round produced 60"
    on values that land just under a whole minute."""
    for value in (1.9999999, 0.0166666666, 10.99999999, 59.9999999):
        gps = _dms(value, value)
        _, minute, sec = gps[0x0002]
        assert float(minute) < 60, f"{value} produced {float(minute)} minutes"
        assert float(sec) < 60, f"{value} produced {float(sec)} seconds"


def test_gps_version_and_altitude() -> None:
    gps = _dms(10.0, 20.0, alt=123.45)
    assert gps[0x0000] == b"\x02\x02\x00\x00"
    assert gps[0x0005] == 0
    assert abs(float(gps[0x0006]) - 123.45) < 0.01


def test_gps_is_skipped_without_coordinates() -> None:
    pytest.importorskip("PIL.Image")
    gps: dict = {}
    camera.CameraWindow._pillow_set_gps(SimpleNamespace(), gps, {"lat": None, "lon": 5.0})
    assert gps == {}
    camera.CameraWindow._pillow_set_gps(SimpleNamespace(), gps, {})
    assert gps == {}


def test_gps_uses_ifd_rationals() -> None:
    """Plain tuples used to survive the encoder but raise on Pillow 11+."""
    pytest.importorskip("PIL.Image")
    from PIL.TiffImagePlugin import IFDRational

    gps = _dms(48.1372, 11.5756)
    assert all(isinstance(v, IFDRational) for v in gps[0x0002])
    assert isinstance(gps[0x0006], IFDRational)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def test_write_exif_prefers_gexiv2_when_present(tmp_path: Path) -> None:
    win = _win(tmp_path, _write_exif_gexiv2=MagicMock(), _write_exif_pillow=MagicMock())
    with patch.object(capture_io, "_HAS_GEXIV2", True):
        camera.CameraWindow._write_exif(win, tmp_path / "x.jpg", 3)
    win._write_exif_gexiv2.assert_called_once_with(tmp_path / "x.jpg", 3)
    win._write_exif_pillow.assert_not_called()


def test_write_exif_falls_back_to_pillow(tmp_path: Path) -> None:
    win = _win(tmp_path, _write_exif_gexiv2=MagicMock(), _write_exif_pillow=MagicMock())
    with patch.object(capture_io, "_HAS_GEXIV2", False):
        camera.CameraWindow._write_exif(win, tmp_path / "x.jpg", 1)
    win._write_exif_pillow.assert_called_once_with(tmp_path / "x.jpg", 1)
    win._write_exif_gexiv2.assert_not_called()
