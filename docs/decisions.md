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
- **Agent container:** `--memory=4g --memory-swap=4g --cpus=3 --pids-limit=4096 --oom-score-adj=500` (900 until 30 Sep; see "The OOM score margin" below). Rootless podman can raise the OOM score, so under host memory pressure the kernel kills agent containers before live services.
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

- **What:** the agent container (and its proxy) run with `--oom-score-adj=500` (was 900, changed 30 Sep; see "The OOM score margin" below); containers the gate creates get 1000. The slice stays at `MemoryMax=4400M` and the agent's own cap at 4g. Rootless podman accepts these here: the user's default is 0 and raising it is allowed.
- **Worst case, accepted:** the nightly model run plus a full slice reaches all of RAM. The cap was kept deliberately, relying on the OOM scores; the slice file's comment says so.

## 2026-09-29 · Builds get a cgroup parent under the slice (gate, step 5)

- **What:** the build API takes `cgroupparent`, but the podman service treats it as a cgroupfs path, not a systemd unit. Tested on this host with a remote build whose `RUN` step read its own cgroup:
  - `agentbox.slice`: fails, `Permission denied` creating `/sys/fs/cgroup/agentbox.slice`.
  - The slice's full path while the slice is idle: the runtime creates the directory itself, and the step runs with `memory.max = max`, uncapped.
  - The slice's full path while the slice is active: fails (cgroup v2 allows no processes in an inner node).
  - A child of the active slice (`/user.slice/user-<uid>.slice/user@<uid>.service/agentbox.slice/<session>-build`): works. The step lands there, and the slice's cap applies to it as an ancestor.
- **So:** the gate sets that child path only while the session's holder container keeps the slice active (checked before each build), also sets the build's own `memory` limit, and refuses a client-supplied `cgroupparent`. The slice cap itself was load-tested by `make itest-oom` (30 Sep, containers; builds use the same kernel mechanism).

## 2026-09-29 · The OOM score margin (agent 900 → 500 on 30 Sep)

The kernel picks the process with the highest `usage + oom_score_adj × total / 1000`, where `usage` is RAM plus swap and `total` is the memory the kill is about: the cgroup's limit for a kill inside a cgroup, RAM plus swap for a host-wide kill.

- **29 Sep, agent 900:** inside the slice, 900 against a dev container's 1000 was worth 0.1 × 4400 MB = 440 MB. `make itest-oom` (Tim, 29 Sep) confirmed both sides: agent 1.2 GB with dev 3.9 GB, the dev container died; agent 3.0 GB with dev 1.6 GB, the agent died.
- **30 Sep, agent 500, created containers 1000.** With this host's numbers (RAM 9,360 MB, swap 24,576 MB, slice 4,400 MB):
  - **Inside the slice:** the gap is 0.5 × 4,400 = 2,200 MB. The dev container dies first unless the agent uses more than 2,200 MB beyond it. For agent 3.0 GB and dev 1.6 GB, the slice fills when the dev container is at about 1.3 GB: agent 3,072 + 2,200 = 5,272 against dev 1,330 + 4,400 = 5,730, so the dev container dies (margin about 450 MB). An agent near its 4 GB cap with a small dev container is still killed first (for example agent 3.6 GB, dev 0.7 GB: 5,886 against 5,100); that is the remaining case, reported by `make itest-oom` as information.
  - **Host-wide, against live services at 0:** `total` is 33,936 MB, so 500 is worth 16,968 MB. A live process is picked before even an idle agent only if its own RAM plus swap exceeds about 17 GB; RAM is 9.4 GB, so that needs more than 7.6 GB of it in swap. The model server holds 3.9 GB. agentbox containers never swap (`MemorySwapMax=0` on the slice, `--memory-swap` equal to `--memory` on the agent), so their usage is RAM only. Gate-created containers at 1000 are always ahead of live services (they would need a live process over 33.9 GB).
  - **Lower than 500 was not chosen:** a full guarantee inside the slice (agent at its 4 GB cap against any dev container) needs a gap of about 930 points, which would leave the agent at ~70 and worth only ~2.4 GB host-wide, less than the model server holds.
- `make itest-oom` now checks agent 3.0 GB with dev 1.6 GB as pass/fail (dev dies, agent survives) and checks the agent's `oom_score_adj` is 500.

## 2026-09-29 · The build cap check, and when a slice is idle

- **What:** `agentbox/buildcap.py` is the part of the step 5 gate that caps builds: before every build it requires the slice to be active (`systemctl --user` reports it active and its cgroup has a finite `memory.max`), refuses a client-supplied `cgroupparent`, drops the client's memory and CPU settings, and sets `cgroupparent` to `<slice path>/<session>-build` plus its own `memory`/`memswap` (so a build is capped twice: its own limit and the slice). `make itest` proves it against a real podman service: an inactive slice refuses the build, and an active one gives the `RUN` step the gate's limit in a cgroup under the slice.
- **Found:** systemd keeps a slice active once it has been used, even when empty. `agentbox.slice` is idle only after a boot (or a manager restart) until the first session, which is exactly when an unchecked build would run uncapped. The integration test uses a never-started slice name for the idle case, since stopping `agentbox.slice` would be a systemd change.
- **The window, stated plainly (30 Sep):** the only time a build could run uncapped is before any session exists. The gate only runs inside a session, and every session's containers sit in the slice, so whenever the gate is there to forward a build, the slice is active. The check before each build is the second line: it turns any case we have not thought of into a refused build instead of a silent uncapped one.
- **Build memory (30 Sep):** each build's own limit is the project's `build_memory`, default `2g` (compiled scientific Python and conda builds need more than 1g). It is refused at `ab project add` and at session start if it is not positive or is above the slice's `MemoryMax`.

## 2026-09-29 · Commit hooks are never skipped

- **What:** no commit uses `--no-verify`/`-n`, a `core.hooksPath` override, or `GIT_PRECOMMIT_CHECK_*=0`. The guard hook blocks all four, and the integration fixtures commit through the host's hook too.
- **Open:** the host's secret scanner (a global pre-commit hook) has no allowlist mechanism, so a false positive blocks a commit outright.

## 2026-09-29 · Step 2: session network and squid, as built

- **DNS:** podman's DNS on an `--internal` network answers `NXDOMAIN` for public names (checked with `example.com`, `pypi.org`), so it does not forward them and is not a way out. DNS stays on so the agent finds its proxy by name; the selftest keeps "direct DNS lookup of a public name fails" to catch a regression.
- **Networks:** `<session>-int` (`--internal`, `isolate=strict`: agent and proxy) and `<session>-egr` (`isolate=strict` bridge: proxy only). The proxy resolves names through the egress network's DNS.
- **Squid:** `docker.io/ubuntu/squid` pinned by digest (6.13), run with `--cap-drop=ALL --cap-add=SETUID,SETGID` (it drops to the proxy user), no-new-privileges, read-only root, tmpfs `/var/spool/squid`, `/var/log/squid`, `/run`, `/tmp` (all `notmpcopyup`), 256 MB, 256 pids, the agent's OOM score, in the slice. The pinger is off (it needs raw sockets). `via off` and `forwarded_for delete` keep the session's addresses out of outgoing requests.
- **Access log:** one line per request (time, client, method, destination `host:port`, status, server address, result), kept in `~/.local/share/agentbox/logs/<session>-proxy.log` when the session ends. Since 30 Sep (review #9) it holds destinations only: no URL paths or queries, which can carry data, and never headers.
- **IPv4-mapped IPv6:** squid normalises `[::ffff:10.0.0.103]` to `10.0.0.103` before the ACLs, so the private list needs no `::ffff:0:0/96` entry (which would have matched every IPv4 address). Tested: refused.
- **Result (make itest, 29 Sep):** in `locked`, `packages` and `open`, every proxied negative is `403`, the model endpoint and `api.anthropic.com` are allowed, `example.com` and `pypi.org` follow the mode, a project's `extra_domains` are allowed from `packages` up, CONNECT to a non-443 port and HTTP to a non-web port are refused, and direct attempts (LAN, model host, `example.com`, `1.1.1.1`) never connect. Two concurrent sessions cannot reach each other's agent or proxy.

## 2026-09-30 · Review of steps 1-2: what changed

The independent review (`docs/reviews/2026-09-30-steps-1-2.md`, annotated per finding) led to these changes; each has a test that failed before the fix.

- **Token at rest (#1):** `make itest` now runs Claude against a stub Messages API on `127.0.0.1` inside the sandbox that accepts the fake token, so Claude completes a turn and writes its post-authentication state; the state dir, backups, logs and run dir are then grepped for the token (none found: transcript, `.claude.json`, and Claude's own `.claude.json` backup are clean). The selftest itself now creates a container from the session's exact argv and checks podman's stored config for the token.
- **Committed secrets (#2):** git history cannot be masked without breaking git. The launcher now warns at session start with the masked file names found in the repo's history.
- **Host addresses (#3):** `host_addrs` (default `10.0.0.103`) is always refused, plus whatever is found at session start; with neither, the session refuses to start.
- **IPv6 (#4):** the networks are created with `--ipv6=false` explicitly; the selftest checks a proxied IPv6 literal fails. A `2000::/3` deny acl was rejected: squid's `dst` acl matches if any address of a name matches, so it would refuse dual-stack hosts.
- **Git dirs (#6, and a wider gap found while testing it):** every git directory inside a writable grant is protected, not only the root's: nested repos, `.git`-file targets, bare repos, submodules.
- **gc and teardown (#7, and a bug found while testing it):** `session.json` is written atomically; gc leaves a session alone while its agent runs, and removes orphaned proxies. Non-interactive sessions now tear down on SIGTERM/SIGHUP too (an interrupted `itest-oom` run had left a proxy running).
- **Mount destinations (#8):** `swap` entries must be plain file names; the argv audit now checks every mount destination is a clean path inside a grant or `/run/agentbox`.
- **Proxy log (#9):** destinations only (`host:port`); `strip_query_terms on` is explicit.
- **Not changed (#5):** `.envrc` and other files that run later are documented, not overlaid (reason in the review file and the design's filesystem policy).

## 2026-09-30 · A stale `claude-live` pass is flagged (decision #1)

- **What:** `make claude-live` (Tim, real token) records the commit it passed on in `tests/live/claude-live-pass.txt`, and only when no token-handling file has uncommitted changes. `make itest` ends with a warning when any token-handling file changed since that commit (committed, uncommitted or new): `agentbox/tokens.py`, `keys.py`, `session.py`, `hardening.py`, `backup.py`, `podman.py`, `adapters/`, `selftest/`, `tests/live/claude_live.py` (list in `tests/live/freshness.py`). No record also warns.
- **Why:** `claude-live` is the only end-to-end check with the real token; without a record nobody can tell whether it still describes the code.

## 2026-09-30 · Masked names in git history refuse the project until accepted (decision #2)

- **What:** `ab project add` refuses a project when the history of any git repo inside its grants (root, nested repos, `data_rw`, `data_ro`) holds a masked file name (`.env`, `.env.*`, `*.pem`, `*.key`, `secrets/...`, swap targets). The message names the files and says to rotate them. `--accept-history-secrets` registers it anyway and stores the names as `history_secrets_accepted`; `ab project accept-history-secrets <project>` does the same for a registered project. A session refuses a name that is in history but not accepted, before it creates anything. If git cannot read a history, the launcher refuses instead of guessing. `--with-secrets` sessions skip the check (they see the files anyway).
- **Why:** the warning alone (review #2) was easy to scroll past; the files are exposed to the agent in every session, so the choice belongs at registration, once, with the advice to rotate.

## 2026-09-30 · End-of-session scan also lists files that run later (step 5)

- **What:** besides the secret scan over the session's commits, the report lists changed files that run later on the host: `Makefile`, `package.json` scripts, `pyproject.toml`/`setup.py`/`setup.cfg` scripts and build hooks, `.pre-commit-config.yaml`, `.vscode/tasks.json`, `.envrc`, git config, and files that became executable. It informs; it does not block.
- **Why:** these are documented, not overlaid (review #5), so the report is where the reviewer finds them.

## 2026-09-30 · Provider API keys: one file each, exported like the Claude token (step 3)

- **What:** each pay-per-use provider key lives in `~/.config/agentbox/keys/<provider>` (0600, owned by the user, not a symlink; the directory is 0700), set with `ab keys set <provider>`, listed (names only) with `ab keys ls`, removed with `ab keys rm`. A key enters a session exactly like the Claude token: its file is bind-mounted read-only at `/run/agentbox/keys/<provider>`, and the session wrapper exports it as `AB_KEY_<PROVIDER>` just before exec. It is never on a command line, in podman's container config, in the generated model config (which names the variable), or in logs, state or backups. The selftest and `make itest` check all of these with fake keys, as for the token. A key file that fails the checks refuses the session rather than being skipped.
- **Only providers with a key are offered.** The OMP model config is generated from a keyless seed, `~/.config/agentbox/omp/models.yml` (same format as OMP's; the launcher refuses a seed holding `apiKey`, a credential-like header, or a `!command` value). A provider without a key file is left out. The local model server (`10.0.0.2:8080`) is always included, with its key if `keys/llama-cpp` exists; if the seed has no provider for it, a default `llama-cpp` entry is added. A key whose provider is not in the seed is not mounted.
- **Domains:** a keyed provider's API domains join the proxy's model allowlist, allowed in every egress mode (including `locked`):

  | Provider | Domains |
  | --- | --- |
  | `anthropic` | `api.anthropic.com` |
  | `openai` | `api.openai.com` |
  | `gemini`, `google` | `generativelanguage.googleapis.com` |
  | `openrouter` | `openrouter.ai` |
  | `mistral` | `api.mistral.ai` |
  | `groq` | `api.groq.com` |
  | `deepseek` | `api.deepseek.com` |
  | `xai` | `api.x.ai` |
  | any other | the host of its `baseUrl` (a name, not an IP); without one the provider is left out |

  A `baseUrl` host is added for a known provider too. A provider whose `baseUrl` is a private or LAN address other than the local model server is left out (squid would refuse it anyway).
- **Accepted risk, and why the key gateway stays deferred:** the agent can read the keys its session holds. `ab keys set` says so and asks for a key made only for agentbox, with a spending cap at the provider. With dedicated, spend-capped, revocable keys, the worst case is bounded by the cap, which is what the LiteLLM gateway would otherwise buy; it stays deferred until a key cannot be capped or scoped.

## 2026-09-30 · What the OMP session mounts from ~/.omp (step 3)

A whitelist, not "everything minus exclusions". Read-only, from the host's `~/.omp` (setting `omp_home`):

| Mounted | Why |
| --- | --- |
| `versions/<active>/` | The provisioned plugins, repos and pylibs. Plugin links written by `omp-env install` are absolute `/root/.omp/versions/<sha>/...` paths |
| `plugins`, `repos`, `pylibs` | Their symlinks resolved; each must point into the same `versions/<active>/` (otherwise refused, e.g. during an install) |
| `agent/config.yml` | OMP settings (design: read-only) |
| `bin/mags-codedev` | The MAGS-CodeDev CLI wrapper |
| `.cache/ms-playwright` | Playwright's Chromium, for Python tools in `pylibs` |

Each is refused if it is a symlink or resolves outside `~/.omp`. Never mounted: `agent/models.yml` and `models.yml.backup` (keys), `agent/.mcp.json` and `agent/mags-codedev.yaml` (may hold credentials; waiting on Tim), `agent/extensions/` (see the approvals entry), `agent/sessions` and every database, blob and terminal file (other projects' history), other `versions/`, `logs`, `run`, `cache`, `.cache/pip`, `puppeteer` (the image's Chromium is used instead), `stats.db`, `.omp-sandbox.json`, `.omp-env.json`, `gpu_cache.json`.

Per project, read-write and persistent: `~/.local/share/agentbox/<project>/omp/agent` at `~/.omp/agent` and `.../omp/logs` at `~/.omp/logs`. Generated per session, read-only on top: `agent/models.yml`, `agent/AGENTS.md`. `~/.omp` itself is a tmpfs owned by the agent (`notmpcopyup`), so `run/` and caches are real tmpfs, per session. Found by the step 3 itest: without it the runtime creates `~/.omp` root-owned as a parent for the mounts, and OMP fails with `EACCES: mkdir ~/.omp/run`.

**Proxy:** OMP runs on bun, whose fetch honours `HTTPS_PROXY`/`NO_PROXY`, so model calls, including those to `10.0.0.2:8080`, go through squid (itest: bun's fetch to the local model shows in the proxy log). OMP's own `PI_PROXY` is deliberately not set: it bypasses private ranges, so the local model would go direct and find no route.

## 2026-09-30 · The OMP image is derived, not rebuilt (step 3)

- **What:** `images/omp/Containerfile` builds `localhost/agentbox-omp` from Tim's `localhost/omp-env` (`make omp-image`). It opens `/root` to 0755 (bun lives in `/root/.bun`, and the session user is not root), links `/root/.omp` to `$HOME/.omp`, and adds `python-is-python3`, `debugpy` 1.8.22, Debian's `chromium` and the compose tool at 1.5.0. `omp-env` itself is unchanged. The podman client in the image is 4.3.1 (Debian bookworm), talking to podman 5.7 on the host; to be checked in step 5.
- **Browser:** OMP's browser tool uses `PUPPETEER_EXECUTABLE_PATH` (the image's Chromium) and passes a proxy to Chromium only through `PUPPETEER_PROXY`, so the session sets that to the squid URL too (an adapter's `proxy_url_env`).
- **Why:** OMP's own install stays the source of truth for extensions; the derived image only fixes what a non-root, read-only, proxied session needs. The apt lists are left in the image (about 20 MB): the guard hook matched the usual apt-lists cleanup line in the Containerfile text.

## 2026-09-30 · OMP approvals map to OMP's own flag, not pi-permission-system (step 3)

- **What:** approvals off → `omp --approval-mode=yolo`; on (lockdown) → `--approval-mode=always-ask`. OMP's `tools.approvalMode` defaults to `yolo`, so without the flag a lockdown session would never ask.
- **pi-permission-system** (`~/.omp/agent/extensions/`) is a third-party extension (npm `pi-permission-system`, MIT) that enforces allow/ask/deny policies per tool, bash command pattern, MCP server and skill from a `pi-permissions.jsonc` file, forwards subagent `ask` prompts to the main session, and has its own yolo toggle. It is a policy layer on top of OMP, not its approval switch. It is not mounted; whether to bring it in (read-only, with a policy per preset) is Tim's call.
