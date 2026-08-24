# Muga — Photo Gallery & Camera

A fast, clean photo and video gallery for Linux desktops and Linux phones (Phosh / FuriOS), built with GTK 4 and libadwaita.  
  
⚠️ **AI-assisted project**  
  
![Muga](muga.png)

---

## What is Muga?

Muga is a gallery app that feels right at home on a modern GNOME desktop and adapts to Linux phones running Phosh (FuriOS, Droidian, UBports). It scans your media folders, ensures consistently smooth performance thanks to a thumbnail cache and an SQLite index, and stays out of your way while doing so. It now also includes a built-in **Camera** that captures photos and video — on phones running Halium / gst-droid the camera taps directly into the hardware (flash, torch, sensors). In addition to several editing features, it allows you to effortlessly integrate your Nextcloud Photos.

---

## Screenshots
<img width="270" alt="Screenshot from 2026-05-09 17:26:52" src="https://github.com/user-attachments/assets/0fc0b6bc-4d4f-43f4-816c-42ae1efdb2da" />
<img width="270" alt="Screenshot from 2026-05-11 06:41:07" src="https://github.com/user-attachments/assets/9024d1b3-3e66-4b43-a16f-53714d736846" />
<img width="270" alt="Screenshot from 2026-05-09 17:51:42" src="https://github.com/user-attachments/assets/dbb491da-fe3a-4009-b95a-0cef134a45a0" />
<img width="270" alt="Screenshot from 2026-05-09 18:22:19" src="https://github.com/user-attachments/assets/acdcd327-486c-419c-8073-1c03cb40a053" />
<img width="270" alt="Screenshot from 2026-05-09 18:22:26" src="https://github.com/user-attachments/assets/97eab779-7d17-4cfa-b1fa-4fb995099506" />
<img width="270" alt="Screenshot from 2026-05-12 06:55:23" src="https://github.com/user-attachments/assets/2ca36f72-40ce-44ce-a80d-b6a8c4871c6d" />
<img width="270" alt="Screenshot from 2026-05-11 06:37:03" src="https://github.com/user-attachments/assets/3f5a73a4-3025-41c1-b8e7-22b00edabd87" />
<img width="270" alt="Screenshot from 2026-05-12 06:55:39" src="https://github.com/user-attachments/assets/0d34eda0-5137-4336-a189-ca29a7542c62" />

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

**Flatpak** — sandboxed, no Python dependencies on the host:
```bash
flatpak install -y flathub org.gnome.Platform//49 org.gnome.Sdk//49
flatpak-builder --user --install --force-clean build-dir io.github.miscde.Muga.yml
flatpak run io.github.miscde.Muga
```
The Flatpak covers desktops and v4l2 webcams. The Halium / gst-droid camera
path (FuriOS, Droidian) needs the Android HAL and sysfs torch nodes, which a
sandbox cannot reach — on those phones use `install.sh` above. See
[docs/compatibility.md](docs/compatibility.md).

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
