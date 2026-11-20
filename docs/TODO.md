# TODO & tech debt

**Nothing in this file blocks use.** It is the durable backlog: what is deliberately not built, what needs a
decision, and what is worth doing when a change next touches the area.

- **What the tool does and does not do** → `docs/progress.md`, the Status table (single source of truth).
- **Why something is the way it is** → `docs/decisions.md` (dated entries; every closed item here has one).
- **The spec** → `docs/design.md`.

**State as of 19 November 2026: feature-complete for Claude, OMP and Pi.** The image chain is
`node:26-bookworm-slim` → `agentbox-base` → `agentbox-{claude,omp,pi}` → optional personal layer, each agent CLI
installed at a Makefile pin the build asserts. Each agent has a profile repo (`agentbox-profile-{claude,omp,pi}`)
and plugins from `ab profile install <agent>`; the Pi profile mirrors the OMP one wherever Pi can load the same
skill or package. `make itest` (213/213), `make claude-live` and `make pi-live` (OpenRouter) passed on the host on
19 Nov; which gates are green at which commit is in `docs/progress.md` and the dated records in `decisions.md`.

**How each open item is written:** *what it is* → *why it matters* → *done looks like* → *where to start* →
*the gate*. That is deliberate: these sit for weeks, and "where to start" is what a cold reader needs.

---

## 1. Open questions — a decision is needed before any code

None.

---

## 2. Built, waiting on a host run

Each needs real podman, the LAN, a real key or a running service, so only the user can close it; until then it is
**unverified**.

- **Pi on the local model server.** `make pi-live` with `AB_LIVE_MODEL=llama-cpp/<model>` once the server answers
  (it hung on 19 Nov, outside agentbox: a plain curl from the host hung too). Confirms the launcher's
  `<baseUrl>/models` query and Pi on the local model.
- **Toulmin from a Pi session.** Pi gets Toulmin through its built-in MCP (`mcp.json`, `exposure: direct`); no
  live run has used it yet.

---

## 3. Ready to build — scoped, no decision needed

- **Do not copy the host's /etc/hosts into containers.** *What:* podman copies the host's `/etc/hosts` into every
  container (`base_hosts_file`), so a session sees LAN host names the host has listed, though their addresses are
  refused by the proxy and the network (seen 20 Nov in an install container). *Why:* a small information leak
  across the sandbox wall. *Done:* session, sidecar, gate-spawned and install containers get podman's generated
  hosts file without the host's entries (`--no-hosts` where it fits, or `--hosts-file=none`-style option of the
  installed podman), and a test pins the flag. *Start:* `hardening.fixed_flags`, the gate's create rewrite.
  *Gate:* unit test, then an itest reading `/etc/hosts` in a session. Outside this repo: Tim's `pi-session-stats` and `pi-content-audit` are OMP extensions (an `omp` manifest,
`@oh-my-pi/*` imports), so the Pi profile cannot carry them until they are ported to Pi's `pi` manifest and
`@earendil-works/*` packages.

---

## 4. Features deferred by design

Not bugs and not oversights — each was considered and left out, and each waits for a need. The Status table in
`progress.md` is authoritative; this is the *why*. (Removed from plans on 19 Nov, not deferred: `ab allow` /
`outbox` / `reload` / `resume`, podman `exec` on the live stack, and the `dev/data` → `data` overlay.)

| Feature | Why not built |
|---|---|
| Codex adapter | `@openai/codex` is a CLI (0.170.0 on 19 Nov). Adding an agent is now mechanical (Pi), but it is a full project: research, a proposal with the user's decisions (login, Codex's own sandbox, config and instruction files, telemetry), image, adapter, live test, profile repo, and a per-release re-check. Waits for a need. |
| Model key gateway | Keys reach a session as environment variables; a gateway would add them per request outside the sandbox. It mainly stops deliberate key theft, a non-goal; careless leaks are checked by the live tests. Worth it with many providers, shared machines or a need to revoke. |
| Clone mode with the dev podman tier | Refused at session start. The gate would translate every bind source under the owner's path into the clone (grant volume device, cover ids, foreign-uid check, unmasked-secret walk). decisions.md, 10 Nov. |
| A writable folder inside a read-only root | D1 in `docs/readonly-root-proposal.md`: needs a validator exemption, a hardlink refusal and the gate's longest-root rule; plans go to a `data_rw` grant outside the root today. Build only if plans must live in the repo. |
| The dev tier with a read-only root | D2 in the same proposal: the dev tier assumes a writable root. Build only if a planner needs its own containers. |
| A profile curator | `ab profile harvest` lists, groups and marks; promoting is a human reading a skill. A curator would need the profile **read-write** (which no normal session gets), a prompt that is itself security-reviewed, and a record of what it rejected and why. Not started until `harvest` has been lived with. |
| Toulmin: restart a dead `tm session serve` | Recorded as a gap by the contract (1.5). Until built, Toulmin's `tm status` tells the owner the socket is dead and which command to run. |

---

## 5. Maintainability backlog

Empty. Keep `SYSCTL_ALLOWED = ()` in gate.py: a deliberate knob, not dead code.

---

## 6. Watch only — not an action

- **agentbox.slice near its cap.** During the 19 Nov itests every start warned at 3.7-4.3 GB of 4.4 GB, with two
  long-lived user sessions running. Nothing failed; act (end idle sessions, or raise the cap) if a session is
  OOM-killed.
- **The local model server** stalled on 19 Nov (outside agentbox) and omp-live's local model is slow (about 257 s a
  turn, accepted 4 Nov). The recorded live passes use `AB_LIVE_MODEL=openrouter/z-ai/glm-5.3-flash`.
- **Egress flakes** (squid upstream stalls; IPv6 suspected). Diagnostics are in place; act only if they recur.
  Candidate fix: a squid `dns_timeout` under the probe's 20 s. `make egress-soak` is the measurement.
- **OMP's Anthropic provider cannot reach api.anthropic.com from a session** (`getaddrinfo ENOTFOUND`): OMP's
  Anthropic client resolves the name itself instead of using `HTTPS_PROXY`. Upstream; it fails closed. Act if
  Anthropic models are wanted in OMP sessions.
- **Session start time.** The grant step on toulmin-build went from 17.9 s to 6.5-8.2 s on 19 Nov (one shared walk);
  what is left is the per-file lstat behind the foreign-owner warning (kept by decision) and podman's own sidecar
  start spread (1.7-3.2 s in about one start in four). Act again only if a start is slow in use;
  `tests/perf/run.sh` re-measures.
- **The 18 Nov teardown stall's cause** is unproven (a blocked terminal write is the likely one; the `tm` pipe
  suspect was disproven). Teardown now frees the project before any output and shows `stopping` with the step; if a
  launcher is ever seen stuck again, `cat /proc/<pid>/stat` and `ab session ls` from the same shell settle it.
- **A pod start inspects the pod four times.** Each inspect is checked on its own; **decided 19 Nov: leave it**
  (exploiting the gap needs deliberate timing, outside the threat model).
- **Plugin tree vs image drift.** A session warns when the tree's lockfile names another image; after
  `make <agent>-image`, rerun `ab profile install <agent>`.

---

## 7. Known limitations — recorded, not TODO

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
| 20 Nov | `ab profile install omp` works again: the cause was a stale aardvark-dns record for its network name; installs now use per-run names and report the proxy's state on failure |
| 19 Nov | Opt-in Playwright in agentbox-base (`image_playwright = true`): Python Playwright 1.65.2 and the headless shell, checksummed; verified in a host session (`test_playwright` 2/2). `python` in every image. Shared skills moved to `agentbox-skills` (a pinned plugin in each profile); `skill-creator` is Claude-only |
| 19 Nov | The Pi packages install and load on the host: four npm extensions (their skills and tools seen by Pi) and EGM's skills, from the read-only tree; the shared `learn-repo` skill is in all three profiles |
| 19 Nov | The Pi profile carries every OMP-profile skill and every OMP package Pi can load (four npm packages at OMP's pins, EGM's skills as a `[[repo]]`); TODO.md rewritten around what is left |
| 19 Nov | A linked worktree's git dir is a git dir for the covers; with `extensions.worktreeConfig` on, every `config.worktree` (the worktree's and the main one) is covered, created empty if missing |
| 19 Nov | The start's tree work reads each directory once: `mounts.plan`'s walks and `grantcheck.scan` share one `mounts.Tree`, and `is_covered` is a set lookup; results pinned identical to the old code by `test_walk_equivalence`; toulmin-build's check 17.9 s → 6.5-8.2 s on the host |
| 19 Nov | `data_env` / `data_bind` verified by hand on the host and pinned by an itest, `test_trusted_optouts` (written from that run; 3/3 on the host) |
| 19 Nov | Long start steps say where they are in the whole start: `agentbox: [3/7] checking <root> for secrets and live data: 17.0s`, the total counted from the steps this start runs (plan fixed up front in `Session.start_plan`). Teardown is numbered the same way (`[n/3]`). |
| 19 Nov | A session being torn down says so: `stopping.json` in its run dir (step, since when); `ab session ls` shows `stopping since HH:MM: <step>, N min`, attach, the lock refusal and registry edits say "is shutting down"; `ab session kill` ends a stuck teardown. Teardown frees the project once nothing of the session runs (before the scan and the Toulmin end), prints last through a writer thread, bounds the post-release work, and `tm` runs in its own process group (the toulmin-build stall of 18 Nov) |
| 19 Nov | Pi: the local model server's models asked from it at start when the seed lists none; OMP and Pi sessions get the profile's `AGENTS.md` after the sandbox notes (it reached neither before); `agentbox-profile-pi` created (skills, `build.sh`, Pi check rules, 134 tests) |
| 19 Nov | `caps` has one value, `refused`: the never-built `opt-in` is gone from the build presets, a stored one reads as `refused` with a note; a git config in a read-only grant that the launcher cannot read refuses the session |
| 19 Nov | The masked-paths spec's in-session errno probe (§5.1, with the §5.3 host sentinel) is an itest, `test_mask_errno`; passes on the host (masked-file writes are EACCES, as measured) |
| 19 Nov | The trusted opt-outs, project settings `data_env` and `data_bind` (`--data-env`, `--data-bind`; on or off): the real root env files, and live data in `./data` visible, writable and left out of the backup, for an agent the user trusts; refused under lockdown, in clone mode, and (`data_bind`) with a read-only root, a read-only-data preset or a mask over `./data`. Replaces 6 Oct's deferred `--data-bind` contract |
| 19 Nov | Pi adapter (1.5.2) built and verified on the host: `test_pi_isolation`, `make pi-live` on OpenRouter; pi-live's bash check made unguessable; the gate retries a podman 500 on a lookup (`podman run --rm` race) |
| 18 Nov | `ab deploy --down [--volumes]`; the deploy contract (`deploy-check`); read-only project root (`code = ro`, `planner` preset); host facts moved out of the repo into agentbox.toml |
| 18 Nov | `ab deploy` built and verified on podman (`test_deploy`, `make itest` 200/200): a reviewed commit copied out with the real `.env`, run through the gate's deploy mode; hand-run stacks refused, their volumes adoptable by name. Proposal folded into design.md |
| 17 Nov | TODO sections 2 and 4 emptied: `ab profile install --resolve`; omp-live refuses without a model; the gate's error paths tested (escape directive, `ignoreIfExists` spellings and build-chunk buffering fixed); late project-mask names refused in binds; the `Gate` class split. Also: backups skip `CACHEDIR.TAG` caches and can prune read-only trees, the scan fingerprints executables by metadata, the mask plan 20× faster on big trees, `--podman none` clears live access, long steps print progress |
| 16 Nov | The independent review's findings fixed (about 100 commits, one test each); modules split along their seams; host gates, both profile deploys and both selftests pass on the result |
| 15 Nov | MAGS-CodeDev decided: it stays outside agentbox as its own container; a failed plugin install keeps the previous tree, and a `[[repo]]` without a `package.json` is refused before linking |
| 13 Nov | OMP's OpenAI-compatible providers (OpenRouter) work through squid |

Older entries (27 Oct - 9 Nov) are in decisions.md under their dates.
