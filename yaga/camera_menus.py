"""The camera's menus: v4l2 controls, quality presets and settings.

Split out of ``CameraWindow``. Three popovers, plus the state they write:

* **Controls** — exposure, white balance and focus, built from whatever the
  v4l2 device actually reports. Probing is deferred until the gear is first
  opened, because enumerating controls on a busy device is not free.
* **Quality** — photo and video resolution and encoder presets.
* **Settings** — geotagging, self-timer, handedness.

Writes are debounced: a user dragging a slider produces a burst of changes,
and each one would otherwise be a settings.save() to disk. ``_on_close``
flushes whatever is still pending.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from . import camera_controls
from .camera_controls import V4l2Control
from .camera_debug import LOGGER
from .camera_orientation import _ICON_ROTATION_DEG
from .camera_widgets import RotatableLabel as _RotatableLabel

# Debounce window for settings.save() calls. Rapid slider/button taps
# coalesce into one disk write at the trailing edge; _on_close flushes
# pending writes synchronously.
_PERSIST_DELAY_MS = 500


class CameraMenusMixin:
    """Popovers and the preferences they drive. Mixed into CameraWindow.

    The block below is the contract with the host class: every name is created
    in ``CameraWindow.__init__`` (or defined on it) and only annotated here.
    """

    _settings: Any
    _controls: dict[str, V4l2Control]
    _controls_built: bool
    _controls_cache: dict[str, dict[str, V4l2Control]]
    _gear_button: Gtk.MenuButton
    _gear_popover: Gtk.Popover | None
    _quality_button: Gtk.MenuButton
    _geo_switch: Gtk.Switch | None
    _geo_enabled: bool
    _flash_enabled: bool
    _capture_mode: str
    _handedness: str
    # (widget, value) pairs — the popovers need the order, and a dict keyed by
    # widget would not survive a rebuild.
    _handedness_buttons: list[tuple[Gtk.Button, str]]
    _image_size_buttons: list[tuple[Gtk.Button, tuple[int, int] | None]]
    _photo_quality_buttons: list[tuple[Gtk.Button, int]]
    _video_quality_buttons: list[tuple[Gtk.Button, int]]
    _image_resolution: tuple[int, int] | None
    _jpeg_quality: int
    _video_bitrate_kbps: int
    _device_orientation: str
    _applied_layout: str | None
    _layout_is_landscape: bool | None
    _settings_persist_source: int | None
    _pipeline: Any
    # Bound in CameraWindow.__init__ as an attribute, not a method.
    _: Any

    if TYPE_CHECKING:
        # Provided by CameraWindow; no runtime definition, so these can never
        # shadow the real methods.
        def _current_device(self) -> dict[str, Any] | None: ...
        def _update_shutter_icon(self) -> None: ...
        def _apply_layout_for(self, orientation: str) -> None: ...
        def _orient_seq(self, items: list) -> list: ...
        def _update_flash_tooltip(self) -> None: ...
        def _apply_flash_to_pipeline(self) -> None: ...
        def _apply_mode_visibility(self) -> None: ...
        def _on_geo_switch_state_set(self, _sw: Gtk.Switch, state: bool) -> bool: ...
        def _show_toast(self, text: str, sticky: bool = False) -> None: ...

    def _mark_controls_dirty_for_device(self, device: dict[str, Any]) -> None:
        """Cheap pre-pipeline-start state reset. Does NOT touch the device
        via v4l2-ctl — that runs only when the user actually opens the
        gear popover. We still show the gear unconditionally when
        v4l2-ctl is installed; if the device turns out to have nothing
        tunable, the post-probe code hides it."""
        path = device.get("path") or ""
        self._controls = {}
        if self._gear_popover is not None:
            self._gear_button.set_popover(None)
            self._gear_popover = None
        self._controls_built = False
        # Show the gear if v4l2-ctl is available and we have a /dev path
        # to probe. The probe itself happens on first popover-open.
        self._gear_button.set_visible(
            bool(path) and camera_controls.controls_supported()
        )

    def _ensure_controls_probed(self) -> None:
        """Run the v4l2-ctl probe for the active device if we haven't yet.
        Called when the gear popover is first opened — never at pipeline
        start, so the probe can't interfere with v4l2src negotiation."""
        device = self._current_device()
        if device is None:
            return
        path = device.get("path") or ""
        if not path or not camera_controls.controls_supported():
            self._controls = {}
            return
        cached = self._controls_cache.get(path)
        if cached is None:
            cached = camera_controls.probe_controls(path)
            self._controls_cache[path] = cached
        self._controls = cached
        # Now that we know what's tunable, hide the gear if it turned
        # out to have nothing useful.
        has_any = any(
            camera_controls.resolve(cached, logical) is not None
            for logical in (
                "auto_exposure", "exposure_absolute",
                "auto_white_balance", "white_balance_temp",
                "auto_focus", "focus_absolute",
                "gain", "brightness", "contrast", "saturation",
            )
        )
        self._gear_button.set_visible(has_any)

    def _on_gear_toggled(self, _btn: Gtk.MenuButton, _pspec: Any) -> None:
        if self._gear_button.get_active() and not self._controls_built:
            self._ensure_controls_probed()
            self._build_controls_popover()

    def _build_controls_popover(self) -> None:
        popover = Gtk.Popover()
        popover.set_autohide(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10); box.set_margin_bottom(10)
        box.set_margin_start(10); box.set_margin_end(10)
        box.set_size_request(280, -1)
        popover.set_child(box)

        # Exposure section.
        exp_auto = camera_controls.resolve(self._controls, "auto_exposure")
        exp_abs = camera_controls.resolve(self._controls, "exposure_absolute")
        exp_slider: Gtk.Scale | None = None
        if exp_auto is not None or exp_abs is not None:
            self._add_section_header(box, self._("Exposure"))
            if exp_auto is not None and exp_auto.type == "menu":
                manual_v, auto_v = self._auto_manual_values(exp_auto.menu)
                if manual_v is not None and auto_v is not None:
                    auto_switch = self._switch_row(
                        box, self._("Auto"), exp_auto.value == auto_v
                    )
                    auto_switch.connect(
                        "notify::active",
                        lambda sw, _p: self._apply_auto(
                            exp_auto, sw.get_active(), auto_v, manual_v, [exp_slider]
                        ),
                    )
            if exp_abs is not None and exp_abs.type == "int":
                exp_slider = self._slider_row(
                    box, self._("Time"), exp_abs,
                    self._control_setter(exp_abs),
                )
                exp_slider.set_sensitive(not exp_abs.inactive)

        # White balance section.
        wb_auto = camera_controls.resolve(self._controls, "auto_white_balance")
        wb_temp = camera_controls.resolve(self._controls, "white_balance_temp")
        wb_slider: Gtk.Scale | None = None
        if wb_auto is not None or wb_temp is not None:
            self._add_section_header(box, self._("White balance"))
            if wb_auto is not None:
                if wb_auto.type == "bool":
                    wb_switch = self._switch_row(
                        box, self._("Auto"), bool(wb_auto.value)
                    )
                    wb_switch.connect(
                        "notify::active",
                        lambda sw, _p: self._apply_bool(
                            wb_auto, sw.get_active(), [wb_slider]
                        ),
                    )
                elif wb_auto.type == "menu":
                    m_v, a_v = self._auto_manual_values(wb_auto.menu)
                    if m_v is not None and a_v is not None:
                        wb_switch = self._switch_row(
                            box, self._("Auto"), wb_auto.value == a_v
                        )
                        wb_switch.connect(
                            "notify::active",
                            lambda sw, _p: self._apply_auto(
                                wb_auto, sw.get_active(), a_v, m_v, [wb_slider]
                            ),
                        )
            if wb_temp is not None and wb_temp.type == "int":
                wb_slider = self._slider_row(
                    box, self._("Temperature"), wb_temp,
                    self._control_setter(wb_temp),
                )
                wb_slider.set_sensitive(not wb_temp.inactive)

        # Focus section.
        focus_auto = camera_controls.resolve(self._controls, "auto_focus")
        focus_abs = camera_controls.resolve(self._controls, "focus_absolute")
        focus_slider: Gtk.Scale | None = None
        if focus_auto is not None or focus_abs is not None:
            self._add_section_header(box, self._("Focus"))
            if focus_auto is not None and focus_auto.type == "bool":
                fsw = self._switch_row(box, self._("Auto"), bool(focus_auto.value))
                fsw.connect(
                    "notify::active",
                    lambda sw, _p: self._apply_bool(
                        focus_auto, sw.get_active(), [focus_slider]
                    ),
                )
            if focus_abs is not None and focus_abs.type == "int":
                focus_slider = self._slider_row(
                    box, self._("Position"), focus_abs,
                    self._control_setter(focus_abs),
                )
                focus_slider.set_sensitive(not focus_abs.inactive)

        # Image section (always present if we have any of these).
        image_controls: list[tuple[str, str]] = [
            ("gain", self._("Gain")),
            ("brightness", self._("Brightness")),
            ("contrast", self._("Contrast")),
            ("saturation", self._("Saturation")),
        ]
        image_added = False
        for logical, label in image_controls:
            ctrl = camera_controls.resolve(self._controls, logical)
            if ctrl is None or ctrl.type != "int":
                continue
            if not image_added:
                self._add_section_header(box, self._("Image"))
                image_added = True
            self._slider_row(
                box, label, ctrl,
                self._control_setter(ctrl),
            )

        # Reset button at the bottom — restores driver defaults across all
        # exposed controls so the user can recover from a tweak experiment.
        reset = Gtk.Button(label=self._("Reset to defaults"))
        reset.add_css_class("flat")
        reset.set_margin_top(8)
        reset.connect("clicked", lambda _b: self._reset_controls_to_default())
        box.append(reset)

        self._gear_popover = popover
        self._gear_button.set_popover(popover)
        self._controls_built = True

    def _add_section_header(self, parent: Gtk.Box, text: str) -> None:
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class("heading")
        label.set_margin_top(4)
        parent.append(label)

    def _switch_row(self, parent: Gtk.Box, text: str, active: bool) -> Gtk.Switch:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.set_hexpand(True)
        row.append(lbl)
        sw = Gtk.Switch()
        sw.set_active(active)
        sw.set_valign(Gtk.Align.CENTER)
        row.append(sw)
        parent.append(row)
        return sw

    def _control_setter(self, ctrl: V4l2Control) -> Callable[[int], Any]:
        """Return a setter bound to *ctrl* right now.

        Binding eagerly matters: the caller builds these inside loops and
        branches, so a plain closure over the loop variable would set
        whichever control happened to be last.
        """
        return lambda value: camera_controls.set_control(
            self._current_device_path(), ctrl.name, value
        )

    def _slider_row(
        self,
        parent: Gtk.Box,
        text: str,
        ctrl: V4l2Control,
        on_change: Callable[[int], Any],
    ) -> Gtk.Scale:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.set_size_request(80, -1)
        row.append(lbl)
        lo = ctrl.min if ctrl.min is not None else 0
        hi = ctrl.max if ctrl.max is not None else 100
        step = max(1, ctrl.step or 1)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        if ctrl.value is not None:
            scale.set_value(ctrl.value)

        # Debounce writes — dragging a slider fires value-changed dozens of
        # times per second; we don't need to launch a subprocess per tick.
        pending: dict[str, Any] = {"timeout": None, "value": None}

        def fire() -> bool:
            pending["timeout"] = None
            v = pending["value"]
            if v is None:
                return False
            on_change(int(v))
            return False

        def on_value_changed(s: Gtk.Scale) -> None:
            pending["value"] = s.get_value()
            if pending["timeout"] is not None:
                GLib.source_remove(pending["timeout"])
            pending["timeout"] = GLib.timeout_add(80, fire)

        scale.connect("value-changed", on_value_changed)
        row.append(scale)
        parent.append(row)
        return scale

    def _auto_manual_values(self, menu: dict[int, str]) -> tuple[int | None, int | None]:
        """For a v4l2 exposure-style menu, pick the (manual, auto) values.
        'Manual' is the obvious one; 'auto' falls back through "Auto Mode",
        "Aperture Priority Mode", then anything else."""
        manual: int | None = None
        auto: int | None = None
        for v, label in menu.items():
            if "manual" in label.lower():
                manual = v
                break
        for v, label in menu.items():
            if v == manual:
                continue
            if "auto" in label.lower():
                auto = v
                break
        if auto is None:
            for v, label in menu.items():
                if v == manual:
                    continue
                if "aperture" in label.lower():
                    auto = v
                    break
        if auto is None:
            for v in menu:
                if v != manual:
                    auto = v
                    break
        return manual, auto

    def _apply_auto(
        self,
        ctrl: V4l2Control,
        auto_on: bool,
        auto_value: int,
        manual_value: int,
        dependents: list[Gtk.Scale | None],
    ) -> None:
        target = auto_value if auto_on else manual_value
        ok = camera_controls.set_control(
            self._current_device_path(), ctrl.name, target
        )
        if not ok:
            return
        ctrl.value = target
        # When auto is on, manual sliders are masked by the kernel — disable
        # them locally to mirror that without needing a re-probe.
        for dep in dependents:
            if dep is not None:
                dep.set_sensitive(not auto_on)

    def _apply_bool(
        self,
        ctrl: V4l2Control,
        on: bool,
        dependents: list[Gtk.Scale | None],
    ) -> None:
        ok = camera_controls.set_control(
            self._current_device_path(), ctrl.name, 1 if on else 0
        )
        if not ok:
            return
        ctrl.value = 1 if on else 0
        for dep in dependents:
            if dep is not None:
                dep.set_sensitive(not on)

    def _reset_controls_to_default(self) -> None:
        path = self._current_device_path()
        if not path:
            return
        for ctrl in self._controls.values():
            if ctrl.default is None or ctrl.readonly:
                continue
            camera_controls.set_control(path, ctrl.name, ctrl.default)
        # Force a rebuild on next open so the UI reflects the reset values.
        self._controls_cache.pop(path, None)
        self._controls = camera_controls.probe_controls(path)
        self._controls_cache[path] = self._controls
        if self._gear_popover is not None:
            self._gear_button.set_popover(None)
            self._gear_popover = None
        self._controls_built = False
        self._show_toast(self._("Controls reset"))

    def _current_device_path(self) -> str:
        device = self._current_device()
        return device.get("path") or "" if device else ""

    def _build_quality_popover(self) -> Gtk.Popover:
        # Orientation-aware layout: in landscape, the whole popover
        # content is transposed so it reads right in the user's view.
        # Outer box stacks the sections HORIZONTALLY in widget space
        # (which is vertical in the user's view); each inner row stacks
        # the buttons VERTICALLY in widget space (horizontal for user).
        # Button labels use _RotatableLabel so they're upright.
        landscape = bool(self._layout_is_landscape)
        outer_orient = (
            Gtk.Orientation.HORIZONTAL if landscape else Gtk.Orientation.VERTICAL
        )
        inner_orient = (
            Gtk.Orientation.VERTICAL if landscape else Gtk.Orientation.HORIZONTAL
        )
        label_rot = _ICON_ROTATION_DEG.get(self._device_orientation, 0)

        # Reset per-popover state — we rebuild this whole subtree on
        # every orientation change, so the old entries point at widgets
        # that are about to be unparented.
        self._photo_quality_buttons = []
        self._video_quality_buttons = []
        self._image_size_buttons = []

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=outer_orient, spacing=10)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)

        def _rotated_text(text: str) -> _RotatableLabel:
            lab = _RotatableLabel()
            lab.set_label(text)
            lab.set_rotation_deg(label_rot)
            return lab

        def _rotated_button(text: str, on_click: Callable) -> Gtk.Button:
            btn = Gtk.Button()
            btn.set_child(_rotated_text(text))
            btn.connect("clicked", on_click)
            return btn

        def _section(
            title: str,
            presets: list[tuple[str, int]],
            current: int,
            on_pick: Callable[[int], None],
            store: list[tuple[Gtk.Button, int]],
        ) -> None:
            # Multi-button sections (Photo/Video quality, Photo size,
            # Handedness): header ABOVE the row of buttons. Portrait =
            # VERTICAL (header above buttons), landscape = HORIZONTAL
            # in widget (header LEFT of buttons in widget = ABOVE in
            # user's rotated view).
            section_orient = (
                Gtk.Orientation.HORIZONTAL if landscape
                else Gtk.Orientation.VERTICAL
            )
            sec = Gtk.Box(orientation=section_orient, spacing=6)
            header = _rotated_text(title)
            header.set_xalign(0)
            header.add_css_class("heading")
            row = Gtk.Box(orientation=inner_orient, spacing=6)
            for label, value in presets:
                btn = _rotated_button(label, lambda _b, v=value: on_pick(v))
                if value == current:
                    btn.add_css_class("suggested-action")
                store.append((btn, value))
                row.append(btn)
            # Header above buttons in user's view — _orient_seq flips
            # widget child order for BOTTOM_UP / RIGHT_UP so the visual
            # ends up consistent across all four orientations.
            for w in self._orient_seq([header, row]):
                sec.append(w)
            box.append(sec)

        if self._capture_mode == "photo":
            _section(
                self._("Photo quality"),
                [
                    (self._("Eco"),  60),
                    (self._("Std"),  85),
                    (self._("High"), 92),
                    (self._("Max"),  98),
                ],
                self._jpeg_quality,
                self._set_jpeg_quality,
                self._photo_quality_buttons,
            )
            box.append(Gtk.Separator(orientation=inner_orient))

            # Photo size: string-keyed presets, built manually. Header
            # above the row of buttons, same as the other multi-button
            # sections.
            section_orient = (
                Gtk.Orientation.HORIZONTAL if landscape
                else Gtk.Orientation.VERTICAL
            )
            size_sec = Gtk.Box(orientation=section_orient, spacing=6)
            size_header = _rotated_text(self._("Photo size"))
            size_header.set_xalign(0)
            size_header.add_css_class("heading")
            size_row = Gtk.Box(orientation=inner_orient, spacing=6)
            size_presets: list[tuple[str, tuple[int, int] | None]] = [
                (self._("Max"),  None),
                (self._("2K"),   (2560, 1920)),
                (self._("FHD"),  (1920, 1440)),
                (self._("HD"),   (1280, 960)),
            ]
            for label, wh in size_presets:
                btn = _rotated_button(
                    label, lambda _b, v=wh: self._set_image_resolution(v),
                )
                if wh == self._image_resolution:
                    btn.add_css_class("suggested-action")
                self._image_size_buttons.append((btn, wh))
                size_row.append(btn)
            for w in self._orient_seq([size_header, size_row]):
                size_sec.append(w)
            box.append(size_sec)

        elif self._capture_mode == "video":
            _section(
                self._("Video quality"),
                [
                    (self._("Eco"),   2000),
                    (self._("Std"),   4000),
                    (self._("High"),  8000),
                    (self._("Max"),  16000),
                ],
                self._video_bitrate_kbps,
                self._set_video_bitrate,
                self._video_quality_buttons,
            )

        popover.set_child(box)
        return popover

    def _build_settings_popover(self) -> Gtk.Popover:
        # Settings popover renders in PORTRAIT layout regardless of
        # device orientation. Earlier iterations rotated the text and
        # the switch in landscape so they "appeared upright" in the
        # user's tilted view — but the rotated widgets clipped (the
        # leading 'G' of "Geotagging" disappeared) and the switch
        # ended up oriented along an axis the user didn't expect.
        # Keeping a single portrait layout means the user tilts their
        # head once to read the popover but the contents always look
        # like a normal column of horizontal controls.
        self._handedness_buttons = []

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)

        def _text(text: str) -> Gtk.Label:
            lab = Gtk.Label()
            lab.set_label(text)
            return lab

        # Handedness: header above a horizontal row of three buttons.
        sec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header = _text(self._("Handedness"))
        header.set_xalign(0)
        header.add_css_class("heading")
        sec.append(header)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        presets: list[tuple[str, str]] = [
            (self._("Right"),   "right"),
            (self._("Left"),    "left"),
            (self._("Neutral"), "neutral"),
        ]
        for label, value in presets:
            btn = Gtk.Button()
            btn.set_child(_text(label))
            if value == self._handedness:
                btn.add_css_class("suggested-action")
            btn.connect("clicked", lambda _b, v=value: self._set_handedness(v))
            self._handedness_buttons.append((btn, value))
            row.append(btn)
        sec.append(row)
        box.append(sec)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Geotagging: header on the left, switch on the right — a
        # standard one-row boolean.
        gps_sec = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        gps_header = _text(self._("Geotagging"))
        gps_header.set_xalign(0)
        gps_header.add_css_class("heading")
        gps_header.set_hexpand(True)
        gps_header.set_valign(Gtk.Align.CENTER)
        self._geo_switch = Gtk.Switch()
        self._geo_switch.set_active(self._geo_enabled)
        self._geo_switch.set_halign(Gtk.Align.END)
        self._geo_switch.set_valign(Gtk.Align.CENTER)
        self._geo_switch.connect("state-set", self._on_geo_switch_state_set)
        gps_sec.append(gps_header)
        gps_sec.append(self._geo_switch)
        box.append(gps_sec)

        popover.set_child(box)
        return popover

    def _set_handedness(self, value: str) -> None:
        if value not in ("right", "left", "neutral"):
            return
        if value == self._handedness:
            return
        self._handedness = value
        # Persist via the shared debounced path; the flush method writes
        # the `handedness` field on Settings along with the other camera
        # picks, so the standalone save() call here is unnecessary.
        self._persist_settings()
        # Highlight the active button.
        for btn, v in self._handedness_buttons:
            if v == value:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        # Re-run the layout pass for the current orientation so shutter
        # and options bar reposition to the new side.
        if self._applied_layout is not None:
            current = self._applied_layout
            self._applied_layout = None  # force re-apply
            self._apply_layout_for(current)

    def _persist_settings(self) -> None:
        # Debounced — calls accumulate, flushed once after _PERSIST_DELAY_MS.
        # Prevents settings.save() spam when sliders or quality buttons are
        # tapped rapidly. Flushed synchronously on close.
        if self._settings is None:
            return
        if self._settings_persist_source is not None:
            try:
                GLib.source_remove(self._settings_persist_source)
            except Exception:
                LOGGER.debug("camera cleanup/op failed", exc_info=True)
        self._settings_persist_source = GLib.timeout_add(
            _PERSIST_DELAY_MS, self._persist_settings_flush
        )

    def _persist_settings_flush(self) -> bool:
        self._settings_persist_source = None
        if self._settings is None:
            return False
        try:
            self._settings.camera_jpeg_quality = int(self._jpeg_quality)
            self._settings.camera_video_bitrate_kbps = int(self._video_bitrate_kbps)
            if self._image_resolution is None:
                self._settings.camera_image_resolution = None
            else:
                self._settings.camera_image_resolution = [
                    int(self._image_resolution[0]),
                    int(self._image_resolution[1]),
                ]
            self._settings.handedness = self._handedness
            self._settings.camera_geo_enabled = bool(self._geo_enabled)
            self._settings.camera_flash_enabled = bool(self._flash_enabled)
            self._settings.save()
        except Exception:
            LOGGER.debug("camera settings persist failed", exc_info=True)
        return False

    def _set_jpeg_quality(self, value: int) -> None:
        self._jpeg_quality = value
        # Live-update the running jpegenc; no pipeline restart needed.
        if self._pipeline is not None:
            jpeg = self._pipeline.get_by_name("snap_jpeg")
            if jpeg is not None:
                try:
                    jpeg.set_property("quality", value)
                except Exception:
                    LOGGER.debug("jpegenc quality update failed", exc_info=True)
        for btn, v in self._photo_quality_buttons:
            if v == value:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        self._persist_settings()

    def _set_video_bitrate(self, value: int) -> None:
        self._video_bitrate_kbps = value
        for btn, v in self._video_quality_buttons:
            if v == value:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        self._persist_settings()

    def _set_image_resolution(
        self, wh: tuple[int, int] | None,
    ) -> None:
        self._image_resolution = wh
        for btn, v in self._image_size_buttons:
            if v == wh:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        self._persist_settings()

    def _set_capture_mode(self, mode: str) -> None:
        if mode == self._capture_mode or mode not in ("photo", "video"):
            return
        self._capture_mode = mode
        # Rebuild the popover so it shows mode-relevant sections.
        self._quality_button.set_popover(self._build_quality_popover())
        self._update_shutter_icon()
        self._apply_mode_visibility()
        # Flash button's tooltip + behaviour depends on mode (flash vs
        # torch). Re-apply both so the user sees the right label and
        # the source's flash-mode reflects the new semantic.
        self._update_flash_tooltip()
        self._apply_flash_to_pipeline()
        self._show_toast(
            self._("Video mode") if mode == "video" else self._("Photo mode")
        )
