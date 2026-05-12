"""V4L2 control probing and application via the v4l2-ctl CLI.

Kept separate from `camera.py` because:
  * the parser deals with multiple kernel naming conventions and needs its
    own unit-testable surface;
  * `v4l2-ctl` is a runtime dependency that may be missing — callers should
    feature-detect via `controls_supported()` and degrade gracefully.

The CLI is preferred over direct `ioctl(VIDIOC_S_CTRL)` because it works
unchanged while a pipeline holds the device open (UVC drivers permit
concurrent control updates) and because subprocess output is trivial to
sanity-check.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

LOGGER = logging.getLogger(__name__)


# Logical name -> list of v4l2 names to probe, newest kernel naming first.
# References: linux/uapi/linux/v4l2-controls.h, v4l2-ctl(1).
CONTROL_ALIASES: dict[str, tuple[str, ...]] = {
    "auto_exposure":       ("auto_exposure", "exposure_auto"),
    "exposure_absolute":   ("exposure_time_absolute", "exposure_absolute", "exposure"),
    "auto_white_balance":  ("white_balance_automatic", "white_balance_temperature_auto", "auto_white_balance"),
    "white_balance_temp":  ("white_balance_temperature",),
    "auto_focus":          ("focus_automatic_continuous", "focus_auto"),
    "focus_absolute":      ("focus_absolute",),
    "auto_focus_start":    ("auto_focus_start",),
    "auto_focus_stop":     ("auto_focus_stop",),
    "gain":                ("gain", "analogue_gain"),
    "brightness":          ("brightness",),
    "contrast":            ("contrast",),
    "saturation":          ("saturation",),
    "sharpness":           ("sharpness",),
    "backlight":           ("backlight_compensation",),
}


# v4l2-ctl --list-ctrls-menus output sample:
#                 brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128 flags=has-min-max
#              auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3 (Aperture Priority Mode)
#                             1: Manual Mode
#                             3: Aperture Priority Mode
_CTRL_RE = re.compile(
    r"^\s*(?P<name>[a-z0-9_]+)\s+0x[0-9a-f]+\s+\((?P<type>[a-z0-9]+)\)\s*:\s*(?P<rest>.*?)\s*$"
)
_MENU_RE = re.compile(r"^\s+(?P<value>-?\d+):\s+(?P<label>.+?)\s*$")
# flags can be comma-space separated and is always last on the line;
# capture greedily to end-of-string and split afterwards.
_FLAGS_RE = re.compile(r"\bflags=([\w\-,\s]+)$")
_KV_RE = re.compile(r"(?<!\w)([a-z_]+)=(-?\d+|0x[0-9a-f]+)")


@dataclass
class V4l2Control:
    name: str
    type: str
    min: Optional[int] = None
    max: Optional[int] = None
    step: int = 1
    default: Optional[int] = None
    value: Optional[int] = None
    flags: list[str] = field(default_factory=list)
    menu: dict[int, str] = field(default_factory=dict)

    @property
    def inactive(self) -> bool:
        return "inactive" in self.flags

    @property
    def readonly(self) -> bool:
        return "read-only" in self.flags


def controls_supported() -> bool:
    return shutil.which("v4l2-ctl") is not None


def probe_controls(device_path: str) -> dict[str, V4l2Control]:
    """Probe a /dev/video* node and return the controls it exposes, keyed
    by the raw v4l2 control name. Returns an empty dict when v4l2-ctl is
    unavailable or the call fails."""
    if not device_path or not controls_supported():
        return {}
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", device_path, "--list-ctrls-menus"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        LOGGER.debug("v4l2-ctl probe failed for %s: %s", device_path, exc)
        return {}

    controls: dict[str, V4l2Control] = {}
    current: V4l2Control | None = None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        m = _CTRL_RE.match(line)
        if m:
            name = m.group("name")
            ctype = m.group("type")
            ctrl = V4l2Control(name=name, type=ctype)
            rest = m.group("rest")
            for k, v in _KV_RE.findall(rest):
                if k in ("min", "max", "step", "default", "value"):
                    try:
                        setattr(ctrl, k, int(v, 0))
                    except ValueError:
                        pass
            flag_m = _FLAGS_RE.search(rest)
            if flag_m:
                ctrl.flags = [s.strip() for s in flag_m.group(1).split(",") if s.strip()]
            controls[name] = ctrl
            current = ctrl
            continue
        if current is not None and current.type == "menu":
            mm = _MENU_RE.match(line)
            if mm:
                try:
                    current.menu[int(mm.group("value"))] = mm.group("label").strip()
                except ValueError:
                    pass
    return controls


def resolve(controls: dict[str, V4l2Control], logical: str) -> V4l2Control | None:
    """Return the control matching a logical name, picking the first alias
    the kernel actually exposes."""
    for raw in CONTROL_ALIASES.get(logical, ()):
        ctrl = controls.get(raw)
        if ctrl is not None:
            return ctrl
    return None


def set_control(device_path: str, name: str, value: int | str) -> bool:
    """Apply a single control. Returns True on success."""
    if not device_path or not controls_supported():
        return False
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", device_path, "-c", f"{name}={value}"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        LOGGER.debug("v4l2-ctl set failed (%s=%s): %s", name, value, exc)
        return False
    if result.returncode != 0:
        LOGGER.debug(
            "v4l2-ctl set %s=%s returned %d: %s",
            name, value, result.returncode, result.stderr.strip(),
        )
        return False
    return True
