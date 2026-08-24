# Runtime baseline

Muga runs on two quite different stacks, and the older one sets the floor.

| Component   | Minimum | Why that number                                                        |
|-------------|---------|------------------------------------------------------------------------|
| Python      | 3.11    | `pyproject.toml` `requires-python`; Debian bookworm ships 3.11          |
| GTK         | 4.8     | Debian bookworm — the base of Droidian and FuriOS                       |
| libadwaita  | 1.2     | Same                                                                    |
| Pillow      | 12.3    | First release clear of the 2026 decoder advisories (see `pyproject.toml`) |
| GStreamer   | 1.22    | Camera only; `gst-droid` on Halium devices                              |

Desktops are far ahead of this (GTK 4.22 / libadwaita 1.9 at the time of
writing), so a deprecation warning on a desktop usually points at an API whose
replacement does not exist on a phone yet.

## Deprecated APIs kept on purpose

**`Adw.PreferencesWindow`** (`muga/settings_window.py`) — deprecated in
libadwaita 1.6 in favour of `Adw.PreferencesDialog`, which needs 1.5. Below
that it does not exist at all. The two also differ in lifecycle: a dialog is
not a `Gtk.Window`, so `close-request` / `destroy` become `closed`, and
`destroy()` becomes `force_close()`. Switching means branching both the base
class and the teardown contract at runtime, on hardware this project cannot
test against. Revisit when the phone baseline reaches libadwaita 1.5.

## Deprecated APIs already replaced

These all had replacements available at or below the baseline, so they were
swapped outright:

| Was                              | Now                                  | Available since |
|----------------------------------|--------------------------------------|-----------------|
| `Gtk.CssProvider.load_from_data` | `gtk_util.load_css` (runtime switch) | GTK 4.12, falls back below |
| `Gtk.Picture.new_for_pixbuf`     | `Gtk.Picture.new_for_paintable`      | GTK 4.0         |
| `Gdk.Texture.new_for_pixbuf`     | `gtk_util.texture_from_pixbuf`       | GTK 4.0         |
| `Image.getdata` (tests)          | `Image.get_flattened_data`           | Pillow 12.3     |

`GLib.unix_signal_add_full` and the `asyncio` event-loop-policy warnings come
from PyGObject itself (`gi/events.py`), not from Muga.

## Checking against the baseline

There is no bookworm runner in CI — the GitHub workflow uses ubuntu-24.04
(GTK 4.14 / libadwaita 1.5). When touching UI code, grep for the API in the
GTK docs and check the "Available since" line against the table above rather
than trusting that it works because it works on your desktop.
