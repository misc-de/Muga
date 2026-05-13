# Yaga — Photo Gallery & Camera

A fast, clean photo and video gallery for Linux desktops and Linux phones (Phosh / FuriOS), built with GTK 4 and libadwaita.  
  
⚠️ **AI-assisted project**  
  
![Yaga](yaga.png)

---

## What is Yaga?

Yaga is a gallery app that feels right at home on a modern GNOME desktop and adapts to Linux phones running Phosh (FuriOS, Droidian, UBports). It scans your media folders, ensures consistently smooth performance thanks to a thumbnail cache and an SQLite index, and stays out of your way while doing so. It now also includes a built-in **Camera** that captures photos and video — on phones running Halium / gst-droid the camera taps directly into the hardware (flash, torch, sensors). In addition to several editing features, it allows you to effortlessly integrate your Nextcloud Photos.

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
  - flash for photos and continuous torch (LED light) for video, persistent across sessions
  - optional geotagging via GeoClue2, with EXIF written in place — no JPEG re-encode
  - accelerometer-driven orientation; chrome rotates without rotating the framebuffer
  - self-timer (3 / 10 s), pinch-to-zoom, tap-to-focus, JPEG quality presets, video bitrate presets
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
Then launch **Yaga** from your app menu, or type `yaga` in a terminal.

**Run directly without installing:**
```bash
python3 -m yaga
```

**Uninstall:**
```bash
bash uninstall.sh
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
