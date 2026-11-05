# TODO & tech debt

**Nothing in this file blocks use.** It is the durable backlog: what is deliberately not built, what needs a
decision, and what is worth doing when a change next touches the area.

- **What the tool does and does not do** → `docs/progress.md`, the Status table (single source of truth).
- **Why something is the way it is** → `docs/decisions.md` (dated entries; every closed item here has one).
- **The spec** → `docs/design.md`.

**State as of 5 November 2026.** The image restructure is done and exercised end to end on the host:
`node:26-bookworm-slim` → `agentbox-base` → `agentbox-{claude,omp}` → optional personal layer, each agent
CLI installed by agentbox at a Makefile pin the build asserts. `make test` (1053), `make itest` (186),
`make claude-live`, `make omp-live`, both `ab selftest`s and both `ab image build`s green; the unit suite
runs with zero ResourceWarnings. Nine of eleven dead-enforcement items removed. The third preset/settings
audit (F-1 … F-12) is fully closed. `config.yml` is agentbox's own and the Playwright cache is gone, both
confirmed on the host (5 Nov: `make itest` 186 OK, `make omp-live` pass with the browser probe).
**Outstanding host run:** `make claude-live` is flagged stale by `freshness.py` (`adapters/omp.py`,
`hardening.py`, `session.py` changed since the recorded pass).

**How each open item is written:** *what it is* → *why it matters* → *done looks like* → *where to start* →
*the gate*. That is deliberate: these sit for weeks, and "where to start" is what a cold reader needs.

---

## 1. Open questions — a decision is needed before any code

### 1.1 `ab profile install` — the last thing between agentbox and decommissioning omp-env

**What.** Sessions mount OMP's plugins from the host's `~/.omp/{plugins,repos,pylibs}`, which only
`omp-env install` produces — on the host, with `--network=host` and `~/.omp` read-write. agentbox no longer
*builds on* omp-env (4 Nov), but it still *depends on* it for plugins.

**Narrowed on 4 Nov.** `agent/config.yml` moved to `~/.config/agentbox/omp/config.yml` and the Playwright
cache was dropped, so the only reason a session reads the host's `~/.omp` at all is now the
plugins/repos/pylibs snapshot plus `bin/mags-codedev` — a wrapper *into* `repos/`, so it belongs with this
decision rather than before it. `test_omp.test_nothing_from_the_host_omp_but_the_plugin_snapshot_and_the_mags_wrapper`
pins that: anything else creeping back in fails a test.

**The `--no-profile` read, 5 Nov.** `ab run --agent omp --project agentbox-profile-omp --no-profile -- -p
--no-title 'Reply with exactly: AB_OK'` answered `AB_OK`, with no `config.yml`, no plugins, no `repos`, no
`pylibs` and no provider key but the local model's. So **OMP with nothing from the host's `~/.omp` starts,
reaches a model and completes a turn** — the extensions are not load-bearing for a plain turn, and this
question is not blocking use. What that run does *not* settle is whether a workflow you actually care about
needs one of them: a single `-p` turn exercises no tool, no statusline, no MCP server and not
`mags-codedev`. **The deciding evidence is therefore use, not another probe:** run real work with
`--no-profile` for a while, and either nothing is missed (delete the whitelist machinery) or you hit a
specific extension (which is the input `ab profile install` needs anyway — see decision 3 below,
reproducibility, which cannot be answered without knowing what is being installed).

**Why it matters.** The stated end state is that omp-env is decommissioned once `ab` is finished; it is a
backup for now. Until this exists, that cannot happen. There is also live drift: plugins installed against
whatever OMP the host's omp-env has, meeting the OMP that `images/omp/Containerfile` pins. The known symptom
is a plugin failing to load (`pi-statusline`'s `stripTerminalSequences`, seen 4 Nov). **Mitigation until
then: keep `OMP_VERSION` near what omp-env installs with.**

**Done looks like.** A profile-owned `plugins.toml` (already in `profile.BUILD_ITEMS`, so it is already
hashed and snapshotted — that was put there for this). `ab profile install [--agent omp]` installs from it
**in a gated session**, against the image's own OMP/node/Bun, into a host-owned tree that sessions mount
read-only. `~/.omp` stops being an input.

**Decisions to make first** — this is not shovel-ready, and these are the reasons:

1. **Where the installed tree lives.** Candidates: `~/.config/agentbox/omp/plugins/` (host-owned config,
   matches the profile) or `<data>/profiles/omp/plugins/<hash>/` (content-keyed, matches the snapshot
   mechanism and gets staleness detection for free). The second composes better with how profiles work.
2. **What network the install gets.** It needs npm and git, so `packages` egress through squid — the point
   being that the install goes through the same gate and allowlist as everything else instead of
   `--network=host`. But a plugin install is not a project, and the session machinery is built around one.
   Either synthesise a project, or build a narrower "one container, session proxy, no grant" path.
3. **Reproducibility.** `plugins.toml` should pin versions and the install should emit a lockfile, or
   "install" means something different every time it runs.
4. **How sessions pick it up.** `omp.omp_mounts` would read the new location instead of `~/.omp`. That
   function is security-sensitive — the whitelist, `_plain`/`_version_dir` validation, the credential
   hard-link check, the mid-start `unchanged()` recheck — and most of it exists *because* `~/.omp` is a
   directory agentbox does not control. Owning the tree would let much of it go, which is a simplification
   worth having but is a careful change, not a mechanical one.

**Where to start.** `agentbox/adapters/omp.py` (`omp_mounts`, `PROVISIONED`, `provisioned_targets`,
`check_no_credentials`), `agentbox/profile.py` (`BUILD_ITEMS`, `snapshot`), `agentbox/cli.py` (the
`ab profile` subparser). Read `docs/decisions.md` → "What the OMP session mounts from ~/.omp" first: it says
why each piece of the current whitelist is there, and each reason has to be answered or retired.

**The gate.** `make test`; `make itest` (the OMP profile and managed-skills round trip); `make omp-live`
with the real plugins, which is the only thing that proves a plugin actually loads.

### 1.2 The MAGS adapter — parked on a complete spec

**What.** An adapter for MAGS-CodeDev (`github.com/TimothyStephens/MAGS-CodeDev`), planned and approved in
outline, then parked.

**Why it is parked.** Waiting on a complete spec. One design change since the plan was approved: its
generated test containers get a **sealed network (no proxy)**, not the session's internal network. That
followed from a finding — the session network carries the squid proxy *and* the gate injects
`HTTP(S)_PROXY` into every container it creates, so "reaches nothing" needs the proxy taken away, not just
the LAN.

**Already built from the approved plan.** The gate's network-pinning test (`tests/unit/test_gate.py`: a
create naming no network at all is still pinned to the session network, on both APIs); the scan's HEAD/ref
movement reporting; and `base_branch` as a project key.

**What reading MAGS's own config established** (4 Nov, from `mags_codedev/config.template.yaml`) — carry
these into the spec:

- `test_runner: "auto"` auto-detects `podman > docker > apptainer > singularity > local`. The plan says test
  containers go through the gate and `local` is refused, so the adapter must **force `test_runner: podman`**
  rather than rely on `auto`, which would silently fall back to `local`.
- `docker_test_image: "mags-dev-env:latest"`, "auto-built if not found" — that build goes through the gate,
  so the name needs to be in the project's `dev_images` allowlist or be a session build the gate records.
- `base_dir: ".mags-codedev"` holds **git worktrees inside the project root**. `scan.worktrees()` walks for
  `.git`, so a MAGS run would produce a "NEW GIT REPO" line per module unless the adapter arranges a skip.
  Decide this in the spec.
- `MAGS_KEYS_FILE` is a 0600 file of `KEY=value` lines that "should live outside the project directory" —
  which matches the plan's keys-on-tmpfs item exactly.

**Structurally confirmed already:** a tmpfs keys file in the agent container cannot be reached by a sibling
container. The gate's bind allowlist is the grants only (`session.bind_roots`), and a tmpfs in the agent's
mount namespace is not a host path at all. This holds **provided the keys file is not written inside a
grant** — which is also what grantcheck would refuse.

### 1.3 D-E9 — remove the network-less `Session` path, or keep it?

**What.** `make_session` always returns `NetSession`; the base `Session` is never instantiated, so a whole
path is unreachable: `Session.network_args`, `start_infra`, `pin_args`,
`add_network_cases`→`runner.network_none_cases`, `egress = "none"`, `sandbox_notes`' `"none"` branch, the
`isinstance(self, NetSession)` checks, and `hardening.py`'s `--network=none` allowance.

**Why it is a decision, not a cleanup.** Eight interlocked sites across `session.py`, `runner.py` and
`hardening.py`, and removing it **forecloses a network-less session for good**. Everything else in the
dead-code backlog was a local deletion; this one changes what agentbox could be asked to do.

**What would have to be true.** No planned feature wants a session with no network at all — worth checking
against §3 (`analysis-offline` uses `locked` egress, not no network, so it is not a user). If the answer is
"no", remove it as one change with tests; if "maybe", leave it and record that here.

---

## 2. Ready to implement — scoped, no decision needed

### 2.1 `ab session gc` does not sweep the two newest kinds of leftover

**What.** `gc_images` filters on `{**settings.labels, "agentbox.role": "dev"}`, so it prunes
*session-built* images older than `image_keep_days` (keeping the newest per tag). Two things sit outside
that sweep:

- **Personal image layers.** Every harness rebuild or `Containerfile` edit gives a new
  `localhost/agentbox-<agent>-user:<key>` and leaves the previous one in storage. They carry
  `agentbox.user-layer=<agent>` and `agentbox.harness-id=<id>`, and neither `agentbox.role=dev` nor the
  settings labels — so nothing collects them.
- **Profile snapshots.** `<data>/profiles/<agent>/<hash>/` gains a directory per distinct profile content,
  kept so a running session's read-only mount stays valid. Small (text), but unbounded.

**Why it matters.** Low: disk only. But both are now produced *routinely* rather than rarely — the layers
every time an image moves, the snapshots every time a skill is edited.

**Done looks like.** `ab session gc` removes user layers that no live session is running and that are not
the current key for their agent, and profile snapshot directories no live session mounts. Both need the
liveness answer `gc` already has: **a sweep must never delete what a running session has mounted.**

**Where to start.** `session.gc_images` (the pattern to copy), `session.sessions()` / the live-session query
for what to protect, `userimage.USER_LAYER_LABEL` and `profile.user_image_ref` for the current key,
`settings.profiles_dir`. Consider adding `agentbox-<agent>-user` images to `make clean`'s label sweep.

**The gate.** Unit tests against the fake podman (it honours `--filter label=` on `images` since 4 Nov);
`make itest` for the real sweep. Mutation-check that a layer or snapshot belonging to a *live* session is
not removed — that is the assertion that matters.

### 2.2 `tests/manual/podman_formats.sh` is only ever run by hand

**What.** The one check of agentbox's `--format` templates against real podman. A unit test keeps the script
in step with the code, and the fake podman refuses the one template podman 5.7 rejects — but the script
itself runs only when someone remembers.

**Why it matters.** This is the class that cost a whole build once: `network ls --format
'{{index .Labels …}}'` is rejected by podman 5.7, the call was swallowed, and network-based session
detection had never worked. A template is a per-subcommand contract with podman's internals; only running it
proves anything.

**Done looks like.** It runs inside `make itest`.

**Why it is manual today.** It reads whatever objects happen to exist on the host rather than creating its
own, so it cannot assert much; under `itest` it would need its own `abt-` objects to inspect.

**Where to start.** `tests/manual/podman_formats.sh`, `tests/unit/test_podman_formats.py` (the sync test and
its `READ_ONLY` list), `tests/integration/harness.py` for making `abt-` objects to point it at.

### 2.3 Unify the duplicated git-identity lookup

**What.** `initproj.py` (~:274) and `session.py` (~:197, `git_identity`) each read the author name/email,
slightly differently.

**Why it matters.** Low — a cosmetic inconsistency between commits made inside a session and by `ab init`.
Review finding 10 (pre-real-project review, 15 Oct). Unify when next touching either.

---

## 3. Features deferred by design

Not bugs and not oversights — each was considered and left out. The Status table in `progress.md` is
authoritative; this is the *why*.

| Feature | Why not built |
|---|---|
| `ab deploy` | Step 6. Would validate a compose file, check `.env` against `.env.dev`, show the diff since the last deploy, confirm, then start the live stack through a sockguard deploy profile — never the raw socket. |
| `ab ws` (subcommand) | `contrib/ab-ws` does the workspace job from outside the CLI, starting agents only through `ab run`, so the sandboxing stays in `ab`. A subcommand would add surface for no new capability. |
| `ab allow` / `reload` / `resume` / `outbox` | `allow` was "loosen a *running* session", which the one-session-per-project lock and the per-session posture make the wrong shape; settings changes take effect from the next session by design. |
| podman `exec` tier | Refused in code (`session.py`: "exec on the live stack is not built"). `observe` reads the live stack, `dev` runs the agent's own; exec on the live stack is the dangerous middle, with no use case yet. |
| Model key gateway | Sessions use the Claude subscription token and per-provider key files. A per-model API-key gateway is a bigger build with no present need. |
| `masked_paths` (per-project masked dir) | Only secret-name masks and the `secrets/` tmpfs exist. `docs/agentbox-masked-paths.md` is a proposal, marked as such. |
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
| D-E9 | the network-less `Session` path | **a decision, not a cleanup** — see §1.3 |
| D-E11 | `gate.py` `SYSCTL_ALLOWED = ()` | **keep.** A deliberate knob that makes two checks always refuse — a constant branch, not dead code. |

### 4.2 Duplicated enforcement logic — consolidate only when a bug traces to the drift

Do not merge pre-emptively; add tests pinning the shared behaviour first.

- **Secret-name test, three copies:** `mounts._masked` (includes custom swap targets),
  `grantcheck._is_secret_name` with its own `SECRET_FILES` (`grantcheck.py` vs `mounts.py`), and inline in
  `gate.unmasked_secret_under` (ignores swap targets). **Known drift:** a custom swap target (e.g.
  `secrets.env`) is masked, but the gate's N6 check does not flag one created mid-session. That is the
  accepted host-writes-during-session limitation, not a bug to fix here.
- **`mask_skip` resolved twice:** `mounts._rel_skips` and `grantcheck._under_mask_skip`. (It was three;
  `grantcheck._rel_skips` went with D-E7.)

### 4.3 Uncovered gate HTTP error paths — cover opportunistically

Reachable only through the HTTP/plumbing layer, mostly fail-closed error returns. Add a test when touching
the area; none is a known defect.

| ID | What is untested |
|---|---|
| E5 | the `remove` failure path (the fake `raw_request` always returns 200) |
| E6 | grant-volume create failure; a name collision with a non-grant volume; named-volume create failure |
| E9 | `Content-Length` with `Transfer-Encoding`; an unsupported transfer encoding; a build context over 8 GB; a body/head over the limit |
| E10 | build-context errors: unreadable dockerfile param, no Containerfile, Containerfile not a file, bad tar, `FROM` with no image, `# escape=` |
| E11 | network create with `ignoreIfExists`; an unrecognised `/create` endpoint |
| E12 | `mount_violations`' "cannot confirm what is mounted" branches |

### 4.4 Support refactors (no behaviour change)

Do these **only alongside a change that already touches the area**, with the suite as the net.

- **`gate.py` (~2,527 lines):** one `Gate` class mixes HTTP plumbing, policy, image handling and mount
  verification. Long functions: `handle` (~110 lines), `decide` (~75), `rewrite_libpod_container` (~70),
  `check_common_libpod` (~57). A pod start inspects the pod three times. Splitting plumbing from policy is
  pure refactoring.
- **`session.py`:** notes text, `ls`/idle and gc are mixed with the lifecycle; moving
  `sandbox_notes`/`dev_notes` out is pure refactoring.

---

## 5. Watch only — not an action

- **Egress flakes** (squid upstream stalls; IPv6 suspected). Diagnostics are in place; act only if they
  recur. Candidate fix: a squid `dns_timeout` under the probe's 20 s. Full notes in `progress.md` → Watch
  items; `make egress-soak` is the measurement.
- **`make omp-live`'s local model is slow** — about 257 s for one turn, which is why that check used to fail
  intermittently. **Accepted** (4 Nov): the check stays on the real LAN endpoint, because exercising it is
  the point. The report names the latency now, so a repeat reads as "slow model" rather than an unexplained
  FAIL. `AB_LIVE_MODEL=<provider/model>` is there for a one-off.
- **omp-env / image plugin drift** — see §1.1. Mitigation: keep `OMP_VERSION` near what omp-env installs.

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
| 4 Nov | The image restructure: `agentbox-base`, both agent layers installing their own CLI at an asserted pin, Bun pinned, node 22→26 |
| 4 Nov | `ab image build <agent>` — a personal layer keyed on the harness image ID + Containerfile, never run when stale, checked against its own base before it is tagged |
| 4 Nov | `base_branch` as a project key (agentbox does not own the value — MAGS does) |
| 4 Nov | Nine dead-enforcement items, each resting on a test of the live guard |
| 4 Nov | The scan reports HEAD and ref movement, not only what changed on disk |
| 4 Nov | `ab run --no-profile` / `profile = "off"` |
| 4 Nov | `AGENTBOX_SESSION` in every session |
| 4 Nov | The profile hash/snapshot narrowed to the load paths |
| 4 Nov | omp-live says *why* it failed; a timeout no longer costs the whole report |
| 4 Nov | The test log runs clean — zero ResourceWarnings |
| 29 Oct | One live session per project (`projectlock.py`) + `ab attach` |
| 29 Oct | The host-owned profile, the inbox, `ab profile harvest/promote/dismiss`; OMP's load paths and autolearn harvesting |
| 28 Oct | Third preset/settings audit, F-1 … F-12 |
| 27 Oct | One settings table (`projectkeys.py`), retiring five parallel per-key lists |
