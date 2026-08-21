"""Tests for the remaining support modules: v4l2 control parsing, the trace
watchdog, the rotated container and the text-sticker renderer.

The v4l2 parser is the one with real surface area — it reads free-form
``v4l2-ctl`` output from whatever kernel driver is on the machine, and a
misparse silently disables a camera control rather than failing loudly.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import requires_display

controls = pytest.importorskip("yaga.camera_controls")
tracer = pytest.importorskip("yaga.tracer")


# ---------------------------------------------------------------------------
# v4l2-ctl output parsing
# ---------------------------------------------------------------------------

V4L2_OUTPUT = """
User Controls

                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128
                       contrast 0x00980901 (int)    : min=0 max=255 step=1 default=32 value=32
          white_balance_automatic 0x0098090c (bool)   : default=1 value=1
                   power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=1 value=1
                                0: Disabled
                                1: 50 Hz
                                2: 60 Hz

Camera Controls

                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3
                                1: Manual Mode
                                3: Aperture Priority Mode
         exposure_time_absolute 0x009a0902 (int)    : min=1 max=5000 step=1 default=157 value=157 flags=inactive
"""


def _probe(output=V4L2_OUTPUT, returncode=0):
    result = MagicMock(stdout=output, stderr="", returncode=returncode)
    with patch.object(controls.shutil, "which", return_value="/usr/bin/v4l2-ctl"), \
         patch.object(controls.subprocess, "run", return_value=result):
        return controls.probe_controls("/dev/video0")


def test_probe_reads_integer_controls() -> None:
    found = _probe()
    brightness = found["brightness"]
    assert brightness.type == "int"
    assert (brightness.min, brightness.max, brightness.step) == (0, 255, 1)
    assert brightness.default == 128
    assert brightness.value == 128


def test_probe_reads_boolean_controls() -> None:
    assert _probe()["white_balance_automatic"].type == "bool"


def test_probe_reads_menu_entries() -> None:
    """The menu labels are what _auto_manual_values matches "Manual" against."""
    menu = _probe()["auto_exposure"].menu
    assert menu == {1: "Manual Mode", 3: "Aperture Priority Mode"}


def test_probe_reads_flags() -> None:
    """An inactive control must not be offered as if it worked."""
    assert "inactive" in _probe()["exposure_time_absolute"].flags


def test_probe_keeps_menus_apart() -> None:
    """Two menus in a row: entries must not leak from one into the next."""
    found = _probe()
    assert set(found["power_line_frequency"].menu) == {0, 1, 2}
    assert set(found["auto_exposure"].menu) == {1, 3}


def test_probe_without_v4l2_ctl() -> None:
    """Most desktops don't have it installed; the camera still has to open."""
    with patch.object(controls.shutil, "which", return_value=None):
        assert controls.probe_controls("/dev/video0") == {}
        assert controls.controls_supported() is False


def test_probe_without_a_device_path() -> None:
    assert controls.probe_controls("") == {}


def test_probe_survives_a_timeout() -> None:
    with patch.object(controls.shutil, "which", return_value="/usr/bin/v4l2-ctl"), \
         patch.object(controls.subprocess, "run",
                      side_effect=controls.subprocess.TimeoutExpired("v4l2-ctl", 2)):
        assert controls.probe_controls("/dev/video0") == {}


def test_probe_survives_garbage_output() -> None:
    assert _probe(output="not v4l2 output at all\n\n???") == {}


def test_probe_ignores_a_non_numeric_value() -> None:
    """A driver printing something unexpected must cost that one field, not
    the whole control."""
    found = _probe(output="   brightness 0x1 (int)    : min=0 max=oops default=1 value=1\n")
    assert "brightness" in found
    assert found["brightness"].min == 0


# ---------------------------------------------------------------------------
# Control aliases
# ---------------------------------------------------------------------------

def test_resolve_finds_the_alias_the_kernel_exposes() -> None:
    """Kernels differ on the spelling; the alias list is why the UI works on
    more than one driver."""
    for logical, aliases in controls.CONTROL_ALIASES.items():
        for alias in aliases:
            ctrl = controls.V4l2Control(name=alias, type="int")
            assert controls.resolve({alias: ctrl}, logical) is ctrl, logical


def test_resolve_prefers_the_first_listed_alias() -> None:
    logical, aliases = next(
        (k, v) for k, v in controls.CONTROL_ALIASES.items() if len(v) > 1
    )
    available = {name: controls.V4l2Control(name=name, type="int") for name in aliases}
    assert controls.resolve(available, logical) is available[aliases[0]]


def test_resolve_returns_none_for_an_absent_control() -> None:
    assert controls.resolve({}, "brightness") is None


def test_resolve_of_an_unknown_logical_name() -> None:
    assert controls.resolve({"brightness": MagicMock()}, "not_a_control") is None


# ---------------------------------------------------------------------------
# Setting a control
# ---------------------------------------------------------------------------

def test_set_control_invokes_v4l2_ctl() -> None:
    result = MagicMock(returncode=0, stderr="")
    with patch.object(controls.shutil, "which", return_value="/usr/bin/v4l2-ctl"), \
         patch.object(controls.subprocess, "run", return_value=result) as run:
        assert controls.set_control("/dev/video0", "brightness", 200) is True
    argv = run.call_args[0][0]
    assert argv[:3] == ["v4l2-ctl", "-d", "/dev/video0"]
    assert "brightness=200" in argv


def test_set_control_reports_a_rejecting_driver() -> None:
    result = MagicMock(returncode=1, stderr="unknown control")
    with patch.object(controls.shutil, "which", return_value="/usr/bin/v4l2-ctl"), \
         patch.object(controls.subprocess, "run", return_value=result):
        assert controls.set_control("/dev/video0", "nonsense", 1) is False


def test_set_control_without_v4l2_ctl() -> None:
    with patch.object(controls.shutil, "which", return_value=None):
        assert controls.set_control("/dev/video0", "brightness", 1) is False


def test_set_control_survives_a_timeout() -> None:
    with patch.object(controls.shutil, "which", return_value="/usr/bin/v4l2-ctl"), \
         patch.object(controls.subprocess, "run",
                      side_effect=controls.subprocess.TimeoutExpired("v4l2-ctl", 2)):
        assert controls.set_control("/dev/video0", "brightness", 1) is False


def test_set_control_never_uses_a_shell() -> None:
    """The control name reaches this from v4l2-ctl's own output; a shell would
    make that an injection point."""
    result = MagicMock(returncode=0, stderr="")
    with patch.object(controls.shutil, "which", return_value="/usr/bin/v4l2-ctl"), \
         patch.object(controls.subprocess, "run", return_value=result) as run:
        controls.set_control("/dev/video0", "brightness; rm -rf /", 1)
    assert run.call_args.kwargs.get("shell") in (None, False)
    assert isinstance(run.call_args[0][0], list)


# ---------------------------------------------------------------------------
# Trace watchdog
# ---------------------------------------------------------------------------

def test_heartbeat_tick_keeps_the_timeout_alive() -> None:
    """Returning False would remove the GLib source and the watchdog would
    then report a permanent stall."""
    assert tracer._heartbeat_tick() is True
    assert tracer._main_heartbeat[0] > 0


def test_thread_name_lookup() -> None:
    current = threading.current_thread()
    assert tracer._thread_name_for(current.ident) == current.name
    assert tracer._thread_name_for(-1) == "?"


def test_frame_filter_only_matches_yaga_code() -> None:
    import inspect

    frame = inspect.currentframe()
    assert tracer._is_yaga_frame(frame) is False   # this file lives in tests/


def test_argument_formatter_truncates_long_values() -> None:
    def sample(short, long_value):
        return tracer._format_args(__import__("inspect").currentframe())

    out = sample("x", "y" * 500)
    assert "short='x'" in out
    assert "..." in out
    assert len(out) < 300, "a huge argument was written to the log in full"


def test_argument_formatter_survives_an_unreprable_value() -> None:
    class Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    def sample(value):
        return tracer._format_args(__import__("inspect").currentframe())

    assert "<unreprable>" in sample(Hostile())


def test_install_writes_a_trace_header(tmp_path: Path) -> None:
    target = tmp_path / "trace.log"
    import sys

    old_profile = sys.getprofile()
    try:
        assert tracer.install(target) == target
    finally:
        sys.setprofile(old_profile)
        threading.setprofile(None)
        if tracer._trace_file is not None:
            tracer._trace_file.close()
            tracer._trace_file = None
    assert "Yaga trace started" in target.read_text()


# ---------------------------------------------------------------------------
# Rotated container
# ---------------------------------------------------------------------------

@requires_display
@pytest.mark.parametrize(
    ("given", "expected"),
    [(0, 0), (90, 90), (180, 180), (270, 270), (360, 0), (-90, 270), (450, 90)],
)
def test_rotated_container_normalises_the_angle(given, expected) -> None:
    from yaga.rotated_container import RotatedContainer

    container = RotatedContainer()
    container.set_rotation(given)
    assert container.get_rotation() == expected


@requires_display
@pytest.mark.parametrize("given", [45, 12, 100, 271])
def test_rotated_container_snaps_off_axis_angles_to_zero(given) -> None:
    """The layout maths only handles quarter turns; anything else would skew
    the child."""
    from yaga.rotated_container import RotatedContainer

    container = RotatedContainer()
    container.set_rotation(given)
    assert container.get_rotation() == 0


@requires_display
def test_rotated_container_holds_one_child() -> None:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from yaga.rotated_container import RotatedContainer

    container = RotatedContainer()
    first, second = Gtk.Label(label="a"), Gtk.Label(label="b")
    container.set_child(first)
    container.set_child(second)
    assert second.get_parent() is container
    assert first.get_parent() is None, "the previous child was not unparented"
    container.set_child(None)


# ---------------------------------------------------------------------------
# Text stickers
# ---------------------------------------------------------------------------

@requires_display
def test_text_sticker_renders_to_rgba() -> None:
    pytest.importorskip("PIL.Image")
    from yaga.editor.text import _make_text_pil

    img = _make_text_pil("Hallo", 32, (255, 0, 0))
    assert img.mode == "RGBA"
    assert img.width > 0 and img.height > 0
    assert img.getextrema()[3][1] > 0, "the sticker is fully transparent"


@requires_display
def test_text_sticker_grows_with_the_font_size() -> None:
    pytest.importorskip("PIL.Image")
    from yaga.editor.text import _make_text_pil

    small = _make_text_pil("Hallo", 16, (255, 255, 255))
    large = _make_text_pil("Hallo", 48, (255, 255, 255))
    assert large.height > small.height


@requires_display
def test_text_sticker_handles_empty_text() -> None:
    pytest.importorskip("PIL.Image")
    from yaga.editor.text import _make_text_pil

    img = _make_text_pil("", 32, (255, 255, 255))
    assert img.width >= 1 and img.height >= 1
