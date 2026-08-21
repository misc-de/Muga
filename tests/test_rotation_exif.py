"""Guards that saving a rotated or edited photo keeps its metadata.

Rotating a photo in the viewer used to write it back with a bare
``img.save(path, quality=95)``, which drops the whole EXIF block: capture
date, camera model, GPS — everything. Since a photo shot on a phone gets
rotated more often than not, that quietly stripped the metadata off most
of a user's library.

It also cost the file a protection it depends on when shared. Delta
Chat's core (``src/blob.rs``, v2.59) demotes an attachment from image to
plain file only when recoding fails *and* the file carries no EXIF at
all:

    if !is_avatar && no_exif {
        error!(context, "Cannot check/recode image, using original data: ...");
        *viewtype = Viewtype::File;
    }

So a stripped JPEG arrives as a file attachment instead of a picture,
while the very same shot straight from the phone's camera app — EXIF
intact — arrives as an image.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

PILImage = pytest.importorskip("PIL.Image")
viewer = pytest.importorskip("yaga.viewer", reason="viewer needs the GTK stack")
from PIL.TiffImagePlugin import IFDRational  # noqa: E402


# EXIF tags we care about surviving a round trip.
_MAKE, _MODEL, _SOFTWARE, _ORIENTATION, _DATETIME = (
    0x010F, 0x0110, 0x0131, 0x0112, 0x0132,
)
_DATETIME_ORIGINAL = 0x9003


def _photo_with_exif(tmp_path, orientation: int = 1, width=60, height=40):
    """A JPEG carrying the tags a phone camera writes, including a GPS fix."""
    img = PILImage.new("RGB", (width, height), (30, 90, 150))
    # Mark the top-left corner so rotations are identifiable.
    for x in range(6):
        for y in range(6):
            img.putpixel((x, y), (255, 0, 0))
    exif = PILImage.Exif()
    exif[_MAKE] = "Yaga"
    exif[_MODEL] = "TestPhone X1"
    exif[_SOFTWARE] = "Yaga"
    exif[_DATETIME] = "2026:08:21 10:00:00"
    exif[_ORIENTATION] = orientation
    exif.get_ifd(0x8769)[_DATETIME_ORIGINAL] = "2026:08:21 10:00:00"
    gps = exif.get_ifd(0x8825)
    gps[0x0001] = "N"
    gps[0x0002] = (IFDRational(52, 1), IFDRational(31, 1), IFDRational(0, 1))
    path = tmp_path / "photo.jpg"
    img.save(path, quality=95, exif=exif.tobytes())
    return path


def _photo_without_exif(tmp_path, width=60, height=40):
    """A JPEG as Yaga's own rotation used to leave it: pixels, no metadata."""
    img = PILImage.new("RGB", (width, height), (30, 90, 150))
    path = tmp_path / "bare.jpg"
    img.save(path, quality=95)
    assert "exif" not in PILImage.open(path).info
    return path


def _read(path):
    return PILImage.open(path).getexif()


# --------------------------------------------------------------------
# Viewer rotation
# --------------------------------------------------------------------


def test_rotation_keeps_camera_model_and_capture_date(tmp_path) -> None:
    path = _photo_with_exif(tmp_path)
    viewer.ViewerWindow._save_rotation_to_disk(str(path), 90)
    exif = _read(path)
    assert exif[_MODEL] == "TestPhone X1"
    assert exif[_DATETIME] == "2026:08:21 10:00:00"
    assert exif.get_ifd(0x8769)[_DATETIME_ORIGINAL] == "2026:08:21 10:00:00"


def test_rotation_keeps_gps(tmp_path) -> None:
    """GPS lives in its own IFD and is the tag most easily lost."""
    path = _photo_with_exif(tmp_path)
    viewer.ViewerWindow._save_rotation_to_disk(str(path), 180)
    gps = _read(path).get_ifd(0x8825)
    assert gps[0x0001] == "N"
    assert [float(v) for v in gps[0x0002]] == [52.0, 31.0, 0.0]


def test_rotation_normalises_orientation_tag(tmp_path) -> None:
    """The rotation goes into the pixels, so the tag must say 'upright'.
    Carrying the source's tag over would rotate the photo a second time in
    every viewer that honours it."""
    path = _photo_with_exif(tmp_path, orientation=6)
    viewer.ViewerWindow._save_rotation_to_disk(str(path), 90)
    assert _read(path)[_ORIENTATION] == 1


def test_rotation_still_rotates_the_pixels(tmp_path) -> None:
    """Metadata must not come at the cost of the actual job."""
    path = _photo_with_exif(tmp_path, width=60, height=40)
    viewer.ViewerWindow._save_rotation_to_disk(str(path), 90)
    assert PILImage.open(path).size == (40, 60)


def test_rotation_gives_a_bare_photo_minimal_exif(tmp_path) -> None:
    """Photos that already lost their metadata to the old code get a minimal
    block back — enough that a receiver never sees a JPEG with no EXIF."""
    path = _photo_without_exif(tmp_path)
    viewer.ViewerWindow._save_rotation_to_disk(str(path), 270)
    assert "exif" in PILImage.open(path).info
    exif = _read(path)
    assert exif[_ORIENTATION] == 1
    assert exif[_SOFTWARE] == "Yaga"


def test_zero_rotation_leaves_the_file_alone(tmp_path) -> None:
    """No rotation requested means no re-encode at all."""
    path = _photo_with_exif(tmp_path)
    before = path.read_bytes()
    viewer.ViewerWindow._save_rotation_to_disk(str(path), 0)
    assert path.read_bytes() == before


# --------------------------------------------------------------------
# The EXIF rebuild helper
# --------------------------------------------------------------------


def test_helper_preserves_tags_and_resets_orientation() -> None:
    exif = PILImage.Exif()
    exif[_MODEL] = "TestPhone X1"
    exif[_ORIENTATION] = 8
    img = SimpleNamespace(info={"exif": exif.tobytes()})
    out = PILImage.Exif()
    out.load(viewer._exif_for_upright_save(img))
    assert out[_MODEL] == "TestPhone X1"
    assert out[_ORIENTATION] == 1


def test_helper_never_returns_empty_for_a_bare_image() -> None:
    """The whole point vs. the old behaviour: no EXIF in must not mean no
    EXIF out."""
    blob = viewer._exif_for_upright_save(SimpleNamespace(info={}))
    assert blob
    out = PILImage.Exif()
    out.load(blob)
    assert out[_ORIENTATION] == 1


# --------------------------------------------------------------------
# Editor
# --------------------------------------------------------------------


def test_editor_writes_exif_even_without_a_source_block() -> None:
    """Same gap on the editor's save path — an edited photo whose source had
    no EXIF used to be written without any."""
    editor = pytest.importorskip("yaga.editor.view")
    blob = editor.EditorView._exif_for_save(SimpleNamespace(_exif_bytes=None))
    assert blob
    out = PILImage.Exif()
    out.load(blob)
    assert out[_ORIENTATION] == 1
    assert out[_SOFTWARE] == "Yaga"


# --------------------------------------------------------------------
# The camera's own EXIF writer
# --------------------------------------------------------------------


def _gps_ifd_from_camera(location):
    """Run the camera's GPS writer against a fresh GPS IFD."""
    camera = pytest.importorskip("yaga.camera")
    exif = PILImage.Exif()
    exif[_MAKE] = "Yaga"
    exif[_ORIENTATION] = 1
    gps = exif.get_ifd(0x8825)
    camera.CameraWindow._pillow_set_gps(SimpleNamespace(), gps, location)
    return exif


def test_geotag_does_not_destroy_the_whole_exif_block() -> None:
    """The bug this test exists for: the GPS coordinates were written as plain
    (numerator, denominator) tuples, which Pillow 11+ rejects with
    ``TypeError: bad operand type for abs()``. Because exif.tobytes()
    serialises the block in one go, that one bad tag took Make, Model,
    DateTime and Orientation down with it — every photo taken with geotagging
    enabled was saved with no EXIF at all."""
    exif = _gps_ifd_from_camera({"lat": 52.5163, "lon": 13.3777, "alt": 34.0})
    blob = exif.tobytes()  # must not raise
    out = PILImage.Exif()
    out.load(blob)
    assert out[_MAKE] == "Yaga"
    assert out[_ORIENTATION] == 1


def test_geotag_round_trips_the_coordinates() -> None:
    exif = _gps_ifd_from_camera({"lat": 52.5163, "lon": -13.3777, "alt": 34.0})
    out = PILImage.Exif()
    out.load(exif.tobytes())
    gps = out.get_ifd(0x8825)
    assert gps[0x0001] == "N"
    assert gps[0x0003] == "W"  # negative longitude
    d, m, s = (float(v) for v in gps[0x0002])
    assert abs((d + m / 60 + s / 3600) - 52.5163) < 1e-6
    assert abs(float(gps[0x0006]) - 34.0) < 0.01


def test_seconds_rounding_up_to_60_carries_over() -> None:
    """Guards the existing carry logic: EXIF requires 0 <= min, sec < 60."""
    exif = _gps_ifd_from_camera({"lat": 9.99999999, "lon": 0.0, "alt": 0.0})
    out = PILImage.Exif()
    out.load(exif.tobytes())
    d, m, s = (float(v) for v in out.get_ifd(0x8825)[0x0002])
    assert m < 60 and s < 60
    assert abs((d + m / 60 + s / 3600) - 9.99999999) < 1e-5
