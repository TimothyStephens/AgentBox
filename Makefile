# The loop: make test (fast, no podman) -> make itest (real podman, self-cleaning) -> make clean
.PHONY: test itest spikes clean guard install-slice claude-live itest-oom omp-image claude-image omp-live verify-sockguard install trial-snapshot trial-check egress-soak

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

# Run by Tim, not by the builder: the throwaway trial (docs/trial.md). trial-snapshot just before the session
# starts (live containers and the time); trial-check after it ends (live stacks unchanged, nothing else under
# ~ changed, nothing of the session left, no grant volume over the trial root).
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

# The podman client in every agent image: podman-remote, static, matching the host's podman service (5.7.0).
# The checksum is the release's own `shasums` entry, checked against the download on 30 Sep; the build's
# ADD --checksum refuses anything else.
PODMAN_REMOTE_VERSION := 5.7.0
PODMAN_REMOTE_SHA256 := 0d1759e3fc42b0e3a4496176dabc7b65f0f334a0ab2b22eaed7c36afeece8b0c
PODMAN_REMOTE_ARGS := --build-arg PODMAN_REMOTE_VERSION=$(PODMAN_REMOTE_VERSION) \
	--build-arg PODMAN_REMOTE_SHA256=$(PODMAN_REMOTE_SHA256)

# The OMP session image, derived from localhost/omp-env (images/omp/Containerfile explains each change).
omp-image:
	podman build --pull=never --build-arg AB_HOME=$(HOME) $(PODMAN_REMOTE_ARGS) -t localhost/agentbox-omp:latest \
	  -t localhost/agentbox-omp:$$(date +%Y%m%d) images/omp

# The Claude session image, derived from localhost/claudecode (images/claude/Containerfile).
claude-image:
	podman build --pull=never $(PODMAN_REMOTE_ARGS) -t localhost/agentbox-claude:latest \
	  -t localhost/agentbox-claude:$$(date +%Y%m%d) images/claude

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
