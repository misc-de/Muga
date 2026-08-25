# Flatpak build and release tasks for Muga.
#
# The short version, from a clean tree to a published repository:
#
#   make flatpak-gpg-key                  # once: create the signing key
#   make flatpak-build FP_GPG=... FP_GPGHOME=...
#   make flatpak-build FP_ARCH=aarch64 FP_GPG=... FP_GPGHOME=...
#   make flatpak-publish FP_GPG=... FP_GPGHOME=...
#   make flatpak-pages PUSH=1
#
# The aarch64 build runs here under qemu — `make flatpak-cross-setup` says what
# that needs. Building on the phone instead still works and is the fallback
# when the emulated build is not trusted for a release:
#
#   scripts/build-arch.sh on the phone, copy its .repo/ back
#   make flatpak-merge ARM_REPO=<path>
#
# Everything about the app itself — tests, linting, installing from source —
# lives in pyproject.toml and install.sh; this file is only about packaging.

APPID       = de.cais.Muga
FP_MANIFEST = $(APPID).yml
VERSION     = $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' muga/__init__.py)
# Read out of the manifest rather than written here as well: this used to be
# hard-coded in three places in flatpak-cross-setup, so a runtime bump told
# people to install one branch while the build asked for another.
FP_RUNTIME  = $(shell sed -n "s/^runtime-version: *'\(.*\)'/\1/p" $(FP_MANIFEST))

FP_REPO     ?= repo
# The architecture to build. Defaults to this machine's; set FP_ARCH=aarch64 to
# cross-build under qemu — see flatpak-cross-setup for what that needs.
FP_ARCH     ?= $(shell flatpak --default-arch)
FP_BUILDDIR ?= .flatpak-build/$(FP_ARCH)
FP_GPG      ?=
FP_GPGHOME  ?=
FP_GPGARGS   = $(if $(FP_GPG),--gpg-sign=$(FP_GPG) $(if $(FP_GPGHOME),--gpg-homedir=$(FP_GPGHOME)),)
# flatpak-builder as a host tool, otherwise the flatpaked org.flatpak.Builder.
FP_BUILDER   = $(shell command -v flatpak-builder >/dev/null 2>&1 \
		&& echo flatpak-builder || echo flatpak run org.flatpak.Builder)

.PHONY: help flatpak-build flatpak-install flatpak-merge flatpak-publish \
	flatpak-pages flatpak-repo-info flatpak-gpg-key flatpak-check \
	flatpak-cross-setup

help:
	@echo "Muga $(VERSION) — packaging targets"
	@echo
	@echo "  flatpak-build      build $(FP_ARCH) into $(FP_REPO)/ (FP_ARCH=aarch64 to cross-build)"
	@echo "  flatpak-install    build and install for the current user (to try it out)"
	@echo "  flatpak-merge      pull in a repo built elsewhere (ARM_REPO=<path>)"
	@echo "  flatpak-publish    sign every architecture, write summary and deltas"
	@echo "  flatpak-pages      write repo + landing page to gh-pages (PUSH=1 to push)"
	@echo "  flatpak-repo-info  show which app refs are in the repo"
	@echo "  flatpak-gpg-key    create the project's own signing key"
	@echo "  flatpak-check      run appstream and desktop-file validation"
	@echo "  flatpak-cross-setup  check what a cross-build for FP_ARCH still needs"

# Builds $(FP_ARCH) — this machine's architecture unless overridden — into
# $(FP_REPO). Passing --arch explicitly is what makes FP_ARCH=aarch64 build
# aarch64 rather than just naming the build directory after it.
flatpak-build:
	$(FP_BUILDER) --force-clean --arch=$(FP_ARCH) --repo=$(FP_REPO) $(FP_GPGARGS) \
		$(FP_BUILDDIR) $(FP_MANIFEST)
	@echo "$(FP_ARCH) is now in $(FP_REPO)/. Refs: make flatpak-repo-info"

# Builds and installs straight into the user installation (to try it out).
flatpak-install:
	$(FP_BUILDER) --force-clean --user --install $(FP_BUILDDIR) $(FP_MANIFEST)
	@echo "Run it with: flatpak run $(APPID)"

# Merges in a repo built on another architecture (ARM_REPO=<path>, the .repo/
# copied off the phone — build-arch.sh writes there, dot-prefixed, so it stays
# out of the folder views on the device).
flatpak-merge:
	@test -n "$(ARM_REPO)" || { echo "Pass ARM_REPO=<path> (the .repo/ copied off the phone)"; exit 1; }
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

# Checks whether this machine can cross-build for FP_ARCH, and says what is
# missing if not. The aarch64 side used to mean building on the phone
# (scripts/build-arch.sh); with qemu-user registered in binfmt_misc it can be
# built here instead.
#
# The F flag on the binfmt registration is the part that matters: it pins the
# interpreter into the kernel, so it is still reachable inside the build
# sandbox, where /usr/bin/qemu-*-static is not mounted. Without it the build
# fails with "exec format error" the moment it runs its first command.
flatpak-cross-setup:
	@echo "Cross-build check for $(FP_ARCH):"
	@test "$(FP_ARCH)" != "$(shell flatpak --default-arch)" \
		|| { echo "  $(FP_ARCH) is this machine's own architecture — nothing to emulate."; exit 0; }
	@if [ -e /proc/sys/fs/binfmt_misc/qemu-$(FP_ARCH) ]; then \
		echo "  binfmt: registered"; \
		grep -q 'flags:.*F' /proc/sys/fs/binfmt_misc/qemu-$(FP_ARCH) \
			&& echo "  binfmt: F flag set (works inside the sandbox)" \
			|| echo "  binfmt: MISSING the F flag — the build will fail inside the sandbox"; \
	else \
		echo "  binfmt: not registered. Install qemu-user-static and"; \
		echo "          qemu-user-static-binfmt (Arch), or the distribution's"; \
		echo "          equivalent, then restart systemd-binfmt."; \
	fi
	@flatpak info org.gnome.Sdk/$(FP_ARCH)/$(FP_RUNTIME) >/dev/null 2>&1 \
		&& echo "  runtime: org.gnome.Sdk/$(FP_ARCH)/$(FP_RUNTIME) installed" \
		|| echo "  runtime: missing — flatpak install --user flathub org.gnome.Platform/$(FP_ARCH)/$(FP_RUNTIME) org.gnome.Sdk/$(FP_ARCH)/$(FP_RUNTIME)"

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
