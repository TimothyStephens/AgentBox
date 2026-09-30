# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 30 Sep 2026

## Current step

Step 3 (OMP adapter), in progress. Decisions #1 and #2 are committed (`make test` 111 pass, `make itest` 21 pass, 30 Sep).

## Done

- **Step 1, core launcher:** registry, presets, path validator, fixed hardening, Claude adapter, masks, backups, gc. Gate passed.
- **Step 2, network:** per-session internal network and squid egress in three modes. Gate passed.
- **Review of steps 1-2** (`docs/reviews/2026-09-30-steps-1-2.md`): fixes committed in `e35bc69`.
- **Decision #1:** `make claude-live` records the commit it passed on (`tests/live/claude-live-pass.txt`); `make itest` warns when token-handling files changed since then (`tests/live/freshness.py`).
- **Decision #2:** `ab project add` refuses a project with masked file names in its git history until `--accept-history-secrets`; sessions refuse names not accepted; `ab project accept-history-secrets <project>`.
- **End-of-session scan scope** (for step 5) written into the design's open items and `decisions.md`.

## Step 3 so far (uncommitted unless noted)

- Found in the image and OMP's source (not in `~/.omp`, which is off limits):
  - OMP 18.1.11 runs under bun (honours `HTTPS_PROXY`/`NO_PROXY`).
  - Config root is `os.homedir()/.omp`; the agent dir is `~/.omp/agent`.
  - `models.yml` `apiKey` is an env var name, a literal, or `!command`.
  - The approval setting is `tools.approvalMode`, default `yolo`, with CLI `--approval-mode=always-ask|write|yolo`.
- `pi-permission-system` (in `~/.omp/agent/extensions/`) is a third-party policy extension (allow/ask/deny rules per tool, bash pattern, MCP, skill, from `pi-permissions.jsonc`), not OMP's approval switch. Plan: map the preset to OMP's own `--approval-mode`, don't mount the extension; reported to Tim.
- The base image keeps bun in `/root` (0700), and `omp-env install` writes absolute `/root/.omp/...` plugin links. So there is a derived image `images/omp/Containerfile` → `localhost/agentbox-omp` (`/root` 0755, `/root/.omp -> $HOME/.omp`, chromium, debugpy, podman-compose). Not built yet.
- Written, not wired in: `agentbox/keys.py` (provider key files, `ab keys set`), `agentbox/miniyaml.py` (strict YAML subset reader), `agentbox/adapters/omp.py` (mount whitelist, generated `models.yml`/`AGENTS.md`).
- Still to do: `SessionSetup` in the adapter base; session and wrapper export keys; squid model domains from keys; selftest checks keys like the token; `ab keys set/ls/rm`; `omp_home` setting; unit tests; build the image; itest with a fake `~/.omp` under `/tmp`, a stub model and fake keys; `make omp-live` for Tim; docs.

## Next

1. Finish step 3. Gate: OMP boots, debugger and browser work through the proxy.
2. Step 4, sockguard observe tier: audit mode, then enforce. Gate: label scoping, including exec on another project's container.
3. Stop before step 5 (it gets its own review).

## Open decisions

- Whether `~/.omp/agent/.mcp.json` and `mags-codedev.yaml` may be mounted (read-only): they may hold credentials, which I can't check. Not mounted until Tim says.

## Waiting on Tim

- **Run `make claude-live`.** No pass is recorded yet, so `make itest` warns until it is run and `tests/live/claude-live-pass.txt` is committed.
- **Two empty network config files** left by the 10:33 crash mid-itest: `~/.local/share/containers/storage/networks/abt-4d8752-claude-n-open-1-{egr,int}.json` (0 bytes). podman cannot remove them (`network not found`) and warns about them on every call; deleting the two files fixes it. I have not touched podman's storage.
