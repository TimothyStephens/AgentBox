# The loop: make test (fast, no podman) -> make itest (real podman, self-cleaning) -> make clean
.PHONY: test itest spikes clean guard install-slice claude-live itest-oom base-image omp-image claude-image omp-live verify-sockguard install trial-snapshot trial-check egress-soak

guard:
	python3 .claude/hooks/test_guard.py

test: guard
	python3 -m unittest discover -s tests/unit -t . $(if $(V),-v,)

# Real podman. Every resource is named abt-<run>-* and labelled agentbox-test=1 plus
# agentbox-test-run=<run>; the suite removes its own run even on failure.
# Ends with a warning if token-handling files changed since the last passing make claude-live.
itest:
	python3 -m unittest discover -s tests/integration -t . -v; rc=$$?; python3 -m tests.live.freshness check; exit $$rc

spikes:
	./spikes/run_all.sh

# Run by Tim, not by the builder: uses the real token in ~/.config/agentbox/ (read in place).
# AB_LIVE_EGRESS=packages or open tries another mode (default locked).
claude-live:
	python3 -m tests.live.claude_live

# Run by Tim, not by the builder, at a quiet time (model server unloaded, available > 5 GB):
# pushes agentbox.slice past its cap and checks who is OOM-killed.
itest-oom:
	python3 -m tests.oom.itest_oom

# Run by Tim, not by the builder: host-side checks for the tickerlab trial (docs/TRIAL.md). trial-snapshot
# just before the session starts (live containers and the time); trial-check after it ends (live stacks
# unchanged, nothing else under ~ changed, nothing of the session left, no grant volume over the trial root).
# Set AB_TRIAL_CANARY to the planted .env value to also assert the real secret leaked nowhere the session
# persisted. Defaults to project tickerlab under $AB_REPOS_DIR; AB_TRIAL_PROJECT/AB_TRIAL_ROOT override.
# The egress flake (3 Oct): one packages-mode session, 300 sequential and 10x30 parallel requests through its
# squid; failure rate and latency per host and upstream address family. SOAK_SEQ/SOAK_WORKERS/SOAK_EACH to change.
egress-soak:
	python3 -m tests.soak.egress_soak

trial-snapshot:
	python3 -m tests.trial.check snapshot

trial-check:
	python3 -m tests.trial.check check

# Run by Tim, not by the builder: OMP with the real ~/.omp, real keys (read in place) and the real models.
# AB_LIVE_EGRESS=locked|packages|open (default packages); AB_LIVE_MODEL=<provider/model> picks the model.
omp-live:
	python3 -m tests.live.omp_live

# Run by Tim (needs cosign v3 on PATH): verifies the pinned sockguard image was signed by sockguard's release
# workflow (identity from its docs/verification.mdx at the pinned release). Tries the pinned digest, then the
# image's other repo digests (the signature may sit on the multi-arch index), and says which one verified.
SG_IDENTITY := ^https://github.com/CodesWhat/sockguard/.github/workflows/release-from-tag.yml@refs/tags/.+$$
SG_ISSUER := https://token.actions.githubusercontent.com
verify-sockguard:
	@command -v cosign >/dev/null || { echo "cosign not on PATH: install cosign v3 (https://docs.sigstore.dev), then rerun"; exit 2; }
	@pin=$$(python3 -c 'from agentbox.config import DEFAULTS; print(DEFAULTS["sockguard_image"])'); \
	repo=$${pin%@*}; \
	others=$$(podman image inspect "$$pin" --format '{{range .RepoDigests}}{{.}} {{end}}'); \
	for ref in "$$pin" $$others; do \
	  echo "== cosign verify $$ref"; \
	  if cosign verify --certificate-identity-regexp '$(SG_IDENTITY)' \
	       --certificate-oidc-issuer '$(SG_ISSUER)' "$$ref" >/dev/null; then \
	    echo "VERIFIED: $$ref (pinned: $$pin)"; exit 0; fi; \
	done; echo "NOT VERIFIED: no digest of $$repo carries a signature from the release workflow"; exit 1

# ---- images -------------------------------------------------------------------------------------------
# The chain is agentbox-base -> agentbox-<agent> -> (optional) a user's own layer (`ab image build`).
# Build the base first; the agent builds are --pull=never and say so if it is missing.
#
# EVERY pinned download in these images is decided here, so "what version does a session run" is one file.
#
# The base's own base is the official node image on debian bookworm-slim -- bookworm-slim with node
# node team. Both agents need node (Claude Code declares engines.node >= 22, OMP installs with npm), and it
# is the node major Tim's omp-env used, so OMP runs on the node it always has.
#
# PIN IT BY DIGEST: a tag moves, and --pull=never on the agent layers means a moved tag would be invisible
# until something broke. Resolve it with
#   podman pull $(OS_BASE_TAG)
#   podman image inspect $(OS_BASE_TAG) --format '{{index .RepoDigests 0}}'
# and put the sha256:... part in OS_BASE_DIGEST (here, or on the command line). Left unset the build uses
# the plain tag and prints a warning: a working build, not a reproducible one.
# NODE_MAJOR is the one place the node version lives. It was 22 -- the major omp-env used -- and
# moved to 26, the current LTS, on 4 Nov. Moving it changes OMP's node major, so `make itest` and
# `make omp-live` are the gate, not just a rebuild.
NODE_MAJOR := 26
OS_BASE_TAG := docker.io/library/node:$(NODE_MAJOR)-bookworm-slim
OS_BASE_DIGEST ?=
OS_BASE := $(OS_BASE_TAG)$(if $(OS_BASE_DIGEST),@$(OS_BASE_DIGEST))

# The podman client in every agent image: podman-remote, static, matching the host's podman service (5.7.0).
# The checksum is the release's own `shasums` entry, checked against the download on 30 Sep; the build's
# ADD --checksum refuses anything else.
PODMAN_REMOTE_VERSION := 5.7.0
PODMAN_REMOTE_SHA256 := 0d1759e3fc42b0e3a4496176dabc7b65f0f334a0ab2b22eaed7c36afeece8b0c
PODMAN_REMOTE_ARGS := --build-arg PODMAN_REMOTE_VERSION=$(PODMAN_REMOTE_VERSION) \
	--build-arg PODMAN_REMOTE_SHA256=$(PODMAN_REMOTE_SHA256)

# Bun, which OMP needs to load extensions at all (@oh-my-pi/pi-coding-agent@18.13.3 declares
# engines.bun >= 1.4.4). sha256 from the release's own SHASUMS256.txt, 4 Nov 2026. omp-env piped
# https://bun.sh/install into bash; this is the same binary with the checksum checked before it is unpacked.
BUN_VERSION := 1.4.4
BUN_SHA256 := bf8ccbfb2b0b80159e0f9a6d15c55317add8077534dbc6130a3edb52fccb395c

# The agent CLIs, installed BY these images (they used to come from a base built outside this repo, which
# meant the image shipped whatever that base had -- 56 and 59 days old at the last check). Each
# Containerfile asserts the installed binary reports its pin, so a pin here cannot be a claim the image
# does not meet; the version also goes on the agentbox.cli-version label that `ab selftest` reports.
CLAUDE_VERSION := 2.1.318
OMP_VERSION := 18.13.3

BASE_WARN_DAYS ?= 14

base-image:
	@if [ -z "$(OS_BASE_DIGEST)" ]; then \
	  echo ">> WARNING: the OS base is not pinned by digest. To pin it, run these two and rerun with" >&2; \
	  echo ">>   OS_BASE_DIGEST=<the sha256:... they print>:" >&2; \
	  echo ">>   podman pull $(OS_BASE_TAG)" >&2; \
	  echo ">>   podman image inspect $(OS_BASE_TAG) --format '{{index .RepoDigests 0}}'" >&2; \
	fi
	podman build --build-arg OS_BASE=$(OS_BASE) $(PODMAN_REMOTE_ARGS) \
	  -t localhost/agentbox-base:latest -t localhost/agentbox-base:$$(date +%Y%m%d) images/base
	@podman image inspect localhost/agentbox-base:latest \
	  --format '>> agentbox-base {{.Id}} built from {{index .Config.Labels "agentbox.base"}}'

# $(1) agent name  $(2) the command that prints its CLI version  $(3) extra build args
#
# The base is ours now, so the CLI version is read from the BUILT image rather than from the base, and the
# build fails if the image does not report the pin its own label carries. The Containerfile already asserts
# that during the build; this catches a label set from a different ARG than the install used.
define build_agent_image
	@if ! podman image exists localhost/agentbox-base:latest; then \
	  echo "FATAL: localhost/agentbox-base:latest is missing. Run \`make base-image\` first." >&2; \
	  exit 1; \
	fi; \
	created=$$(podman image inspect localhost/agentbox-base:latest --format '{{.Created}}'); \
	age=$$(( ( $$(date +%s) - $$(date -d "$$created" +%s) ) / 86400 )); \
	echo ">> agentbox-base: $$age day(s) old (created $$created)"; \
	if [ "$$age" -gt "$(BASE_WARN_DAYS)" ]; then \
	  echo ">> WARNING: agentbox-base is $$age days old (> $(BASE_WARN_DAYS)); \`make base-image\` for current packages." >&2; \
	fi
	podman build --pull=never $(3) \
	  -t localhost/agentbox-$(1):latest -t localhost/agentbox-$(1):$$(date +%Y%m%d) images/$(1)
	@ver=$$(podman run --rm --entrypoint "" localhost/agentbox-$(1):latest $(2) 2>/dev/null | head -1 | tr -d '\r'); \
	label=$$(podman image inspect localhost/agentbox-$(1):latest \
	  --format '{{index .Config.Labels "agentbox.cli-version"}}'); \
	echo ">> agentbox-$(1): reports '$$ver', labelled '$$label'"; \
	case "$$ver" in *"$$label"*) ;; \
	  *) echo ">> FATAL: the image does not report its own pin; the label would be a lie." >&2; exit 1;; \
	esac

endef

# Collected into variables rather than written inline: a line continuation inside a $(call ...) is a
# needless thing to get wrong, and every ARG an image declares without a default has to be passed here
# (tests/unit/test_images.py checks that, because an unsupplied ARG is silently empty).
# No PODMAN_REMOTE_ARGS here: the podman client moved to the base, so the OMP layer declares no such ARG
# and podman warned "one or more build args were not consumed" (host, 4 Nov). A build arg nothing consumes
# is a silent no-op -- harmless here, but the same shape as a pin that is not being applied where you think
# it is, so tests/unit/test_images.py now checks both directions.
OMP_BUILD_ARGS := --build-arg OMP_VERSION=$(OMP_VERSION) --build-arg BUN_VERSION=$(BUN_VERSION) \
	--build-arg BUN_SHA256=$(BUN_SHA256) --build-arg AB_HOME=$(HOME)
CLAUDE_BUILD_ARGS := --build-arg CLAUDE_VERSION=$(CLAUDE_VERSION)

# The OMP session image (images/omp/Containerfile explains every departure from omp-env).
omp-image:
	$(call build_agent_image,omp,omp --version,$(OMP_BUILD_ARGS))

# The Claude session image (images/claude/Containerfile).
claude-image:
	$(call build_agent_image,claude,claude --version,$(CLAUDE_BUILD_ARGS))

# Run by Tim, not by the builder: puts `agentbox` and an `ab` alias in ~/.local/bin (BINDIR=... to change).
# Refuses to overwrite an `ab` or `agentbox` that is not ours (ApacheBench installs `ab`).
BINDIR ?= $(HOME)/.local/bin
install:
	python3 -m agentbox.install --bindir "$(BINDIR)" --repo "$(CURDIR)"

# Run by Tim, not by the builder: installs the cgroup slice every agentbox container runs in.
install-slice:
	mkdir -p $(HOME)/.config/systemd/user
	install -m 644 deploy/agentbox.slice $(HOME)/.config/systemd/user/agentbox.slice
	systemctl --user daemon-reload
	systemctl --user show agentbox.slice -p LoadState -p MemoryMax -p MemorySwapMax -p TasksMax

# Removes only what the tests created (label agentbox-test=1).
clean:
	-podman ps -aq --filter label=agentbox-test=1 | xargs -r podman rm -f
	-podman pod ps -q --filter label=agentbox-test=1 | xargs -r podman pod rm -f
	-podman network ls -q --filter label=agentbox-test=1 | xargs -r podman network rm
	-podman volume ls -q --filter label=agentbox-test=1 | xargs -r podman volume rm
