"""Test data recorded from real hardware.

Everything in here was read off a FuriPhone running FuriOS 14.0 (Halium,
aarch64, kernel 4.19) on 2026-08-21 — the class of device Yaga's camera code
is written for, and the one where the desktop cannot stand in: droidcamsrc
instead of v4l2, sensorfwd instead of iio-sensor-proxy.

The point is not to run tests against that phone. Tests that need it would be
skipped everywhere else, including CI. The point is that the *shapes* here are
observed rather than assumed, so the tests that use them check the real
protocol instead of a guess about it — the accelerometer packet layout below
is exactly the kind of thing that looks plausible either way in source.
"""
