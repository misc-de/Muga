"""Device-enumeration tests using shapes recorded from a real Halium phone.

The phone this data came from exposes cameras twice over — through
droidcamsrc (gst-droid talking to the Android HAL) and through libcamera on
PipeWire — and Muga deliberately takes the first. That choice is only correct
because the /dev/video* nodes on Halium are ISP and encoder helpers rather
than capture devices, which is not something the enumeration can detect for
itself. Worth a test that says so.

See tests/fixtures/hardware.py for what was recorded and how.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.fixtures.hardware import (
    DROIDCAM_EXPECTED_COUNT,
    DROIDCAM_PAD_TEMPLATES,
    DROIDCAM_PSPEC_MAXIMUM,
    DROIDCAM_PSPEC_MINIMUM,
    LIBCAMERA_CAPS_STRING,
    LIBCAMERA_DEVICES,
    V4L2_CAPS_STRING,
)

devices = pytest.importorskip("muga.camera_devices")


def _gst(*, droidcam=True, pspec_max=DROIDCAM_PSPEC_MAXIMUM, monitor_devices=()):
    """A Gst stand-in matching what the recorded phone reports."""
    element = MagicMock()
    pspec = MagicMock()
    pspec.minimum = DROIDCAM_PSPEC_MINIMUM
    pspec.maximum = pspec_max
    element.find_property.return_value = pspec if pspec_max is not None else None
    element.get_pad_template_list.return_value = [
        MagicMock(name_template=name) for name in DROIDCAM_PAD_TEMPLATES
    ]

    gst = MagicMock()
    gst.ElementFactory.find.side_effect = (
        lambda name: object() if (name != "droidcamsrc" or droidcam) else None
    )
    gst.ElementFactory.make.return_value = element if droidcam else None
    gst.DeviceMonitor.new.return_value = _monitor(monitor_devices)
    return gst


def _monitor(entries):
    monitor = MagicMock()
    monitor.get_devices.return_value = [_device(e) for e in entries]
    return monitor


def _caps(caps_string):
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return Gst.Caps.from_string(caps_string)


def _device(entry, caps_string=LIBCAMERA_CAPS_STRING):
    props = MagicMock()
    props.get_string.side_effect = lambda key: entry["props"].get(key)
    dev = MagicMock()
    dev.get_properties.return_value = props
    dev.get_display_name.return_value = entry["display_name"]
    dev.get_device_class.return_value = entry["device_class"]
    # Real caps, ranges and all — see the fixture note.
    dev.get_caps.return_value = _caps(caps_string)
    return dev


# ---------------------------------------------------------------------------
# droidcamsrc camera count
# ---------------------------------------------------------------------------

def test_camera_count_comes_from_the_property_range() -> None:
    """Deriving it from the GParamSpec avoids opening each HAL camera in turn
    — rapid open/close cycles wedge the HAL on some phones."""
    assert devices.droidcam_camera_count(_gst()) == DROIDCAM_EXPECTED_COUNT


def test_camera_count_is_zero_without_droidcamsrc() -> None:
    assert devices.droidcam_camera_count(_gst(droidcam=False)) == 0


def test_camera_count_falls_back_when_the_property_is_missing() -> None:
    assert devices.droidcam_camera_count(_gst(pspec_max=None)) == 2


def test_camera_count_caps_an_int32_max_range() -> None:
    """Some drivers report INT32_MAX instead of the real ceiling; offering two
    billion cameras is worse than guessing four."""
    assert devices.droidcam_camera_count(_gst(pspec_max=2**31 - 1)) == 4


def test_camera_count_is_at_least_one() -> None:
    assert devices.droidcam_camera_count(_gst(pspec_max=0)) == 1


# ---------------------------------------------------------------------------
# droidcamsrc device list
# ---------------------------------------------------------------------------

def test_droidcam_devices_match_the_phones_real_layout() -> None:
    """Camera 0 back, 1 front, 2 an extra back — the naming the code assumes,
    and what libcamera independently reports for the same phone."""
    found = devices.enumerate_droidcam_devices(_gst())
    assert len(found) == DROIDCAM_EXPECTED_COUNT
    assert [d["location"] for d in found] == ["back", "front", "back"]

    from_libcamera = [d["props"]["api.libcamera.location"] for d in LIBCAMERA_DEVICES]
    assert [d["location"] for d in found] == from_libcamera, (
        "the assumed camera order no longer matches what the device reports"
    )


def test_droidcam_devices_carry_their_camera_id() -> None:
    found = devices.enumerate_droidcam_devices(_gst())
    assert [d["droidcam_id"] for d in found] == [0, 1, 2]
    assert all(d["source_factory"] == "droidcamsrc" for d in found)
    assert all(d["path"] == "" for d in found), "droidcamsrc has no /dev node"


def test_droidcam_devices_are_named_for_a_picker() -> None:
    names = [d["name"] for d in devices.enumerate_droidcam_devices(_gst())]
    assert names == ["Back camera", "Front camera", "Back camera 2"]


def test_no_droidcam_devices_without_the_element() -> None:
    assert devices.enumerate_droidcam_devices(_gst(droidcam=False)) == []


# ---------------------------------------------------------------------------
# Which enumeration path wins
# ---------------------------------------------------------------------------

def test_halium_prefers_droidcamsrc_over_libcamera() -> None:
    """This phone offers both. The /dev/video* nodes here are ISP and encoder
    helpers, and v4l2src fails on them with ENOTTY — so the presence of
    droidcamsrc is the signal to ignore the other path entirely."""
    found = devices.enumerate_devices(_gst(monitor_devices=LIBCAMERA_DEVICES))
    assert all(d["source_factory"] == "droidcamsrc" for d in found)
    assert len(found) == DROIDCAM_EXPECTED_COUNT


def test_a_desktop_uses_the_device_monitor() -> None:
    found = devices.enumerate_devices(
        _gst(droidcam=False, monitor_devices=LIBCAMERA_DEVICES))
    assert [d["name"] for d in found] == [e["display_name"] for e in LIBCAMERA_DEVICES]


def test_monitor_devices_are_classified_from_libcamera_properties() -> None:
    """api.libcamera.location is authoritative — the display names here
    ("Back Camera 1") would also parse, but not every driver provides one."""
    found = devices.enumerate_devices(
        _gst(droidcam=False, monitor_devices=LIBCAMERA_DEVICES))
    assert [d["location"] for d in found] == ["back", "front", "back"]


def test_monitor_devices_are_recognised_as_pipewire() -> None:
    """They must be opened through Gst.Device.create_element (pipewiresrc),
    not a raw v4l2src — PipeWire holds the node exclusively."""
    found = devices.enumerate_devices(
        _gst(droidcam=False, monitor_devices=LIBCAMERA_DEVICES))
    assert all(d["pipewire"] is True for d in found)
    assert all(d["gst_device"] is not None for d in found)


def test_libcamera_paths_are_not_treated_as_v4l2_nodes() -> None:
    """object.path is "libcamera:camera0", not "/dev/video0" — building a
    v4l2src for it would fail."""
    found = devices.enumerate_devices(
        _gst(droidcam=False, monitor_devices=LIBCAMERA_DEVICES))
    assert all(d["source_factory"] == "" for d in found)
    assert all(d["path"].startswith("libcamera:") for d in found)


def test_recorded_properties_survive_the_helpers() -> None:
    """The individual accessors have to agree with what the phone sends."""
    for entry in LIBCAMERA_DEVICES:
        props = _device(entry).get_properties()
        assert devices.device_path(props) == entry["props"]["object.path"]
        assert devices.is_pipewire_device(props) is True
        assert devices.classify_location(props, entry["display_name"]) == \
            entry["props"]["api.libcamera.location"]


def test_pad_templates_cover_what_the_pipelines_ask_for() -> None:
    """The preview and record pipelines link vfsrc; still capture links
    imgsrc. Both have to exist on the real element."""
    assert "vfsrc" in DROIDCAM_PAD_TEMPLATES
    assert "imgsrc" in DROIDCAM_PAD_TEMPLATES


# ---------------------------------------------------------------------------
# Range caps — the shape libcamera actually reports
# ---------------------------------------------------------------------------

def test_range_caps_still_identify_the_format() -> None:
    """libcamera advertises width and height as ranges until the device is
    opened. Gst.Structure.get_int answers (False, 0) for a range even though
    the field is there, so a device_kinds built on concrete numbers came back
    empty — and the enumeration then dropped the camera as metadata-only.

    On the recorded phone droidcamsrc takes over and hides that; a desktop on
    the libcamera stack would have found no camera at all.
    """
    assert devices.device_kinds(_caps(LIBCAMERA_CAPS_STRING)) == {"raw"}


def test_range_caps_offer_no_discrete_modes() -> None:
    """The resolution picker needs concrete numbers, so it correctly gets
    none here and hides itself — that half was never the bug."""
    assert devices.modes_from_caps(_caps(LIBCAMERA_CAPS_STRING)) == []


def test_range_caps_survive_enumeration() -> None:
    found = devices.enumerate_devices(
        _gst(droidcam=False, monitor_devices=LIBCAMERA_DEVICES))
    assert len(found) == len(LIBCAMERA_DEVICES), (
        "the libcamera cameras were filtered out again")
    assert all(d["kinds"] == {"raw"} for d in found)


def test_a_range_only_jpeg_device_is_recognised() -> None:
    assert devices.device_kinds(
        _caps("image/jpeg, width=(int)[640, 4096], height=(int)[480, 2160]")) == {"jpeg"}


def test_discrete_caps_are_unaffected() -> None:
    """The v4l2 path has to keep working exactly as before."""
    caps = _caps(V4L2_CAPS_STRING)
    assert devices.device_kinds(caps) == {"raw", "jpeg"}
    assert devices.modes_from_caps(caps) == [(1920, 1080, "jpeg"), (1280, 720, "raw")]


def test_metadata_only_devices_are_still_filtered() -> None:
    """The filter exists for a reason — PipeWire also publishes nodes that
    carry no video at all, and those should not reach the camera picker."""
    assert devices.device_kinds(_caps("meta/x-klv, sparse=(boolean)true")) == set()

    metadata_entry = {
        "display_name": "Metadata node",
        "device_class": "Video/Source",
        "props": {"object.path": "pipewire:meta0", "node.name": "meta"},
    }
    gst = _gst(droidcam=False, monitor_devices=[metadata_entry])
    gst.DeviceMonitor.new.return_value.get_devices.return_value = [
        _device(metadata_entry, caps_string="meta/x-klv, sparse=(boolean)true")
    ]
    found = devices.enumerate_devices(gst)
    assert not any(d["name"] == "Metadata node" for d in found)


def test_no_caps_at_all_is_still_filtered() -> None:
    assert devices.device_kinds(None) == set()


def test_broken_caps_do_not_raise() -> None:
    caps = MagicMock()
    caps.get_size.side_effect = RuntimeError("bad caps")
    assert devices.device_kinds(caps) == set()
