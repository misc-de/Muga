# Yaga — Photo Gallery

A fast, clean photo and video gallery for Linux desktops, built with GTK 4 and libadwaita.

![Yaga](yaga.png)

---

## Screenshots
<img width="300" alt="Screenshot from 2026-05-09 17:26:52" src="https://github.com/user-attachments/assets/0fc0b6bc-4d4f-43f4-816c-42ae1efdb2da" />
<img width="300" alt="Screenshot from 2026-05-09 17:46:18" src="https://github.com/user-attachments/assets/ed12b04a-d63c-4dd5-bef7-7900b62255dc" />
<img width="300" alt="Screenshot from 2026-05-09 17:32:20" src="https://github.com/user-attachments/assets/e09fe9e7-311e-4802-88e3-2221d0b8ae04" />
<img width="300" alt="Screenshot from 2026-05-09 17:32:29" src="https://github.com/user-attachments/assets/484a1886-1b38-454e-a593-53b10e31f3a1" />
<img width="300" alt="Screenshot from 2026-05-09 17:32:36" src="https://github.com/user-attachments/assets/0c34fd96-ef2c-4655-abc0-79b93a00bd5d" />

---

## What is Yaga?

Yaga is a gallery app that feels at home on a modern GNOME desktop. It scans your media folders, keeps everything snappy with a thumbnail cache and a SQLite index, and stays out of your way.

---

## Highlights

- **Multiple libraries** — separate tabs for Photos, Pictures, Videos, Screenshots, and any extra folders you add
- **Nextcloud sync** — browse your Nextcloud photo library directly, no FUSE or GVFS mount needed; thumbnails load on demand
- **Date grouping** — sort by date and photos are grouped under clear section headers (day / week / month / year)
- **Built-in editor** — crop, rotate, adjust brightness / contrast / colour channels, add frames for holidays and occasions, drop stickers
- **QR code scanner** — scan Nextcloud app-password QR codes straight from the camera to connect your account instantly
- **Video playback** — watch videos directly in the app or hand them off to any external player
- **Pull-to-refresh** — scroll past the top to kick off a re-scan, just like on a phone
- **Selection mode** — long-press any photo to enter multi-select, then delete or move a whole batch at once
- **Folder view** — drill into subfolders; folder tiles show a 2×2 preview mosaic
- **Share & open externally** — send a photo by e-mail or open it in any other app with one tap
- **Light / dark / system theme** — follows your desktop or lets you override it
- **English & German UI** — switches at runtime without restarting

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
