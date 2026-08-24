"""Translation lookup.

Muga uses standard gettext catalogues (``po/*.po``), so translators can work
with the usual tooling — Poedit, Weblate, ``msgmerge`` — instead of editing a
Python dict. ``tools/i18n.py`` extracts new strings and compiles the
catalogues.

Catalogues are found in one of two places, in order:

1. ``muga/data/locale/<lang>/LC_MESSAGES/muga.mo`` — what an installed copy
   (pip, Flatpak, ``install.sh``) ships.
2. ``po/<lang>.po`` — parsed directly, so a plain ``python3 -m muga`` from a
   fresh checkout is translated without a build step first.

A missing or untranslated string falls back to the msgid, which is the English
text — the same behaviour the old lookup table had.
"""

from __future__ import annotations

import gettext
import locale
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DOMAIN = "muga"
_LOCALE_DIR = Path(__file__).parent / "data" / "locale"
_PO_DIR = Path(__file__).parent.parent / "po"

# The languages offered in Settings. "en" needs no catalogue: its msgids are
# the English strings themselves.
SOURCE_LANGUAGE = "en"


def available_languages() -> list[str]:
    """Languages with a catalogue, plus the source language, sorted."""
    langs = {SOURCE_LANGUAGE}
    if _LOCALE_DIR.is_dir():
        langs.update(
            d.name for d in _LOCALE_DIR.iterdir()
            if (d / "LC_MESSAGES" / f"{DOMAIN}.mo").is_file()
        )
    if _PO_DIR.is_dir():
        langs.update(p.stem for p in _PO_DIR.glob("*.po"))
    return sorted(langs)


# --- .po fallback ------------------------------------------------------------

_PO_ENTRY = re.compile(
    r'^msgid((?:[ \t]*"(?:[^"\\]|\\.)*"[ \t]*\n?)+)'
    r'^msgstr((?:[ \t]*"(?:[^"\\]|\\.)*"[ \t]*\n?)+)',
    re.M,
)


def _po_unquote(chunk: str) -> str:
    """Concatenated PO string literals -> the text they stand for.

    Deliberately not ``unicode_escape``: the chunk is already decoded text, so
    that would decode the non-ASCII characters a second time and turn "—" into
    "â\x80\x94". Chained ``str.replace`` calls are wrong too — unescaping
    ``\\`` after ``\n`` lets a literal backslash-n become a newline. One
    left-to-right pass is the only correct reading.
    """
    joined = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', chunk))
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
    out: list[str] = []
    i = 0
    while i < len(joined):
        char = joined[i]
        if char == "\\" and i + 1 < len(joined):
            nxt = joined[i + 1]
            out.append(escapes.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


class _PoCatalogue(gettext.NullTranslations):
    """Minimal .po reader for running straight from a checkout.

    Only what Muga uses: singular messages, no plural forms, no contexts.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._catalog: dict[str, str] = {}
        text = path.read_text(encoding="utf-8")
        for msgid_chunk, msgstr_chunk in _PO_ENTRY.findall(text):
            msgid = _po_unquote(msgid_chunk)
            msgstr = _po_unquote(msgstr_chunk)
            if msgid and msgstr:
                self._catalog[msgid] = msgstr

    def gettext(self, message: str) -> str:
        return self._catalog.get(message, message)


_CATALOGUES: dict[str, gettext.NullTranslations] = {}


def _catalogue(lang: str) -> gettext.NullTranslations:
    """Return (and cache) the catalogue for *lang*, never raising."""
    cached = _CATALOGUES.get(lang)
    if cached is not None:
        return cached

    cat: gettext.NullTranslations = gettext.NullTranslations()
    if lang != SOURCE_LANGUAGE:
        try:
            cat = gettext.translation(DOMAIN, localedir=str(_LOCALE_DIR), languages=[lang])
        except OSError:
            po = _PO_DIR / f"{lang}.po"
            if po.is_file():
                try:
                    cat = _PoCatalogue(po)
                except Exception:
                    LOGGER.debug("could not read %s", po, exc_info=True)
            else:
                LOGGER.debug("no catalogue for language %r", lang)
    _CATALOGUES[lang] = cat
    return cat


def system_language() -> str:
    """The system's language if Muga has a catalogue for it, else English."""
    try:
        code = (locale.getdefaultlocale()[0] or "")[:2].lower()
    except (ValueError, TypeError):
        code = ""
    return code if code in available_languages() else SOURCE_LANGUAGE


@dataclass
class Translator:
    # Mirrors Settings.language: English unless a translation is asked for.
    # "system" remains a valid value and still resolves via system_language().
    language: str = SOURCE_LANGUAGE
    _cached_lang: str | None = field(default=None, init=False, repr=False, compare=False)
    _cached_active: str = field(default=SOURCE_LANGUAGE, init=False, repr=False, compare=False)

    @property
    def active_language(self) -> str:
        lang = self.language
        if lang != self._cached_lang:
            if lang == "system":
                self._cached_active = system_language()
            else:
                self._cached_active = lang if lang in available_languages() else SOURCE_LANGUAGE
            self._cached_lang = lang
        return self._cached_active

    def gettext(self, text: str) -> str:
        return _catalogue(self.active_language).gettext(text)
