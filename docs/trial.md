# The throwaway trial

A first real session on a project that does not matter: Claude Code with the pods preset (the dev tier, observe
on the project's live stack) and egress `packages`. You run every command yourself. The builder does not run
the trial.

The session will be called `ab-claude-abt-trial-1`, and the commands below use that name. If `ab ls` shows
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
ab ls                                                   # sessions, state, container and network counts

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

`ab preview` does not exist yet. It is in the design ("Preview and deploy") but not built. The dev stack has
no published ports and sits on the session's internal network, so nothing on the host or your desktop can
reach it directly. For this trial, check the app from the host instead:

```bash
podman ps --filter label=agentbox.session=ab-claude-abt-trial-1 --filter label=agentbox.role=dev --format '{{.Names}}'
podman exec <one of those names> wget -qO- http://localhost:<port>/
```

Or ask the agent to curl its own service. If you want the browser view, `ab preview` is the next thing to
build.

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

If the session was killed instead (`ab kill ab-claude-abt-trial-1`, or `ab gc` after a crash), the same
logs and scan are written.

Then, from the agentbox repo:

```bash
cd ~/repos/agentbox && make trial-check
```

It prints one PASS or FAIL line per check, with the details under each FAIL:
- the live stacks are unchanged (the same container IDs, start times and restart counts as at the
  snapshot);
- nothing under `~` changed outside `~/repos/abt-trial` and the excluded paths (an mtime scan; no file
  contents are read; the exclusions are listed);
- no container, pod, volume, network, podman service or gate of `abt-trial` remains;
- no gate-created volume over `~/repos/abt-trial` remains.

## Afterwards

Images the session built stay in podman's store (labelled, and in the gate's record for `abt-trial`).
`ab gc` removes them after 7 days. To remove everything now:

```bash
ab project rm abt-trial
podman images --filter label=agentbox.project=abt-trial -q | xargs -r podman rmi
rm -rf ~/repos/abt-trial
```
