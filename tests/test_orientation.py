"""Tests for the device-orientation classifier.

This decides which way up every captured photo is saved and where the shutter
sits, from a noisy accelerometer feed. Three things make it more than a lookup
table, and all three have bitten before:

  * hysteresis, so a phone held near 45° does not flap between layouts;
  * an inverted X axis, because the Halium HAL reports the opposite sign from
    the Android convention (the comment in the source says so explicitly, and
    photos came out sideways in landscape when it was taken at face value);
  * a NaN guard, because one bad sample from a flaky HAL used to poison the
    smoothing average and pin the device to bottom-up.
"""

from __future__ import annotations

import math

import pytest

orientation = pytest.importorskip("muga.camera_orientation")

from muga.camera_orientation import (  # noqa: E402
    ORIENT_BOTTOM_UP,
    ORIENT_LEFT_UP,
    ORIENT_NORMAL,
    ORIENT_RIGHT_UP,
    _classify_orientation,
    is_landscape,
)

ALL = (ORIENT_NORMAL, ORIENT_BOTTOM_UP, ORIENT_LEFT_UP, ORIENT_RIGHT_UP)


# ---------------------------------------------------------------------------
# Landscape predicate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [(ORIENT_NORMAL, False), (ORIENT_BOTTOM_UP, False),
     (ORIENT_LEFT_UP, True), (ORIENT_RIGHT_UP, True), ("nonsense", False)],
)
def test_is_landscape(value, expected) -> None:
    assert is_landscape(value) is expected


# ---------------------------------------------------------------------------
# Clear-cut orientations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [
        (0.0, 1.0, ORIENT_NORMAL),        # upright
        (0.0, -1.0, ORIENT_BOTTOM_UP),    # upside down
        (1.0, 0.0, ORIENT_RIGHT_UP),      # inverted X: +X is right-up here
        (-1.0, 0.0, ORIENT_LEFT_UP),
    ],
)
def test_unambiguous_orientations(x, y, expected) -> None:
    assert _classify_orientation(x, y, ORIENT_NORMAL) == expected


def test_x_axis_is_inverted_from_the_android_convention() -> None:
    """The HAL reports +X in right-up where Android specifies left-up. Taking
    the sign at face value saved every landscape photo sideways."""
    assert _classify_orientation(0.9, 0.1, ORIENT_NORMAL) == ORIENT_RIGHT_UP
    assert _classify_orientation(-0.9, 0.1, ORIENT_NORMAL) == ORIENT_LEFT_UP


# ---------------------------------------------------------------------------
# Hysteresis
# ---------------------------------------------------------------------------

def test_a_flat_device_keeps_the_current_orientation() -> None:
    """Face up or face down there is no horizontal component to read, so the
    last known orientation has to stand."""
    for current in ALL:
        assert _classify_orientation(0.01, 0.01, current) == current


def test_entering_landscape_needs_more_than_leaving_it() -> None:
    """Without the gap a phone held near the diagonal flaps between layouts,
    rebuilding both popovers each time.

    At ratio_x = 0.5 the reading is past the exit threshold but short of the
    entry one, so a portrait device stays portrait while a landscape one stays
    landscape. Which *side* is up is always re-read from the sign of X, so the
    negative-x case is the one that stays LEFT_UP.
    """
    assert _classify_orientation(0.5, 0.5, ORIENT_NORMAL) == ORIENT_NORMAL
    assert _classify_orientation(-0.5, 0.5, ORIENT_LEFT_UP) == ORIENT_LEFT_UP
    assert _classify_orientation(0.5, 0.5, ORIENT_RIGHT_UP) == ORIENT_RIGHT_UP


def test_landscape_side_is_re_read_even_inside_the_band() -> None:
    """Turning the phone the other way round while staying landscape has to
    move the shutter across, so hysteresis holds the *mode*, not the side."""
    assert _classify_orientation(0.5, 0.5, ORIENT_LEFT_UP) == ORIENT_RIGHT_UP


def test_a_decisive_turn_still_switches() -> None:
    assert _classify_orientation(0.95, 0.05, ORIENT_NORMAL) == ORIENT_RIGHT_UP
    assert _classify_orientation(0.05, 0.95, ORIENT_RIGHT_UP) == ORIENT_NORMAL


def test_hysteresis_band_is_stable_from_either_side() -> None:
    """Anywhere inside the band, the answer depends only on where you came
    from — that is what makes it stable."""
    x, y = 0.55, 0.45
    from_portrait = _classify_orientation(x, y, ORIENT_NORMAL)
    from_landscape = _classify_orientation(x, y, ORIENT_LEFT_UP)
    assert from_portrait != from_landscape
    assert _classify_orientation(x, y, from_portrait) == from_portrait
    assert _classify_orientation(x, y, from_landscape) == from_landscape


def test_classifier_is_idempotent_everywhere() -> None:
    """Feeding a result back in must never move it — otherwise the layout can
    oscillate on a perfectly steady reading."""
    for i in range(-10, 11):
        for j in range(-10, 11):
            x, y = i / 10, j / 10
            for start in ALL:
                once = _classify_orientation(x, y, start)
                assert _classify_orientation(x, y, once) == once


def test_classifier_always_returns_a_known_state() -> None:
    for i in range(-10, 11):
        for j in range(-10, 11):
            for start in ALL:
                assert _classify_orientation(i / 10, j / 10, start) in ALL


# ---------------------------------------------------------------------------
# Sample processing: smoothing and the NaN guard
# ---------------------------------------------------------------------------

def _backend():
    backend = orientation._SensordBackend()
    backend._on_change = None
    return backend


def test_first_sample_seeds_the_average() -> None:
    """Starting the EWMA at zero would take several samples to catch up, so
    the camera would open in the wrong layout."""
    backend = _backend()
    backend._process_sample(0.0, 1.0, 0.0)
    assert backend._smoothed_y == pytest.approx(1.0)
    assert backend._orientation == ORIENT_NORMAL


def test_later_samples_are_smoothed_not_replaced() -> None:
    backend = _backend()
    backend._process_sample(0.0, 1.0, 0.0)
    backend._process_sample(0.0, -1.0, 0.0)
    assert -1.0 < backend._smoothed_y < 1.0, "one sample overrode the average"


@pytest.mark.parametrize("magnitude", [0.5, 1.0, 1.5])
def test_an_ordinary_jolt_does_not_flip_the_layout(magnitude) -> None:
    """Setting the phone down or a footstep produces one transient sample."""
    backend = _backend()
    for _ in range(10):
        backend._process_sample(0.0, 1.0, 0.0)
    backend._process_sample(magnitude, -magnitude, 0.0)
    assert backend._orientation == ORIENT_NORMAL


def test_an_extreme_jolt_can_still_flip_the_layout() -> None:
    """Documents where the smoothing gives out rather than claiming it never
    does: at alpha 0.25 a single sample carries a quarter of the weight, so a
    2 g spike opposing the current attitude is enough to switch. That is a
    hard shake or a drop — but it is a single sample, and the layout does
    visibly jump. Lowering alpha would buy immunity at the cost of turn
    latency; pinned here so the trade-off is explicit.
    """
    backend = _backend()
    for _ in range(10):
        backend._process_sample(0.0, 1.0, 0.0)
    backend._process_sample(2.0, -2.0, 0.0)
    assert backend._orientation == ORIENT_RIGHT_UP


def test_a_sustained_turn_does_flip_the_layout() -> None:
    backend = _backend()
    for _ in range(10):
        backend._process_sample(0.0, 1.0, 0.0)
    for _ in range(30):
        backend._process_sample(-1.0, 0.0, 0.0)
    assert backend._orientation == ORIENT_LEFT_UP


@pytest.mark.parametrize(
    ("x", "y"),
    [(float("nan"), 1.0), (1.0, float("nan")), (float("inf"), 1.0),
     (1.0, float("-inf")), (float("nan"), float("nan"))],
)
def test_non_finite_samples_are_dropped(x, y) -> None:
    """A NaN poisons the EWMA permanently, and NaN-vs-positive comparisons in
    the classifier then pin the device to bottom-up."""
    backend = _backend()
    backend._process_sample(0.0, 1.0, 0.0)
    good_x, good_y = backend._smoothed_x, backend._smoothed_y

    backend._process_sample(x, y, 0.0)

    assert backend._smoothed_x == good_x
    assert backend._smoothed_y == good_y
    assert math.isfinite(backend._smoothed_x)
    assert backend._orientation == ORIENT_NORMAL


def test_change_callback_fires_only_on_a_real_change() -> None:
    backend = _backend()
    seen = []
    backend._on_change = seen.append

    for _ in range(20):
        backend._process_sample(0.0, 1.0, 0.0)
    assert seen == [ORIENT_NORMAL], "the callback fired on every sample"

    for _ in range(30):
        backend._process_sample(1.0, 0.0, 0.0)
    assert seen == [ORIENT_NORMAL, ORIENT_RIGHT_UP]


def test_process_sample_without_a_callback() -> None:
    backend = _backend()
    backend._process_sample(0.0, 1.0, 0.0)   # must not raise


# ---------------------------------------------------------------------------
# Client facade
# ---------------------------------------------------------------------------

def test_client_starts_stopped() -> None:
    client = orientation.OrientationClient()
    assert client.running is False
    assert client.backend_name in ("", None) or isinstance(client.backend_name, str)


def test_client_stop_is_safe_before_start() -> None:
    orientation.OrientationClient().stop()
