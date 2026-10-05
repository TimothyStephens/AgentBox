# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 5 Oct 2026

## Current step

**Stopped.** The first real-project session found a critical gate hole: the agent's masks and read-only covers did not reach containers it started through the gate, so a bind of the project root exposed the covered data. Fixed: the covers are now a gate-enforced bind check. The mount part of the step-5 review is reopened and re-reviewed in `docs/reviews/2026-10-01-step-5-coverage.md` (5 Oct). **Before the first real project runs again, that area wants an independent re-review (Tim).**

Earlier this session: `make egress-soak` measured the egress flake (0 failures idle and under load; the tail is the host's uplink, not agentbox).

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

- **After the trial** (`785ee15`). Security was clean; usability failed (the agent could not run an image it built).
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

- **Review of 785ee15 and trial 2** (this commit). The review is answered in `docs/reviews/2026-10-02-785ee15.md`:
  - **H1:** a build record entry could be forged by echoing an ID. Fixed: only the reply's final ID counts, before the client sees it, plus a label and creation-time check.
  - **M1:** pods are forwarded by the checked ID.
  - **M2:** the case-duplicate exemption is keyed by path.
  - **L1-L5:** fixed or tested.
  - **Trial 2:**
    - short names of this project's builds resolve to them;
    - the agent uses `<service>.dns.podman`, which podman's DNS answers and which goes direct;
    - agent memory changes are diffed in the end-of-session scan;
    - `ab project reset-state` (asks first, keeps a copy);
    - lockdown sessions start from empty agent state;
    - `ab preview` is built (127.0.0.1 only, one session service, removed with the session; `test_step6_preview`).
- **Egress soak** (`5d43feb`): `make egress-soak`; the flake is the host's uplink, not agentbox (decisions.md, 5 Oct).
- **Covers enforced at the gate** (this commit): the critical real-project finding. A spawned container's bind that reaches a mask or read-only cover (secrets, `.git/hooks`, `.git/config`, compose/lockdown covers, the `.env` swap) is refused; `.git/config` cannot be written from a spawned container either. `test_step6_covers` (the spec's §5.2 bypass test). decisions.md / the coverage review, 5 Oct.

## Next

1. **Independent re-review of the mount area** (reopened 5 Oct), then the first real project: `~/repos/Life_Updates/life-updates`, from its `wip/old-claude-session` branch, with Tim.

## Open decisions (Tim)

- **Lockdown OMP:** keep `--no-extensions` (your plugins off), or rely on the repo-code covers alone (plugins on)?

## Watch items

- **Egress flake, measured (5 Oct):** `make egress-soak` showed 0 failures in 600 requests idle and 600 under `make itest`. squid used only IPv4 and stayed far from its memory and CPU limits. The latency tail (up to about 5 s on parallel requests) is the same straight from the host without agentbox, so it is the host's uplink. Not reproduced; diagnostics are in place for the next occurrence (decisions.md, 5 Oct).
- **Egress flake, third test (2 Oct, last full run):** `test_step3_omp.test_chromium_follows_the_egress_mode`: with open egress, Chromium did not load example.com through squid. It failed once more alone, loaded the page in a manual rerun, then passed twice. Three tests hit it today (compose pypi 503, step 2 selftest 20 s timeouts, this one), always on squid's upstream side. The next step is to collect squid's cache.log (now kept) from a failing session and test the IPv6 suspect directly.
- **Egress flake, with diagnostics (2 Oct, after trial 2):** in a full itest run, a step 2 selftest (packages) had `CONNECT api.anthropic.com:443` and a nip.io request get no answer within the probe's 20 s (`000 after 20011ms`). squid had logged both requests, so it accepted them and then did not answer in time. The host resolved api.anthropic.com to both IPv4 and IPv6 in 0 ms. That points at squid's upstream connect stalling (an IPv6 attempt is one suspect), not at the gate or DNS on the host. Failure details now carry the proxy's log lines themselves.
- **Egress flake, again (2 Oct):** in one full itest run, squid answered `503 Service Unavailable` to a build's CONNECT to pypi.org (`test_step5_compose`). The same test passed alone four times, every time over IPv4 (`151.101.x.223`). The host resolves pypi.org to IPv6 first, and the session networks are IPv4-only, so one suspect is squid trying IPv6 only; unconfirmed. Kept proxy logs now include squid's stderr (cache.log), which says why an upstream connection failed, and the compose test prints the proxy's pypi/503 lines when it fails.
- **Selftest egress flake (1 Oct):** three proxied CONNECTs (api.anthropic.com, pypi.org, files.pythonhosted.org) went unanswered (`000`) in one step-4 selftest; it passed alone and in a full rerun. Next time, the failed lines carry the proxy's log lines, the DNS answer and timings, and `<logs>/<session>-*-selftest-diag.log` has the details.

## Waiting on Tim

- **The trial** (`docs/trial.md`).
- **`make claude-live` and `make omp-live`:** both passed on 2 Oct against `91f8e93`; the gate, session and selftest changed again in this commit.
