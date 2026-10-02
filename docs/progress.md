# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 2 Oct 2026 (after the trial)

## Current step

**Stopped.** The trial's usability failures are fixed; a real podman-compose workflow now passes through the real gate (`test_step5_compose`). Tim reruns the trial.

## Done

- **Steps 1-5** (`e35feab`), the reviews of steps 1-2 and 3-4, and Tim's decisions 1-4 after step 5 (`bbd81b6`). Live checks on 1 Oct: `claude-live` and `omp-live` pass, and `make install` works.
- **Step 5 audit** (`73fb267`). Each review file ends with a response table: fixed, rejected or deferred, and the test.
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
- **Follow-ups** (`91f8e93`):
  - **Bind sources:** a grant root itself is bound as is. A container's sub-path bind becomes a `SubPath` of the gate's bind-backed volume over that root, which podman 5.7 resolves inside the volume. The evidence is in decisions.md: a symlink present at create, swapped in, or swapped in as an intermediate component never leads out; 0 escapes in 150 racing starts against 21 of 60 for a plain bind. Pods keep sub-paths refused.
  - **Image allowlist:** the dev tier runs and builds only from allowlisted-registry images, its own builds, or the project's `dev_images` (`--dev-image`); the gate reads a build's Containerfile first.
  - **Selftest:** the local model line is SKIP with a reason when 10.0.0.2 does not answer from the host.
- **Before the trial** (`a16a74e`):
  - a project's own builds, from any session, are allowed by the gate's record of the image IDs it built, not by labels; `ab gc` keeps the record in step;
  - failed proxied selftest checks carry diagnostics (proxy log lines, DNS answer, timings), with no retries;
  - `docs/trial.md`, `make trial-snapshot` and `make trial-check`.

- **After the trial** (this commit; `make test` 281 OK, `make itest` 155 OK). Security was clean; usability failed (the agent could not run an image it built).
  - `test_step5_compose` runs a real compose workflow through the real gate, and it failed first. These fixes made it pass:
    - builds reach the proxy (by IP, plus the session's DNS);
    - pulls of the project's own present images are answered by the gate;
    - podman's own stop-signal and image annotations pass the start check;
    - `depends_on` works for session containers;
    - `NO_PROXY` and `no_proxy` can both be set (the second goes to `envmerge`, since sockguard refuses case-duplicate keys);
    - missing objects get 404;
    - session pod operations go to the podman service (sockguard cannot resolve pods), and teardown removes a left pod.
  - Both agent images now have curl and python3-venv.
  - `trial-check`:
    - refuses while a session runs;
    - snapshots only live compose projects, and warns about old launcher containers;
    - lists live stacks' bind writes and the host-side Claude configs apart from failures;
    - cuts long lists per directory.
  - Found by the full itest: a race where a build's ID was recorded after the client saw the end of the reply (fixed: recorded as it passes), and kept proxy logs now carry squid's stderr.

## Next

1. **The trial again** (`docs/trial.md`), run by Tim: reset `~/repos/abt-trial` and give the agent the same task.

## Open decisions (Tim)

- **Lockdown OMP:** keep `--no-extensions` (your plugins off), or rely on the repo-code covers alone (plugins on)?
- **Preview:** `ab preview` is not built, so the trial's app can only be checked from the host (see trial.md, step d). Build it next if you want the browser view.

## Watch items

- **Egress flake, again (2 Oct):** in one full itest run, squid answered `503 Service Unavailable` to a build's CONNECT to pypi.org (`test_step5_compose`). The same test passed alone four times, every time over IPv4 (`151.101.x.223`). The host resolves pypi.org to IPv6 first, and the session networks are IPv4-only, so one suspect is squid trying IPv6 only; unconfirmed. Kept proxy logs now include squid's stderr (cache.log), which says why an upstream connection failed, and the compose test prints the proxy's pypi/503 lines when it fails.
- **Selftest egress flake (1 Oct):** three proxied CONNECTs (api.anthropic.com, pypi.org, files.pythonhosted.org) went unanswered (`000`) in one step-4 selftest; it passed alone and in a full rerun. Next time, the failed lines carry the proxy's log lines, the DNS answer and timings, and `<logs>/<session>-*-selftest-diag.log` has the details.

## Waiting on Tim

- **The trial** (`docs/trial.md`).
- **`make claude-live` and `make omp-live`:** both passed on 2 Oct against `91f8e93`; the gate, session and selftest changed again in this commit.
