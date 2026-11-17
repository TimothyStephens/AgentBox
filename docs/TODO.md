# TODO & tech debt

**Nothing in this file blocks use.** It is the durable backlog: what is deliberately not built, what needs a
decision, and what is worth doing when a change next touches the area.

- **What the tool does and does not do** → `docs/progress.md`, the Status table (single source of truth).
- **Why something is the way it is** → `docs/decisions.md` (dated entries; every closed item here has one).
- **The spec** → `docs/design.md`.

**State as of 16 November 2026.** The image chain is `node:26-bookworm-slim` → `agentbox-base` →
`agentbox-{claude,omp}` → optional personal layer, each agent CLI installed at a Makefile pin the build
asserts. Plugins for both agents come from `ab profile install <agent>` (the profile's `plugins.toml`,
installed in a sandbox, mounted read-only); `~/.omp` is not an input and `omp_home` is a retired setting.
Which gates are green, and at which commit, is recorded in `docs/progress.md` (Status) and the dated run
records in `docs/decisions.md`; this file keeps no counts. The independent review of 16 Nov and the fixes it
led to are one decisions.md entry ("Review 2026-11-16"); the host gates on that tree (`make check`, `itest`,
`claude-live`, `omp-live`, both profiles deployed and installed, both selftests) passed the same day.

**How each open item is written:** *what it is* → *why it matters* → *done looks like* → *where to start* →
*the gate*. That is deliberate: these sit for weeks, and "where to start" is what a cold reader needs.

---

## 1. Open questions — a decision is needed before any code

None open. MAGS-CodeDev was decided on 15 Nov: it stays outside agentbox, as its own container, and
nothing of it remains in the launcher (decisions.md, 2026-11-15). The three session features its plan
listed (a sealed network for gate-created containers, a combined keys file on tmpfs, worktree-aware scan)
are not asked for by anything else and are not on the backlog.

---

## 2. Ready to implement — scoped, no decision needed

### 2.1 Done 17 Nov: `ab profile install <agent> --resolve`

Prints each entry's pin beside its source's current one (`git ls-remote <url> HEAD` for marketplaces and repos,
the registry's `latest` for npm), in the install's hardened container with nothing of the host mounted, and
changes nothing; it prints the `ref = "…"` / `version = "…"` line to paste. Exit 1 if an entry could not be
checked. The script was run for real against the omp profile's sources from a session (3 of 7 entries had
newer pins). Unverified on the host: one `ab profile install omp --resolve`, which adds the container.

### 2.2 Done 17 Nov: `make omp-live` refuses at once when no model is chosen

Both halves: with neither `AB_LIVE_MODEL` nor `~/.config/agentbox/omp/config.yml` it exits 2 before making
anything, naming both fixes; and `config.yml` is now copied into the run's config beside `models.yml`, so a
default model set there is actually used (it was not copied before, so it could not have helped).
Unverified on the host: one `make omp-live`.

### 2.3 Done 16 Nov: regression review of the 16 Nov fixes

Four fresh reviewers over `c782560..`; seven findings fixed with tests (`docs/reviews/2026-11-16-regression.md`).
One left for a decision: `--podman none` keeps the preset's `live = observe`.

---

## 3. Features deferred by design

Not bugs and not oversights — each was considered and left out. The Status table in `progress.md` is
authoritative; this is the *why*.

| Feature | Why not built |
|---|---|
| `ab deploy` | Step 6. Would validate a compose file, check `.env` against `.env.dev`, show the diff since the last deploy, confirm, then start the live stack through a sockguard deploy profile — never the raw socket. |
| `ab allow` / `reload` / `resume` / `outbox` | `allow` was "loosen a *running* session", which the one-session-per-project lock and the per-session posture make the wrong shape; settings changes take effect from the next session by design. |
| podman `exec` tier | Refused in code (`project.py` and `projectkeys.py`: "exec on the live stack is not built"). `observe` reads the live stack, `dev` runs the agent's own; exec on the live stack is the dangerous middle, with no use case yet. |
| Model key gateway | Sessions use the Claude subscription token and per-provider key files. A per-model API-key gateway is a bigger build with no present need. |
| Toulmin: restart a dead `tm session serve` | Recorded as a gap by the contract (1.5). Until built, Toulmin's `tm status` tells the owner the socket is dead and which command to run. |
| Clone mode with the dev podman tier | Refused at session start. The gate would translate every bind source under the owner's path into the clone (grant volume device, cover ids, foreign-uid check, unmasked-secret walk). decisions.md, 10 Nov. |
| The masked-paths spec's in-session errno probe (`docs/agentbox-masked-paths.md` §5.1) as an itest | The `mask` setting itself is built (10 Nov); the probe stays manual until a change touches the covers tests. |
| `--data-bind` (live data in `./data`) | Contract already fixed: mask `./data` via covers, the user names `--data-backup <target>`, agentbox masks and does not back up. The named-volume default serves until a real project forces it. |
| `dev/data`→`data` overlay | Deliberately not built; the substitute mechanism stays `.env`-only unless a real project forces it. |
| Pi / Codex adapters | Step 8. The adapter interface is the only per-agent code and has carried two agents through a restructure, so a third is mostly configuration. |
| Caps / devices opt-in | The gate refuses `cap_add`, `devices` and security options under every preset; the `caps = opt-in` preset value is inert and `ab project show` says so. |
| A profile curator | `ab profile harvest` lists, groups and marks; promoting is a human reading a skill. A curator would need the profile **read-write** (which no normal session gets), a prompt that is itself security-reviewed, and a record of what it rejected and why. **Deliberately not started until `harvest` has been lived with** — right now we would be guessing at the hard part. |

### 3.1 A read-only project root, for a planner preset

Separate because it is the one deferred feature with a real design cost, and because `research` *reads* as
though the project were read-only and is not.

**What would be needed**, in order:

1. A new mount mode in `mounts.plan` — the root is an unconditional read-write bind under every preset today
   (`rw_grants = [root] + data_rw`).
2. **The gate**, which is the expensive part: its read-only-grant handling forces `ro` on binds and on
   grant-volume SubPaths from `ro_roots`, and the after-start mountinfo check and the cover pins all assume
   a writable root. That is security-reviewed ground (decisions.md, 5–6 Oct).
3. A decision on **one writable subdirectory inside a read-only root** (for the plan itself). The grant
   validator does not model "a writable hole inside a read-only grant" at all.
4. A preset to expose it. `grantcheck` largely becomes moot for such a project — a read-only root cannot be
   damaged — which is worth stating rather than discovering.

**Today's mitigation:** commit before a `research` session; the per-session rsync snapshot is a
within-session undo (explicitly not a data backup), so unwanted edits are a `git diff` away.

---

## 4. Maintainability backlog

Deferred work with a stated trigger, carried from `docs/reviews/2026-10-06-cleanup.md`. Line numbers drift —
treat them as a starting point and confirm before acting.

### 4.1 Enforcement dead code — 9 of 11 removed (4 Nov)

The rule was "remove only after a real-use baseline, each with a test proving the **live** guard bites
first". The baseline arrived with the image restructure and nine went (decisions.md, 4 Nov — including the
two that were worth more than the deletion: `check_limits` had no test at all, and `hardening`'s `/dev/null`
branch returned *before* the grant check, so the tests that used it were skipping that check unintentionally).

Left:

| ID | Where | Status |
|---|---|---|
| D-E9 | the network-less `Session` path | **removed, 5 Nov** (decisions.md). `Session` and `NetSession` are one class; `make_session` has one thing to return. |
| D-E11 | `gate.py` `SYSCTL_ALLOWED = ()` | **keep.** A deliberate knob that makes two checks always refuse — a constant branch, not dead code. |

### 4.2 Done 17 Nov: duplicated enforcement logic

The secret-name list and matcher were made one on 16 Nov (item 28), and `mask_skip` one resolver (item 56).
The "known drift" recorded here was not real: every swap target must be a masked name (F-12), so the gate's
late-secret check (N6) already found a mid-session `secrets.env`; a test pins it. The real gap of the same
kind was the project's own `mask` names: the gate now gets them and refuses a bind containing a file or
directory of that name that appeared after the start.

### 4.3 Done 17 Nov: gate error paths covered

E5, E6, E9, E10, E11 and E12 have tests (`test_gate_error_paths.py`, `test_gate_http_build_errors.py`, 53
tests). Writing them found three fail-open or late-failing paths, all fixed: the `# escape=` directive read
outside buildkit's rule (it could hide a FROM), `ignoreIfExists` spellings, and a build chunk buffered before
the size limit (`docs/reviews/2026-11-17-gate-error-paths.md`).

### 4.4 Support refactors (no behaviour change)

Do these **only alongside a change that already touches the area**, with the suite as the net.

- **Module-level splits done, 16 Nov** (review item 54): `gate.py` lost its schema tables, Containerfile
  reader, mountinfo parsers and HTTP framing to `gate_*` modules; `session.py` lost notes, liveness, state,
  infra, lifecycle, the lock and the project record to their own modules; `cli.py` has one module per noun.
- **Left: the `Gate` class itself** (~1,600 of `gate.py`'s ~2,050 lines) still mixes policy, image handling
  and mount verification. Long methods: `decide` (~85 lines), `rewrite_libpod_container`
  (~70), `check_common_libpod` (~58). A pod start inspects the pod three times. Splitting it is pure refactoring.

---

## 5. Watch only — not an action

- **Egress flakes** (squid upstream stalls; IPv6 suspected). Diagnostics are in place; act only if they
  recur. Candidate fix: a squid `dns_timeout` under the probe's 20 s. Full notes in `progress.md` → Watch
  items; `make egress-soak` is the measurement.
- **`make omp-live`'s local model is slow** — about 257 s for one turn, which is why that check used to fail
  intermittently. **Accepted** (4 Nov). The recorded passes since 13 Nov use
  `AB_LIVE_MODEL=openrouter/z-ai/glm-5.3-flash` (about 50 s); see 2.2 for running without one.
- **OMP's Anthropic provider cannot reach api.anthropic.com from a session** (`getaddrinfo ENOTFOUND`, 11 Nov,
  again 16 Nov): OMP's Anthropic client resolves the name itself instead of going through `HTTPS_PROXY`; its
  OpenAI-compatible client is fine. Upstream OMP behaviour; not tested through a third-party route by choice
  (12 Nov). Act if Anthropic models are wanted in OMP sessions.
- **Session start time** (profiled 16 Nov, decisions.md). After the log-driver and stop-timeout changes the
  rest is podman's own container starts and the host's `podman images`. Sockguard's `podman run -d` varied
  0.7-6.7 s in the first profile (journald log driver) and 0.5-0.9 s in all 13 runs after the switch to
  k8s-file (17 Nov, from `perf-results/`). What is left is one sidecar `run -d` in about one start in four
  taking 1.7-3.2 s (sockguard-dev or holder, not the same one each time) with nothing of agentbox's
  running beside it: podman's own variance, likely its storage or database lock. No launcher change.
  The grants are walked about five times per start, but a bare walk is cheap (0.45 s on 98k files in a
  session, 17 Nov); the cost was per-file work inside them. Fixed 17 Nov: the mask plan (19.9 s to 0.9 s)
  and the scan record (16.4 s to 2.1 s, no executable hashed, tagged caches skipped). Left: grantcheck's
  per-file lstat (11.6 s there, for the foreign-owner warning and sizes); decide after toulmin's numbers on
  the host. `tests/perf/run.sh` re-measures.
- **Plugin tree vs image drift.** The tree's lockfile names the image it was built against and a session warns
  when that differs; after `make omp-image` or `make claude-image`, rerun `ab profile install <agent>`.

---

## 6. Known limitations — recorded, not TODO

Consequences of the threat model (**mistake containment + cross-project isolation**, *not* a wall against a
deliberately malicious or prompt-injected agent). Listed so they are not mistaken for gaps.

- **grantcheck catches common shapes only.** Secrets under other names, more DB shapes, exempt names and
  host-written files all get past it. The operational rule — keep secrets and live data out of the grant —
  is the real control; masks and the gate's cover checks are defence in depth behind it.
- **Host writes during a session are not re-caught.** The scan and masks are fixed at session start.
- **A project's own root subdirectory is accepted as its own `data_rw`** (audit F9, 27 Oct) — benign: both
  mounts are read-write and `mounts.plan` keeps the more specific one. Cross-*project* overlap is enforced
  in both directions.
- **Backups are a within-session undo, not a data backup** — same disk, only as fresh as session start.
  Never presented as one.

---

## Recently closed

Full reasoning for each is in `docs/decisions.md` under the date.

| Closed | What |
|---|---|
| 16 Nov | The independent review's findings fixed (about 100 commits, one test each); modules split along their seams; host gates, both profile deploys and both selftests pass on the result |
| 15 Nov | MAGS-CodeDev decided: it stays outside agentbox as its own container; a failed plugin install keeps the previous tree, and a `[[repo]]` without a `package.json` is refused before linking |
| 13 Nov | OMP's OpenAI-compatible providers (OpenRouter) work through squid |
| 9 Nov | `ab profile install claude` — the Claude profile's `plugins.toml` in agentbox's `[[plugin]]` format, each catalog cloned at its sha, mounted read-only at `/run/agentbox/claude/marketplaces` and declared to Claude Code through the copied `settings.json`; host gate (`make claude-live`) still to run |
| 5 Nov | D-E9: the network-less `Session` path removed — one session class, and the audit now requires the agent container to be on a session network |
| 6 Nov | `ab profile install` — plugins declared by the profile, installed in a sandbox against the image's own OMP, mounted read-only; `~/.omp` stops being an input and ~160 lines of whitelist go with it |
| 5 Nov | `tests/manual/podman_formats.sh` runs inside `make itest`, against the run's own abt- objects, with a skip counted as a failure |
| 5 Nov | `ab session gc` sweeps personal image layers and profile snapshots (current key and live sessions protected; `image_keep_days` is the rollback window) |
| 4 Nov | The image restructure: `agentbox-base`, both agent layers installing their own CLI at an asserted pin, Bun pinned, node 22→26 |
| 4 Nov | `ab image build <agent>` — a personal layer keyed on the harness image ID + Containerfile, never run when stale, checked against its own base before it is tagged |
| 4 Nov | `base_branch` as a project key (agentbox does not own the value — MAGS does) |
| 4 Nov | Nine dead-enforcement items, each resting on a test of the live guard |
| 4 Nov | The scan reports HEAD and ref movement, not only what changed on disk |
| 4 Nov | `ab session start --no-profile` / `profile = "off"` |
| 4 Nov | `AGENTBOX_SESSION` in every session |
| 4 Nov | The profile hash/snapshot narrowed to the load paths |
| 4 Nov | omp-live says *why* it failed; a timeout no longer costs the whole report |
| 4 Nov | The test log runs clean — zero ResourceWarnings |
| 29 Oct | One live session per project (`projectlock.py`) + `ab session attach` |
| 29 Oct | The host-owned profile, the inbox, `ab profile harvest/promote/dismiss`; OMP's load paths and autolearn harvesting |
| 28 Oct | Third preset/settings audit, F-1 … F-12 |
| 27 Oct | One settings table (`projectkeys.py`), retiring five parallel per-key lists |
