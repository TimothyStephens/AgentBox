# Regression review of the 16 Nov fixes (TODO §2.3)

**Range.** `c782560..3863ee9`: 108 commits (the 16 Nov review fixes, the module splits, the profiling and
start-time work, the gate's build-path changes, progress lines).

**How.** Four fresh reviewers, each given only the diff for its area, `docs/design.md`'s threat model and
the instruction to look for defects introduced in the range (not features or style), to separate moved code
from changed code (`--color-moved`, AST comparison against `c782560`), and to verify every candidate by
running something. No reviewer saw the fixer's conclusions. Each finding below was checked again by the
fixer before it was fixed; each fix has a unit test that fails without it.

| Area | Files |
|---|---|
| Gate | `gate.py`, `gate_http.py`, `gate_dockerfile.py`, `gate_schema.py` |
| Session lifecycle | `session.py`, `lifecycle.py`, `sessionlock.py`, `projectlock.py`, `liveness.py`, `state.py`, `infra.py`, `progress.py`, `backup.py`, `podman.py` |
| Masks and secrets | `mounts.py`, `grantcheck.py`, `scan.py`, `historysecrets.py`, `fsutil.py`, `paths.py`, `projectkeys.py`, `layout.py` |
| Installer, adapters, CLI | `installer.py`, `adapters/`, `cli*.py`, `project.py`, `notes.py`, `initproj.py`, the rest |

## Findings

| # | Sev | Finding | Regression? | Fix |
|---|---|---|---|---|
| R-4 | high | `ab profile install omp` refused the absolute `node_modules/<pkg>` links `omp plugin link` writes, so every profile with a `[[repo]]` entry (the shipped omp profile has two) failed to install. | Yes, cf09b11 | 3863ee9: under `node_modules/`, an absolute target below `C_TREE` is judged as the same path in the staging tree. Verified on the host 17 Nov: `ab profile install omp` installs the shipped profile again. |
| R-1 | medium | The git-history secret check missed names git quotes (`café.env`, `secrets/é.txt`, a newline or `"` in a name). | Older for users on git's defaults; a regression (7cf862a) for users whose global config set `core.quotepath=false`, since git's global config is no longer read | 605d56d: `git log -z`; non-UTF-8 names decoded with replacement |
| R-3 | medium | A later `././Containerfile` in a build context replaced the Containerfile the gate checked; `x/../Containerfile` and `/Containerfile` did the same. | `././` yes (46c2409); the other two older | be30abe: tar members keyed by the path they unpack to |
| R-2 | medium | `core.hooksPath` naming a grant's root was neither covered (covering it covers the grant) nor reported. | No: a gap in new code (7cf862a) | 42ddd21: a grantcheck finding |
| R-5 | low | Sidecar logs under `k8s-file` had no size limit, and squid logs every request to stdout. | Yes, df862d3 | `--log-opt=max-size=256mb` on every sidecar; podman accepts it (`make itest` 17 Nov, 190 OK, starts real sidecars). Rotation at the cap is not tested. |
| R-6 | low | The progress heartbeat thread and the main thread both wrote through `print`, which writes text and newline separately, so lines could interleave; a Ctrl-C at `Thread.start` left the heartbeat running. | Yes, 2b332c3 | the default `warn` writes a whole line in one call; `__enter__` stops the heartbeat if `start` is interrupted |
| R-7 | low | Any `OSError` in the backup's progress path (full `/tmp`, closed stderr) was reported as "install rsync". | Yes, 2b332c3 | only a missing binary gets the install advice |

## For the user's decision, not fixed

- **`ab project set p --podman none` keeps the preset's `live = observe`** (1886181, recorded in decisions.md
  items 46-50). On `build`, `pods` and `operate` the agent can still read the live stack after "none", and
  no CLI route removes it short of changing preset. The fixer's view: `none` should clear `live` too. **Decided 17 Nov:** Tim chose that; `none` now clears `live` (decisions.md, 17 Nov).

## Checked and found fine (summary)

- Moved code in all four areas matches its old body apart from the intended changes (AST comparison).
- Every CLI command, flag, default, choice and exit code survived the CLI split.
- The `_MASK_RE` matcher gives the same answer as the old `fnmatchcase` loop on 300 021 inputs.
- `fsutil.inside` is the expression it replaced in every call site; `/a/bc` is not inside `/a/b`.
- The gate's upstream connection after the spool picks the same destination, and closes socket and temp file
  on every error path; the cut-off relay is bounded and catches its own parse errors.
- The installer's container hardening only tightened; no adapter mount became writable.
- Teardown's `-t 0` set matches the real sidecar names; the agent and dev containers keep `-t 5`.
- The backup's progress path gives the same return code and stats as the plain path; no pipe deadlock.

## Noted

- One full unit run under load (two suites at once) failed
  `test_projectlock.RunHoldsTheProject.test_the_lock_is_released_when_prepare_raises` once; it passed alone
  and in the next full run. Probably the 30 s test timeout under load; watch for a repeat.
- A reviewer ran `pip download` into the repo root by mistake and removed exactly those files; the tree was
  clean afterwards.
