# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 1 Oct 2026

## Current step

**Stopped.** The step 5 audit (A1, A4, B, C) is fixed or answered, and A2/A3 are replaced by the field table (`docs/reviews/2026-10-01-step-5-coverage.md`). Next: a real trial on one throwaway project, set up with Tim.

## Done

- **Steps 1-5** (`e35feab`), the reviews of steps 1-2 and 3-4, and Tim's decisions 1-4 after step 5 (`bbd81b6`). Live checks on 1 Oct: `claude-live` and `omp-live` pass, and `make install` works.
- **Step 5 audit** (this commit). Each review file ends with a response table: fixed, rejected or deferred, and the test.
  - **Gate:**
    - create fields are allowlisted (unknown fields refused);
    - every host-reaching or privileged field found by capturing podman-remote's requests is refused;
    - a verified-ID record means nothing is usable before its post-create check passes (A4's timing question);
    - the post-create check fails closed;
    - start-time re-checks plus a mountinfo check after start (found: a bind source swapped for a symlink after create was mounted at start);
    - named volumes must be the session's;
    - build tags, options and secrets are restricted;
    - its own deny list, canonical paths, no pipelining.
    - Docker-API creates, infra pods and sysctls are refused, because sockguard refuses them anyway.
  - **Scan:** git never runs in a repo whose config the agent changed or created (H2 was confirmed: the old scan ran the agent's command on the host); file hashes instead of `git status`; all refs; it cannot stop a teardown; `ab kill` and gc scan and keep logs.
  - **Teardown:** anonymous volumes (`rm -v`), the build cgroup, the per-session TMPDIR (buildah cache).
  - **Lockdown:** the project's `.omp` is covered whole.
  - **Tests:** `test_step5_fields` (95 field cases through the real gate), plus unit tests for each finding; the leftovers check covers pods, volumes and images.

## Next

1. **The real trial** on a throwaway project, with Tim.

## Open decisions (Tim)

- **Start-time race (high, residual):** accept the detect-and-remove approach, bind only grant roots, or rewrite sub-path binds as volume sub-paths. The options are in `docs/decisions.md` (step 5 audit).
- **Lockdown OMP:** keep `--no-extensions` (your plugins off), or rely on the repo-code covers alone (plugins on)?
- **Image reads (A4 low, deferred):** any local image can be inspected or used as `FROM`; scope it, or accept?

## Waiting on Tim

- **`make claude-live` and `make omp-live`:** the session, gate and adapter files changed again, so `make itest` warns until new passes are recorded.
