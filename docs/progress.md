# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 30 Sep 2026

## Current step

Follow-ups to steps 3-4 are committed. Waiting on Tim to rerun `make omp-live` (and `make claude-live`). Then a review of steps 3-4, then step 5.

## Done

- **Steps 1-2**, gates passed; review fixes `e35bc69`. **Decisions #1 and #2**: `f60e728`.
- **Step 3, OMP adapter** (`32e980f`) and **step 4, sockguard observe tier** (`2b8adb7`).
- **claude-live** passed on 30 Sep and was recorded in `9d8fa85`.
- **Follow-ups (this commit):**
  - Kept logs are one file per session (`<session>-<start>-proxy.log`). A reused session name had merged claude-live's two sessions into one file.
  - `claude-live` and `omp-live` FAIL when a session that answered has an empty proxy log.
  - The OMP whitelist is reworked from the real `~/.omp` layout: each link's actual target; the versions those live in or the plugins link into; refused only if a target is missing or changes during start. The fixture mirrors that layout.
  - `~/.config/agentbox/omp/mcp.json` and `mags-codedev.yaml` are mounted read-only only if Tim creates them; otherwise the session prints one line.
  - The credential rule is in `decisions.md`. pi-permission-system is not used.
  - `observe_logs` is a project setting, off by default.
  - The podman service runs in `agentbox.slice` (transient `systemd-run --user --scope`), and `ab gc` stops orphaned services (crash test).
  - `make verify-sockguard` checks the image signature with cosign (on PATH).

## Next

1. Tim reruns `make omp-live` and `make claude-live`.
2. Review of steps 3-4.
3. **Step 5:** host-side gate (from `spikes/spike_gate.py`), dev tier, build network, port rejection, proxy injection, exec scoping, image pull allowlist, the build cap (`agentbox/buildcap.py`), and the end-of-session scan (secret scan plus changed files that run later).
4. **Carried into step 5:**
   - The OMP image's podman client is 4.3.1 against a 5.7 service. Reads work; a logs stream once printed "channel 123 … lost synchronization". Check creates, builds and logs.
   - `exec` tier stays refused until the gate scopes it.

## Open decisions (Tim)

- None.

## Waiting on Tim

- **Rerun `make omp-live`.**
  - Optional: create `~/.config/agentbox/omp/models.yml` (your `~/.omp/agent/models.yml` with the `apiKey` lines removed), and run `ab keys set <provider>` for providers you want. Without a seed, only the local model is offered.
  - `omp-live` runs without `mcp.json`/`mags-codedev.yaml` (it uses a temporary config dir) and prints the one-line notice.
- **Rerun `make claude-live`:** the log fix changed `session.py` (token-handling), so `make itest` warns until a new pass is recorded; session 2 should now show its access log and denied list.
- **Install cosign v3** and run `make verify-sockguard`; tell me which digest verified.
- **Two empty network config files** left by the 10:33 crash: `~/.local/share/containers/storage/networks/abt-4d8752-claude-n-open-1-{egr,int}.json`. podman can't remove them; deleting the two files fixes the warning.
