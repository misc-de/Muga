#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Muga — install (no pip / no root required)
#
# Installs:
#   • a launcher script  → ~/.local/bin/muga
#   • the app icon       → ~/.local/share/icons/hicolor/128x128/apps/
#   • the desktop entry  → ~/.local/share/applications/
#   • AppStream metadata → ~/.local/share/metainfo/
#
# The Python source stays right here in the project directory.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL="${HOME}/.local"

echo "Installing Muga ..."
echo "  Source:  ${SCRIPT_DIR}"
echo "  Prefix:  ${LOCAL}"
echo ""

# ── Translations ────────────────────────────────────────────────────────────
# Compile po/*.po into the catalogues the app loads. Not fatal if gettext is
# missing: muga/i18n.py falls back to reading the .po files directly, just a
# little slower at startup.
if command -v msgfmt >/dev/null 2>&1; then
    python3 "${SCRIPT_DIR}/tools/i18n.py" compile >/dev/null \
        && echo "  ✓ locales   compiled" \
        || echo "  ! locales   compile failed — falling back to po/*.po at runtime"
else
    echo "  ! locales   msgfmt not found — falling back to po/*.po at runtime"
fi

# ── Launcher script ─────────────────────────────────────────────────────────
mkdir -p "${LOCAL}/bin"
cat > "${LOCAL}/bin/muga" <<EOF
#!/usr/bin/env bash
exec env PYTHONPATH="${SCRIPT_DIR}" python3 -m muga "\$@"
EOF
chmod +x "${LOCAL}/bin/muga"
echo "  ✓ launcher  ${LOCAL}/bin/muga"

# ── App icon ─────────────────────────────────────────────────────────────────
install -Dm644 \
    "${SCRIPT_DIR}/muga/data/icons/hicolor/128x128/apps/io.github.miscde.Muga.png" \
    "${LOCAL}/share/icons/hicolor/128x128/apps/io.github.miscde.Muga.png"
echo "  ✓ icon      ${LOCAL}/share/icons/hicolor/128x128/apps/io.github.miscde.Muga.png"

# ── Desktop entry ─────────────────────────────────────────────────────────────
# Write a patched copy that uses the installed launcher path
mkdir -p "${LOCAL}/share/applications"
sed "s|Exec=.*|Exec=${LOCAL}/bin/muga|" \
    "${SCRIPT_DIR}/data/io.github.miscde.Muga.desktop" \
    > "${LOCAL}/share/applications/io.github.miscde.Muga.desktop"
echo "  ✓ desktop   ${LOCAL}/share/applications/io.github.miscde.Muga.desktop"

# ── AppStream metadata ───────────────────────────────────────────────────────
install -Dm644 \
    "${SCRIPT_DIR}/data/io.github.miscde.Muga.metainfo.xml" \
    "${LOCAL}/share/metainfo/io.github.miscde.Muga.metainfo.xml"
echo "  ✓ metainfo  ${LOCAL}/share/metainfo/io.github.miscde.Muga.metainfo.xml"

# ── Refresh system caches ────────────────────────────────────────────────────
gtk-update-icon-cache -f -t "${LOCAL}/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "${LOCAL}/share/applications" 2>/dev/null || true

echo ""
echo "Done.  Run 'muga' or launch Muga from your app menu."
echo "(Make sure ~/.local/bin is in your PATH)"
