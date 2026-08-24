# Flatpak build and release tasks for Muga.
#
# The short version, from a clean tree to a published repository:
#
#   make flatpak-gpg-key                  # once: create the signing key
#   make flatpak-build FP_GPG=... FP_GPGHOME=...
#   scripts/build-arch.sh on the phone, copy its repo/ back
#   make flatpak-merge ARM_REPO=<path>
#   make flatpak-publish FP_GPG=... FP_GPGHOME=...
#   make flatpak-pages PUSH=1
#
# Everything about the app itself — tests, linting, installing from source —
# lives in pyproject.toml and install.sh; this file is only about packaging.

APPID       = de.cais.Muga
FP_MANIFEST = $(APPID).yml
VERSION     = $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' muga/__init__.py)

FP_REPO     ?= repo
FP_ARCH      = $(shell flatpak --default-arch)
FP_BUILDDIR ?= .flatpak-build/$(FP_ARCH)
FP_GPG      ?=
FP_GPGHOME  ?=
FP_GPGARGS   = $(if $(FP_GPG),--gpg-sign=$(FP_GPG) $(if $(FP_GPGHOME),--gpg-homedir=$(FP_GPGHOME)),)
# flatpak-builder as a host tool, otherwise the flatpaked org.flatpak.Builder.
FP_BUILDER   = $(shell command -v flatpak-builder >/dev/null 2>&1 \
		&& echo flatpak-builder || echo flatpak run org.flatpak.Builder)

.PHONY: help flatpak-build flatpak-install flatpak-merge flatpak-publish \
	flatpak-pages flatpak-repo-info flatpak-gpg-key flatpak-check

help:
	@echo "Muga $(VERSION) — packaging targets"
	@echo
	@echo "  flatpak-build      build this machine's architecture into $(FP_REPO)/"
	@echo "  flatpak-install    build and install for the current user (to try it out)"
	@echo "  flatpak-merge      pull in a repo built elsewhere (ARM_REPO=<path>)"
	@echo "  flatpak-publish    sign every architecture, write summary and deltas"
	@echo "  flatpak-pages      write repo + landing page to gh-pages (PUSH=1 to push)"
	@echo "  flatpak-repo-info  show which app refs are in the repo"
	@echo "  flatpak-gpg-key    create the project's own signing key"
	@echo "  flatpak-check      run appstream and desktop-file validation"

# Builds the current host architecture into $(FP_REPO).
flatpak-build:
	$(FP_BUILDER) --force-clean --repo=$(FP_REPO) $(FP_GPGARGS) \
		$(FP_BUILDDIR) $(FP_MANIFEST)
	@echo "$(FP_ARCH) is now in $(FP_REPO)/. Refs: make flatpak-repo-info"

# Builds and installs straight into the user installation (to try it out).
flatpak-install:
	$(FP_BUILDER) --force-clean --user --install $(FP_BUILDDIR) $(FP_MANIFEST)
	@echo "Run it with: flatpak run $(APPID)"

# Merges in a repo built on another architecture (ARM_REPO=<path>, the repo/
# copied off the phone).
flatpak-merge:
	@test -n "$(ARM_REPO)" || { echo "Pass ARM_REPO=<path> (the repo/ copied off the phone)"; exit 1; }
	ostree --repo=$(FP_REPO) pull-local $(ARM_REPO)
	@echo "Merged. Now: make flatpak-publish"

# Signs every commit in the repo, then writes summary, AppStream data and
# static deltas.
#
# Note: without --arch, `flatpak build-sign` signs only this machine's
# architecture. The aarch64 commit built on the phone would stay unsigned —
# and because `remote-info` only checks the (signed) summary, that surfaces
# first on the actual install: "GPG verification enabled, but no signatures
# found". Hence the loop over every architecture in the repo.
flatpak-publish:
ifneq ($(FP_GPG),)
	@for arch in $$(find $(FP_REPO)/refs/heads/app/$(APPID) -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null); do \
		echo "Signing $(APPID) ($$arch)"; \
		flatpak build-sign $(FP_REPO) $(APPID) --arch=$$arch $(FP_GPGARGS) || exit 1; \
	done
endif
	@# Drop existing deltas: build-update-repo only regenerates missing ones.
	@# A delta built before signing carries the unsigned commit inside it — the
	@# install then fails with "no signatures found" even though the signature
	@# is in the repo (invisible without --no-static-deltas).
	rm -rf $(FP_REPO)/deltas $(FP_REPO)/delta-indexes
	flatpak build-update-repo --generate-static-deltas --prune $(FP_GPGARGS) $(FP_REPO)
	@echo "$(FP_REPO)/ is ready to host (serve it over HTTPS)."

# Writes repo + .flatpakrepo + landing page into the gh-pages branch, from
# where GitHub Pages serves them. Only pushes with `make flatpak-pages PUSH=1`.
flatpak-pages:
	scripts/publish-pages.sh $(if $(PUSH),--push,)

# Shows which app refs (architectures) are currently in the repo.
flatpak-repo-info:
	@ostree --repo=$(FP_REPO) refs 2>/dev/null | grep -E "^app/" | sort \
		|| echo "(no $(FP_REPO) built yet)"

# What Flathub's CI checks. The metainfo and desktop entry validate with host
# tools; the manifest needs flatpak-builder-lint, which ships inside
# org.flatpak.Builder (flatpak install -y --user flathub org.flatpak.Builder).
#
# Known findings on the Flathub manifest, both explained in docs/flathub.md:
# appid-url-not-reachable (the ID names a GitHub account that is not ours) and
# appid-filename-mismatch (expected — the file is renamed on submission).
flatpak-check:
	appstreamcli validate --explain data/$(APPID).metainfo.xml || true
	desktop-file-validate data/$(APPID).desktop || true
	@flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
		manifest $(APPID).flathub.yml || true

# Creates the project's own signing key in a project-local GnuPG directory
# (not in the personal keyring) and writes it into the .flatpakrepo.
GPGHOME = $(HOME)/.local/share/muga-flatpak
flatpak-gpg-key:
	@test ! -d $(GPGHOME) || { echo "$(GPGHOME) already exists — nothing to do."; exit 0; }
	mkdir -p $(GPGHOME) && chmod 700 $(GPGHOME)
	gpg --homedir $(GPGHOME) --batch --passphrase '' --quick-generate-key \
		"Muga Flatpak Repo <flatpak@cais.de>" ed25519 sign never
	@echo
	@echo "The key is in $(GPGHOME). Build and sign with:"
	@echo "  make flatpak-build FP_GPG=\$$(gpg --homedir $(GPGHOME) --list-keys --with-colons | awk -F: '/^fpr/{print \$$10; exit}') FP_GPGHOME=$(GPGHOME)"
