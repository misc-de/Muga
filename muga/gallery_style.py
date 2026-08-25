"""Styling and the touch gestures layered on top of the gallery.

Split out of ``GalleryWindow``. Two things that are easy to confuse with the
gallery's own logic but are really presentation:

* the CSS — a single provider holding the tile rules plus 72 pre-baked
  rotation classes — and the theme/tile-size updates that redraw with it
* the gestures a phone needs: pull-to-refresh on the grid, and drag/swipe
  across the category bar

Gesture handlers deliberately keep their state on the window rather than in
the gesture objects, because GTK recycles those between sequences.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk

from .gtk_util import load_css

if TYPE_CHECKING:
    from .config import Settings
    from .gallery_grid import GalleryGrid

LOGGER = logging.getLogger(__name__)


class GalleryStyleGestureMixin:
    """CSS, theme and the pull/swipe gestures.

    The block below is the contract with the host class: every name is created
    in ``GalleryWindow.__init__`` (or defined on it) and only annotated here.
    """

    settings: Settings
    gallery_grid: GalleryGrid
    category: str
    category_buttons: dict[str, Gtk.ToggleButton]
    current_folder: str | None
    nav_box: Gtk.Box
    refresh_button: Gtk.Button
    _selection_mode: bool
    _tile_css: Gtk.CssProvider
    _grid_width: int

    # Pull-to-refresh, driven from the grid's drag gesture.
    _pull_offset_px: float
    _pull_threshold_px: float
    _pull_started_at_top: bool
    _pull_animation: Adw.TimedAnimation | None

    # Category-bar drag state. Kept on the window because GTK recycles the
    # gesture objects between sequences.
    _nav_drag_start_us: int
    _nav_press_button: Gtk.ToggleButton | None

    # Motion in px below which we treat a press+release as a tap (synthesise
    # the button click) rather than a swipe. Above this on the primary axis
    # the existing _on_nav_swipe velocity logic gets a shot.
    _NAV_SWIPE_TAP_PX = 16

    if TYPE_CHECKING:
        # Provided by GalleryWindow; no runtime definition, so these can never
        # shadow the real methods.
        def refresh(self, scan: bool = False, scope: str | None = None,
                    reset_scroll: bool = False) -> None: ...
        def _rebuild_categories(self) -> None: ...
        def _go_back_folder(self) -> None: ...
        def _cancel_nc_thumb_queue(self) -> None: ...
        def _reset_category_view(self) -> None: ...

    def _on_system_theme_changed(self, _mgr, _param) -> None:
        self._rebuild_categories()

    def _apply_grid_settings(self) -> None:
        columns = min(max(int(self.settings.grid_columns), 2), 10)
        self.gallery_grid.set_columns(columns)

    def _on_folder_swipe(self, _gesture: Gtk.GestureSwipe, velocity_x: float, velocity_y: float) -> None:
        if self._selection_mode or self.current_folder is None:
            return
        if abs(velocity_x) < 350 or abs(velocity_x) <= abs(velocity_y):
            return
        if velocity_x > 0:
            self._go_back_folder()

    def _on_nav_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        # Claim immediately so the ToggleButton's internal Gtk.GestureClick
        # can't lock the press sequence and starve us of motion / release
        # events. With "claim on motion threshold" the click already won by
        # the time motion accumulated, which is why every previous variant
        # failed to swipe over icons. We pay for this by losing the button's
        # press visual; in exchange the gesture is reliable.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._nav_drag_start_us = GLib.get_monotonic_time()
        # Remember which button the press landed on so a non-swipe release
        # can synthesise the click that the denied GestureClick would have.
        self._nav_press_button = self._find_nav_button_at(x, y)

    def _on_nav_drag_update(self, _gesture: Gtk.GestureDrag, _ox: float, _oy: float) -> None:
        # Decisions happen on drag-end; cumulative offset is the truthful signal.
        return

    def _on_nav_drag_end(self, _gesture: Gtk.GestureDrag, ox: float, oy: float) -> None:
        elapsed_us = max(1, GLib.get_monotonic_time() - getattr(self, "_nav_drag_start_us", 0))
        is_vertical = (
            self.nav_box.get_orientation() == Gtk.Orientation.VERTICAL
        )
        primary, secondary = (oy, ox) if is_vertical else (ox, oy)
        press_button = getattr(self, "_nav_press_button", None)
        self._nav_press_button = None

        if abs(primary) >= self._NAV_SWIPE_TAP_PX and abs(primary) > abs(secondary):
            # Real swipe — convert offset/time to px/s and reuse the swipe handler.
            scale = 1_000_000 / elapsed_us
            self._on_nav_swipe(None, ox * scale, oy * scale)
            return
        # Tap. The button never saw the click — drag-begin claimed the
        # sequence out from under it — so every outcome has to be driven
        # from here.
        if press_button is None:
            return
        if press_button.get_active():
            # A tap on the section you are already in. There is nothing for
            # the button to report: it is lit, and set_active(True) on an
            # already-active ToggleButton emits no "toggled" at all. So the
            # branch in _on_category_toggled that handles this never ran on
            # a device, however well it tested — the tap was swallowed whole
            # here, one layer above it. Reported twice as "tapping the tab
            # I'm on does nothing", and both times fixed in the handler.
            self._cancel_nc_thumb_queue()
            self._reset_category_view()
            return
        # Otherwise synthesise the click: set_active(True) emits "toggled" →
        # _on_category_toggled, exactly the path a real click would take.
        try:
            press_button.set_active(True)
        except Exception:
            LOGGER.debug("press_button.set_active failed", exc_info=True)

    def _find_nav_button_at(self, x: float, y: float) -> "Gtk.ToggleButton | None":
        """Walk nav_box children and return the ToggleButton whose
        allocation contains (x, y) in nav_box coords. Used by the swipe
        gesture to know which category a finger-down was aiming at, even
        though we claim the sequence before the button's click sees it."""
        child = self.nav_box.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.ToggleButton):
                ok, bounds = child.compute_bounds(self.nav_box)
                if ok:
                    bx = bounds.get_x()
                    by = bounds.get_y()
                    if (bx <= x <= bx + bounds.get_width()
                        and by <= y <= by + bounds.get_height()):
                        return child
            child = child.get_next_sibling()
        return None

    def _on_nav_swipe(self, _gesture, velocity_x: float, velocity_y: float) -> None:
        """Swipe on the nav bar to step through categories along its main axis.

        Horizontal nav (top/bottom): velocity_x picks the direction, swipe
        right (positive x) jumps to the next category, left to the previous.
        Vertical nav (left/right side rail): velocity_y instead, down = next,
        up = previous. The same 350 px/s threshold the folder-back swipe uses
        keeps stray finger drags from triggering a category jump. No wrap at
        the ends — silent no-op so the user can't accidentally lap past the
        last category back to the first.
        """
        if self._selection_mode:
            return
        is_vertical = (
            self.nav_box.get_orientation() == Gtk.Orientation.VERTICAL
        )
        if is_vertical:
            primary, secondary = velocity_y, velocity_x
        else:
            primary, secondary = velocity_x, velocity_y
        if abs(primary) < 350 or abs(primary) <= abs(secondary):
            return
        cats = [cat for cat, _label, _path in self.settings.categories()]
        if not cats or self.category not in cats:
            return
        idx = cats.index(self.category)
        new_idx = idx + (1 if primary > 0 else -1)
        if not (0 <= new_idx < len(cats)):
            return
        target = self.category_buttons.get(cats[new_idx])
        if target is not None:
            target.set_active(True)  # fires _on_category_toggled

    def _on_grid_tick(self, widget: Gtk.Widget, _clock) -> bool:
        width = widget.get_width()
        if width != self._grid_width:
            self._grid_width = width
            self._update_tile_size(width)
        return GLib.SOURCE_CONTINUE

    def _on_pull_drag_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        # Only arm the pull when the gallery is fully scrolled to the top
        # at the moment the press starts. Anywhere else the gesture must
        # stay a no-op so normal kinetic scrolling and tile clicks keep
        # working.
        adj = self.gallery_grid.get_vadjustment()
        self._pull_started_at_top = adj.get_value() <= adj.get_lower() + 1.0
        self._pull_offset_px = 0.0
        if self._pull_animation is not None:
            self._pull_animation.pause()
            self._pull_animation = None

    def _on_pull_drag_update(self, _gesture: Gtk.GestureDrag,
                             _offset_x: float, offset_y: float) -> None:
        if not self._pull_started_at_top or self._selection_mode:
            return
        if offset_y <= 0:
            # User changed mind and dragged upward — collapse any
            # visual offset and stop tracking until the next touch-down.
            if self._pull_offset_px != 0:
                self._pull_offset_px = 0.0
                self.gallery_grid.grid_view.set_margin_top(0)
                self.gallery_grid.pull_revealer.set_reveal_child(False)
            return
        # 1:1 follow up to threshold, then diminishing returns so the
        # extra pull "fights back" the way an Android list bounces past
        # its natural limit.
        if offset_y <= self._pull_threshold_px:
            eased = offset_y
        else:
            excess = offset_y - self._pull_threshold_px
            eased = self._pull_threshold_px + min(excess * 0.4, 60.0)
        self._pull_offset_px = eased
        self.gallery_grid.grid_view.set_margin_top(int(eased))
        self.gallery_grid.pull_revealer.set_reveal_child(eased >= 24)

    def _on_pull_drag_end(self, _gesture: Gtk.GestureDrag,
                          _offset_x: float, _offset_y: float) -> None:
        if not self._pull_started_at_top:
            return
        triggered = self._pull_offset_px >= self._pull_threshold_px
        self._pull_started_at_top = False
        if triggered:
            self._trigger_pull_refresh()
            self._animate_pull_release(duration_ms=420, easing=Adw.Easing.EASE_OUT_CUBIC)
        else:
            self._animate_pull_release(duration_ms=260, easing=Adw.Easing.EASE_OUT_BACK)
        self._pull_offset_px = 0.0

    def _animate_pull_release(self, duration_ms: int, easing: "Adw.Easing") -> None:
        """Spring the grid's top margin back to 0. EASE_OUT_BACK gives a
        small wobble at the end; EASE_OUT_CUBIC just glides."""
        start = float(self.gallery_grid.grid_view.get_margin_top())
        if start <= 0:
            self.gallery_grid.pull_revealer.set_reveal_child(False)
            return
        target = Adw.CallbackAnimationTarget.new(
            lambda v: self.gallery_grid.grid_view.set_margin_top(max(0, int(v)))
        )
        animation = Adw.TimedAnimation.new(
            self.gallery_grid.grid_view, start, 0.0, duration_ms, target,
        )
        animation.set_easing(easing)
        animation.connect(
            "done",
            lambda *_: self.gallery_grid.pull_revealer.set_reveal_child(False),
        )
        self._pull_animation = animation
        animation.play()

    def _trigger_pull_refresh(self) -> None:
        if not self.refresh_button.get_sensitive():
            return
        LOGGER.info("Pull refresh triggered for category %s", self.category)
        self.gallery_grid.pull_revealer.set_reveal_child(True)
        # scope="current" keeps the scan limited to the active category —
        # the pull gesture is a "refresh what I'm looking at" affordance.
        self.refresh(scan=True, scope="current")
        GLib.timeout_add(
            1200,
            lambda: self.gallery_grid.pull_revealer.set_reveal_child(False) or False,
        )

    def _update_tile_size(self, scroller_width: int) -> None:
        if scroller_width <= 0:
            return
        columns = min(max(int(self.settings.grid_columns), 2), 10)
        # Subtract 2px margin per tile to prevent layout feedback loop
        cell_size = max(32, scroller_width // columns)
        # Only set height: the homogeneous Box distributes width automatically,
        # so min/max-width here would create a measurement feedback loop.
        # (GTK4 CSS has no max-height for generic widgets, so we rely on
        # the tile's lack of vexpand to keep it at min-height.)
        load_css(
            self._tile_css,
            f""".gallery-tile {{
                min-height: {cell_size}px;
            }}""",
        )

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        # Pre-baked rotation classes in 5° increments (0..355°). Toggling a class
        # is much cheaper than rewriting a CssProvider during a live gesture.
        rotation_css = "\n".join(
            f".rot-{i*5} {{ transform: rotate({i*5}deg); }}" for i in range(72)
        )
        load_css(
            provider,
            """
            .gallery-tile {
                padding: 0;
                margin: 1px;
                border-radius: 0;
                min-width: 0;
                min-height: 0;
            }
            .gallery-tile > * {
                margin: 0;
            }
            .gallery-tile.empty,
            .gallery-tile.empty:hover,
            .gallery-tile.empty:active {
                background: transparent;
                box-shadow: none;
            }
            listview.gallery-grid > row {
                padding: 0;
            }
            listview.gallery-grid > row:hover,
            listview.gallery-grid > row:selected {
                background: transparent;
            }
            gridview.gallery-grid > child {
                padding: 1px;
            }
            .date-header {
                min-height: 120px;
                padding: 16px 8px;
                background: transparent;
                color: @window_fg_color;
                font-size: 32px;
            }
            /* Up/down arrows pinned to the right edge of every month header.
               Subtle by default (low opacity), full opacity on hover so they
               stay discoverable without competing with the date typography.
               Hit-area sized for touch (~44px square per Apple HIG / Material
               minimum); padding rather than icon scaling does the work, so
               the icon glyph itself stays at its default symbolic 16px. */
            .date-header-nav {
                opacity: 0.45;
                min-width: 44px;
                min-height: 44px;
                padding: 12px;
            }
            .date-header-nav:hover {
                opacity: 1.0;
            }
            /* Pin the icon glyph at the default symbolic size: without this,
               some themes scale the icon proportionally with the button's
               padding/min-size, which would defeat the "big button, small
               icon" intent. */
            .date-header-nav image {
                -gtk-icon-size: 16px;
            }
            .folder-label {
                background: rgba(0,0,0,0.55);
                color: white;
                padding: 4px 8px;
                font-weight: 600;
            }
            .view-switcher {
                border-top: 1px solid @borders;
                padding-top: 4px;
            }
            /* Side rail (nav at left/right). Sized to content, with a
               separator border on the side facing the gallery. No max-width
               here: combined with the descendant button min-width override
               below, GTK4 was observed to enter a measure feedback loop when
               a long category label pushed the natural width past the cap. */
            .nav-sidebar {
                padding: 4px 2px;
            }
            .nav-sidebar button {
                /* Cancel libadwaita's button min-width so the rail tracks the
                   actual icon+label size rather than the toolbar-toggle width. */
                min-width: 0;
                padding: 6px 4px;
                margin: 1px 2px;
            }
            .nav-sidebar-left {
                border-right: 1px solid @borders;
            }
            .nav-sidebar-right {
                border-left: 1px solid @borders;
            }
            .sel-check {
                background: alpha(@window_bg_color, 0.75);
                border-radius: 999px;
                padding: 2px;
                margin: 5px;
                -gtk-icon-size: 18px;
            }
            .sel-check.checked {
                background: @accent_bg_color;
                color: @accent_fg_color;
            }
            .viewer-date {
                /* Floating pill at the top of the viewer; readable over any image */
                padding: 12px 24px 14px 24px;
                margin-top: 12px;
                background: rgba(0,0,0,0.55);
                color: white;
                border-radius: 18px;
            }
            .viewer-date-day {
                font-size: 32px;
                font-weight: 500;
                opacity: 0.95;
            }
            .viewer-date-year {
                font-size: 22px;
                opacity: 0.65;
                margin-top: -4px;
            }
            .viewer-filename {
                /* Same black-pill look as the date, but at the regular font size */
                padding: 8px 18px;
                background: rgba(0,0,0,0.55);
                color: white;
                border-radius: 14px;
            }
            /* Editor toolbar: icons follow the standard window foreground so
               they stay legible in both light and dark themes (the viewer
               window's fullscreen black backdrop would otherwise tint the
               toolbar dark and make the symbolic icons disappear). */
            .editor-nav,
            .editor-nav button,
            .editor-nav image,
            .editor-nav label {
                color: @window_fg_color;
            }
            .editor-nav {
                background-color: @headerbar_bg_color;
            }
            """
            + rotation_css
        )
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        Gtk.StyleContext.add_provider_for_display(
            display, self._tile_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _apply_theme(self) -> None:
        style = Adw.StyleManager.get_default()
        style.set_color_scheme(
            {
                "system": Adw.ColorScheme.DEFAULT,
                "light": Adw.ColorScheme.FORCE_LIGHT,
                "dark": Adw.ColorScheme.FORCE_DARK,
            }.get(self.settings.theme, Adw.ColorScheme.DEFAULT)
        )
