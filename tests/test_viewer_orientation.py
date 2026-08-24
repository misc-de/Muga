"""Guards for the sensor-driven photo rotation in the viewer.

The image viewer rotates the displayed photo to follow the device's physical
orientation (accelerometer), so a phone held sideways shows the picture upright
even when auto-rotate is off and the window stays portrait. Two things must
hold for that to look right:

  * The viewer's orientation→angle map must match the camera viewfinder's. They
    are defined in two modules; if one is edited and the other isn't, a photo
    would rotate the opposite way from how the camera previewed it. We compare
    them at the source level (AST) so the test needs neither GStreamer nor a
    display to load the camera module.
  * RotatedContainer must only ever rotate by 0/90/180/270° — any other value
    is snapped to 0 rather than skewing the layout.
"""
from __future__ import annotations

import ast
from pathlib import Path


from tests.conftest import requires_display

import muga
from muga.camera_orientation import ALL_ORIENTATIONS
from muga.viewer import _SENSOR_ROTATION_DEG

_PKG_ROOT = Path(muga.__file__).parent


def _extract_name_keyed_dict(module_file: str, var_name: str) -> dict[str, int]:
    """Return {key_name: int_value} for a module-level ``var_name = {...}``
    whose keys are bare names (e.g. ORIENT_NORMAL) and values int literals.
    Parsed from source so we don't import the module (camera.py pulls in the
    GStreamer stack on import)."""
    source = (_PKG_ROOT / module_file).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Name)]
        if not any(t.id == var_name for t in targets):
            continue
        assert isinstance(node.value, ast.Dict), f"{var_name} is not a dict literal"
        out: dict[str, int] = {}
        for key, value in zip(node.value.keys, node.value.values):
            assert isinstance(key, ast.Name), f"{var_name} key is not a bare name"
            assert isinstance(value, ast.Constant) and isinstance(value.value, int)
            out[key.id] = value.value
        return out
    raise AssertionError(f"{var_name} not found in {module_file}")


def test_viewer_covers_every_orientation() -> None:
    """Every orientation the sensor can report has a defined display angle, so
    a real device transition never falls through to the default."""
    assert set(_SENSOR_ROTATION_DEG) == set(ALL_ORIENTATIONS)
    assert all(v in (0, 90, 180, 270) for v in _SENSOR_ROTATION_DEG.values())


def test_viewer_and_camera_rotation_maps_agree() -> None:
    """The viewer's photo rotation must match the camera viewfinder's glyph
    rotation orientation-for-orientation. Both dicts key off the same ORIENT_*
    names, so we compare them by name without importing either module's heavy
    runtime deps."""
    # The table moved next to the rest of the orientation logic.
    camera_map = _extract_name_keyed_dict("camera_orientation.py", "_ICON_ROTATION_DEG")
    viewer_map = _extract_name_keyed_dict("viewer.py", "_SENSOR_ROTATION_DEG")
    assert viewer_map == camera_map


@requires_display
def test_rotated_container_snaps_to_quarter_turns() -> None:
    """RotatedContainer normalises any angle to {0,90,180,270}; off-axis values
    snap to 0 so the layout never skews.

    Marked rather than guarded: constructing a GTK widget without a display
    aborts the process, and the try/except this replaces could never catch
    that — running the suite headless took the whole run down here.
    """
    from muga.rotated_container import RotatedContainer

    rc = RotatedContainer()
    assert rc.get_rotation() == 0
    for given, expected in [
        (0, 0), (90, 90), (180, 180), (270, 270),
        (360, 0), (-90, 270), (450, 90), (45, 0), (123, 0),
    ]:
        rc.set_rotation(given)
        assert rc.get_rotation() == expected, f"{given} -> {rc.get_rotation()}"
