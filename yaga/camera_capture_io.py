"""Writing a captured frame to disk, with its metadata intact.

Split out of ``CameraWindow``, which had grown past 125 methods. Everything
from "GStreamer handed us a buffer" to "there is a correct JPEG in the user's
photo folder" lives here: orientation, optional downscaling, the JPEG encode,
and the EXIF block (camera model, timestamp, GPS).

Two EXIF backends, in preference order:

* **GExiv2** — a real Exiv2 binding, full tag support. Not present everywhere.
* **Pillow** — always available, and enough for the tags Yaga writes.

Both end up at ``_write_exif_app1_inplace``, which patches the APP1 segment
without re-encoding the pixels: a re-encode would cost a generation of JPEG
quality just to record where the photo was taken.
"""

from __future__ import annotations

import os as _os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

try:
    gi.require_version("GExiv2", "0.10")
    from gi.repository import GExiv2  # type: ignore
    _HAS_GEXIV2 = True
except (ValueError, ImportError):
    GExiv2 = None  # type: ignore
    _HAS_GEXIV2 = False

from .camera_debug import LOGGER, dlog as _dlog
from .camera_orientation import (
    ORIENT_BOTTOM_UP,
    ORIENT_LEFT_UP,
    ORIENT_NORMAL,
    ORIENT_RIGHT_UP,
)

if TYPE_CHECKING:
    from .camera_geo import GeoClient


try:
    gi.require_version("GExiv2", "0.10")
    from gi.repository import GExiv2  # type: ignore
    _HAS_GEXIV2 = True
except (ValueError, ImportError):
    GExiv2 = None  # type: ignore
    _HAS_GEXIV2 = False

def _write_exif_app1_inplace(path: Path, exif_tiff: bytes) -> None:
    """Patch a JPEG's APP1 (EXIF) segment in place — no decode / re-
    encode of the pixel data. `exif_tiff` is the raw TIFF blob that
    PIL.Image.Exif().tobytes() returns (no marker, no length, no
    "Exif\\0\\0" prefix — we add those here).

    JPEG layout we care about::

        SOI            FF D8
        APPn segs      FF Em LL LL .. payload (LL = big-endian length
                                     including LL itself but not Em)
        ...
        SOS, data, EOI

    We rewrite the file as:
        SOI + new APP1 (EXIF) + every original segment EXCEPT the
        existing APP1 segments + the rest of the file from SOS onward.

    Atomic via tmp + os.replace so a crash mid-write doesn't leave a
    truncated photo on disk.
    """
    raw = path.read_bytes()
    if len(raw) < 4 or raw[0] != 0xFF or raw[1] != 0xD8:
        # Not a JPEG (or empty) — nothing to patch.
        return
    # Build the new APP1 (EXIF) segment.
    body = b"Exif\x00\x00" + exif_tiff
    if len(body) > 0xFFFD:
        # APP1 length field is uint16 and includes itself (2 bytes).
        # Anything bigger needs the Extended-EXIF spec we don't support.
        LOGGER.debug("EXIF payload too large (%d bytes); skipping", len(body))
        return
    import struct
    new_app1 = b"\xFF\xE1" + struct.pack(">H", len(body) + 2) + body
    # Walk the existing segments, skipping any existing APP1.
    out = bytearray(b"\xFF\xD8")
    out += new_app1
    i = 2
    while i + 1 < len(raw):
        if raw[i] != 0xFF:
            # Hit pixel data without seeing SOS — broken JPEG, bail and
            # write back original bytes (we already inserted APP1 at
            # the start, which is still a structurally valid file).
            out += raw[i:]
            break
        marker = raw[i + 1]
        if marker == 0xDA:  # SOS — everything from here is image data
            out += raw[i:]
            break
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7,
                      0xD8, 0xD9, 0x01):
            # Standalone markers (no length). RST*, SOI, EOI, TEM.
            # Append and advance 2.
            out += raw[i:i + 2]
            i += 2
            continue
        # Length-prefixed segment.
        if i + 4 > len(raw):
            break
        seg_len = (raw[i + 2] << 8) | raw[i + 3]
        seg_end = i + 2 + seg_len
        if seg_end > len(raw):
            break
        if marker == 0xE1:
            # Existing APP1 — could be EXIF or XMP. Skip EXIF, keep
            # XMP (which uses the "http://ns.adobe.com/xap/1.0/\0"
            # signature, distinguishable from "Exif\0\0").
            seg = raw[i + 4:seg_end]
            if seg.startswith(b"Exif\x00\x00"):
                i = seg_end
                continue
        out += raw[i:seg_end]
        i = seg_end
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(out)
            fh.flush()
            try:
                _os.fsync(fh.fileno())
            except OSError:
                LOGGER.debug("_os.fsync failed", exc_info=True)
        _os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                LOGGER.debug("tmp.unlink failed", exc_info=True)

# Clockwise rotation (degrees) a captured frame needs so the saved photo
# comes out upright. The sensor is fixed in the chassis, so a frame is
# always in *device* coordinates: turning the phone turns the scene the
# other way inside the frame, and we have to turn it back by the same
# angle the viewer applies to display a photo upright (see
# viewer._SENSOR_ROTATION_DEG — same compensation, other direction of
# the same pipeline).
#
# Note the two landscape lays are the mirror of the naive EXIF cookbook
# reading of the orientation names: _classify_orientation inverts the X
# axis to match this HAL's accelerometer mounting (see
# camera_orientation.py), so the string that reaches us is the one that
# keeps the UI layout right, and the pixels need the opposite quarter
# turn from what the name literally suggests. Photos used to be saved
# with the un-mirrored map and came out sideways in landscape.
_CAPTURE_ROTATION_CW = {
    ORIENT_NORMAL:    0,
    ORIENT_BOTTOM_UP: 180,
    ORIENT_LEFT_UP:   270,
    ORIENT_RIGHT_UP:  90,
}

# EXIF Orientation tag describing an *outstanding* clockwise rotation —
# only used as a fallback when the pixels couldn't be rotated (no
# Pillow). 1 = upright, 6 = rotate 90° CW to display, 3 = 180°,
# 8 = 270° CW (i.e. 90° CCW).
_CW_TO_EXIF_ORIENTATION = {0: 1, 90: 6, 180: 3, 270: 8}


class CameraCaptureIOMixin:
    """Frame -> file. Mixed into CameraWindow.

    The block below is the contract with the host class: every name is created
    in ``CameraWindow.__init__`` (or defined on it) and only annotated here.
    """

    _save_dir: Path
    _jpeg_quality: int
    _image_resolution: tuple[int, int] | None
    _capture_orientation: str
    _geo: GeoClient | None
    _on_captured: Any
    _Gst: Any
    # Bound in CameraWindow.__init__ as an attribute, not a method — it holds
    # the gallery's Translator.gettext (or an identity fallback).
    _: Callable[[str], str]

    if TYPE_CHECKING:
        # Provided by CameraWindow; no runtime definition, so these can never
        # shadow the real methods.
        def _show_toast(self, message: str) -> None: ...
        def _current_device(self) -> dict | None: ...

    def _orient_and_resize(self, data: bytes) -> tuple[bytes, int]:
        """Turn the captured JPEG upright and apply the optional
        resolution downscale in a single Pillow pass.

        The rotation goes into the pixels rather than into an EXIF
        Orientation tag, so the photo is right way up everywhere — file
        managers, Nextcloud's web view and the share targets that ignore
        the tag included.

        Returns the (possibly rewritten) bytes plus the EXIF Orientation
        value the caller still has to write: 1 once the rotation sits in
        the pixels, or the tag describing the outstanding rotation if
        Pillow wasn't around to bake it in.

        When there is nothing to do — upright portrait shot, no
        downscale — the original bytes are handed straight back: no
        decode, no re-encode, no generation loss on the common case.
        """
        rotation = _CAPTURE_ROTATION_CW.get(self._capture_orientation, 0)
        target = self._image_resolution
        if rotation == 0 and target is None:
            return data, 1
        try:
            import io

            from PIL import Image as PILImage

            # transpose() below returns a plain Image, not the ImageFile that
            # open() hands back — name the wider type up front.
            src: PILImage.Image = PILImage.open(io.BytesIO(data))
            resized = False
            # Image resolution picker on Halium sets _image_resolution;
            # we keep aspect ratio by fitting inside the target box
            # (thumbnail()), only downscaling (never upscaling).
            if target is not None:
                tw, th = target
                if src.width > tw or src.height > th:
                    src.thumbnail((tw, th), PILImage.Resampling.LANCZOS)
                    resized = True
            if rotation:
                # Quarter turns only, so transpose() just re-indexes
                # pixels — no resampling. The re-encode below is the
                # whole quality cost of baking the rotation in. Pillow's
                # ROTATE_* names are counter-clockwise, hence the
                # 90↔270 swap against our clockwise angles.
                src = src.transpose({
                    90:  PILImage.Transpose.ROTATE_270,
                    180: PILImage.Transpose.ROTATE_180,
                    270: PILImage.Transpose.ROTATE_90,
                }[rotation])
            if not (rotation or resized):
                return data, 1
            quality = max(0, min(100, self._jpeg_quality))
            if rotation and not resized:
                # Rotation is the only reason we're re-encoding here, and
                # the user never asked for that extra generation — keep
                # it well above the preset so straightening a photo
                # doesn't visibly cost quality. A downscale re-encodes
                # anyway, so there the preset is exactly what they asked
                # for and we leave it alone.
                quality = max(quality, 92)
            out = io.BytesIO()
            src.save(out, format="JPEG", quality=quality)
            data = out.getvalue()
            _dlog(
                f"[yaga.camera] capture: {src.width}x{src.height} "
                f"rotation={rotation}deg q={quality} ({len(data)} bytes)"
            )
            return data, 1
        except Exception as exc:
            # Pillow missing or the decode blew up. Keep the native
            # bytes and fall back to the EXIF tag, so the photo is at
            # least upright in the viewers that honour it (Yaga's own
            # gallery does).
            _dlog(f"[yaga.camera] capture: orient/resize failed, keeping "
                f"native ({exc})")
            return data, _CW_TO_EXIF_ORIENTATION.get(rotation, 1)

    def _write_sample(self, sample: Any) -> None:
        buf = sample.get_buffer() if sample is not None else None
        if buf is None:
            _dlog("[yaga.camera] capture: sample has no buffer")
            self._show_toast(self._("No frame available"))
            return
        success, mapinfo = buf.map(self._Gst.MapFlags.READ)
        if not success:
            _dlog("[yaga.camera] capture: buffer.map failed")
            self._show_toast(self._("Could not read frame"))
            return
        try:
            data = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        _dlog(f"[yaga.camera] capture: jpeg bytes={len(data)} save_dir={self._save_dir}")

        data, exif_orientation = self._orient_and_resize(data)

        try:
            self._save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _dlog(f"[yaga.camera] capture: mkdir {self._save_dir} failed: {exc}")
            self._show_toast(self._("Failed to save: %s") % exc)
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        # O_CREAT|O_EXCL atomically creates a new file or fails. Combined
        # with a per-i bump on EEXIST, this closes the TOCTOU window
        # between `path.exists()` and `path.write_bytes()` — on a shared
        # save-dir, another local user could otherwise win the race and
        # have us write into their symlink target.
        path = self._save_dir / f"{stamp}.jpg"
        i = 1
        fd = -1
        for _attempt in range(1000):
            try:
                fd = _os.open(
                    path,
                    _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL,
                    0o644,
                )
                break
            except FileExistsError:
                path = self._save_dir / f"{stamp}_{i}.jpg"
                i += 1
            except OSError as exc:
                _dlog(f"[yaga.camera] capture: open {path} failed: {exc}")
                self._show_toast(self._("Failed to save: %s") % exc)
                return
        if fd < 0:
            self._show_toast(self._("Failed to save: too many collisions"))
            return
        try:
            with _os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            _dlog(f"[yaga.camera] capture: write {path} failed: {exc}")
            self._show_toast(self._("Failed to save: %s") % exc)
            return
        _dlog(f"[yaga.camera] capture: SAVED {path}")

        self._write_exif(path, exif_orientation)
        self._show_toast(self._("Saved %s") % path.name)
        if self._on_captured is not None:
            try:
                self._on_captured(path)
            except Exception:
                LOGGER.debug("on_captured callback failed", exc_info=True)

    def _write_exif(self, path: Path, orientation: int = 1) -> None:
        # Prefer GExiv2 when it's available (proper Exiv2 backend, full
        # tag support). Fall back to Pillow when the GExiv2 GIR isn't
        # installed — covers the basic tags + GPS without requiring the
        # gir1.2-gexiv2-0.10 system package.
        #
        # `orientation` is normally 1: _orient_and_resize has already
        # turned the pixels upright, and a stale tag on top of that
        # would rotate the photo a second time in every viewer that
        # honours it. Anything else means the bake-in failed and the
        # tag is carrying the rotation instead.
        if _HAS_GEXIV2:
            self._write_exif_gexiv2(path, orientation)
        else:
            self._write_exif_pillow(path, orientation)

    def _current_exif_basics(self, orientation: int = 1) -> dict[str, Any]:
        """Common bits used by both EXIF backends."""
        device = self._current_device()
        model = (device.get("name") if device else None) or ""
        if model:
            model = re.sub(r"[^\x20-\x7e]+", " ", model).strip()[:64]
        return {
            "make": "Yaga",
            "model": model,
            "software": "Yaga",
            "now": time.strftime("%Y:%m:%d %H:%M:%S"),
            "orientation": orientation,
        }

    def _write_exif_gexiv2(self, path: Path, orientation: int = 1) -> None:
        basics = self._current_exif_basics(orientation)
        try:
            md = GExiv2.Metadata()  # type: ignore[union-attr]
            md.open_path(str(path))
            md.set_tag_string("Exif.Image.Make", basics["make"])
            if basics["model"]:
                md.set_tag_string("Exif.Image.Model", basics["model"])
            md.set_tag_string("Exif.Image.Software", basics["software"])
            md.set_tag_string("Exif.Image.DateTime", basics["now"])
            md.set_tag_string("Exif.Photo.DateTimeOriginal", basics["now"])
            md.set_tag_string("Exif.Photo.DateTimeDigitized", basics["now"])
            md.set_tag_string(
                "Exif.Image.Orientation", str(basics["orientation"])
            )
            if self._geo is not None:
                location = self._geo.latest()
                if location is not None:
                    try:
                        md.set_gps_info(
                            location["lon"], location["lat"],
                            location.get("alt", 0.0),
                        )
                        md.set_tag_string(
                            "Exif.GPSInfo.GPSProcessingMethod", "GeoClue"
                        )
                    except Exception:
                        LOGGER.debug("set_gps_info failed", exc_info=True)
            md.save_file(str(path))
        except Exception:
            LOGGER.debug("Could not write EXIF (GExiv2) for %s", path, exc_info=True)

    def _write_exif_pillow(self, path: Path, orientation: int = 1) -> None:
        """Pillow-based EXIF writer used when GExiv2 isn't installed.
        Covers Make/Model/Software/DateTime/Orientation, plus GPS when
        the user has the geo toggle on and there's a fresh fix.

        Builds the EXIF blob via PIL.Image.Exif() (which doesn't
        require opening the source JPEG) and patches the file's APP1
        segment in place. Avoids the decode-then-re-encode cycle of
        Image.save("JPEG", quality=...), which lost ~5-10 quality
        points per save and stalled the UI 200-600 ms on a phone."""
        try:
            from PIL.Image import Exif
        except ImportError:
            return
        basics = self._current_exif_basics(orientation)
        try:
            exif = Exif()
            # 0th IFD (image-level metadata).
            exif[0x010F] = basics["make"]           # Make
            if basics["model"]:
                exif[0x0110] = basics["model"]      # Model
            exif[0x0131] = basics["software"]       # Software
            exif[0x0132] = basics["now"]            # DateTime
            exif[0x0112] = int(basics["orientation"])  # Orientation
            # Exif sub-IFD (Photo.* tags in Exiv2 vocabulary).
            exif_ifd = exif.get_ifd(0x8769)
            exif_ifd[0x9003] = basics["now"]        # DateTimeOriginal
            exif_ifd[0x9004] = basics["now"]        # DateTimeDigitized
            # GPS sub-IFD. Guarded separately: exif.tobytes() serialises the
            # whole block at once, so anything the GPS tags upset takes the
            # camera model, capture date and orientation down with it. A photo
            # without a geotag is a far smaller loss than one with no metadata
            # at all — and a JPEG with no EXIF is treated as a plain file
            # rather than an image by some receivers (Delta Chat, for one).
            if self._geo is not None:
                loc = self._geo.latest()
                if loc is not None:
                    try:
                        gps = exif.get_ifd(0x8825)
                        self._pillow_set_gps(gps, loc)
                        exif.tobytes()  # fail here, while GPS is still droppable
                    except Exception:
                        LOGGER.debug("Dropping GPS from EXIF", exc_info=True)
                        exif.get_ifd(0x8825).clear()
            _write_exif_app1_inplace(path, exif.tobytes())
        except Exception:
            LOGGER.debug("Could not write EXIF (Pillow) for %s", path, exc_info=True)

    def _pillow_set_gps(self, gps_ifd: dict, location: dict) -> None:
        lat = location.get("lat")
        lon = location.get("lon")
        if lat is None or lon is None:
            return
        alt = location.get("alt", 0.0) or 0.0

        # RATIONAL tags have to be handed to Pillow as IFDRational. A plain
        # (numerator, denominator) tuple used to survive the encoder, but
        # Pillow 11+ runs every rational through abs() and raises TypeError
        # on a tuple — which, because exif.tobytes() builds the whole block
        # in one go, silently cost the photo its *entire* EXIF, not just the
        # GPS tags.
        from PIL.TiffImagePlugin import IFDRational

        def to_dms(decimal: float) -> tuple:
            # Round in integer ten-thousandths of an arcsecond so a value that
            # rounds up to exactly 60" (or 60') carries into the next minute /
            # degree instead of being written as an out-of-range 60 — EXIF GPS
            # requires 0 <= minutes, seconds < 60.
            total = int(round(decimal * 3600 * 10000))
            d, rem = divmod(total, 3600 * 10000)
            m, rem = divmod(rem, 60 * 10000)
            return (
                IFDRational(d, 1),
                IFDRational(m, 1),
                IFDRational(rem, 10000),
            )

        gps_ifd[0x0000] = b"\x02\x02\x00\x00"        # GPSVersionID 2.2.0.0
        gps_ifd[0x0001] = "N" if lat >= 0 else "S"   # LatitudeRef
        gps_ifd[0x0002] = to_dms(abs(lat))           # Latitude
        gps_ifd[0x0003] = "E" if lon >= 0 else "W"   # LongitudeRef
        gps_ifd[0x0004] = to_dms(abs(lon))           # Longitude
        gps_ifd[0x0005] = 0 if alt >= 0 else 1       # AltitudeRef
        gps_ifd[0x0006] = IFDRational(int(round(abs(alt) * 100)), 100)  # Altitude
