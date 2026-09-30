# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 30 Sep 2026

## Current step

Stopped before step 5 (host-side gate and dev tier), which gets its own review. Steps 1-4 are committed, with their gates passing.

## Done

- **Step 1, core launcher** and **step 2, network:** gates passed. Review fixes: `e35bc69`.
- **Decisions #1 and #2** (`f60e728`): `make itest` flags a stale `claude-live` pass; masked names in git history refuse a project until `--accept-history-secrets`.
- **Step 3, OMP adapter** (`32e980f`):
  - provider keys (`ab keys set|ls|rm`), exported like the Claude token;
  - `models.yml` with keyed providers plus the local model;
  - the derived image `localhost/agentbox-omp`;
  - a whitelist of `~/.omp` mounts on a user tmpfs;
  - approvals mapped to `--approval-mode`.
- **Step 4, sockguard observe tier:**
  - a per-session podman service, with sockguard 2.2.1 (pinned by digest) in front;
  - read-only access to the live compose stack, environments redacted, everything else denied;
  - audit-mode discovery in the itest only;
  - selftest cases against a canary container.
  - Gate: `make itest` step 4 module (label scoping, including exec on another project's container, refused).

## Next

1. **Step 5** (after its review): host-side gate (start from `spikes/spike_gate.py`), dev tier, build network, port rejection, proxy injection, exec scoping, image pull allowlist, the build cap (`agentbox/buildcap.py`), and the end-of-session scan (secret scan over the session's commits, plus changed files that run later on the host).
2. **Carried into step 5:**
   - The OMP image's podman client is 4.3.1; step 4 showed it works against the 5.7 service for reads. Check it for creates and builds.
   - `exec` tier stays refused until the gate scopes it.

## Open decisions (Tim)

- `~/.omp/agent/.mcp.json` and `agent/mags-codedev.yaml`: may they be mounted read-only? They may hold credentials, which I can't check (`~/.omp` is off limits to me). Not mounted until you say.
- `pi-permission-system`: bring it in (read-only, with a policy per preset) or leave it out? Approvals already map to OMP's own `--approval-mode`.
- Sockguard's release signature: verify it with cosign? It isn't installed on the host, and installing it is your call.

## Waiting on Tim

- **Run `make claude-live`** and commit `tests/live/claude-live-pass.txt`: no pass is recorded, and token-handling code changed in steps 3 and 4.
- **Run `make omp-live`** (real `~/.omp`, keys and local model). Before it: copy `~/.omp/agent/models.yml` to `~/.config/agentbox/omp/models.yml` with every `apiKey` line removed, and `ab keys set <provider>` for each provider you want (use dedicated, spend-capped keys). Without a seed, only the local model is offered.
- **Two empty network config files** left by the 10:33 crash: `~/.local/share/containers/storage/networks/abt-4d8752-claude-n-open-1-{egr,int}.json` (0 bytes). podman cannot remove them and warns about them on every call; deleting the two files fixes it. I have not touched podman's storage.
