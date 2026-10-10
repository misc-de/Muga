# Credits

Muga builds on these open-source projects, services and assets. Thanks to
everyone who builds and maintains them. Each keeps its own licence; Muga itself
is released under the [MIT License](LICENSE).

## Libraries & frameworks

| Project | Used for | Licence |
|---|---|---|
| [GTK 4](https://www.gtk.org/) · [libadwaita](https://gitlab.gnome.org/GNOME/libadwaita) | user interface | LGPL-2.1+ |
| [PyGObject](https://pygobject.gnome.org/) | Python bindings for GTK/GLib | LGPL-2.1+ |
| [Pango](https://gitlab.gnome.org/GNOME/pango) · [GdkPixbuf](https://gitlab.gnome.org/GNOME/gdk-pixbuf) · [Graphene](https://github.com/ebassi/graphene) | text, image loading, transforms | LGPL-2.1+ / MIT |
| [cairo](https://www.cairographics.org/) · [pycairo](https://github.com/pygobject/pycairo) | editor drawing (text, stickers, frames) | LGPL-2.1 / MPL-1.1 |
| [Pillow](https://python-pillow.org/) | image decoding, editing and filters | MIT-CMU |
| [defusedxml](https://github.com/tiran/defusedxml) | hardened WebDAV XML parsing | PSF-2.0 |
| [GStreamer](https://gstreamer.freedesktop.org/) | camera, video recording, QR scanning | LGPL-2.1+ |
| [ZXing-C++](https://github.com/zxing-cpp/zxing-cpp) (GStreamer `zxing` element) | QR-code decoding | Apache-2.0 |
| [gst-droid](https://github.com/sailfishos/gst-droid) (`droidcamsrc`) | camera on Halium phones (FuriOS, Droidian) | LGPL-2.1+ |
| [PipeWire](https://pipewire.org/) | camera access on desktop | MIT |
| [GExiv2](https://gitlab.gnome.org/GNOME/gexiv2) · [Exiv2](https://exiv2.org/) | writing EXIF/GPS metadata to photos | GPL-2.0+ |
| [libsecret](https://gitlab.gnome.org/GNOME/libsecret) | storing the Nextcloud app password | LGPL-2.1+ |
| [rawpy](https://github.com/letmaik/rawpy) · [LibRaw](https://www.libraw.org/) (optional) | RAW thumbnails | MIT / LGPL-2.1 or CDDL-1.0 |

## System services

| Project | Used for | Licence |
|---|---|---|
| [GeoClue](https://gitlab.freedesktop.org/geoclue/geoclue) | opt-in photo geotagging | GPL-2.0+ |
| [iio-sensor-proxy](https://gitlab.freedesktop.org/hadess/iio-sensor-proxy) | device orientation on desktop Linux | GPL-3.0 |
| [sensorfw](https://github.com/sailfishos/sensorfw) (`sensord`) | device orientation on Halium phones | LGPL-2.1 |

## External tools

| Project | Used for | Licence |
|---|---|---|
| [FFmpeg](https://ffmpeg.org/) · [ffmpegthumbnailer](https://github.com/dirkvdb/ffmpegthumbnailer) | video thumbnails | LGPL-2.1+ / GPL-2.0+ |
| [v4l-utils](https://git.linuxtv.org/v4l-utils.git) (`v4l2-ctl`) | V4L2 camera controls | GPL-2.0 |
| [xdg-utils](https://www.freedesktop.org/wiki/Software/xdg-utils/) | opening files and sharing by e-mail | MIT |

## Services & protocols

- [Nextcloud](https://nextcloud.com/) — Nextcloud Photos integration via WebDAV.
- [Model Context Protocol](https://modelcontextprotocol.io/) — specification behind the built-in MCP server.

## Code & assets

- The device-orientation reader follows the sensord pattern from
  [Sensor-Suite](https://github.com/misc-de/Sensor-Suite).
- `xsi-view-more-symbolic.svg` is taken unchanged from
  [xapp-symbolic-icons](https://github.com/xapp-project/xapp-symbolic-icons)
  (Linux Mint / XApp project, LGPL-3.0-only).
- The Nextcloud logo (`nextcloud-symbolic.svg`, `nc-icon-*.png`) is a trademark
  of Nextcloud GmbH, used only to mark the Nextcloud integration. Muga is not
  affiliated with or endorsed by Nextcloud.
