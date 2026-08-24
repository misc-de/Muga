"""In-app update checker and installer for Yaga (git pull or zip download).

Mirrors the DrivePulse updater: when Yaga runs from a git checkout (the
default — install.sh keeps the source in place and launches it with
``python -m yaga``), an update is a ``git pull``. When it runs from a plain
copy (no ``.git``), it downloads the branch zip from GitHub and overlays it.

User data lives in XDG dirs, not in the source tree, so the zip overlay only
has to skip ``.git``. The SQLite schema migrates itself via PRAGMA
user_version on next launch, so there's no separate migration step.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import NamedTuple

from . import VERSION

LOGGER = logging.getLogger(__name__)

# Repo root: yaga/updater.py → yaga/ → <repo>. This is where .git lives and
# where the zip overlay is applied.
_APP_DIR = Path(__file__).resolve().parent.parent
_GITHUB_REPO = "misc-de/Yaga"
_DEFAULT_BRANCH = "main"
# The canonical version string lives in yaga/__init__.py; we read it from the
# remote (raw / git show) and parse VERSION out of it.
_RAW_INIT_URL = f"https://raw.githubusercontent.com/{_GITHUB_REPO}/{{branch}}/yaga/__init__.py"
_ZIP_URL = f"https://github.com/{_GITHUB_REPO}/archive/refs/heads/{{branch}}.zip"
_USER_AGENT = f"Yaga/{VERSION}"
_VERSION_RE = re.compile(r"""VERSION\s*=\s*['"]([^'"]+)['"]""")

def is_flatpak() -> bool:
    """True when Yaga runs inside a Flatpak sandbox.

    Neither update strategy can work there: /app is mounted read-only, so
    there is no checkout to ``git pull`` and the zip overlay would have to
    write into site-packages. A Flatpak is updated through the host instead
    (``flatpak update`` or a software centre, which reads the releases in
    data/io.github.miscde.Yaga.metainfo.xml).

    Both signals are set by flatpak itself; either alone is enough.
    """
    return os.path.exists("/.flatpak-info") or bool(os.environ.get("FLATPAK_ID"))


# Never overwrite these during a zip overlay.
_ZIP_SKIP = {".git"}
# Sanity bound on the unpacked archive. The source tree is a few MB; anything
# near this is a decompression bomb or the wrong URL, not a Yaga release.
_MAX_UNPACKED_BYTES = 250 * 1024 * 1024


class UpdateInfo(NamedTuple):
    available: bool
    remote_version: str | None  # None when no update or unknown


def get_current_version() -> str:
    return VERSION


def _is_git_repo() -> bool:
    return (_APP_DIR / ".git").exists()


def _parse_version(text: str | None) -> str | None:
    if not text:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple:
    """Comparable form of a dotted version string. Non-numeric trailing parts
    (``1.2.0rc1``) sort before the plain release, which is the conventional
    reading and good enough for an "is the remote newer" check."""
    parts: list = []
    for chunk in version.strip().split("."):
        digits = re.match(r"(\d+)(.*)", chunk)
        if digits:
            parts.append((int(digits.group(1)), digits.group(2) or "~"))
        else:
            parts.append((-1, chunk))
    return tuple(parts)


def _is_newer(remote: str | None, local: str) -> bool:
    """True only when *remote* is strictly newer than *local*.

    The previous check was ``remote != local``, which also fired when the
    remote was *older* — a branch rolled back on GitHub would have been
    offered (and installed) as an "update", silently downgrading the app.
    """
    if not remote:
        return False
    try:
        return _version_tuple(remote) > _version_tuple(local)
    except Exception:
        # Unparseable version: fall back to "different means newer" rather
        # than never offering an update again.
        return remote != local


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def _git(*args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=_APP_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return r.returncode, (r.stdout.strip() or r.stderr.strip())
    except Exception as exc:
        LOGGER.debug("git %s: %s", args, exc)
        return -1, ""


def _current_branch() -> str:
    code, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if code == 0 and branch and branch != "HEAD":
        return branch
    return _DEFAULT_BRANCH


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — Yaga has no requests dependency)
# ---------------------------------------------------------------------------

def _http_get_text(url: str, timeout: int = 15) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https GitHub URL
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:
        LOGGER.debug("HTTP GET %s: %s", url, exc)
        return None


def _http_download(url: str, dest: Path, timeout: int = 120) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:  # noqa: S310
            shutil.copyfileobj(resp, fh)
        return True
    except Exception as exc:
        LOGGER.error("Download %s failed: %s", url, exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_for_update() -> UpdateInfo:
    """Return whether a newer version is available (does network I/O — call
    off the UI thread).

    Reports "no update" inside a Flatpak: offering one there would only lead
    to an apply that cannot succeed. See `is_flatpak`.
    """
    if is_flatpak():
        return UpdateInfo(False, None)
    if _is_git_repo():
        return _check_git()
    return _check_zip()


def apply_update() -> bool:
    """Download and apply the update (does network/disk I/O — call off the UI
    thread). Returns True on success; the caller should then restart.

    Refuses inside a Flatpak rather than failing halfway through a write to a
    read-only /app. See `is_flatpak`.
    """
    if is_flatpak():
        LOGGER.info("Running as a Flatpak — updates come from the host, not from here")
        return False
    if _is_git_repo():
        return _apply_git()
    return _apply_zip()


# ---------------------------------------------------------------------------
# git strategy
# ---------------------------------------------------------------------------

def _check_git() -> UpdateInfo:
    branch = _current_branch()
    _git("fetch", "--quiet", timeout=30)
    code, count_str = _git("rev-list", f"HEAD..origin/{branch}", "--count")
    if code != 0:
        return UpdateInfo(False, None)
    try:
        behind = int(count_str) > 0
    except ValueError:
        return UpdateInfo(False, None)
    if not behind:
        return UpdateInfo(False, None)
    _, init_text = _git("show", f"origin/{branch}:yaga/__init__.py")
    return UpdateInfo(True, _parse_version(init_text))


def _apply_git() -> bool:
    # --ff-only so we never create a merge commit on a user's checkout; if the
    # working tree has diverged or has local commits, the pull fails cleanly
    # and the UI reports an error instead of leaving a half-merged tree.
    code, out = _git("pull", "--ff-only", "--quiet", timeout=120)
    if code != 0:
        LOGGER.error("git pull failed: %s", out)
        return False
    return True


# ---------------------------------------------------------------------------
# zip strategy
# ---------------------------------------------------------------------------

def _check_zip() -> UpdateInfo:
    remote_ver = _parse_version(_http_get_text(_RAW_INIT_URL.format(branch=_DEFAULT_BRANCH)))
    if not remote_ver:
        return UpdateInfo(False, None)
    if not _is_newer(remote_ver, VERSION):
        return UpdateInfo(False, None)
    return UpdateInfo(True, remote_ver)


def _apply_zip() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "yaga.zip"
        LOGGER.info("Downloading update zip…")
        if not _http_download(_ZIP_URL.format(branch=_DEFAULT_BRANCH), zip_path):
            return False

        extract_dir = Path(tmp) / "extracted"
        extract_dir.mkdir()
        try:
            with zipfile.ZipFile(zip_path) as zf:
                # A zip that expands to far more than a source checkout could
                # ever be is not an update — refuse it before it fills the
                # user's disk. (Yaga's own tree is a couple of MB.)
                total = sum(info.file_size for info in zf.infolist())
                if total > _MAX_UNPACKED_BYTES:
                    LOGGER.error(
                        "Refusing update archive: unpacks to %.0f MB (limit %.0f MB)",
                        total / 1e6, _MAX_UNPACKED_BYTES / 1e6,
                    )
                    return False
                zf.extractall(extract_dir)
        except Exception as exc:
            LOGGER.error("ZIP extraction failed: %s", exc)
            return False

        # GitHub extracts to a single subdirectory (e.g. Yaga-main).
        subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if len(subdirs) != 1:
            LOGGER.error("Unexpected ZIP structure: %s", subdirs)
            return False

        # Overlaying happens file by file over the *running* installation, so a
        # failure partway through (disk full, a read-only file) would leave a
        # tree that is half 0.2.0 and half 0.3.0 — an app that may not even
        # import. Back up every file we are about to replace and put it back if
        # anything goes wrong.
        backup = Path(tmp) / "backup"
        backup.mkdir()
        try:
            _copy_update(subdirs[0], _APP_DIR, backup)
        except Exception as exc:
            LOGGER.error("Update failed midway (%s) — rolling back", exc)
            try:
                _restore_backup(backup, _APP_DIR)
                LOGGER.info("Rollback complete; the installation is unchanged.")
            except Exception:
                LOGGER.exception("Rollback FAILED — installation may be inconsistent")
            return False
        return True


def _copy_update(src: Path, dst: Path, backup: Path | None = None) -> None:
    """Recursively overlay *src* onto *dst*, skipping entries in _ZIP_SKIP.
    Only copies files present in the zip — local-only files (caches, the user's
    data dirs live outside the tree anyway) are left untouched.

    When *backup* is given, every file that is about to be overwritten is
    copied there first (mirroring the relative layout), so _restore_backup can
    undo a partial overlay.
    """
    for item in src.iterdir():
        if item.name in _ZIP_SKIP:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _copy_update(item, target, (backup / item.name) if backup else None)
        else:
            if backup is not None and target.exists():
                backup.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / item.name)
            shutil.copy2(item, target)


def _restore_backup(backup: Path, dst: Path) -> None:
    """Copy every file saved under *backup* back over *dst*."""
    if not backup.exists():
        return
    for item in backup.iterdir():
        target = dst / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _restore_backup(item, target)
        else:
            shutil.copy2(item, target)
