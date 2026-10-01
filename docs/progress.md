# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 30 Sep 2026

## Current step

Waiting. A review of steps 3-4 runs next, then step 5.

## Done

- **Steps 1-2**, gates passed; review fixes `e35bc69`. **Decisions #1 and #2**: `f60e728`.
- **Step 3, OMP adapter** (`32e980f`), **step 4, sockguard observe tier** (`2b8adb7`), **follow-ups** (`66fcf62`):
  - one log file per session;
  - the real `~/.omp` layout;
  - the credential rule;
  - `observe_logs`;
  - the podman service in the slice, with gc of orphans;
  - `make verify-sockguard`.
- **claude-live** passed again on 30 Sep on `66fcf62` (recorded in `3ed26fb`). Session 2 now shows its own access log and the denied `example.com:443`.
- **verify-sockguard** (Tim, 30 Sep): the pinned digest `sha256:3ae41b2f…` verified against sockguard's release workflow.
- **This commit:**
  - Both agent images carry podman-remote 5.7.0 (pinned, checksum-verified, first on PATH) and the compose tool 1.5.0.
  - The Claude adapter uses a new derived image, `localhost/agentbox-claude` (`make claude-image`).
  - An itest checks the client major version equals the server's in both images.
  - A session now says when the local model provider has no key.

## omp-live (30 Sep): what it showed

- **FAIL: `401 Invalid API Key` from the local model server.** The seed's `llama-cpp` has no key file, so OMP sent the placeholder `none`. Fix on Tim's side: `ab keys set llama-cpp`, then rerun. Sessions now print a line saying this.
- **One plugin fails to load:** `@narumitw/pi-statusline` imports `stripTerminalSequences`, which OMP 18.1.11 (the version in `omp-env:latest`) does not export. That is a plugin/OMP version mismatch in the omp-env install (`plugins` is from version `19760485e9c2`), not a sandbox problem; it would fail the same way outside agentbox.
- **Denied, as expected in `packages`:** `catalog.stencil.so`, `api.kilo.ai`, `api.venice.ai`, `zenmux.ai`, `coding-intl.dashscope.aliyuncs.com`. These are OMP's model-catalog and discovery calls for built-in providers that have no key here. OMP carried on.
- **The rest held:** the proxy log was captured per session, no key leaked (none were set), and everything was torn down.

## Next

1. Review of steps 3-4.
2. **Step 5:** host-side gate (from `spikes/spike_gate.py`), dev tier, build network, port rejection, proxy injection, exec scoping, image pull allowlist, the build cap (`agentbox/buildcap.py`), and the end-of-session scan (secret scan plus changed files that run later). `exec` tier stays refused until the gate scopes it.

## Open decisions (Tim)

- None.

## Waiting on Tim

- `ab keys set llama-cpp` (if the local model server needs a key), then rerun `make omp-live`.
- `make claude-live`: the image change and the provider-key notice touched token-handling files (`agentbox/adapters/`), so `make itest` warns until a new pass is recorded.
