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
| `exif.py` | EXIF parsing, shared by the scanner and the viewer: camera, GPS, capture time |

The write tools sit behind `mcp_write_enabled`, checked on every call rather
than cached, so flipping the switch takes effect on the next request in both
directions. Their entire security boundary is `_resolve_for_write`: resolve
first, then require the result to sit inside a configured media folder, and
refuse symlinks outright — deleting a link and deleting its target are
different acts and the client cannot say which it meant.

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

## The two dates

A photo carries a file mtime and, usually, an EXIF capture date. They are
different facts — copying a card gives every file today's mtime — so both are
kept and the user picks which one orders the grid.

`MediaItem.display_time` (capture date, else mtime) and the SQL
`COALESCE(taken_at, mtime)` are the same rule on either side of the database.
`GalleryRenderMixin._DATE_MODES` maps each grouped sort mode to the pair
*(query order, which timestamp the month headers are cut from)*, so a mode
cannot sort by one date and label by the other — the failure that would look
like the grid being randomly wrong.

The capture-date sort needs its own indexes: no index on `mtime` can serve
`ORDER BY COALESCE(taken_at, mtime)`, and without them SQLite scans the table
and sorts it into a temp B-tree (0.1 ms → 9.9 ms per page at 30k rows). They
are created in `_MIGRATION_V10` and deliberately *not* in `SCHEMA_V1`: that
script runs against pre-`taken_at` databases, where naming the column raises
"no such column" — an error the constructor answers by discarding the index.

## Spotting the same photo twice

The aggregate views collapse repeats, and what counts as "the same photo"
depends on what the source can tell us:

* **name + size** is the baseline and all a local file offers — hashing those
  would mean reading every byte of the library on every scan.
* **a content checksum** is used on top where Nextcloud reports one
  (`<oc:checksums>` in the PROPFIND; present only for files uploaded with a
  checksum). It catches a copy that was renamed on the server, which name and
  size cannot.

The two are separate windows in `_one_row_per_file`, and a row must lead both.
That makes the checksum purely additive: a row without one sits alone in its
partition and always ranks first, so keying on the checksum can never split a
pair that name+size had joined — which matters, because a synced photo has a
checksum on the server side only.

The second window is skipped entirely when `Database.has_checksums()` is false.
It is not free — 32 ms of a 91 ms page on a 12k-picture phone library — and
with no checksums anywhere it could only rank every row 1.

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
