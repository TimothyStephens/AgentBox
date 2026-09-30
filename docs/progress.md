# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 30 Sep 2026

## Current step

Step 4 (sockguard, observe tier) is next. Step 3 (OMP adapter) is committed with its gate passing on a fake `~/.omp`; `make omp-live` (real `~/.omp`) is waiting on Tim.

## Done

- **Step 1, core launcher** and **step 2, network:** gates passed.
- **Review of steps 1-2:** fixes committed in `e35bc69`.
- **Decisions #1 and #2** (`f60e728`): stale `claude-live` passes flagged by `make itest`; masked names in git history refuse a project until `--accept-history-secrets`.
- **Step 3, OMP adapter:**
  - Provider keys: `ab keys set|ls|rm`, one 0600 file per provider, exported as `AB_KEY_<PROVIDER>` like the Claude token.
  - Generated `models.yml`: only keyed providers plus the local model; their domains are allowed in every mode.
  - The OMP image is derived from `omp-env` (`make omp-image`).
  - A whitelist of `~/.omp` mounts on a user tmpfs `~/.omp`, and a generated `AGENTS.md`.
  - Approvals map to OMP's `--approval-mode`.
  - Gate (`make itest`, step 3 module):
    - selftest passes (lockdown and python), including every key check;
    - OMP boots and answers through its generated config with the session key (stub model);
    - no key is found at rest, and only the named `~/.omp` files are visible;
    - the debugger stops at a breakpoint over DAP;
    - Chromium follows the egress mode;
    - OMP's own shared browser opens example.com through squid, with `~/.omp/run` a real tmpfs;
    - bun reaches the local model through squid.

## Next

1. Step 4, sockguard observe tier: audit mode first, then enforce. Gate: label scoping verified, including exec on another project's container. Start from `spikes/spike2_sockguard.sh` and `spikes/spike_gate.py`; pin sockguard by digest.
2. Stop before step 5 (it gets its own review).

## Open decisions (Tim)

- `~/.omp/agent/.mcp.json` and `agent/mags-codedev.yaml`: may they be mounted read-only? They may hold credentials, which I can't check (`~/.omp` is off limits to me). Not mounted until you say.
- `pi-permission-system`: bring it in (read-only, with a policy per preset) or leave it out? Approvals already map to OMP's own `--approval-mode`.

## Waiting on Tim

- **Run `make claude-live`** and commit `tests/live/claude-live-pass.txt`: no pass is recorded, and token-handling code changed in step 3.
- **Run `make omp-live`** (real `~/.omp`, keys and local model). Before it: copy `~/.omp/agent/models.yml` to `~/.config/agentbox/omp/models.yml` with every `apiKey` line removed, and `ab keys set <provider>` for each provider you want (use dedicated, spend-capped keys). Without a seed, only the local model is offered.
- **Two empty network config files** left by the 10:33 crash: `~/.local/share/containers/storage/networks/abt-4d8752-claude-n-open-1-{egr,int}.json` (0 bytes). podman cannot remove them and warns about them on every call; deleting the two files fixes it. I have not touched podman's storage.
