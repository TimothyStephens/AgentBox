# The throwaway trial

A first real session on a project that does not matter: Claude Code with the pods preset (the dev tier, observe
on the project's live stack) and egress `packages`. You run every command yourself. The builder does not run
the trial.

The session will be called `ab-claude-abt-trial-1`, and the commands below use that name. If `ab session ls` shows
another number, use that one.

## a) Create and register the project

```bash
mkdir -p ~/repos/abt-trial
git -C ~/repos/abt-trial init
ab project add abt-trial ~/repos/abt-trial --preset pods --egress packages
ab project show abt-trial
```

`project show` should list the preset `pods`, egress `packages`, podman `dev` (live: observe),
`compose_project abt-trial`, `observe_logs false` and `dev_images none`.

Just before starting the session, take the snapshot `make trial-check` compares against. It records the live
containers (ID, start time, restart count) and the current time:

```bash
cd ~/repos/agentbox && make trial-snapshot
```

It records only containers of live compose projects, with their bind-mount sources. It also warns about any
container running from `localhost/claudecode` or `localhost/omp-env` outside agentbox (the old launchers). Stop
those first, or their writes show up in the check.

The home scan in `trial-check` reports *every* file under `~` modified after this moment, outside the trial
project and the excluded paths it prints. So avoid editing files under `~` while the trial runs, or note what
you touched.

## b) Start a Claude session in tmux

```bash
tmux new -s trial
ab run --agent claude --project abt-trial
```

Ask it for something small with a dev stack: for example, "a two-service app with podman-compose: a Python web
service and a Redis, with a test that calls the web service". Detach with `Ctrl-b d` and reattach with
`tmux attach -t trial`.

What the agent should find:
- it can build, start, exec into and remove its own containers;
- images: anything on docker.io, ghcr.io, lscr.io or quay.io (it must `podman pull` first), plus its own
  builds;
- bind mounts: the project directory or paths inside it (a path inside becomes a sub-path of the project's
  grant volume);
- no published ports, privileged mode, host network, other projects' containers or images, or Docker-API
  creates. Each is refused with a message that says why.

## c) Watch it from a second terminal

```bash
ab session ls                                           # sessions, state, container and network counts

# the proxy: every destination the session asked for, and squid's verdict
podman logs -f ab-claude-abt-trial-1-proxy

# its containers: the agent, proxy, sockguard(s), holder, and the dev stack it made
podman ps -a --filter label=agentbox.project=abt-trial \
  --format '{{.Names}}  {{.Status}}  {{index .Labels "agentbox.role"}}'

# the gate: every API call, and what it refused and why
tail -f "$XDG_RUNTIME_DIR/agentbox/services/ab-claude-abt-trial-1/gate.log" | grep --line-buffered '"deny"'
# (drop the grep to see every call; "allow" lines name the kind, e.g. libpod_create, build, start)
```

The gate log's `why` field is the message the agent got. Refusals you would not expect are the most useful
thing the trial can find.

## d) Open the app in your desktop browser

While the session runs, open one of its dev services on this host's loopback, then tunnel it to your desktop:

```bash
ab session preview ab-claude-abt-trial-1 web:8000  # <compose service or container name>:<port inside it>
# prints the port, e.g. http://127.0.0.1:41234/, and the ssh line for your desktop:
#   ssh -N -L 41234:127.0.0.1:41234 vmuser@ubuntu-dev     then open http://127.0.0.1:41234/
```

The preview listens on 127.0.0.1 only (never 0.0.0.0 or the LAN), reaches only that one service of the
session, and is removed when the session ends (or `ab session preview ab-claude-abt-trial-1 --stop`). From inside the
sandbox, the agent reaches its services as `<service>.dns.podman`.

## e) End the session and read the scan

End Claude with `/exit` (or `Ctrl-D`) in the tmux window. Teardown runs in order:
1. it stops the gate and the podman service;
2. it keeps the gate, proxy and sockguard logs;
3. it removes the session's containers, pods, volumes (including the grant volume) and networks;
4. it prints the end-of-session scan's summary in that terminal.

The full reports are kept under `~/.local/share/agentbox/logs/`, named
`ab-claude-abt-trial-1-<YYYYmmdd-HHMMSS>-<kind>.log`:

```bash
ls -t ~/.local/share/agentbox/logs/ | head
less ~/.local/share/agentbox/logs/ab-claude-abt-trial-1-*-scan.log   # secrets in commits, files that run later,
                                                                      # new executables, new git repos
less ~/.local/share/agentbox/logs/ab-claude-abt-trial-1-*-gate.log   # every API call and verdict
less ~/.local/share/agentbox/logs/ab-claude-abt-trial-1-*-proxy.log  # every destination and verdict
```

If the session was killed instead (`ab session kill ab-claude-abt-trial-1`, or `ab session gc` after a crash), the same
logs and scan are written.

Then, from the agentbox repo:

```bash
cd ~/repos/agentbox && make trial-check
```

It refuses to run while a session of `abt-trial` is still running. Otherwise it prints one PASS or FAIL line
per check, with the details under each FAIL (long lists are cut per directory):
- the live stacks are unchanged (the same container IDs, start times and restart counts as at the
  snapshot);
- nothing under `~` changed outside `~/repos/abt-trial` and the excluded paths (an mtime scan; no file
  contents are read; the exclusions are listed). Two groups are listed separately as INFO and do not fail the
  check: changes under the live stacks' bind mounts ("written by live stacks, expected") and under `~/.claude`
  and `~/.config/claude-container` ("host-side Claude configs, written outside the session");
- no container, pod, volume, network, podman service or gate of `abt-trial` remains;
- no gate-created volume over `~/repos/abt-trial` remains.

## Afterwards

Images the session built stay in podman's store (labelled, and in the gate's record for `abt-trial`).
`ab session gc` removes them after 7 days. To remove everything now:

```bash
ab project rm abt-trial
podman images --filter label=agentbox.project=abt-trial -q | xargs -r podman rmi
rm -rf ~/repos/abt-trial
```
