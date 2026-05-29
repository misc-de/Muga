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

import yaga

_PKG_ROOT = Path(yaga.__file__).parent


def _iter_module_names() -> list[str]:
    return [
        name
        for _finder, name, _ispkg in pkgutil.walk_packages(
            yaga.__path__, prefix="yaga."
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
