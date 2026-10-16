# Tech debt & TODO

The **TODO** below is the prioritized "what's next" (needs / could / nice). Everything from **Maintainability backlog** onward is deferred, non-blocking work to pick up when a real change touches the area.

## TODO — what's next

Updated 2026-10-16, after the pre-real-project re-review (`reviews/2026-10-15-pre-real-project.md`: all 10 findings actioned — 1–6 fixed with tests, 7–8 documented, 9 n/a, 10 below). `make test` (510) and `make itest` (165) green at that point.

### Needs — before the first real project
- **First real project, supervised.** Register `life-updates` as `lockdown`, `ab selftest` it, then run attended (step-7 commands / `docs/TRIAL.md`). Tim drives on his fork's current branch; I assist. Its live `deploy` stack stays untouched.

  *Gates cleared:* the independent re-review is **done** and actioned; `make claude-live` and `make omp-live` **re-passed 16 Oct against `563f352`** (after the review fixes), and `make test`/`make itest` are green on the same tree. The live-freshness check is clear.

### Could — valuable, not blocking
- **`make claude-image`.** Rebuild to clear the one `ab selftest` SKIP (the current image predates the CLI-version label) and freshen the base CLI. Local and reversible; I can run it.
- **Decide `--with-secrets`'s fate.** Left open in `decisions.md`; Track 1 (grantcheck) may make it redundant.

### Nice to have — cleanup / future
- **Unify the duplicated git-identity lookup** (`initproj.py` / `session.py`) — review finding 10; detail under *Support refactors* below.
- **Deferred-by-design features** (intentionally not built — see the `progress.md` Status table): `ab ws`/`attach`/`deploy`/`outbox`, the podman `exec` tier, the model-key gateway, `masked_paths`, `--data-bind`, Pi/Codex adapters.
- The **Maintainability backlog** below: dead-code removal (each with a test proving deadness), the `gate.py`/`session.py` refactors, and the untested error paths E5–E12.

### Watch only — not an action
- **Egress flakes** (squid upstream stalls; IPv6 suspected). Diagnostics are in place; act only if they recur. Candidate fix: a squid `dns_timeout` under the probe's 20 s. Full notes in `progress.md` → Watch items.

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
- **"Covered" computed five ways:** `session._prepare_reserved` (~:397-398, excludes read-only git/compose covers, runs before adapter covers exist — fails closed), `cli._mask_dsts` (~:88-89), `session.covered_paths`, `session.masked_paths`, `mounts.hardlink_masks` (~:236-241). `cli._mask_dsts` should eventually reuse the session's computation.
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
