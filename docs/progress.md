# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 1 Oct 2026

## Current step

**Stopped.** Tim's decisions 1-4 after step 5 are applied, plus one fix found by the builder. The independent review of step 5 did not finish (it stopped before any findings), so step 5 still needs a complete review. Next after that: a real trial on one throwaway project, set up with Tim.

## Done

- **Steps 1-5** (`e35feab`) and the reviews of steps 1-2 and 3-4. Live checks on 1 Oct: `claude-live` and `omp-live` pass, and `make install` works.
- **Since step 5** (this commit):
  - **Decision 1, OMP repo code in lockdown:**
    - OMP cannot switch off only repo-local code, so lockdown uses `--no-extensions` and project MCP config off;
    - the repo's code locations are covered, because `--no-extensions` misses custom tools;
    - tested: four planted pieces ran in the control and none in lockdown.
  - **Decision 2:** the gate sets `no_proxy` in dev containers (localhost, 127.0.0.1, the subnet, and dev container names and aliases); the session notes cover the rest.
  - **Decision 3:** session-built images carry project and session labels; `ab gc` removes those older than 7 days except the newest per tag.
  - **Decision 4:** the guard-change rule is recorded. No `.claude/` edits were made this round.
  - **Builder's finding:** case-variant JSON fields (`"PortMappings"`) published a host port past the gate and sockguard. Fixed: strict parsing, exact field spellings, and verification of what podman stored after every create (removed and refused on any violation).

## Next

1. **A complete independent review of step 5** (the brief is `docs/reviews/2026-10-01-step-5.prompt.md`).
2. **The real trial** on a throwaway project, with Tim.

## Open decisions (Tim)

- **Lockdown OMP:** keep `--no-extensions` (your plugins off), or rely on the repo-code covers alone (plugins on)? The covers alone were not tested without `--no-extensions`.

## Waiting on Tim

- **`make claude-live`:** token-handling files changed again (adapters, session, gate), so `make itest` warns until a new pass is recorded.
