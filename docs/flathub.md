# Submitting Muga to Flathub

Muga already ships two Flatpak manifests:

| File | Purpose |
| --- | --- |
| `io.github.miscde.Muga.yml` | the repository build — source is the working tree, exported to `repo/` and published on gh-pages |
| `io.github.miscde.Muga.flathub.yml` | the Flathub build — source is a tagged git state |

Runtime, modules and permissions are identical between the two on purpose, so
what Flathub builds rests on the same base as what the project's own repository
serves.

## Before opening the pull request

Flathub's CI runs `flatpak-builder-lint`; run the same checks locally first:

```bash
flatpak install -y --user flathub org.flatpak.Builder
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
    manifest io.github.miscde.Muga.flathub.yml
```

And build once from the tagged state the manifest names — not from the working
tree — so the submission is tested as Flathub will build it:

```bash
flatpak-builder --force-clean --install --user \
    .flatpak-build/flathub io.github.miscde.Muga.flathub.yml
```

## Open points

These are what still stands between the manifest and a submission:

1. **The app ID claims a namespace the project does not own.** This is the
   blocking one. `flatpak-builder-lint` reports `appid-url-not-reachable`:
   from `io.github.miscde.Muga` it derives `https://github.com/miscde/muga`,
   which is a 404. The GitHub account behind the project is **`misc-de`**, with
   a hyphen; `github.com/miscde` is a *different, unrelated user*. For an
   `io.github.*` ID Flathub requires that the namespace is yours, so this ID
   cannot be submitted as it stands.

   The correct form encodes the hyphen as an underscore: **`io.github.misc_de.Muga`**
   (`-` is not allowed in a D-Bus name, and Flathub converts `_` back to `-`
   when it checks). Renaming again touches the same surface the Yaga → Muga
   rename did — desktop entry, metainfo, icon filenames, both manifests, the
   OSTree refs and the `.flatpakrepo` — so it is a deliberate decision, not a
   detail to slip in.

   Nothing forces it for the project's own repository: `io.github.miscde.Muga`
   works perfectly well there, and the Flatpaks currently published under it
   are fine. It only blocks Flathub.

2. **Screenshots are missing from the metainfo.** Flathub requires at least one
   `<screenshot>` in `data/io.github.miscde.Muga.metainfo.xml`, served over
   HTTPS from a stable address. The shots in the README are GitHub
   `user-attachments` URLs that were uploaded to the then-private Yaga
   repository and now answer 404 — they cannot be reused. Take fresh ones,
   commit them under `data/screenshots/`, and reference them at
   `https://misc-de.github.io/Muga/screenshots/<name>.png`; `publish-pages.sh`
   has to copy that directory into the branch as well. One shot must carry
   `<caption>`, and Flathub wants a desktop-shaped one (`type="default"`)
   alongside the phone-shaped ones.

3. **The `v0.3.1` tag does not exist yet.** The manifest names it; Flathub
   builds only from published states. Tag the release and push the tag, then
   pin `commit:` to its SHA.

4. **`--device=all` needs a rationale in the pull request.** The linter flags
   it. Muga uses direct v4l2 control ioctls for focus and exposure, which the
   portal-mediated `--device=camera` does not cover — that is the argument to
   make, and it is already a comment in the manifest.

5. **The Halium / gst-droid camera path does not work under Flathub** any more
   than it does in the project's own Flatpak: it needs the Android HAL and the
   sysfs torch nodes, which no sandbox reaches. That is a property of the
   sandbox, not of the submission, and `docs/compatibility.md` documents it.

## The submission itself

1. Fork `github.com/flathub/flathub` and create a branch named exactly
   `io.github.miscde.Muga`.
2. Add `io.github.miscde.Muga.flathub.yml` to it under the name
   `io.github.miscde.Muga.yml` — Flathub expects the manifest to be named after
   the app ID.
3. Open the pull request against the `new-pr` branch.
4. A reviewer picks it up; the permissions above are what they will ask about.

Once it is accepted, Flathub builds the app itself, and the project's own
repository at `https://misc-de.github.io/Muga/` can stay as the faster channel
for aarch64 phone builds.
