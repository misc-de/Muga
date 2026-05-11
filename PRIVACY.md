# Privacy Notice

Yaga is a local desktop gallery. It has no accounts, no analytics, no crash
reporting, and no "phone-home" of any kind. This document lists exactly which
data the app touches, where it lives, and how to remove it.

If you spot a behavior that contradicts this note, that is a bug — please file
an issue.

---

## TL;DR

- All your photos, thumbnails, and the search index stay on your machine.
- The app makes network requests **only** to a Nextcloud server **you**
  configured, and **only** while you keep the Nextcloud integration enabled.
- No third-party servers, no telemetry, no advertising IDs, no fingerprinting.

---

## What stays on your machine

Yaga follows the XDG base-dir spec
([yaga/config.py:10-15](yaga/config.py#L10-L15)):

| Path | Contents |
|---|---|
| `~/.config/yaga/settings.json` | UI preferences, last-opened folder, Nextcloud URL and username (plaintext). |
| `~/.config/yaga/nc_password` | Nextcloud app-password, `0600`, **only** used when the system keyring is unavailable (see *Credentials* below). |
| `~/.local/share/yaga/yaga.sqlite3` | Local media index: file paths, sizes, mtimes, image dimensions, date-taken, full-text search index over filenames. No image content. |
| `~/.cache/yaga/thumbnails/` | Generated thumbnail JPEGs of your local photos. |
| `~/.cache/yaga/nextcloud/` | Cached thumbnails (and on-demand downloads) of Nextcloud photos. Cleared when you disconnect. |
| `~/.cache/yaga/debug.log`, `trace.log` | Diagnostic logs (off by default; opt-in via settings). |

Yaga only reads from the folders you point it at. It does not scan your whole
home directory.

---

## What goes over the network

The **only** outbound network traffic is to the Nextcloud instance you
configure yourself:

- WebDAV requests for folder listings, thumbnails, and on-demand file
  downloads ([yaga/nextcloud.py](yaga/nextcloud.py)).
- No request is sent until you complete the connect flow in
  *Settings → Nextcloud* and explicitly enable the integration.
- The Nextcloud integration is gated: once you disable it (or pick "Einmalig"
  in the in-viewer prompt), background fetches stop and the session is not
  re-established without explicit consent
  ([yaga/app.py:1450-1471](yaga/app.py#L1450-L1471),
  [yaga/viewer.py:466-492](yaga/viewer.py#L466-L492)).
- WebDAV XML responses are parsed with `defusedxml` to neutralize XML-bomb /
  external-entity attacks from a hostile or MitM'd server
  ([pyproject.toml:12-14](pyproject.toml#L12-L14)).
- If you enter an `http://` URL, the app warns you that credentials and
  photos would travel unencrypted and requires a second confirmation before
  connecting ([yaga/settings_window.py:410-426](yaga/settings_window.py#L410-L426)).

There is no auto-update check, no usage ping, no error reporting.

---

## Credentials

Yaga never asks for your main Nextcloud password — only for an **app
password** you generate in *Nextcloud → Settings → Security → App passwords*.
You can paste it or scan its QR code with your camera.

Storage ([yaga/config.py:172-248](yaga/config.py#L172-L248)):

1. **Preferred**: system keyring via libsecret (GNOME Keyring, KWallet, …)
   under the schema `de.furilabs.yaga.nextcloud`.
2. **Fallback** when no keyring is available: `~/.config/yaga/nc_password`,
   written atomically with file mode `0600` inside a `0700` directory.

The Nextcloud server URL and username are stored in `settings.json` in
plaintext (they are not secret on their own, but if your home directory is
shared, anyone with read access can see them).

---

## EXIF metadata

Your photos may contain EXIF metadata: camera model, capture time, and
**GPS coordinates**. Yaga reads this metadata to sort and display photos,
and shows it in the *Image Info* panel
([yaga/app.py:2138-2160](yaga/app.py#L2138-L2160)).

**Yaga does not currently strip EXIF data when you export, copy, or share a
photo.** If you upload a photo to a service that preserves metadata, your
location and device may be exposed. Strip EXIF with an external tool
(`exiftool`, GIMP "Export As" with metadata disabled, etc.) before sharing
sensitive shots.

---

## Deletion

Deleting a photo in Yaga moves it to your **system trash** via
`Gio.File.trash()`
([yaga/viewer.py:1189](yaga/viewer.py#L1189),
[yaga/app.py:1716](yaga/app.py#L1716),
[yaga/app.py:1837](yaga/app.py#L1837)) — it is recoverable until you empty
the trash. Yaga does not overwrite or shred files. For secure deletion of
sensitive material, use a dedicated tool or full-disk encryption.

---

## Third parties

Yaga depends on these libraries at runtime (see [pyproject.toml](pyproject.toml)):

- Pillow — image decoding/encoding
- pycairo, PyGObject — GTK 4 / libadwaita bindings
- defusedxml — hardened XML parser for WebDAV responses

None of them call home. None of them are loaded with Yaga-specific
telemetry hooks.

---

## How to wipe everything

```bash
rm -rf ~/.config/yaga ~/.cache/yaga ~/.local/share/yaga
secret-tool clear server "<your-nextcloud-url>" user "<your-username>"  # if keyring was used
```

The `uninstall.sh` script removes the launcher and desktop entry but
intentionally leaves your data alone. Run the commands above to delete it.

---

## Scope and limits of this notice

Yaga is software you run on your own machine. It is not an online service,
so there is no "data controller" in the GDPR sense and no server-side data
to request or erase. This notice documents the local behavior of the app
itself; data your operating system, your Nextcloud server, or other apps
keep about your files is out of scope.
