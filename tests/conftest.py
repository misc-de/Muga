"""Shared pytest configuration.

Most of the suite runs headless by calling methods unbound with a
SimpleNamespace ``self``. A few code paths genuinely build GTK widgets and
cannot be tested that way — GTK segfaults (not raises) when it is used without
a display, so those tests are skipped rather than allowed to take the whole
process down.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest

# --- test isolation ---------------------------------------------------------
# Point the XDG roots at a throwaway directory before anything imports
# muga.config, which derives CONFIG_DIR / CACHE_DIR / DATA_DIR (and DB_PATH,
# THUMB_DIR, the log paths) from them at import time. Setting the environment
# rather than monkeypatching the constants covers every module that imported
# them by value, and it covers the fixtures that build a real GalleryWindow.
#
# Without this a plain `pytest` run edits the developer's own gallery: the
# update-flow tests call settings.save(), which rewrote settings.json, and the
# scan tests wrote into the real muga.sqlite3 and thumbnail cache.
_XDG_SANDBOX = tempfile.mkdtemp(prefix="muga-tests-")
for _var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    os.environ[_var] = os.path.join(_XDG_SANDBOX, _var[4:-5].lower())
atexit.register(shutil.rmtree, _XDG_SANDBOX, True)


def _has_display() -> bool:
    """True when GTK can safely construct widgets.

    The environment check has to come first and cannot be skipped: with no
    display at all, ``Gtk.init_check()`` and ``Gdk.Display.open()`` both abort
    the process instead of returning a value, which would take the whole test
    run down rather than failing one test. Once a display is advertised,
    ``init_check`` is the honest answer — it reports False for a display that
    is set but unusable.
    """
    import os

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
    except (ImportError, ValueError):
        return False
    try:
        return bool(Gtk.init_check())
    except Exception:
        return False


requires_display = pytest.mark.skipif(
    not _has_display(),
    reason="needs a display: GTK aborts the process rather than raising",
)


def _offscreen_raster_works() -> bool:
    """True when rendering a node offscreen gives back the colour it was given.

    Some environments — a headless CI runner with no GPU, notably — realise a
    GskCairoRenderer happily and then hand back a texture whose pixels are not
    what was painted. The tests that check "the rotation actually moves
    pixels" build their snapshot out of plain GTK primitives and never touch
    Muga code, so where this is broken they say nothing about the app; they
    only report that the environment cannot rasterise. Skipping is the honest
    answer there, and this is the check that decides it: paint one opaque red
    square, read one pixel back.
    """
    if not _has_display():
        return False
    try:
        import io

        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gsk", "4.0")
        gi.require_version("Graphene", "1.0")
        from gi.repository import Gdk, Graphene, Gsk, Gtk
        from PIL import Image

        snapshot = Gtk.Snapshot()
        snapshot.append_color(
            Gdk.RGBA(red=1.0, green=0.0, blue=0.0, alpha=1.0),
            Graphene.Rect().init(0, 0, 4, 4))
        renderer = Gsk.CairoRenderer.new()
        renderer.realize(None)
        try:
            texture = renderer.render_texture(
                snapshot.to_node(), Graphene.Rect().init(0, 0, 4, 4))
            png = texture.save_to_png_bytes().get_data()
        finally:
            renderer.unrealize()
        r, g, b, a = Image.open(io.BytesIO(png)).convert("RGBA").load()[1, 1]
        return a > 200 and r > 200 and g < 60 and b < 60
    except Exception:
        return False


requires_offscreen_raster = pytest.mark.skipif(
    not _offscreen_raster_works(),
    reason="offscreen rendering does not reproduce colours in this environment",
)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "requires_display: test constructs real GTK widgets",
    )
    config.addinivalue_line(
        "markers",
        "requires_offscreen_raster: test reads pixels back from a rendered node",
    )


@pytest.fixture(scope="session")
def gtk_app():
    """The one Adw.Application this process may register.

    An application ID can only be exported on the session bus once per
    process, so every test that needs a GalleryWindow parent shares this.
    """
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    app = Adw.Application(application_id="de.cais.MugaTests")
    app.register()
    return app


@pytest.fixture
def gallery_window(gtk_app):
    """A real GalleryWindow with the initial scan suppressed.

    ``refresh`` is patched out because the constructor calls it with
    ``scan=True``, which would walk the user's actual media folders.
    """
    from unittest.mock import patch

    import muga.app as app_mod

    with patch.object(app_mod.GalleryWindow, "refresh"):
        win = app_mod.GalleryWindow(gtk_app)
    yield win
    win._closing = True
    win.destroy()


@pytest.fixture
def pump():
    """Run pending GLib main-loop work to completion.

    Several code paths finish on the main loop — a worker thread hands its
    result back through ``GLib.idle_add``, or a handler defers the expensive
    half with ``GLib.timeout_add``. Without a running loop those callbacks
    never fire, so the test would only ever see the first half of the
    operation. This drains whatever is pending, with a bound so a callback
    that keeps rescheduling itself fails the test instead of hanging it.
    """
    from gi.repository import GLib

    def _pump(max_iterations: int = 200) -> int:
        context = GLib.MainContext.default()
        ran = 0
        while context.pending() and ran < max_iterations:
            context.iteration(False)
            ran += 1
        assert ran < max_iterations, "main loop still busy — a callback keeps rescheduling"
        return ran

    return _pump
