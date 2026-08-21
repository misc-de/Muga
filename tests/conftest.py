"""Shared pytest configuration.

Most of the suite runs headless by calling methods unbound with a
SimpleNamespace ``self``. A few code paths genuinely build GTK widgets and
cannot be tested that way — GTK segfaults (not raises) when it is used without
a display, so those tests are skipped rather than allowed to take the whole
process down.
"""

from __future__ import annotations

import pytest


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


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "requires_display: test constructs real GTK widgets",
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
    app = Adw.Application(application_id="io.github.miscde.YagaTests")
    app.register()
    return app


@pytest.fixture
def gallery_window(gtk_app):
    """A real GalleryWindow with the initial scan suppressed.

    ``refresh`` is patched out because the constructor calls it with
    ``scan=True``, which would walk the user's actual media folders.
    """
    from unittest.mock import patch

    import yaga.app as app_mod

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
