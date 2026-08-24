# Module layout

Muga's two main windows were single classes of ~3000 and ~4300 lines. Both are
now split across mixin modules — the class is still one object at runtime, but
each file holds one concern and can be read on its own.

## Gallery

| Module | Holds |
|---|---|
| `app.py` | `GalleryWindow` itself: construction, scanning, sorting, categories, folder creation, settings |
| `gallery_render.py` | Rendering and pagination, including the sliding month window |
| `gallery_selection.py` | Selection mode and the file actions it drives (delete, move, share) |
| `gallery_style.py` | CSS, theme, tile sizing, and the pull/swipe gestures |
| `gallery_thumbnails.py` | Thumbnail workers (local and Nextcloud) and the disk-cache budget |
| `gallery_grid.py` | The `GridView` itself — predates this split |

## Camera

| Module | Holds |
|---|---|
| `camera.py` | `CameraWindow` itself: preview pipeline, shutter, layout, focus, flash, zoom |
| `camera_capture_io.py` | Frame → file: orientation, resize, JPEG encode, EXIF (GExiv2 or Pillow) |
| `camera_menus.py` | The three popovers — v4l2 controls, quality presets, settings |
| `camera_video.py` | The recording pipeline and its finalisation |
| `camera_debug.py` | `dlog`, shared by all of the above |
| `camera_devices.py`, `camera_controls.py`, `camera_geo.py`, `camera_orientation.py`, `camera_torch.py`, `camera_widgets.py` | Device enumeration, v4l2 controls, GeoClue, sensors, torch, rotatable widgets |

## MCP server

Not part of either window — a module-level singleton, started and stopped from
`Settings → MCP` and re-synced whenever settings are applied.

| Module | Holds |
|---|---|
| `mcp_server.py` | The HTTP transport, the JSON-RPC/MCP layer, the read-only tool surface, the bind-scope resolution, and the process-wide start/stop |
| `mcp_tokens.py` | The bearer tokens: a `0600` JSON file, add / rename / remove / verify |

Where it listens is a scope, not an address: `local` / `lan` / `public` /
`all`, resolved against the machine's actual interfaces at bind time
(`resolve_bind`). Storing the scope rather than a literal IP means a DHCP
lease change does not leave a stale address in `settings.json`, and an
unresolvable scope can fall back to loopback — the only safe direction to
guess in.

The singleton is deliberate. A nav-position change destroys and recreates
`GalleryWindow`, building the new one *before* the old is gone; a
window-owned server would still be holding the listening port at that moment,
and the new window's start would fail with "address already in use".
`sync_with_settings()` instead re-points the existing server at the new
window's database and leaves the socket alone.

## Why mixins

They are honest about what this is: one window object whose methods were
grouped by topic. A method in `gallery_selection.py` still touches
`self.gallery_grid` and `self.database`; moving it did not decouple it, and
pretending otherwise with a service class taking eight constructor arguments
would have added indirection without removing the coupling.

What the split does buy:

* each file is under ~550 lines and about one thing
* every mixin opens with a **contract block** — the attributes and methods it
  expects the host class to provide, annotated so the type checker sees one
  consistent picture and a reader sees the coupling instead of guessing at it
* methods the host provides are declared under `if TYPE_CHECKING:`, so nothing
  is defined at runtime that could silently shadow the real implementation

`GalleryWindow` went from 114 methods to 49, `CameraWindow` from 127 to 87.

## Tests that read source

A few tests assert on implementation by grepping the source (`"def _on_folder_swipe" in source`).
Those now read the whole group via `gallery_source()` in `tests/test_core.py`
and `tests/test_recent_changes.py`, so moving a method between gallery modules
does not fail a test for the wrong reason. The camera equivalents name their
module directly.
