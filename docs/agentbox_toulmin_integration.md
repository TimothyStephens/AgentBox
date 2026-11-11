# agentbox: Toulmin integration (gate B prerequisites)

Date: 2026-11-10. Source of truth for the Toulmin side: `toulmin_v1_design.md` sections 2.1, 5.2, 5.3, 11.14, and the "agentbox reconciliation" note under Future Improvements. This file is self-contained; the builder does not need the design document.

Toulmin is a single-user CLI (`tm`) that records analysis runs and lets an AI agent, running inside an agentbox container, draft claims about registered data through an MCP tool server (`tm-agent`) that runs outside the container as the owner. agentbox's job is to launch the agent with the right mounts, hand it the bridge to that tool server, and report back what it did. Toulmin trusts agentbox's report and does not verify it.

The Toulmin side (`tm session start|confirm|serve|end`) does not exist yet; this spec fixes the interface so both sides can be built against it. Where this spec says "agentbox", it means the launcher's `ab run` path and its teardown.

## 1. What to build

### 1.1 Clone mode (replaces the bind mount of the owner's checkout for git projects)

- A preset attribute or project setting `mount: clone` (default for git roots once Toulmin integration is on; the existing checkout mode stays the default for non-git roots and for projects that opt out).
- At session start: `git clone --no-hardlinks <owner repo root> projects/<name>/clones/<session>/`, then `git checkout -b agentbox/<session>` in the clone. The clone is the container's project root at the owner's real repository path (so paths the agent sees match the owner's). The owner's checkout, its `.git`, and anything untracked or ignored in it are not mounted.
- The rsync backup is skipped in clone mode.
- At teardown: record the clone path, the base commit (the commit the clone was made from), and the head of `agentbox/<session>`; keep the clone until `ab project clean` or the owner removes it. Never push, merge, or delete branches in the owner's repository.
- Registry root, overlap checks, the one-live-session lock, and `ab attach` keep keying on the owner's path.

Why not a worktree: a worktree's `.git` is a file pointing into the owner's repository, which is not mounted, so git cannot run inside the container.

### 1.2 Session mount list

- A launcher-level list of extra mounts for one session, fed by the integration step, audited by the same rules as adapter mounts, and shown in `run_args()` like every other mount.
- Two entries for Toulmin: the session socket directory (host path from the start message, see 2.1), mounted read-write at a container path agentbox chooses (suggested `/run/agentbox/toulmin/`); and the `tm-bridge` binary (host path from the start message), mounted read-only at a container path agentbox chooses (suggested `/run/agentbox/bin/tm-bridge`). Mount the socket directory, never the socket file, so a restarted tool server's new socket is visible.
- The must-not-mount list from the start message is appended to that session's `extra_protected`, and adapter mounts are checked against it too. Anything on that list that a grant or adapter mount would expose is a hard refusal with the offending path named.

### 1.3 Per-session MCP entry

- Claude adapter: add an `mcpServers` entry named `toulmin` whose command is the bridge's container path with the socket's container path as argument, and whose token comes from a file: `--token-file /run/agentbox/toulmin/token`. Prefer merging into the per-session `.claude.json`; fall back to `--mcp-config <generated file>` if that is the right flag at the pinned version (probe it, see 3.3).
- OMP adapter: generate `.mcp.json` in the run directory with the same entry and mount it at `agent/.mcp.json`, the way `models.yml` is handled today.
- The token is written to a `0600` file in the run directory and mounted into the socket directory's container path as `token`. It never appears in `--env`, in argv, or in logs. `podman inspect` must not reveal it.

### 1.4 Environment

- Add `AGENTBOX_AGENT=<claude|omp>` to the environment of every session, next to the existing `AGENTBOX_SESSION=<session id>`. Toulmin keys its "possibly agent-issued" label on these two.

### 1.5 The four `tm` calls, in order

All four run on the host as the owner, from the launcher. `tm` is found on `PATH` or given by a setting `toulmin.tm_path`. If `tm` is absent or `tm session start` fails, the session does not start.

1. **Before anything else:** `tm session start --role writer --json` (role is a fixed string in V1). Read the start message from stdout (schema 2.1). Record `session`, `token`, `socket_dir`, `bridge`, `agent_denied`, `must_not_mount`, and `project_root` in `session.json` so teardown can run without this process.
2. **After the plan is complete and audited, before `podman run`:** compute `mounts_hash` as `sha256:` plus the hex SHA-256 of the canonical JSON (RFC 8785) of the planned mount list (each entry `{"source","target","mode"}`, sorted by `target`). Call `tm session confirm <session> --json <file>` with the confirm message (schema 2.2). Non-zero exit aborts the session before the container exists.
3. **Immediately after confirm succeeds:** start `tm session serve <session>` the way the gate and the podman API service are started (`Popen`, `start_new_session=True`, pid file under `services/<session>/`, stopped at teardown by pid plus cmdline check). It runs in the foreground of its own process and exits on its own when the session ends. It will refuse to start if confirm has not been recorded.
4. **In `teardown()`, after the end-of-session scan and before the lock is released:** `tm session end <session> --json <file>` with the end message (schema 2.3). This runs from every teardown path: normal agent exit, exceptions, SIGINT, SIGTERM, SIGHUP, `ab session kill`, and `ab session gc`. In the `gc` case the launcher process is gone, so everything the message needs must already be in `session.json`; `head` comes from the scan's ref map, and `head == base` is a valid message. Then stop the `serve` child. A non-zero exit from `tm session end` is logged, not fatal.

Not required now, recorded as a gap: restarting a `serve` child that dies mid-session. Until that exists, Toulmin's `tm status` tells the owner the socket is dead and which command to run.

### 1.6 Unchanged

Nine presets, egress always through the session proxy at `locked`, `packages`, or `open`, `--userns=keep-id`, `--cap-drop=ALL`, `--read-only`, no new privileges. Toulmin records `network` as one of those three; there is no network-off, unisolated, or separate-user mode and none is being asked for.

## 2. Message schemas

All messages are one JSON object. `handshake` is the protocol version and is `1`. Unknown keys are ignored by both sides; missing required keys are an error. Paths are absolute. Every path Toulmin sends is a host path; every path agentbox sends back under `container_paths` is a container path.

### 2.1 Start (Toulmin to agentbox, stdout of `tm session start --json`)

```json
{"handshake":1,
 "session":"S4",
 "token":"<opaque string, 32 or more characters>",
 "bridge":"/home/tim/.local/bin/tm-bridge",
 "socket":"/tmp/toulmin-1000/S4/sock",
 "mount_socket_dir":"/tmp/toulmin-1000/S4",
 "agent_denied":["data/patients.tsv"],
 "must_not_mount":["/shared/records/proj","/home/tim/.cache/toulmin","/home/tim/.config/toulmin","/tmp/toulmin-1000/*"],
 "project_root":"/home/tim/proj",
 "role":"writer"}
```

- `mount_socket_dir` is the one path under `/tmp/toulmin-1000/*` that must be mounted; the glob otherwise applies.
- `agent_denied` paths are relative to `project_root`. agentbox does not need to hide them; the tool server refuses to serve them. If a preset can mask paths, masking them is welcome but not required.
- `must_not_mount` entries are absolute host paths or globs; append them to `extra_protected` for the session.

### 2.2 Confirm (agentbox to Toulmin, file passed to `tm session confirm S4 --json FILE`)

```json
{"handshake":1,
 "session":"S4",
 "preset":"analysis-offline",
 "isolation":"container",
 "network":"locked",
 "lists_applied":true,
 "mounts_hash":"sha256:<hex>",
 "container_paths":{"socket_dir":"/run/agentbox/toulmin","bridge":"/run/agentbox/bin/tm-bridge","project_root":"/home/tim/proj"},
 "clone":"/home/tim/.local/share/agentbox/projects/proj/clones/S4",
 "branch":"agentbox/S4",
 "base":"<sha>",
 "agent":"claude",
 "launcher":"agentbox <version>"}
```

- `isolation` is always `container` from agentbox.
- `network` is exactly one of `locked`, `packages`, `open`.
- `lists_applied` is `true` only if every `must_not_mount` entry was added to `extra_protected` and no planned mount violates it; otherwise do not call confirm, abort.
- `mounts_hash` is over the planned list (section 1.5 step 2). Toulmin stores it and shows it; it does not recompute it.
- `clone`, `branch`, and `base` are present in clone mode and absent in checkout mode. In checkout mode Toulmin labels the session `owner checkout mounted` and records the agent's commits from `project_root` instead.

### 2.3 End (agentbox to Toulmin, file passed to `tm session end S4 --json FILE`)

```json
{"handshake":1,
 "session":"S4",
 "clone":"/home/tim/.local/share/agentbox/projects/proj/clones/S4",
 "base":"<sha>",
 "head":"<sha>",
 "exit":"agent_exit",
 "ended_at":"2026-11-10T18:04:12Z"}
```

- `exit` is one of `agent_exit`, `signal`, `kill`, `gc`, `error`.
- `head` may equal `base`. `clone` is omitted in checkout mode, in which case `head` is the head of `project_root`'s current branch at teardown.
- Toulmin reads the commits between `base` and `head` from the clone (or `project_root`) itself and computes `git patch-id --stable` for each; agentbox sends no diffs.

## 3. What to return to confirm the interface

Return these five artifacts from a session run against a stub `tm` (a shell script that prints the 2.1 message with fixed values, writes every file it is given to a known directory, and exits 0). Toulmin's real `tm` does not exist yet; the stub is the contract.

1. **The planned mount list and its hash** as the launcher computed them for one session in clone mode, with the socket directory and bridge entries visible and the owner's checkout absent.
2. **The three files `tm` received**: the confirm message, the end message, and the token file's mode and location. Confirm that `grep <token> <(podman inspect <container>)` is empty.
3. **The agent's view**: from inside the container, `ls -la /run/agentbox/toulmin/`, `ls -la /run/agentbox/bin/`, `cat` of the MCP config the harness loaded, `env | grep AGENTBOX`, `git -C <project_root> status --short --branch` (expect the session branch and a clean tree), and a failed `ls` of one `must_not_mount` path.
4. **A teardown trace from each of the five `exit` values**, showing `tm session end` was called with the right `head` each time, including a `gc` run after `kill -9` of the launcher.
5. **A diff of the launcher** limited to the files touched, with the audit rules that changed called out, and the two probe results: the environment variables OMP sets in its subprocesses, and whether `claude --mcp-config` is accepted at the pinned Claude Code version (or that merging into `.claude.json` was used instead).

Anything in section 1 that cannot be built as specified goes in the reply as a numbered deviation with the closest thing agentbox can do, so the Toulmin side can adapt before gate B is drafted. Do not silently narrow the spec.
