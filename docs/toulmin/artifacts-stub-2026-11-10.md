# Toulmin gate B: artifacts from a session against the stub `tm`

Date: 2026-11-10. Branch: `toulmin-gate-b`. Produced by `AB_TOULMIN_ARTIFACTS=<dir> python3 -m unittest tests.unit.test_toulmin` in the agentbox sandbox (fake podman: the `podman run` argv is recorded, no container runs). Section numbers follow docs/agentbox_toulmin_integration.md §3.

## 1. The planned mount list and its hash (clone mode)

```json
{
 "mounts_hash": "sha256:f725aef2e7d7252c24a88645edbf1b02952494e757d46f55b7e86edbbed8b4c8",
 "mounts": [
  {
   "source": "tmpfs",
   "target": "/home/vmuser",
   "mode": "rw"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/bin/tm-bridge",
   "target": "/run/agentbox/bin/tm-bridge",
   "mode": "ro"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/data/projects/p1/claude",
   "target": "/run/agentbox/claude",
   "mode": "rw"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/run/sessions/abt-unit-claude-p1-1/claude-instructions.md",
   "target": "/run/agentbox/claude/CLAUDE.md",
   "mode": "ro"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/data/projects/p1/claude/inbox",
   "target": "/run/agentbox/claude/inbox",
   "mode": "rw"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/config/claude-oauth-token",
   "target": "/run/agentbox/token",
   "mode": "ro"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/toulmin-1000/S4",
   "target": "/run/agentbox/toulmin",
   "mode": "rw"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/run/sessions/abt-unit-claude-p1-1/toulmin-token",
   "target": "/run/agentbox/toulmin/token",
   "mode": "ro"
  },
  {
   "source": "tmpfs",
   "target": "/tmp",
   "mode": "rw"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/data/projects/p1/clones/abt-unit-claude-p1-1",
   "target": "/tmp/abt-unit-_39ypvbj/projects/p1",
   "mode": "rw"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/data/projects/p1/clones/abt-unit-claude-p1-1/.git/config",
   "target": "/tmp/abt-unit-_39ypvbj/projects/p1/.git/config",
   "mode": "ro"
  },
  {
   "source": "/tmp/abt-unit-_39ypvbj/data/projects/p1/clones/abt-unit-claude-p1-1/.git/hooks",
   "target": "/tmp/abt-unit-_39ypvbj/projects/p1/.git/hooks",
   "mode": "ro"
  }
 ]
}
```

The owner's checkout is absent as a source: the root target is served by the clone. Socket directory and bridge are the `/run/agentbox/toulmin` and `/run/agentbox/bin/tm-bridge` entries; the token is the read-only entry inside the socket directory.

## 2. The three files tm received

### confirm (clone mode)
```json
{
 "agent": "claude",
 "base": "666fa3affb165276d0a193d7a96f3838fba0b3a2",
 "branch": "agentbox/abt-unit-claude-p1-1",
 "clone": "/tmp/abt-unit-_39ypvbj/data/projects/p1/clones/abt-unit-claude-p1-1",
 "container_paths": {
  "bridge": "/run/agentbox/bin/tm-bridge",
  "project_root": "/tmp/abt-unit-_39ypvbj/projects/p1",
  "socket_dir": "/run/agentbox/toulmin"
 },
 "handshake": 1,
 "isolation": "container",
 "launcher": "agentbox 13e045f",
 "lists_applied": true,
 "mounts_hash": "sha256:f725aef2e7d7252c24a88645edbf1b02952494e757d46f55b7e86edbbed8b4c8",
 "network": "packages",
 "preset": "script",
 "session": "S4"
}
```

### confirm (checkout mode, same project with `mount = checkout`)
```json
{
 "agent": "claude",
 "container_paths": {
  "bridge": "/run/agentbox/bin/tm-bridge",
  "project_root": "/tmp/abt-unit-62oj8srr/projects/p1",
  "socket_dir": "/run/agentbox/toulmin"
 },
 "handshake": 1,
 "isolation": "container",
 "launcher": "agentbox 13e045f",
 "lists_applied": true,
 "mounts_hash": "sha256:70e764fd4536587f390fd60c0c68701cd4441e49f6b53d4ef4d984e64fdc1e63",
 "network": "packages",
 "preset": "script",
 "session": "S4"
}
```

### end (agent_exit)
```json
{
 "base": "666fa3affb165276d0a193d7a96f3838fba0b3a2",
 "clone": "/tmp/abt-unit-_39ypvbj/data/projects/p1/clones/abt-unit-claude-p1-1",
 "ended_at": "2026-11-10T19:35:57Z",
 "exit": "agent_exit",
 "handshake": 1,
 "head": "666fa3affb165276d0a193d7a96f3838fba0b3a2",
 "session": "S4"
}
```

### the token file

```
/tmp/abt-unit-h69yngbv/run/sessions/abt-unit-claude-p1-1/toulmin-token  mode 0o600  (removed with the run dir at teardown)
```

The token appears in no `--env` and in no argv (asserted over the recorded `podman run` argv, which is what `podman inspect` would show). The `grep <token> <(podman inspect ...)` itself needs a real container: `tests/integration/test_toulmin.py` on the host.

### the recorded `podman run` argv
```
run
--rm
--name=abt-unit-claude-p1-1
--hostname=abt-unit-claude-p1-1
--label=agentbox-test=1
--label=agentbox.session=abt-unit-claude-p1-1
--label=agentbox.project=p1
--label=agentbox.owner=p1/abt-unit-claude-p1-1
--label=agentbox.role=agent
--interactive
--cap-drop=ALL
--security-opt=no-new-privileges
--read-only
--read-only-tmpfs=false
--tmpfs=/tmp:rw,nosuid,nodev,mode=1777,notmpcopyup
--userns=keep-id
--init
--pids-limit=4096
--memory=4g
--memory-swap=4g
--cpus=3
--oom-score-adj=500
--tmpfs=/home/vmuser:rw,nosuid,nodev,mode=0700,U,notmpcopyup
--env=AGENTBOX_SESSION=abt-unit-claude-p1-1
--env=AGENTBOX_AGENT=claude
--env=HOME=/home/vmuser
--env=CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
--env=DISABLE_AUTOUPDATER=1
--env=DISABLE_TELEMETRY=1
--env=CLAUDE_CONFIG_DIR=/run/agentbox/claude
--env=HTTP_PROXY=http://abt-unit-claude-p1-1-proxy:3128
--env=HTTPS_PROXY=http://abt-unit-claude-p1-1-proxy:3128
--env=http_proxy=http://abt-unit-claude-p1-1-proxy:3128
--env=https_proxy=http://abt-unit-claude-p1-1-proxy:3128
--env=NO_PROXY=localhost,127.0.0.1,::1
--env=no_proxy=localhost,127.0.0.1,::1
--workdir=/tmp/abt-unit-_39ypvbj/projects/p1
--network=abt-unit-claude-p1-1-int
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/data/projects/p1/clones/abt-unit-claude-p1-1,dst=/tmp/abt-unit-_39ypvbj/projects/p1
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/data/projects/p1/clones/abt-unit-claude-p1-1/.git/hooks,dst=/tmp/abt-unit-_39ypvbj/projects/p1/.git/hooks,ro
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/data/projects/p1/clones/abt-unit-claude-p1-1/.git/config,dst=/tmp/abt-unit-_39ypvbj/projects/p1/.git/config,ro
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/data/projects/p1/claude,dst=/run/agentbox/claude
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/config/claude-oauth-token,dst=/run/agentbox/token,ro
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/run/sessions/abt-unit-claude-p1-1/claude-instructions.md,dst=/run/agentbox/claude/CLAUDE.md,ro
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/data/projects/p1/claude/inbox,dst=/run/agentbox/claude/inbox
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/toulmin-1000/S4,dst=/run/agentbox/toulmin
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/bin/tm-bridge,dst=/run/agentbox/bin/tm-bridge,ro
--mount=type=bind,src=/tmp/abt-unit-_39ypvbj/run/sessions/abt-unit-claude-p1-1/toulmin-token,dst=/run/agentbox/toulmin/token,ro
--entrypoint=/bin/sh
localhost/agentbox-claude:latest
-c
if [ -r /run/agentbox/token ]; then IFS= read -r CLAUDE_CODE_OAUTH_TOKEN < /run/agentbox/token; export CLAUDE_CODE_OAUTH_TOKEN; fi; exec "$@"
agentbox
claude
--dangerously-skip-permissions
```

## 3. The agent's view

Produced on the host by `tests/integration/test_toulmin.py` (itest run 47690e, real podman, 10 Nov 21:35), which also asserted that the token is absent from `podman inspect` of the running agent container and that `/run/agentbox/toulmin` is present in it:

```
== ls -la /run/agentbox/toulmin/
total 8
drwxrwxr-x 2 vmuser vmuser   60 Nov 10 21:35 .
drwxr-xr-t 5 root   root   4096 Nov 10 21:35 ..
-rw------- 1 vmuser vmuser   51 Nov 10 21:34 token
== ls -la /run/agentbox/bin/
total 12
drwxr-xr-x 2 root   root   4096 Nov 10 21:35 .
drwxr-xr-t 5 root   root   4096 Nov 10 21:35 ..
-rwxr-xr-x 1 vmuser vmuser   38 Nov 10 21:34 tm-bridge
== MCP config the harness loads (.claude.json mcpServers)
{"toulmin": {"type": "stdio", "command": "/run/agentbox/bin/tm-bridge",
             "args": ["/run/agentbox/toulmin/sock", "--token-file", "/run/agentbox/toulmin/token"]}}
== env | grep AGENTBOX
AGENTBOX_AGENT=claude
AGENTBOX_SESSION=abt-47690e-claude-tlm-1
== git status --short --branch
## agentbox/abt-47690e-claude-tlm-1
== ls of a must_not_mount path
ls: cannot access '/tmp/abt-47690e/shared/records/proj': No such file or directory
ls failed as expected
== the bridge runs
tm-bridge stub
== token file
51 /run/agentbox/toulmin/token
```

The confirm and end messages of that run carried `launcher: agentbox 7424f0c`, `mounts_hash: sha256:fd97125b…e3e6`, base = head = `bffd8c1f…`, `exit: agent_exit`. The MCP configs the two harnesses load, as captured in the sandbox run, follow.

### MCP config the Claude harness loads (`.claude.json` in CLAUDE_CONFIG_DIR, during the session; removed at teardown)
```json
{
 "mcpServers": {
  "toulmin": {
   "type": "stdio",
   "command": "/run/agentbox/bin/tm-bridge",
   "args": [
    "/run/agentbox/toulmin/sock",
    "--token-file",
    "/run/agentbox/toulmin/token"
   ]
  }
 }
}
```

### MCP config the OMP harness loads (generated, mounted at `~/.omp/agent/.mcp.json`, the user's seed merged in)
```json
{
 "mcpServers": {
  "mine": {
   "command": "x"
  },
  "toulmin": {
   "type": "stdio",
   "command": "/run/agentbox/bin/tm-bridge",
   "args": [
    "/run/agentbox/toulmin/sock",
    "--token-file",
    "/run/agentbox/toulmin/token"
   ]
  }
 }
}
```

## 4. Teardown traces, one per exit value

### agent_exit
```json
{
 "base": "9a287fb5178550018a676abced3f5ea4aee6ab46",
 "clone": "/tmp/abt-unit-42ifbrpq/data/projects/p1/clones/abt-unit-claude-p1-1",
 "ended_at": "2026-11-10T19:36:32Z",
 "exit": "agent_exit",
 "handshake": 1,
 "head": "a8756c055f87c6eb3700008a77bb7aff7eee1495",
 "session": "S4"
}
```

### signal-SIGTERM
```json
{
 "base": "c7f50aecb0d366b370ba1e8c00d8018d21bad3ec",
 "clone": "/tmp/abt-unit-8lcrajo8/data/projects/p1/clones/abt-unit-claude-p1-1",
 "ended_at": "2026-11-10T19:36:41Z",
 "exit": "signal",
 "handshake": 1,
 "head": "9632c7a16d6d342f81a7c79abfd20c0433e11eba",
 "session": "S4"
}
```

### signal-SIGHUP
```json
{
 "base": "c7f50aecb0d366b370ba1e8c00d8018d21bad3ec",
 "clone": "/tmp/abt-unit-8lcrajo8/data/projects/p1/clones/abt-unit-claude-p1-1",
 "ended_at": "2026-11-10T19:36:48Z",
 "exit": "signal",
 "handshake": 1,
 "head": "257d80cfa50ed4e820b418e8b8e745d9fff9b871",
 "session": "S4"
}
```

### kill
```json
{
 "base": "c7f50aecb0d366b370ba1e8c00d8018d21bad3ec",
 "clone": "/tmp/abt-unit-8lcrajo8/data/projects/p1/clones/abt-unit-claude-p1-1",
 "ended_at": "2026-11-10T19:36:56Z",
 "exit": "kill",
 "handshake": 1,
 "head": "526d3343cee24107afa0aa7660a20b20022919d1",
 "session": "S4"
}
```

### gc-after-sigkill
```json
{
 "base": "c7f50aecb0d366b370ba1e8c00d8018d21bad3ec",
 "clone": "/tmp/abt-unit-8lcrajo8/data/projects/p1/clones/abt-unit-claude-p1-1",
 "ended_at": "2026-11-10T19:37:03Z",
 "exit": "gc",
 "handshake": 1,
 "head": "790d49caa49f55d9e90519de89d2c8c4e46e1a03",
 "session": "S4"
}
```

### error
```json
{
 "base": "1477093ea4a20618e2fdf619bfee7158ae062dd3",
 "clone": "/tmp/abt-unit-e6a_k_70/data/projects/p1/clones/abt-unit-claude-p1-1",
 "ended_at": "2026-11-10T19:36:39Z",
 "exit": "error",
 "handshake": 1,
 "head": "1477093ea4a20618e2fdf619bfee7158ae062dd3",
 "session": "S4"
}
```

In every trace `head` is the commit made in the clone during the session (a different sha from `base`), except `error`, where no agent ran and `head == base`. The `gc` trace follows a `kill -9` of the launcher: nothing was sent until `ab session gc` found the husk, and the orphaned `serve` child was stopped by it.

