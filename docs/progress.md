# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 4 Nov 2026

## Status (single source of truth — what this tool does and does not do)

Threat model: **mistake containment + cross-project isolation**, NOT a wall against a deliberately malicious or prompt-injected agent (design.md top). Read every line below against that.

| Feature | State | Notes |
| --- | --- | --- |
| **`ab init` templates** (pod, python) | **built** | Scaffolds a grantcheck-clean project by construction; registers it; zero warnings/overrides on a fresh project (`test_init`). pod: data in a named volume `<n>_data`. python: data path from `DATA_DIR` in `.env`/`.env.dev`. Takes the same setting flags as `ab project add`/`set`, all validated before anything is written. |
| **Presets** (lockdown, build/pods, build-open, script/python, research, operate, analysis-offline) | **built** | `build`/`script` canonical, `pods`/`python` true aliases (resolve exactly as before). Open egress is a preset *attribute* materialised onto the project's entry, never a live reference — a hand-written entry naming an open preset is fail-closed; changing a preset clears the security overrides set against the old one. decisions.md, 26 Oct. |
| **One settings table** (`projectkeys.py`) | **built** | One row per registry key decides every property of it (flags, validator, looseness order, security, cleared-on-preset-change, unsettable, display, inertness); `presets.SETTINGS`/`ALLOWED`/`LOOSENESS`, `registry.PROJECT_KEYS`, the cli lists, the ceiling and `ab project show` all derive from it. A new key is a TypeError until each property is answered. Retired the five parallel lists behind five audit findings (decisions.md, 27 Oct). |
| **`ab project set`** | **built** | Changes a registered project in place with the shared flags, re-validated (ceiling, grantcheck, history secrets); `--unset KEY`, `--clear-<list>`. Refused while a session for the project is live — as are `rm`, `reset-state` and the `accept-*` commands (no `--force`); liveness is any RUNNING container labelled `agentbox.project=<name>`, so a dead launcher no longer hides one. The whole entry (unknown keys included) is validated before anything is saved, and `load_project` is just as strict. Moving `root` is still rm + add. |
| **grantcheck (Track 1, primary wall)** | **built** | Refuses a session when a read-write grant holds an uncovered secret (`.env`/`*.pem`/`*.key`/`secrets/`), a git-credential URL, or live data (`*.db` etc. >1 MB, or a compose data bind). Ack via `accept-grant-exposure`, re-prompted on content change (M4). |
| **grantcheck coverage** | **known limitation** | Catches common shapes only: secrets under other names, more DB shapes, exempt names, host-written files all get past it (decisions.md H2/M1–M3). `data_ro` is now scanned, as warnings (26 Oct, F4). Operational rule (keep secrets/live data out of the grant) is the real control. |
| **Covers / masks + gate enforcement** | **built** | Masks for secrets; `.git/hooks`/`.git/config`, compose/lockdown covers; enforced for gate-spawned containers (N1–N9, cover identity, parent pins, mountinfo). Reviewed 5–6 Oct (`docs/reviews/2026-10-0{5,6}-covers*.md`). |
| **H1 (U/chown + foreign-uid binds)** | **built** | Gate refuses `U`/`chown` on grant binds/SubPaths and refuses a gate-spawned bind of a foreign-uid source; grantcheck warns on foreign-owned/unreadable entries. |
| **H3 (mask_skip not an exemption)** | **built** | `mask_skip` is a speed knob; grantcheck still scans those folders; not offered as a remedy. |
| **M4 (ack shows findings, tracks content)** | **built** | Full findings printed; fingerprint over content, so a byte change re-prompts. |
| **Egress (locked/packages/open, squid)** | **built** | Modes behave as designed; private-address refusal; an occasional upstream latency flake is the host's uplink, not agentbox (watch items below; `make egress-soak`). |
| **Preview** (`ab session preview`) | **built** | One session service on 127.0.0.1, reached by `ssh -L`, removed with the session (`test_preview`). |
| **Podman tiers** | **none/observe/dev built; exec NOT built** | `exec` is refused in code (`session.py`: "exec on the live stack is not built"). The built tiers scope to the session's own dev stack; builds via the holder-netns path. |
| **`ab profile install`** (OMP plugins) | **built** | The profile declares plugins in `plugins.toml` (marketplace entries pinned by a 40-char sha, npm packages at exact versions, git checkouts linked); `ab profile install omp` installs them in ONE hardened container behind the session proxy, `packages` egress and no model-host rule, into `~/.config/agentbox/omp/plugins`, which sessions mount **read-only** (proved by a real read-only bind mount in `test_step3_omp`). Pinning is agentbox's: `omp plugin marketplace add` cannot take a commit, so the catalog is cloned, checked out, verified and added by path. The tree carries a lockfile recording the image it was built against, and a session says so when that no longer matches. **`~/.omp` is not read at all** — `omp_home` is a retired setting — and ~160 lines of whitelist went with it (decisions.md, 5–6 Nov). |
| **Adapters** | **Claude + OMP built; Pi/Codex not built** | `adapters/claude.py`, `adapters/omp.py` exist; Pi/Codex are planned (step 8). |
| **Caps/devices opt-in** | **not built** | The gate refuses `cap_add`, `devices` and security options in every preset; the `caps = opt-in` preset value is inert. |
| **Forgotten-session guards** | **built** | `ab session ls` shows start/age/idle and flags long-idle sessions; `ab run` warns if a session for the project is already up. No auto-reap (decisions.md 6 Oct). |
| **Backups (per-session rsync)** | **built, scoped** | `--link-dest` snapshots of the grant (incl. untracked/gitignored). A within-session undo, **NOT a data backup** (same disk, only fresh as of session start) — never presented as one. |
| **`ab ws` / `allow` / `deploy` / `outbox` / `reload` / `resume`** | **not built** | No such subcommands (`ab run` starts a session; `ab project set` changes settings, from the next session). `ab allow` was the "loosen a *running* session" idea; `deploy` is step 6, `ws` step 7 — though `contrib/ab-ws` now does the workspace job from outside the CLI. **`ab attach` is built** (29 Oct): the way back into the one live session a project is allowed. |
| **Model key gateway** | **deferred** | Sessions use the Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) and per-provider key files; the per-model API-key gateway is not built. |
| **`masked_paths`** (per-project masked dir) | **not built** | No such setting; only secret-name masks + `secrets/` tmpfs exist. `docs/agentbox-masked-paths.md` is a proposal, now marked as such. |
| **`--data-bind`** (live data in `./data`) | **deferred** | Contract fixed (mask `./data` via covers; user names `--data-backup <target>`; agentbox masks, does not back up). Use the named-volume default until then. |
| **`dev/data`→`data` overlay** | **not built** | Deliberately not built; the substitute mechanism stays `.env`-only unless a real project forces it. |
| **Real projects on the box** | **off** | Gated until the mount/covers area + Track 1 are independently re-reviewed again (below). |

Optional TODOs and deferred maintainability work (none of it blocks use) live in `docs/TODO.md`, which is
organised by what can be acted on: §1 open questions needing a decision, §2 scoped and ready, §3 deferred by
design, §4 the maintainability backlog, §5 watch-only, §6 known limitations.

## Current step

**Stopped.** The threat model is now written down (mistake containment + cross-project isolation, NOT a malicious/injected agent; `docs/design.md` top, decisions.md 6 Oct). The fifth review (`docs/reviews/2026-10-06-final.md`) was scoped against it: H3, M4, the cheap half of H1, and the backup proof were done; everything else is recorded as a known limitation under the threat model (decisions.md 6 Oct), not a TODO. `ab init` (pod/python) now scaffolds a grantcheck-clean project by construction (data in a named volume; `--data-bind` deferred).

Two tracks after the covers fix was reviewed again (`docs/reviews/2026-10-06-covers2.md`, a fourth high, N1).
- **Track 1 (primary wall):** the grant holds code; a grant with uncovered secrets or live data refuses to start (`agentbox/grantcheck.py`, `ab project accept-grant-exposure` / `--i-understand-grant-exposure`). Covers are defence in depth behind it. Documented in the design doc and the session notes.
- **Track 2:** N1 (stop/start race: per-container lock + PID recheck), N2 (nothing mounted under a dir mask), N3 (`RUN --mount source=` checked by the gate), N4-N9 fixed. Builds confirmed: a COPY of a cover arrives empty, and a crafted context carrying masked content is refused.

**Real projects stay off until this area is independently re-reviewed again (Tim).** The covers review of 5 Oct is still open from last round too.

## Done

- **One project is one directory; a reused name is checked** (9 Nov): project state moved from `<data>/<name>/`,
  `<data>/backups/<name>/` and `<data>/built-images/<name>.json` to `<data>/projects/<name>/{<agent>,backups,built-images.json}`
  (a project named `logs` used to put its transcripts inside the kept logs; `purge` left the gate's build record behind). A
  flat data dir migrates on the next `ab`. `projects/<name>/project.json` records the root; `ab project add`/`ab init`
  refuse the name for another root without `--fresh` (archive) or `--reattach`; a session start checks too. `purge`
  deletes the directory whole. decisions.md, 9 Nov.
- **CLI: session commands grouped under `ab session`** (this commit): the session-management verbs moved from top level to `ab session ls` / `kill` / `gc` / `preview`, mirroring `ab project`; `ab run`, `ab init`, `ab selftest` stay top-level. Clean cutover, no aliases (pre-release). Two noun namespaces now: `ab project` (durable config) and `ab session` (running instances). Docs, help text, warnings and tests updated; `make test` 494 OK.
- **Fifth-review H-series confirmed + forgotten-session guards**: H3/M4 confirmed with tests; H1 expensive half built (gate refuses a foreign-uid bind source). `ab session ls` shows start/age/idle and flags long-idle running sessions; `ab run` warns on an already-running session for the same project (no auto-reap, by choice). Minor CLI: `ab project rm` takes several names, `ab run` takes positional `<agent> <project>`. `make test`: 428 OK. See the Status table above and decisions.md (6 Oct).
- **`ab init` scaffolding**: `ab init <pod|python> --name <n> [--upstream <url>]` writes a project whose layout passes grantcheck by construction (0600 gitignored `.env` masked, committed fake `.env.dev`, `AGENTS.md` of enforced reality, app/tests/dev skeleton; pod adds compose.yaml/compose.dev.yaml/Containerfile with **data in a named volume `<n>_data`**, not a `./data` bind), `git init`s + first commit, optionally pushes `--upstream` (code backup only), then runs `ab project add` itself. `test_init.test_fresh_project_is_clean_by_construction`: both templates register with zero findings, zero warnings, zero overrides. `--data-bind` deferred (decisions.md 6 Oct, with its contract); the per-session rsync is explicitly not a data backup.
- **Threat model + fifth review** (`docs/reviews/2026-10-06-final.md`): threat model at the top of the design doc. H3 (`mask_skip` is a speed knob, not an exemption — scanned, and a secret there is reported "unmasked"), M4 (the exposure ack shows the findings and fingerprints their content, so a changed file re-prompts), the cheap half of H1 (the gate refuses `U`/`chown` on grant binds and SubPaths; `grantcheck` warns on a foreign-owned or unreadable grant entry), and the backup proof (untracked and gitignored files are kept; `test_untracked_and_gitignored_files_are_backed_up`). The rest (H2 secret-name shapes, M1 data shapes, M2 `data_ro`, M3 git-credential shapes, L1-L6) are known limitations in decisions.md.

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
  - **Scan:** git never runs in a repo whose config the agent changed or created (H2 was confirmed: the old scan ran the agent's command on the host); file hashes instead of `git status`; all refs; it cannot stop a teardown; `ab session kill` and gc scan and keep logs.
  - **Teardown:** anonymous volumes (`rm -v`), the build cgroup, the per-session TMPDIR (buildah cache).
  - **Lockdown:** the project's `.omp` is covered whole.
  - **Tests:** `test_step5_fields` (95 field cases through the real gate), plus unit tests for each finding; the leftovers check covers pods, volumes and images.
- **Follow-ups** (`91f8e93`):
  - **Bind sources:** a grant root itself is bound as is. A container's sub-path bind becomes a `SubPath` of the gate's bind-backed volume over that root, which podman 5.7 resolves inside the volume. The evidence is in decisions.md: a symlink present at create, swapped in, or swapped in as an intermediate component never leads out; 0 escapes in 150 racing starts against 21 of 60 for a plain bind. Pods keep sub-paths refused.
  - **Image allowlist:** the dev tier runs and builds only from allowlisted-registry images, its own builds, or the project's `dev_images` (`--dev-image`); the gate reads a build's Containerfile first.
  - **Selftest:** the local model line is SKIP with a reason when 10.0.0.2 does not answer from the host.
- **Before the trial** (`a16a74e`):
  - a project's own builds, from any session, are allowed by the gate's record of the image IDs it built, not by labels; `ab session gc` keeps the record in step;
  - failed proxied selftest checks carry diagnostics (proxy log lines, DNS answer, timings), with no retries;
  - `docs/TRIAL.md`, `make trial-snapshot` and `make trial-check`.

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
    - `ab session preview` is built (127.0.0.1 only, one session service, removed with the session; `test_step6_preview`).
- **Egress soak** (`5d43feb`): `make egress-soak`; the flake is the host's uplink, not agentbox (decisions.md, 5 Oct).
- **Track 1 + covers audit 2** (this commit; `docs/reviews/2026-10-06-covers2.md`, no rejections): `grantcheck.py` refuses a grant holding uncovered secrets or live data (the primary wall); N1 (stop/start race: per-container lock + PID recheck), N2 (nothing under a dir mask), N3 (`RUN --mount source=`), N4-N9. Podman checks run: stop-after-init leaves `exited`; a duplicate tmpfs+bind destination is refused.
- **Covers audit** (`5690d9f`; `docs/reviews/2026-10-05-covers.md`, no rejections): H1 (cover parents pinned, cover identity checked), H2 (start = init, check mounts before the entrypoint, then start; fail closed), H3 (read-only grants stay read-only), builds (COPY of masks arrives empty; a context carrying masked content is refused), plus M1/L1-L4.
- **Covers enforced at the gate** (`76e5721`): the critical real-project finding. A spawned container's bind that reaches a mask or read-only cover (secrets, `.git/hooks`, `.git/config`, compose/lockdown covers, the `.env` swap) is refused; `.git/config` cannot be written from a spawned container either. `test_step6_covers` (the spec's §5.2 bypass test). decisions.md / the coverage review, 5 Oct.

- **Third preset/settings audit** (28 Oct, F-1 … F-12; decisions.md 28 Oct): the model held — the
  open-egress invariant intact, all eleven previous findings closed — but both highs were the same defect:
  unknown state read as "nothing running". Fixed as two structural changes plus the rest. One liveness
  predicate (`session.project_is_live`), fail-closed by construction, used by every command that refuses on
  a live project; podman's readers raise instead of returning `[]`; a fail-open sweep of every swallowed
  error on a safety path (six fixed, four left with the reasoning recorded). One grant preflight
  (`grantcheck.preflight`) so `ab project check`'s verdict is `ab run`'s, which also split "covered" into
  `mounts.hidden` (unreadable) and `mounts.covered` (what the gate refuses binds onto) — a read-only cover
  had been hiding secrets from the scan. Acknowledgements are honoured on `ab project set`, and a tightening
  is never blocked. Podman access became a capability set, not a ladder. Plus `compose_project`
  explicitness, same-project grant nesting, `dev_images` reserved names, the `security` column made to drive
  four real things, and the history-secrets acknowledgement tracking its whole found set. **F-12's text was
  never supplied** (TODO.md).

- **`podman network ls` had never worked** (28 Oct, after the audit round): its `--format` template is
  rejected by podman 5.7 and the failure was swallowed, so session networks were invisible to
  `ab session ls`/`kill`/`gc`/`preview`; the sweep made it loud, and it is fixed by reading `--format json`.
  The fake podman now refuses the templates real podman refuses, a real-output fixture pins the shape, and
  `tests/manual/podman_formats.sh` (read-only) checks every `--format` the code issues against real podman,
  with a unit test keeping the two in step. F-12's cleanups landed with it: `--swap` exists as a flag, a
  project's swap merges with the default `.env`→`.env.dev`, the ceiling refusal names the right limit, and
  three dead parameters plus one dead branch are gone (decisions.md, 28 Oct later).

- **The host-owned agent profile and the inbox** (29 Oct, decisions.md): what an agent LOADS now comes from
  `~/.config/agentbox/<agent>/profile/`, content-hashed, snapshotted per hash and mounted **read-only** at
  the agent's load paths under every preset — lockdown included, since host-owned is a different trust class
  from repo-shipped code. A session cannot change what later sessions load; what it wants to keep goes to
  the per-project `inbox/`, which nothing loads, gets a provenance sidecar at teardown, and is reviewed with
  `ab profile harvest` / `promote` / `dismiss` (the profile is a git repo, so a promotion is a commit). The
  end-of-session scan also learned that a repo's `.claude/`, `.omp/`, `CLAUDE.md`, `AGENTS.md` and
  `.mcp.json` are files that run later. `ab project rm`/`add` now say what state they keep and reattach, and
  `ab project purge` is the one command that deletes it. OMP has the inbox but not the read-only load paths:
  autolearn writes exactly there, and whether it can be pointed elsewhere could not be determined from
  inside the sandbox (TODO.md).

- **OMP's profile load paths** (29 Oct, decisions.md): resolved from oh-my-pi's source — autolearn writes
  only to `agent/managed-skills`, never to `agent/skills` — so OMP now loads the host-owned profile
  read-only at its five authored paths, in every preset. `managed-skills` is given to each session empty and
  harvested into the project's inbox at teardown, so autolearn keeps working and what it learns waits for
  `ab profile harvest`. The scan also picked up `.agents/`, `.codex/`, `.opencode/` and `.github/skills/`.
  Still unverified by the builder: whether the installed OMP has that split (`~/.omp` is off limits to a
  session) — the check and the one-line revert are in TODO.md.

- **One live session per project** (29 Oct, decisions.md): a lock file in the project's state dir, taken by
  `ab run` before any work and released by teardown on every path that can run code; a stale holder is
  reaped and reported, a live one is never overruled, and an unreadable podman frees nothing. `ab attach
  <project>` is the way back to a running session. Users name projects now (`ab attach web`,
  `ab session kill web`); session ids keep their suffix in logs, labels and inbox provenance. `ab run` on an
  unregistered name prints the `ab project add` command instead of registering anything.

## Next

The one remaining step is the **first real project** — `life-updates`, with Tim, registered `lockdown` first, run supervised; its live `deploy` stack stays untouched. All gates are cleared: the independent re-review is **done** (`reviews/2026-10-15-pre-real-project.md`, all 10 findings actioned), the live tests re-passed 16 Oct against `563f352`, and `make test`/`make itest` are green. Optional/deferred work (not blocking) lives in `docs/TODO.md`.

## Open decisions (Tim)

- **Lockdown OMP:** resolved 15 Oct — keep `--no-extensions` (decisions.md).
- **`--with-secrets`:** resolved 16 Oct — **dropped**; masks can no longer be turned off (decisions.md).

## Watch items

- **Egress flake, caught with diagnostics (5 Oct, covers-audit itest run):** `test_step2_network.test_locked`, "proxied public name resolving to 10.0.0.103 refused": squid's own log shows it spent 19,995 ms on `10-0-0-103.nip.io:8088` before the probe gave up at 20 s (`NONE_NONE_ABORTED`), while the next request to the same name was refused at once. The slow part is squid resolving nip.io (an external wildcard DNS service) to evaluate the private-address rule; the host resolved it in 629 ms. Passed on rerun. Candidate fix (not applied, for Tim): give squid a `dns_timeout` well under the probe's 20 s, or point that selftest case at a name we control.
- **Egress flake, measured (5 Oct):** `make egress-soak` showed 0 failures in 600 requests idle and 600 under `make itest`. squid used only IPv4 and stayed far from its memory and CPU limits. The latency tail (up to about 5 s on parallel requests) is the same straight from the host without agentbox, so it is the host's uplink. Not reproduced; diagnostics are in place for the next occurrence (decisions.md, 5 Oct).
- **Egress flake, third test (2 Oct, last full run):** `test_step3_omp.test_chromium_follows_the_egress_mode`: with open egress, Chromium did not load example.com through squid. It failed once more alone, loaded the page in a manual rerun, then passed twice. Three tests hit it today (compose pypi 503, step 2 selftest 20 s timeouts, this one), always on squid's upstream side. The next step is to collect squid's cache.log (now kept) from a failing session and test the IPv6 suspect directly.
- **Egress flake, with diagnostics (2 Oct, after trial 2):** in a full itest run, a step 2 selftest (packages) had `CONNECT api.anthropic.com:443` and a nip.io request get no answer within the probe's 20 s (`000 after 20011ms`). squid had logged both requests, so it accepted them and then did not answer in time. The host resolved api.anthropic.com to both IPv4 and IPv6 in 0 ms. That points at squid's upstream connect stalling (an IPv6 attempt is one suspect), not at the gate or DNS on the host. Failure details now carry the proxy's log lines themselves.
- **Egress flake, again (2 Oct):** in one full itest run, squid answered `503 Service Unavailable` to a build's CONNECT to pypi.org (`test_step5_compose`). The same test passed alone four times, every time over IPv4 (`151.101.x.223`). The host resolves pypi.org to IPv6 first, and the session networks are IPv4-only, so one suspect is squid trying IPv6 only; unconfirmed. Kept proxy logs now include squid's stderr (cache.log), which says why an upstream connection failed, and the compose test prints the proxy's pypi/503 lines when it fails.
- **Selftest egress flake (1 Oct):** three proxied CONNECTs (api.anthropic.com, pypi.org, files.pythonhosted.org) went unanswered (`000`) in one step-4 selftest; it passed alone and in a full rerun. Next time, the failed lines carry the proxy's log lines, the DNS answer and timings, and `<logs>/<session>-*-selftest-diag.log` has the details.

## Waiting on Tim

- **The trial** (`docs/TRIAL.md`): **done** 15 Oct — `ab selftest` 105 pass / 0 fail, `make trial-check` all checks passed (the secret-leak scan clean).
- **`make claude-live` / `make omp-live`:** re-passed 16 Oct against `563f352` (after the review fixes). claude-live egress=locked (search ok, fetch denied, bash ok, token nowhere); omp-live egress=packages (PyPI allowed, non-allowlisted hosts denied, key nowhere). `make test` (510) and `make itest` (165) green on the same tree; live-freshness clear.
