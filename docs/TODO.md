# TODO & tech debt

**Nothing in this file blocks use.** It is the durable backlog: what is deliberately not built, what needs a
decision, and what is worth doing when a change next touches the area.

- **What the tool does and does not do** → `docs/progress.md`, the Status table (single source of truth).
- **Why something is the way it is** → `docs/decisions.md` (dated entries; every closed item here has one).
- **The spec** → `docs/design.md`.

**State as of 19 November 2026: feature-complete for Claude, OMP and Pi.** The image chain is
`node:26-bookworm-slim` → `agentbox-base` → `agentbox-{claude,omp,pi}` → optional personal layer, each agent CLI
installed at a Makefile pin the build asserts. Each agent has a profile repo (`agentbox-profile-{claude,omp,pi}`)
and plugins from `ab profile install <agent>`. Which gates are green, and at which commit, is recorded in
`docs/progress.md` (Status) and the dated run records in `docs/decisions.md`; this file keeps no counts. What is
left is: host runs of work that is unit-tested only (section 2), features deferred by design (section 3), and
watch items (section 5).

**How each open item is written:** *what it is* → *why it matters* → *done looks like* → *where to start* →
*the gate*. That is deliberate: these sit for weeks, and "where to start" is what a cold reader needs.

---

## 1. Open questions — a decision is needed before any code

None. MAGS-CodeDev was decided on 15 Nov: it stays outside agentbox as its own container (decisions.md).

---

## 2. Built, waiting on a host run

Nothing to write; each needs real podman, the LAN or a real key, so only the user can close it. Until then each is
**unverified** (decisions.md, 19 Nov, names what each run must show).

- **Pi on the local model server**: `make pi-live` with a local model once the server answers (it hung on 19 Nov,
  outside agentbox); confirms the launcher's `<baseUrl>/models` query.
- **Pi profile** (pushed and deployed 19 Nov): a Pi session should report a profile
  hash and shows `/skill:show-me`. `ab profile install pi` needs one package declared in its `plugins.toml`
  (an empty declaration is refused by design).

---

## 2a. Ready to build — scoped, no decision needed

None.

---

## 3. Features deferred by design

Not bugs and not oversights — each was considered and left out. The Status table in `progress.md` is
authoritative; this is the *why*.

| Feature | Why not built |
|---|---|
| Model key gateway | Sessions use the Claude subscription token and per-provider key files. A per-model API-key gateway is a bigger build with no present need. |
| Toulmin: restart a dead `tm session serve` | Recorded as a gap by the contract (1.5). Until built, Toulmin's `tm status` tells the owner the socket is dead and which command to run. |
| Clone mode with the dev podman tier | Refused at session start. The gate would translate every bind source under the owner's path into the clone (grant volume device, cover ids, foreign-uid check, unmasked-secret walk). decisions.md, 10 Nov. |
| Codex adapter | Step 8. The Pi adapter is built, Pi packages included (18 Nov; `ab profile install pi`, docs/pi-proposal.md). Codex waits for a need. |
| A profile curator | `ab profile harvest` lists, groups and marks; promoting is a human reading a skill. A curator would need the profile **read-write** (which no normal session gets), a prompt that is itself security-reviewed, and a record of what it rejected and why. **Deliberately not started until `harvest` has been lived with** — right now we would be guessing at the hard part. |

### 3.1 A writable folder inside a read-only root; the dev tier with one

The read-only root is **built** (18 Nov, `planner` / `code = ro`; decisions.md). Not built, by decision (D1, D2 in
`docs/readonly-root-proposal.md`): a writable folder *inside* the read-only root (proposal step 3: validator
exemption, hardlink refusal, the gate's longest-root rule) and the dev tier with a read-only root (step 4).
Build them only if plans must live in the repo, or a planner needs its own containers.

---

## 4. Maintainability backlog

Empty since 17 Nov: dead enforcement code removed (4-5 Nov), the duplicated secret-name and `mask_skip` logic
made one (16 Nov) with the project-mask gap it hid closed, the gate's untested error paths covered (53 tests,
three fixes), and `gate.py`, `session.py` and `cli.py` split along their seams (16-17 Nov). Keep
`SYSCTL_ALLOWED = ()` in gate.py: a deliberate knob, not dead code.

---

## 5. Watch only — not an action

- **Egress flakes** (squid upstream stalls; IPv6 suspected). Diagnostics are in place; act only if they
  recur. Candidate fix: a squid `dns_timeout` under the probe's 20 s. Full notes in `progress.md` → Watch
  items; `make egress-soak` is the measurement.
- **`make omp-live`'s local model is slow** — about 257 s for one turn, which is why that check used to fail
  intermittently. **Accepted** (4 Nov). The recorded passes since 13 Nov use
  `AB_LIVE_MODEL=openrouter/z-ai/glm-5.3-flash` (about 50 s). Without it omp-live needs a default model in
  `~/.config/agentbox/omp/config.yml`, and refuses at once if neither is set (17 Nov). On the config.yml
  model it hung on 17 Nov with agentbox.slice at 4392/4400 MB and passed on 18 Nov (99 s) at about 3.5 GB, so
  memory pressure is the likely cause; act if it hangs again with memory free. On 19 Nov the local server
  itself hung (a plain curl from the host too), so pi-live used OpenRouter.
- **OMP's Anthropic provider cannot reach api.anthropic.com from a session** (`getaddrinfo ENOTFOUND`, 11 Nov,
  again 16 Nov): OMP's Anthropic client resolves the name itself instead of going through `HTTPS_PROXY`; its
  OpenAI-compatible client is fine. Upstream OMP behaviour; not tested through a third-party route by choice
  (12 Nov). Act if Anthropic models are wanted in OMP sessions.
- **Session start time.** Profiled 16-17 Nov (decisions.md). What remains is podman's own: about one start in
  four has one sidecar `run -d` taking 1.7-3.2 s (one omp-live start on 18 Nov took 17 s, with 4 sessions
  running and the pool at 3.5 GB; the next start in the same run took 3.1 s) (journald was the bigger spread, gone since the k8s-file
  switch). On large trees the per-file work was fixed (mask plan, scan record, backup skips caches). **Measured 19
  Nov** on the host: the grant step of a toulmin-build start took 17.9 s, and the cost was not the scan's per-file
  lstat (narrowing it would have saved about 15%) but the same tree walked four times with redundant per-directory
  syscalls. **Done 19 Nov:** the plan and the scan share one listing per directory (`mounts.Tree`); about half the
  time on a generated 21k-file tree in a session. The per-file lstat stays (the foreign-owner warning is a
  mistake-catcher, user's decision). Re-measure toulmin-build with the command in decisions.md (19 Nov); act again
  only if the grant step is still over a few seconds there. `tests/perf/run.sh` re-measures the rest.
- **A pod start inspects the pod four times** (`require_dev`, `start_targets`, `check_start`'s infra lookup,
  `decide`'s second `require_dev`), and the gate serves requests on threads, so the pod can change in between.
  Not a known bug: each inspect is checked on its own. Reading it once would make the checks agree on one
  state. **Decided 19 Nov: leave it** — exploiting the gap needs deliberate timing, an attack the threat model
  excludes, and the change would touch the gate for no gain within it.
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
| 19 Nov | A linked worktree's git dir is a git dir for the covers; with `extensions.worktreeConfig` on, every `config.worktree` (the worktree's and the main one) is covered, created empty if missing |
| 19 Nov | The start's tree work reads each directory once: `mounts.plan`'s walks and `grantcheck.scan` share one `mounts.Tree`, and `is_covered` is a set lookup; results pinned identical to the old code by `test_walk_equivalence`. Host re-measure of toulmin-build pending |
| 19 Nov | `data_env` / `data_bind` verified by hand on the host and pinned by an itest, `test_trusted_optouts` (written from that run; 3/3 on the host) |
| 19 Nov | Long start steps say where they are in the whole start: `agentbox: [3/7] checking <root> for secrets and live data: 17.0s`, the total counted from the steps this start runs (plan fixed up front in `Session.start_plan`). Teardown is numbered the same way (`[n/3]`). |
| 19 Nov | A session being torn down says so: `stopping.json` in its run dir (step, since when); `ab session ls` shows `stopping since HH:MM: <step>, N min`, attach, the lock refusal and registry edits say "is shutting down"; `ab session kill` ends a stuck teardown. Teardown frees the project once nothing of the session runs (before the scan and the Toulmin end), prints last through a writer thread, bounds the post-release work, and `tm` runs in its own process group (the toulmin-build stall of 18 Nov) |
| 19 Nov | Pi: the local model server's models asked from it at start when the seed lists none; OMP and Pi sessions get the profile's `AGENTS.md` after the sandbox notes (it reached neither before); `agentbox-profile-pi` created (skills, `build.sh`, Pi check rules, 134 tests) |
| 19 Nov | `caps` has one value, `refused`: the never-built `opt-in` is gone from the build presets, a stored one reads as `refused` with a note; a git config in a read-only grant that the launcher cannot read refuses the session |
| 19 Nov | The masked-paths spec's in-session errno probe (§5.1, with the §5.3 host sentinel) is an itest, `test_mask_errno`; written, unverified until the host runs it |
| 19 Nov | The trusted opt-outs, project settings `data_env` and `data_bind` (`--data-env`, `--data-bind`; on or off): the real root env files, and live data in `./data` visible, writable and left out of the backup, for an agent the user trusts; refused under lockdown, in clone mode, and (`data_bind`) with a read-only root, a read-only-data preset or a mask over `./data`. Replaces 6 Oct's deferred `--data-bind` contract |
| 19 Nov | Pi adapter (1.5.2) built and verified on the host: `test_pi_isolation`, `make pi-live` on OpenRouter; pi-live's bash check made unguessable; the gate retries a podman 500 on a lookup (`podman run --rm` race) |
| 18 Nov | `ab deploy --down [--volumes]`; the deploy contract (`deploy-check`); read-only project root (`code = ro`, `planner` preset); host facts moved out of the repo into agentbox.toml |
| 18 Nov | `ab deploy` built and verified on podman (`test_deploy`, `make itest` 200/200): a reviewed commit copied out with the real `.env`, run through the gate's deploy mode; hand-run stacks refused, their volumes adoptable by name. Proposal folded into design.md |
| 17 Nov | TODO sections 2 and 4 emptied: `ab profile install --resolve`; omp-live refuses without a model; the gate's error paths tested (escape directive, `ignoreIfExists` spellings and build-chunk buffering fixed); late project-mask names refused in binds; the `Gate` class split. Also: backups skip `CACHEDIR.TAG` caches and can prune read-only trees, the scan fingerprints executables by metadata, the mask plan 20× faster on big trees, `--podman none` clears live access, long steps print progress |
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
