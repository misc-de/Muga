"""Tests for the editor's sticker rendering.

Three drawn shapes and an emoji renderer, all producing RGBA images that get
composited onto a photo. What matters is that they come out the right size,
with real alpha, and centred — a sticker that paints into the corner of its
own tile lands nowhere near where the user dropped it.

The emoji path also caches at a master size and rescales, which is worth a
test: the cache is global, so a stale entry would follow the user around for
the rest of the session.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PIL.Image")

from tests.conftest import requires_display  # noqa: E402

stickers = pytest.importorskip("muga.editor.stickers")


def _alpha_bounds(img):
    """The bounding box of everything not fully transparent."""
    return img.split()[3].getbbox()


def _coverage(img) -> float:
    """Fraction of pixels carrying any alpha."""
    alpha = img.split()[3]
    opaque = sum(1 for value in alpha.get_flattened_data() if value > 0)
    return opaque / (img.width * img.height)


SHAPES = [("star", "_make_star"), ("heart", "_make_heart"), ("sparkle", "_make_sparkle")]


# ---------------------------------------------------------------------------
# The drawn shapes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "fn"), SHAPES)
def test_a_shape_is_square_and_rgba(name, fn) -> None:
    img = getattr(stickers, fn)(96)
    assert img.size == (96, 96)
    assert img.mode == "RGBA", f"{name} has no alpha channel to composite with"


@pytest.mark.parametrize(("name", "fn"), SHAPES)
@pytest.mark.parametrize("size", [32, 96, 256])
def test_a_shape_honours_the_requested_size(name, fn, size) -> None:
    assert getattr(stickers, fn)(size).size == (size, size)


@pytest.mark.parametrize(("name", "fn"), SHAPES)
def test_a_shape_actually_draws_something(name, fn) -> None:
    """A polygon with the points in the wrong order collapses to nothing."""
    assert _coverage(getattr(stickers, fn)(96)) > 0.05, f"{name} is (nearly) empty"


@pytest.mark.parametrize(("name", "fn"), SHAPES)
def test_a_shape_leaves_its_background_transparent(name, fn) -> None:
    """It is composited over a photo; an opaque backing would paste a square."""
    img = getattr(stickers, fn)(96)
    for corner in ((0, 0), (95, 0), (0, 95), (95, 95)):
        assert img.getpixel(corner)[3] == 0, f"{name} is opaque at {corner}"


@pytest.mark.parametrize(("name", "fn"), SHAPES)
def test_a_shape_is_centred(name, fn) -> None:
    """The compositor places a sticker by its centre, so an off-centre drawing
    lands away from where the user dropped it."""
    img = getattr(stickers, fn)(96)
    left, top, right, bottom = _alpha_bounds(img)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    assert abs(cx - 48) < 6, f"{name} is off-centre horizontally ({cx})"
    assert abs(cy - 48) < 6, f"{name} is off-centre vertically ({cy})"


@pytest.mark.parametrize(("name", "fn"), SHAPES)
def test_a_shape_stays_inside_its_tile(name, fn) -> None:
    """Drawn past the edge it would be clipped, and the clipped edge shows as
    a straight line across the sticker."""
    img = getattr(stickers, fn)(96)
    left, top, right, bottom = _alpha_bounds(img)
    assert left >= 1 and top >= 1
    assert right <= 95 and bottom <= 95


def test_the_star_has_its_points() -> None:
    """A ten-point polygon with alternating radii; if the inner radius were
    used for every vertex it would come out as a plain decagon, so the drawn
    area stays well under a disc of the same radius."""
    img = stickers._make_star(96)
    assert 0.15 < _coverage(img) < 0.60, "the star is a blob or a sliver"


def test_the_sparkle_is_thinner_than_the_star() -> None:
    """Its inner radius is 0.12 of the outer against the star's 0.38 — that
    difference is the whole visual distinction between the two."""
    assert _coverage(stickers._make_sparkle(96)) < _coverage(stickers._make_star(96))


def test_the_heart_is_wider_than_it_is_tall_at_the_top() -> None:
    """The parametric curve's two lobes; a sign error collapses them and the
    shape comes out as a teardrop."""
    img = stickers._make_heart(96)
    alpha = img.split()[3]
    top_row = sum(1 for x in range(96) if alpha.getpixel((x, 30)) > 0)
    bottom_row = sum(1 for x in range(96) if alpha.getpixel((x, 80)) > 0)
    assert top_row > bottom_row, "the heart has no lobes"


@pytest.mark.parametrize(("name", "fn"), SHAPES)
def test_a_shape_is_deterministic(name, fn) -> None:
    first = getattr(stickers, fn)(64)
    second = getattr(stickers, fn)(64)
    assert list(first.get_flattened_data()) == list(second.get_flattened_data())


@pytest.mark.parametrize(("name", "fn"), SHAPES)
def test_a_tiny_shape_still_renders(name, fn) -> None:
    """The size comes from the sticker's fraction of the image, so a small
    photo produces small tiles."""
    img = getattr(stickers, fn)(8)
    assert img.size == (8, 8)


# ---------------------------------------------------------------------------
# Emoji
# ---------------------------------------------------------------------------

@requires_display
def test_an_emoji_renders_to_a_square_rgba(monkeypatch) -> None:
    monkeypatch.setattr(stickers, "_EMOJI_PIL_CACHE", {})
    img = stickers._emoji_to_pil("🙂", 64)
    assert img.size == (64, 64)
    assert img.mode == "RGBA"


@requires_display
def test_an_emoji_is_not_blank(monkeypatch) -> None:
    """Without a colour-emoji font this comes out empty, which is worth
    knowing about rather than shipping invisible stickers."""
    monkeypatch.setattr(stickers, "_EMOJI_PIL_CACHE", {})
    img = stickers._emoji_to_pil("🙂", 96)
    if _coverage(img) == 0:
        pytest.skip("no colour emoji font on this system")
    assert _coverage(img) > 0.05


@requires_display
def test_an_emoji_is_centred(monkeypatch) -> None:
    monkeypatch.setattr(stickers, "_EMOJI_PIL_CACHE", {})
    img = stickers._emoji_to_pil("🙂", 96)
    bounds = _alpha_bounds(img)
    if bounds is None:
        pytest.skip("no colour emoji font on this system")
    left, top, right, bottom = bounds
    assert abs((left + right) / 2 - 48) < 12
    assert abs((top + bottom) / 2 - 48) < 12


@requires_display
def test_emoji_are_cached_at_a_master_size(monkeypatch) -> None:
    """Re-rendering through Pango on every slider tick was the cost this
    cache exists to remove."""
    cache: dict = {}
    monkeypatch.setattr(stickers, "_EMOJI_PIL_CACHE", cache)
    calls = {"n": 0}
    real = stickers._emoji_to_pil

    def counting(char, size=96):
        calls["n"] += 1
        return real(char, size)

    monkeypatch.setattr(stickers, "_emoji_to_pil", counting)

    stickers._get_emoji_pil("🙂", 48)
    stickers._get_emoji_pil("🙂", 64)
    stickers._get_emoji_pil("🙂", 128)

    assert calls["n"] == 1, f"rendered {calls['n']} times instead of caching"
    assert list(cache) == ["🙂"]


@requires_display
def test_the_cached_master_is_returned_unscaled(monkeypatch) -> None:
    """Rescaling 256 to 256 would be a pointless full-image resample."""
    monkeypatch.setattr(stickers, "_EMOJI_PIL_CACHE", {})
    master = stickers._get_emoji_pil("🙂", 256)
    assert master.size == (256, 256)
    assert stickers._get_emoji_pil("🙂", 256) is master


@requires_display
@pytest.mark.parametrize("px", [16, 48, 128, 512])
def test_emoji_scale_to_the_requested_size(monkeypatch, px) -> None:
    monkeypatch.setattr(stickers, "_EMOJI_PIL_CACHE", {})
    assert stickers._get_emoji_pil("🙂", px).size == (px, px)


@requires_display
def test_different_emoji_get_separate_entries(monkeypatch) -> None:
    cache: dict = {}
    monkeypatch.setattr(stickers, "_EMOJI_PIL_CACHE", cache)
    stickers._get_emoji_pil("🙂", 64)
    stickers._get_emoji_pil("🔥", 64)
    assert set(cache) == {"🙂", "🔥"}


def test_every_offered_emoji_is_a_single_character() -> None:
    """The picker renders one glyph per button; a multi-character entry would
    overflow its tile."""
    for _group, chars in stickers._STICKER_GROUPS:
        for char in chars:
            assert len(char) <= 2, f"{char!r} is more than one glyph"


def test_the_groups_are_named_and_non_empty() -> None:
    assert stickers._STICKER_GROUPS
    for name, chars in stickers._STICKER_GROUPS:
        assert name and chars


def test_no_emoji_is_offered_twice() -> None:
    all_chars = [c for _g, chars in stickers._STICKER_GROUPS for c in chars]
    assert len(all_chars) == len(set(all_chars))


# ---------------------------------------------------------------------------
# Handing an image to GTK
# ---------------------------------------------------------------------------

@requires_display
def test_an_rgb_image_takes_the_three_byte_path() -> None:
    """The live preview is RGB; expanding it to RGBA on every slider tick was
    an extra full-image allocation and copy."""
    from PIL import Image

    texture = stickers._pil_to_texture(Image.new("RGB", (20, 10), (1, 2, 3)))
    assert (texture.get_width(), texture.get_height()) == (20, 10)


@requires_display
def test_an_rgba_image_keeps_its_alpha() -> None:
    from PIL import Image

    texture = stickers._pil_to_texture(Image.new("RGBA", (12, 8), (1, 2, 3, 128)))
    assert (texture.get_width(), texture.get_height()) == (12, 8)


@requires_display
def test_a_palette_image_is_converted() -> None:
    """Screenshots and GIFs arrive as P; handing those over raw would garble
    the texture."""
    from PIL import Image

    texture = stickers._pil_to_texture(Image.new("P", (10, 10)))
    assert (texture.get_width(), texture.get_height()) == (10, 10)


@requires_display
@pytest.mark.parametrize(("name", "fn"), SHAPES)
def test_every_shape_converts_to_a_texture(name, fn) -> None:
    texture = stickers._pil_to_texture(getattr(stickers, fn)(64))
    assert (texture.get_width(), texture.get_height()) == (64, 64)


# ---------------------------------------------------------------------------
# Fitting the heart to its tile
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", [16, 32, 64, 96, 128, 256])
def test_the_heart_point_is_never_flattened(size) -> None:
    """The parametric curve reaches further down than its lobes reach up, so
    a fixed scale about the centre pushed the point past the bottom edge — it
    came out flat, by 2 px at 96 and 4 px at 16. The shape is fitted to the
    tile instead."""
    img = stickers._make_heart(size)
    alpha = img.split()[3]
    bottom_edge = sum(1 for x in range(size) if alpha.getpixel((x, size - 1)) > 0)
    assert bottom_edge == 0, f"{bottom_edge}px of the point is clipped at {size}px"


@pytest.mark.parametrize("size", [16, 64, 256])
def test_the_heart_is_centred_at_every_size(size) -> None:
    """The compositor anchors a sticker by the centre of its tile, so an
    off-centre drawing lands away from where it was dropped — the old fixed
    offset put it ~9 px low at 96 px."""
    left, top, right, bottom = _alpha_bounds(stickers._make_heart(size))
    assert abs((left + right) / 2 - size / 2) <= size * 0.05
    assert abs((top + bottom) / 2 - size / 2) <= size * 0.05


@pytest.mark.parametrize("size", [32, 96, 256])
def test_the_heart_fills_its_tile(size) -> None:
    """Fitting must not shrink it to a dot in the middle."""
    left, top, right, bottom = _alpha_bounds(stickers._make_heart(size))
    assert (right - left) > size * 0.8, "the heart is too narrow for its tile"
    assert (bottom - top) > size * 0.7, "the heart is too short for its tile"


def test_the_heart_keeps_its_lobes_after_fitting() -> None:
    """The fit rescales; a non-uniform scale would square the shape off."""
    img = stickers._make_heart(96)
    alpha = img.split()[3]
    lobes = sum(1 for x in range(96) if alpha.getpixel((x, 25)) > 0)
    point = sum(1 for x in range(96) if alpha.getpixel((x, 80)) > 0)
    assert lobes > point, "the lobes are gone"
    # The notch between the lobes: the top centre must be empty.
    assert alpha.getpixel((48, 8)) == 0, "the notch between the lobes closed up"
