# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 1 Oct 2026

## Current step

Step 5 (host-side gate, dev tier, end-of-session scan) is next. The review of steps 3-4 is answered and its fixes are committed.

## Done

- **Steps 1-4**, with their gates, and the follow-ups (`66fcf62`, `fe19aba`). Live runs on 30 Sep, both passing:
  - `claude-live` (recorded in `f242f10`; the latest pass file is committed with this change);
  - `omp-live`: AB_OK through the local model, the browser opened pypi.org, debugpy worked, no key at rest;
  - `verify-sockguard`: the pinned digest verified.
- **Review of steps 3-4** (`docs/reviews/2026-10-01-steps-3-4.md`, annotated per finding):
  - **Fixed:**
    - #1 HIGH: libpod inspect leaked the live stack's env and `environment:` values; observe is narrowed;
    - #2: Claude lockdown no longer pre-trusts and gets a managed policy; OMP's persistent code paths are read-only;
    - #3: `~/.omp` targets must be `versions/<v>/<name>`; hard-linked credential files and credentials in git URLs are refused;
    - #4: pods were not scoped; removed;
    - #6: the raw podman socket moved outside the mountable run dir, and the audit refuses foreign sockets;
    - #7: a defaulted compose project must be this project's own stack;
    - #8: pip hashes, `/root` credential check;
    - gc on first use after boot (a doc claim).
  - **Not reproduced:** #5 (OMP's 400 dumps hold no key).
  - **Rejected:** #9 (credential rule).
  - **Deferred to Tim:** the second half of #2, below.
- **`make install`** (agentbox launcher plus `ab` alias, never over a foreign `ab`) and `README.md` with the setup steps.

## Next

1. **Step 5:**
   - the host-side gate (start from `spikes/spike_gate.py`) between the agent and sockguard;
   - the dev tier on `<project>-dev`: create pinned to the session network, no port publishing, proxy injection, restart policies stripped, owner labels, limits and OOM score;
   - builds through the holder's network namespace and the build cap (`agentbox/buildcap.py`);
   - exec scoped to the session's own containers;
   - the image pull allowlist;
   - the end-of-session scan: secret scan of the session's commits, plus changed files that run later.
2. Stop when step 5 is built and green; it gets its own review before real projects use it.

## Open decisions (Tim)

- **OMP repo-provided code in `lockdown`** (review #2): OMP loads extensions, hooks, tools and MCP servers from the repo (`.omp/`, `.claude/`) without asking. Accept, or run lockdown OMP sessions with `--no-extensions`, which also drops the plugins from `~/.omp`?

## Waiting on Tim

- **`make install`:** first remove your temporary `~/.local/bin/ab` (`make install` refuses to overwrite it).
- **`make claude-live`:** token-handling files changed (adapters, session, hardening), so `make itest` warns until a new pass is recorded.
