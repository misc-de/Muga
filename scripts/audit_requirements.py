#!/usr/bin/env python3
"""Print the requirements the CI vulnerability audit should install.

The audit step used to name its packages by hand — ``pip install 'Pillow>=12.3'
defusedxml``. That list is a copy of pyproject.toml's, and a copy drifts: a
dependency added to the project would simply never have been audited, and
nothing would have said so. This derives the list instead, so the audit covers
whatever the project actually declares.

Both dependency sets are included. The build backend is as much a part of what
a release is built from as the runtime dependencies are, and setuptools has a
history of advisories of its own (CVE-2024-6345, CVE-2025-47273,
CVE-2026-59890) — an audit that skips it is looking at half the tree.

Usage (see .github/workflows/ci.yml):

    pip install $(python3 scripts/audit_requirements.py)
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Dependencies that cannot be installed from PyPI on a bare CI runner: both
# build C extensions against system libraries (girepository, cairo) that are
# not present, and both are what the GNOME runtime and the distro packages
# supply anyway — the Flatpak never resolves them through pip at all. Skipped
# here rather than dropped silently: the name is listed so that adding a third
# such dependency is a decision someone makes in this file.
NOT_ON_PYPI = {"pygobject", "pycairo"}


def _name_of(requirement: str) -> str:
    """The bare distribution name from a requirement string, normalised."""
    name = re.split(r"[\[<>=!~;\s]", requirement.strip(), maxsplit=1)[0]
    return name.replace("_", "-").lower()


def requirements(pyproject: Path | None = None) -> list[str]:
    """Every declared dependency that the audit can install, as given."""
    data = tomllib.loads((pyproject or ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = [
        *data.get("build-system", {}).get("requires", []),
        *data.get("project", {}).get("dependencies", []),
    ]
    return [req for req in declared if _name_of(req) not in NOT_ON_PYPI]


def main() -> int:
    print(" ".join(requirements()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
