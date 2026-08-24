"""Regression guards for bug classes that previously shipped on main.

These are deliberately cheap, static checks tied to concrete past defects:

  * A stray ASCII quote left an f-string unterminated in app.py, so the whole
    module failed to import and the app wouldn't start at all — yet the test
    suite stayed green because nothing imported every module.
  * The i18n tables defined several keys twice with *different* values; the
    later duplicate silently won, so edits to the "dead" entry did nothing.
"""
from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import muga


_PKG_ROOT = Path(muga.__file__).parent


def _iter_module_names() -> list[str]:
    return [
        name
        for _finder, name, _ispkg in pkgutil.walk_packages(
            muga.__path__, prefix="muga."
        )
    ]


def test_every_module_imports() -> None:
    """Importing every module catches syntax errors / broken imports at HEAD.

    Without this, a parse error in a module no test happens to import (as once
    happened in app.py) sails through CI and breaks startup in the field."""
    failures: list[str] = []
    for name in _iter_module_names():
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - we want to report any failure
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "modules failed to import:\n" + "\n".join(failures)


def _duplicate_dict_keys(source: str) -> list[tuple[int, str]]:
    """Return (lineno, key) for every repeated constant string key in any dict
    literal in *source*. Repeats are silently collapsed by Python at runtime,
    so they can only be caught at the source level."""
    dups: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        seen: set[str] = set()
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value in seen:
                    dups.append((key.lineno, key.value))
                seen.add(key.value)
    return dups


def test_no_duplicate_dict_keys_in_package() -> None:
    """No dict literal in the package may repeat a string key. This is the
    bug class that silently broke three German editor translations."""
    offenders: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        for lineno, key in _duplicate_dict_keys(path.read_text(encoding="utf-8")):
            rel = path.relative_to(_PKG_ROOT.parent)
            offenders.append(f"{rel}:{lineno}: duplicate key {key!r}")
    assert not offenders, "duplicate dict keys found:\n" + "\n".join(offenders)


def _po_entries(text: str) -> dict[str, str]:
    """msgid -> msgstr for a .po/.pot file, header entry excluded.

    Unescaping goes through the app's own _po_unquote, so these tests compare
    the same strings the running app compares — and exercise that parser on
    every catalogue as a side effect.
    """
    import re

    from muga.i18n import _po_unquote

    pairs = re.findall(
        r'^msgid((?:[ \t]*"(?:[^"\\]|\\.)*"[ \t]*\n?)+)'
        r'^msgstr((?:[ \t]*"(?:[^"\\]|\\.)*"[ \t]*\n?)+)', text, re.M)
    out = {}
    for mid, mstr in pairs:
        key = _po_unquote(mid)
        if key:
            out[key] = _po_unquote(mstr)
    return out


def test_catalogues_cover_exactly_the_template() -> None:
    """Every po/*.po must carry the same msgids as po/muga.pot.

    A missing msgid leaks the English source string into a localised UI; a
    stray one is dead weight, and usually means a real translation was lost
    when the template was regenerated. `tools/i18n.py update` keeps them in
    step — this is the guard that says whether someone forgot to run it.
    """
    po_dir = Path(__file__).resolve().parent.parent / "po"
    template = set(_po_entries((po_dir / "muga.pot").read_text(encoding="utf-8")))
    assert template, "po/muga.pot is empty — run tools/i18n.py extract"

    problems: list[str] = []
    for po in sorted(po_dir.glob("*.po")):
        entries = set(_po_entries(po.read_text(encoding="utf-8")))
        missing, extra = template - entries, entries - template
        if missing:
            problems.append(f"{po.name} missing {len(missing)}: {sorted(missing)[:5]}")
        if extra:
            problems.append(f"{po.name} has {len(extra)} unknown: {sorted(extra)[:5]}")
    assert not problems, "catalogues out of sync with the template:\n" + "\n".join(problems)


def test_translations_keep_their_format_placeholders() -> None:
    """A translated string must take the same %-arguments as its source.

    This is the one translation bug that crashes rather than looks wrong:
    "%d items" translated without the %d raises at format time, inside
    whatever UI callback happened to build the label.
    """
    import re
    po_dir = Path(__file__).resolve().parent.parent / "po"
    spec = re.compile(r"%(?:\((\w+)\))?[-#0 +]*\d*(?:\.\d+)?([diouxXeEfFgGcrsa%])")

    problems: list[str] = []
    for po in sorted(po_dir.glob("*.po")):
        for msgid, msgstr in _po_entries(po.read_text(encoding="utf-8")).items():
            if not msgstr:
                continue          # untranslated falls back to the msgid
            want = sorted(m for m in spec.findall(msgid) if m[1] != "%")
            got = sorted(m for m in spec.findall(msgstr) if m[1] != "%")
            if want != got:
                problems.append(f"{po.name}: {msgid!r} takes {want}, translation takes {got}")
    assert not problems, "placeholder mismatch:\n" + "\n".join(problems)


def test_every_catalogue_language_is_offered_and_loadable() -> None:
    """A shipped catalogue the user cannot select is invisible work."""
    from muga.i18n import SOURCE_LANGUAGE, Translator, available_languages

    langs = available_languages()
    assert SOURCE_LANGUAGE in langs
    for lang in langs:
        # Must not raise, and must return a str for an unknown key.
        assert Translator(lang).gettext("__definitely not a real msgid__") == \
            "__definitely not a real msgid__"


def test_po_unquote_handles_escapes_and_non_ascii() -> None:
    """The .po fallback parser must not decode text a second time.

    Two ways to get this wrong, both of which happened while the catalogues
    were first generated:

      * ``unicode_escape`` on an already-decoded string turns "—" into
        "â\\x80\\x94", so every msgid carrying a dash, arrow or ellipsis stops
        matching what the code passes to ``_()`` — silently untranslated.
      * Chained ``str.replace`` calls unescape ``\\\\`` last, so a literal
        backslash-n arrives as a newline.
    """
    from muga.i18n import _po_unquote

    assert _po_unquote('"ab"') == "ab"
    assert _po_unquote('"a\\nb"') == "a\nb"          # \n is a newline
    assert _po_unquote('"a\\\\nb"') == "a\\nb"       # \\n is backslash + n
    assert _po_unquote('"x\\"y"') == 'x"y'
    assert _po_unquote('"—"') == "—"
    assert _po_unquote('"→ ✓ …"') == "→ ✓ …"
    assert _po_unquote('"a"\n"b"') == "ab"           # continuation lines


def test_catalogues_carry_no_double_encoded_text() -> None:
    """A msgid that went through a bad decode never matches the source string.

    "â" plus a control byte is the fingerprint of UTF-8 read as Latin-1; it
    cannot occur in Muga's real UI strings, so its presence means a catalogue
    was written by a broken tool.
    """
    po_dir = Path(__file__).resolve().parent.parent / "po"
    suspects: list[str] = []
    for cat in sorted(list(po_dir.glob("*.po")) + list(po_dir.glob("*.pot"))):
        text = cat.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "â\x80" in line or "Ã¤" in line or "Ã¼" in line or "Ã¶" in line:
                suspects.append(f"{cat.name}: {line[:80]}")
    assert not suspects, "double-encoded text in catalogues:\n" + "\n".join(suspects)


def test_every_literal_passed_to_translate_is_in_the_template() -> None:
    """A string literal handed to _() must exist in po/muga.pot.

    Without this the failure is invisible: an untemplated string simply shows
    up in English, in an otherwise German UI, and nobody notices until a user
    reports it. Catching it here means `tools/i18n.py extract` gets run as
    part of the change that introduced the string.

    Only literals are checked — `self._(label)` with a variable cannot be
    resolved statically, and those msgids are carried in the template with a
    "referenced indirectly" comment instead.
    """
    root = Path(__file__).resolve().parent.parent
    template = set(_po_entries((root / "po" / "muga.pot").read_text(encoding="utf-8")))

    missing: list[str] = []
    for path in sorted((root / "muga").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            is_translate = (
                (isinstance(fn, ast.Name) and fn.id == "_")
                or (isinstance(fn, ast.Attribute) and fn.attr == "_")
            )
            if not is_translate or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value and arg.value not in template:
                    missing.append(f"{path.relative_to(root)}:{arg.lineno}: {arg.value!r}")
    assert not missing, (
        "strings passed to _() but absent from po/muga.pot — run "
        "`tools/i18n.py extract && tools/i18n.py update`:\n" + "\n".join(missing)
    )


def test_the_builtin_mo_writer_matches_msgfmt() -> None:
    """`pip install .` compiles catalogues, and cannot assume gettext is there.

    Most build environments have no msgfmt, so tools/i18n.py carries its own
    MO writer. It has one job: produce a file gettext reads back the same way
    it reads msgfmt's. Compared entry by entry against the real thing —
    skipped where msgfmt is unavailable, since then there is nothing to
    compare against.
    """
    import gettext
    import importlib.util
    import shutil
    import subprocess
    import tempfile

    if shutil.which("msgfmt") is None:
        pytest.skip("msgfmt not installed — nothing to compare against")

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("_i18n_tool", root / "tools" / "i18n.py")
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    for po in sorted((root / "po").glob("*.po")):
        catalogue = tool._parse_po(po.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            ours, theirs = Path(tmp) / "ours.mo", Path(tmp) / "theirs.mo"
            tool._write_mo(catalogue, ours)
            subprocess.run(["msgfmt", "-o", str(theirs), str(po)], check=True)

            with open(ours, "rb") as fh:
                mine = gettext.GNUTranslations(fh)
            with open(theirs, "rb") as fh:
                reference = gettext.GNUTranslations(fh)

            translated = [k for k, v in catalogue.items() if k and v]
            assert translated, f"{po.name} has no translations to check"
            differing = [k for k in translated if mine.gettext(k) != reference.gettext(k)]
            assert not differing, f"{po.name}: {len(differing)} entries differ, e.g. {differing[:3]}"


def test_only_the_muga_package_is_installed() -> None:
    """setuptools' auto-discovery must not treat every top-level dir as a package.

    Without an include filter, `pip install .` put tests/, data/, tools/, po/
    and any stale build/ into site-packages as importable top-level names —
    so `import tests` from an unrelated project would have found this one's
    suite.
    """
    import tomllib

    root = Path(__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as fh:
        config = tomllib.load(fh)
    find = config["tool"]["setuptools"]["packages"]["find"]
    assert find.get("include") == ["muga*"], (
        "packages.find must be restricted to the muga package; "
        f"got {find.get('include')!r}"
    )
