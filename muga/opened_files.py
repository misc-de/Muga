"""Turn the paths the desktop hands over into items the viewer can show.

A file opened from elsewhere need not be in the index, and need not be under a
folder the gallery knows. It gets a plain item built from the file itself, plus
its neighbours in the same folder so that swiping behaves like it does in the
gallery.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .models import MediaItem, media_type_for

LOGGER = logging.getLogger(__name__)

# Not a category the index knows: writes keyed on it (cached EXIF, a refreshed
# thumbnail) find no row and do nothing, which is right for an unindexed file.
OPENED_CATEGORY = "opened"

# The folder is listed on the main loop and every neighbour costs a stat, so a
# camera folder of 50k photos only contributes the ones around the opened file.
_MAX_NEIGHBOURS = 500

# Where the document portal says a forwarded file really lives.
_HOST_PATH_XATTR = "user.document-portal.host-path"


def _real_path(path: Path) -> Path:
    """Prefer the file's own path to the portal's single-file view of it.

    Flatpak forwards an opened file as /run/user/N/doc/<id>/name, a folder with
    nothing else in it. If the sandbox can reach the original anyway (Pictures,
    say), using that gives swiping its neighbours and keeps the name of the
    folder in the info panel.
    """
    try:
        raw = os.getxattr(path, _HOST_PATH_XATTR)
    except OSError:
        return path
    host = Path(raw.rstrip(b"\0").decode("utf-8", "surrogateescape"))
    return host if os.access(host, os.R_OK) else path


def _item_for(path: Path, media_type: str) -> MediaItem | None:
    try:
        stat = path.stat()
    except OSError:
        LOGGER.debug("Cannot read opened file %s", path, exc_info=True)
        return None
    return MediaItem(
        id=0,
        path=str(path),
        category=OPENED_CATEGORY,
        media_type=media_type,
        folder=str(path.parent),
        name=path.name,
        mtime=stat.st_mtime,
        size=stat.st_size,
    )


def _opened_item(path: Path) -> MediaItem | None:
    # The desktop only sends what we registered for, so a suffix we do not know
    # is still worth a try: the viewer reports a file it cannot decode.
    return _item_for(path, media_type_for(path) or "image")


def _with_neighbours(path: Path) -> tuple[list[MediaItem], int]:
    opened = _opened_item(path)
    if opened is None:
        return [], 0
    try:
        with os.scandir(path.parent) as entries:
            names = sorted(
                (e.name for e in entries
                 if not e.name.startswith(".") and media_type_for(Path(e.name))),
                key=str.casefold,
            )
    except OSError:
        LOGGER.debug("Cannot list the folder of %s", path, exc_info=True)
        return [opened], 0
    if path.name not in names:
        return [opened], 0
    at = names.index(path.name)
    items: list[MediaItem] = []
    index = 0
    for name in names[max(0, at - _MAX_NEIGHBOURS):at + _MAX_NEIGHBOURS + 1]:
        if name == path.name:
            index = len(items)
            items.append(opened)
            continue
        neighbour = path.parent / name
        media_type = media_type_for(neighbour)
        item = _item_for(neighbour, media_type) if media_type else None
        if item is not None:
            items.append(item)
    return items, index


def items_for_paths(paths: list[str]) -> tuple[list[MediaItem], int]:
    """The items to show and which to start on; empty if none could be read.

    One file brings its folder along. Several are taken as the exact set the
    user picked.
    """
    resolved = [_real_path(Path(path)) for path in paths]
    if len(resolved) == 1:
        return _with_neighbours(resolved[0])
    items = [item for item in map(_opened_item, resolved) if item is not None]
    return items, 0
