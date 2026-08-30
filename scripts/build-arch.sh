#!/usr/bin/env bash
# Builds Muga for the architecture of the machine it runs on, into ./.repo.
#
# Meant for the aarch64 side: the FuriPhone FLX1 has only the flatpaked
# org.flatpak.Builder, whose build-finish/build-export step fails with
# "fchown: Invalid argument" — the sandbox may not set owners inside $HOME.
# So only the build itself runs inside the builder Flatpak; finishing and
# exporting are done by the system's native flatpak.
#
#   scripts/build-arch.sh [manifest]
#
# Result: ./.repo carrying the ref app/de.cais.Muga/<arch>/master.
#
# Dot-prefixed, like .flatpak-build and .flatpak-builder next to it. This runs
# on a phone, where the checkout sits in the same home directory the file
# manager and the gallery show — a few hundred MB of OSTree objects called
# "repo" is clutter in every folder view on the device. Override with
# FP_REPO=<path> if you want it somewhere else.
set -euo pipefail

manifest="${1:-de.cais.Muga.yml}"
appid="$(basename "$manifest" .yml)"
arch="$(flatpak --default-arch)"
builddir=".flatpak-build/$arch"
repo="${FP_REPO:-.repo}"

if command -v flatpak-builder >/dev/null 2>&1; then
    builder=(flatpak-builder)
    extra=()
else
    builder=(flatpak run org.flatpak.Builder)
    # rofiles-fuse puts an overlay over the tree on which fchown fails with
    # EINVAL — that is what breaks the finishing step inside the builder
    # Flatpak. Without the overlay files are copied instead of hardlinked:
    # slower, but it runs.
    extra=(--disable-rofiles-fuse)
fi

echo "==> Building $appid for $arch"
"${builder[@]}" --force-clean "${extra[@]}" "$builddir" "$manifest"

# Pull finish-args and command out of the manifest (only the lines of the
# finish-args block; comments and blank lines dropped).
mapfile -t finish_args < <(
    awk '
        /^finish-args:/ { inblock = 1; next }
        inblock && /^[a-zA-Z]/ { inblock = 0 }
        inblock && /^[[:space:]]*-[[:space:]]*--/ {
            sub(/^[[:space:]]*-[[:space:]]*/, "")
            sub(/[[:space:]]+#.*$/, "")
            print
        }
    ' "$manifest"
)
command_name="$(awk '/^command:/ { print $2; exit }' "$manifest")"
[[ -n "$command_name" ]] || { echo "No 'command:' found in $manifest." >&2; exit 1; }

# If the builder finished on its own, command= is already in the metadata —
# a second call would abort with "already finalized".
if grep -q '^command=' "$builddir/metadata" 2>/dev/null; then
    echo "==> Build is already finished (command=$command_name)"
else
    echo "==> Finishing the build natively (${#finish_args[@]} finish-args, command=$command_name)"
    flatpak build-finish "$builddir" --command="$command_name" "${finish_args[@]}"
fi

echo "==> Exporting to $repo"
flatpak build-export "$repo" "$builddir" master

echo
echo "Done. $repo now holds:"
find "$repo/refs/heads/app" -type f 2>/dev/null | sed "s|$repo/refs/heads/|  |"

# The export directory used to be ./repo. One left over from then still looks
# like a finished build — copying that one back to the desktop would merge a
# stale architecture into the release without any warning.
if [[ "$repo" != "repo" && -d repo/refs ]]; then
    echo
    echo "Note: an older ./repo is still here from before this script moved to"
    echo "$repo. Copy back $repo, not repo/ — and delete repo/ once you have."
fi
