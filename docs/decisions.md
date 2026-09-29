# Decisions

Deviations from, and refinements of, `docs/design.md`. Newest last. The design doc is updated to match each entry.

## 2026-09-29 · Claude Code authenticates with a setup-token token, not an API key

- **What:** Claude Code gets `CLAUDE_CODE_OAUTH_TOKEN` from a dedicated token made with `claude setup-token`, kept in `~/.config/agentbox/claude-oauth-token` (regular file, owned by the user, mode `0600`; the launcher refuses anything else). The key gateway is deferred. `ab keys rotate claude` tells you to run `claude setup-token` and replaces the file.
- **How it enters the session:** the file is bind-mounted read-only at `/run/agentbox/claude-token`, and a one-line wrapper exports it just before `exec claude`. It is never on a command line and never in the container's podman config, so `podman inspect` and podman's storage do not hold it.
- **Accepted risk:** the agent can read its own token in every egress mode. It is a dedicated token that can only make model requests, and it can be revoked without logging out the host.
- **Conditions:** the token never lands in a session directory, a log, a backup or selftest output (integration tests grep for a fake token in all of them). No header-swapping proxy is built for the subscription token.
- **Why:** there is no paid API key; spike 4 showed the gateway plumbing works but has nothing to hold.

## 2026-09-29 · Claude state is per project, seeded by copy

- **What:** each project has a persistent, writable `CLAUDE_CONFIG_DIR` at `~/.local/share/agentbox/<project>/claude/`, so history and `--resume` survive between sessions. At session start the launcher copies `settings.json`, `CLAUDE.md` and `commands/` from `~/.config/agentbox/claude/` into it, overwriting only those files. The host's `~/.claude` is never mounted: on this VM it is the builder's config, hooks included.
- **Why:** a read-only mount makes Claude Code fail on writes; the design's `~/.local/share/agentbox/<agent>/` layout is replaced by `<project>/<agent>/` so all of a project's state sits in one place.

## 2026-09-29 · The agent runs as the host user, with a tmpfs home

- **What:** `--userns=keep-id`, so the agent is `vmuser` inside the container and written files stay owned by `vmuser`. `HOME` is the host home path, as a tmpfs owned by the user; project mounts sit under it at their real paths.
- **Why:** the Claude image runs as root with a root-owned `HOME=/config`, and Claude Code refuses `--dangerously-skip-permissions` as root.

## 2026-09-29 · Egress defaults to `packages`; `open` only when the project says so

- **What:** for every agent, egress is `packages` unless the project's own config says `egress = "open"`. The `pods` preset now defaults to `packages` (was `open`). A preset, including a custom one, cannot grant `open` by itself. `lockdown` stays `locked` and remains a ceiling.
- **Why:** the widest egress should be a per-project choice written down next to the project, not a side effect of choosing a preset.

## 2026-09-29 · Squid rules tightened

- **What:**
  - CONNECT only to port 443; plain HTTP only to ports 80 and 443 (except the local model rule).
  - The private list adds `0.0.0.0/8` (on Linux that is the proxy itself), `192.0.0.0/24`, `198.18.0.0/15`, multicast and reserved ranges, `::/128`, `ff00::/8`, and the VM's own addresses read at session start.
  - `locked` allows the adapter's model domains only (Claude: `api.anthropic.com`), plus `10.0.0.2:8080`.
  - `packages` adds: `pypi.org`, `.pythonhosted.org`, `registry.npmjs.org`, `.crates.io`, `crates.io`, `.github.com`, `github.com`, `.githubusercontent.com`, `.debian.org`, `.ubuntu.com`, `dl-cdn.alpinelinux.org`, `conda.anaconda.org`, `repo.anaconda.com`, `cloud.r-project.org`, `.bioconductor.org`, `bioconductor.org`. Container registries are not on it: pulls happen on the host (next entry).
  - Anything else is a per-project `extra_domains` addition.
- **Why:** the design's `open` rule (`allow all`) allowed CONNECT to any port, and its private list missed ranges that reach the proxy or the host.

## 2026-09-29 · Image pulls are controlled in the gate (step 5)

- **What:** the gate allowlists registries for `images/pull` and for pulls triggered by container create: `docker.io` (`registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com`), `ghcr.io` (`pkg-containers.githubusercontent.com`), `lscr.io`, `quay.io` (`.quay.io`). Builds are forced to never pull, so `FROM` images must already exist. Selftest adds: a pull from an unlisted registry is refused; a build with an unpulled `FROM` fails without touching the network.
- **Why:** pulls run on the host, outside squid, and an image name can carry data to any server.

## 2026-09-29 · Secret masking scope

- **What:** masks apply in every subdirectory of every grant: files named `.env`, `.env.*`, `*.pem`, `*.key` get `/dev/null`, directories named `secrets` get an empty tmpfs. Exempt: `.env.example`, `.env.sample`, `.env.template`, `.env.dev`. The walk skips `.git`, `node_modules`, `.venv`, `venv`, `__pycache__` and a per-project `mask_skip` list. Symlinks are not followed. Masks are computed at session start: a secret file created during the session is not masked.
- **Dev swap:** where a directory holds `.env.dev`, it is mounted read-write at `.env`. If there is no `.env` yet, the launcher creates an empty one (mode 0600) as the mount point.
- **Why:** `.env.example` is meant to be committed and read; some data folders hold millions of files.

## 2026-09-29 · Backups

- **What:** read-write grants are rsynced before each session to `~/.local/share/agentbox/backups/<project>/<timestamp>-<session>/`, with `--link-dest` to the previous snapshot. `.git` is kept (a restore without objects is useless for undoing git damage). Excluded: `.venv`, `venv`, `node_modules`, `__pycache__`, common build caches and the project's `mask_skip` list. Retention: the last 5 snapshots plus the newest of each day for 7 days. A warning is printed when a snapshot's size passes `backup_warn_mb`. No session can mount the backups directory.

## 2026-09-29 · Resource limits and one slice for all agentbox containers

- **Measured (29 Sep):** 9.8 GB RAM, 6 cores, 23 GB swap; the `cpu`, `memory` and `pids` controllers are delegated to vmuser; the media stack used about 1.35 GB, ollama 3.9 GB with a model loaded, life-updates and one unlabelled container about 0.45 GB.
- **Agent container:** `--memory=4g --memory-swap=4g --cpus=3 --pids-limit=4096 --oom-score-adj=900` (see the OOM-order entry below). Rootless podman can raise the OOM score, so under host memory pressure the kernel kills agent containers before live services.
- **Containers the agent creates** (gate, step 5): `--memory=1g --pids-limit=1024 --oom-score-adj=1000` unless the compose file sets its own.
- **One cap for everything:** every container `ab` starts, and every container the gate creates, runs with `--cgroup-parent=agentbox.slice`; the gate refuses a client-supplied cgroup parent. `deploy/agentbox.slice` sets `MemoryMax` (RAM − media stack − 3 GB for ollama − 1 GB headroom, capped at 6G), `MemorySwapMax=0` and `TasksMax`. Tim installs it with `make install-slice`. The launcher refuses to start while the slice is not loaded (unless `cgroup_parent` is set to empty), and warns when the slice is above 75% of its memory cap. If rootless `--cgroup-parent` with a user slice does not work on this host, the fallback is a gate budget counted across all sessions.

## 2026-09-29 · Resource names and labels are configurable

- **What:** everything a session creates is named `<prefix><agent>-<project>-<n>[-role]` and carries `agentbox.session`, `agentbox.project`, `agentbox.owner=<project>/<session>`, `agentbox.role`, plus the configured extra labels. Defaults: prefix `ab-`, label `agentbox=1`. The test suites use prefix `abt-<run>-` and labels `agentbox-test=1`, `agentbox-test-run=<run>`.
- **Why:** the design's `<agent>-<project>-<n>` cannot meet the build's `abt-` rule, and cleanup must be able to select exactly one test run.

## 2026-09-29 · Dummy fixture uses busybox httpd

- **What:** `tests/fixtures/dummy/` uses `busybox:1.36` httpd instead of nginx. **Why:** already on the host, and proven in the spikes.

## 2026-09-29 · Every tmpfs is mounted with `notmpcopyup`

- **What:** podman's tmpfs mounts copy whatever is at the mount point into the new tmpfs by default (`tmpcopyup`). A tmpfs over a `secrets/` directory therefore held a copy of the secrets. Every tmpfs the launcher creates (`/tmp`, `HOME`, masked directories) now carries `notmpcopyup`, and the argv audit refuses any tmpfs without it. Masked directories are also chowned to the agent (`U=true`) so they are usable, empty and writable. The selftest checks that a masked directory is empty.
- **Why:** found by the step 1 integration test; an earlier pass was an accident (the copied directory was unreadable to the agent, not empty).

## 2026-09-29 · Commits from the sandbox use the host's git identity

- **What:** the sandbox has no git config of its own, so only `user.name` and `user.email` are passed, as `GIT_AUTHOR_*` and `GIT_COMMITTER_*` environment variables, from the host's global git config or from `git_name`/`git_email` in `agentbox.toml`. `~/.gitconfig` is never mounted; no other host git config (hooks, credential helpers) enters the sandbox.
- **Committer marked:** the committer name is `<name> via agentbox-<agent>`, so agent commits are identifiable in `git log --format='%cn'`.

## 2026-09-29 · OOM order inside the slice: dev stack first, then the agent

- **What:** the agent container runs with `--oom-score-adj=900`; containers the gate creates get 1000. The slice stays at `MemoryMax=4400M` and the agent's own cap at 4g. When the slice or the host runs out of memory, the kernel kills the dev stack before the agent, and both before any live service (score 0). Rootless podman accepts 900 here: the user's default is 0 and raising it is allowed.
- **Worst case, accepted:** the nightly model run plus a full slice reaches all of RAM. The cap was kept deliberately, relying on the OOM scores; the slice file's comment says so.

## 2026-09-29 · Builds get a cgroup parent under the slice (gate, step 5)

- **What:** the build API takes `cgroupparent`, but the podman service treats it as a cgroupfs path, not a systemd unit. Tested on this host with a remote build whose `RUN` step read its own cgroup:
  - `agentbox.slice`: fails, `Permission denied` creating `/sys/fs/cgroup/agentbox.slice`.
  - The slice's full path while the slice is idle: the runtime creates the directory itself, and the step runs with `memory.max = max`, uncapped.
  - The slice's full path while the slice is active: fails (cgroup v2 allows no processes in an inner node).
  - A child of the active slice (`/user.slice/user-<uid>.slice/user@<uid>.service/agentbox.slice/<session>-build`): works. The step lands there, and the slice's cap applies to it as an ancestor.
- **So:** the gate sets that child path only while the session's holder container keeps the slice active (checked before each build), also sets the build's own `memory` limit, and refuses a client-supplied `cgroupparent`. The cap itself has not been load-tested (open item).

## 2026-09-29 · The OOM score is a tie-breaker inside the slice, not a guarantee

- **What:** for a kill inside a cgroup, the kernel scales `oom_score_adj` to that cgroup's limit. With `MemoryMax=4400M`, the agent's 900 against a dev container's 1000 is worth 10% of 4400M, about 440 MB. A dev container dies first only while its usage is more than the agent's minus 440 MB; a much bigger agent is killed before a small dev container. `make itest-oom` (run by Tim) tests the dev-bigger case and reports the agent-bigger case.
- **Why it matters:** "dev stack dies before the agent" holds for the usual case (a dev stack that grows), not for every case.

## 2026-09-29 · The build cap check, and when a slice is idle

- **What:** `agentbox/buildcap.py` is the part of the step 5 gate that caps builds: before every build it requires the slice to be active (`systemctl --user` reports it active and its cgroup has a finite `memory.max`), refuses a client-supplied `cgroupparent`, drops the client's memory and CPU settings, and sets `cgroupparent` to `<slice path>/<session>-build` plus its own `memory`/`memswap` (so a build is capped twice: its own limit and the slice). `make itest` proves it against a real podman service: an inactive slice refuses the build, and an active one gives the `RUN` step the gate's limit in a cgroup under the slice.
- **Found:** systemd keeps a slice active once it has been used, even when empty. `agentbox.slice` is idle only after a boot (or a manager restart) until the first session, which is exactly when an unchecked build would run uncapped. The integration test uses a never-started slice name for the idle case, since stopping `agentbox.slice` would be a systemd change.

## 2026-09-29 · Commit hooks are never skipped

- **What:** no commit uses `--no-verify`/`-n`, a `core.hooksPath` override, or `GIT_PRECOMMIT_CHECK_*=0`. The guard hook blocks all four, and the integration fixtures commit through the host's hook too.
- **Open:** the host's secret scanner (a global pre-commit hook) has no allowlist mechanism, so a false positive blocks a commit outright.

## 2026-09-29 · Step 2: session network and squid, as built

- **DNS:** podman's DNS on an `--internal` network answers `NXDOMAIN` for public names (checked with `example.com`, `pypi.org`), so it does not forward them and is not a way out. DNS stays on so the agent finds its proxy by name; the selftest keeps "direct DNS lookup of a public name fails" to catch a regression.
- **Networks:** `<session>-int` (`--internal`, `isolate=strict`: agent and proxy) and `<session>-egr` (`isolate=strict` bridge: proxy only). The proxy resolves names through the egress network's DNS.
- **Squid:** `docker.io/ubuntu/squid` pinned by digest (6.13), run with `--cap-drop=ALL --cap-add=SETUID,SETGID` (it drops to the proxy user), no-new-privileges, read-only root, tmpfs `/var/spool/squid`, `/var/log/squid`, `/run`, `/tmp` (all `notmpcopyup`), 256 MB, 256 pids, the agent's OOM score, in the slice. The pinger is off (it needs raw sockets). `via off` and `forwarded_for delete` keep the session's addresses out of outgoing requests.
- **Access log:** one line per request (time, client, method, URL, status, server address, result), kept in `~/.local/share/agentbox/logs/<session>-proxy.log` when the session ends. It holds URLs, never headers.
- **IPv4-mapped IPv6:** squid normalises `[::ffff:10.0.0.103]` to `10.0.0.103` before the ACLs, so the private list needs no `::ffff:0:0/96` entry (which would have matched every IPv4 address). Tested: refused.
- **Result (make itest, 29 Sep):** in `locked`, `packages` and `open`, every proxied negative is `403`, the model endpoint and `api.anthropic.com` are allowed, `example.com` and `pypi.org` follow the mode, a project's `extra_domains` are allowed from `packages` up, CONNECT to a non-443 port and HTTP to a non-web port are refused, and direct attempts (LAN, model host, `example.com`, `1.1.1.1`) never connect. Two concurrent sessions cannot reach each other's agent or proxy.
