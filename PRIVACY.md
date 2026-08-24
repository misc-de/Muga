# Privacy Notice

Muga is a local desktop gallery. It has no accounts, no analytics, no crash
reporting, and no "phone-home" of any kind. This document lists exactly which
data the app touches, where it lives, and how to remove it.

If you spot a behavior that contradicts this note, that is a bug — please file
an issue.

---

## TL;DR

- All your photos, thumbnails, and the search index stay on your machine.
- The app makes network requests **only** to a Nextcloud server **you**
  configured, and **only** while you keep the Nextcloud integration enabled.
- The optional MCP server is the one thing that *accepts* connections. It is
  off by default, listens on this device only unless you widen it, refuses to
  start without a token you issued, and can only read the index — never your
  files, never anything outside it.
- No third-party servers, no telemetry, no advertising IDs, no fingerprinting.

---

## What stays on your machine

Muga follows the XDG base-dir spec
([muga/config.py:10-15](muga/config.py#L10-L15)):

| Path | Contents |
|---|---|
| `~/.config/muga/settings.json` | UI preferences, last-opened folder, Nextcloud URL and username (plaintext). |
| `~/.config/muga/nc_password` | Nextcloud app-password, `0600`, **only** used when the system keyring is unavailable (see *Credentials* below). |
| `~/.local/share/muga/muga.sqlite3` | Local media index: file paths, sizes, mtimes, image dimensions, date-taken, full-text search index over filenames. No image content. |
| `~/.cache/muga/thumbnails/` | Generated thumbnail JPEGs of your local photos. |
| `~/.cache/muga/nextcloud/` | Cached thumbnails (and on-demand downloads) of Nextcloud photos. Cleared when you disconnect. |
| `~/.cache/muga/debug.log`, `trace.log` | Diagnostic logs (off by default; opt-in via settings). |
| `~/.config/muga/mcp_tokens.json` | MCP access tokens, `0600`. Only present once you create one in *Settings → MCP*. |

Muga only reads from the folders you point it at. It does not scan your whole
home directory.

---

## What goes over the network

The **only** outbound network traffic is to the Nextcloud instance you
configure yourself:

- WebDAV requests for folder listings, thumbnails, and on-demand file
  downloads ([muga/nextcloud.py](muga/nextcloud.py)).
- No request is sent until you complete the connect flow in
  *Settings → Nextcloud* and explicitly enable the integration.
- The Nextcloud integration is gated: once you disable it (or pick "Einmalig"
  in the in-viewer prompt), background fetches stop and the session is not
  re-established without explicit consent
  ([muga/app.py:1450-1471](muga/app.py#L1450-L1471),
  [muga/viewer.py:466-492](muga/viewer.py#L466-L492)).
- WebDAV XML responses are parsed with `defusedxml` to neutralize XML-bomb /
  external-entity attacks from a hostile or MitM'd server
  ([pyproject.toml:12-14](pyproject.toml#L12-L14)).
- If you enter an `http://` URL, the app warns you that credentials and
  photos would travel unencrypted and requires a second confirmation before
  connecting ([muga/settings_window.py:410-426](muga/settings_window.py#L410-L426)).

There is no auto-update check, no usage ping, no error reporting.

---

## What comes in over the network

Muga accepts exactly one kind of inbound connection, and only when you switch
it on: the **MCP server** in *Settings → MCP*
([muga/mcp_server.py](muga/mcp_server.py)).

- **Off by default.** No socket is opened until you enable it.
- **Loopback by default.** *Reachable from* decides how far it listens: this
  device only (`127.0.0.1`), your local network, a public address, or every
  interface. Anything beyond the first is a deliberate choice you make in the
  combo — turning the server on does not make it reachable from the network.
  A saved scope whose interface has gone away falls back to loopback, never
  to something wider.
- **No token, no server.** It will not start until you have created an access
  token — a client authenticates with one, and a socket that refuses every
  request is only a way to be wrong later.
- **Read-only.** Every tool it exposes is a database SELECT: category listings,
  search, folder listings, per-file metadata, totals. There is no tool that
  writes, moves, deletes, or reads file *contents* — an MCP client gets paths
  and metadata, not your photos.
- **What a client can see:** file paths, names, folders, sizes, modification
  times, and the EXIF block the scanner extracted — the same data as the local
  index. Nextcloud items only appear if the gallery itself is showing them.
- **Tokens are revocable.** Deleting one in *Settings → MCP* takes effect on
  the next request, with no restart
  ([muga/mcp_tokens.py](muga/mcp_tokens.py)).
- **Plain HTTP, no TLS.** On loopback that is moot. On any wider scope, anyone
  able to watch that network sees the metadata and the bearer token in transit
  — keep it to networks you trust, and do not expose the port to the internet
  or forward it through a router.

---

## Credentials

Muga never asks for your main Nextcloud password — only for an **app
password** you generate in *Nextcloud → Settings → Security → App passwords*.
You can paste it or scan its QR code with your camera.

Storage ([muga/config.py:172-248](muga/config.py#L172-L248)):

1. **Preferred**: system keyring via libsecret (GNOME Keyring, KWallet, …)
   under the schema `de.furilabs.muga.nextcloud`.
2. **Fallback** when no keyring is available: `~/.config/muga/nc_password`,
   written atomically with file mode `0600` inside a `0700` directory.

The Nextcloud server URL and username are stored in `settings.json` in
plaintext (they are not secret on their own, but if your home directory is
shared, anyone with read access can see them).

---

## EXIF metadata

Your photos may contain EXIF metadata: camera model, capture time, and
**GPS coordinates**. Muga reads this metadata to sort and display photos,
and shows it in the *Image Info* panel
([muga/app.py:2138-2160](muga/app.py#L2138-L2160)).

**Muga does not currently strip EXIF data when you export, copy, or share a
photo.** If you upload a photo to a service that preserves metadata, your
location and device may be exposed. Strip EXIF with an external tool
(`exiftool`, GIMP "Export As" with metadata disabled, etc.) before sharing
sensitive shots.

---

## Deletion

Deleting a photo in Muga moves it to your **system trash** via
`Gio.File.trash()`
([muga/viewer.py:1189](muga/viewer.py#L1189),
[muga/app.py:1716](muga/app.py#L1716),
[muga/app.py:1837](muga/app.py#L1837)) — it is recoverable until you empty
the trash. Muga does not overwrite or shred files. For secure deletion of
sensitive material, use a dedicated tool or full-disk encryption.

---

## Third parties

Muga depends on these libraries at runtime (see [pyproject.toml](pyproject.toml)):

- Pillow — image decoding/encoding
- pycairo, PyGObject — GTK 4 / libadwaita bindings
- defusedxml — hardened XML parser for WebDAV responses

None of them call home. None of them are loaded with Muga-specific
telemetry hooks.

---

## How to wipe everything

```bash
rm -rf ~/.config/muga ~/.cache/muga ~/.local/share/muga
secret-tool clear server "<your-nextcloud-url>" user "<your-username>"  # if keyring was used
```

The `uninstall.sh` script removes the launcher and desktop entry but
intentionally leaves your data alone. Run the commands above to delete it.

---

## Scope and limits of this notice

Muga is software you run on your own machine. It is not an online service,
so there is no "data controller" in the GDPR sense and no server-side data
to request or erase. This notice documents the local behavior of the app
itself; data your operating system, your Nextcloud server, or other apps
keep about your files is out of scope.
