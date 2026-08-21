#!/usr/bin/env python3
"""Translation maintenance for Yaga.

    tools/i18n.py extract     rebuild po/yaga.pot from the sources
    tools/i18n.py update      merge the .pot into every po/*.po
    tools/i18n.py compile     build yaga/data/locale/<lang>/LC_MESSAGES/yaga.mo
    tools/i18n.py stat        show translation coverage per language

`compile` is what an install needs; the app falls back to reading po/*.po
directly when no .mo is present, so a checkout works without running it.

Needs gettext (xgettext, msgmerge, msgfmt) for everything except `stat`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PO_DIR = ROOT / "po"
LOCALE_DIR = ROOT / "yaga" / "data" / "locale"
POT = PO_DIR / "yaga.pot"
DOMAIN = "yaga"


def _sources() -> list[str]:
    return sorted(str(p.relative_to(ROOT)) for p in (ROOT / "yaga").rglob("*.py"))


def extract() -> int:
    PO_DIR.mkdir(exist_ok=True)
    subprocess.run(
        ["xgettext", "--language=Python", "--keyword=_", "--from-code=UTF-8",
         "--package-name=Yaga", "--sort-output",
         "--msgid-bugs-address=https://github.com/misc-de/Yaga/issues",
         "-o", str(POT), *_sources()],
        cwd=ROOT, check=True,
    )
    # Strings that only ever reach _() through a variable — month names, frame
    # themes, sort labels. xgettext cannot see them, but they are translated,
    # so they have to survive the round trip.
    known = set(_msgids(POT.read_text(encoding="utf-8")))
    extra = sorted(s for s in _indirect() if s not in known)
    if extra:
        with POT.open("a", encoding="utf-8") as fh:
            for s in extra:
                fh.write("\n#. Referenced indirectly — passed to _() as a variable.\n")
                fh.write(f"msgid {_quote(s)}\nmsgstr \"\"\n")
    print(f"{POT.relative_to(ROOT)}: {len(known) + len(extra)} messages "
          f"({len(extra)} referenced indirectly)")
    return 0


def _indirect() -> set[str]:
    """Msgids that exist only in an existing catalogue, not in the sources."""
    out: set[str] = set()
    for po in PO_DIR.glob("*.po"):
        out.update(_msgids(po.read_text(encoding="utf-8")))
    return out


def _msgids(text: str) -> list[str]:
    ids = re.findall(r'^msgid((?:[ \t]*"(?:[^"\\]|\\.)*"[ \t]*\n?)+)', text, re.M)
    return [m for m in ("".join(re.findall(r'"((?:[^"\\]|\\.)*)"', c)) for c in ids) if m]


def _quote(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


def update() -> int:
    for po in sorted(PO_DIR.glob("*.po")):
        subprocess.run(["msgmerge", "--update", "--backup=none", "--quiet",
                        str(po), str(POT)], check=True)
        print(f"merged {po.relative_to(ROOT)}")
    return 0


def compile_all() -> int:
    for po in sorted(PO_DIR.glob("*.po")):
        target = LOCALE_DIR / po.stem / "LC_MESSAGES" / f"{DOMAIN}.mo"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["msgfmt", "--check-format", "-o", str(target), str(po)], check=True)
        print(f"{target.relative_to(ROOT)}")
    return 0


def stat() -> int:
    for po in sorted(PO_DIR.glob("*.po")):
        text = po.read_text(encoding="utf-8")
        pairs = re.findall(
            r'^msgid((?:[ \t]*"(?:[^"\\]|\\.)*"[ \t]*\n?)+)'
            r'^msgstr((?:[ \t]*"(?:[^"\\]|\\.)*"[ \t]*\n?)+)', text, re.M)
        total = done = 0
        for mid, mstr in pairs:
            if not "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', mid)):
                continue          # header
            total += 1
            if "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', mstr)):
                done += 1
        pct = (done / total * 100) if total else 0.0
        print(f"{po.stem:6} {done:4}/{total:<4} {pct:5.1f}%")
    return 0


COMMANDS = {"extract": extract, "update": update, "compile": compile_all, "stat": stat}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]]())
