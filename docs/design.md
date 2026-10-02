# Agent Sandbox Design

Sep 29, 2026 · @Tim

## Summary

One launcher, `agentbox` (`ab`), runs any coding agent (OMP, Claude Code, Pi, Codex) in a hardened rootless podman container on ubuntu-dev, scoped to one project, with explicit grants for directories, pods and network destinations. The hardening lives in the launcher and on the host side, so swapping the agent never weakens it.

**Goals**

- A stray `rm -rf /*` or an over-eager "fix" can only touch what the session was granted.
- Projects never see each other: files, agent sessions, browser profiles, caches, networks, pods.
- Agents can inspect and troubleshoot specific pods through a policy enforced outside the sandbox, not a shim inside it.
- Egress is set per project (locked, packages or open), and your LAN and host services are never reachable.
- Every guarantee is checked by a selftest, not assumed.

* Agents get as much power as the project allows: web access, package installs and running their own pod stacks, all inside those bounds.

**Non-goals**

- Stopping a deliberate kernel or runtime escape. A container on a shared kernel is not a VM boundary; a separate agent VM is a future option.
- Stopping deferred execution entirely. Code the agent writes and you later run executes as you; the design narrows this, it cannot remove it.

## Environment and constraints

The design targets one VM that is both the dev environment and a home server, with no root for the working user.

| Item | Value | Consequence |
| --- | --- | --- |
| Host | ubuntu-dev, Ubuntu, remote dev VM | All projects and home services share one kernel and one user |
| User | `vmuser`, no root | No AppArmor profiles, no bwrap on the host, no LVM snapshots |
| Kernel | 7.0.0-31 | Landlock ABI 8; path-based Unix socket blocking arrives in 7.1 (ABI 9) |
| Filesystem | ext4 on LVM | No reflink snapshots; use hypervisor snapshots and git |
| Container engine | Rootless podman 5.7.0, pasta networking | Container escape lands as `vmuser` |
| Network backend | netavark 1.16.1 | Bridge networks are not isolated by default; `isolate=strict` must be set |
| Compose | podman-compose | Every container carries `io.podman.compose.project=<name>` |
| Local models | `http://10.0.0.2:8080/v1` on another machine | Must be allowlisted through the egress proxy |
| Remote models | Cloud APIs over HTTPS | Allowlisted by domain |

**Services already on this VM**

| Service | Exposure today | Notes |
| --- | --- | --- |
| media\_server (gluetun, caddy, \*arr, deluge, jellyfin) | `10.0.0.103:8088`, `:8112` (LAN, intended) | All share gluetun's network namespace on network `trashguides` |
| life-updates (`web`, `collector`) + ollama | `*:8000`, `*:11434` | Compose project named `deploy`; ollama is only used by life-updates |
| js-render-server | `0.0.0.0:8082` (stopped) | Ad hoc `podman run`, no compose label |
| glances | `0.0.0.0:8003` | Host process; exposes the process list |
| two python3 servers | `0.0.0.0:8004`, `:8006` | Host processes, not yet identified |

## Threat model

The design defends against mistakes and misdirected agents, not a determined kernel exploit.

| Threat | Example | Handled by |
| --- | --- | --- |
| Accidental destruction | `rm -rf /*`, deleting a state directory | Only granted paths are mounted; read-write grants backed up first |
| Agent self-repair | Killing processes, `mount`, renaming shared state, reverse-engineering its own code (all seen in the probes) | Zero capabilities, seccomp, `no-new-privileges`, per-session state |
| Cross-project leakage | Reading another project's transcripts, browser cookies, service definitions, caches | Per-project mounts, per-session `run` tmpfs, per-project caches |
| Lateral network access | Hitting another project's ports, the media stack, the LAN | Internal session network; proxy refuses private and host addresses in every egress mode |
| Agent-built images and containers | A Containerfile `RUN curl 10.0.0.103:8088`, a container on podman's default network | Host-side gate pins every container and build to the project network and proxy |
| Over-broad pod access | `podman run -v ~:/h`, `rm -f` on a live pod | Gate + sockguard: own dev stack only, binds inside the project |
| Exfiltration over open egress | An injected agent sending data out | No host secrets in the sandbox (only a dedicated, revocable model token); `packages` by default, `open` only when the project says so; `locked` for sensitive projects; image pulls allowlisted by registry |
| Prompt injection | Web page or repo text telling the agent to escape | Same controls; injection gains nothing the agent could not already do |
| Deferred execution | Git hooks, compose volume lines, downloaded code run later by you | Read-only hooks and config, `ab deploy` diff and policy check, lockfiles |

**Residual risk.** A kernel or runtime escape lands as `vmuser`, which owns every project, every service and the keys on this VM. Moving agents to a dedicated user or a separate VM shrinks that; see Open items and future options.

## Findings that motivated the design

The current scripts isolate less than they appear to, and none of OMP's features needed the privileges it was given.

**podman-allow (the in-sandbox shim)**

- `run` and `create` pass through in every mode, so `podman run -v ~:/h alpine rm -rf /h` is allowed.
- Only the first container argument is checked: `podman rm -f allowed other` passes.
- Destructive verbs (`rm`, `kill`, `rename`, `commit`) are allowed on listed pods; discovery (`ps`, `pod ps`) is denied.
- Bypassable by design: `unset PODMAN_ALLOW`, `/usr/bin/podman`, curl to the socket, or editing `~/.omp/bin/podman`.
- The forwarding fallback writes under `/run/podman`, which `vmuser` cannot create, so it likely never started.

**omp-workspace**

- `--privileged` plus unconfined seccomp and AppArmor, with no feature needing them.
- `--network=host` exposes every host and LAN port, including other projects' services.
- The whole `~/.omp` is mounted read-write, including OMP's installed code.
- Every project mounts at `/workspace`, so all sessions land in one `--workspace--` bucket.
- `~/.omp/run` is shared by every session: background-service sockets, service definitions and one Chromium profile.

**claudeCode\_start**

- Already well hardened (`--cap-drop=all`, `--read-only`, `no-new-privileges`); it becomes the baseline.
- Mounts `.` unvalidated and shares one config directory across projects.

**Probe results (hardened run of the existing OMP image)**

| Test | Result |
| --- | --- |
| Security posture | `CapEff 0`, `NoNewPrivs 1`, `Seccomp 2` |
| OMP boot + model call | Worked |
| Debugger (debugpy launch, breakpoint) | Worked; agent had to pip-install `debugpy` and symlink `python` |
| Shared browser | Worked after clearing stale profile locks left in the shared `run` directory |
| Agent behaviour when blocked | Tried `mount`, killed processes, grepped its own source, deleted shared state |

## Architecture

&#91;embedded content: session architecture · two host-side gates\]

Only the launcher calls podman. Inside a session, the agent's only ways out are the allowlist proxy and a podman socket filtered by the gate and sockguard, limited to its own project's stacks; everything else has no route or gets a `403`.

## Components

Each component owns one guarantee, and only the launcher ever calls `podman run`.

### Launcher

- A Python CLI, `agentbox` (alias `ab`), configured under `~/.config/agentbox/`.
- Reads the project registry, the chosen profile and the harness adapter, validates everything, then builds the session: container, network, proxy, sockguard, mounts.
- Replaces `omp-workspace`, `claudeCode_start` and `podman-allow`.

### Fixed hardening

Applied to every session; no adapter or profile can override it.

- `--cap-drop=ALL`, `--security-opt=no-new-privileges`, podman's default seccomp profile.
- Never `--privileged`, never `seccomp=unconfined` or `apparmor=unconfined`, never `--network=host`.
- Read-only root filesystem with tmpfs for `/tmp`; PID and memory limits.
- `--userns=keep-id`, so the agent runs as `vmuser` and written files stay owned by `vmuser`; `HOME` is a tmpfs at the host home path.
- Limits: `--memory=4g --memory-swap=4g --cpus=3 --pids-limit=4096 --oom-score-adj=500` (sized from ubuntu-dev: 9.8 GB RAM, 6 cores). The OOM score makes the kernel kill agent containers before live services (host-wide, 500 is worth about 17 GB of RAM plus swap); containers the gate creates get 1000, so inside the slice a dev stack dies before the agent unless the agent uses more than 2.2 GB beyond it (see decisions.md).
- Every agentbox container, including the ones the gate creates for the agent, runs under `--cgroup-parent=agentbox.slice`, one kernel-enforced memory, swap and task cap for all sessions together (`deploy/agentbox.slice`, installed with `make install-slice`).

### Harness adapters

The only per-agent code. An adapter declares, and the launcher validates:

- image and launch command;
- read-only config paths; the launcher rewrites the model config so cloud providers point at the key gateway with no keys;
- read-write state, scoped per project, under `~/.local/share/agentbox/<project>/<agent>/`;
- secrets as read-only single files, only where the harness cannot use the gateway;
- paths that must be tmpfs (OMP's `run`);
- a generated `AGENTS.md` describing the real sandbox, including the project's preset and egress mode;
- how the preset's approval setting maps to the harness's own flag (e.g. Claude Code's `--dangerously-skip-permissions` when approvals are off);
- tools that must exist in the image (checked by selftest);
- harness-internal sandboxes switched off, since the container is the boundary.

### Project registry and profiles

```toml
[project.helper]
root    = "~/repos/helper"
preset  = "pods"                  # lockdown | pods | python (see Presets)

[project.dataproc]
root    = "~/repos/dataproc"
preset  = "python"
data_ro = ["~/data/raw"]
data_rw = ["~/data/processed"]
egress  = "open"                  # override: this project may search the web
mask_skip = ["data/cache"]        # folders the secret-mask walk skips (huge trees)
build_memory = "4g"               # per-build memory limit in the dev tier (default 2g, at most the slice cap)
extra_domains = ["huggingface.co"] # added to the egress allowlist
```

- A project names a preset; any setting written on the project overrides the preset's value. Fixed hardening is never overridable.
- Data grants (`data_ro`, `data_rw`) sit beside the code grant, so inputs can be read-only while outputs are writable.
- One compose project per session by default. Interconnected projects get an explicit project group, never ad hoc multi-project sessions.

### Filesystem policy

- Projects mount at their real absolute path, so agent sessions and compose relative paths line up.
- Validator: `realpath` every grant; refuse `/`, `$HOME`, its ancestors, `/etc`, `/var`, `/usr`, `/boot`, `/dev`, `/run`, `~/.ssh`, `~/.gnupg`, podman storage.
- A grant may not contain or overlap another registered project.
- Compose files: read-write when the agent owns the dev stack (the `pods` preset), read-only with an outbox otherwise. The live stack only changes through `ab deploy`.
- `.env` files are masked (a read-only empty file bound over them) unless `--with-secrets` (see Project secrets for the full mask list).
- `hooks/`, `config` and `config.worktree` are overlaid read-only for every git directory that exists inside a read-write grant when the session starts: the root's `.git`, nested repositories anywhere in the tree, the targets of `.git` files (worktrees, submodule checkouts, `--separate-git-dir`), bare repositories, and submodule git dirs under `modules/`. A `.git` file pointing outside every grant is not mounted, so git does not work there and its hooks are out of reach. Folders in `mask_skip` are not searched. A repo the agent creates during a session has writable hooks and config: the end-of-session report names it (NEW GIT REPO) and never runs git in it; check its `.git/config` and hooks before you use git there.
- Other files that run later are only documented, not overlaid: `Makefile`, package scripts, `.pre-commit-config.yaml`, editor tasks, Python build scripts, `.envrc`, a project `.venv` (its `.pth` files run on every interpreter start). They are code you choose to run; review the diff first. The end-of-session report (step 5) lists which of them changed, by content hash, ignored files included (a gitignored `.envrc`, `.venv` `.pth` files), and those changed in commits on any branch. `.envrc` is not auto-run by direnv until it is allowed again after any change, and direnv, mise and asdf are not installed on ubuntu-dev (30 Sep).
- Deferred-execution denylist, never writable: `~/.config/containers`, `~/.config/systemd`, `~/.config/autostart`, `~/.local/bin`, shell rc files.
- Read-write grants are rsynced to `~/.local/share/agentbox/backups/<project>/<timestamp>-<session>/` before the session starts, as `--link-dest` snapshots (unchanged files shared). `.git` is kept; `.venv`, `venv`, `node_modules`, `__pycache__`, build caches and the project's `mask_skip` folders are not. Kept: the last 5 snapshots plus one per day for 7 days. A warning is printed above a configured size. No grant may include the backups directory.

### Network and egress

- Each session gets its own `internal: true` network with `isolate=strict`: no route out except through the proxy.
- An allowlist proxy (squid) sits on that network and on an egress network. The agent, and every container it starts, gets `HTTPS_PROXY`/`HTTP_PROXY`. A client that ignores the proxy simply cannot connect.
- Every egress mode refuses private, loopback, link-local and the VM's own addresses (a configured list, `host_addrs`, plus those found at session start; a session refuses to start if the list is empty). Squid checks the resolved IP, so a public name pointing at `10.0.0.x` is refused too. Every request is logged, as a destination (`host:port`), never a URL path or query.
- The session networks are IPv4-only by decision, so no IPv6 destination is routable from a session. (A deny rule for global IPv6 is not used: squid's `dst` ACL matches if any address of a name matches, so it would refuse every dual-stack host.) The selftest checks a proxied IPv6 literal fails.

| Egress mode | Reachable | Use for |
| --- | --- | --- |
| `locked` | Model endpoints only | Sensitive projects, review-only work |
| `packages` (default) | + PyPI, npm, crates.io, GitHub, Debian/Ubuntu/Alpine, conda, CRAN, Bioconductor | Scripting and builds that need dependencies |
| `open` | + any public host | Web search, fetching fixes, trying new packages |

- Egress is `packages` unless the project's own config says `egress = "open"`; no preset grants `open` by itself. `lockdown` is always `locked`.
- Container registries are not on the squid list: image pulls run on the host, and the gate allowlists registries (see Host-side gate).
- Project-specific domains go in the project's `extra_domains`.

```
# always
acl localmodel dst 10.0.0.2
acl modelport  port 8080
acl private    dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12
               192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4
               ::/128 ::1 fc00::/7 fe80::/10 ff00::/8 <the VM's own addresses>
acl sslport    port 443
acl webports   port 80 443
acl models     dstdomain <adapter model hosts, e.g. api.anthropic.com>
http_access allow localmodel modelport
http_access deny  private
http_access deny  CONNECT !sslport
http_access deny  !webports
http_access allow models
# packages: + http_access allow <package list> and <extra_domains>
# open:     + http_access allow all
http_access deny  all
```

- `10.0.0.2` must not be in the sandbox's `NO_PROXY`.

### Model key gateway

Deferred (decisions.md, 30 Sep): keys enter the session as dedicated, spend-capped files instead. The gateway returns if a provider's key cannot be capped or scoped.

- API keys for cloud models live in a small gateway on the session network (a LiteLLM-style proxy), never in the agent container.
- The agent's model config points at the gateway with no keys; the gateway adds them and forwards through the proxy.
- With open egress, the most an injected agent can send out is what the sandbox holds: project code and granted data, not your credentials.

### Secrets and credentials

A secret the agent's dev stack uses is a secret the agent can read, so the agent works with dev credentials and real ones only exist at deploy.

**Project secrets**

- Masked by default, in every subdirectory of every grant: files `.env`, `.env.*`, `*.pem`, `*.key` get a read-only empty file bound over them (not `/dev/null`: a device node in the tree breaks rootless builds), `secrets/` directories an empty tmpfs. `.env.example`, `.env.sample`, `.env.template` and `.env.dev` are exempt. The walk skips `.git`, `node_modules`, `.venv`, `venv`, `__pycache__` and the project's `mask_skip` folders, and does not follow symlinks.
- Masks are computed at session start: a secret file created during a session is not masked.
- **The dev swap.** Inside the sandbox, the launcher mounts `.env.dev` at the path `.env`, so compose variable substitution, `env_file:` and dotenv-style loaders all read dev values with no code changes. The real `.env` is never mounted; `ab deploy` uses it on the host.
- Other file names use a per-project mapping, e.g. `swap = { ".env" = ".env.dev", "secrets.env" = "secrets.dev.env" }`. A masked file with no mapped dev file stays empty.
- The dev file is mounted read-write so the agent can add variables. `ab deploy` compares variable names (never values) between the dev and real files and refuses to deploy while the real file is missing any, listing them.
- Commit a `.env.example` with names and dummy values; git-ignore both `.env` and `.env.dev`.
- `.env.dev` holds low-value dev credentials (separate keys with spending limits, throwaway passwords). The agent sees them and the dev stack uses them.
- Podman secrets do not hide values from the agent: it can exec into its own containers and read `/run/secrets/`.
- The live stack stays opaque at the `observe` tier, since sockguard redacts container environment in `inspect` by default. The `exec` tier exposes it.
- Masking does not cover git history: a `.env` ever committed is visible through `git log -p`. `ab project add` refuses a project whose history (in any repo inside its grants) holds masked file names, naming them and saying to rotate them, until `--accept-history-secrets` is passed; the accepted names are stored on the project. A session refuses any such name that was not accepted (`ab project accept-history-secrets <project>` accepts the current set).

**Model credentials**

- One file per provider, `~/.config/agentbox/keys/<provider>` (0600), set with `ab keys set <provider>`, which warns that a pay-per-use key should be dedicated to agentbox and spend-capped at the provider. Keys are removed from `~/.omp/agent/models.yml` so there is one place to update.
- A key enters a session like the Claude token: its file is mounted read-only and exported as `AB_KEY_<PROVIDER>` just before the agent starts, so it is never on a command line or in podman's config; the generated model config names the variable. Only providers with a key are offered, and their API domains are allowed in every egress mode (decisions.md lists them). The agent can read the keys it holds; with a dedicated, capped key that is bounded by the cap, so the key gateway below stays deferred.
- Claude Code uses a dedicated subscription token from `claude setup-token`, kept in `~/.config/agentbox/claude-oauth-token` (0600) and rotated with `ab keys rotate claude`. It is mounted read-only into the session and exported as `CLAUDE_CODE_OAUTH_TOKEN` just before the agent starts, so it is never in podman's container config. The agent can read it; that is accepted because it only makes model requests and is revocable. It never lands in session directories, logs, backups or selftest output: `make itest` checks what the launcher writes with a fake token, and runs Claude against a local stub API that accepts that token, so Claude's own post-authentication writes are checked too; `make claude-live` repeats the check with the real token and API. The host's own login (`~/.claude`) is never mounted.

**How host edits reach a running session**

| How a piece arrives | Edit on the host |
| --- | --- |
| Directory bind mount (install dirs, session history) | Visible immediately |
| Single-file bind mount (`config.yml`, `settings.json`) | Only if rewritten in place; most editors save by write-and-rename, which the container does not see. Use `ab reload <session>`. |
| Generated at session start (`models.yml`, `AGENTS.md`, squid rules) | Next session, or `ab reload <session>` |
| Key files | Next session (they are read at start) |

### Podman access via sockguard

One host-side gate and one sockguard per session. The sandbox gets only the gate's 0600 Unix socket; the gate forwards to sockguard, and sockguard to a podman API service the launcher starts for the session, in a transient scope under `agentbox.slice`, on a socket in its private run dir (no user-wide podman socket is enabled on ubuntu-dev); `ab gc` stops services left by crashed sessions. The observe tier alone mounts its sockguard's socket at `/run/agentbox/podman.sock`. The dev tier gets the gate there (gate → a dev sockguard with owner isolation → the service), and with observe on the live stack too, the live sockguard is a second socket, `/run/agentbox/podman-live.sock`.

| Tier | Agent can | Enforced by |
| --- | --- | --- |
| none (default) | Nothing | No socket mounted |
| observe | container list, inspect through the Docker API (environment redacted), per-container stats, on the live compose project; logs only with the project's `observe_logs = true` (off by default: logs can hold secrets, and what the agent reads goes to its model provider). No libpod inspect (its create command holds `-e` values), no `top`, no pods (not label-scoped) | Visibility scoped by `io.podman.compose.project` label; everything else denied. With the project name as the default stack, the stack's containers must come from inside the project root |
| exec | observe + exec and restart in those containers | Exec command allowlist; exec exposes everything that container mounts |
| dev | Build images (including `RUN`), create, start, stop and remove its own `<project>-dev` stack, exec into it, read its logs | Gate + sockguard: owner label, binds inside the project root, no host ports, project network only |

- Never granted: touching the live `<project>` stack beyond observe/exec (including deleting its volumes, or mounting any volume the session did not create), other projects' containers, privileged mode, host networking or namespaces, host port publishing. The dev tier may delete its own volumes.
- Extra capabilities or devices a compose file needs (e.g. `NET_ADMIN`, `/dev/net/tun`) are opt-in per project.
- Image pulls happen on the host, outside the proxy; the gate's and sockguard's registry allowlist (`docker.io`, `ghcr.io`, `lscr.io`, `quay.io` by default) is the control there.
- Memory, CPU and PID limits apply to every container the agent starts.

### Host-side gate

Sockguard can refuse dangerous settings, but it cannot force a network, inspect port bindings, or scope exec against a client that drives the API directly (spike 2). A small gate in front of it rewrites and checks every request from the agent:

- every container the agent creates goes onto the session network `<session>-int` (aliases kept); whatever network the client asks for is replaced, and host, `ns:`/path, `container:`, pasta and slirp4netns modes are refused. Pods with an infra container are refused (the dev sockguard cannot resolve pods, so no container could join one); podman-compose's pods have none, so membership is dropped and each container is pinned like any other. Docker-API container creates are refused too: sockguard refuses the cgroup parent that keeps them in the slice;
- builds run on that network by joining a per-session holder container's network namespace: the gate sets the build's network entry in `nsoptions` to `/proc/<pid>/ns/net`, drops every other namespace entry except the client's `user` one, re-checks the holder's PID on every build (PIDs are reused), and passes the proxy in as build arguments;
- any host port publishing is rejected;
- image pulls (`images/pull`; creates never pull, and an image volume must name an image already present) are allowed only from listed registries: `docker.io`, `ghcr.io`, `lscr.io`, `quay.io`; builds never pull, so `FROM` images must already exist;
- every created container gets `--cgroup-parent=agentbox.slice` (a client-supplied cgroup parent is refused) and, unless the compose file sets its own, `--memory=1g --pids-limit=1024`, plus `--oom-score-adj=1000`;
- builds run inside the podman service, not under `ab`, so the gate sets the build API's `cgroupparent` to a child of the slice, as a full cgroupfs path: `/user.slice/user-<uid>.slice/user@<uid>.service/agentbox.slice/<session>-build`. The service treats the value as a path, not a systemd unit: `agentbox.slice` alone fails (`Permission denied` on `/sys/fs/cgroup/agentbox.slice`); the slice's own path fails while the slice is active (cgroup v2 allows no processes in an inner node) and, while it is idle, is created by the runtime with no cap. A child path works while the slice is active, which it is whenever the session's holder runs, and the gate checks that before every build. The gate also sets the build's own `memory` limit from the project's `build_memory` (default `2g`, at most the slice's cap), refuses a client-supplied `cgroupparent`, and refuses the build if the slice is not active (only possible before any session exists);
- exec is scoped: before creating an exec session the gate requires the target container to carry `agentbox.dev-owner=<session>` and `agentbox.role=dev` and to have passed the gate's post-create check, and it only starts, resizes or removes exec sessions it created on such a container;
- binds: a grant root itself is bound as is; a path below one becomes a `SubPath` of the gate's own bind-backed volume over that root, which podman resolves inside the volume, so a symlink cannot lead out even if swapped in before start (decision, 1 Oct). Images: the dev tier runs and builds only from allowlisted-registry images, this session's own builds, or the project's `dev_images`; the gate reads a build's Containerfile before forwarding it;
- every create's top-level fields must be ones the gate knows; host paths in any field (binds outside the grants, symlinks out, relative sources, rootfs, overlay volumes, init path, conmon PID file, log path, health log directory, hosts file, runtime) and privilege (privileged, capabilities, devices, security options, host namespaces, sysctls outside `net.*`, runtime annotations, the service's environment, podman secrets, dependency containers, cgroups off, OOM killer off, unlimited memory or pids) are refused. Named volumes must be the session's own (a new name is created labelled first); `volumes_from` is refused;
- after every create the gate inspects what podman stored and judges that; on a violation, or a reply it cannot read, it removes the object and refuses. Only objects that passed are recorded (by ID) as the session's, and every request naming a container, pod, volume or network needs both the record and the dev labels, so nothing can be used while its check runs;
- start, restart and init re-check the container from what podman stored, and after the start the gate reads `/proc/<pid>/mountinfo` and removes a container whose binds or grant volumes came from outside the grants;
- build tags must be `localhost/` names, never `localhost/agentbox-*`, and never a name an image outside this project's dev builds already has; build query keys must be known (cache export, local outputs, added capabilities, volumes, remote contexts and extra contexts refused), and build secrets come only from the context. The session's podman service has its own `TMPDIR`, so `RUN --mount=type=cache` is never shared with the host's builds;
- push, manifests, registry login, commit, export, archive, checkpoint/restore, update, mount, kube play/generate, image tag/untag/remove/load/import, network connect/disconnect, prune, secrets, quadlets, artifacts and system operations are refused by the gate itself, as well as by sockguard; the request path must be canonical, and pipelined requests are refused;
- joining a pod is allowed only for pods the session owns; joining another container's namespaces is refused;
- proxy variables are injected into every container, so app containers reach the internet only through the proxy.

Without the gate, a container created with no network, or a `podman build`, runs on podman's default network with unfiltered access to your LAN.

### Session lifecycle

- Sessions are named `<prefix><agent>-<project>-<n>` (prefix `ab-`; tests use `abt-<run>-`); everything the session creates carries the owner label `agentbox.owner=<project>/<session>`, plus `agentbox.session`, `agentbox.project`, `agentbox.role` and the configured extra labels (`agentbox=1`).
- `ab kill` removes the container, its network, proxy, gateway, gate, sockguard instance and every labelled container.

### Preview and deploy

- `ab preview <project> <service:port>` starts a launcher-run forwarder on `127.0.0.1` into the dev stack; you view it from your desktop with `ssh -L`.
- `ab deploy <project> --lan 10.0.0.103:<port>` validates the compose file (binds inside the project, no privileged or host networking, ports only on the address named), checks the real .env has every variable name in .env.dev, shows the diff since the last deploy, asks for confirmation, then starts the live `<project>` stack through a sockguard deploy profile, never the raw socket.

### Workspace UI

- The tmux layout from `omp-workspace` (agent pane, two shells, btop, monitor) becomes `ab ws`, and every pane that enters the sandbox goes through the launcher.

## Presets

A preset is a named bundle of the settings above, so one word gives a project a known-good configuration. Presets change nothing the fixed hardening guarantees; they only choose among the per-project options.

| Setting | `lockdown` | `pods` | `python` |
| --- | --- | --- | --- |
| For | Sensitive projects, review and careful edits | Building, fixing and testing compose stacks | Scripts and data processing |
| Code | Read-write, backed up | Read-write, backed up | Read-write, backed up |
| Compose files | Read-only, changes via outbox | Read-write; live stack changes only via `ab deploy` | Read-only, changes via outbox |
| Data grants | Read-only only | None unless declared | As declared (`data_ro`, `data_rw`) |
| `.env` files | Masked; `--with-secrets` refused | Masked unless `--with-secrets` | Masked unless `--with-secrets` |
| Egress | `locked` | `packages` (`open` only if the project says so) | `packages` |
| Podman tier | none | dev on `<project>-dev`, observe on the live stack (no logs unless `observe_logs = true`) | none |
| Extra capabilities, devices | Refused | Opt-in per project | Refused |
| Agent approval prompts | On | Off (`--auto`) | Off (`--auto`) |

**Overrides**

- Any setting written on the project, or passed as a flag, overrides the preset: e.g. `python` with `egress = "open"` for web search.
- Every preset defaults to `packages` or tighter. `open` comes only from `egress = "open"` written on the project, never from a preset. A stack that holds personal data (e.g. life-updates) should stay on `packages`, or use `lockdown`.
- `lockdown` is a ceiling: overrides may only tighten it. To loosen, switch the project to another preset.
- `ab project show <project>` prints every effective setting and whether it came from the preset or an override.

**Custom presets** are TOML files in `~/.config/agentbox/presets/`, for example:

```toml
# ~/.config/agentbox/presets/pods.toml
compose   = "rw"
egress    = "packages"
podman    = { tier = "dev", live = "observe" }
caps      = "opt-in"
secrets   = "masked"
approvals = "off"
```

## Failure and recovery

A power loss or reboot ends every running session, but everything written to the host survives, and sessions resume from their agent's saved history.

| After a reboot | State |
| --- | --- |
| Code edits, data outputs, outbox, pre-session backups | Kept (host disk) |
| Agent conversation history | Kept; resumable with the agent's continue flag |
| Project `.venv` and other files inside the project | Kept |
| Packages installed into the container itself | Lost; the container filesystem is discarded |
| In-flight commands, the tmux workspace, `run` tmpfs, browser session | Lost |
| Session proxy, gateway, gate, sockguard | Gone; restarted per session |
| Agent dev stacks (`<project>-dev`) | Stay stopped: the gate strips restart policies from agent-created containers |
| Leftover session networks and stopped containers | Remain until cleaned up |
| Live stacks (media, life-updates, `ab deploy`) | Restart only if configured to (below) |

**Bringing sessions back**

- `ab gc` removes everything carrying a dead session's owner label: containers, networks, sockets. A session whose launcher is gone but whose agent container still runs is left alone with a warning (`ab kill` removes it); an orphaned proxy or network is removed. Sessions tear down on exit, Ctrl-C, SIGTERM and SIGHUP. The launcher runs it automatically on its first use after boot.
- `ab resume <session>` recreates the session with the same project, preset and agent, and starts the agent with its continue flag.
- Power loss mid-write can leave a truncated file; ext4's journal protects the filesystem, not the file. Git plus the pre-session backup cover this.

**Bringing live stacks back**

- Rootless containers do not restart on boot by themselves. They need a restart policy (`restart: unless-stopped`), podman's `podman-restart.service` enabled for your user, and lingering enabled so your user's services start without a login.
- `ab deploy` sets the restart policy and checks the other two, warning if either is missing.
- Check today: `loginctl show-user vmuser -p Linger` and `systemctl --user is-enabled podman-restart.service`.

## Selftest

`ab selftest --agent <a> --project <p>` proves the boundaries hold, and runs on every image rebuild and every new adapter.

**Two classes of network test**

| Class | How it's sent | Pass condition | Target must be live? |
| --- | --- | --- | --- |
| Proxied | Through `HTTPS_PROXY` | Negatives: squid `403` only. Positive: anything except `403` (a `502`/`503` means allowed but down). | No |
| Direct | `curl --noproxy '*'` | Network-level failure (unreachable or timeout). A `Connection refused` counts as a leak. | Yes |

- A host-side baseline runs first on every selftest. Direct targets that return `000` from the host are skipped, not passed; zero live direct targets fails the run.
- Negatives always target the VM's address (`10.0.0.103`), never `127.0.0.1`, which inside a sandbox is the sandbox itself.

**Cases**

| Check | Expected |
| --- | --- |
| `/proc/self/status` | `CapEff 0`, `NoNewPrivs 1`, `Seccomp 2` |
| Model API keys in the sandbox environment or files | Absent (the session's own model token is the only credential) |
| The model token in podman's container config, logs, backups, session directories, selftest output | Absent |
| Direct DNS lookup of a public name | Fails |
| Proxied IPv6 literal | Not reachable (IPv4-only networks) |
| Write outside grants, write to `.git/hooks`, read `~/.ssh` | Fail |
| Read another project's root or session bucket | Fail |
| Proxied `http://10.0.0.2:8080/v1/models` | Not `403` |
| Proxied `10.0.0.2:22`, `10.0.0.103:8088`, `10.0.0.103:8003` | `403` in every egress mode |
| Proxied public name that resolves to a private address | `403` |
| Proxied `example.com` | `403` in `locked` and `packages`; not `403` in `open` |
| Proxied `pypi.org` | `403` in `locked`; not `403` otherwise |
| Direct `10.0.0.2:8080`, `10.0.0.103:8088`, `10.0.0.103:8003`, `example.com` | Unreachable |
| Connect to the real podman socket path | Fail |
| `podman run -v $HOME:/h`, `podman stop allowed other` | Denied |
| Create a container with no network, or on another network | Lands on `<session>-int`, or denied |
| Create a container with a published port | Denied |
| Build with `RUN curl 10.0.0.103:8088` | Build step fails |
| `podman logs`/`exec` on another project's container | Denied (checked against a canary container the selftest creates in a project of its own) |
| Observe tier: list shows only the live stack; inspect redacts the environment; exec, stop, create, pull | Only the live stack; empty environment; all denied |
| Adapter's required tools (e.g. `python`, `debugpy`) | Present |

## Adapters

The OMP adapter needs nothing beyond the fixed hardening; its work is in splitting `~/.omp` and fixing the image.

**OMP**

| Piece | Setting |
| --- | --- |
| Security flags | Fixed hardening only |
| Image | `localhost/agentbox-omp`, derived from `omp-env` (`make omp-image`): `/root` opened for the keep-id user, `/root/.omp` linked to `~/.omp`, plus `python-is-python3`, `debugpy`, Chromium, podman-remote 5.7.0 (pinned, checksum-verified, first on PATH) and the compose tool 1.5.0 |
| What comes from the host's `~/.omp` | A whitelist, all read-only: `plugins`, `repos`, `pylibs` from their actual link targets, each of which must be exactly `versions/<v>/<name>` (they may be in different versions); the `versions/<v>/` directories those live in or the plugins link into; refused if a credential file in `~/.omp` is hard-linked or a mounted repo's git config has credentials in a URL; `agent/config.yml`; `bin/mags-codedev`; `.cache/ms-playwright`. Nothing else (decisions.md lists what is left out and why) |
| MCP and MAGS config | Only if Tim creates `~/.config/agentbox/omp/mcp.json` / `mags-codedev.yaml` himself: mounted read-only at OMP's paths, never copied from `~/.omp` (the credential rule in decisions.md) |
| `~/.omp` in the session | A tmpfs owned by the agent, with the pieces above mounted on it; `run/` and caches live there and vanish with the session |
| Model config (`models.yml`) | Generated per session from a keyless seed (`~/.config/agentbox/omp/models.yml`): only providers with a key file (`ab keys set`), plus the local model server always; keys are named by variable, exported at start like the Claude token |
| Sessions, OMP's databases, logs | Per project, read-write: `~/.local/share/agentbox/<project>/omp/{agent,logs}` at `~/.omp/agent` and `~/.omp/logs`; sessions are named after the project's real path. The places OMP loads code from (`agent/extensions`, `hooks`, `tools`, `.mcp.json`, `mcp.json`, `settings.json`) are covered read-only and empty, so a session cannot plant code for later ones |
| Browser | The image's Chromium through `PUPPETEER_EXECUTABLE_PATH`, proxied with `PUPPETEER_PROXY`; its shared daemon runs in the session's `run` tmpfs |
| Approval prompts | OMP's own `--approval-mode`: `yolo` when the preset turns approvals off, `always-ask` when on (OMP's default is `yolo`) |
| `AGENTS.md` | Generated per session, read-only |
| Old `--workspace--` and `-tmp*` buckets | Never mounted (the host's `agent/sessions` is not mounted at all) |

**Claude Code**

| Piece | Setting |
| --- | --- |
| Security flags | Fixed hardening (the existing script's flags) |
| Image | `localhost/agentbox-claude`, derived from `claudecode` (`make claude-image`): podman-remote 5.7.0 (pinned, checksum-verified, first on PATH), python3 and the compose tool 1.5.0 |
| Model access | Dedicated `claude setup-token` token, mounted read-only and exported as `CLAUDE_CODE_OAUTH_TOKEN` at start; the host's `~/.claude` is never mounted |
| Per-project state | The whole `CLAUDE_CONFIG_DIR` per project at `~/.local/share/agentbox/<project>/claude/`, read-write and persistent (history, `--resume`); `settings.json`, `CLAUDE.md` and `commands/` copied in from `~/.config/agentbox/claude/` at each start |
| User | `vmuser` via `keep-id` (Claude Code refuses to skip permissions as root) |
| Network settings | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`; model domain `api.anthropic.com` |
| Approval prompts | Follow the preset: `--dangerously-skip-permissions` and a pre-trusted folder when off; when on, no pre-trust and a read-only managed policy (`/etc/claude-code/managed-settings.json`: bypass mode disabled, only managed hooks and permission rules), so a repo's own settings cannot switch approvals off |
| Internal sandbox | Off; the container is the boundary |

**Pi, Codex** — same shape: image, command, read-only config, per-project state. A new adapter should be about 20 lines.

## Host migration

Existing stacks change one at a time, each after a hypervisor snapshot, and only after the launcher passes its selftest on a dummy project.

1. **Snapshot the VM** at the hypervisor.
2. **Clean up leftovers:** `podman rm omp-1 omp-2 omp-3 happy_spence xenodochial_borg romantic_jones`; with no OMP running anywhere, `rm -rf ~/.omp/run`.
3. **Archive** the `--workspace--` and `-tmp*` session buckets.
4. **life-updates:**
   - set `name: life-updates` in its compose file (today it is `deploy`);
   - move ollama into this project and onto its network;
   - recreate that network with `isolate=strict`, replacing `shared`;
   - remove ollama's `ports:` line; bind `8000` to `127.0.0.1` unless used from the LAN.
5. **media\_server:** recreate `trashguides` with `isolate=strict`; keep the `10.0.0.103` LAN bindings; delete the orphan `media_server_trashguides`.
6. **js-render-server:** move into a compose file with a `name:`, publish on `127.0.0.1`.
7. **Host processes:** identify the python3 servers on `8004`/`8006`; decide whether glances on `8003` needs LAN exposure.
8. **Register projects with presets:** life-updates as `lockdown`, or `pods` with `egress = "packages"` when an agent should run its stack; then run `ab selftest` for each.
9. **Retire** `omp-workspace`, `claudeCode_start` and `podman-allow`.

Every compose network change needs a `down`/`up`, so each step briefly takes that stack offline.

## Build order

Build against a dummy compose project (two nginx containers) first; real stacks come last.

1. **Core launcher:** registry, presets, path validator, fixed hardening, Claude adapter. Gate: filesystem and posture selftest cases pass.
2. **Network:** per-session internal network, squid sidecar with the three egress modes (the model key gateway is deferred). Gate: proxied and direct cases pass in every mode; no keys in the sandbox except the session's own model token; the token appears in no log, session directory, backup or selftest output.
3. **OMP adapter:** rebuilt image, `~/.omp` split, `run` tmpfs, generated `AGENTS.md`. Gate: OMP boots, debugger and shared browser work through the proxy.
4. **Sockguard, observe tier:** audit mode first, then enforce. Gate: label scoping verified, including exec on another project's container.
5. **Host-side gate and dev tier:** network pinning, build network, port rejection, proxy injection. Gate: the gate and build cases pass on a dummy two-container compose project.
6. **`ab preview` and `ab deploy`.**
7. **Workspace UI:** tmux layout on top of the launcher.
8. **Pi adapter:** proves swapping harnesses works.
9. **Host migration**, one stack per snapshot.

## Usage

Day to day you register a project once, then start sessions by agent and project; everything else comes from the project's preset and overrides. Command names are proposed and may change during the build.

| Command | What it does |
| --- | --- |
| `ab project add <name> <root> --preset <p>` | Register a project with a preset (validated against overlap) |
| `ab project show <name>` | Every effective setting, and whether it came from the preset or an override |
| `ab allow <name> --egress open` / `--cap NET_ADMIN` / `--device /dev/net/tun` | Per-project overrides |
| `ab ws --agent <a> --project <name>` | Full tmux workspace: agent pane, two sandbox shells, btop, monitor |
| `ab run --agent <a> --project <name>` | Agent only, no workspace |
| `ab attach <name>` | Return to a running session |
| `ab preview <name> <service:port>` | View the dev stack from your desktop via `ssh -L` |
| `ab deploy <name> --lan 10.0.0.103:<port>` | Validated, diffed, confirmed deploy of the live stack |
| `ab outbox <session>` | Proposed changes as diffs, for projects whose compose files are read-only |
| `ab ls` | Running sessions, their project, preset and uptime |
| `ab kill <session>` | Tear down everything the session created |
| `ab selftest --agent <a> --project <name>` | Run the boundary checks |
| `ab keys set <provider>` / `ls` / `rm` | Set, list (names only) or remove a provider's API key; used from the next session |
| `ab keys rotate claude` | Replace the Claude subscription token |
| `ab project accept-history-secrets <name>` | Accept (after rotating) the masked file names found in a project's git history |
| `ab reload <session>` | Re-apply edited config files and regenerated rules to a running session |
| `ab resume <session>` | Recreate a session lost to a reboot and continue the agent's conversation |
| `ab gc` | Remove containers, networks and sockets left by dead sessions (runs automatically after boot) |

**A web helper visible on your LAN (`pods`)**

```bash
ab project add helper ~/repos/helper --preset pods
ab ws --agent claude --project helper
# agent: edits, podman-compose up -d --build, curl http://web:8080, reads logs, repeats
ab preview helper web:8080        # then: ssh -L 18080:localhost:18080 ubuntu-dev
ab deploy helper --lan 10.0.0.103:8090
```

**A data-processing script (`python`)**

```bash
ab project add dataproc ~/repos/dataproc --preset python \
   --data-ro ~/data/raw --data-rw ~/data/processed
ab ws --agent omp --project dataproc
# detach tmux, come back later with: ab attach dataproc
```

**A sensitive project (`lockdown`)**

```bash
ab project add life-updates ~/repos/Life_Updates --preset lockdown
ab ws --agent omp --project life-updates
```

## Worked example: a new pod for the home server

A web page that takes a file upload, transforms it and charts the results, built by an agent and deployed to the LAN. The one step you cannot delegate is reviewing the diff before `ab deploy`: that is when agent-written code starts running on your LAN with your real files.

**1. Set up the project (once, on the host)**

```bash
mkdir -p ~/repos/filelab && cd ~/repos/filelab && git init
mkdir -p ~/data/filelab-samples          # a few test files
ab project add filelab ~/repos/filelab --preset pods \
   --data-ro ~/data/filelab-samples
ab project show filelab                  # confirm: pods, open egress, dev tier
ab selftest --agent claude --project filelab
```

- Test with sample files; the agent sees everything its dev stack handles.
- Any secrets the app needs go in `.env.dev` as low-value dev values. The real `.env` comes at deploy and stays masked.

**2. Start the agent and brief it**

```bash
ab ws --agent claude --project filelab
```

Example brief, given all at once:

```markdown
Build a web app in this repo: upload a CSV or image, apply transforms, show results as charts.
Run it as a podman-compose stack (web, plus a worker if needed), uploads in a named volume, config from .env.dev.
Test it yourself: bring the stack up, upload files from /home/vmuser/data/filelab-samples with curl,
check responses and logs. Add automated tests. Commit working steps to git.
```

**3. The agent works unattended**

1. Writes the code, `Containerfile` and `compose.yml`.
2. Runs `podman-compose -p filelab-dev up -d --build`.
3. Tests with `curl -F file=@... http://web:8000/upload` and `podman logs`.
4. Fixes, rebuilds and repeats, installing packages as needed.
5. Commits.

Detach with `Ctrl-b d` and return with `ab attach filelab`.

**4. Preview from your desktop**

```bash
ab preview filelab web:8000                   # on the server
ssh -L 18080:localhost:18080 ubuntu-dev       # on your desktop
```

Open `http://localhost:18080`, try it, and type feedback into the agent pane. Repeat steps 3 and 4 until it's right. If the agent hits a wall, loosen only that setting with `ab allow filelab ...`.

**5. Close the dev session and review**

```bash
cd ~/repos/filelab && git log --oneline && git diff <first-commit>..HEAD
ab kill <session>
```

- Read closely: `compose.yml` (volumes, ports, capabilities), the `Containerfile` (what it downloads), and new dependencies.

**6. Deploy to the LAN**

```bash
nano ~/repos/filelab/.env                # real config, never visible to the agent
ab deploy filelab --lan 10.0.0.103:8095
```

- `ab deploy` validates the compose file, checks the real .env has every variable name in .env.dev, shows the diff since the last deploy, asks for confirmation, and starts the live `filelab` stack with a restart policy.
- Uploads land in the live stack's own volume, which no agent session is ever granted (the gate refuses any named volume the session did not create).
- Add a login, or basic auth through caddy, before deploying: an unauthenticated upload page is open to every device on the LAN.

**7. Later changes**

- `ab ws --agent claude --project filelab` starts a fresh `filelab-dev` stack.
- The agent can read the live stack's logs (observe) to chase a reported bug, but builds fixes only in `filelab-dev`.
- Ship each change with `ab deploy filelab --lan 10.0.0.103:8095`, reviewing the diff each time.

## Spike results

Five runs on ubuntu-dev (29 September 2026) confirmed the egress design, compose isolation, exec scoping in the gate, and builds that reach the project proxy but not the LAN. The key gateway is deferred: Claude Code uses a subscription token instead.

| Spike | Result | Consequence for the design |
| --- | --- | --- |
| 1 · Gate: containers | Pass: created containers pinned to the internal network, `--network host` rewritten, port publishing refused | The gate can be a thin rewrite layer for creates |
| 1 · Gate: builds | Pass: a build joined to a holder container's network namespace reached the stand-in proxy and not the LAN, run directly and when forced through the gate (both strategies). Forcing offline also works. Builds on a named network fail on this host (`did not get container start message from parent: EOF`) | Each session gets a holder container on its network, and the gate points every build at the holder's namespace. The gate must never pass through a namespace path the client sends: the observe run showed the client forwards one as-is |
| 1 · Baseline | Containers on podman's default network and on plain bridge networks both reach `10.0.0.103:8088` | Every agent-created container and build must go through the gate |
| 2 · Sockguard | List, inspect and logs are scoped by compose label. A client driving the API directly can create and start an exec session in another project's container | Sockguard alone does not scope exec. With the gate in front, the cross-project exec and a foreign session start were refused (run 3, 15 of 15). A second pass hit gate timeouts on a slow host; the timeout is now 60 s |
| 3 · Squid | 14 of 14: `locked` and `open` modes, `10.0.0.2:8080` allowed, other ports and the LAN refused, a public name resolving to a private address refused, no route when bypassed | Egress design confirmed as written |
| 4 · Key gateway | Plumbing works: the gateway forwarded the call with the real key and Anthropic refused it (organization disabled). Claude Code rejected a made-up model alias | Skipped for now: no paid API key. With a subscription login, Claude Code's credential has to live inside the sandbox (see Open items). The gateway stays useful only for remote models billed by API key |
| 5 · Compose isolation | `isolate: "strict"` reaches the network and blocks another network; two plain networks can reach each other | Compose files can carry isolation, and must: it is off by default |

## Open items and future options

**To verify during the build**

- [x] Sockguard's label visibility blocks `exec` on non-visible containers: it does not against a raw API client (spike 2), so the gate scopes exec; the gate refused it in run 3.
- [ ] Sockguard maturity: a small project at a security boundary. Pinned by digest to 2.2.1 (step 4); its release signature is not verified yet (no cosign on the host).
- [x] Host-side gate: a thin rewrite layer in front of sockguard works for container creates (spike 1).
- [x] Builds reach the project proxy and not the LAN by joining a holder container's network namespace (spike 1 v4).
- [x] Gate: pass the proxy to builds as build arguments (step 5; a build's `RUN` sees `http_proxy`). pip, npm and apt inside a build are not yet checked one by one.
- [x] Gate: on every build, the holder's PID is checked against its own container ID (cgroup path), twice, and every client namespace path except `user` is dropped (step 5; a forged `nsoptions` path is ignored in the itest).
- [ ] Claude Code with a setup-token token works when its only route out is the squid proxy (replaces spike 4): `make claude-live`, run by Tim.
- [x] DNS on the internal session network does not resolve public names (step 2; kept as a selftest case).
- [ ] Model key gateway: deferred. LiteLLM forwarding works (spike 4); needed only if a remote model is billed by API key.
- [x] OMP's browser and web tools honour the proxy variables: OMP runs on bun, whose fetch honours `HTTPS_PROXY`/`NO_PROXY`; its browser gets the proxy through `PUPPETEER_PROXY` (step 3, `make itest`). OMP's own `PI_PROXY` is not set: it bypasses private ranges, so the local model would go direct and fail.
- [x] podman-compose passes `driver_opts: isolate: "strict"` through to the network, and it isolates (spike 5).
- [x] Squid IP-literal rules for `10.0.0.2:8080`, and private-address refusal after DNS resolution, behave as written (spike 3, 14 of 14).
- [x] OMP's shared browser works with `run` as a real tmpfs (step 3: `~/.omp` is a tmpfs of its own; OMP opened example.com through the proxy in its shared headless browser).
- [x] OMP's session directory naming follows the real project path once mounted there (step 3).
- [x] Claude Code's config splits cleanly into a read-only token plus per-project state (step 1: token mounted read-only and exported at start; whole `CLAUDE_CONFIG_DIR` per project, seeded by copy).
- [ ] Identify the python3 servers on `8004`/`8006`.
- [x] OMP's flag for turning its approval prompts off: `--approval-mode=yolo|write|always-ask` (step 3). The third-party `pi-permission-system` extension in `~/.omp/agent/extensions/` is a separate policy layer; not mounted, Tim to decide.
- [ ] OMP with the real `~/.omp`, keys and local model: `make omp-live`, run by Tim.
- [ ] Claude Code credentials: reopened. Subscription login only, so no gateway. Proposed: a dedicated long-lived token from `claude setup-token`, passed as `CLAUDE_CODE_OAUTH_TOKEN` from a 0600 file, never the host's `~/.claude` credentials. To verify: it works in the sandbox, and revoking it doesn't log out the host.
- [ ] Live stacks restart on boot: check lingering and `podman-restart.service` for vmuser; enabling lingering may need an admin.
- [x] Gate strips restart policies from agent-created containers (step 5).
- [x] Gate: joining another container's namespaces is refused, and a pod only if it is the session's own. Pods with an infra container are refused in the dev tier, because sockguard cannot resolve pods (step 5; real podman: `test_step5_fields.test_pod_infra`, `test_pod_member`, 1 Oct).
- [x] End-of-session scan (step 5, `agentbox/scan.py`): secrets in the session's commits by the host hook's own regex (its `scanner:allow` marker ignored), plus changed files that run later on the host (`Makefile`, `package.json`, `pyproject.toml`/`setup.py`/`setup.cfg`, `.pre-commit-config.yaml`, `.vscode/tasks.json`, `.envrc`, git config) and files that became executable. It informs, never blocks.
- [ ] Build memory cap: confirm with a build that allocates past a small per-build `memory` limit that the gate-set limit and the slice cap both hold (spike, 29 Sep: build steps land in `agentbox.slice/<child>` as intended; the cap itself was not load-tested, the host had 2 GB free).

**Future options**

| Option | What changes | When |
| --- | --- | --- |
| Dedicated `agent` Linux user | An escape lands as `agent`, not `vmuser`; sockguard admits by connecting user | If you can get root once |
| Separate agent VM | An escape reaches only that VM; sockguard serves over mutual-TLS TCP, compose dirs synced read-only | Agents routinely handle untrusted repos or sites, or this VM holds data you cannot lose |
| Landlock inner layer | Path-based socket blocking inside the container | Kernel 7.1+ (ABI 9) |

The launcher keeps the socket endpoint and directory sources as settings, so moving to the separate VM is a config change.

NVIDIA OpenShell was considered and set aside: it is alpha, it offers no scoped podman access, and it is unconfirmed whether it supports mounting host project directories per project.
