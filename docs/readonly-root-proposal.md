# Proposal: a read-only project root (for a planner preset)

Status: proposal, 18 Nov 2026. No code written. Expands `docs/TODO.md` §3.1 and design.md "Planned / not built".
Tags: **[code]** = read in this tree at the cited function; **[inf]** = inference, not run or measured;
**[itest]** = only a host itest on real podman can settle it.

## 1. Why

`research` reads as though the agent only looks, but the root is read-write under every preset (design.md,
Presets table). A planner session should read the code and write a plan, and be unable to edit the code by
mistake. Today's only mitigation is "commit first; the rsync snapshot is a within-session undo".

## 2. What exists today

- **The plan.** `mounts.plan` builds `rw_grants = [root] + list(data_rw)` with no condition, binds each
  read-write, and binds `data_ro` with `ro=True` **[code]**. Everything else keys off those two lists:
  - `find_git_dirs` and `git_overlays` run only over `rw_grants`. A git dir in a read-only grant gets no
    hooks/config cover, because nothing there is writable **[code]**.
  - `secret_mounts(g, ro=g in data_ro)` makes the `.env.dev` swap bind read-only in a read-only grant. When
    `.env` is missing it skips the swap with a note instead of creating `.env` **[code]**.
  - `user_masks` creates a missing mask path on the host even in a read-only grant **[code]**. That is a host
    write into a "read-only" grant, which is harmless but worth knowing.
  - `compose_overlays` runs over the root only.
  - `covered()` treats a read-only bind whose destination is a grant root as a grant, not a cover. So a
    read-only root would not flood the gate's covered list **[code]**.
- **Grant validation.** `paths._check_within_one_project` refuses a read-write grant inside the same project's
  read-only grant, because "the deeper mount wins" and `ab project show` would lie **[code]**. So the
  writable hole cannot simply be a `data_rw` entry under a read-only root.
- **Session.** `Session.bind_roots` = root + data_rw + data_ro. `gate_config` passes
  `ro_roots = data_ro`. `pin_args` pins read-only under `data_ro` only. `_backup_grants` snapshots
  `[root] + data_rw`. `_write_scan_state` scans `[root_src] + data_rw` **[code]**.
  `_prepare_reserved` already refuses one combination (clone mode with the dev tier), which is the pattern
  for refusing another **[code]**.
- **Gate.** Read-only handling is generic, keyed on `ro_roots` through `MountsMixin.is_ro_root(real)` ("inside
  any ro root") **[code]**. Its callers:
  - `Gate.check_common_libpod` forces `ro` on a plain bind and on a grant-volume SubPath;
  - `Gate.rewrite_compat_container` does the same for `Binds` and `Mounts`;
  - `MountsMixin.mount_violations` → `check_host` refuses a read-only grant mounted `rw` after `init`.

  Separately, `check_bind` → `_refuse_if_covered` refuses binding any root that contains a cover. Sub-paths go
  through `grant_subpath`, which picks the longest holding root, and `grant_volume` **[code]**.
- **Presets/keys.** `projectkeys.KEYS` already has `data` (`ro-only` | `declared`), the model for a new
  posture key. The name `root` is taken by the structural path key **[code]**.
- **grantcheck.** `preflight` scans root + data_rw as refusing grants and data_ro as `warn_grants`. Every
  finding in a read-only grant, unmasked secrets included, becomes a warning **[code]**.

## 3. The mount plan change

Add a preset-resolvable key **`code`** (`ro` | `rw`, tightest first, `security=True`, `clears=True`).
`mounts.plan(..., code="ro")` then builds:

    ro_grants = [root] + data_ro          rw_grants = [hole?] + data_rw

The rest of `plan` runs unchanged over these two lists, so the `data_ro` path is reused as is: a read-only
root bind, read-only swap binds, no git covers for the root's `.git`, masks layered over it. Two changes:

- Skip `compose_overlays` for a read-only root, or run it on the hole only. A read-only cover inside a
  read-only bind adds nothing except gate refusals.
- Fix one gap **[code]**: `git_overlays` ignores git dirs outside rw grants. With a hole, the root's
  `.git/config` could set `core.hooksPath = plans/hooks` (or an `include.path`) inside the writable hole.
  The owner's host git would then run what the agent wrote. `git_overlays` must read the configs of git dirs
  in *every* grant and cover each target that lies in a rw grant. `grantcheck._git_config_escapes` has the
  same blind spot: a target inside some grant is not reported.

  The gap exists today for a `data_ro` repo whose hooks path points into a `data_rw` grant (rare). It
  becomes the main case once a hole exists.

`podman` stacks a deeper mount over a shallower one, so masks over a read-only root bind are the same
mechanism as masks over a read-write one. Every mask target already exists, so podman never needs to create
a mount point inside the read-only bind **[inf; itest]**. The hole's mount point must exist before start:
`mounts.prepare` creates it on the host, where the root is writable.

## 4. The writable hole

| | Layering | `.git` / scan | Validator, covers | Risk |
|---|---|---|---|---|
| **(a) none** | root `ro`, masks on top | no commits; plan goes to chat or inbox | nothing new | low; the plan is lost if not copied out |
| **(b) subdir bind rw over ro root** | `--mount bind,src=R,dst=R,ro` then `bind,src=R/plans,dst=R/plans` (rw, deeper) | `.git` read-only: no commit. Plan shows as untracked/modified on the host; the owner commits it | new key, exempt from `_check_within_one_project`; hole must not be or contain `.git`, not sit under or over a mask, not be `mask_skip`'d; masks, git and compose covers apply inside it as a rw grant | hardlinks and hooksPath (below); the hole is code the owner may run |
| **(c) separate rw grant outside the root** | an ordinary `data_rw` bind at its real host path, e.g. `~/plans/<project>` | as (b), and outside the repo | **already built**: `check_grant`, cross-project `check_overlaps`; preset needs `data = "declared"` | lowest; zero new mount semantics |
| **(d) the inbox** | already mounted rw (Claude: `/run/agentbox/claude/inbox`; OMP: under its agent dir) | outside the repo | none | misuse: the inbox is a review queue for the shared profile (`ab profile harvest`). Ephemeral when the profile is off |

A fixed in-container path for (c), such as `/run/agentbox/notes`, would break "everything mounts at its real
host path" (`mounts.py` docstring), and `hardening._audit_mount` admits only grant and `/run/agentbox`
destinations **[code]**. Keep (c) at its real path.

**Escape through the hole (b):**

- **Symlinks.** A symlink in the hole resolves in the container's namespace, so it lands on the read-only
  root bind **[inf]**.
- **New hardlinks and renames.** `link(2)` and `rename(2)` across two mounts fail with `EXDEV`, even on one
  filesystem **[inf: kernel `do_linkat`/`do_renameat2` mnt check; itest]**.
- **Existing hardlinks.** A file in the hole that is already a second link to a file in the root lets the
  agent rewrite the root file through the hole. The plan must refuse it or mask it, as `hardlink_masks`
  does for covers **[code: no such check today]**.

**Commits:** with a read-only root, `git commit`, `stash`, `fetch` and index refreshes fail. The workflow is:
the agent writes the plan; the owner reviews it on the host and commits it. The end-of-session scan should
scan the hole and data_rw only. Any change in the read-only root is the host's, and `scan.report` would
otherwise attribute it to the session. HEAD and ref movement cannot come from the agent.

## 5. The gate (only matters if the dev tier is allowed)

What must change so that a spawned container also sees the root read-only and gets no writable view of it:

1. `Session.gate_config`: `ro_roots` = data_ro **plus the root** when `code = ro`; `bind_roots` gains the
   hole. With only this change, every caller of `is_ro_root` above forces or demands `ro` on the root and
   everything under it **[code]**.
2. `MountsMixin.is_ro_root` becomes "the *longest* grant root containing `real` is read-only" (as
   `grant_subpath` and `pin_args` already pick the longest holder). Without this, the hole is forced
   read-only in spawned containers, and the after-start check removes a container that mounts it rw.
   One function changes, and all four call sites follow.
3. `grant_subpath`: a source under the hole resolves to the hole's own grant volume (longest holder,
   already). Its SubPath goes through `openat2` in the hole's root, so a symlink swapped in before start
   cannot reach the read-only part (decisions 1 Oct evidence) **[code + inf]**. A symlink in the hole that
   *already* points into the root realpaths into the root and is forced `ro` **[code]**.
4. `check_bind` / `_refuse_if_covered`: unchanged. Binding the whole root is still refused when it holds a
   mask; a read-only root has fewer covers (no git or compose covers), so more sub-path binds pass, all
   forced `ro`.
5. `Session.pin_args`: compute `ro_roots` the same way as (1). Pins under the read-only root are moot,
   because the agent cannot rename there, but they are harmless. Pins inside the hole still matter.
6. `covers_intact`, `unmasked_secret_under`, build-context checks: unchanged (reads only) **[code]**.

## 6. Interactions

- **Masks and covers.** They are layered over the read-only bind exactly as over a read-write one, and they
  still matter: a read-only root is readable, and masks hide secrets. Git and compose read-only covers
  disappear for the root (§3).
- **`.env.dev` swap.** It binds read-only. With no `.env` present, the `data_ro` rule skips the swap and
  says so. Decide whether the launcher may create the empty `.env` in a read-only root, as it does in a
  read-write one (D6).
- **Clone mode.** It exists to isolate writes, and there is nothing to isolate. `mount_mode_of` picks clone
  whenever Toulmin is on and the root is a git repo **[code]**, so `code = ro` should force `checkout` or be
  refused with clone.
- **Pre-session backup.** Moot for the root: skip it in `_backup_grants` and back up the hole + data_rw.
  This saves the rsync on large trees.
- **grantcheck.** Only *half* moot. Live data and git-config escapes cannot be damaged through a read-only
  root, so they become warnings or are dropped. Unmasked secrets and credential URLs are still readable and
  can be sent to the model API, so they must still refuse. Downgrading the root to `warn_grants` (the
  `data_ro` rule) would wrongly downgrade secrets too. `scan()` needs a split, not a reuse.
- **Toulmin.** `must_not_mount` and the mount-list hash need no change (the mode is in the list). The start
  uses `--role writer`; whether a reader role exists is Toulmin's contract, not ours to guess (D7).
- **Dev podman tier.** Writable binds are the point of `build`. A planner does not need them. Refuse
  `code = ro` with the dev tier at first, as clone + dev is refused. The observe tier makes no binds.

## 7. Preset

**`planner`**: `research`'s values (compose ro, podman none, caps refused, secrets masked, approvals off,
`data = "declared"` so (c) works) plus `code = "ro"`. Egress: see D3. Not derived from `lockdown`: lockdown
is a ceiling with approvals on and locked egress, and a lockdown project can already tighten to `code = ro`.

Every built-in preset must state `code` (`"rw"`) explicitly: `effective()` resolves only keys the preset
holds. A new user preset inherits lockdown's `code` (D4). `ab project show` should print
`code  ro (the project root is read-only; writable: <hole or data_rw>)  from preset planner`. The `research`
description should stop saying "as under every preset".

## 8. Tests

**Unit** (fake podman):
- `test_mounts`: the `code=ro` plan (root bind `ro`, masks over it, no root git covers, swap `ro`); the hole
  as a rw bind; the hooksPath-into-hole cover; the hole-hardlink refusal.
- `test_projectkeys` / `test_policy`: the key, ceiling order, every preset states `code`.
- `test_policy` (where `check_overlaps` is tested): hole validation (`.git`, under a mask, symlink component, `..`).
- `test_session`: `gate_config.ro_roots` includes the root; backup and scan grants; clone/dev refusals.
- `test_grantcheck`: secret still refuses, live data warns.
- `test_review_step5`/`test_gate`: `is_ro_root` longest-root, forced `ro` on root binds and SubPaths, hole
  SubPath stays rw, after-start check accepts a rw hole and refuses a rw root.

**itest** (`test_step1_core`, `test_step6_covers`):
- in the agent container, writes to root files, `.git`, `mkdir`, `git commit` fail with EROFS;
- a mask still reads empty;
- the hole is writable;
- `ln`/`mv` from the hole into the root fail (EXDEV);
- a symlink in the hole to `../src/x` cannot be written through;
- spawned container (once dev is allowed): `-v root`, `-v root/src`, `-v root/src:rw` are all read-only or
  refused; `-v hole` is writable; a swap-to-symlink race on a hole SubPath never yields a writable root
  path.

## 9. Size and build order

Each step ends green (`make check`); steps 1 and 3 also need `make itest` on the host.

1. **`code` key + `planner` + plan without a hole** (option (c) for the plan). Includes the dev and clone
   refusals, backup/scan grants, the grantcheck split, `ab project show`, and design.md + decisions.md.
   Also sets `ro_roots` in `gate_config` (one line; inert while dev is refused). About 150 lines of code and
   200 of tests.
2. **The `git_overlays` / `_git_config_escapes` gap** for git dirs in read-only grants. Stands alone. About
   40 + 60.
3. **The hole (b)**, if D1 says so: key, validator exemption, plan, hardlink refusal, `bind_roots`,
   `is_ro_root` longest-root. About 120 + 200.
4. **Dev tier with a read-only root**, only if wanted: lift the refusal and add the spawned-container itests.
   About 20 + 150 (itest).

Total about 330 lines of code and 600 of tests for 1-3. **Host-itest-only:**
- podman layering (masks and a rw hole over a read-only bind; no mount-point creation needed);
- the harnesses (Claude Code, OMP) start with a read-only cwd. Neither is known to write into the project
  at start **[inf]**;
- EXDEV for link and rename across the hole;
- spawned-container read-only enforcement;
- the SubPath race on a hole.

## 10. Decisions needed

| # | Decision | Recommendation | Reason |
|---|---|---|---|
| D1 | Where the plan is written | (c) a `data_rw` grant first; (b) in-tree hole as step 3 only if plans must live in the repo | (c) is already built and reviewed; (b) adds a validator exemption, a hardlink check, a hooksPath cover and gate longest-root logic |
| D2 | Dev podman tier with `code = ro` | Refuse at first | Only the dev tier makes the gate matter; refusing keeps step 1 off security-reviewed ground |
| D3 | `planner` egress | `packages`; `--egress open` per project | Planning needs package docs more than the open web; open stays one explicit flag away |
| D4 | `code` for new user presets (inherit lockdown) | `rw` | `ro` would silently make existing user presets read-only on upgrade; tightening stays available |
| D5 | Clone mode with `code = ro` | Force `checkout` (with a note) | Clone isolates writes; with none possible it only costs a clone |
| D6 | Create a missing `.env` for the swap in a read-only root | No (keep the `data_ro` rule) | A planner needs no runtime env; nothing is hidden by skipping |
| D7 | Toulmin role for a read-only session | Keep `writer` until Toulmin defines a reader role | Ours is not to invent the contract |
| D8 | Rename or repurpose `research` | Leave it; fix its description | Changing it would make existing projects read-only without notice |
