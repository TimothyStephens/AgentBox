# agentbox

`agentbox` (`ab`) runs coding agents (Claude Code, OMP, Pi, others) in hardened rootless podman sandboxes on this machine, one project per session, with scoped access to pods, directories and the network. **`docs/design.md` is the spec. Read it before planning anything.** Its "Spike results" and "Open items" sections are current as of 29 September 2026.

You are running directly on the host as `vmuser`, with the same power over podman and files as Tim. The sandbox you are building does not exist yet, so the rules below are what keep his other projects and live services safe. A guard hook (`.claude/hooks/guard.py`) enforces the worst cases; follow the rules even where the hook can't see (inside scripts you write).

## Hard rules

- **Never touch anything you did not create.** Live services on this host: the `media_server` stack (10.0.0.103:8088 and :8112), the compose project `deploy` (life-updates) and `ollama`, plus other projects under `~`. Do not stop, remove, exec into, restart, or change their containers, pods, networks or volumes.
- **Name and label everything you create.** Names start with `abt-` (tests) or `spike` (the spike kit); every container, pod, network and volume carries the label `agentbox-test=1`. Compose projects are always started with `-p abt-<name>`. Clean up by label (`make clean`), never with `--all` or `prune`.
- **Credentials are off limits.** Don't read `~/.ssh`, `~/.claude/.credentials.json`, `~/.config/claude-container/`, `~/.config/agentbox/claude-oauth-token`, `~/.omp/`, or any `.env`. Tests use fake tokens under `/tmp`. Anything that needs a real credential (the Claude-through-squid test) goes in a make target Tim runs himself: write it, tell him the command, and do not run it.
- **No root, no system changes.** No `sudo`, no `systemctl --user` changes, no edits to `~/.bashrc`, `~/.config/containers/`, or systemd units. If a step seems to need one, stop and ask.
- **Mounts:** test containers mount only paths inside this repo or `/tmp`. Never `--privileged`, never `--network host`.
- **If the guard blocks you, don't find another way to do the same thing.** Tell Tim what you wanted and why.

## Environment (ubuntu-dev)

- Ubuntu, kernel 7.0.0-31, rootless podman 5.7.0, netavark 1.16.1, podman-compose 1.5.0, ext4 on LVM. `vmuser` has no root.
- The podman API socket is `$XDG_RUNTIME_DIR/podman/podman.sock`. Tests start their own `podman system service` on a temp socket (see `spikes/common.sh`, `start_api`) rather than using it.
- Local models: `http://10.0.0.2:8080/v1` (another machine, may be off).
- The host is slow at times: allow 60 s on API calls before calling something a failure.

## What the spikes proved (`spikes/`, all passing as of run 5)

- Any container on podman's default network or a plain bridge network reaches the LAN; an `--internal` network doesn't. Everything agent-created must go through the gate.
- Squid egress modes (locked/packages/open) behave as designed, including refusing public names that resolve to private addresses.
- `driver_opts: isolate: "strict"` passes through podman-compose and isolates; plain networks reach each other.
- sockguard scopes list/inspect/logs by compose label and enforces bind-mount allowlists, but **does not scope exec** against a raw API client. The gate does: it inspects the target's labels before exec create and only starts exec sessions it created.
- **Builds:** `podman build --network <name>` fails on this host (`did not get container start message from parent: EOF`). What works: join a per-session holder container's network namespace, `nsoptions` network entry `Path=/proc/<pid>/ns/net`, `networkmode=2`. The podman client sends `networkmode` as `0`/`1`/`2` and the network inside `nsoptions`; it forwards any namespace path it's given as-is, so the gate must always overwrite it, drop every client namespace entry except `user`, and re-check the holder PID on each build.
- podman echoes each build `STEP` line, so match test markers on whole output lines only.
- Alpine's busybox has no `httpd`; use `busybox:1.36` for stand-in web servers.
- `spikes/spike_gate.py` is the working prototype of the gate; start from it.
- Claude Code in sessions authenticates with a subscription token (`CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`), not an API key. The model key gateway is deferred.

## Working loop

Run tests yourself and read their output; Tim should not have to copy commands or logs.

- `make test`: fast checks, no podman (unit tests plus a fake podman API, as in the spike kit's approach).
- `make itest`: real podman, `abt-` names, cleans up after itself even on failure.
- `make clean`: removes leftovers by label.
- `make spikes`: the original spikes, as a regression check for the host.
- Python 3, standard library only at runtime. Test tools go in `.venv` (`python3 -m venv .venv`).
- Commit after each green step, with a message that says what now works.
- Record any deviation from `docs/design.md` in `docs/decisions.md` (date, what, why) and update the design doc to match.

## Build order

Follow the "Build order" section of `docs/design.md`, against a dummy two-container compose project (`tests/fixtures/`), real stacks last. Each step has a gate (the tests that must pass) before the next starts.
