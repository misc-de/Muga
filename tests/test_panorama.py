"""Tests for 360° photos: recognising them, and the rotations behind the view.

The XMP parsing has to cope with both RDF forms and with whatever prefix a
writer bound the GPano namespace to; the rotations decide where the user
ends up looking, so a sign error there is a sphere that turns the wrong way.
The GL drawing itself needs a display and is checked by screenshots.
"""

from __future__ import annotations

import math

import pytest

panorama = pytest.importorskip("muga.panorama")

NS = 'xmlns:GPano="http://ns.google.com/photos/1.0/panorama/"'


def _xmp(body: str, ns: str = NS) -> str:
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description rdf:about="" {ns} {body}</rdf:Description></rdf:RDF></x:xmpmeta>'
    )


def _close(a, b, eps=1e-6) -> bool:
    return all(abs(x - y) < eps for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Recognising
# ---------------------------------------------------------------------------

def test_attribute_form_full_sphere() -> None:
    info = panorama.panorama_info_from_xmp(_xmp('GPano:ProjectionType="equirectangular">'))
    assert info == panorama.PanoramaInfo()
    assert info.wraps


def test_element_form() -> None:
    info = panorama.panorama_info_from_xmp(_xmp(
        "><GPano:ProjectionType>equirectangular</GPano:ProjectionType>"
        "<GPano:FullPanoWidthPixels>8000</GPano:FullPanoWidthPixels>"
        "<GPano:FullPanoHeightPixels>4000</GPano:FullPanoHeightPixels>"
    ))
    assert info is not None and info.wraps


def test_other_prefix_for_the_namespace() -> None:
    ns = 'xmlns:pano="http://ns.google.com/photos/1.0/panorama/"'
    info = panorama.panorama_info_from_xmp(_xmp('pano:ProjectionType="equirectangular">', ns))
    assert info is not None


def test_not_equirectangular_is_no_panorama() -> None:
    assert panorama.panorama_info_from_xmp(_xmp('GPano:ProjectionType="cylindrical">')) is None
    assert panorama.panorama_info_from_xmp(_xmp(">")) is None
    # The field without the namespace bound means nothing
    assert panorama.panorama_info_from_xmp(_xmp('GPano:ProjectionType="equirectangular">', ns="")) is None


def test_cropped_panorama_as_fractions() -> None:
    info = panorama.panorama_info_from_xmp(_xmp(
        'GPano:ProjectionType="equirectangular" GPano:FullPanoWidthPixels="8000" '
        'GPano:FullPanoHeightPixels="4000" GPano:CroppedAreaImageWidthPixels="4000" '
        'GPano:CroppedAreaImageHeightPixels="1000" GPano:CroppedAreaLeftPixels="2000" '
        'GPano:CroppedAreaTopPixels="1500">'
    ))
    assert (info.left, info.top, info.width, info.height) == (0.25, 0.375, 0.5, 0.25)
    assert not info.wraps


def test_nonsense_crop_falls_back_to_the_full_sphere() -> None:
    info = panorama.panorama_info_from_xmp(_xmp(
        'GPano:ProjectionType="equirectangular" GPano:FullPanoWidthPixels="8000" '
        'GPano:FullPanoHeightPixels="4000" GPano:CroppedAreaImageWidthPixels="99999" '
        'GPano:CroppedAreaImageHeightPixels="-3" GPano:CroppedAreaLeftPixels="nan">'
    ))
    assert info.wraps and info.height == 1.0 and info.left == 0.0


def test_initial_view_relative_to_the_pose() -> None:
    info = panorama.panorama_info_from_xmp(_xmp(
        'GPano:ProjectionType="equirectangular" GPano:PoseHeadingDegrees="350" '
        'GPano:InitialViewHeadingDegrees="20" GPano:InitialViewPitchDegrees="-10">'
    ))
    assert info.initial_heading == pytest.approx(30.0)
    assert info.initial_pitch == -10.0
    assert info.pose_heading == 350.0


def test_no_pose_no_heading() -> None:
    info = panorama.panorama_info_from_xmp(_xmp('GPano:ProjectionType="equirectangular">'))
    assert info.pose_heading is None


def test_compass_heading_is_the_longitude_of_the_device_view() -> None:
    """With the rotation vector, longitude 0 of the device view is north:
    a phone facing east looks at +90°, so a photo whose middle faces
    *pose* degrees is shown at heading - pose."""
    upright = panorama.quaternion_matrix(*_upright_facing_north())
    east = panorama.mat_mul(panorama.rot_z(-90.0), upright)
    m = panorama.mat_mul(panorama.ANDROID_TO_SPHERE, east)
    assert panorama.longitude_of(panorama.mat_apply(m, FORWARD)) == pytest.approx(90.0)
    pose = 120.0
    shown = panorama.mat_mul(panorama.rot_y(pose), m)  # yaw offset -pose
    assert panorama.longitude_of(panorama.mat_apply(shown, FORWARD)) == pytest.approx(90.0 - pose)


def test_read_from_a_file(tmp_path) -> None:
    pano = tmp_path / "pano.jpg"
    pano.write_bytes(b"\xff\xd8\xff\xe1junk" + _xmp('GPano:ProjectionType="equirectangular">').encode() + b"\xff\xd9")
    flat = tmp_path / "flat.jpg"
    flat.write_bytes(b"\xff\xd8\xff\xe0 nothing here \xff\xd9")
    assert panorama.read_panorama_info(str(pano)) is not None
    assert panorama.read_panorama_info(str(flat)) is None
    assert panorama.read_panorama_info(str(tmp_path / "missing.jpg")) is None


# ---------------------------------------------------------------------------
# Rotations
# ---------------------------------------------------------------------------

FORWARD = (0.0, 0.0, -1.0)


@pytest.mark.parametrize("yaw", [0.0, 45.0, 90.0, -120.0])
def test_view_matrix_looks_where_asked(yaw: float) -> None:
    forward = panorama.mat_apply(panorama.view_matrix(yaw, 20.0), FORWARD)
    assert panorama.longitude_of(forward) == pytest.approx(yaw)
    assert panorama.latitude_of(forward) == pytest.approx(20.0)


def test_view_keeps_the_horizon_level() -> None:
    right = panorama.mat_apply(panorama.view_matrix(70.0, 30.0), (1.0, 0.0, 0.0))
    assert right[1] == pytest.approx(0.0)


def _upright_facing_north():
    """The phone held upright in portrait, screen towards the user, back to north."""
    s = math.sin(math.radians(45))
    return (s, 0.0, 0.0, s)  # 90° about the device's x axis


def test_phone_upright_looks_at_the_horizon() -> None:
    q = _upright_facing_north()
    forward = panorama.mat_apply(panorama.device_view(q, 0), FORWARD)
    assert panorama.latitude_of(forward) == pytest.approx(0.0, abs=1e-6)
    assert panorama.longitude_of(forward) == pytest.approx(0.0, abs=1e-6)
    up = panorama.mat_apply(panorama.device_view(q, 0), (0.0, 1.0, 0.0))
    assert _close(up, (0.0, 1.0, 0.0))


def test_phone_flat_on_the_table_looks_down() -> None:
    forward = panorama.mat_apply(panorama.device_view((0.0, 0.0, 0.0, 1.0), 0), FORWARD)
    assert panorama.latitude_of(forward) == pytest.approx(-90.0)


def test_turning_right_turns_the_view_right() -> None:
    # Upright, then 30° clockwise seen from above (about the world's up axis)
    upright = panorama.quaternion_matrix(*_upright_facing_north())
    turn = panorama.rot_z(-30.0)  # Android world: z up, so clockwise is negative
    m = panorama.mat_mul(panorama.ANDROID_TO_SPHERE, panorama.mat_mul(turn, upright))
    assert panorama.longitude_of(panorama.mat_apply(m, FORWARD)) == pytest.approx(30.0)


def test_sideways_screen_keeps_the_picture_upright() -> None:
    # Held upright with the left edge up: the screen's up is the device's -x
    s = math.sin(math.radians(45))
    upright = panorama.quaternion_matrix(s, 0.0, 0.0, s)
    roll = panorama.quaternion_matrix(0.0, 0.0, math.sin(math.radians(-45)), math.cos(math.radians(-45)))
    m = panorama.mat_mul(upright, roll)  # device turned 90° clockwise about its z axis
    q = _quaternion_from(m)
    assert panorama.screen_angle_for(q, landscape=True, current=0) == 90
    up = panorama.mat_apply(panorama.device_view(q, 90), (0.0, 1.0, 0.0))
    assert _close(up, (0.0, 1.0, 0.0), 1e-6)


def test_a_portrait_view_is_the_upright_device_whatever_gravity_says() -> None:
    """The picture is not turned while the sphere follows the phone: held
    upside down, the sphere turns, not the screen's axes."""
    upside_down = _quaternion_from(panorama.mat_mul(
        panorama.quaternion_matrix(*_upright_facing_north()), panorama.rot_z(180.0 - 1e-3)))
    assert panorama.screen_angle_for(upside_down, landscape=False, current=0) == 0


def test_flat_phone_keeps_the_screen_angle() -> None:
    assert panorama.screen_angle_for((0.0, 0.0, 0.0, 1.0), landscape=True, current=270) == 270


def test_quaternion_round_trip() -> None:
    q = (0.1, -0.3, 0.2, 0.9)
    n = math.sqrt(sum(v * v for v in q))
    q = tuple(v / n for v in q)
    back = _quaternion_from(panorama.quaternion_matrix(*q))
    assert _close(back, q, 1e-9) or _close(back, tuple(-v for v in q), 1e-9)


def test_orthonormal_repairs_a_blend() -> None:
    a, b = panorama.view_matrix(0, 0), panorama.view_matrix(90, 0)
    blend = tuple(tuple((a[i][j] + b[i][j]) / 2 for j in range(3)) for i in range(3))
    m = panorama._orthonormal(blend)
    mt = tuple(zip(*m))
    assert _close([v for row in panorama.mat_mul(m, mt) for v in row], [1, 0, 0, 0, 1, 0, 0, 0, 1], 1e-9)
    assert panorama.longitude_of(panorama.mat_apply(m, FORWARD)) == pytest.approx(45.0)


def _quaternion_from(m):
    """x, y, z, w of a rotation matrix (trace method; fine away from 180°)."""
    w = math.sqrt(max(0.0, 1 + m[0][0] + m[1][1] + m[2][2])) / 2
    if w < 1e-6:
        # 180° turns are not needed here
        raise AssertionError("degenerate test rotation")
    return ((m[2][1] - m[1][2]) / (4 * w), (m[0][2] - m[2][0]) / (4 * w), (m[1][0] - m[0][1]) / (4 * w), w)


# ---------------------------------------------------------------------------
# As the phone's background
# ---------------------------------------------------------------------------

AMBIENT_SCHEMA_XML = """<schemalist><schema id="org.plinius.ambient" path="/org/plinius/ambient/">
<key name="effect" type="s"><choices><choice value="none"/><choice value="snow"/><choice value="dust"/>{extra}</choices>
<default>'none'</default></key>
<key name="panorama" type="s"><default>''</default></key>
{enabled}
</schema></schemalist>"""


def _ambient_source(tmp_path, monkeypatch, knows_panorama=True):
    import shutil
    import subprocess

    from gi.repository import Gio

    if shutil.which("glib-compile-schemas") is None:
        pytest.skip("glib-compile-schemas missing")
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    # Up to plinius-ambient 0.3 the photo was an effect, without the switch
    enabled = '<key name="panorama-enabled" type="b"><default>false</default></key>' if knows_panorama else ""
    (schemas / "org.plinius.ambient.gschema.xml").write_text(AMBIENT_SCHEMA_XML.format(extra="", enabled=enabled))
    subprocess.run(["glib-compile-schemas", str(schemas)], check=True)
    source = Gio.SettingsSchemaSource.new_from_directory(str(schemas), None, False)
    monkeypatch.setattr(Gio.SettingsSchemaSource, "get_default", staticmethod(lambda: source))
    return Gio


def test_background_needs_an_ambient_that_shows_panoramas(tmp_path, monkeypatch) -> None:
    Gio = _ambient_source(tmp_path, monkeypatch, knows_panorama=False)
    assert panorama.ambient_settings(Gio.memory_settings_backend_new()) is None


def test_set_as_background_copies_and_keeps_the_effect(tmp_path, monkeypatch) -> None:
    """The photo is the background; the snow or rain goes on over it."""
    from gi.repository import GLib

    Gio = _ambient_source(tmp_path, monkeypatch)
    data = tmp_path / "data"
    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(data))
    settings = panorama.ambient_settings(Gio.memory_settings_backend_new())
    assert settings is not None
    settings.set_string("effect", "snow")
    photo = tmp_path / "trip.jpg"
    photo.write_bytes(b"first")
    target = panorama.set_as_background(str(photo), settings)
    assert target == str(data / "backgrounds" / "360" / "trip.jpg")
    assert settings.get_boolean("panorama-enabled")
    assert settings.get_string("panorama") == target
    assert settings.get_string("effect") == "snow", "the effect over the photo was switched off"
    # A second photo joins the first (GNOME Settings lists them all)
    other = tmp_path / "other.jpg"
    other.write_bytes(b"second")
    panorama.set_as_background(str(other), settings)
    assert sorted(p.name for p in (data / "backgrounds" / "360").iterdir()) == ["other.jpg", "trip.jpg"]
    assert photo.exists()


# ---------------------------------------------------------------------------
# The shared claim on the sensor
# ---------------------------------------------------------------------------

def _claims(bus) -> list[str]:
    return [c.args[3] for c in bus.call.call_args_list]


def test_two_views_share_one_claim(monkeypatch) -> None:
    """The bridge counts claims per connection: the second view's release
    must not switch the sensor off under the first."""
    from unittest.mock import MagicMock

    from gi.repository import Gio

    monkeypatch.setattr(Gio, "bus_watch_name_on_connection", MagicMock(return_value=7))
    monkeypatch.setattr(Gio, "bus_unwatch_name", MagicMock())
    shared = panorama._SharedClaim()
    bus = MagicMock()
    first, second = MagicMock(), MagicMock()
    shared.add(bus, first)
    shared.add(bus, second)
    assert _claims(bus) == ["ClaimOrientation"]
    shared.remove(first)
    assert _claims(bus) == ["ClaimOrientation"], "released while a view still listens"
    shared._on_signal(None, None, None, None, None, panorama.GLib.Variant("(dddd)", (0.0, 0.0, 0.0, 1.0)))
    second.assert_called_once_with((0.0, 0.0, 0.0, 1.0))
    first.assert_not_called()
    shared.remove(second)
    assert _claims(bus) == ["ClaimOrientation", "ReleaseOrientation"]


def test_a_restarted_bridge_is_claimed_again(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from gi.repository import Gio

    watch = MagicMock(return_value=7)
    monkeypatch.setattr(Gio, "bus_watch_name_on_connection", watch)
    shared = panorama._SharedClaim()
    bus = MagicMock()
    shared.add(bus, MagicMock())
    appeared = watch.call_args.args[3]
    appeared(bus, "net.hadess.SensorProxy", ":1.99")
    assert _claims(bus) == ["ClaimOrientation", "ClaimOrientation"]
