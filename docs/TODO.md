# TODO & tech debt

Nothing here blocks use. These are the optional / deferred items left — pick them up when a real change touches the area, or when they become worth doing. (Active work and the build state live in `progress.md`.)

Last reviewed 2026-11-04, after the image restructure. `make test` (1035), `make itest` (186),
`make claude-live`, `make omp-live`, both `ab selftest`s and both `ab image build`s are green on the host at
that point; the unit suite also runs with zero ResourceWarnings. The third preset/settings audit
(F-1 … F-12, decisions.md 28 Oct) is fully closed.

## Needs an answer / a real fix

- ~~**OMP profile load paths**~~ — **resolved and built, 29 Oct.** The question was whether a read-only
  profile at `agent/{skills,rules,prompts,agents,commands}` would break autolearn, which writes into that
  area. Answered from the public source (`github.com/can1357/oh-my-pi`:
  `packages/coding-agent/src/autolearn/managed-skills.ts`, `discovery/builtin.ts`, `docs/skills.md`):
  **autolearn writes only to `<agentDir>/managed-skills`**, never to `<agentDir>/skills`, and both are
  discovered unconditionally. So the authored directories carry the profile read-only, and autolearn keeps
  its own directory — which agentbox gives it **empty every session** and harvests into the project's inbox
  at teardown, because a skill the agent wrote itself is a proposal, not something later sessions should
  load unreviewed. `omp.AUTOLEARN_DIR` names the one directory this rests on.

  **The version caveat is closed, 4 Nov.** It used to depend on whichever OMP the host's `omp-env` image
  happened to carry, which nobody could check from inside a sandbox. agentbox installs its own OMP now
  (`OMP_VERSION := 18.13.3` in the Makefile, asserted at build time), and `make itest` exercises the whole
  arrangement against the real binary: `test_omp_profile_paths_are_read_only`,
  `test_omp_reads_a_profile_skill`, and the round trip that writes to `managed-skills`, finds it in the
  inbox after teardown, and finds the directory empty again next session. All green on the host (186 itests).

- ~~**Two concurrent sessions of one project share its agent state**~~ — **fixed 29 Oct.** One live session
  per project, enforced by a lock file in the project's state dir (`agentbox/projectlock.py`): `ab run`
  takes it before any work and `ab attach <project>` is the way back to the session that holds it. The
  sharing is impossible now rather than warned about, so the warning is gone. A stale lock (its launcher
  gone and nothing of its session running) is reaped, with what was removed reported; a live holder is never
  overruled, and an unreadable podman stops the operation instead of freeing a lock on a guess.

- **`base_branch` has no source** (4 Nov). The scan reports a named base branch's new commits one by one
  (`scan.report(..., base_branch=)`), and teardown reads the value from the scan state written at session
  start, so `ab session kill` and `ab session gc` name the same branch the launcher would. **Nothing writes
  it.** The one line needed is in `session._write_scan_state`, which has to put `base_branch` in the doc; what
  it reads it *from* is the open question — a project registry key (so any project can name its integration
  branch) or an agent-config value belonging to the adapter that wants it. Until then the parameter is
  exercised by unit tests only (`tests/unit/test_scan.py:RefMovement`).

- ~~**The agent image layers are not restructured yet**~~ — **done, 4 Nov.** The chain is
  `node:26-bookworm-slim` → `agentbox-base` → `agentbox-{claude,omp}` → optionally a personal layer, and
  each agent image installs its own CLI at a Makefile pin that the build asserts. The two facts that
  blocked it are settled: node comes from the official image (so no node tarball checksum of ours), and
  OMP is `@oh-my-pi/pi-coding-agent@18.13.3` with Bun 1.4.4, both checksum- or integrity-verified. See
  decisions.md. Unbuilt here (`make` is not in a sandbox): `make base-image`, `make claude-image omp-image`,
  then `make itest` and both live targets are the gate.

- **`omp-env` is now only a host tool** (4 Nov). agentbox no longer builds on `localhost/omp-env`, but
  `omp-env install` is still what provisions `~/.omp/{plugins,repos,pylibs}`, which sessions mount
  read-only. So the image and the plugins can drift apart: the plugins are installed against whatever OMP
  the host's omp-env has, while the session now runs the OMP pinned in `images/omp/Containerfile`. The
  known symptom is a plugin that fails to load (`pi-statusline`'s `stripTerminalSequences`). Closing this
  properly is the `ab profile install` work below — a profile-owned `plugins.toml` installed in a gated
  session against the image's own OMP. Until then, keep `OMP_VERSION` near what omp-env installs with.

- **The MAGS adapter is parked** (4 Nov), waiting on a complete spec: MAGS changes that are not pushed yet,
  and one design change since the plan was approved — its generated test containers get a **sealed network
  (no proxy)** rather than the session's internal network. That matters because the session network carries
  the squid proxy and the gate injects `HTTP(S)_PROXY` into every container it creates, so "reaches nothing"
  needed the proxy taken away, not just the LAN. Built from the approved plan already: the gate's
  network-pinning test (`tests/unit/test_gate.py`, a create naming no network at all), and the scan's
  HEAD/ref movement plus `base_branch` reporting. Nothing to do until the spec arrives.

- ~~**Node is pinned to 22, and the current LTS is newer**~~ — **moved to 26 on 4 Nov**, at Tim's call.
  `NODE_MAJOR` in the Makefile is the one place it lives, and `tests/unit/test_images.py` pins that the
  Containerfile's own `ARG OS_BASE` default names the same image (a stale default is a trap for a
  hand-build rather than an error). `26-bookworm-slim` is published and node 26 is the current LTS, both
  confirmed from `docker-library/official-images`. **This changes OMP's node major from 22 to 26**, so the
  gate is `make base-image` + both agent images, then `make itest` and `make omp-live` — not just a rebuild.

- ~~**`make omp-live` passes but the local model is slow**~~ — **accepted, 4 Nov.** One turn takes about
  257s, which is why that check used to fail intermittently (a 900s timeout, then an empty answer, then a
  pass). Tim accepts the latency: the check stays on the real LAN model, because exercising that endpoint
  is the point of it. The report names the case and the latency now, so a repeat reads as "slow model"
  rather than as an unexplained FAIL, and `AB_LIVE_MODEL=<provider/model>` is there for a one-off.

- **Old user image layers are never pruned** (4 Nov). Each harness image rebuild or Containerfile edit
  gives a new `localhost/agentbox-<agent>-user:<key>` tag and leaves the previous one in podman storage.
  `ab session gc` prunes agentbox's own images by age (`image_keep_days`); the user layers are not in that
  sweep. Low: `podman image rm` by hand, or add them to the same sweep once the shape settles.

## Nice to have — cleanup / future
- **A curator for the profile** (29 Oct, deliberately not built). `ab profile harvest` lists, groups and
  marks; promoting is still a human reading a skill and deciding. The follow-up would be a small project of
  its own: an agent session whose *job* is to review the inbox — read each item with its provenance, judge
  whether it generalises beyond the project that produced it, rewrite it to house style, flag overlaps with
  what the profile already has, and propose the promotion as a diff for a human to approve. It wants a
  project of its own because it needs the profile read-write (which no normal session gets), a prompt that
  is itself reviewed, and a record of what it rejected and why. Not started: harvest has to be lived with
  first, to see what the review actually involves.
- **Profile snapshots are never garbage-collected.** `<data>/profiles/<agent>/<hash>/` gains a directory per
  distinct profile content, and old ones are kept so a running session's mount stays valid. They are small
  (text), and `ab session gc` would be the place to drop any hash no live session mounts.
- **A read-only project-root mount mode, for a planner preset** (the agent reads the code and writes a plan, but cannot edit the code). **Not built, and deliberately not bundled into the preset redesign** (26 Oct): the root is an unconditional read-write bind under every preset (`mounts.plan`: `rw_grants = [root] + data_rw`), so this needs a **new mount mode** *and* **another pass over the gate/covers logic** — the gate's read-only-grant handling (forcing `ro` on binds and grant-volume SubPaths), the after-start mountinfo check, and the cover pins all assume a writable root, and that area is security-reviewed (decisions.md, 5–6 Oct). It would also want a decision on one writable sub-directory inside a read-only root (notes/plans), which the grant validator does not model today. Raised because `research` reads as if the project were read-only and is not; design.md's preset table and the `--preset` description now say so explicitly. **Today's mitigation:** commit before a research session, plus the per-session rsync snapshot — a *within-session undo* that restores the grant to how it was at session start (explicitly not a data backup, decisions.md 6 Oct) — so unwanted edits are a `git diff` / `git checkout` away.
- **`--format` templates are only checked by hand.** `tests/manual/podman_formats.sh` is the one check
  against real podman (a unit test keeps it in step with the code, and the fake podman refuses the one
  template we know 5.7 rejects). Running it inside `make itest` would make it automatic; it is manual today
  because it reads whatever objects happen to exist on the host rather than creating its own.
- **Unify the duplicated git-identity lookup** (`initproj.py` / `session.py`) — review finding 10; detail under *Support refactors* below.
- **Deferred-by-design features** (intentionally not built — see the `progress.md` Status table): `ab ws`/`deploy`/`outbox`/`allow`/`reload`/`resume`, the podman `exec` tier, the model-key gateway, `masked_paths`, `--data-bind`, Pi/Codex adapters. (`ab attach` **is** built — 29 Oct, with the per-project lock. `ab ws` as a subcommand is not, but `contrib/ab-ws` is a tmux workspace launcher that does the job from outside, starting agents only through `ab run`.)
- **Audit finding F9** (second preset/settings audit, 27 Oct) — **left as-is, benign.** A project's own root subdirectory is accepted as its own `data_rw` at add time, because the project is not yet in the registry and so is absent from the overlap comparison. Harmless: both mounts are read-write, and `mounts.plan` keeps the more specific one. (Cross-*project* overlap is enforced in both directions — decisions.md, 27 Oct, F3.)
- The **Maintainability backlog** below: dead-code removal (each with a test proving deadness), the `gate.py`/`session.py` refactors, and the untested error paths E5–E12.

## Watch only — not an action
- **Egress flakes** (squid upstream stalls; IPv6 suspected). Diagnostics are in place; act only if they recur. Candidate fix: a squid `dns_timeout` under the probe's 20 s. Full notes in `progress.md` → Watch items.

## Retired by the 27 Oct settings table

The five parallel per-key lists are gone: `cli.SECURITY_OVERRIDES`, `cli.UNSETTABLE`,
`registry.PROJECT_KEYS`, `_print_effective`'s display gating and the ceiling's looseness orders all derive
from `agentbox/projectkeys.py` now (decisions.md, 27 Oct). Five audit findings had been keys missing from one
of them. The remaining duplication below is the same class and the same remedy when it next bites: one
table, derived views.

## Maintainability backlog

Durable backlog of deferred work, moved here from `docs/reviews/2026-10-06-cleanup.md` and `docs/decisions.md` (6 Oct) so it is not lost. Line numbers are from the 6 Oct cleanup audit and drift with edits — treat them as a starting point, confirm before acting. Each item says why it is deferred and what should be true before it is done.

## Enforcement dead code (remove only after a real-use baseline, each with a test proving deadness first)

Proven to have no production caller, but it guards a boundary, so removal waits until the tests around the reachable paths bite (that is what the 6 Oct test pass started). Before deleting any item: add/confirm a test that fails if the live path's guard is removed, so the deletion rests on a net that bites.

| ID | Where | Why dead / why deferred |
|---|---|---|
| D-E1 | `gate.py` `after_start` (~:1001-1010) | No production caller; `decide`/the handler use `prepare_start` + `after_start_checked`. Only unit tests called it (now rewritten onto the live path). Remove once nothing references it. |
| D-E2 | `gate.py` `labels_of` (~:492) | No caller anywhere. |
| D-E3 | `gate.py` `check_binds` (~:1245) | No caller anywhere. |
| D-E4 | `gate.py` `self.refused` (~:417, :522) | Written on every removal, never pruned, never read. Protection is absence from `verified`. |
| D-E5 | `gate.py` `check_limits(compat=)` (~:1385); `pipe_back(watch=)` (~:2386); `read_chunked` `sock is None` branch (~:2222) | Params never read / never passed; the branch was copied from the spike and only ever gets a real socket. |
| D-E6 | `gate.py` `rewrite_network(compat=None)` auto-detect (~:1659-1660) | `decide` always passes `compat`; only a test uses the auto-detect (T7). |
| D-E7 | `grantcheck.py` `_rel_skips` (~:36-42) | No caller; H3 stopped honouring `mask_skip` in the scan. |
| D-E8 | `hardening.py` `/dev/null` mask branch (~:104-108) | Production masks use `mask_file` since step 5 (`mounts.py`); `session.py` never emits `/dev/null`. Only tests use it. |
| D-E9 | Step-1 network-less `Session` path | `make_session` always returns `NetSession`; base `Session` is never instantiated. Unreachable: `Session.network_args` (~:505), `start_infra` (~:603), `pin_args` (~:645), `add_network_cases`→`runner.network_none_cases` (~:656, `runner.py:238`), `egress = "none"` (~:332), `sandbox_notes` `"none"` branch (~:287), the `isinstance(self, NetSession)` checks (~:416-417), and `hardening.py:75`'s `--network=none` allowance. Large, interlocked — remove as one change, with tests, after confirming no step-7+ feature revives a network-less session. |
| D-E10 | `session.py` `podman_access(…, warn)` (~:117) | `warn` param never used. |
| D-E11 | `gate.py` `SYSCTL_ALLOWED = ()` (~:267) | A deliberate knob that makes two checks always refuse. Keep, but be aware it is a constant branch; not really "dead". |

## Duplicated enforcement logic — consolidate WITH tests; trigger: a real bug traces to the drift

Do not merge pre-emptively. Consolidate when a real bug is traced to one of these diverging. Add tests pinning the shared behaviour first.

- **Secret-name test, three copies:** `mounts._masked` (includes custom swap targets), `grantcheck._is_secret_name` with its own `SECRET_FILES` (`grantcheck.py:30` vs `mounts.py:11`), and inline in `gate.unmasked_secret_under` (`gate.py` ~:628-632, ignores swap targets). Known drift: a custom swap target (e.g. `secrets.env`) is masked, but the gate's N6 check does not flag one created mid-session — this is the accepted host-writes-during-session known limitation (`docs/decisions.md`), not a bug to fix here.
- ~~**"Covered" computed five ways**~~ — **done 28 Oct** (audit F-6). There are now two named computations
  with different jobs: `mounts.hidden` (paths that read EMPTY — masks, mask dirs, dev-swap targets; the only
  set the grant scan may use) and `mounts.covered` (that plus the read-only covers; what the gate refuses a
  bind onto). `cli._mask_dsts` is gone, `session._plan_covered` delegates, and every grant scan goes through
  `grantcheck.preflight`. `session.masked_paths` (build contexts) and `mounts.hardlink_masks` still walk the
  plan themselves — both answer a third question (what a packed context must carry empty, and which inodes
  have another name) and neither decides a verdict, so they are left alone.
- **`mask_skip` resolved three times:** `mounts._rel_skips`, the dead `grantcheck._rel_skips` (see D-E7), and `grantcheck._under_mask_skip`.

## Uncovered gate HTTP error-paths (low value; cover opportunistically)

Reachable only through the HTTP/plumbing layer, mostly fail-closed error returns. Low value; add a test when touching the area.

| ID | Where | What is untested |
|---|---|---|
| E5 | `gate.py` ~:523-532 | `remove` failure path (the fake `raw_request` always returns 200). |
| E6 | `gate.py` ~:775-779, :1264-1265 | Grant-volume create failure or a name collision with a non-grant volume; named-volume create failure. |
| E9 | `gate.py` ~:2411-2416, :2257, :2205, :2165 | `Content-Length` with `Transfer-Encoding`; an unsupported transfer encoding; a build context over 8 GB; a body/head over the limit. |
| E10 | `gate.py` ~:1306-1331, :2016-2017, :1978-1981 | Build context errors: unreadable dockerfile param, no Containerfile, Containerfile not a file, bad tar, `FROM` with no image, `# escape=` directive. |
| E11 | `gate.py` ~:1903-1904, :1914-1915 | Network create with `ignoreIfExists`; an unrecognised `/create` endpoint. |
| E12 | `gate.py` ~:1069-1071, :1083-1085 | `mount_violations` "cannot confirm what is mounted" branches. |

## Support refactors (no behaviour change)

- **`gate.py` size / single-class split:** ~2,540 lines; one `Gate` class mixes HTTP plumbing, policy, image handling and mount verification. Long functions: `handle` (~110 lines), `decide` (~75), `rewrite_libpod_container` (~70), `check_common_libpod` (~57). A pod start inspects the pod three times (~:976, :984, :1939). Splitting plumbing from policy is a pure refactor — do it only alongside a change that already touches the gate, with the test suite as the net.
- **`session.py`:** notes text, `ls`/idle and gc are mixed with the lifecycle; moving `sandbox_notes`/`dev_notes` out is a pure refactor.
- **Duplicated git-identity lookup** (`initproj.py` ~:274 and `session.py` ~:197): the two read the author name/email slightly differently; unify when next touching either (pre-real-project review 2026-10-15, finding 10).
