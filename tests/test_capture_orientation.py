"""Guards for baking the device orientation into saved photos.

Photos used to be saved as the sensor delivered them, with the device
orientation only described by an EXIF Orientation tag — and that tag had
the two landscape lays swapped, so every photo shot sideways came out
sideways. Now ``_orient_and_resize`` rotates the pixels themselves and
the tag is left at 1.

The rotation direction is the whole point of these tests, so they check
actual pixel positions rather than just the image dimensions: a map that
turns the frame the wrong way still swaps width and height.

``CameraWindow`` can't be instantiated (PyGObject's metaclass forbids
``object.__new__`` on Gtk widget subclasses, and importing the module
pulls in GStreamer), so the methods run as unbound functions against a
``SimpleNamespace`` self carrying only the attributes they touch.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from yaga.camera_orientation import (
    ALL_ORIENTATIONS,
    ORIENT_BOTTOM_UP,
    ORIENT_LEFT_UP,
    ORIENT_NORMAL,
    ORIENT_RIGHT_UP,
)

PILImage = pytest.importorskip("PIL.Image")
camera = pytest.importorskip(
    "yaga.camera", reason="camera module needs the GStreamer bindings"
)
# Frame->file, EXIF and the rotation tables moved into their own module.
capture_io = pytest.importorskip(
    "yaga.camera_capture_io", reason="camera module needs the GStreamer bindings"
)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

# A frame whose four corners are all different colours, so a rotation is
# identifiable by which corner a colour ends up in. Portrait-shaped (tall)
# so a quarter turn also changes the aspect ratio.
_TOP_LEFT = (255, 0, 0)
_TOP_RIGHT = (0, 255, 0)
_BOTTOM_RIGHT = (0, 0, 255)
_BOTTOM_LEFT = (255, 255, 0)


def _make_jpeg(width: int = 40, height: int = 80) -> bytes:
    img = PILImage.new("RGB", (width, height), (0, 0, 0))
    px = img.load()
    for x, y, colour in (
        (0, 0, _TOP_LEFT),
        (width - 1, 0, _TOP_RIGHT),
        (width - 1, height - 1, _BOTTOM_RIGHT),
        (0, height - 1, _BOTTOM_LEFT),
    ):
        # Paint a 4x4 block, not a single pixel: JPEG is lossy and
        # chroma-subsampled, so a lone pixel would bleed into its
        # neighbours and the corner check would be reading noise.
        for dx in range(4):
            for dy in range(4):
                px[
                    min(max(x + (dx if x == 0 else -dx), 0), width - 1),
                    min(max(y + (dy if y == 0 else -dy), 0), height - 1),
                ] = colour
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()


def _corners(data: bytes) -> dict[str, tuple[int, int, int]]:
    """Read the four corner colours back, snapped to the nearest pure
    channel combination so JPEG ringing doesn't matter."""
    img = PILImage.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size

    def at(x: int, y: int) -> tuple[int, int, int]:
        r, g, b = img.getpixel((x, y))
        return (255 if r > 127 else 0, 255 if g > 127 else 0, 255 if b > 127 else 0)

    return {
        "tl": at(1, 1),
        "tr": at(w - 2, 1),
        "br": at(w - 2, h - 2),
        "bl": at(1, h - 2),
    }


def _window(orientation: str, resolution=None, quality: int = 92):
    return SimpleNamespace(
        _capture_orientation=orientation,
        _image_resolution=resolution,
        _jpeg_quality=quality,
    )


def _run(orientation: str, data: bytes, **kwargs):
    return camera.CameraWindow._orient_and_resize(
        _window(orientation, **kwargs), data
    )


# --------------------------------------------------------------------
# Rotation map
# --------------------------------------------------------------------


def test_every_orientation_has_a_rotation() -> None:
    """A real device transition must never fall through to the default —
    an unmapped lay would silently save that photo unrotated."""
    assert set(capture_io._CAPTURE_ROTATION_CW) == set(ALL_ORIENTATIONS)
    assert all(v in (0, 90, 180, 270) for v in capture_io._CAPTURE_ROTATION_CW.values())


def test_landscape_lays_rotate_opposite_ways() -> None:
    """The regression that started this: left-up and right-up were mapped
    to the same-signed quarter turn family as the EXIF cookbook suggests,
    which is mirrored from what this HAL's accelerometer reports."""
    assert capture_io._CAPTURE_ROTATION_CW[ORIENT_LEFT_UP] == 270
    assert capture_io._CAPTURE_ROTATION_CW[ORIENT_RIGHT_UP] == 90


# --------------------------------------------------------------------
# Pixel-level rotation
# --------------------------------------------------------------------


def test_upright_portrait_is_passed_through_untouched() -> None:
    """No rotation and no downscale means no decode/re-encode at all —
    the common shot must not pay a JPEG generation."""
    data = _make_jpeg()
    out, tag = _run(ORIENT_NORMAL, data)
    assert out is data
    assert tag == 1


def test_bottom_up_turns_the_frame_over() -> None:
    data = _make_jpeg()
    out, tag = _run(ORIENT_BOTTOM_UP, data)
    assert tag == 1
    corners = _corners(out)
    assert corners["br"] == _TOP_LEFT
    assert corners["tl"] == _BOTTOM_RIGHT


@pytest.mark.parametrize(
    "orientation, expected_tl",
    [
        # left-up needs 270° CW, so the frame's top-right corner is what
        # ends up in the top-left. (Don't reason from the name: this
        # HAL's accelerometer X axis is inverted, so the orientation
        # string is mirrored against the physical lay it describes —
        # see _classify_orientation in camera_orientation.py.)
        (ORIENT_LEFT_UP, _TOP_RIGHT),
        # right-up is the mirror: 90° CW, so bottom-left goes top-left.
        (ORIENT_RIGHT_UP, _BOTTOM_LEFT),
    ],
)
def test_landscape_rotations_move_pixels_the_right_way(
    orientation: str, expected_tl: tuple[int, int, int]
) -> None:
    """Whichever corner colour ends up top-left identifies the direction
    of the quarter turn — a swap here is exactly the bug users saw as
    'landscape photos are sideways'."""
    data = _make_jpeg(40, 80)
    out, tag = _run(orientation, data)
    assert tag == 1
    assert PILImage.open(io.BytesIO(out)).size == (80, 40)
    assert _corners(out)["tl"] == expected_tl


def test_left_up_and_right_up_are_inverses() -> None:
    """Sanity check on the pair: rotating a frame both ways must land the
    same corner in opposite places."""
    data = _make_jpeg()
    left, _ = _run(ORIENT_LEFT_UP, data)
    right, _ = _run(ORIENT_RIGHT_UP, data)
    assert _corners(left)["tl"] == _corners(right)["br"]


# --------------------------------------------------------------------
# Interaction with the downscale and the EXIF fallback
# --------------------------------------------------------------------


def test_downscale_and_rotation_share_one_encode() -> None:
    """Both transforms run in a single Pillow pass, so a resized photo
    doesn't get encoded twice."""
    data = _make_jpeg(400, 800)
    out, tag = _run(ORIENT_RIGHT_UP, data, resolution=(200, 200))
    assert tag == 1
    w, h = PILImage.open(io.BytesIO(out)).size
    # Fitted inside 200x200 as a portrait frame (100x200), then turned
    # a quarter — so landscape, and within the box either way.
    assert (w, h) == (200, 100)


def test_resolution_target_larger_than_frame_does_not_re_encode() -> None:
    """thumbnail() only ever downscales; when the frame already fits and
    there's no rotation the bytes must pass through."""
    data = _make_jpeg(40, 80)
    out, tag = _run(ORIENT_NORMAL, data, resolution=(4000, 4000))
    assert out is data
    assert tag == 1


def _make_textured_jpeg(width: int, height: int, quality: int = 95) -> bytes:
    """A frame with enough high-frequency detail that the JPEG quality
    setting actually shows up in the file size. Deterministic, so the
    size comparisons below don't flake."""
    img = PILImage.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            v = (x * 37 + y * 71) % 256
            px[x, y] = (v, (v * 5) % 256, (v * 11) % 256)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def test_pure_rotation_re_encodes_above_the_quality_preset() -> None:
    """Straightening a photo is a generation the user never asked for, so
    it must not be taken at a low quality preset — that would make simply
    holding the phone sideways degrade the shot."""
    data = _make_textured_jpeg(120, 240)
    out, _ = _run(ORIENT_RIGHT_UP, data, quality=20)
    at_preset = io.BytesIO()
    PILImage.open(io.BytesIO(data)).save(at_preset, format="JPEG", quality=20)
    assert len(out) > len(at_preset.getvalue()) * 1.5


def test_downscale_still_honours_the_quality_preset() -> None:
    """The rotation boost must not leak into the downscale path — a
    resize re-encodes anyway, and there the preset is exactly what the
    user picked."""
    data = _make_textured_jpeg(400, 800)
    low, _ = _run(ORIENT_RIGHT_UP, data, resolution=(200, 200), quality=20)
    high, _ = _run(ORIENT_RIGHT_UP, data, resolution=(200, 200), quality=95)
    assert len(low) < len(high)


def test_broken_jpeg_falls_back_to_the_exif_tag() -> None:
    """If the pixels can't be rotated the rotation has to survive as a
    tag instead — a photo that's wrong in some viewers beats one that's
    wrong in all of them."""
    out, tag = _run(ORIENT_LEFT_UP, b"not a jpeg at all")
    assert out == b"not a jpeg at all"
    # 270° CW outstanding == EXIF 8.
    assert tag == 8
    assert capture_io._CW_TO_EXIF_ORIENTATION[90] == 6
    assert capture_io._CW_TO_EXIF_ORIENTATION[180] == 3


# --------------------------------------------------------------------
# Latching
# --------------------------------------------------------------------


def test_exif_basics_carry_the_passed_orientation() -> None:
    """The writer must tag what it was handed — hardcoding 1 would strip
    the fallback path's rotation back off."""
    win = SimpleNamespace(_current_device=lambda: {"name": "Test Cam"})
    basics = camera.CameraWindow._current_exif_basics(win, 8)
    assert basics["orientation"] == 8
    assert camera.CameraWindow._current_exif_basics(win)["orientation"] == 1


def test_capture_latches_the_orientation_at_shutter_time() -> None:
    """Saving happens well after the frame is taken (on Halium the whole
    pipeline is rebuilt in between). If the save path read the live
    orientation, turning the phone while the photo was being written
    would rotate it by however far it had drifted."""
    # The shutter lives in camera.py, the save path in camera_capture_io.py.
    body = "\n".join(
        open((mod.__file__ or "").replace(".pyc", ".py"), encoding="utf-8").read()
        for mod in (camera, capture_io)
    )
    capture = body.split("def _capture(self) -> None:", 1)[1]
    capture = capture.split("\n    def ", 1)[0]
    assert "self._capture_orientation = self._device_orientation" in capture
    # ...and the save path must read the latched value, never the live one.
    orient = body.split("def _orient_and_resize", 1)[1].split("\n    def ", 1)[0]
    assert "_capture_orientation" in orient
    assert "_device_orientation" not in orient
