#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Yaga — uninstall
# ---------------------------------------------------------------------------

LOCAL="${HOME}/.local"

echo "Uninstalling Yaga ..."

rm -f "${LOCAL}/bin/yaga"
echo "  ✓ removed launcher"

rm -f "${LOCAL}/share/icons/hicolor/128x128/apps/io.github.miscde.Yaga.png"
echo "  ✓ removed icon"

rm -f "${LOCAL}/share/applications/io.github.miscde.Yaga.desktop"
echo "  ✓ removed desktop entry"

rm -f "${LOCAL}/share/metainfo/io.github.miscde.Yaga.metainfo.xml"
echo "  ✓ removed metainfo"

gtk-update-icon-cache -f -t "${LOCAL}/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "${LOCAL}/share/applications" 2>/dev/null || true

echo ""
echo "Done."
