# TODO & tech debt

**Nothing in this file blocks use.** It is the durable backlog: what is deliberately not built, what needs a
decision, and what is worth doing when a change next touches the area.

- **What the tool does and does not do** → `docs/progress.md`, the Status table (single source of truth).
- **Why something is the way it is** → `docs/decisions.md` (dated entries; every closed item here has one).
- **The spec** → `docs/design.md`.

**State as of 6 November 2026.** The image restructure is done and exercised end to end on the host:
`node:26-bookworm-slim` → `agentbox-base` → `agentbox-{claude,omp}` → optional personal layer, each agent
CLI installed by agentbox at a Makefile pin the build asserts (node's major included, since a
digest pin can silently override the tag). `make test` (1150), `make itest`,
`make claude-live`, `make omp-live`, both `ab selftest`s and both `ab image build`s green; the unit suite
runs with zero ResourceWarnings. Ten of eleven dead-enforcement items removed (D-E11 is a deliberate knob, and stays). The third preset/settings
audit (F-1 … F-12) is fully closed. `config.yml` is agentbox's own and the Playwright cache is gone, both
confirmed on the host (5 Nov: `make itest` 186 OK, `make omp-live` pass with the browser probe, then again
after `make omp-image && ab image build omp` so the image's own change is exercised too). `make claude-live`
re-recorded the same day. `ab session gc` now sweeps personal image layers and profile snapshots as well as
session-built images.

**6 November: `~/.omp` is no longer an input.** `ab profile install omp` builds a host-owned tree from
the profile's `plugins.toml` (marketplace plugins pinned by sha, npm packages at exact versions, git
checkouts linked), in one hardened container behind the session proxy, and sessions mount it **read-only**.
`make itest` and `make omp-live` both pass, which settles the question that could have forced a redesign:
**OMP tolerates a read-only `~/.omp/plugins`** — proved by a real read-only bind mount, not a `chmod`
stand-in. About 160 lines of whitelist went with the host directory, and `omp_home` is a retired setting.

**No outstanding host run.** `make test` (1150), `make itest`, `make claude-live`, `make omp-live`, both
`ab selftest`s, both `ab image build`s and `ab profile install omp` are all green on the host as of
6 November.

**How each open item is written:** *what it is* → *why it matters* → *done looks like* → *where to start* →
*the gate*. That is deliberate: these sit for weeks, and "where to start" is what a cold reader needs.

---

## 1. Open questions — a decision is needed before any code

### 1.1 MAGS — three session features, not an adapter

**Decided 5 Nov: MAGS runs as an OMP extension, and agentbox grows the three things it needs.** Not a
`--agent mags` adapter, and not a standalone tool outside `ab`. The evidence is in omp-env's
`manifest.json`: `mags-codedev` is a git entry with `extensionDir: mags-codedev-extension`, so **MAGS
ships an OMP extension** — "part of OMP" is how it is built, not a convenience. It is a `[[repo]]` entry in the
profile's `plugins.toml`; dropping that entry is how it stays out of sessions.

**Why not standalone on the host.** Its job is to run model-generated code and its own generated tests. On
the host that means no gate (the spikes proved any container on a default or plain bridge network reaches
the LAN, including this machine's live stacks), no secret masks over `.env`, and no end-of-session scan. Of
everything here, a tool that executes generated code is the worst candidate for running outside the
sandbox.

**Why not an adapter.** The adapter interface is for *harnesses*: an interactive agent CLI with its own
config, per-project state, instructions file and profile mounts. MAGS is a batch tool that calls models
itself and needs none of that, while the things it does need are session and gate features that sit
outside the adapter interface anyway.

**What to build — three features, each useful beyond MAGS:**

1. **A sealed network for gate-created containers.** Today the gate pins every container the agent creates
   to the session network *and* injects `HTTP(S)_PROXY`, so "reaches nothing" needs the proxy taken away,
   not just the LAN. A project setting (`spawned_network = "sealed"`) giving an `--internal`,
   `isolate=strict` network with no proxy environment. Any "run generated code" workflow wants it.
2. **A combined keys file on tmpfs** (`MAGS_KEYS_FILE`: 0600, `KEY=value` lines). agentbox already mounts
   per-provider key files read-only and never in the environment; this renders one file from them into the
   agent's tmpfs. What makes it safe is already enforced: never inside a grant, which grantcheck refuses.
3. **The scan must tell a worktree from a new repo.** `base_dir: .mags-codedev` holds git worktrees in the
   project root, so the scan prints "NEW GIT REPO" per module. The right fix is general: a `.git` *file*
   pointing inside the project is a worktree. Better than a skip list, which would hide a real new repo.

**Still needed from Tim:** the four answers below, which are the spec. Nothing else blocks it.

**Already built from the approved plan.** The gate's network-pinning test (`tests/unit/test_gate.py`: a
create naming no network at all is still pinned to the session network, on both APIs); the scan's HEAD/ref
movement reporting; and `base_branch` as a project key.

**What reading MAGS's own config established** (4 Nov, from `mags_codedev/config.template.yaml`) — carry
these into the spec:

- `test_runner: "auto"` auto-detects `podman > docker > apptainer > singularity > local`. The plan says test
  containers go through the gate and `local` is refused, so the session must **force
  `test_runner: podman`** rather than rely on `auto`, which would silently fall back to `local`.
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

---

## 2. Ready to implement — scoped, no decision needed

### 2.1 `ab profile install --resolve`: print the current sha of every declared entry

**What.** `plugins.toml` pins every `ref` to a 40-character sha, deliberately (a catalog is mutable, so a
branch is not a pin). Bumping one means looking it up: `git ls-remote <url> <branch>` for a repo or a
catalog, the npm registry for a package. `--resolve` should do that for every entry and print
`declared → current`, so a bump is one command and a diff in the profile repo.

**Why it matters.** Low, but it is the friction that makes people reach for a branch. The rule holds
better if keeping it is cheap.

**Done looks like.** `ab profile install omp --resolve` prints one line per entry and changes nothing. It
runs in the same hardened container the install uses (network belongs in a container, not in `ab`), needs
no clone — `git ls-remote` and an npm registry read are enough — and says plainly when an entry is already
current.

**Where to start.** `agentbox/installer.py` (`Install.container_args`, `_script`: this is a second, much
smaller script), `agentbox/plugins.py` for the entry kinds, `cmd_profile_install` for the flag.

**The gate.** `make test`; by hand on the host once, since it reaches the network.

### 2.2 Unify the duplicated git-identity lookup

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
| D-E9 | the network-less `Session` path | **removed, 5 Nov** (decisions.md). `Session` and `NetSession` are one class; `make_session` has one thing to return. |
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
| 4 Nov | `ab run --no-profile` / `profile = "off"` |
| 4 Nov | `AGENTBOX_SESSION` in every session |
| 4 Nov | The profile hash/snapshot narrowed to the load paths |
| 4 Nov | omp-live says *why* it failed; a timeout no longer costs the whole report |
| 4 Nov | The test log runs clean — zero ResourceWarnings |
| 29 Oct | One live session per project (`projectlock.py`) + `ab attach` |
| 29 Oct | The host-owned profile, the inbox, `ab profile harvest/promote/dismiss`; OMP's load paths and autolearn harvesting |
| 28 Oct | Third preset/settings audit, F-1 … F-12 |
| 27 Oct | One settings table (`projectkeys.py`), retiring five parallel per-key lists |
