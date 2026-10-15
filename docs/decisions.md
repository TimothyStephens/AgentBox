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

## 2026-09-30 · Step 4: sockguard observe tier, as built

- **A podman service per session.** No podman API socket runs for vmuser (`podman.socket` is disabled, and enabling it is a systemd change). A session with podman access starts its own `podman system service --time=0` on `<run dir>/podman/podman.sock` (the run dir is 0700), records its PID, and stops it at teardown; `ab gc` stops it for dead sessions. A stop checks the PID's command line first (PIDs are reused). Only the sockguard container mounts that socket.
- **Sockguard 2.2.1, pinned by digest** (`docker.io/codeswhat/sockguard@sha256:3ae41b2f…`), per session: `--network=none`, keep-id as the user, `--cap-drop=ALL`, no-new-privileges, read-only root, 64 MB, 128 pids, the agent's OOM score, in the slice, labelled `agentbox.role=sockguard`. It listens on a 0600 socket in the run dir, mounted into the agent at `/run/agentbox/podman.sock`; `CONTAINER_HOST` and `DOCKER_HOST` point at it. Its JSON audit log is kept in `~/.local/share/agentbox/logs/<session>-sockguard.log`. The release signature is not verified yet (no cosign on the host); still an open item.
- **Observe = reads of the live stack only.** Visibility: `io.podman.compose.project=<compose_project>` (a new project setting, default the project name; `ab project add --compose-project`). Allowed, on both API surfaces: `_ping`, `version`, container list, inspect, logs, top and stats, and pod list/inspect. Everything else is denied, including exec on the live stack. Environments, mount paths, network and host topology are redacted. `insecure_allow_read_exfiltration` is on because logs and top need it; the visibility label bounds them.
- **Audit mode never runs in a session.** Sockguard's `mode: audit` forwards everything its rules would deny (visibility included), which would hand the agent the raw socket. "Audit first" is therefore a discovery test: a deny-all profile in audit mode, the test's own two compose stacks, and the observe commands. It found `podman --remote` needs exactly: `_ping` (both surfaces), `libpod/version`, `libpod/containers/json`, a container's `json`, `logs` and `top`, and `libpod/pods/json`. Those rules then run in enforce mode.
- **`podman stats` does not work under observe.** Its endpoint (`GET /libpod/containers/stats`) reports every container on the host with no label filter, and sockguard refuses it outright. Per-container stats via the Docker API (`/containers/<name>/stats`) are scoped and allowed; the session's notes tell the agent.
- **Tiers until the gate exists (step 5):** `observe` works. `exec` (tier or live) is refused at session start, because sockguard does not scope exec. `dev` (the `pods` preset) gives only its `live: observe` part, with a warning.
- **Selftest:** with podman access, it creates a stopped canary container in a compose project of its own (busybox, no network, the session's labels) and checks from inside the sandbox:
  - the list shows exactly the live stack;
  - inspect, logs, exec, stop and remove on the canary are refused;
  - create and pull are refused;
  - on the live stack, inspect is allowed with an empty environment and logs are allowed, while exec and stop are refused.

  The canary stands in for other projects, so the selftest never probes a real service. Without podman access, it checks that no socket is present.
- **Gate (`make itest`, step 4 module):**
  - The observe commands work under enforce and list only the live stack. `podman --remote exec/logs/inspect` on another project's container and `exec`/`stop` on the live stack are refused, as are `rm`, `run -v $HOME:/h` and `pull`. A raw API exec create on another project's container gets `403`.
  - The selftest passes for Claude (node client) and OMP (podman client).
  - No podman service or sockguard is left after teardown.

## 2026-09-30 · The credential rule (Tim)

This rule settles what may enter a sandbox. No protections are added beyond it.

- **LLM provider keys and the Claude token: accepted risk.** They enter as environment variables, as built (file → variable just before exec). If one leaks, Tim rotates it. Paid keys should be dedicated and spend-capped; `ab keys set` warns.
- **Every other credential never enters a sandbox through agentbox.** That covers GitHub tokens, home-server or service passwords, SSH keys and anything in MCP configs. The only way one gets in is a file Tim creates himself under `~/.config/agentbox/`.
- **The existing checks stay:** keys and the token are kept out of logs, state, backups and `podman inspect`, and the selftest and `make itest` check that.

## 2026-09-30 · OMP: user config files, pi-permission-system, the real ~/.omp layout

- **`.mcp.json` and `mags-codedev.yaml`** (the latter holds API keys on the host): never copied from `~/.omp`, never parsed or cleaned. If Tim creates `~/.config/agentbox/omp/mcp.json` or `~/.config/agentbox/omp/mags-codedev.yaml`, it is mounted read-only at `~/.omp/agent/.mcp.json` or `~/.omp/agent/mags-codedev.yaml`; he is responsible for what is in them (the credential rule). If neither exists, the session prints one line saying so and mounts nothing.
- **pi-permission-system:** not used. OMP's `--approval-mode` is the preset mapping; the extension is not mounted.
- **The real layout** (found by `make omp-live`, 30 Sep): `plugins -> versions/19760485e9c2/plugins`, while `repos` and `pylibs -> versions/c1b51fef8911/`. The earlier check that all three share one version refused it. Now:
  - each link's actual target is mounted read-only at `~/.omp/<name>`;
  - every version directory those targets live in is mounted whole at `~/.omp/versions/<v>`;
  - so is any version the plugins' own links or `package.json`/`omp-plugins.lock.json` point into, since `omp-env install` writes absolute `/root/.omp/versions/<v>/...` paths;
  - an unreferenced version (`legacy-...`) is not mounted.

  The session is refused only if a target is missing, or if the links change between session start and container start (an install or rollback running). `tests/ompfake.py` mirrors this layout, including a plugin linked by an absolute `/root/.omp` path.

## 2026-09-30 · Observe tier: logs off by default; the podman service in the slice

- **`observe_logs`** (project setting, `ab project add --observe-logs`), default false. Live-stack logs can hold secrets (an `apikey=` in a logged URL), and whatever the agent reads also goes to its model provider. With it off, sockguard's rules have no log endpoint at all. Inspect with the environment redacted stays on regardless. The `pods` preset's observe access follows this default.
- **Where the podman service runs:** until now as a child of `ab`, so in the launcher's own cgroup (the terminal's session scope), outside `agentbox.slice`. It now starts with `systemd-run --user --scope --slice=agentbox.slice --unit=<session>-podman-<start>`. That is a transient scope, not a configuration change: systemd-run registers the scope and execs podman in place, so the PID `ab` holds is the service's. Its memory and tasks count against the slice. When `cgroup_parent` is empty (tests without the slice), it is a plain child.
- **gc of orphaned services:** `ab gc` scans `/proc` for `podman system service` processes whose socket is in a session run dir under this `AB_RUN_DIR`. It stops each whose session is not alive and was not left alone because its agent still runs, even when the run dir and containers are already gone. Tested by killing a launcher with SIGKILL, removing its agent and run dir, then running `ab gc`.

## 2026-09-30 · Each session's kept logs have their own file

- **What:** proxy and sockguard logs are kept as `<session>-<start time>-proxy.log` / `-sockguard.log`, created exclusively (a second teardown of the same session leaves the file alone).
- **Why (bug found by `make claude-live`, 30 Sep):** session names are reused once a session ends. The second session's proxy log was appended to the first one's file, so claude-live showed "(none)" for session 2, and its denied list proved nothing. `claude-live` and `omp-live` now FAIL when a session that answered has an empty proxy log. The step 2 itest runs two sessions with the same name and checks each has its own non-empty log.

## 2026-09-30 · Sockguard's signature: `make verify-sockguard`

- **What:** Tim installs cosign v3; `make verify-sockguard` runs `cosign verify` with the identity sockguard's docs give (`release-from-tag.yml@refs/tags/*`, issuer `token.actions.githubusercontent.com`). It tries the pinned digest first, then the image's other repo digests, because the signature may sit on the multi-arch index rather than the platform manifest pinned here. It says which one verified; if that is not the pinned one, the pin moves to it. Without cosign on PATH, it says so and fails.
- **Result (Tim, 30 Sep):** the pinned digest `sha256:3ae41b2f…` verified against the release workflow identity. The pin stays.

## 2026-09-30 · Agent images carry podman-remote 5.7.0 and the compose tool 1.5.0

- **What:** the OMP image's Debian podman (4.3.1) is replaced as the client, not just checked. Both agent images get podman-remote 5.7.0, static, at `/usr/local/bin/podman`, first on PATH. The server on the host is 5.7.0. The download is checked by the build's `ADD --checksum=sha256:0d1759e3…`, the value from the release's own `shasums` file, which I verified against the download on 30 Sep. The version and checksum are set once in the Makefile (`PODMAN_REMOTE_VERSION`, `PODMAN_REMOTE_SHA256`) and passed to both builds.
- **Claude image:** `localhost/claudecode` had no podman, python or curl, so it now has a derived image too, `localhost/agentbox-claude` (`make claude-image`). It adds podman-remote, python3 and the compose tool 1.5.0 (pip, the host's version). The Claude adapter uses it; `claudecode` itself is unchanged.
- **Checked:** `make itest` (step 4) runs `podman --remote version` and `podman-compose version` through sockguard in both images, and fails if the client's major version differs from the server's, if `podman` is not `/usr/local/bin/podman`, or if the compose version differs from the host's. Both adapters list `podman` and `podman-compose` as required tools, so every selftest checks they are present.
- **Why:** step 4 saw the 4.3 client lose sync on a logs stream from the 5.7 server; step 5 (creates, builds, compose) should not run on a mismatched client.

## 2026-10-01 · Review of steps 3-4: what changed

The review (`docs/reviews/2026-10-01-steps-3-4.md`, annotated per finding) led to these changes. Each one has a test that failed before the fix, except where noted.

- **Observe narrowed (#1 HIGH, #4):**
  - libpod inspect leaked the live stack's `env_file` and `environment:` values through `CreateCommand`, and another project's pod was readable.
  - Observe now allows container lists, Docker-API inspect, per-container stats and (with `observe_logs`) logs. No libpod inspect, `top` or pods.
  - `podman inspect`, `top` and `pod ps` no longer work under observe; the session notes give the curl alternative.
- **Approvals in lockdown (#2):**
  - Claude: no pre-trust or bypass acceptance when approvals are on, and a read-only managed policy (bypass mode disabled, only managed hooks and permission rules).
  - OMP: the persistent agent dir's code-loading places are covered read-only and empty.
  - Open for Tim: OMP's repo-provided extensions, hooks, tools and MCP in lockdown (accept, or `--no-extensions` and lose the plugins).
- **`~/.omp` targets (#3):**
  - Each must be exactly `versions/<v>/<name>`, with `<v>` a real directory; version directories are re-checked at container start.
  - Refused if a credential file in `~/.omp` is hard-linked, or a mounted repo's git config has credentials in a URL.
- **Raw podman socket (#6):** the service moves to `<run>/services/<session>/`, outside the mountable session run dir; the argv audit refuses any socket as a bind source except sockguard's.
- **Defaulted compose project (#7):** the stack's containers must come from inside the project root; an explicit `compose_project` is trusted.
- **Images (#8):** pip installs pinned with hashes (`--require-hashes`); the OMP build fails on credential-like files in `/root` before opening it.
- **gc on first use after boot:** implemented (the design claimed it). The first `ab run`, `ab selftest` or `ab ls` after a boot runs it, keyed to the kernel's boot id.
- **Not changed:**
  - #5: not reproduced. OMP's 400 dumps hold no key; the test stays.
  - #9: rejected under the credential rule. The "never in logs" checks cover accidental leaks; an agent can always send its own key somewhere.

## 2026-10-01 · `make install`

- **What:** `make install` (run by Tim) writes `~/.local/bin/agentbox`, a two-line launcher for this checkout marked as ours, and `~/.local/bin/ab`, a symlink to it (`BINDIR=` to change).
  - It refuses to overwrite an `ab` or `agentbox` that is not ours; ApacheBench installs `ab`, and so would a hand-made wrapper.
  - It notes when another `ab` on PATH shadows its own, or is shadowed by it.
  - `README.md` has the setup steps.

## 2026-10-01 · Step 5: the host-side gate and the dev tier, as built

- **Layout.** Dev tier (`podman = {tier = "dev"}`; the `pods` preset):
  - The agent's `/run/agentbox/podman.sock` is the **gate** (`agentbox/gate.py`, a Python process per session, run like the podman service in a scope under `agentbox.slice`, stopped at teardown, found by `ab gc` if orphaned).
  - The gate forwards to a second, **dev sockguard** with owner isolation (`ownership.owner=<session>`, label `agentbox.dev-owner`). That sockguard forwards to the session's podman service.
  - The gate reads labels from the podman service directly, for its own checks only.
  - With `live = "observe"` too, the live stack's read-only sockguard is a second socket, `/run/agentbox/podman-live.sock` (`AB_PODMAN_LIVE`). That replaces the design's single socket: one sockguard cannot show the live stack and the dev stack at once (visibility labels are ANDed).
- **The gate's rules** (unit tests in `tests/unit/test_gate.py`, real podman in `tests/integration/test_step5_gate.py`):
  - **Creates** (libpod and Docker API):
    - pinned to `<session>-int`, network aliases kept so compose service names resolve;
    - refused: published ports, privileged, added capabilities and devices, host or container namespaces, binds outside the project's grants, a client cgroup parent;
    - restart policy dropped, `systemd` mode off;
    - labels stamped (`agentbox.dev-owner`, `agentbox.role=dev` and the session labels);
    - cgroup parent `agentbox.slice`, 1 GiB and 1024 pids unless set, OOM score 1000;
    - lower-case proxy variables added.
  - **Pods:** a pod with an infra container is pinned the same way. One without (podman-compose's default) has no network of its own: its containers are pinned individually, and their membership of it is dropped, since it shares nothing.
  - **Networks** are created internal and labelled. **Volumes** are labelled; driver options (bind-backed volumes) are refused.
  - **Builds:**
    - `RUN` steps join the holder container's network namespace. The holder's PID is checked against its own container ID in its cgroup path, read twice per build.
    - The client's namespace options are dropped except `user`, and so are its security knobs: seccomp, AppArmor, isolation, id mappings and the rest.
    - The build never pulls, is capped by `buildcap`, and gets the proxy as build args.
  - **Pulls:** allowed only from docker.io, ghcr.io, lscr.io and quay.io; short names become docker.io names.
  - **Named resources:** every request naming a container, pod, volume or network must name one with this session's dev labels. That keeps the agent's own container, the proxy, both sockguards, the holder, other projects' containers and the live stack out of reach.
  - **Exec sessions:** they can be used only if the gate created them.
- **Proxy variables.**
  - The **agent** keeps both spellings of the proxy variables; **dev containers** get the lower-case ones only (sockguard refuses case-variant duplicate keys in a create body). curl, wget, Python and Go read lower case.
  - `no_proxy` covers the session subnet and `.dns.podman`.
  - **Known limit:** a client that applies the proxy to a bare service name (`http://web:8080`) asks squid, which cannot resolve it. The agent uses `curl --noproxy '*'`, as the session notes say. For services calling each other over HTTP by bare name, this is the step 5 review's call.
- **Masks are now an empty read-only file, not `/dev/null`.** A character device in the project tree made a rootless `podman build` of that directory fail (`operation not permitted`; found by bisecting the build context). Every mask is now a read-only bind of one empty 0444 file in the session's run dir; the audit requires those mounts to be read-only.
- **Dev sockguard config.**
  - A profile has its own `request_body` policy and does not inherit the root's; found when every pull was refused. The policy now sits in the profile.
  - Blind writes are acknowledged, because exec takes any command; the gate scopes exec to the session's dev containers.
  - The rules were checked with an audit-mode discovery run of compose up and down, run, exec, logs and build.
  - Not allowed: image deletion, archive, export, commit, checkpoint/restore, kube, secrets, prune.
- **Teardown** removes the session's dev containers, pods, volumes and networks, including anonymous volumes (`rm -v`, since 1 Oct; before that they survived, review step 5C M1); dev data does not persist between sessions. Built images stay in the store (labelled with the session).
- **End-of-session scan** (`agentbox/scan.py`):
  - **At start:** HEAD, changed files, executables and git config of every repo in the writable grants.
  - **At the end, it reports:**
    - possible secrets in the lines the session's commits added. The pattern is the host pre-commit hook's own regex, read from the hook at run time, in the same `grep -E -i` engine, with its `scanner:allow` marker ignored. The scan carries no copy of the pattern: without a hook it says the secret scan was skipped. Matches are shown by their first 12 characters only.
    - changed files that run later on the host;
    - files that became executable;
    - changed git config.
  - It prints a summary when something is found, keeps the full report as `<session>-<start>-scan.log`, and never blocks.

## 2026-10-01 · OMP repo code in lockdown (Tim's decision 1)

- **Checked first:** can OMP switch off only repo-local extensions, hooks, tools and MCP servers? No.
  - Its native loader reads the repo's `.omp/` and `~/.omp/agent` together, with no project-only setting.
  - The only project switch is `mcp.enableProjectConfig`, for MCP.
- **So, in lockdown only:**
  - OMP runs with `--no-extensions` (this drops Tim's plugins in lockdown);
  - a read-only overlay (`~/.omp/agentbox-lockdown.yml`, passed with `--config`) sets `mcp.enableProjectConfig: false`;
  - every repo code location that exists at session start (`.omp/{extensions,hooks,tools}`, `.omp/mcp.json`, `.omp/.mcp.json`, `.claude/{extensions,hooks,tools}`, `.mcp.json`) is covered empty and read-only.
- **The covers are needed:** `--no-extensions` does not stop a repo's custom tool.
- **Test** (`test_review_1001.test_omp_repo_code_does_not_run_in_lockdown`): code planted in all of those places.
  - With approvals off (the control), the OMP extension, OMP hook, Claude hook and OMP tool ran.
  - In lockdown, none ran.
- **Other presets:** unchanged.
- **Note for Tim:** the covers alone might keep repo code out while leaving the plugins on. Not done; `--no-extensions` stays as decided.

## 2026-10-01 · `no_proxy` in dev containers (Tim's decision 2)

- **What:** the gate sets `no_proxy` (lower case; sockguard refuses case-variant duplicates) in every dev container to `localhost`, `127.0.0.1`, the session subnet, and the names and aliases of the dev containers already on the session network, plus the container's own. The session's infrastructure (agent, proxy, holder) is not listed. A value the compose file sets wins. No squid changes.
- **Session notes:**
  - if a service-to-service call still goes to the proxy, add the name to `no_proxy` in that service's compose `environment:`;
  - some clients ignore `no_proxy` (busybox `wget`: use `-Y off`). Found in the itest: busybox `wget` went to the proxy with `web` in `no_proxy`, and reached `web` directly with `-Y off`.

## 2026-10-01 · Session-built images (Tim's decision 3)

- **What:**
  - Builds carry `agentbox.project` and `agentbox.session`, stamped by the gate.
  - `ab gc` removes a project's session-built images (`agentbox.role=dev`) older than `image_keep_days` (7), except the newest image carrying each tag. With podman a tag points at one image, so in practice old untagged rebuilds go and each tag's current image stays.
  - The removal is never forced, so an image a container still uses stays.
  - `ab kill` leaves images alone.

## 2026-10-01 · Guard changes and false positives (Tim's decision 4)

- **What:**
  - Any edit to `.claude/` goes in its own commit prefixed `guard:`, with the reason, and is listed in the report.
  - When the guard blocks a false positive, it is reported.
  - The builder does not route around a block unless the blocked text is prose or test data, and says which.

## 2026-10-01 · The gate checks field spellings and what podman stored (builder's finding)

- **Found:** the step 5 review stopped before its findings. Item 7 of its brief (podman matches JSON field names case-insensitively) was cheap to check, so the builder checked it.
  - A libpod create body with `"PortMappings"` instead of `"portmappings"` passed the gate and the dev sockguard. Podman created the container with `0.0.0.0:18081` published (create only, never started, in a test).
  - `CAP_ADD` was caught by sockguard; ports were not.
- **Fixed in the gate, two ways:**
  - **Before forwarding:** it refuses a JSON object holding two keys that differ only in case. It renames every field it knows to its exact spelling, per body kind, before checking.
  - **After every container or pod create:** it inspects what podman stored and removes the container or pod, then refuses, on any violation. It checks: published ports, privileged, added capabilities, devices, host/`ns:`/foreign `container:` namespaces, networks other than the session's, binds outside the grants, cgroup parent, restart policy, OOM score, dev labels. That closes this class of parsing differences, not just case.
- **Tests:**
  - `test_gate.SpellingAndVerification` (unit);
  - `test_step5_gate.test_c_refusals`: four case-variant bodies through the real gate, all refused, nothing created.

## 2026-10-01 · Step 5 audit: A1, A4, B, C fixed; mounts and privilege by field table (builder)

Findings and responses are in `docs/reviews/2026-10-01-step-5{A1,A4,B,C}.md`; the field table is `docs/reviews/2026-10-01-step-5-coverage.md`. Deviations from the design as it stood:

- **Create bodies are allowlisted.** Every top-level field of a libpod create, pod create, network create and volume create must be one the gate knows; an unknown field is refused. The field list came from capturing podman-remote's own requests: a local proxy answered every create with 500, so nothing was made. The capture found host-reaching fields the gate had passed through:
  - `oci_runtime` (a host binary run as the runtime);
  - `conmon_pid_file`, `log_configuration.path`, `healthLogDestination` (host files written);
  - `init_path`, `base_hosts_file`, `rootfs`, `overlay_volumes` (host paths read or mounted);
  - `env_host` (the podman service's whole environment: 76 variables on this host);
  - `secrets`/`secret_env`, `dependencyContainers`, `volumes_from`;
  - `volumes[].Options` with `volume-opt=` (creates a bind-backed volume implicitly);
  - `seccomp_policy=image`, `cgroups_mode=disabled`, `resource_limits.unified`, and `run.oci.*` annotations (crun's `keep_original_groups`).
  - All are refused now.
  - The allowlist was checked against both podman clients in use: the host's and the one in the agent images (both 5.7.0). The image's client also sends `volatile` on every create, which the first itest run caught (compose up refused). It is allowed: it only skips storage syncing.
- **The gate keeps its own record of checked objects** (`verified`). A container, pod, volume or network is usable only after its post-create check passed. This answers A4's timing question (see the A4 response).
- **Start is checked too (found 1 Oct, A2 replacement):** podman resolves a bind source when the container *starts*. A project directory swapped for a symlink after create was mounted at start with the symlink's target, and no race was needed. Start, restart and init now re-check the container from what podman stored, re-resolving bind sources. After the start, the gate reads `/proc/<pid>/mountinfo` and removes a container whose binds came from outside the grants. **Residual (for Tim, below):** a swap timed between the gate's check and the runtime's mount is caught only after the fact.
- **Limits:** explicit memory and pids limits must be positive and at most 4 GiB and 4096 per container (`memory_max`, `pids_max`). Swap is off for dev containers (swap = memory). A client OOM score other than 1000 is refused (it used to be overwritten).
- **Not supported in the dev tier, refused by the gate with a reason.** Sockguard refuses each of these anyway, so nothing that worked before stops working:
  - **Docker-API container creates:** sockguard refuses the forced `HostConfig.CgroupParent`, and without it the container would leave agentbox.slice. Compat creates never worked through the gate; nothing had exercised them end to end. podman and podman-compose use the libpod API.
  - **Pods with an infra container:** sockguard's owner policy cannot resolve pods (pod inspect through it is 404), and it refuses any container that names one.
  - **Sysctls:** sockguard refuses all of them.
  - The gate's checks behind the first two stay, behind config flags `compat_creates` and `infra_pods` (both off), and are unit-tested.
- **Network create:** no options at all. Sockguard refuses network options, so `isolate=strict` cannot be set on agent-made networks. They are internal, and nothing can attach to them: containers are pinned to the session network, and connect is refused.
- **Volume labels:** podman's libpod volume API reads `Label`; the gate had been stamping `Labels`, which that API ignores. It now stamps the right key per API and checks the stored labels after create.
- **Builds:**
  - the query must use known keys;
  - tags must be `localhost/` names, never `agentbox*`, and never a name an image outside this project's dev builds already has;
  - secrets come only from the context.
  - The session's podman service gets its own `TMPDIR` (`<data>/tmp/<session>`, removed at teardown). buildah keeps `RUN --mount=type=cache` there, so a session's cache is never shared with the host's builds; this was checked on this host.
- **Paths and framing:** the gate's own deny list (push, commit, kube and the rest), canonical paths, no pipelined requests, and no ambiguous framing.
- **End-of-session scan:**
  - git runs only in repos whose metadata is unchanged since the start, hardened as described in the scan's docstring;
  - changed files are found by content hash;
  - commits are scanned on all refs;
  - the scan and log keeping run in the module teardown, so `ab kill` and `ab gc` do both, and the scan can no longer stop a teardown.
- **OMP lockdown** covers the project's `.omp` whole, plus Claude's settings files.
- **For Tim (residual, high; decided later the same day, see "Bind sources" below):** the start-time race. A session can swap a bind source between the gate's start check and the runtime's mount. The swapped mount is then detected from mountinfo and the container removed, but its process has already run. The options:
  1. **Accept,** with detection and removal as now.
  2. **Bind only the grant roots themselves.** No sub-path binds; the agent cannot replace a grant root. This breaks compose files that bind `./data`.
  3. **Rewrite sub-path binds** into a gate-made volume over the grant root plus a `SubPath`. podman resolves the sub-path inside that root, so a symlink cannot leave it; this wants a check of podman's sub-path handling first.

## 2026-10-01 · Bind sources: sub-paths through a grant volume (Tim: not option 1; option 3 if podman holds)

- **Decision (Tim):** the start-time window is not acceptable. It could mount any host path (~/.ssh, the token file, live `.env` files) into a process the agent controls, and removing the container afterwards is too late. First option 2 (grant roots only), then option 3 only if podman stays inside the volume in every case.
- **Option 2, done first:** only a grant root itself may be bind-mounted. A grant root is a mount point in the agent's container, so the agent cannot replace it.
- **Option 3, evidence on this host** (podman 5.7.0, 1 Oct; throwaway `abt-` volume and containers under /tmp):
  - **Setup:** a local volume with `type=none,o=bind,device=<root>`, mounted with `subpath=`. podman resolves the sub-path with `securejoin.OpenInRoot` (`openat2`, resolution confined to the volume).
  - **Symlinks pointing out of the root,** relative (`../outside`) and absolute, present at create: refused (`no such file or directory`).
  - **Sub-path a directory at create, swapped for a symlink out before start:** refused at start, for relative and absolute targets, and when the swapped part is an intermediate component (`mid/b` with `mid` swapped).
  - **A symlink inside the root:** followed, and it stays inside.
  - **Race:** 150 starts while a loop flipped the sub-path between a directory and a symlink out. 32 saw the inside file, 118 were refused, none saw the outside one.
  - **Control:** the same race against a plain sub-path bind escaped in 21 of 60 starts.
  - **Removing the volume:** removing the bind-backed volume, both while a container used it and after, left the root's files in place.
  - **Through the real gate:** `test_step5_fields.test_bind_sub_path`. A sub-path bind works; the same path swapped for a symlink out before start is refused by podman (500, `securejoin.OpenInRoot ... no such file or directory`) and never shows the outside file.
- **So option 3 is in:**
  - A container's bind of a path below a grant root becomes a `SubPath` of the gate's own bind-backed volume over that root. That volume is named `<session>-grant<n>`, created on the raw service and labelled role `grant`, so the agent can name it in no request.
  - Mount options carry over where a volume accepts them (`ro`, `z`, `U`, ...); propagation and `rbind` are dropped.
  - A grant root itself is still a plain bind.
  - After create, a bind must be a grant root, and a grant volume's device must be one. After start, mountinfo must show the root, or a path inside it for a grant volume.
- **Pods and Docker-API creates keep option 2** (sub-paths refused): pods with an infra container and Docker-API creates are refused anyway, and infra-less pods' mounts are rare.

## 2026-10-01 · Dev-tier image allowlist (Tim, A4 low)

- **What:** the images the dev tier may use are restricted.
  - **Run:** a container's `image` and `image_volumes`.
  - **Build:** every image the Containerfile names (`FROM`, `COPY --from`, `RUN --mount=...,from=`).
- **Allowed:** an image with a name from an allowlisted registry (docker.io, ghcr.io, lscr.io, quay.io), an image built in this session, or a name matching the project's `dev_images` patterns (`ab project add --dev-image 'localhost/myapp:*'`). Any other local image, such as another project's build or a live stack's own `localhost/` image, is refused.
- **Builds:**
  - **Spooling:** the gate spools the build context to the session's TMPDIR (8 GiB at most) and reads its Containerfile(s) before forwarding.
  - **ARG substitution:** ARG defaults and build args are substituted. A reference the gate cannot resolve is refused.
  - **Not images:** stage names, stage numbers and `scratch`.
  - **Heredoc bodies** are skipped.
- **Consequence:** an image this project built in an *earlier* session is not usable by default, because it is not "the session's own build". A compose service whose image already exists is refused until it is rebuilt or listed in `dev_images` (for example `localhost/<project>-*`). That is Tim's default as given; widening it to the project's own earlier builds is a one-line change if wanted.

## 2026-10-01 · The local model selftest line is SKIP when the model is off

- `proxied local model ... allowed` is a SKIP, with the reason, when `10.0.0.2:8080` does not answer from the host. It is the same host-side check the direct cases already use. When the model answers, the line is a real check.

## 2026-10-02 · A project's own builds are the gate's record, not labels (Tim)

- **What:** the dev tier may run or build from an image that this project built through the gate in any session, not only the current one.
  - **The record:** which images those are is the gate's record, `<data>/built-images/<project>.json`. Each image ID was taken from the last line of a build reply the gate forwarded itself (podman 5.7 ends a successful libpod build with the full image ID; checked 2 Oct). It is written under a lock.
  - **Labels:** a label on an image does not count, here or for build tags. An image carrying this project's labels but built outside the gate is refused, for running, for building from it, and for taking its tag (`test_review_step5.Images`, `test_step5_fields.test_image_forged_label`).
  - **gc:** `ab gc` drops an image ID from the record when it removes the image.
- **Replaces:** "this session's own builds" (decided 1 Oct), and the label checks for build tags (A4-G8).

## 2026-10-02 · Selftest egress failures are diagnosed, not retried (Tim)

- The probe reports the time each proxied check took.
- When a proxied check fails, its result gets three things:
  - the proxy's access-log lines for that host, from the session's kept proxy log;
  - the DNS answer for the host, resolved from the host;
  - the time the DNS lookup and the check took.
- The same goes to `<logs>/<session>-<start>-selftest-diag.log`.
- There are no retries. This is for diagnosing the 1 Oct flake: three proxied CONNECTs unanswered in one step-4 selftest, which passed alone and in a full rerun.

## 2026-10-02 · What real podman-compose needed (the trial)

The trial's agent could not run an image it built. The itests had passed with hand-built requests while real compose failed. `test_step5_compose` now runs a real compose workflow through the real gate, and these fixes made it pass:

- **Builds reach the proxy.** A build's RUN steps run in the holder's network namespace with their own resolv.conf, where the proxy's container name does not resolve.
  - The gate passes the proxy by its IP, as build args in both spellings (buildah gives these predefined args to RUN steps as environment).
  - It sets the build's DNS server to podman's resolver on the session network's gateway.
- **The project's own images can be run.** `podman run` and compose pull with `policy=missing` before every create. A pull of an image that is already present, and that the dev tier may use (in the gate's build record, or listed), is answered by the gate (`{"images": [id], "id": id}`) and never forwarded.
- **Start no longer trips over podman's own annotations.** podman adds the stop signal and stop timeout as annotations: `org.opencontainers.image.stopSignal`, `org.systemd.property.KillSignal`, `org.systemd.property.TimeoutStopUSec`.
  - The post-create and start checks accept exactly the values podman derives from the container's own config.
  - They also accept the image's own annotations, compared by value.
  - `run.oci.*`, `org.systemd.*` and the other runtime-changing prefixes stay refused, even from an image.
- **`depends_on` (`--requires`)** is allowed when every named container is this session's and passed its check. The gate passes them by ID, and the post-create check requires the stored dependencies to be checked containers.
  - **Note (updated 6 Oct):** `prepare_start` now initialises and checks each dependency too (and holds its lock), so a dependency podman would start is checked, not just trusted from create time.
- **`NO_PROXY` and `no_proxy`** are two variables. The case-duplicate check now applies to JSON field names only, not to keys of data maps (env, labels, annotations, ...).
  - The dev sockguard still refuses an env object with two keys that differ only in case, so the gate moves the second spelling into `envmerge` (`KEY=VALUE`), which podman sets the same way.
  - A value containing `$` would be expanded there, so that one case is refused with a message.
- **A missing container, pod, volume or network is answered 404,** as podman would. Compose asks whether things exist before it creates them, and the gate's 403 broke that.
- **Pods:** the dev sockguard's owner policy cannot resolve pods (compose's `pod rm` failed and `down` left the pod).
  - For a pod that is this session's (labels and the verified record, checked first), the gate sends these operations to the podman service itself: inspect, exists, top, start (after its per-member checks), stop, kill, restart and remove.
  - Teardown (what `ab kill` runs) removes a pod left behind.
  - Both are tested in the compose itest.
- **Images:** curl and python3-venv are added to agentbox-claude and agentbox-omp.
- **Unchanged:** compose `down` still asks to delete the session network and is refused. `down` succeeds anyway.
- **Found by the full itest runs after these fixes:**
  - **A race in the build record.** The gate wrote a build's image ID after the client had the whole reply, so a build started right away (the same tag again) could be refused. The ID is now recorded as soon as it passes through the gate, before the client sees the end of the reply.
  - **Kept proxy logs now include squid's stderr (cache.log)** when the proxy handled any request. It says why an upstream connection failed: one full run had squid answer 503 to a build's CONNECT to pypi.org. That is the egress watch item in progress.md.

## 2026-10-02 · Review of 785ee15 and trial 2 (Tim)

The review's responses are in `docs/reviews/2026-10-02-785ee15.md`.
- **H1, the build record:** only the final ID of a build reply is recorded, before the client sees it, and only for an image this session labelled and created during the build.
- **Pods:** pod operations go to the podman service by the checked ID.
- **Case-duplicate exemption:** keyed by path.
- **Status codes:** "not this session's" answers like "missing" (404).
- **Audit trail:** the gate log is the only record for pod operations sent to the podman service; sockguard does not see them.

From trial 2:
- **Short image names:** podman-compose builds `localhost/<project>_<service>` and runs the short name. A short name whose `localhost/` image is in this project's build record now resolves to it: a pull is answered locally, a create is rewritten to the full name, and a build `FROM` it is checked as that image. Tested in `test_step5_compose` (no `image:` line).
- **The agent's own HTTP calls:** they went to squid because its `no_proxy` lacks the service names, and requests ignores CIDR entries. podman's DNS answers `<service>.dns.podman` on the session network, and the agent's `NO_PROXY`/`no_proxy` already held `.dns.podman`. curl and Python's urllib reach `web.dns.podman` directly, and squid never sees it (`test_step5_compose`). The session notes now tell the agent to use `<service>.dns.podman`.
- **Agent memory:** it persists per project across sessions, and stays.
  - **Scan:** the end-of-session scan lists the memory, instruction and settings files of the agent state that changed, with a diff. Credential-like files are listed by name only; transcripts are not read. The start snapshot is taken after agentbox seeds the state, so its own notes are not a change.
  - **Reset:** `ab project reset-state <project>` moves a project's agent state to `<backups>/<project>/agent-state-<stamp>`. It asks first (`--yes` skips the question) and refuses while a session runs.
  - **Lockdown:** sessions use an empty per-session state (`<data>/ephemeral/<session>`), removed at teardown.
- **`ab preview` is built.**
  - **Forwarder:** `ab preview <session> <service>:<port> [--port N]` starts a forwarder container (busybox `nc -lk -e`) on the session's internal network. It publishes `127.0.0.1:N` only (a container on an internal network can publish on loopback; checked on this host), never 0.0.0.0 or a LAN address.
  - **Target:** only a running dev container of that session can be previewed.
  - **Removal:** the forwarder carries the session's labels, so the session's teardown, `ab kill` and gc remove it. `ab preview <session> --stop` removes it earlier.
  - **From the desktop:** `ssh -N -L N:127.0.0.1:N <host>`. Tested in `test_step6_preview`.

## 2026-10-05 · The egress flake, measured (Tim: top priority before the first real project)

- **Tool:** `make egress-soak` (`tests/soak/egress_soak.py`). One packages-mode session (example.com as an extra domain); 300 sequential and 10 workers x 30 parallel requests through its squid to pypi.org, files.pythonhosted.org, registry.npmjs.org, github.com and example.com. It reports failures and p50/p95/max per host and phase (client side), and per host and upstream address family (squid side, from its access log), plus squid's cache.log errors and its cgroup memory and CPU.
- **squid's access log** now also records its total time and DNS time per request (`%tr %dt`, appended, so the existing parsers are unchanged), and `buffered_logs off`.
- **Idle (5 Oct):** 600 requests, 0 failures. Every upstream connection squid made was IPv4 (no IPv6 attempts). Sequential p95 about 250-300 ms; parallel p95 1.3-2.3 s, max 3.4 s.
- **During `make itest` (5 Oct):** 600 requests, 0 failures, all IPv4. Parallel max 5.4 s. squid: memory about 118 of 256 MiB, CPU at most 4.7%, no memory events. That itest run passed in full.
- **The same parallel requests straight from the host** (no agentbox, squid or podman): no errors, but the same tail, max 5.2 s, 89 of 300 over 900 ms, clustered near 1, 2, 4 and 5 s. That points to retransmitted connection attempts on this host's uplink.
- **Conclusion:**
  - **Not IPv6:** squid never used it.
  - **Not squid's resources:** memory and CPU were far from their limits, so nothing about squid's memory or the tests' parallelism needs changing.
  - **What the evidence shows:** the latency tail is the host's own network. The earlier failures (20 s probe timeouts, a pypi 503 during a build, Chromium) match rare, longer stalls of that kind. None was reproduced in 1,200 soak requests or in the full itest runs.
  - **No fix applied.** There was nothing in agentbox to fix. Next time the failing line will carry squid's log line with its upstream address, total and DNS time, plus its cache.log.

## 2026-10-05 · Covers are enforced at the gate, not only in the agent's mount namespace (critical)

Found by the first real-project session (spec: `docs/agentbox-masked-paths.md` in that repo, §6.2). The agent's
read-only and empty covers — secret masks, `.git/hooks`, `.git/config`/`config.worktree`, compose-file covers,
the OMP lockdown covers, and the `.env` dev-swap — live in the agent container's own mount namespace. A
container the agent starts through the gate does not have them: a `-v` of the whole project root gave a
writable bind with the covers absent, so a root container could read and write anything under the grant,
including a live database and `.git/hooks`. The existing cover tests only checked from inside the agent's own
container (the spec's §5.1), so they missed it.

- **Fix (gate).** The session passes the gate a `covered` list: the real host path of every mask/cover in the
  mount plan (mask and tmpfs covers; read-only binds inside a grant that are not a grant themselves; the
  `.env` dev-swap, where the bind's destination differs from its source) plus the adapter's project covers.
  For every bind the gate forwards — a grant-root bind, a sub-path (which becomes a grant-volume SubPath), and
  the compat `Binds`/`Mounts` forms — it canonicalises the source and refuses if it resolves onto, under, or
  over any covered path. So binding the whole project root is refused (it contains covers), binding `data/`,
  `.git`, `.git/config` or `.env` is refused, and a sibling like `dev/data` is allowed. The after-start
  mountinfo check maps a grant-volume's mounted path back to a host path and refuses it too, in case a symlink
  swap redirected a SubPath onto a cover (the grant volume resolves SubPaths in-root, so a swap stays in the
  root but could otherwise land on `data/`).
- **A cover-free grant can still be bound whole.** Only grant roots that contain a cover are refused; a
  read-write grant with nothing covered under it binds directly. The field test adds such a grant for its
  allowed bind case.
- **`.git/config` read-only.** It was already a read-only bind in the agent container (verified), so the agent
  cannot set `core.hooksPath` to redirect away from the read-only `.git/hooks`. It is now also covered at the
  gate, so a spawned container cannot write it either. The session notes said `.git/hooks` and `.git/config`
  are read-only; they now also say this holds for containers the agent starts, and that `core.hooksPath`
  cannot be redirected.
- **masked_paths (the project repo's spec) is not implemented here.** The request was the gate-enforced cover
  check; the covers that exist today (secret masks, git, compose, lockdown) are what the gate now enforces for
  spawned containers. A general per-project `masked_paths` setting would add its paths to the same `covered`
  list and needs no further gate work.
- **Tests.** `tests/integration/test_step6_covers.py` is the spec's §5.2 bypass test, run from a gate-spawned
  container (a must-pass): binding the root, `secrets/`, `.env`, `.git`, `.git/config`, `.git/hooks`, a
  relative and a `..` spelling are all refused and never expose a seeded sentinel; a non-covered sub-directory
  binds; the Docker-API `Binds` form is refused too; `.git/config` is read-only. Unit tests:
  `test_review_step5.Covers` (overlap, compat, after-start swap) and `test_session.CoveredPaths`.
- **Tests that only covered the agent container (so missed this):** the selftest filesystem checks run by the
  probe inside the agent container — `test_step1_core.test_selftest_python_preset` / `test_selftest_lockdown`
  (the `.git/hooks`, `masked`, `.env shows .env.dev` cases) and `test_agent_can_commit`; the OMP lockdown
  cover tests `test_omp.test_lockdown_covers_repo_code` and `test_review_1001` (argv / in-container only); and
  the unit mount tests `test_mounts` (plan only). They stay — the in-session check is still worth having — but
  the gate test above is what guarantees the covers hold for spawned containers.

**This reopens the mount parts of the step-5 review; that area is re-reviewed (`docs/reviews/2026-10-01-step-5-coverage.md`, 5 Oct note) before the first real project runs.**

**Corrections after the independent audit (`docs/reviews/2026-10-05-covers.md`, same day).** The entry above over-claimed. Three highs were real and are fixed:
- **H1, a cover's parent renamed (confirmed on this kernel):** Linux refuses to rename a mount point but not a directory that contains one, so `mv .git .g` or `mv dev dev2` moved a cover to a path the gate's list did not name. Fix: in a dev-tier session, every directory between a grant root and a cover is bound onto itself in the agent's container (pinned), so renaming or removing it fails with EBUSY. Consequence: moving a file out of such a directory fails with EXDEV (`git mv` included; `mv` falls back to copy and delete, which copies the masked view). The session notes say so. Backstop: the gate records each cover's (st_dev, st_ino) at session start, never resolves a cover path again (a symlink left at the old name cannot redirect the entry), and refuses every further bind if a cover was moved, replaced or deleted. A future `masked_paths` setting needs no gate work beyond adding its paths to the covered list (the pins are derived from it) — the earlier "no further gate work" was only true with the pins.
- **H2, a sub-path swapped onto a cover between create and start:** the old after-start check ran after the entrypoint, and skipped a container that had already exited. Fix: start, restart and pod start now have the gate stop (for restart) and `init` each container and its dependencies, read `/proc/<pid>/mountinfo` while the entrypoint has not run (checked on this host: init gives a PID with mounts in place; an exited container can be re-initialised), and only then forward start. A missing PID or unreadable mount table is a refusal. A restart is forwarded as a start of the re-initialised container.
- **H3, a read-only grant bound read-write:** the gate keeps the read-only grant roots, forces `ro` on binds and grant-volume mounts from them (libpod and compat forms), and the mount check refuses a read-only grant mounted `rw`.
- **Builds (Tim's question):** a build context is not a bind. The client packs it inside the agent's own mount namespace, where every mask reads as empty, so `COPY secrets/` or `COPY .env` gets empty files and the dev-swap `.env` gets the `.env.dev` values; `COPY --from` is images only; `RUN --mount` binds the uploaded copy, not the project. Measured: a Containerfile COPYing `secrets/` and `.env` got 0 bytes of each. The gate now also checks the spooled context: content at a masked path means the context was not packed through the masks, and the build is refused. Not refused: a context that merely *contains* a cover as the session sees it (empty masks, `.git`), since that is what `COPY . /app` from a project root sends in every repo with a `.env` or `.git`, and refusing it would break those builds without protecting anything. (Tim asked for "a build COPYing a cover is refused"; this is narrower — say if you want empty covers refused too.)
- **Medium/low:** the `.git/config` probe that proved nothing was replaced with the rename cases (M1); added cases for the dev-swap `.env`, a nested mask, compose covers, and a read-only grant. A masked file with other hard links is reported when the session is planned (L1; a cover hides a path, not an inode). Naming the gate's grant volume directly is refused (L2, test). Image save and export are refused in the gate itself (L3). Not changed: `.git/config.worktree` (covered by the same code as `config`, not tested separately); lockdown covers (lockdown has no podman tier, so nothing can be spawned); `RUN --mount=type=cache` (per-session TMPDIR already, 2 Oct).

## 2026-10-06 · Track 1 (the grant is the primary wall) and the second covers audit (Tim)

The first covers fix was reviewed again (`docs/reviews/2026-10-06-covers2.md`, N1 confirmed). Covers can't be the primary guarantee, so there are two tracks.

**Track 1 — an enforced clean grant (`agentbox/grantcheck.py`), the primary rule; covers are defence in depth behind it.** Before a session starts (`ab run`) and at `ab project add`, each read-write grant root is scanned (masked and `mask_skip` sub-dirs skipped) for things that do not belong in a grant the agent gets read-write:
- a non-empty `.env` / `.env.*` / `*.pem` / `*.key`, or any file under a `secrets/` directory, that the masks do not cover;
- a `.git` with credentials in a remote URL (`scheme://user:pass@host`);
- live data: `*.db`/`*.sqlite`/`*.sqlite3`/`*.duckdb` over 1 MB, or any host path a compose file in the grant binds as a volume.
If any are found, the session refuses to start and names them. `ab project add --i-understand-grant-exposure`, or `ab project accept-grant-exposure <name>`, records an acknowledgement (a fingerprint of the findings, so a new exposure re-prompts), like history secrets. A path agentbox already masks or swaps is not flagged (the secret-file half of the check usually passes for a normal project; the live-data half is what catches cases like a database inside the repo). Documented as the primary rule in CLAUDE.md and the design doc: the grant holds code; secrets and live data live outside it.

**Track 2 — the N-findings, order N1, N2, N3, then the lows.** The two podman-behaviour checks the reviewer named were run first:
- **stop after init:** `podman create && podman init && podman stop` leaves the container `exited` with no PID. So N1 is real: a stop between the gate's check and the forwarded start lets the start re-initialise it and re-resolve a sub-path.
- **duplicate destination:** `--mount type=tmpfs,dst=/x --mount type=bind,...,dst=/x` is refused by podman (`duplicate mount destination`), either order. So N2 (a real bind mounted over a directory mask) would have failed the whole session, not silently exposed — but it is fixed to not produce the duplicate at all.

Fixes:
- **N1 (high):** the gate keeps a per-container lock. A start holds its containers' (and their dependencies') locks from `prepare_start`'s init-and-check through podman's start reply; `stop`, `kill`, `pause`, `unpause`, `restart`, `init` and `remove` for a container (and a pod's members) take the same lock, so nothing re-initialises it in the window. After the reply, with the locks still held, the gate rechecks: a different PID means a re-init slipped in (removed, refused); the same PID still running is re-checked strictly; an exited container is accepted because the held lock rules out a re-init. Unit tests plus a 150-iteration stop/start race itest (`test_step6_covers`, no leak).
- **N2 (medium):** nothing is mounted, pinned or listed inside a directory mask. The plan drops every mount whose destination is strictly under a mask/tmpfs cover; `pin_args` and `covered_paths` skip paths under a cover. So a git dir or compose file under `secrets/` no longer gets a real bind over the empty mask.
- **N3 (medium):** the gate checks `RUN --mount` itself, not only buildah: a `source=` that is absolute or climbs with `..` (for a context bind or a cache), and `bind-propagation=`/`relabel=`, are refused.
- **N4 (low):** another hard link to a covered file is masked too (not just noted), for masks, files under a mask dir, the real file behind the `.env` swap, and `.git/config`.
- **N5 (low):** `covers_intact` is checked at start as well as create; the message says a host-side edit (`git config`, an editor that saves by renaming) can trip it.
- **N6 (low):** a bind's source is walked for secret-named files that appeared after the session started; a non-empty, uncovered one refuses the bind (give-up limit 200k entries).
- **N7 (low):** a sub-path bind whose source is on a different filesystem from its grant root is refused with a clear reason (grant volumes are non-recursive binds).
- **N8 (low):** left as a documented heuristic, as the reviewer allowed. A name-based backstop (refuse any non-empty secret-named member) was tried and reverted: the dev-swap `.env` is non-empty by design, so it broke ordinary `COPY . /app` builds. The agent cannot get a cover's real content into a context anyway (masks read empty in the session), and the context-match check still refuses content at a truly-masked path.
- **N9 (test):** a symlink created at runtime inside a bound directory, absolute or relative, cannot reach a cover (tested).

**Doc over-claims the audit listed, corrected:** the H2 response, the `prepare_start` docstring and the test header no longer say a swap "never reaches a running process" without the N1 lock; the "masked_paths needs no gate work" note is corrected (entries missing at start can't be pinned; a masked dir holding a git/compose file is the N2 shape); the hard-link line now reflects N4 (masked, not just noted); the build note reflects N3/N8; the 2 Oct "dependencies without a separate start check" note is updated.

**Real projects stay off until Track 1 has landed (it has) and N1/N2/N3 are fixed (they are) and this area is independently re-reviewed again.**

## 2026-10-06 · Threat model set, and the fifth review scoped (Tim)

The threat model is now written at the top of `docs/design.md`: agentbox targets **mistake containment and cross-project isolation**, and is explicitly **not** a defence against a deliberately malicious or prompt-injected agent. The primary secret/data control is operational — keep secrets and irreplaceable data out of the grant — with `grantcheck` as its safety net, not an exhaustive wall. With that decided, from the fifth review (`docs/reviews/2026-10-06-final.md`) only four items were acted on; the rest are recorded below as known limitations, not TODOs.

**Done:**
- **H3 — `mask_skip` is not an exemption.** `grantcheck` no longer skips `mask_skip` folders: it scans them and reports a secret-named file there as "unmasked (mask_skip)" (the mask also skips it, so it would be exposed). The refusal message no longer offers `mask_skip` as a remedy. Design doc updated. (`grantcheck.scan`, `test_grantcheck.test_mask_skip_is_not_an_exemption`.)
- **M4 — the ack shows the findings and tracks content.** The exposure fingerprint is now over each finding's content signature (path + size + mtime + inode), so a file whose content changes re-prompts, not only a renamed one. `ab project add --i-understand-grant-exposure` and `ab project accept-grant-exposure` both print the full findings list (not a bare count). (`grantcheck.fingerprint`, `_sig`; `test_grantcheck.test_fingerprint_changes_with_content_not_just_path`.)
- **Cheap half of H1.** The gate refuses the `U` / `chown` mount options on grant binds and grant-volume SubPaths (libpod `mounts`/`volumes` and compat `Binds`/`Mounts`): an accidental recursive re-own of bound project files is a mistake-model harm. `grantcheck` reports a grant directory or file it cannot read, or that another user owns, as a **warning** ("a container's data dir? move it out of the grant"); the warning is shown and does not descend into the directory, but does not by itself block the session. (`gate.VOLUME_OPTS`/`MOUNT_OPTION_OK`; `test_review_step5.test_u_and_chown_mount_options_refused_on_grant_binds`; `test_grantcheck.test_warns_on_foreign_owned_or_unreadable_entries`.)
- **Backups include everything on disk.** Confirmed: the per-session backup is an rsync of the grant tree, not a git export, so untracked and gitignored files are kept (only the cache dirs in `EXCLUDE_NAMES` and `mask_skip` folders are left out). Restore is the mistake-model safety net. (`test_backup_tokens.test_untracked_and_gitignored_files_are_backed_up`.)

**Known limitations (accepted under the threat model, not TODOs).** These are places where a *deliberately* hidden secret or an unusual data shape gets past `grantcheck` and the masks. They matter for a malicious agent, which is a non-goal; the operational rule (keep secrets and live data out of the grant) is the real control.
- **H2 / M1–M3 — `grantcheck` catches shapes, not everything.**
  - **Secrets under other names** (review shapes 4, 5, 16): a credential in `config/prod.yaml`, `deploy/id_ed25519`, `terraform.tfstate`, `.npmrc`, `.git-credentials`, a kubeconfig, a token-only git URL (`https://TOKEN@host`), `http.extraheader`, or a submodule's `.git/config` is neither masked nor flagged. The check keys on the mask's four name patterns plus `user:pass@` git URLs; it does not scan file contents.
  - **Data shapes beyond the suffix/size list** (shapes 8–13): a sqlite file under 1 MB, a `-wal`/`-journal`, a `.sql`/`.dump`/`.rdb`/`.aof` export, a Postgres/MySQL/Mongo/Redis data dir with no `.db` file in it, or a data dir bound by something other than a recognised compose file (a quadlet, a `podman run -v` in a script). Detection is by name/suffix and by compose binds that contain a `.db`-type file, not by database magic bytes or by "untracked = data".
  - **`data_ro` grants are not scanned by `grantcheck`** (shapes 2, 22). `history_secrets` does scan them for committed secret names; live data there is the grant's declared purpose.
  - **Exempt names and skipped dirs** (shapes 6–7): real values in `.env.dev`/`.env.example`, or a secret under `.venv`/`node_modules`.
  - **Files written into a grant during a session by a host process** (shape 21): the scan runs at start; the masks are fixed then. The operational rule is that no live service writes into a grant.
  - ~~**The expensive half of H1:** the gate does not refuse a bind of a foreign-owned dir at bind time.~~ **Done 6 Oct** (see the `ab init` / session-guard entry below): the gate now refuses a gate-spawned container's bind whose source is owned by another uid (`_refuse_if_foreign`), and `grantcheck` still warns at scan time. A missing source is left to podman to reject.
- **The L-series (lows) from the review:** `--with-secrets` on repo-local secrets is refused with the real requirement (not the dead-end `accept-grant-exposure`, fixed 14 Oct); a growing DB re-prompts as it crosses each MB; symlinks across grants; a grant symlink retargeted by a host process between scan and mount. All fail closed or are host-process-only. (Four items; earlier drafts miscounted this as "L1–L6".)

## 2026-10-06 · `ab init` scaffolds the safe layout; data in a named volume, `--data-bind` deferred (Tim)

`ab init <pod|python> --name <n>` makes the only project shape that passes `grantcheck` with no override — the safe layout is the one thing it can produce, rather than something to assemble by hand and get right. It writes the tree (`.gitignore`, a 0600 gitignored `.env`, the committed fake twin `.env.dev` with the same keys, `AGENTS.md` stating the enforced reality, `README.md`, an `app/`+`tests/` skeleton, `dev/fixtures/`; the `pod` template adds `compose.yaml`, `compose.dev.yaml`, `Containerfile`), `git init`s a normal working repo with a first commit, optionally adds and pushes `--upstream`, and then runs `ab project add` itself so the project is registered and grant-checked immediately. `agentbox/initproj.py`; `tests/unit/test_init.py`'s `test_fresh_project_is_clean_by_construction` asserts the load-bearing property for both templates: zero grantcheck findings, zero warnings, zero overrides. `repos_dir` (default `~/repos`, `AB_REPOS_DIR`) was added to config so this is testable.

**Live data lives in a podman named volume (`<n>_data`), not a `./data` bind.** The bytes sit outside the repo tree, so a repo-level `rm` or `git clean` cannot take them, and no agent session is ever granted the volume (the gate refuses any named volume the session did not create). This is the durable default and needs no masking. In-pod `/data` is identical dev and prod (the dev stack binds `./dev/data` there, the live stack the volume), so the app code never branches on environment for its data path.

**The `python` template takes its data path from config, not a `./data` in the tree** (Tim, 6 Oct). There is no live stack, so instead of a volume the app reads `DATA_DIR` from the environment: `.env.dev` (what the agent sees) sets `DATA_DIR=./dev/data` (fake data in the repo), while the real path lives only in the masked `.env` and is used outside the sandbox. No `./data`, no `dev/data`→`data` overlay. The substitute mechanism stays `.env`-only for now; the `dev/data`→`data` overlay is not built unless a real project forces it.

**The per-session rsync snapshot is NOT a data backup, and `ab init` must never present it as one** (Tim, 6 Oct). It is same-disk (`~/.local/share/agentbox/backups/...` on the same LVM), and only as fresh as the last session *start*, so for production data that changes between sessions it is neither off-box nor current. It exists to undo a mistake *within* a session (restore the grant to how it was at start), nothing more. So `ab init` was explicitly **not** built to register `./data` with the per-session backup as if that made it safe (option 2 in the scoping question, rejected). The upstream backs up code only; `.env` is gitignored; the data volume is the user's to snapshot — `ab init` says so in its output.

**`--data-bind` is deferred, with a fixed contract for when it lands:** live data in a gitignored `./data` inside the repo, masked in sessions by the existing cover mechanism (a tmpfs cover over `./data`, enforced at the gate like `secrets/`, and `grantcheck`-aware so an uncovered `./data` still blocks). It will require the user to name their OWN external backup target (`--data-backup <target>`); agentbox masks `./data` but explicitly does **not** back it up, and `init` prints that `./data` is the user's to back up. No new backup subsystem is introduced for it. Until then, use the named-volume default.

## 2026-10-06 · Fifth-review H-series confirmed, and the forgotten-session guards (Tim)

The three fifth-review items that pivoted to the threat model / `ab init` before they were confirmed are now landed with tests under the mistake-containment model:
- **H3** (`mask_skip` is not an exemption): `test_grantcheck.test_mask_skip_is_not_an_exemption` — a secret-named file and a >1 MB db under a `mask_skip` dir both still produce findings, and the refusal message offers no `mask_skip` remedy.
- **M4** (the ack shows findings and tracks content): `test_session.test_a_content_change_reprompts_not_just_a_path_change` (accept, rewrite the same file's bytes, session refuses until re-acked) and `test_session.test_accept_shows_the_full_findings_not_a_count` (the CLI prints every finding).
- **H1**: the cheap half (gate refuses `U`/`chown` on grant binds and SubPaths) was already tested; the **expensive half is now built** — the gate refuses a gate-spawned container's bind of a foreign-uid source (`gate._refuse_if_foreign`, `test_review_step5.test_a_bind_of_a_foreign_uid_dir_is_refused`), and `grantcheck` still warns at scan time (`test_grantcheck.test_warns_on_foreign_owned_or_unreadable_entries`). The H2 / M1–M3 secret-name and DB-shape tail stays a documented known limitation; not built.

**Forgotten-session guards** (a session ran ~30 h holding eight containers before it was noticed). Chosen for least surprise, no auto-reaping:
- `ab ls` now shows each session's **start time, age and idle time**, and flags a running session idle longer than `session_idle_warn_hours` (default 6) with `<-- idle` plus a warning line telling how to end it. Idle is measured as time since the agent last wrote to its state dir (transcripts/memory/settings) — a cheap proxy (`session.idle_seconds`), `?` when it can't be told.
- `ab run` **warns** (does not block) when a session for the same project is already running, naming it, so a second session is never started unawares.
- **Auto-reaping was rejected**: tearing down a long-running session that is actually working would be destructive and surprising. The run-collision warning plus the `ab ls` flag mean a session cannot sit for a day unnoticed once you touch the tool again, without the launcher ever killing live work. `ab gc` still only reaps sessions whose launcher is gone (unchanged).

**Minor CLI** (cosmetic): `ab project rm` takes several names (all-or-nothing: a bad name removes none); `ab run` accepts positional `ab run <agent> <project>` as well as the `--agent`/`--project` flags (the flag form keeps `--shell` and `-- <extra>` passthrough; in the positional shorthand, flags still go through the flag form or after `--`).

## 2026-10-06 · Cleanup audit, safe-now pass: comments, real enforcement tests, support dead code (Tim)

Acting on `docs/reviews/2026-10-06-cleanup.md`, safe-now items only; no enforcement behaviour changed, no enforcement code removed (that is deferred until the tests around it bite — see the report's follow-up).

**Measured coverage** (`.venv`, `coverage run --branch`, unit tests; itest excluded): TOTAL 79%→80%; `hardening.py` 94→99%, `grantcheck.py` 84→88%, `gate.py` 79→80%, `session.py` 82%, `sockguard.py` 57% (the remainder is itest-only).

**Comments/docstrings (Rank 2):** 13 wrong comments corrected to the audit's "correct statement" (no behaviour change).

**Real enforcement tests (Rank 1/1b), each mutation-proven — guard disabled → test red → restored → green:**
- E2 `gate.holder_ns` (both fakes override it): PID-reuse check (PID must be in the holder's own cgroup) — `test_gate.HolderNs.test_refuses_a_reused_pid_not_in_the_holders_cgroup`, disabling `cid not in cgroup` → red; plus the not-running and wrong-label branches.
- E3/T2 swap-at-start on the LIVE path: the two tests that called the dead `after_start` now drive `prepare_start` — `test_review_step5.Starts.test_what_was_mounted_is_checked_at_start` / `test_grant_volume_mount_checked_at_start`; disabling `prepare_start`'s `mount_violations(strict=True)` → red (5 failures).
- E4 `after_start_checked` PID recheck — `Starts.test_after_start_checked_refuses_a_reinitialised_pid`; disabling `if now and now != pid` → red.
- E7 `gate.unmasked_secret_under` (new secret file, secrets/ dir file, scan-limit) — `CoversAudit.test_unmasked_secret_under_finds_late_secrets`; `return p`→`continue` → red.
- E8 `covers_intact` deleted cover — `CoversAudit.test_covers_intact_detects_a_deleted_cover`; `except OSError: return c`→`continue` → red.
- E1/T1 `hardening._audit_mount` bind-source/type/propagation with an allowed destination — `test_hardening.HardeningTest.test_mount_source_type_and_propagation_refused_with_an_allowed_destination`; disabling the source-outside check → red.
- E13 `session._check_setup` (adapter mount/tmpfs outside root, cover outside project, bad env, comma) — `test_session.CheckSetup.*`; disabling the mount-root check → red.
- E14 foreign/unreadable file warning, E15 uncovered secrets/ file, E16 `config.worktree` creds + compose long-form/`$VAR`/single-file/covered — `test_grantcheck.GrantScan.*`; each guard disabled → red.
- E17 `sockguard.config` bad mode and unsafe project name — `test_sockguard.Config.test_config_refuses_a_bad_mode_or_unsafe_project_name`; disabling either guard → red.
- T4 production config (both fakes force `infra_pods`/`compat_creates` on): `test_review_step5.ProductionConfig` refuses Docker-API creates and infra pods with production's own config; disabling `compat_creates` guard → red.
- T3 the misnamed `test_after_start_rejects_a_swap_onto_a_cover` renamed to `test_mount_violations_rejects_a_grant_volume_swapped_onto_a_cover` (it calls `mount_violations`, not `after_start`).
- T8 the mid-file `unittest.main()` in `test_gate.py` and `test_hardening.py` moved to the end, so the classes after them (`NoProxy`, `SpellingAndVerification`, `MaskFile`) run under direct execution too.

**Support dead code removed (Rank 3, SUPPORT modules only):** duplicate imports and the always-`?` name in `cli._mask_dsts`; two function-level `import subprocess` in `session.py`; the unused `project_root` param of `Adapter.seed_state` (base + Claude + caller); `getattr(sess, "podman_dev", …)` → `sess.podman_dev`; unused `reg_root` in a test.

**Deferred (recorded, not done):** all ENFORCEMENT dead code (D-E1..D-E11) and the duplicated enforcement logic (three secret-name checks, five "covered" computations, three `mask_skip` resolutions) — consolidate later, with tests. The `caps` preset key stays (documented inert; removing it breaks custom TOML). The "secrets.env created mid-session" drift is the accepted host-writes-during-session known limitation and was not expanded.

## 2026-10-07 · Session commands grouped under `ab session` (Tim)

The session-management verbs — `ls`, `kill`, `gc`, `preview` — moved from the top level to a noun namespace, `ab session ls` / `kill` / `gc` / `preview`, mirroring `ab project` and `ab keys`. `ab run`, `ab init` and `ab selftest` stay top-level: `run`/`init` are the hot-path verbs that *create* a project or session, and `selftest` is keyed by `--project` (it spins up a throwaway session to check a project's boundaries), not an existing session. Clean cutover, no top-level aliases — the launcher is pre-release, so there is no installed base to keep compatible; aliases are easy to add later if wanted.

Why: `ab ls` listed *sessions* while `ab project ls` listed *projects*, so bare `ab ls` was ambiguous. Now there are two parallel namespaces — **`ab project`** for the durable registry entries (the configuration: a root grant, preset, data grants, egress) and **`ab session`** for the live instances (list, kill, gc, preview) — and one project can have many sessions over time. Help text, printed warnings (idle/collision), the design-doc Usage table and inline references, trial.md, RUNTIME.md, progress.md, and the tests were all updated; older dated entries above that say `ab ls`/`ab kill`/`ab gc`/`ab preview` mean the `ab session …` forms now. `make test`: 494 OK.

## 2026-10-14 · `--with-secrets` dead-end fixed; the keyed-pod secret model confirmed (Tim)

**Bug (fixed, message only).** On a scaffolded pod (real `.env` in the repo root, masked by default),
`ab run --with-secrets` turned the masks off, so Track 1 (`grantcheck`) saw an uncovered `.env` and refused —
pointing at `ab project accept-grant-exposure`. But that command evaluates the project's *masked default*
config (`_mask_dsts`), where `.env` is covered, so it reported "nothing to accept": a remedy that does
nothing, and `--i-understand-grant-exposure` is an `ab project add` flag, not a `run` flag. The refusal under
`--with-secrets` now states the real requirement instead of the dead-end command (`session._prepare_reserved`;
`test_session.GrantExposure.test_with_secrets_message_states_the_real_requirement`). No change to *whether* it
refuses — only the guidance.

**Design, confirmed from the code (no behaviour change).** `--with-secrets` sets `secrets=exposed`, which
`mounts.plan` reads as "apply no masks", giving the agent read-write to the real secret files in the grant.
That is in tension with Track 1 and is **only cleanly usable for a project whose real secrets live OUTSIDE the
grant** (nothing in the grant matches the mask names); for repo-local secrets it is refused by design (a grant
is for code). The sanctioned ways a real external key reaches runtime without the agent getting read-write to
it inside the grant:

- **A key the agent itself needs** (e.g. an LLM-enrichment API the agent calls during development): `ab keys
  set <provider>` → `~/.config/agentbox/keys/<provider>` (0600). That path is under `config_dir`, a protected
  path no grant may overlap (`paths.protected_extra`), so it is never in the grant. It is mounted read-only
  and exported as `AB_KEY_<PROVIDER>` just before the agent starts; the provider's API domain is allowlisted
  in egress. The agent can *read* the value (env) but never has read-write to the key file.
- **A pod's own runtime secret** (the live stack's external API key, e.g. life_updates LLM enrichment): lives
  in the real `.env`, which is **host-only** and used at **deploy** (`ab deploy`, planned) when the live stack
  runs on the host. In a session the pod uses `.env.dev` (low-value dev creds) via the dev-swap; the real
  `.env` is never mounted into a session. So the keyed pod develops against dev creds and only ever uses the
  real key outside any session.

Net: the agent never needs — and is never given — read-write to a real external key inside the grant.
`--with-secrets` remains a narrow escape hatch for the secrets-outside-the-grant case, not the keyed-pod path;
whether to keep it at all, given Track 1, is left open for a later pass.
