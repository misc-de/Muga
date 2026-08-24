#!/usr/bin/env bash
# Puts the OSTree repo into the gh-pages branch, from where GitHub Pages
# serves it at https://misc-de.github.io/Muga/.
#
#   scripts/publish-pages.sh            # build and commit gh-pages locally
#   scripts/publish-pages.sh --push     # and push it
#
# The branch is rewritten on every run (one commit, no history) — an OSTree
# repo is a cache, not a version history. The working tree of the current
# branch is left alone: the branch is assembled in a worktree.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

repo_dir="${FP_REPO:-repo}"
branch="gh-pages"
version="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' muga/__init__.py)"
worktree="$(mktemp -d)"
trap 'git worktree remove --force "$worktree" 2>/dev/null || true; rm -rf "$worktree"' EXIT

[[ -d "$repo_dir/objects" ]] || { echo "No OSTree repo in $repo_dir/ — run 'make flatpak-build' and 'make flatpak-publish' first." >&2; exit 1; }

refs=$(find "$repo_dir/refs/heads/app" -type f 2>/dev/null | sed "s|$repo_dir/refs/heads/||" || true)
[[ -n "$refs" ]] || { echo "The repo holds no app refs." >&2; exit 1; }
echo "Publishing:"; echo "$refs" | sed 's/^/  /'

# An empty worktree for gh-pages (creating the branch if it does not exist).
if git show-ref --verify --quiet "refs/heads/$branch"; then
    git worktree add --force "$worktree" "$branch" >/dev/null
    git -C "$worktree" rm -rq . 2>/dev/null || true
else
    git worktree add --force --detach "$worktree" >/dev/null
    git -C "$worktree" checkout --orphan "$branch" >/dev/null 2>&1
    git -C "$worktree" rm -rq --cached . 2>/dev/null || true
    find "$worktree" -mindepth 1 -maxdepth 1 -not -name .git -exec rm -rf {} +
fi

# Jekyll would swallow directories with a leading underscore.
touch "$worktree/.nojekyll"
cp data/de.cais.Muga.flatpakrepo data/de.cais.Muga.gpg "$worktree/"
cp -r "$repo_dir" "$worktree/repo"

# The metainfo points its <screenshot> images at
# https://misc-de.github.io/Muga/screenshots/ — AppStream needs them reachable
# over HTTPS, and a software centre shows nothing without them.
cp -r data/screenshots "$worktree/screenshots"

cat > "$worktree/index.html" <<'HTML'
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Muga – Flatpak repository</title>
<style>body{font-family:system-ui,sans-serif;max-width:46rem;margin:3rem auto;padding:0 1rem;line-height:1.5}
code,pre{background:#f4f4f4;border-radius:4px}pre{padding:1rem;overflow:auto}code{padding:.1rem .3rem}
@media(prefers-color-scheme:dark){body{background:#1c1c1c;color:#e6e6e6}code,pre{background:#2b2b2b}a{color:#8ab4f8}}</style>
</head><body>
<h1>Muga</h1>
<p>A GTK 4 and libadwaita photo and video gallery for Linux desktops and phones —
with thumbnails, an editor, Nextcloud Photos and a built-in camera.
Signed Flatpak repository for <b>x86_64</b> and <b>aarch64</b>.</p>
<h2>Install</h2>
<pre>flatpak remote-add --if-not-exists muga https://misc-de.github.io/Muga/de.cais.Muga.flatpakrepo
flatpak install muga de.cais.Muga
flatpak run de.cais.Muga</pre>
<p>Updates from then on with <code>flatpak update</code>.</p>
<h2>Renamed from io.github.miscde.Muga</h2>
<p>The app ID is now <code>de.cais.Muga</code>. It briefly shipped as
<code>io.github.miscde.Muga</code>, which named a GitHub account that is not
ours. If you installed it during that window, remove the old one and add the
remote above again — a Flatpak's data lives under its ID, so the gallery
rebuilds its index on first start:</p>
<pre>flatpak uninstall --delete-data io.github.miscde.Muga
flatpak remote-delete muga</pre>
<h2>Cameras</h2>
<p>v4l2 webcams work in the sandbox. The Halium / gst-droid path (FuriOS,
Droidian) does not — it needs the Android HAL and sysfs torch nodes a sandbox
cannot reach. On those phones install from source with <code>install.sh</code>.</p>
<p>Source: <a href="https://github.com/misc-de/Muga">github.com/misc-de/Muga</a>.</p>
</body></html>
HTML

git -C "$worktree" add -A
if git -C "$worktree" diff --cached --quiet; then
    echo "No changes — gh-pages is already up to date."
else
    git -C "$worktree" commit -q -m "Flatpak repository updated ($version)"
    echo "gh-pages committed."
fi

if [[ "${1:-}" == "--push" ]]; then
    git push -f origin "$branch"
    echo "Pushed. In the repository settings GitHub Pages must point at branch gh-pages, folder /."
else
    echo "Not pushed yet. If everything looks right:  git push -f origin $branch"
fi
