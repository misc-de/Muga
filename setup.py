"""Build hook: compile the gettext catalogues into the wheel.

Everything else about this package is declared in pyproject.toml. setup.py
exists only for this one command override, which cannot be expressed
declaratively.

Without it, `pip install .` produces an installation with no catalogues at
all: the .mo files are build artefacts (see .gitignore) and an installed
copy has no po/ directory next to the package for i18n.py to fall back on,
so the app would run in English. install.sh and the Flatpak manifest call
tools/i18n.py directly; this covers every other way in.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


def _compile_catalogues() -> None:
    """Run tools/i18n.py compile, without requiring gettext or an install."""
    tool_path = ROOT / "tools" / "i18n.py"
    if not tool_path.is_file() or not (ROOT / "po").is_dir():
        # Building from a tree without the sources — nothing to compile. The
        # app falls back to po/*.po, or to English if that is absent too.
        print("i18n: no po/ or tools/i18n.py, skipping catalogue compilation")
        return

    spec = importlib.util.spec_from_file_location("_yaga_i18n_tool", tool_path)
    if spec is None or spec.loader is None:      # pragma: no cover - defensive
        print("i18n: could not load tools/i18n.py, skipping")
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.compile_all()


class BuildPyWithCatalogues(build_py):
    """build_py, but compile po/*.po first so package_data can pick the .mo up."""

    def run(self) -> None:
        try:
            _compile_catalogues()
        except Exception as exc:      # noqa: BLE001 - a build must not die here
            # A missing translation is a degraded install, not a broken one:
            # every string falls back to its English source.
            print(f"i18n: catalogue compilation failed ({exc}); "
                  "the install will fall back to English", file=sys.stderr)
        super().run()


setup(cmdclass={"build_py": BuildPyWithCatalogues})
