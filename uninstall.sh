#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Muga — uninstall
# ---------------------------------------------------------------------------

LOCAL="${HOME}/.local"

echo "Uninstalling Muga ..."

rm -f "${LOCAL}/bin/muga"
echo "  ✓ removed launcher"

rm -f "${LOCAL}/share/icons/hicolor/128x128/apps/io.github.miscde.Muga.png"
echo "  ✓ removed icon"

rm -f "${LOCAL}/share/applications/io.github.miscde.Muga.desktop"
echo "  ✓ removed desktop entry"

rm -f "${LOCAL}/share/metainfo/io.github.miscde.Muga.metainfo.xml"
echo "  ✓ removed metainfo"

gtk-update-icon-cache -f -t "${LOCAL}/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "${LOCAL}/share/applications" 2>/dev/null || true

echo ""
echo "Done."
