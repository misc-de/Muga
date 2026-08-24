# Muga — Photo Gallery & Camera

A fast, clean photo and video gallery for Linux desktops and Linux phones (Phosh / FuriOS), built with GTK 4 and libadwaita.  
  
⚠️ **AI-assisted project**  
  
![Muga](muga.png)

---

## What is Muga?

Muga is a gallery app that feels right at home on a modern GNOME desktop and adapts to Linux phones running Phosh (FuriOS, Droidian, UBports). It scans your media folders, ensures consistently smooth performance thanks to a thumbnail cache and an SQLite index, and stays out of your way while doing so. It now also includes a built-in **Camera** that captures photos and video — on phones running Halium / gst-droid the camera taps directly into the hardware (flash, torch, sensors). In addition to several editing features, it allows you to effortlessly integrate your Nextcloud Photos.

---

## Screenshots
<img width="270" alt="Overview — every library in one grid" src="data/screenshots/overview.jpg" />
<img width="270" alt="Photos grouped by month and year" src="data/screenshots/date-grouping.jpg" />
<img width="270" alt="Full-screen viewer" src="data/screenshots/viewer.jpg" />
<img width="270" alt="Editor — filters" src="data/screenshots/editor-filters.jpg" />
<img width="270" alt="Editor — brightness, contrast and colour channels" src="data/screenshots/editor-adjust.jpg" />
<img width="270" alt="Settings — media folders" src="data/screenshots/settings-folders.jpg" />
<img width="270" alt="Settings — appearance and cache" src="data/screenshots/settings-appearance.jpg" />
<img width="270" alt="Settings — Nextcloud" src="data/screenshots/settings-nextcloud.jpg" />

---

## Highlights

- **Multiple libraries**  
separate tabs for Photos, Pictures, Videos, Screenshots, and any extra folders you add
- **Built-in camera**  
capture photos and record video without leaving the app; swipe the shutter between photo and video modes. On Halium / gst-droid phones (FuriOS, Droidian, …) the camera drives the HAL directly:
  - optional geotagging via GeoClue2, with EXIF written in place — no JPEG re-encode
  - self-timer (3 / 10 s), pinch-to-zoom, tap-to-focus, flash for photos, video light / torch for recording, JPEG quality presets, video quality presets
  - handedness toggle (right / left / neutral) so the shutter button sits under your thumb
- **Mobile-adaptive UI**  
narrow-window breakpoints hide / re-order desktop-only chrome on phones; pull-to-refresh replaces the title-bar refresh icon
- **Nextcloud sync**  
browse your Nextcloud photo library directly, no FUSE or GVFS mount needed; thumbnails load on demand
- **QR code scanner**  
scan Nextcloud app-password QR codes straight from the camera to connect your account instantly
- **Date grouping**  
sort by date and photos are grouped under clear section headers (day / week / month / year). Long galleries use a sliding window: only the visible months stay in memory so jumping forward through years stays fast.
- **Built-in editor**  
crop, rotate, adjust brightness / contrast / colour channels, add frames for holidays and occasions, drop stickers
- **Video playback**  
watch videos directly in the app or hand them off to any external player
- **Selection mode**  
long-press any photo to enter multi-select, then delete or move a whole batch at once
- **Folder view**  
drill into subfolders; folder tiles show a 2×2 preview mosaic

---

## Install & Run

**One-time install** — adds a launcher and a desktop entry, no root required:
```bash
bash install.sh
```
Then launch **Muga** from your app menu, or type `muga` in a terminal.

**Run directly without installing:**
```bash
python3 -m muga
```

**Uninstall:**
```bash
bash uninstall.sh
```

**Flatpak** — sandboxed, no Python dependencies on the host. Prebuilt and
signed for **x86_64** and **aarch64**:
```bash
flatpak remote-add --if-not-exists muga https://misc-de.github.io/Muga/io.github.miscde.Muga.flatpakrepo
flatpak install muga io.github.miscde.Muga
flatpak run io.github.miscde.Muga
```
Updates from then on with `flatpak update`.

To build it yourself instead:
```bash
flatpak install -y flathub org.gnome.Platform//49 org.gnome.Sdk//49
flatpak-builder --user --install --force-clean build-dir io.github.miscde.Muga.yml
flatpak run io.github.miscde.Muga
```
The Flatpak covers desktops and v4l2 webcams. The Halium / gst-droid camera
path (FuriOS, Droidian) needs the Android HAL and sysfs torch nodes, which a
sandbox cannot reach — on those phones use `install.sh` above. See
[docs/compatibility.md](docs/compatibility.md).

Packaging the repository yourself — both architectures, signing, gh-pages —
is in the [Makefile](Makefile) (`make help`); the Flathub submission is
described in [docs/flathub.md](docs/flathub.md).

For camera release checks on phones and desktops, see
[docs/camera-validation.md](docs/camera-validation.md).
For the module layout, see [docs/architecture.md](docs/architecture.md); for
the GTK/libadwaita versions Muga targets, [docs/compatibility.md](docs/compatibility.md).

## Translating

Catalogues are standard gettext, in [po/](po/) — usable with Poedit, Weblate or
`msgmerge`. To add a language, copy `po/muga.pot` to `po/<code>.po` and
translate it; it is selectable in Settings as soon as the file exists.

Compiling is handled for you: `pip install .` builds the catalogues during the
install, and `install.sh` does the same. Running from a checkout needs no build
step at all — Muga reads `po/*.po` directly when no compiled catalogue is
present. `tools/i18n.py` carries its own MO writer, so none of this requires
gettext to be installed.

```bash
tools/i18n.py extract    # rebuild po/muga.pot from the sources
tools/i18n.py update     # merge the template into every po/*.po
tools/i18n.py compile    # build the .mo files an install ships
tools/i18n.py stat       # coverage per language
```

---

## Diagnostics

Open **Settings → Diagnostics** to copy a compact report for bug reports.
It includes runtime versions, storage paths, media-folder settings,
Nextcloud connection state, GStreamer camera plugins, and detected torch
sysfs paths. It does **not** include passwords or app tokens.

For camera debugging from a terminal, run:
```bash
MUGA_CAMERA_DEBUG=1 python3 -m muga
```

---

## Nextcloud Setup

1. Open **Settings → Nextcloud**
2. Enter your server URL and username
3. Either paste an app password or tap **Scan QR code** — go to *Nextcloud → Settings → Security → App passwords*, create one, and scan the QR code with your camera
4. Hit **Connect**

Photos are streamed directly over WebDAV. Thumbnails are cached locally; full files are only downloaded when you open them.

---

## Privacy & License

- **Privacy:** local-first, no telemetry. See [PRIVACY.md](PRIVACY.md) for what is stored where and how to wipe it.
- **License:** [MIT](LICENSE).
