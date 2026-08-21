#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Yaga — install (no pip / no root required)
#
# Installs:
#   • a launcher script  → ~/.local/bin/yaga
#   • the app icon       → ~/.local/share/icons/hicolor/128x128/apps/
#   • the desktop entry  → ~/.local/share/applications/
#   • AppStream metadata → ~/.local/share/metainfo/
#
# The Python source stays right here in the project directory.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL="${HOME}/.local"

echo "Installing Yaga ..."
echo "  Source:  ${SCRIPT_DIR}"
echo "  Prefix:  ${LOCAL}"
echo ""

# ── Translations ────────────────────────────────────────────────────────────
# Compile po/*.po into the catalogues the app loads. Not fatal if gettext is
# missing: yaga/i18n.py falls back to reading the .po files directly, just a
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
cat > "${LOCAL}/bin/yaga" <<EOF
#!/usr/bin/env bash
exec env PYTHONPATH="${SCRIPT_DIR}" python3 -m yaga "\$@"
EOF
chmod +x "${LOCAL}/bin/yaga"
echo "  ✓ launcher  ${LOCAL}/bin/yaga"

# ── App icon ─────────────────────────────────────────────────────────────────
install -Dm644 \
    "${SCRIPT_DIR}/yaga/data/icons/hicolor/128x128/apps/io.github.miscde.Yaga.png" \
    "${LOCAL}/share/icons/hicolor/128x128/apps/io.github.miscde.Yaga.png"
echo "  ✓ icon      ${LOCAL}/share/icons/hicolor/128x128/apps/io.github.miscde.Yaga.png"

# ── Desktop entry ─────────────────────────────────────────────────────────────
# Write a patched copy that uses the installed launcher path
mkdir -p "${LOCAL}/share/applications"
sed "s|Exec=.*|Exec=${LOCAL}/bin/yaga|" \
    "${SCRIPT_DIR}/data/io.github.miscde.Yaga.desktop" \
    > "${LOCAL}/share/applications/io.github.miscde.Yaga.desktop"
echo "  ✓ desktop   ${LOCAL}/share/applications/io.github.miscde.Yaga.desktop"

# ── AppStream metadata ───────────────────────────────────────────────────────
install -Dm644 \
    "${SCRIPT_DIR}/data/io.github.miscde.Yaga.metainfo.xml" \
    "${LOCAL}/share/metainfo/io.github.miscde.Yaga.metainfo.xml"
echo "  ✓ metainfo  ${LOCAL}/share/metainfo/io.github.miscde.Yaga.metainfo.xml"

# ── Refresh system caches ────────────────────────────────────────────────────
gtk-update-icon-cache -f -t "${LOCAL}/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "${LOCAL}/share/applications" 2>/dev/null || true

echo ""
echo "Done.  Run 'yaga' or launch Yaga from your app menu."
echo "(Make sure ~/.local/bin is in your PATH)"
