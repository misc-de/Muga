# Submitting Muga to Flathub

Muga already ships two Flatpak manifests:

| File | Purpose |
| --- | --- |
| `de.cais.Muga.yml` | the repository build — source is the working tree, exported to `repo/` and published on gh-pages |
| `de.cais.Muga.flathub.yml` | the Flathub build — source is a tagged git state |

Runtime, modules and permissions are identical between the two on purpose, so
what Flathub builds rests on the same base as what the project's own repository
serves.

## Before opening the pull request

Flathub's CI runs `flatpak-builder-lint`; run the same checks locally first:

```bash
flatpak install -y --user flathub org.flatpak.Builder
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
    manifest de.cais.Muga.flathub.yml
```

And build once from the tagged state the manifest names — not from the working
tree — so the submission is tested as Flathub will build it:

```bash
flatpak-builder --force-clean --install --user \
    .flatpak-build/flathub de.cais.Muga.flathub.yml
```

## Open points

These are what still stands between the manifest and a submission:

1. **A domain check is what Flathub will ask for.** The app ID is
   `de.cais.Muga`, so Flathub has to see that `cais.de` is ours — usually a
   DNS TXT record or a file served from the domain, agreed with the reviewer
   in the pull request. This replaced `io.github.miscde.Muga`, which derived
   `github.com/miscde` — an unrelated user, since this project is `misc-de`.
   `flatpak-builder-lint` rejected that outright; the domain form also keeps
   the ID valid if the code ever moves off GitHub, and matches the sibling
   apps (`de.cais.Schwupp`, `de.cais.Emilia`).

2. **Two screenshots still show the old name.** The metainfo carries all eight
   shots, served from `https://misc-de.github.io/Muga/screenshots/`, and
   `appstreamcli validate` passes. But `overview.jpg` and `date-grouping.jpg`
   were taken before the rename and show "Yaga" in the header bar. Flathub will
   not reject them, yet they are the first thing a visitor sees. Retake those
   two on the phone and overwrite the files — no other change is needed, the
   names and captions stay.

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
   `de.cais.Muga`.
2. Add `de.cais.Muga.flathub.yml` to it under the name
   `de.cais.Muga.yml` — Flathub expects the manifest to be named after
   the app ID.
3. Open the pull request against the `new-pr` branch.
4. A reviewer picks it up; the permissions above are what they will ask about.

Once it is accepted, Flathub builds the app itself, and the project's own
repository at `https://misc-de.github.io/Muga/` can stay as the faster channel
for aarch64 phone builds.
