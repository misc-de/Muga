"""Small GTK/GLib helpers shared across the UI modules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk


def idle_once(fn: Callable[..., Any], *args: Any) -> int:
    """Run *fn* on the next main-loop idle turn, exactly once.

    ``GLib.idle_add`` re-arms the source whenever the callback returns
    anything truthy, so a plain ``lambda: self.do_thing()`` silently becomes a
    busy loop the moment ``do_thing`` starts returning a value. Call sites used
    to guard against that with ``lambda: (self.do_thing(), GLib.SOURCE_REMOVE)[1]``,
    which reads as a puzzle and hides the actual call. This wrapper discards
    the return value instead, so the one-shot contract is in the name.

    Returns the GLib source id, so callers can still cancel a pending run.
    """

    def _run() -> bool:
        fn(*args)
        return GLib.SOURCE_REMOVE  # type: ignore[return-value]

    return GLib.idle_add(_run)


# Same threshold as the gallery's mobile breakpoint (see
# GalleryWindow._is_mobile_width): below it a window is phone-shaped.
_DESKTOP_MIN_EDGE = 600


def display_is_desktop() -> bool:
    """Is Muga running on a desktop-sized screen rather than a phone?

    Measured off the monitor, not the window: this is asked while the first
    window is still being built, when it has no size yet. The short edge is
    what decides, so a phone held in landscape doesn't briefly count as a
    desktop. Falls back to False — the phone-shaped answer, which keeps the
    legacy top bar — when there is no display to ask (headless runs, tests).
    """
    display = Gdk.Display.get_default()
    if display is None:
        return False
    try:
        monitors = display.get_monitors()
        monitor = monitors.get_item(0) if monitors is not None else None
        if monitor is None:
            return False
        geometry = monitor.get_geometry()
    except Exception:
        return False
    return min(geometry.width, geometry.height) >= _DESKTOP_MIN_EDGE


def load_css(provider: "Gtk.CssProvider", css: str) -> None:
    """Feed *css* into *provider*, on new and old GTK alike.

    ``load_from_string`` arrived in GTK 4.12 and deprecated ``load_from_data``.
    Muga also targets Phosh phones on older stacks (Droidian and FuriOS ship
    GTK 4.8), so the old call has to stay reachable rather than be replaced
    outright.
    """
    if hasattr(provider, "load_from_string"):
        provider.load_from_string(css)
    else:  # GTK < 4.12
        provider.load_from_data(css.encode())


def texture_from_pixbuf(pixbuf: "GdkPixbuf.Pixbuf") -> "Gdk.Texture":
    """Wrap a decoded pixbuf in a texture without copying it twice.

    ``Gdk.Texture.new_for_pixbuf`` is deprecated as of GTK 4.20, and the
    replacement it points at — ``Gdk.MemoryTexture`` — has been available
    since GTK 4.0, so this needs no version guard.

    A memory texture is display-independent, which is why callers can build
    one on a worker thread and only hand the finished object back to the main
    loop.
    """
    fmt = (
        Gdk.MemoryFormat.R8G8B8A8
        if pixbuf.get_has_alpha()
        else Gdk.MemoryFormat.R8G8B8
    )
    return Gdk.MemoryTexture.new(
        pixbuf.get_width(),
        pixbuf.get_height(),
        fmt,
        GLib.Bytes.new(pixbuf.get_pixels()),
        pixbuf.get_rowstride(),
    )
