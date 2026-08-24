"""Torch / video-light control for Halium camera devices."""
from __future__ import annotations

import logging


LOGGER = logging.getLogger(__name__)

# Kernel LED sysfs nodes that expose the rear-camera torch (continuous
# light) on Halium / Hybris phones. Copied from FuriLabs' camera
# flashlightcontroller.cpp pattern: write every node that exists so the
# app does not need per-device detection.
TORCH_SYSFS_PATHS: tuple[str, ...] = (
    "/sys/class/leds/torch-light/brightness",
    "/sys/class/leds/led:flash_torch/brightness",
    "/sys/class/leds/flashlight/brightness",
    "/sys/class/leds/torch-light0/brightness",
    "/sys/class/leds/torch-light1/brightness",
    "/sys/class/leds/led:torch_0/brightness",
    "/sys/class/leds/led:torch_1/brightness",
    "/sys/devices/platform/soc/soc:i2c@1/i2c-23/23-0059/s2mpb02-led/leds/torch-sec1/brightness",
    "/sys/class/leds/led:switch/brightness",
    "/sys/class/leds/led:switch_0/brightness",
    "/sys/devices/virtual/camera/flash/rear_flash",
)


def set_torch_sysfs(on: bool) -> bool:
    """Toggle the rear-camera torch via known kernel LED nodes.

    Returns True if at least one node was written. Missing paths and
    permission failures are normal on desktops and unsupported devices,
    so callers decide whether to surface the failure.
    """
    value = "1" if on else "0"
    wrote = False
    for path in TORCH_SYSFS_PATHS:
        try:
            with open(path, "w") as fh:
                fh.write(value)
            wrote = True
        except OSError:
            continue
    return wrote

