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

1. **Two separate domain questions — do not confuse them.**

   *At submission.* Flathub requires that "the author or the developer or the
   project must have control over the domain. The corresponding URL must be
   reachable over HTTPS." For `de.cais.Muga` that is `cais.de`, which redirects
   to `www.cais.de` and answers 200 — reachability is satisfied; control is
   asserted in the pull request. This is the step the old ID could never pass:
   `io.github.miscde.Muga` derived `github.com/miscde`, an unrelated user,
   since this project is `misc-de` and a hyphen cannot appear in a D-Bus name.

   *After acceptance,* for the "verified" badge, Flathub issues a token in the
   developer portal and looks for it in one of two places:

   - a DNS TXT record named `_flathub.cais.de` whose value is that token
     (a UUID like `00000000-aaaa-0000-aaaa-000000000000`); several apps on one
     domain add several TXT values to the same record, or
   - a file at `https://cais.de/.well-known/org.flathub.VerifiedApps.txt`, one
     token per line, `#` starting a comment. HTTPS is required and redirects
     are followed, so the file may live on `www.cais.de`.

   **The token does not exist yet** — Flathub generates it once the app is
   accepted, so neither the record nor the file can be prepared in advance.
   Today `_flathub.cais.de` is unset and the well-known path answers 404,
   which is the expected state.

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
