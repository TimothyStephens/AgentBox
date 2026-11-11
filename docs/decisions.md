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

## 2026-10-15 · Lockdown OMP keeps `--no-extensions` (plugins off in lockdown)

- **What:** the `lockdown` preset continues to launch OMP with `--no-extensions` (plus `--approval-mode=always-ask` and the lockdown overlay), as it already does (`adapters/omp.py` `approvals_on_args`). This disables OMP's extension discovery entirely — the repo's `.omp/`/`.claude` code *and* the user's own `~/.omp` plugins.
- **Considered:** dropping `--no-extensions` so the user's plugins load in lockdown, relying on the REPO_CODE covers alone (empty, read-only mounts over repo-shipped `.omp/`, `.claude/extensions|hooks|tools`, settings and MCP) to keep repo code from running. The covers were shown to neutralise repo code even with extensions on (itest, 1 Oct), so they — not `--no-extensions` — are the real enforcement.
- **Why keep it:** lockdown is the "review-only, nothing runs unasked" profile; `--no-extensions` matches that intent and keeps the covers as defense-in-depth rather than the sole wall. The cost (the user's own plugins are off in lockdown) is small and bounded to lockdown — other presets run with extensions on — and one of the user's plugins (`@narumitw/pi-statusline`) is broken upstream regardless (seen in `make omp-live`, 15 Oct).
- **To revisit:** if lockdown review sessions come to depend on the user's plugins, drop `--no-extensions` and lean on the covers; it is a one-line change in `approvals_on_args`.

## 2026-10-15 · Dev databases use a session-created named volume, not a host bind (audit #7)

- **What:** the gate refuses binding a host path owned by another uid into a spawned container (the H1 fix, `gate.py` `_refuse_if_foreign`). A dev database in a `compose.dev.yaml` that binds a host directory takes ownership of that directory on first `up`, so the next `up` after a `down` is refused. We keep the strict check unchanged and steer dev databases to a **named volume created in the session**, which the gate already allows (it refuses only named volumes the session did not create).
- **Why:** the check is exactly the accident-containment the audit asked for (a session must not reach a live stack's data dir). Loosening it with a per-project exempt-paths list would reopen that hole for the listed paths. The pod scaffold already puts live data in a named volume; dev stacks should do the same.
- **Guidance:** in `compose.dev.yaml`, give a database service a named volume (e.g. `pgdata:/var/lib/postgresql/data` with a top-level `volumes: { pgdata: {} }`), not `./dev/data/db:/var/lib/postgresql/data`. A restart of a stopped container is unaffected; only a fresh `up` over a host-bind data dir the container chowned is refused. Documented in design.md.

## 2026-10-15 · Backups exclude the covered git/compose files; restore without --delete (audit #8)

- **What:** the pre-session backup excludes exactly the gate's covered paths (`1927027`): `.git/config`, `.git/hooks` and the compose files, alongside the secret masks. We keep this. `.git/config` can hold credentials, and the agent cannot damage any of these (they are read-only in the session), so there is no reason to copy them into a snapshot on disk.
- **How to restore:** copy a snapshot from `~/.local/share/agentbox/backups/<project>/<timestamp>-<session>/` over the project **without** `rsync --delete` (e.g. `rsync -a <snapshot>/<grant>/ <project>/`), so the excluded, still-present covered files (`.git/config`, hooks, compose, real `.env`) are preserved rather than removed. Documented in design.md.

## 2026-10-16 · `--with-secrets` dropped; masks can no longer be turned off (Tim)

- **What:** the `ab run --with-secrets` flag is removed, along with its plumbing (the `with_secrets` path in `presets.effective` that set the secrets value to *exposed*, `session.load_project`/`Session`/`_prepare_reserved`, and `mounts.plan`'s `secrets` parameter). `ALLOWED` lists only `locked` and `masked` for the secrets setting, so the *exposed* value was reachable *only* through this flag; with it gone, secret masks always apply and there is no route to turn them off. The secrets project setting remains as displayed metadata (locked/masked) but no longer gates masking.
- **Why:** reviewing the open question from 14 Oct, `--with-secrets` had no valid effective use. In its one "allowed" case (real secrets *outside* the grant) nothing in the grant matches the mask names, so turning masks off did nothing; in every case where it *would* act (secrets in the grant) Track 1 refused it; and a secret in a `data_ro` grant would have been exposed **unguarded**, since `grantcheck.scan` covers only `root + data_rw`. So it could never both run and be safe. The sanctioned paths cover the real needs: a key the agent itself needs via `ab keys set <provider>` (outside every grant, read-only, `AB_KEY_<PROVIDER>`), and a pod's own runtime secret via the real `.env` at deploy. Supersedes the "left open" note of 14 Oct.
- **Tests/docs:** removed `test_policy.test_with_secrets` (+ the lockdown-refusal assertion), `test_mounts.test_with_secrets_exposes`, `test_session.…with_secrets_message…`; `make test` (505) green, `make itest` re-run to confirm the launcher still stands up a session. design.md, USAGE.md (regenerated), TRIAL.md, progress.md updated.

## 2026-10-26 · The preset redesign, `ab project set`, and the open-egress invariant (Tim)

`ab init` is now the one place a project is fully configured, and a registered project can be changed in
place. The work landed in steps; an audit of the half-built state produced ten findings (F1-F10), all
actioned here.

**One definition of the project-setting flags.** `--preset`, `--data-ro`, `--data-rw`, `--egress`,
`--extra-domain`, `--build-memory`, `--mask-skip`, `--podman`, `--compose-project`, `--dev-image`,
`--observe-logs`/`--no-observe-logs`, `--accept-history-secrets` and `--i-understand-grant-exposure` are
declared once (`cli._add_project_setting_args`) and applied once (`cli._apply_project_settings`), for
`ab init`, `ab project add` and `ab project set` alike, so the three cannot drift. `test_policy.SharedArgsTest`
compares the three parsers' actions; `test_init.SharedArgsParityTest` compares the registry entry the same
flags produce through init and through add. `ab init` therefore configures a project completely at creation
time (it used to hand `cmd_project_add` a hardcoded all-defaults namespace), and prints the effective
configuration with each value's source, as add and set do.

**The preset set.** `build` (was `pods`), `build-open`, `script` (was `python`), `research`, `operate`,
`analysis-offline`, and `lockdown` unchanged. `pods`/`python` are kept as **true aliases**: separate BUILTIN
entries holding the identical dict, not a redirect, so an existing registry entry resolves exactly as before,
provenance string included (`test_pods_and_python_resolve_identically_before_and_after`). `ab init pod`
defaults to `build`, `ab init python` to `script`.

| | lockdown | build (=pods) | build-open | script (=python) | research | operate | analysis-offline |
| --- | --- | --- | --- | --- | --- | --- | --- |
| compose | ro | rw | rw | ro | ro | ro | ro |
| egress | locked | packages | **open** | packages | **open** | packages | locked |
| podman | none | dev + live observe | dev + live observe | none | none | **live observe only** | none |
| caps | refused | opt-in | opt-in | refused | refused | refused | refused |
| approvals | on | off | off | off | off | off | off |
| data | ro-only | declared | declared | declared | declared | ro-only | ro-only |

`operate`'s `{tier = "none", live = "observe"}` needed no new code: `session.podman_access` already keys on
tier **or** live. The `secrets` value is vestigial metadata (16 Oct: masks can no longer be turned off).

**The open-egress invariant (F8b), stated exactly.** `egress = "open"` is an attribute of the preset
*definition* (research and build-open declare it). It is never granted by live reference: `effective()`
demotes a preset-sourced `open` to `packages`, so the only thing that actually opens a project is an `egress`
key on its own registry entry. `materialize_open_egress()` writes that key when a project is registered or
edited with such a preset, so `ab project show` prints it as project-sourced and `projects.toml` shows it.
Three consequences, all deliberate:
- editing a preset file to say `open` later does **not** open any already-registered project;
- a **hand-written** registry entry with `preset = "research"` and no `egress` key resolves to `packages`
  (fail-closed), not open — `test_an_open_preset_is_fail_closed_by_live_reference`;
- because the open default lives on the definition rather than in a name list, a user's own `research.toml`
  that says `egress = "locked"` stays locked. (The first draft used a `DEFAULT_OPEN_PRESETS` name list, which
  a user file could not override. That list is gone.)

**F2 — changing a preset clears the overrides it was set against.** A project switched from `research` to
`analysis-offline` kept `egress = "open"`, so "offline" was open. Changing the preset (via `set`, or `add`
where there is nothing to clear) now drops the stored **security** overrides — `egress`, `podman`, `data_ro`,
`data_rw`, `extra_domains`, `observe_logs` — applies the new preset including its open-egress attribute, and
prints what it cleared. Overrides given in the same command are kept. `build_memory`, `compose_project`,
`mask_skip` and `swap` are not posture and survive.

**F8a — a user preset under a new name inherits lockdown's values** for every key it omits (it inherited
`python`'s, so a one-line file quietly got packages egress and approvals off). A file named after a built-in
still customises that built-in.

**F1 — the lockdown ceiling now covers `podman.live` and `observe_logs`.** It checked `podman.tier` only, so
`lockdown` + `live = "observe"` handed a review-only project read access to the live stack; `observe_logs` is
not one of `presets.SETTINGS`, so the override loop never saw it.

**F7 — `exec` (tier or live) is refused at validation**, so `ab project add/init/set --podman exec` is
refused where it is written, not at the next `ab run`. The value stays known, so the message is "not built"
rather than "bad value"; `session.podman_access` keeps its own check behind it.

**F3 — no registry edit while a session for the project is live.** `ab project rm` used to succeed and orphan
the session: the entry vanished, `ab project show` denied the project existed, the containers kept running,
and the grant lost its overlap protection, so another project could be registered over the live grant. `rm`
(all-or-nothing across names), `accept-grant-exposure`, `accept-history-secrets` and `reset-state` now refuse,
naming the session and the `ab session kill <session>` that ends it. **No `--force`**: ending live work is
`ab session kill`'s job, consistent with rejecting auto-reaping (6 Oct), and with the `ab project` /
`ab session` namespace split (7 Oct).

**F5/F9 — `ab project set NAME [flags]`** changes a registered project in place, re-running everything `add`
validates (preset, ceiling, build_memory, grantcheck, history secrets, the root grant), so a `set` cannot
leave an entry a fresh `add` would have refused. `--unset KEY` returns a setting to its preset value;
`--clear-data-ro`/`--clear-data-rw`/`--clear-extra-domains`/`--clear-dev-images` empty a list setting; giving
`--unset KEY` and that key's own flag together is refused. `--unset egress` on an open preset deliberately
does not re-materialise open (otherwise it would be a no-op exactly where it matters); the fail-closed
resolution applies instead. **`root` is still not editable** — moving a code grant stays `rm` + `add`, since
it changes overlap, backups and orphaned state.

**F4 — read-only data grants are scanned, as warnings.** `data_ro` was not scanned at all (the M2 known
limitation), so whatever a read-only grant held was invisible. `grantcheck.scan(warn_grants=...)` now walks
them with the same checks and reports the findings as warnings at add, set and session start. They stay
**allowed**: a read-only grant full of live data is the declared purpose of `analysis-offline` and `operate`.

**F6 — `ab init` validates every setting before writing anything.** A bad flag used to scaffold the tree,
git-init it, commit, and only then fail at registration, leaving an unregistered directory that made the
corrected retry fail with "already exists; refusing to scaffold over it".

**F10 — `--preset` lists every preset that exists** (built-ins in table order, then user presets) with its
one-line description, in `--help` and in the unknown-preset error, and `choices` now refuses an unknown name
at parse time. `docs/gen_usage.py` generates `docs/USAGE.md` against an empty config dir so the reference
lists the built-ins only, whoever runs it.

## 2026-10-27 · Second preset/settings audit: one settings table, then F1-F11 (Tim)

The audit returned eleven findings, and named the pattern behind five of them: a setting key missing from one
of five hand-maintained parallel lists (`cli.SECURITY_OVERRIDES`, `cli.UNSETTABLE`, `registry.PROJECT_KEYS`,
the display gating in `_print_effective`, the ceiling's looseness orders). Same drift class as the five
duplicated "covered" computations in `docs/TODO.md`: when one property of a key lives in five places, adding
a key gets four of them right. So the lists went first, and several findings then fell out for free.

**One table (`agentbox/projectkeys.py`).** One row per registry key, answering every property of that key:
its CLI flag(s) and argparse dest, which validator checks it, its allowed values **tightest first** (which
*is* the ceiling's looseness order), whether it is security-relevant, whether a preset change clears it,
whether `--unset` takes it, how `ab project show` displays it, and when it is inert and why. `Key` has no
defaults on the decision fields, so a new key is a `TypeError` until each is answered, and `validate_entry`
refuses a key that is not in the table — a key cannot be added anywhere else. Derived from it:
`presets.SETTINGS`/`ALLOWED`/`LOOSENESS`/`EGRESS`/`PODMAN_TIERS`, `registry.PROJECT_KEYS`,
`cli.SETTING_FLAGS`/`SECURITY_OVERRIDES`/`UNSETTABLE`/`CLEAR_FLAGS`, the ceiling comparison, F7's
loosening labels and F8's inert markers. The small validators moved out of `session.py` into it, with no
aliases left behind.

**Deliberate consequences of the table, beyond the audit's list:**
- the clearing set is the `clears` column, so a hand-written preset-setting override (`approvals`,
  `compose`, ...) is cleared by a preset change too — an override must not outlive the preset it was set
  against, whichever key it is;
- `--clear-mask-skip` exists, falling out of "list-valued and unsettable";
- `secrets` is marked `(ignored: masks always apply and cannot be turned off)` rather than removed — there is
  nothing left for it to enforce (16 Oct), and the display now says so honestly; same treatment as `caps`.
  Removing both keys is a clean follow-up but breaks existing preset files, so not in this round.

**The findings.**
- **F1 — open egress is written only when a preset is NEWLY chosen.** `materialize_open_egress` ran on every
  edit, so `ab project set p1 --build-memory 4g` on a research project silently opened its egress. One
  `newly_chosen` flag (fresh entry, or a real switch per `same_preset`) now gates both the clearing and the
  write; re-choosing the same preset is not "newly chosen", so a hand-written entry with no `egress` key
  stays fail-closed at `packages`.
- **F2 — liveness is a running container's label.** `_refuse_if_live` read the project from `session.json`
  and required a live launcher PID, so a session whose launcher had died looked absent — exactly the case
  `ab session gc` leaves alone, and exactly when `rm` orphaned it. It now asks podman for any **running**
  container labelled `agentbox.project=<name>`, taking the project and the session name from the container's
  own labels. The tests drive the fake podman's `ps` output instead of mocking `sessions()`.
- **F3 — overlap covers every grant pair.** `check_grant`'s `other_roots` only ever held other projects'
  *roots*, so a `data_rw` grant could sit inside another project's `data_rw` or `data_ro`.
  `paths.check_overlaps` is now the one cross-project check and the rule is the matrix: a read-write grant
  may not overlap any grant of another project, in either direction; read-only may overlap read-only. Run at
  add, set and session start. `check_grant` keeps only "is this path safe in itself?", so there is one
  mechanism rather than two drifting ones.
- **F4 — `compose_project` and `dev_images` are security-relevant and cleared** (from the table), and an
  explicit `compose_project` is displayed together with the fact that it waives the live-stack ownership
  check.
- **F5 — the ceiling is a preset attribute**, `ceiling = true`, not `preset_name == "lockdown"`: true for
  lockdown, and true by default for any preset a user file defines *or modifies* unless it says
  `ceiling = false` (fail-closed, and consistent with a new user preset inheriting lockdown's values —
  customising `build.toml` makes that a ceiling too). The comparison is generic, through the table's
  looseness, so it covers `podman` component-wise and `observe_logs` with no per-key code.
  `lockdown.toml` is still refused.
- **F6 — a swap can no longer expose a secret.** The stand-in may not be a masked name and the destination
  must be one, so `swap = { "notes.txt" = ".env" }` — the real `.env` mounted at an unmasked path while the
  mask covered the original — is refused. This invalidates the design doc's old `secrets.env` example, which
  is replaced with `.env.local`.
- **F7 — a preset change reports before → after**, labelled from the looseness order:
  `egress: locked → open (loosened by the preset change)`, `observe_logs: true → false (tightened ...)`,
  `(cleared ...)` for an unordered key.
- **F8, F10 — from the table.** Every stored setting is shown, with `(ignored: ...)` when the posture makes
  it do nothing, instead of being hidden (which had hidden a waived ownership check, a dead allowlist, and
  `swap` and both acknowledgement keys from every project); `--unset` on a key the project never stored says
  so instead of claiming a change.
- **F9 — left as-is, benign.** Noted in `docs/TODO.md`.
- **F11 — the whole entry is validated before saving.** `set` checked only what the command touched, so a
  hand-written key or value survived the save and failed afterwards — a secret-exposing `swap` was *stored*,
  and only the closing effective-config display raised. `projectkeys.validate_entry` (unknown keys, every
  value through its own validator, root/preset present) now runs before any write and again at session start,
  so `load_project` is as strict as `set` and a failed `set` writes nothing. `projects.toml`'s header stops
  inviting hand edits and points at `ab project set`.

**Process (CLAUDE.md).** A rule was added after I reverted `tests/live/claude-live-pass.txt`, which held a
genuine `make claude-live` result Tim had not yet committed: uncommitted changes I did not make are his —
never revert, stage or commit them; report and ask. Baseline comparisons go in a separate worktree, never a
`git checkout` in the live tree.

**Added the same week (27 Oct).** Two follow-ups from reading the output on a real project:

- **`ab project check <name>`** (read-only). `grant_exposure_accepted` is a fingerprint — a hash of each
  finding's path, size, mtime and inode — so `ab project show` could only print an opaque number, and there
  was no way to see *what* had been accepted or whether it still described the grant. `check` runs the same
  scan a session runs and prints the findings in full, both fingerprints, and the verdict (accepted /
  changed since accepted / not accepted / stale acceptance / clean), exiting 1 when a session would refuse.
  It writes nothing, and unlike `rm`/`set`/`accept-*` it is **not** refused while a session is live: seeing
  what a running project accepted is exactly when you want it. `ab project show` points at it whenever an
  acknowledgement is stored, and `check` notes when an accepted exposure sits beside `egress = "open"`.
  What the fingerprint does and does not catch, measured: editing a flagged file (size or mtime) re-prompts,
  as does a new or removed finding; a change *inside* a flagged directory, or to the credential in a git
  remote URL, does not, because those findings are fingerprinted at directory/path granularity.
- **A provenance log beside the pass record.** `freshness.record` appends
  `<commit> <ISO timestamp> egress=<mode> host=<hostname>` to `tests/live/claude-live-pass.log`
  (append-only, committed with the record). The one-line record is overwritten by each run, so an overwrite
  left no trace; the log makes it visible after the fact, which is why a loud-overwrite refusal was not
  added — re-running and replacing the record is the normal case. Writing either file already requires
  `from_live_run=True`, so only the live runner can produce a line.

## 2026-10-28 — Third audit: liveness fail-closed by construction, one grant preflight, and the rest

The audit (F-1 … F-12) found the preset/settings model holding — the open-egress invariant intact, all
eleven previous findings closed — and two highs that were the same defect in the same question. **"Is this
project live?" had now been answered wrongly in three audits running** (F3: `rm` succeeded during a live
session; F2: the launcher PID was the only signal, so a dead launcher hid running containers; F-1: the fix
for F2 *replaced* the session.json signal with container labels, so a session still in `prepare()` became
invisible again; F-2/F-10: a failing `podman ps`, a missing binary or unparseable output all returned "no
containers"). So this round was two structural fixes plus the rest, not twelve patches.

- **One liveness predicate, fail-closed by construction.** `session.project_is_live(settings, podman, name)
  -> (live, why)`. Every signal is consulted and **any** of them says live; an unreadable signal **also**
  says live, with the reason named. Used by `project set`/`rm`/`reset-state`/both `accept-*` and by
  `ab run`'s already-running warning, so there is one answer, not five. A session run dir whose metadata
  cannot be attributed to a project counts as live for *every* project (and names the session, suggesting
  `ab session gc`) — the alternative was parsing the project out of the session name, which is exactly the
  kind of inference that got this wrong three times. There is still no `--force`.
- **Podman stopped swallowing failures on safety paths.** `podman.run` turns an `OSError` into an `ab:`
  error (a missing binary was a traceback); `containers_json` and `ids` raise on a non-zero exit or
  unparseable JSON instead of returning `[]`. `_check_live_stack_is_ours` therefore refuses when `ps` fails
  rather than passing vacuously, and `teardown` warns per kind when a listing cannot be read instead of
  reporting a clean teardown.
- **The fail-open sweep.** Every `check=False`, bare `except` and `or "[]"` in `agentbox/` was classified by
  what a swallowed failure decides. Fixed: an unreadable `.git` *file* left that repo's hooks/config
  uncovered and started anyway; an un-lstat'able candidate in `hardlink_masks` left another hard link to a
  masked secret unmasked; an un-stat'able bind source read as "not a socket"; an unreadable cover identity
  was omitted from `cover_ids`, so the gate's rename detection silently did not cover it; `sessions()`'
  network listing and `_young` both read a failure as "nothing there". `ENOENT` is the one tolerated failure
  at each site, and only where absence is itself the answer. Left as they were, with the reasoning recorded
  in the commit: `egress.host_addresses` (empty makes `squid_conf` refuse), `gate.covers_intact` (an
  unreadable cover reads as "not intact"), `grantcheck`'s walk handlers (each unreadable entry becomes a
  warning), and the end-of-session scan's broad `except` (it informs after the session ended and must not
  stop the removal).
- **One grant preflight, and two senses of "covered" pulled apart.** `ab project check` built its own mount
  plan and its own covered predicate while a session built another, so `check`'s verdict was not `run`'s.
  `grantcheck.preflight(settings, name, entry, *, plan=None)` is now the only entry point, and `ab run`
  passes the plan it already built. Separating the two sets fixed a real under-report: a session passed the
  *gate's* covered set to the scan, which includes the read-only covers, so a non-empty
  `.git/hooks/deploy.key` was invisible to it — a read-only cover stops writes, not reads. The scan uses
  `mounts.hidden` (masks, mask dirs, dev-swap targets); the gate uses `mounts.covered` (that plus the
  read-only covers).
- **Acknowledgements are honoured, and a tightening is never blocked.** `ab project set` demanded
  `--i-understand-grant-exposure` again on every edit to a project with an accepted exposure, including
  `--egress locked`, which only makes the project safer. Now: findings matching the acknowledgement proceed
  silently; findings that changed refuse an edit that **grants something new** (naming what is new, changed
  or gone) and let through one that grants nothing new, with the change reported; the flag re-accepts and
  prints what is newly accepted. `ab project add` is unchanged — a new project has nothing to be safer than.
  The same rule was applied to the history-secrets acknowledgement, which had the same defect.
  **Shape change:** `grant_exposure_accepted` now stores the accepted findings' *signatures*, not only
  their fingerprint, because "refuse with a diff" has to know what was accepted. The fingerprint is derived
  from them, so `show` and `check` print the same short id as before, and an acknowledgement stored as a
  bare fingerprint (every existing one) still works — it just cannot be diffed, which both commands say.
  One key, so there is no second place to drift.
- **`projectkeys.loosenings`** is the one answer to "does this edit grant anything new?", used by the two
  acknowledgement settlers and by `set`'s report. Conservative so that *empty* can be trusted: ordered keys
  compare on their order, a list setting counts unless the new value is a subset of the old, any other
  security-relevant change counts, and a grant counts unless it is already inside a grant with the same or
  wider access. `preset` is not compared as a value (a preset change shows up in the settings it resolves
  to); `root` is compared as a grant.
- **Podman access is a capability set, not a ladder.** `none < observe < dev < exec` made a ceiling of
  `{tier = "dev"}` *allow* an override of `{tier = "observe"}` — but observe reads the live host stack while
  dev runs the project's own sandboxed one. `podman_capabilities` maps each spelling to a set
  (`dev-stack`, `live-observe`, `live-exec`) and "looser" is "not a subset", so a dev ceiling refuses
  observe and an observe ceiling refuses dev.
- **`compose_project` is explicit only when it differs.** The waiver was `"compose_project" in entry`, so
  storing the default (what `--compose-project shop` writes on project `shop`) turned off the live-stack
  ownership check while changing nothing about what is observed. `show` no longer calls that explicit.
- **A read-write grant may not sit inside the same project's read-only grant** (the deeper mount wins, so
  the path would be writable while `show` lists it read-only), nor may one path be declared as both. The
  other direction stays allowed: read-only inside read-write is tighter than the display, not looser.
- **`dev_images` may not match agentbox's own image names**, which is what catches `*` and `localhost/*` —
  patterns broad enough to cover every other local image. The dev-tier notes now list the project's actual
  patterns instead of saying "unless the project lists them".
- **The settings table's `security` column was a column nothing read.** Rather than delete it, it now
  decides which settings `loosenings` compares, which overrides `show` marks as loosening their preset,
  what `set` reports about the edit it made, and an invariant: `clears` implies `security`. That invariant
  caught `secrets`, which was cleared on a preset change while marked not security-relevant (it decides
  whether secret files are masked; that it does nothing *today* is what the `inert` column says), and the
  comparison caught that `observe_logs`/`compose_project`/`dev_images`/`mask_skip`/`swap` were skipped
  because they are absent from the effective dict — so `set --observe-logs` had counted as granting nothing.

**F-12 was never supplied.** The hand-off listed it as "the grouped cleanups" with no text, the same gap as
F9 last round; nothing was done for it. F9 itself stays as recorded in TODO.md.

**Test-only change worth noting:** several session tests called `prepare()`, whose pre-session backup needs
`rsync`, which the agentbox sandbox has not got — so they could not run while this work was being done.
Those that do not test the backup now call `prepare(do_backup=False)`, which took the sandbox's unrunnable
count from 132 to 121.

## 2026-10-28 (later) — `podman network ls` never worked, and F-12

**The template podman 5.7 rejects.** `sessions()` ran
`podman network ls --format '{{.Name}} {{index .Labels "agentbox.session"}}'`, which podman 5.7 refuses:
"error calling index: cannot index slice/array with type string". The call passed `check=False`, so it
failed silently — **network-based session detection had never worked** — and the fail-open sweep turned the
silent failure into a raise, which broke `ab session ls`/`kill`/`gc`/`preview` and the itest's leftover
checks (9 failures, one cause). Networks are listed as `--format json` now and the labels are read in
Python; that report's keys are lowercase (`name`, `labels`), unlike `ps --format json`.

A `--format` template is a contract with podman's internals *per subcommand* — `index .Labels` is fine on
`image inspect` and refused on `network ls` — so three things now stand against this class:

- **The fake podman refuses what real podman refuses** (`REJECTED_FORMATS`, quoting the host's own message),
  instead of answering every `--format` with the same canned string. That is why no unit test could catch
  it: the fake accepted a template real podman never would.
- **`tests/unit/fixtures/podman_network_ls.json`** is real host output, so the shape the parser reads is the
  shape podman prints.
- **`tests/manual/podman_formats.sh`** runs every `--format` the code issues against real podman,
  read-only, and a unit test asserts every `--format` literal in `agentbox/` appears in that script (plus
  that every podman subcommand in it is a reading one). Tim runs it on the host; it is the only check that
  can see this class for real.

**Were the other swallowed calls ever working?** Five of the six fail-open sites the sweep fixed are pure
filesystem (`_gitfile_target`, both `hardlink_masks` lstats, the mount audit's stat, `cover_ids`, `_young`)
— no podman, and each demonstrably works (masks, covers and gc behave on the host). The sixth was this
network listing. Of the podman readers made strict in the same round, only `ps -a --format {{.Names}}` and
`ps -a --format json` have direct evidence from the host (the itest waits on non-empty output from the
first; `ab session ls` printed a session's container and state, which needs the second). `network ls`,
`pod ps` and `volume ls` with `{{.Name}}`, and the `network inspect`/`inspect`/`image inspect` templates,
are standard documented formats but were **not** verified here — that is exactly what the script is for.
`tests/integration/harness.py` had the same swallow in `leftovers()`/`leftover_images()`/`all_volumes()`: a
rejected template there would have made every leftover check pass vacuously, so those listings now raise on
a non-zero exit (the call sites that legitimately tolerate failure, such as `image exists`, are untouched).

**F-12, the grouped cleanups.**

- **`swap` has a CLI flag** (`--swap TARGET=STANDIN`, repeatable). It was security-relevant, hand-edit-only
  — against `projects.toml`'s own header — and yet `--unset swap` existed to remove a key no command could
  set. The flag's value goes through the same validator, which matters most for `ab init`, where
  `_apply_project_settings` is the check that runs *before* anything is scaffolded.
- **A project's swap merges with the default** `.env` → `.env.dev` instead of replacing it. Replacing it
  meant adding one `.env.local` mapping silently stopped `.env.dev` standing in for `.env` — the mapping
  every `ab init` project is built around. `mounts.effective_swap` is the one computation, idempotent, and
  called at every boundary that reads a swap. A mapping for the same target still wins; what is no longer
  expressible is "ignore `.env.dev` even though it exists", which no project wanted.
- **The ceiling refusal names the right limit.** It said "the preset's own value is False" for
  `observe_logs`, which no preset sets — the limit there is the settings table's tightest value. It now says
  which of the two it is, and the same fix covers a setting the ceiling preset simply omits.
- **Dead parameters removed:** `cli._grant_kw(s, _reg, exclude)` (both ignored since `paths.check_overlaps`
  took over cross-project overlap, while passing them read as if they still scoped the check), and
  `session.history_secrets(..., swap, ...)` / `_history_masked(..., swap, ...)`, which fell out of use with
  the next item.
- **`mounts._masked`'s `name in swap_targets` arm is gone.** Since F6 every swap target must itself be a
  masked name, so the pattern test that followed already answered True. Removed with a test proving the
  deadness first (CLAUDE.md's rule), which is a property of the validator rather than a coverage claim:
  every swap the validator accepts has only masked names as targets.

## 2026-10-29 — The host-owned agent profile, and the inbox

Two commits. The research behind them is in this session's report; the shape it argued for is what was
built.

**The scan learned about agent files.** `.claude/` and `.omp/` (whole), plus `CLAUDE.md`, `AGENTS.md`,
`.mcp.json` and `mcp.json` by name, are "files that run later" now. A skill, hook, sub-agent or MCP server
a session adds to a repo is read by the next session and by the user's own agent outside agentbox — the
same class as a Makefile and arguably sharper, being instructions something will follow. `lockdown` already
covers those paths read-only, so this reports what every other preset leaves writable.

**The profile: `~/.config/agentbox/<agent>/profile/`, snapshotted and mounted read-only.** Modelled on OMP's
`versions/<v>/` snapshot, and the reasoning is the same: pin what a session got, so it cannot change under
it and can be named afterwards. The tree is hashed by content (paths, executable bits, bytes — no mtimes,
`.git` excluded), copied once per hash to `<data>/profiles/<agent>/<hash>/`, and mounted read-only at the
agent's load paths. Four consequences, each deliberate:

- **It loads under every preset, lockdown included.** Host-owned is a different trust class from
  repo-shipped: the threat model is about a repository (or a prompt-injected agent) planting something a
  later session runs, and the profile is neither. This is the first thing in the design that lockdown
  deliberately *does* load.
- **A session cannot teach itself.** The load paths are read-only, so Claude's user-level skill/CLAUDE.md
  writes fail. That is the point: what used to accumulate silently in a per-project config dir now has to
  go through a person.
- **No project override layer in agentbox.** Project-level customisation is the repo's own `.claude`/`.omp`,
  which already exists, is committed, is reviewed in the normal way and is now reported by the scan. A
  third layer would have meant precedence rules with nothing to decide them.
- **No symlinks in a profile.** A link would be followed when the snapshot is copied, so a file outside the
  profile would end up mounted in every session. Any link refuses the session, by name.

**`settings.json` stays a copy, not a mount.** Claude Code writes it itself — the adapter has recorded
`skipDangerousModePermissionPrompt` there since 7 Oct — so a read-only one would fail at every launch. It is
copied from the profile at each start: the host profile stays authoritative, without pretending the file is
immutable. This was the one "test whether it tolerates read-only" question, and the answer was already in
our own code.

**The inbox: `<data>/<project>/<agent>/inbox/`, read-write, never loaded.** Mounted inside the agent's own
config dir so it is in front of the agent, at a path no loader reads. It is per project and persistent — except under
`lockdown`, where it lives inside the session's own ephemeral state and is deleted with it (29 Oct
follow-up; the first version kept it, on the argument that an unloaded queue cannot weaken lockdown, and
Tim's call was that "the session keeps nothing" should hold without exceptions). The agent is told which of
the two it has, so it does not write a skill believing someone will read it, and teardown says how many
items are going. At teardown each new item in a *persistent* inbox gets a provenance sidecar (project, session, timestamp, preset, egress, agent) and
the count is printed; teardown is the only moment the host knows which items are new, since nothing hooks
the agent's writes. Provenance matters because an item is read later, out of context, and `egress open` in
particular deserves a closer look.

**Claude's memory shortcuts, measured against the new shape.** `#` aimed at the *project's* `CLAUDE.md`
still works (the repo is in the grant, and the scan now reports the change). `#` aimed at the *user-level*
`CLAUDE.md` fails, because that file is generated per session and read-only. It cannot be redirected —
nothing intercepts the write — so the session notes state both halves and name the inbox. Routing it
automatically would need Claude Code to support a configurable memory path.

**`ab profile harvest` / `promote` / `dismiss`**, read-only by default. Lists unreviewed items across every
project that has an inbox, registered or not (an item outliving its project is exactly what must still be
reviewable), marks open-egress provenance, and groups by name and content hash so near-duplicates show up
as one line. `promote` copies into the profile and commits with the provenance in the message; the
destination is explicit (`--as`) unless the item is plainly a skill directory, because promoting writes into
the one thing every session loads. The profile is a git repo (initialised on first use), so `git log` is the
history of what the agent has been taught, and the session log records the commit beside the hash.

**Project removal, made explicit.** `ab project rm` keeps the code, the agent state, the inbox and the
backups — it only drops the registry entry — so re-adding the name silently reattached a project's memory.
Both ends now say so: `rm` prints what stays and how to delete it, `add` prints the state it reattached.
`ab project purge <name> --yes` is the only command that deletes state and inbox, and it prints the number
of unreviewed inbox items first.

**OMP's load paths are not wired, and that is reported rather than guessed.** A profile for OMP would occupy
`agent/{skills,rules,prompts,agents,commands}` read-only, which is where autolearn writes. Whether
autolearn's output directory can be pointed at the inbox could not be determined from inside the sandbox
(`~/.omp` is off limits by this project's own rules, and OMP is not public), so `profile_mounts` is empty
for OMP: breaking autolearn silently was the one outcome to avoid. The inbox, the snapshot, the provenance
and the harvest commands all work for OMP today. The question and four options are in `docs/TODO.md`, and
the itest carries the OMP load-path cases as skips that name the reason.

**Follow-ups the same day (29 Oct).** Four small ones, from reading the result:

- **`rsync` is in the agent images now.** agentbox's own test suite shells out to it (the pre-session grant
  snapshot is `rsync --link-dest`), so `make test` run *inside* a session could not exercise any code path
  that prepares a session: 111 of its tests errored on the missing binary. `profile.py` itself does **not**
  use rsync (it copies with `shutil`), so the profile needed no install-doc or preflight change — but the
  check turned up that rsync has always been a hard *host* requirement of `ab run`, undocumented, and that
  its absence was a `FileNotFoundError` traceback. `docs/INSTALL.md` now lists it, and `backup.py` reports
  it as a condition with the fix in it.
- **A lockdown inbox is ephemeral** (see above).
- **A redundant state `CLAUDE.md` is deleted, not queued.** If the injected block is taken out and what
  remains is byte-identical to the profile's own CLAUDE.md, the file holds nothing anyone added and
  queueing it would be asking the user to review their own seed. An inbox full of copies is how a review
  queue stops being read. The adapter decides (`redundant_state_copy`), because only the code that wrote
  the file knows what a copy of it looks like.
- **The session log records `dirty`** beside the hash and the commit: three different questions -- what the
  session loaded, which reviewed state that came from, and whether the two had drifted. A session that
  loaded something in nobody's history is exactly what you want to see when a skill turns out to have
  misbehaved.

**Not built, on purpose: the curator.** Reviewing an inbox item is still a person reading a skill. An agent
whose job is that review wants a project of its own (the profile read-write, a reviewed prompt, a record of
what it rejected); `docs/TODO.md` describes it as the follow-up. Harvest should be lived with first.

## 2026-10-29 (later) — OMP loads the profile; autolearn keeps its own directory

The question that held OMP's profile back is answered, from the public source Tim pointed at
(`github.com/can1357/oh-my-pi`: `packages/coding-agent/src/autolearn/managed-skills.ts`,
`discovery/builtin.ts`, `docs/skills.md`): **autolearn writes only to `<agentDir>/managed-skills`**, never
to `<agentDir>/skills`, and both directories are discovered unconditionally. That is the whole basis for the
change, so `omp.AUTOLEARN_DIR` names it in one place with the citation beside it.

- **The authored paths carry the profile, read-only:** `agent/{skills,rules,prompts,agents,commands}` from
  `~/.config/agentbox/omp/profile/`, in every preset including lockdown — the same deal Claude has. Taking
  `agent/skills` read-only no longer costs autolearn anything, because it was never writing there.
- **`agent/managed-skills` is per session.** The session gets it empty every time, from its own run dir, and
  at teardown everything in it is moved into the project's inbox as `learned-<name>` with the usual
  provenance sidecar. A skill the agent wrote itself is a *proposal*: it goes to the review queue, not into
  what every later session loads. Nothing it learned survives into the next session unpromoted, and a
  lockdown session keeps none of it (its inbox is ephemeral too). Autolearn is unaffected — it writes where
  it always wrote, and the output stops being invisible.
- **The session notes say so**, per agent rather than generically: OMP is told where its learned skills go
  and that the authored directories are read-only; Claude is told about its `#` shortcuts. That paragraph
  used to be Claude-specific text shown to OMP sessions as well, which was simply wrong — OMP reads neither
  CLAUDE.md nor a `#` shortcut (`Adapter.extra_profile_notes`).
- **Mount points under `~/.omp/agent`** are created by the launcher, like Claude's: `Adapter.state_mirrors`
  says which container paths mirror the per-project state dir, so `_own_mount_point` works for both agents
  and the end-of-session state scan does not report the runtime's own mount points as changes.

**What the builder could not verify.** Whether the *installed* OMP is recent enough to have the split.
`~/.omp` is off limits to an agentbox session by this project's own rules, and is not visible inside one;
the attempt was refused, correctly. So the five read-only mounts are wired on the strength of Tim's reading
of the source, with the check left to him (`grep -rl managed-skills ~/.omp/versions/*/`) and the one-line
revert named in `docs/TODO.md`. The risk if the installed build is older: autolearn's writes to
`agent/skills` would be refused — visible, reversible, and on a feature whose output has never appeared in
that tree (the 30 Sep snapshot in `tests/fixtures/omp/tree.txt` has no skills directory at all).

**Also, the scan learned four more "runs later" directories:** `.agents/`, `.codex/`, `.opencode/` and
`.github/skills/`. A repo can feed more than one agent, and unlike `.claude`/`.omp` these are not covered
by lockdown at all, so the report is the only thing that would mention them.

## 2026-10-29 (later still) — One live session per project

Two sessions of one project shared its agent state with no locking: Claude's `.claude.json` and settings,
OMP's `agent/`, and the inbox, all last-writer-wins. `ab run` warned and started anyway, which is a label on
a problem rather than a fix (it was `docs/TODO.md`'s "real fix" item). Now a project has one session slot.

- **The lock** is a file in the project's state dir (`<data>/<project>/session.lock`), created with
  `O_EXCL` so two `ab run`s racing cannot both win, holding the launcher's pid (with its start time, so a
  recycled pid cannot impersonate it) and the session id once there is one. `agentbox/projectlock.py` is
  file mechanics only; judging a holder needs podman, and that judgement lives in `session`.
- **Released on every exit that can run code**: `teardown()` releases by session id, and teardown is on
  every path -- normal exit, an exception in `prepare()`, SIGINT/SIGTERM/SIGHUP (`_guarded` handles all
  three), `ab session kill`, `ab session gc`. `ab run` also holds its own handle, for the window before the
  session has a name. SIGKILL cannot release anything, which is what the stale check is for.
- **Stale means its launcher is gone AND nothing of its session is running or starting.** A live launcher is
  never overruled, even with no containers left: a session in `prepare()` looks exactly like that, and this
  is the question three audits got wrong by guessing (F3, F2, F-1). So a *hung* launcher holds its project
  until `ab session kill` ends it -- the one action that can actually stop it; tearing down containers it
  does not have would free nothing. That is a deliberate departure from the brief, which said "pid dead or
  session containers gone".
- **An unreadable podman frees nothing.** `sessions()` raises, and the run stops with that error rather
  than stealing a lock it cannot justify.
- **A backstop for a missing lock:** a running session of the project with no lock file at all -- one
  started before this existed, or whose lock was removed by hand -- also refuses, naming the session. Found
  by an existing test that kept passing when it should not have.
- **Two facts the lock itest established on its first real run** (29 Oct): `ab run` passthrough goes to the
AGENT unless `--shell` is given, so three of these tests ran `claude -c "echo ..."` and died with a 401 on
the harness's fake token (one `run_shell` helper now, so the form cannot drift); and the **agent container
is named after the session itself** (`<session>`), with only the sidecars taking a suffix
(`<session>-proxy`). A test looking for a `-agent` name found nothing. The unit fixtures said `-agent` too,
which was harmless -- the code keys off the `agentbox.role` label, not the name -- but it encoded a false
fact, and `ab attach` does use the name podman reports, so the shared `ps_json` helpers now produce the
real shape.

**An itest for the lock** (`tests/integration/test_lock.py`) covers what the fake podman cannot: a session
started **on a pty**, so `ab run` takes its interactive path, then (a) a second run refused, (b) SIGHUP --
the closed-terminal case, an ssh drop or a killed tmux pane -- leaving no lock and no containers, with the
logs and scan still kept, and (c) SIGKILL. (c) is two steps, because that is its real shape: while the
agent is still running the next run is *refused* (SIGKILL cannot stop a container, and a live session is
never reaped out from under anyone); once the agent is gone, what remains is a husk, which the next run
reaps and reports. The signal is sent explicitly rather than by closing the pty master -- the launcher runs
in its own session, so there is no controlling terminal for the kernel to hang up.

**`ab attach <project>`** is the way back in: `podman attach` on the session's agent container, the same
  stdio the launcher has. If the launcher's own terminal is still open, both are attached to the one agent;
  that is what re-attaching to a process means, and the multiplexed version is still `ab ws` (step 7).
- **Users name projects, not sessions.** `ab attach web`, `ab session kill web` (ids still accepted),
  `ab session ls` leads with the project and shows `id <session>` at the end, and the idle warning names the
  project. The session id keeps its `-N` suffix and stays in logs, container labels and inbox provenance,
  where it identifies one run among several over time.
- **No implicit registration.** `ab run` on an unregistered name prints the `ab project add` command to run,
  with the root filled in from the repos dir when something is actually there and a placeholder when it is
  not. A project names a grant the agent gets read-write; that is a decision, not a default.

**Harness defaults, so a test cannot end without a verdict** (29 Oct). Two ways a CLI test can fail to
produce one, both of which happened here:

- **stdin.** `ab run` reads stdin when it has no TTY, so a test whose refusal stops working reads from
  whatever stdin the runner had -- a terminal, in a developer's shell -- and blocks. Every in-process `ab`
  now gets an empty, already-at-EOF stdin (`helpers.SettingsCase.ab`, which also replaced seven
  near-identical per-file helpers), and `tests/integration/harness.py`'s `ab()` passes
  `stdin=subprocess.DEVNULL` unless a test gives it `input`.
- **waiting.** A session that gets further than a test intended waits on something real -- `_wait_proxy`
  polls for `ready_timeout`, 60s -- and several of those is a suite that looks hung. Every in-process `ab`
  has a deadline (SIGALRM) and passing it is a FAIL naming the command and everything printed up to that
  point; the integration harness turns `TimeoutExpired` into the same thing instead of a traceback.

`tests/unit/test_harness.py` pins both, including the case the default prevents: with the default opted out
of and a stdin that never reaches EOF, the run ends as a failure rather than hanging. A harness guarantee
that nothing checks is one that quietly lapses.

**A correction.** When mutation runs stopped producing verdicts earlier, I put it down to the stdin block.
That was wrong in this sandbox: stdin here is already at EOF, so `read()` returns at once. The real causes
were a background job killed for low memory, and `_wait_proxy` waiting 60s per test once a mutated refusal
let the run proceed. The stdin hazard is real on a terminal and the default is still right -- but it was
not what I had just observed, and I said it was.

**Two weak tests found while doing this**, both of the same kind as the ones the itest turned up: an
assertion that matched `podman volume ls` as if it were a removal (so "teardown really ran" was vacuous --
`ids()` issues those listings itself), and the `ab run` warning test, which passed because `gc` happened to
reap the session first. Both now assert on the specific containers and networks removed.

## 4 November 2026 -- the scan reports where HEAD and the refs went

A session can end with a clean work tree, no new file and no new object, and still have changed what the next
git command in the repo does: `git switch`, `git reset --hard`, `git branch -f`, `git branch -D`. Every signal
the end-of-session scan had was a file hash or a commit newly reachable from some ref, so none of those showed
up at all. An agent whose work ends on a moved branch -- which is the normal shape of a test-writing agent --
left nothing in the report but `commits added this session: N`.

`scan.snapshot` is now version 3: refs are kept **by name** (`byname`), with HEAD's object and its symbolic
ref, alongside the flat object list version 2 wrote. The flat list stays because it is what the new-commit
search negates with, and because a session started on the older code is reported by this code at its teardown
-- such a start state has no `byname`, and the report says it cannot compare rather than calling every ref new.

`report` then names, per repo: refs created, deleted or moved (with how many commits the move added and how
many it **took off**, from `rev-list --count --left-right`), and HEAD on a different branch or detached.

**What goes in the summary.** A branch moved forward is what work looks like, and `commits added this session`
already says it happened, so a fast-forward is in the full report only. The summary gets what is surprising: a
ref deleted, a ref rewound (commits dropped), a move whose old tip is no longer in the repo, and HEAD
somewhere else. Otherwise every session that commits would carry a summary line and the summary would stop
being read.

**The base branch is the exception.** When the project names the branch its work lands on, that branch's new
commits are listed one by one -- short id and subject -- and summarised whatever it did, including a plain
fast-forward. That branch's commits are the session's product; a count says nothing about what it is. Capped
at 50 lines. A commit subject is the agent's own text, so it is held to the same rule as an added line: if
the host hook's pattern matches a subject, the id is printed and the subject is withheld.

The grep-with-the-hook's-engine call is now one helper (`_grep`), used by both the added-line scan and the
subject check, so the two cannot drift into different regex dialects.

**Not wired yet:** nothing sets `base_branch`. `report` takes it, and teardown reads it from the scan state
written at session start (so `ab session kill` and `ab session gc` name the same branch the launcher would),
but where the value comes from belongs to the agent-config work that needs it -- one line when that lands.

## 4 November 2026 -- running without the profile: `ab run --no-profile` and `profile = "off"`

Two switches, because they answer different questions. `--no-profile` is "not this run" -- reproducing
something without the user's instructions in the way, or telling an agent's own behaviour apart from a
skill's. `profile = "off"` on the project is "not this project, ever". When both say off the log records the
flag, since that is the one just typed.

**Off means off at every door.** The profile reaches a session through five of them, and skipping four would
have been worse than skipping none, because the result would have looked bare without being bare:

| door | with the profile off |
|---|---|
| the read-only profile mounts (`skills`, `agents`, `commands`, ...) | not mounted |
| items COPIED into state instead of mounted (Claude's `settings.json`, which Claude writes to) | not copied |
| the profile's own `CLAUDE.md`, folded into the generated per-session instruction file | not read |
| OMP's `~/.omp` `plugins`/`repos`/`pylibs` snapshot and the `versions/<v>/` dirs serving them | not mounted |
| the container image | the harness image, never a user layer |

Nothing is read either: no snapshot is taken, no profile git repo is created, no legacy seed is migrated and
the state-to-inbox migration is skipped. A run that does not load the profile must not be deciding what the
profile looks like for the next run that does.

**What stays.** agentbox's own session notes -- they describe the sandbox the agent is in, which is not the
user's profile and is not optional. For OMP, `agent/config.yml`, the MAGS-CodeDev wrapper and the Playwright
cache stay too: those are not extensions, and OMP does not start without them. The inbox stays (it is
agentbox's, and nothing loads it anyway) and so does autolearn's own `managed-skills` directory.

**The image.** `profile.user_image(settings, agent)` is the hook point for the planned
`agentbox-base -> agentbox-<agent> -> user layer` chain, and it returns "" today. The launcher asks it in
`session.Session.image`, and `--no-profile` returns the harness image without asking: a user layer is built
FROM the harness image and is profile content like a skill is, so "no profile" that still ran the user's own
build of the agent would be a lie. The hook exists now so the guarantee holds the moment something builds
one, rather than being remembered later.

**`profile` is not security-relevant, and that is load-bearing.** The key has an order (off is less than on)
but turning the host-owned profile on grants no capability -- it is host-owned and trusted by design, which
is why it has always loaded under lockdown. A ceiling preset checks every ordered non-preset key, so marking
it security-relevant would have made `ab project set lk --profile on` an error while an *unstored* lockdown
project resolved to "on" anyway: a rule meaning two different things depending on whether the default
happened to be written down. So `presets.ORDERED_NON_SETTINGS` now takes ordered keys that are
**security-relevant**, with the rule stated -- a ceiling bounds a project's security posture, and a key that
grants nothing has no posture to bound.

**The old omp-env base image's own container clients are not used by anything** (asked alongside this work).
`omp.PATH` puts `/usr/local/bin` ahead of `/usr/bin`, and `images/omp/Containerfile` installs the pinned,
checksummed podman-remote 5.7.0 there precisely to shadow the base image's Debian client, whose major
version does not match the host service. `required_tools` -- the list `ab selftest` checks inside a real
session -- is `bash, git, omp, bun, python, chromium, podman, podman-compose`, and the compose tool comes
from `images/requirements.txt` via pip, not from the base. So no agentbox path invokes a container client
that the base image provides, and the image restructure can drop them.

## 4 November 2026 -- the personal image layer, and why it is keyed on the harness image ID

`ab image build <agent>` builds a `Containerfile` from the profile on top of the agent's harness image
(`agentbox/userimage.py`). The decisions worth writing down are about what it is keyed on and what it is
checked against.

**The tag is keyed on the harness image ID plus the Containerfile, not on the profile hash.** Three
consequences, all wanted: adding a skill does not invalidate a built image (the profile changes constantly;
the image's inputs do not); editing the Containerfile does invalidate it; and rebuilding the harness image
invalidates it. The **ID** rather than the `:latest` tag it was built from, because that tag moves -- a tag
would make the key look stable while what is underneath changed, which is the exact failure this is for.

**A layer built on an older harness image is never run.** When the key does not match, nothing is tagged
under it, so `ab run` reports `personal layer out of date: ab image build <agent>` and runs the plain
harness image. Running it anyway was the alternative, and it means the agent CLI, the podman client and the
toolchain under a session are whatever they were the last time someone remembered to rebuild -- silently,
because the tag still resolves. A session that is one warning short of personalised is a much better
failure than one that is quietly months old.

**Four image properties are compared against the harness image:** `USER`, `ENTRYPOINT`, `CMD` and `PATH`.
They are how the launcher's contract with the image is expressed -- the wrapper runs through the entrypoint,
the agent binary is found on PATH, the container runs as the host user via USER plus `--userns=keep-id`.
`PATH` is in there because it is how "the agent binary path" is checkable from `image inspect` at all:
prepend a directory and `claude` is whatever is in it. The binary is then actually *run*: the built image is
started once, offline, and must report the agent version the harness image is pinned at and carry `git`,
`rg`, `python3` and the podman client.

**A rejected build is never tagged.** It is built under `<ref>-candidate`, checked, and only then given the
real tag. So a bad edit cannot take away the layer that was working: the failure is a message listing every
reason, and the previous image is untouched.

**`image inspect` is read as JSON, in Python**, by both this and the session's CLI-version check -- which
was a Go template (`{{index .Labels "agentbox.cli-version"}}`) until now. The template works on
`image inspect`; the point is that two readers of the same label should not reach it two different ways, and
`podman network ls` already taught this project what a per-subcommand template contract costs.
`podman image exists` answers by exit status, and 0/1 are the only answers taken: anything else raises
rather than reading as "no", so a podman that cannot answer does not look like a missing image.

**A correction, from the first host run** (`ab selftest`, 4 Nov). A profile that holds a `Containerfile`
nobody has built yet reported `personal layer out of date` on every session. "Out of date" is only true of
a layer that exists and no longer matches; for one that was never built it sends the reader looking for an
image that was never there. The two cases are told apart by the `agentbox.user-layer=<agent>` label
`ab image build` puts on what it builds (`userimage.any_built`), and the never-built message names the
`Containerfile` in the **profile** rather than in the snapshot, since that is the file to edit or delete.
The label query is what makes it per-agent: without it, an OMP layer would make Claude's unbuilt
Containerfile read as out of date.

Two things fell out of adding that query. The fake podman now honours `--filter label=` on `images`,
because a fake that answers every filter with the same list cannot tell "no layer" from "some other
agent's layer" -- the same defect class as the `network ls` template. And the image-gc test's fixture
images carried none of the labels agentbox's own query filters on, so they only matched because the fake
ignored the filter; the fixture is now what podman would really return for that query.

## 4 November 2026 -- `make omp-live` has to say why it failed

The same host run had `make omp-live` fail with one line: `FAIL 1. OMP answered AB_OK through the proxy`.
Three completely different things produce that line -- the model was never reached, the model answered
slowly and the run was cut off, or OMP answered something other than what was asked -- and the verdict
distinguished none of them. We learned the truth (stdout was empty; two model calls took 67s and 80s) by
reading the raw sections by eye. A live check that cannot say why it failed makes the next person re-run it
to learn what the last run already knew.

So the verdict now names the case (`stdout was empty (exit 0, 147s)` / `no AB_OK line (…, 3 line(s))` /
`did not finish in time (900s)`), and an INFO line reports how many model requests went through the proxy
and how many bytes, with a pointer when none did (the model is down, or its host is not allowed) or when
it was slow enough to be the suspect.

**And a timeout no longer costs the whole report.** It used to raise out of `main`, so the first attempt
died on step 1 and never ran the key-leak scan or the teardown check -- the two checks you most want after
a run that left something behind. It is still a FAIL naming the command (the harness guarantee from 29
Oct), but one the rest of the run survives.

## 4 November 2026 -- a layer is checked against its own base, not against a list

The first real `ab image build claude` was **rejected**: "missing from the image: rg". `rg` is not in that
harness image either -- the Claude image is still `FROM localhost/claudecode`, and ripgrep arrives with
`agentbox-base`, which is not under it yet. So the layer was being held to a standard its own base fails.
`ab image build omp` succeeded at the same time only because omp-env happens to ship ripgrep.

The defect was absolute-versus-relative. Everything else in `verify` already compares against the harness
image -- USER, ENTRYPOINT, CMD, PATH -- and the tool and version checks were the two that did not. They do
now: the harness image and the candidate are each probed once, offline, and only the **difference** is a
failure. A layer can be asked not to have broken anything; it cannot be asked to fix its base.

Three cases follow, each reported as what it is rather than as the layer's fault:

- a tool missing from both images is no finding at all;
- a tool the harness has and the layer lost is "`rg` present in `<harness>` but not in this layer";
- the two agreeing on a version that differs from the harness image's own `agentbox.cli-version` label is
  "the harness image is off its own pin; rebuild it" -- a `make <agent>-image` problem, and blaming the
  layer would send the reader to the wrong file. Likewise when the check cannot run in the harness image at
  all: then the layer simply cannot be compared, and that is what it says.

The fake podman gained a per-image `run` answer for this (`FAKE_PODMAN_RUN_OUT_BY_IMAGE`). One canned
answer for both probes makes every comparison vacuous -- a tool missing from both reads as a tool the layer
kept -- which is the same shape as answering every `--format` with one canned string.

**What `ab image build omp` proved on the way:** the happy path works end to end on real podman. The
layer installed neovim and set `EDITOR`/`VISUAL`, was checked, tagged, and is now what OMP sessions run.
`ENV` additions are deliberately not in `PINNED_CONFIG` -- only `PATH` is, because it decides which binary
`claude` or `omp` resolves to -- so setting an editor is allowed and moving the agent is not.

**What this was diagnosing, for the record.** `make itest` at the same commit runs the *real* `omp -p
--no-title 'Reply with exactly: STUB_OK'` against a stub model and asserts the exact answer, `OMP_EXIT=0`,
the model POST carrying the session key, and the session history on the host
(`test_step3_omp.test_omp_answers_through_its_model_config_with_the_key`). It passed. So agentbox at HEAD
drives `omp -p` to an exact-match answer, and the variable in omp-live is the real model server -- which
answered in 67s and 80s, took two turns for a one-line prompt, and gave three different outcomes in three
attempts. The known-broken `pi-statusline` extension is not it either: decisions.md has it broken upstream
since 15 Oct, and omp-live passed on 16 Oct with it already broken.


## 4 November 2026 -- the agent images build their own CLI, on agentbox-base

The restructure is finished: `node:26-bookworm-slim` -> `agentbox-base` -> `agentbox-{claude,omp}` ->
optionally a personal layer. Both agent images used to be `FROM localhost/{claudecode,omp-env}`, built
outside this repo, which meant a session ran whatever CLI those images happened to hold -- 56 and 59 days
old at the last check, and in OMP's case a version its own Containerfile did not ask for.

**The OS base is `node:26-bookworm-slim`, and node lives in the base rather than per agent.** Both agents
need node: Claude Code 2.1.x declares `engines.node >= 22`, and OMP is installed with npm. A pinned node
tarball in two layers is exactly the duplication the base exists to remove, and the official node image is
debian bookworm-slim with node already on it, maintained and checksummed by the node team -- so there is no
node checksum of ours to keep current. It is also the node major omp-env used, so OMP runs on the node it
has always run on. (Node 22 and not the newest LTS, which the query returned as 26.13.0: changing OMP's
node major is a separate decision with its own testing, and nothing here needed it.)

**Every pinned download is decided in the Makefile**, so "what version does a session run" is one file:
`OS_BASE_DIGEST`, `PODMAN_REMOTE_VERSION`/`_SHA256`, `BUN_VERSION`/`_SHA256`, `CLAUDE_VERSION`,
`OMP_VERSION`.

**A pin is asserted, not written down.** Each agent build runs the installed binary and greps its output
for the version it pinned, and only then puts that version on the `agentbox.cli-version` label. `make`
checks the built image against its own label afterwards too. This is the direct answer to omp-env's bug:
it declared `ARG OMP_VERSION=18.1.3` *above* its `FROM`, so the ARG was out of scope by the time
`npm install -g @oh-my-pi/pi-coding-agent@${OMP_VERSION}` ran -- it asked for 18.1.3, installed npm's
latest, and nothing said a word. A pin that is only written down is not a pin.

**Bun is pinned and checksum-verified.** OMP cannot load extensions without it
(`@oh-my-pi/pi-coding-agent@18.13.3` declares `engines.bun >= 1.4.4` and no node engine at all), so it is
a runtime dependency. omp-env piped `https://bun.sh/install` into bash, which installs whatever that script
serves and verifies nothing; this takes the same binary from the release with `ADD --checksum`, which podman
checks before it is unpacked. The published `oh-my-pi` package on npm is at 0.2.0 and is **not** OMP -- the
package is `@oh-my-pi/pi-coding-agent`, which is why the earlier lookup was so confusing.

**What omp-env had that is deliberately left out:** `podman` and `docker.io` (no agentbox path invokes
either, and the base's pinned podman-remote 5.x must shadow Debian's 4.3); `nano`, `vim`, neovim, the
pi-bridge lua config and `EDITOR`/`VISUAL`/`PI_NVIM_APPNAME` -- an editor is a personal choice and belongs
in a personal layer, which is where Tim's already is; `gnupg`; omp-env's `.bashrc` and global git identity
(agentbox passes a git identity through the environment). Kept: Chromium with its full runtime library set,
`python-is-python3` and debugpy, the `/root` credential guard before `/root` is opened to the session user,
and the `/root/.omp` symlink the absolute plugin links need.

**Two new invariants in `tests/unit/test_images.py`**, both for failure modes with no symptom:

- **an ARG with no default must be passed by the build.** Unsupplied, it is silently empty -- the same
  shape as declaring one above the FROM. The test reads every post-FROM defaultless ARG out of the
  Containerfiles and requires a matching `--build-arg` in the Makefile.
- **neither layer may carry a container client of its own**, checked against the apt install step.

The pin comparison in `ab image build` moved from equality to containment, because the label holds the
version (`2.1.318`) while the binary prints its own banner (`2.1.318 (Claude Code)`, `omp/18.13.3`).
Equality there would have failed every build; the build-time grep inside the Containerfile is the real gate.

**Not verified here:** `make` is not installed in a sandbox, so the Makefile and the three Containerfiles
are unexecuted. The tests check their text; the builds are Tim's.


## 4 November 2026 -- the restructure's first real build, and what it reported

`make base-image`, `make claude-image omp-image`, `ab image build` for both agents, both selftests
(71 pass / 0 fail / 1 skip each), `make test`, `make itest`, `make claude-live` and **`make omp-live`** all
pass on the host. omp-live is the one worth noting: it had been failing, and its browser and debugpy probes
both worked this time where both had failed before. The cause was the drift the restructure removes -- the
session was running OMP 18.1.11 out of a 59-day-old base while the host's plugins had moved on. Nothing in
agentbox needed fixing for it; the pinned image did it.

**One real finding from the build:** podman printed `one or more build args were not consumed:
[PODMAN_REMOTE_SHA256 PODMAN_REMOTE_VERSION]` on `make omp-image`. `OMP_BUILD_ARGS` still carried
`$(PODMAN_REMOTE_ARGS)` after the podman client moved into the base, so the OMP layer declares no such ARG.
Harmless in itself -- but a build arg nothing consumes is indistinguishable from a pin that is not being
applied where you think it is, which is the omp-env bug wearing different clothes. So `test_images.py`
checks **both directions** now: every defaultless ARG must be passed by the Makefile, and every
`--build-arg` the Makefile passes must be an ARG the Containerfile declares. The test resolves the
Makefile's `$(VAR)` indirection itself, with a third test asserting the collector finds something so the
other two cannot pass vacuously.

(A note on mutation testing: the first attempt at mutating these two checks weakened the assertion rather
than introducing a violation -- `assertIn(arg, passed | {arg})` -- which passes on a clean tree no matter
what, because there is nothing for the check to catch. The mutations that count introduce the violation:
put `$(PODMAN_REMOTE_ARGS)` back, or rename a declared ARG. Both reddened.)

**The warnings in the test log, triaged.** `make test` and `make itest` printed about ninety lines of
`ResourceWarning: unclosed file` plus tracemalloc notes, and two `MCP server "m"/"m2" failed to connect`.

- The MCP ones are a test's own design: `test_review_1001` plants `.omp/mcp.json` and `.mcp.json` declaring
  servers whose command is `sh -c "touch MARK_...; sleep 20"`, and asserts on whether the marker file
  appears -- it is measuring whether repo-shipped MCP config *executes*. OMP correctly reports the stub is
  not a real server. Expected, and the message is OMP's.
- The ResourceWarnings were **all in test code**; product code has no leak (the two `open()` calls outside a
  `with` in `sockguard.py` hand a log to `Popen` and close it, which an AST sweep of `agentbox/` confirmed).
  Cosmetic, then -- but a test log nobody can skim is how a real finding gets missed, and two have now been
  found by reading these logs. So the 36 leaking sites became `read_file`/`read_json` helpers in
  `tests/unit/helpers.py` and `tests/integration/harness.py`, and the affected modules run clean under
  `python3 -W error::ResourceWarning`. The `open(f, "w").close()` sites are left alone: they close.


## 4 November 2026 -- `base_branch` is a project key, and agentbox does not own the value

The scan could name a base branch's new commits one by one from the start, but nothing told it which
branch, so the parameter was exercised by unit tests only. It is a project registry key now
(`ab project set <p> --base-branch mags/integration`), recorded into the scan state at session START so
`ab session kill` and `ab session gc` name the branch the session was configured with even if the registry
is edited while it runs.

**Why a registry key rather than reading MAGS's config.** MAGS-CodeDev owns `settings.base_branch` in its
own `config.template.yaml`; the value is its business. agentbox needs only to be *told* the name. Reading
MAGS's YAML to find it would couple agentbox to another tool's file format -- and to its schema changing --
for a string the user can state in a word, and would work for MAGS alone while the scan feature is
agent-agnostic. So the project names its own integration branch, whatever put it there. One value in two
places is the cost, and both places are the user's.

agentbox neither creates nor checks out the branch, and says so in the flag's help: a name that does not
exist yet simply has no new commits to report.

**Not security-relevant**, for the same reason `profile` is not: it changes what the scan REPORTS and grants
nothing, so a ceiling preset must not refuse it. The validator is strict about one thing only -- the value
reaches `git log <ref> ^<ref>`, so it must be a ref and nothing else: no leading `-` or `.`, no `..`, no
`//`, no trailing `/` or `.lock`, no spaces or shell metacharacters.


## 4 November 2026 -- the first five dead-enforcement deletions

The backlog's rule was "remove only after a real-use baseline, each with a test proving deadness first".
The baseline arrived with the image restructure: `make test`, `make itest`, both `ab selftest`s and both
live targets green on the host. So the five items with no interlocking went, each confirmed dead by search
and each resting on a test of the LIVE guard rather than on the removal looking safe.

| removed | what it was | the live guard, and its test |
|---|---|---|
| `gate.labels_of` | an inspect-then-read-labels helper | `require_dev`, pinned by `test_named_resources_must_be_the_sessions_dev_stack` |
| `gate.check_binds` | a plural wrapper over `check_bind` | `check_bind` in `check_common_libpod`, pinned by `test_libpod_refusals` (a bind on `/etc`) |
| `gate.self.refused` | a set written on every removal | `verified.discard(oid)` in `remove()` |
| `grantcheck._rel_skips` | a `mask_skip` resolver | H3: the grant scan does not honour `mask_skip` at all, pinned by `test_mask_skip_is_not_an_exemption` |
| `session.podman_access(warn)` | an unread parameter | none needed |

**The one that earned the rule.** `self.refused` was written on every removal and read nowhere, so nothing
rested on it -- and nothing proved what *did*. `tests/unit/test_gate.py:RemovalProtection` is that proof,
written before the deletion: after `remove()`, the fake still answers `inspect` with the dev labels (as
podman would for an object whose delete did not take effect), and the object is refused anyway, through
`decide` as well as through `require_dev`. Mutating `remove()` to stop discarding from `verified` reddens
five tests; before this commit it would have reddened none. Deleting the dead set without that test would
have left the real guard as untested as it was.

**Not touched, and why.** `D-E9` (the network-less `Session` path) is eight interlocked sites across
`session.py`, `runner.py` and `hardening.py`, and removing it forecloses a network-less session for good --
a design decision, not a cleanup. `D-E11` (`SYSCTL_ALLOWED = ()`) stays: it is a deliberate knob that makes
two checks always refuse, not dead code.

### The second four, the same day: D-E1, D-E5, D-E6, D-E8

These were deferred because each was *reached by a test*, which is not the same as being used. Moving each
test onto the live path first is what made them deletable, and in two cases that was the real work.

| removed | why it was dead | what moved first |
|---|---|---|
| `gate.after_start` | `decide`/the handler use `prepare_start` + `after_start_checked` | nothing: its tests had already moved to the live start path (`test_review_step5`, E3/T2) |
| `gate.check_limits(compat=)` | declared, never read | **`check_limits` had no test at all.** `test_gate.Limits` covers all three caps, including podman's `-1`-means-unlimited, which is exactly what a cap exists to stop |
| `gate.pipe_back(watch=)` | never passed: `pipe_build_reply` feeds its own watcher and hands the remainder on | nothing |
| `gate.read_chunked`'s `sock is None` | one caller, always a real socket | nothing: the live path is covered by the `spool_body` chunked tests |
| `gate.rewrite_network(compat=None)` auto-detect | `decide` always passes `compat` | the two test lines that relied on the auto-detect now pass it explicitly |
| `hardening`'s `/dev/null` mask branch | production has used `mask_file` since step 5 | the two tests that reached it drive the real mask file -- which also puts the mask source under the grant check, where `/dev/null` had returned early |

**The two that were worth more than the deletion.** `check_limits` guards the session's memory and pids
caps and had *no test of any kind*; removing an unused parameter is what surfaced that. And `hardening`'s
`/dev/null` branch `return`ed before the "is this source inside a grant?" check, so the tests that used it
were skipping that check without anyone intending to; on the real mask file they do not.

Nine of the eleven dead-enforcement items are now gone.



## 4 November 2026 -- `config.yml` becomes agentbox's own, and the Playwright cache goes

Tim: "I don't like the idea of the user's default, system OMP interfering with agentbox's." Confirming
where each harness loads from turned up that **Claude already has that property and OMP did not**. Claude's
`$HOME` is a fresh tmpfs, `CLAUDE_CONFIG_DIR` points at the per-project state dir, and the host's
`~/.claude` is never mounted (`ab selftest` asserts it is not visible). OMP read six things out of the
host's `~/.omp`, and three of those were real interference, not just coupling:

- `omp-env install` on the host rewrites `~/.omp/plugins`, so the next session silently gets different
  plugins -- which is what made `make omp-live` fail earlier the same day;
- a host OMP upgrade changes which `versions/<v>/` exist;
- **`agent/config.yml` is the host OMP's own config**, so changing the default model of the OMP Tim runs
  himself changed the default model of every agentbox session with it.

Two of the six moved today, the two that needed no new mechanism:

**`config.yml` is now `~/.config/agentbox/omp/config.yml`**, read through `USER_FILES` exactly as
`mcp.json` and `mags-codedev.yaml` always were -- so it is mounted read-only at `~/.omp/agent/config.yml`
when it exists, and absent means OMP starts on its own defaults. **No migration, deliberately:** copying
the host's file in would carry over whatever is in it (including, in principle, a key -- `config.yml` is
exactly where the session notes warn not to put one), and the point of the move is that agentbox's copy is
the only one a session sees. The session names the file and prints the `cp` for anyone who wants what they
had.

**The Playwright cache is gone.** The image carries chromium and OMP's browser tool uses
`PUPPETEER_EXECUTABLE_PATH`, so `~/.omp/.cache/ms-playwright` was a second copy nothing read. The image's
`PLAYWRIGHT_BROWSERS_PATH` went with it: it pointed into `~/.omp`, which is a **tmpfs** in a session, so
leaving it set would have aimed Playwright at an empty directory. **The gate is `make omp-live`'s browser
probe** -- it worked with the cache mounted, and whether it needed it is the one thing this change cannot
prove from a unit test. Reverting is one line in each file.

**`--no-profile` now means "nothing from the host's ~/.omp".** `bin/mags-codedev` used to survive the flag,
on the reasoning that a MAGS wrapper is not an extension. That classification was wrong: it is a wrapper
INTO `repos/mags-codedev`, which the flag does drop, so all it did was leave a broken command behind.
Gated with the plugins, the flag has a property worth stating -- and it is the property Tim asked about, so
it is tested directly (`test_no_profile.test_nothing_at_all_comes_from_the_host_omp`, with a control that
the wrapper IS there with the profile on, so the test cannot pass by nothing being mounted either way).
agentbox's own `config.yml` still arrives under the flag: `--no-profile` turns off the profile and the
plugins, not agentbox's configuration of the agent.

**`bin/mags-codedev` did NOT move out of ~/.omp**, against the plan in the previous message. It is a wrapper *into*
`repos/mags-codedev`, so it only works while `repos` is mounted; moving it alone would break MAGS use now
for no gain, since `repos` is still mounted anyway. It belongs with the plugins/repos/pylibs decision
(TODO.md 1.1). The earlier suggestion -- a pinned pip install of MAGS-CodeDev in the OMP image -- is also
wrong on its own terms: it is a private GitHub dependency, so `pip install git+...` cannot satisfy
`images/*/Containerfile`'s `--require-hashes` rule, and putting one workflow's tool into the image every
OMP session runs is the wrong trade for a parked adapter. Tim's personal layer (`ab image build omp`) is
where that belongs.

**What this leaves.** The only reason a session reads the host's `~/.omp` at all is now the
plugins/repos/pylibs snapshot plus that one wrapper. `test_omp.test_nothing_from_the_host_omp_but_the_plugin_snapshot_and_the_mags_wrapper`
pins it: every mount source under the host home must be `versions/...` or `bin/mags-codedev`, so the next
thing to creep back in fails a test rather than a review.

**Confirmed on the host, 5 November.** `make itest` ran 186 tests, OK -- including the four whose assertions
this change inverted -- and `make omp-live` passed with the browser probe working, which is the gate the
Playwright drop needed: **the cache was not load-bearing.** One caveat worth writing down rather than
forgetting: `make omp-live` does not rebuild the image, so the probe proves the *cache mount* is unnecessary,
while removing `PLAYWRIGHT_BROWSERS_PATH` from `images/omp/Containerfile` is only exercised after
`make omp-image` (and then `ab image build omp`, since the layer is keyed on the harness image ID). An env
var pointing into a tmpfs that nothing populates is the weaker half of the change, but it is unproven until
that rebuild.

**An expected warning in the session log, so nobody chases it.** OMP logs
`Ignoring unreadable user MCP config for server lists ... ~/.omp/agent/mcp.json: JSON Parse error: Unexpected
EOF` on every start where no `mcp.json` was seeded. That is `CODE_FILES` working: agentbox mounts the
zero-byte `omp-empty` read-only over `agent/{.mcp.json,mcp.json,settings.json}` so a repo (or the host) cannot
hand OMP MCP servers to launch. "Ignoring" is the intended outcome; the warning is the cost. Mounting a file
containing `{}` would silence it, and is deliberately not done: zero bytes is the unambiguous "this is masked"
marker the covers tests read (`test -s` on the placeholder, `SECRETS_BYTES=0`), and trading that signal for a
quieter log is the wrong way round.

## 5 November 2026 -- `ab session gc` sweeps personal image layers and profile snapshots

TODO.md's gc backlog item, now closed. `gc_images` filters on `agentbox.role=dev` plus the settings labels, so it could see neither
of the two leftovers agentbox now produces *routinely*:

- **personal image layers.** Every harness rebuild and every `Containerfile` edit is a new
  `localhost/agentbox-<agent>-user:<key>`, and the old tag stays in storage. Today's
  `make omp-image && ab image build omp` made two in a row.
- **profile snapshots.** `<data>/profiles/<agent>/<hash>/` gains a directory per distinct profile content,
  so one per skill edit. Text, but unbounded.

**The rules are not "delete what is old".** Two of the three exist to stop exactly that:

1. **The current layer is never removed, however old it is.** Age is not staleness here: a layer built in
   September that still matches today's harness image ID and `Containerfile` is the one `ab run` uses. The
   key is recomputed from the same `profile.user_image_ref` the session resolves through, so there is one
   definition of "current", not two.
2. **A superseded layer stays for the keep window.** The documented rollback is `podman tag` onto the
   previous layer (4 Nov), and a sweep that took it the moment a rebuild superseded it would remove the
   thing being rolled back to. `image_keep_days` is therefore the rollback window too, and its comment in
   `config.py` now says so -- one knob for everything gc keeps.
3. **`rmi` is never forced**, as in `gc_images`: a layer a container still uses, or one carrying a rollback
   tag of its own, fails to remove, and is reported and kept rather than forced.

**Unknown state is never read as "sweep it".** Without a current key *every* layer looks superseded, which
is the fail-open shape the third audit spent a round closing. So no current key means **nothing is swept
for that agent**: no `Containerfile` in the profile, `podman image inspect` of the harness image failing,
or an inspect doc with no `Id`. A deleted `Containerfile` and an unreadable profile directory are not
distinguishable from here, and `podman rmi` by hand always is.

**Snapshots are not swept at all while any session is alive.** A snapshot is a live session's read-only
mount source, and `session.json` does not record which hash a session took -- `_write_meta` deliberately
runs *before* the profile is loaded, so that a session still in `prepare()` is visible as live. So "which
snapshots are in use" cannot be answered, and the standing lesson is to refuse an unanswerable question
rather than guess at it. `sessions()` raising on an unreadable podman keeps that fail-closed: gc removes
nothing rather than deciding the host is idle. Belt and braces, `profile.snapshot` now touches a snapshot's
mtime when it hands one out again, so the mtime means **last used** rather than first copied, and only
directories shaped like a hash (`^[0-9a-f]{16}$`) are ever removed -- a `.<hash>.<pid>.tmp` another process
is half-way through writing, and anything a person put there, are not this sweep's to touch.

**`make clean` deliberately does NOT sweep user layers**, against the "consider" that backlog item carried. `make clean`
removes test leftovers by the `agentbox-test=1` label; a personal layer is Tim's own build, and a target
whose job is "tidy up after the tests" must not delete it.

**One list of agents.** `adapters.NAMES` replaces four hand-written `("claude", "omp")` tuples in `cli.py`
and the sweep iterates it, because "for each agent" was already asked in five places and an agent missing
from one of them is a silent no-op -- a sweep that skips an agent rather than an error. A test pins the list
against the adapters and against the CLI's own `--agent` choices.

**A mutation harness can lie, and this one did.** Two of nine mutations reported GREEN ("no test catches
this"), and one of those was false: the harness wrote same-size mutations into `agentbox/session.py` within
the same mtime second, so CPython reused the `__pycache__` entry compiled from the *previous* mutation and
the subprocess ran code nobody had written. `(mtime_seconds, size)` is the whole staleness check, and
`None` -> `""` keeps the size identical. The fix is one line -- drop `agentbox/__pycache__` between
mutations, and run them with `PYTHONDONTWRITEBYTECODE=1`. Worth recording because a false GREEN sends you
to write a test that already exists, and the same bug in the other direction (a false RED) would certify a
test that proves nothing. The *other* GREEN was real: the "harness image is gone" test went through the
exception handler, so the `if harness_id else None` branch beside it had no test at all until one was added.

## 5 November 2026 -- the `--format` contract check runs under `make itest`

`tests/manual/podman_formats.sh` was the only thing that could check this class for real -- a `--format`
template is a per-subcommand contract with podman's internals, `index .Labels` being fine on
`image inspect` and refused on `network ls` -- and it ran when somebody remembered. That is how the
rejected `network ls` template lived in the code for a whole build.

It now runs inside `make itest` (`tests/integration/test_podman_formats.py`). The change that makes it
worth running is not the wiring: run by hand the script inspected **whatever the host happened to have**,
so on an idle machine every inspect check SKIPped and it asserted almost nothing. The script now takes
`AB_FMT_NET` / `AB_FMT_CTR` / `AB_FMT_IMG`, and the itest creates an `abt-<run>` network, starts a
container **on** that network (so `{{json .NetworkSettings.Networks}}` is checked against a populated
value, which is what agentbox reads it for) and points the image checks at `busybox:1.36`. Every check
therefore has a target, and the itest asserts `skip=0`: under `itest` a skip means a target it was handed
could not be inspected, not that the host was idle.

**With a control, because `returncode == 0` is also what a gutted script returns.**
`tests/integration/fmtrefuser.sh` is a stand-in podman that refuses every `--format`; the second test
runs the script against it and requires a non-zero exit and the "podman rejected:" report. The unit suite
pins the other direction: every `--format` literal in `agentbox/` must appear in the script, the script
may only issue read-only subcommands (Tim runs it on a host with live services), and now also that the
itest exists and passes all three `AB_FMT_*` variables -- deleting that one file would quietly make the
script manual again.

Still read-only by construction: the script lists and inspects, and the objects it is pointed at are the
test's own, labelled and removed by the harness.

## 5 November 2026 -- D-E9: there is one kind of session

Tim: "Remove it." The backlog carried this as a **decision, not a cleanup**, because removing it forecloses
a network-less session for good, and because it was eight interlocked sites rather than a local deletion.

What was there: `make_session` always returned `NetSession`, so the base `Session` was never instantiated
and its whole half had never run -- `network_args` returning `--network=none`, `egress = "none"`,
`start_infra` as a no-op, the `pin_args` stub, `add_network_cases` -> `runner.network_none_cases`'s "only
loopback" check, the two `isinstance(self, NetSession)` tests, and `hardening.audit`'s `--network=none`
allowance.

**The argument for removing it rather than keeping it as an option.** Unreachable code is not a feature,
it is a claim. That path had never executed, so a session with no network at all would not have worked if
anyone had asked for it -- and they would have found that out at the worst moment. Nothing in the deferred
list wants it: `analysis-offline` runs `locked` egress **through the proxy**, which is a different thing.
Rebuilding it deliberately, with tests, costs less than carrying eight untested sites through every
refactor of `session.py`.

**It is now one class.** `NetSession`'s body moved into `Session`, the base's five overridden stubs went,
`SQUID_READY` moved above the class, and the merged `teardown` calls the module-level `teardown` directly
(there is no `super()` left to call). `make_session` stays, since every caller goes through it, but its
docstring now says it is not a choice of class.

**One change here is a tightening worth naming.** `hardening.audit` accepted `--network=none` on the agent
container, for the base session that had no network. It is refused now: the agent container's network must
be one of the session's. `--network=none` is *tighter* than a session network, so this is not a security
gain -- it is an invariant. The session network is where the proxy and the gate live, and an agent
container that arrived with any other network value was built by a route nobody intended. Three spellings
are tested (`none`, the egress network, a plain `bridge`), and the mutation that restores the old escape
goes red.

`agentbox/selftest/runner.py` loses `network_none_cases`; the remaining `network_cases` already asserts
"only loopback and the session network". Two tests were pointed at the one class (`test_projectlock`
patched `NetSession.run_captured`, `test_failopen` subclassed it) and `test_egress.NetSessionTest` became
`SessionNetworkTest` -- renames, not weakenings.

1075 unit tests, 0 failures, 0 errors, 0 ResourceWarnings; three mutations red (the `--network=none`
escape restored, `egress` back to `"none"`, the dev tier forced off).

## 5 November 2026 -- `ab profile install`: the format, and the four questions answered

Tim: "Build it. I have no need for ~/.omp and won't miss anything. I have already moved ~/.omp so we can
start testing cleanly." (Moving it breaks nothing: `provisioned_targets` skips links that are not there, so
a session warns and runs without extensions.) This entry records the answers; the first commit is the file
format and its validator.

**1. The installed tree lives in `~/.config/agentbox/omp/plugins/`**, beside `config.yml` and `mcp.json` --
one place, which is what Tim asked for when `config.yml` moved. Written only by `ab profile install`,
mounted read-only in sessions. The alternative was a content-keyed directory under `<data>/profiles/`,
which would have given staleness detection for free; a **lockfile** beside the tree gives the same thing
more legibly, because it can also record the OMP version the plugins were built against. That is the drift
this whole item exists to end: plugins installed against the host's omp-env OMP, meeting the OMP the image
pins (`pi-statusline`'s `stripTerminalSequences`, 4 Nov).

**2. The install gets one hardened container behind the session proxy**, with `packages` egress -- not a
synthesised project. It reuses the audited pieces (`net_create_args`, `proxy_run_args`, `squid_conf`,
`hardening.fixed_flags`) with only the install tree mounted read-write, no grants, no podman socket.
Synthesising a project to satisfy the session machinery would mean a lock, a scan and a backup for
something that is not a project.

**3. Pinning, revised against the real manifest.** The plan said "refuse anything unpinned". Tim's
`omp-env/manifest.json` has five npm entries at exact versions and four git repos at `ref: main`, four of
which are his own. So: **npm must be exact** (a range or `latest` resolves to different code on different
days, and the manifest has the number), **git may name a branch**, and the **lockfile records the commit it
resolved to**, with `ab profile install --locked` reproducing that tree. A branch in the input plus a
commit in the lockfile is the npm/cargo bargain; refusing branches would have made the tool useless for
four of nine entries, which is how a rule gets worked around instead of followed.

**4. `omp_mounts` moves to the new tree in its own commit, after the installer works.** Most of that
whitelist (`_plain`, `_version_dir`, `provisioned_targets`, `referenced_versions`) exists *because*
`~/.omp` is a directory agentbox does not control. Owning the tree retires most of it and keeps the three
rules that still earn their place: no symlink leaving the tree, no credential files, read-only mount.

**`plugins.toml` is a validator, not a loop over a TOML file.** Every string becomes argv in a container
with network (`npm install <name>@<version>`, `git clone <url>`, `git checkout <ref>`) and several become
directory names in a tree every OMP session mounts. So: npm names and exact versions by regex; https only,
with no credentials in the URL (a credential never enters a sandbox, and this one would sit in the profile);
refs checked against git's own rule (`..`, `@{`, a `.lock` suffix) as well as a leading `-`, which git
would read as an option; `extension_dir` and git names may not leave the tree; `node_modules` is reserved,
because that is where the npm entries land in the same tree. **An unknown key is an error, not an ignored
line** -- a dropped `verison` is an entry with no version at all, which is the exact failure this file
exists to prevent. Nine mutations red, one per rule.

**No `pylibs`.** omp-env produced one, but the manifest names no python dependency, so the installer does
not invent a source for it. If a plugin needs python libraries that becomes a `[[pylib]]` section with
pinned, hashed requirements -- the rule `images/*/Containerfile` already enforces for pip.

**And the manifest settled the MAGS question.** `mags-codedev` is in `gitRepos` with
`extensionDir: mags-codedev-extension`: MAGS **ships an OMP extension**, so "part of OMP" is not a
convenience, it is how the thing is built. It is a git entry in the template, and dropping that entry is
how it stays out of sessions. One correction to what I said on 4 Nov: I called it a *private* GitHub
dependency. The manifest shows a plain public `https://github.com/TimothyStephens/MAGS-CodeDev.git`. The
conclusion stands on different ground -- one workflow's tool does not belong in the image every OMP session
runs, and a `ref: main` install baked into an image is unreproducible -- but "private" was wrong.

## 5 November 2026 (later) -- `plugins.toml` is the profile's format, and agentbox was wrong about it

Tim: "should it not go in `~/repos/agentbox-profile-omp/plugins.toml`?" -- and that file **already exists,
with a schema**, which is not the one I had just built. Two corrections, the second the substantive one.

**Where it lives.** The profile repo owns it; `~/.config/agentbox/omp/profile/plugins.toml` is the
deployed copy. "Copy the example into the live profile" was the wrong instruction: the live profile is
deployed from the repo, so an edit there would be overwritten, and the version-controlled declaration is
the point ("what the agent was taught has a history").

**The schema.** I had modelled `plugins.toml` on omp-env's `manifest.json` -- `[[npm]]` entries and
`[[git]]` clones -- because that is what the host had. The profile declares **OMP marketplace plugins**:

```toml
[[plugin]]
id = "egm@egm"
marketplace = "egm"                       # must equal the part of `id` after "@"
source = "google/elephant-goldfish-model" # as `/marketplace add` takes it
ref = "464db031e7dd1b9a79803ac8ea45f55c7c20c5bc"
```

That is a different mechanism, not a different spelling: a marketplace catalog
(`.omp-plugin/marketplace.json`, or `.claude-plugin/marketplace.json` as the Claude-compatible fallback)
versus an npm install or a git clone. The lesson is the plain one: **the profile is the spec for what the
profile declares.** agentbox validates and installs; it does not get to choose the format. omp-env's
manifest was the previous generation's input, and reading it as the requirement produced a format nobody
had asked for.

**All three arrival paths, because the profile asked for exactly that.** Its own comment says an OMP
extension can arrive as a marketplace plugin, as "an npm package declaring `omp.extensions`", or through
`omp plugin link` of a local checkout -- that "only the first is expressible in this file today", and that
adding the others "is an `ab` change, not a profile change". So `plugins.toml` now has three sections:
`[[plugin]]` (the profile's, unchanged), `[[npm]]` (name + exact version) and `[[repo]]` (url + sha +
`extension_dir`, linked with `omp plugin link`). The five npm plugins and four git repos in omp-env's
manifest are expressible again, and so is MAGS -- as a `[[repo]]`, which is how omp-env had it
(`extensionDir: mags-codedev-extension`).

**`ref` is a 40-character sha everywhere, which reverses my own revision from an hour earlier.** I had
decided git entries could name a branch, with the lockfile recording the commit, on the grounds that four
of Tim's repos are tracked on `main`. The profile's file already answered this, with the better reason:
*"Never a branch or tag: marketplace catalogs are mutable and an unpinned install is not reproducible."*
That reason does not stop at marketplaces -- a branch would make the tree a session mounts depend on when
it happened to be installed -- so it applies to `[[repo]]` too. "A sha is a nuisance to look up" is a job
for `ab profile install --resolve`, not a looser file. The three `ref`-not-a-sha mutations go red, and
an abbreviated sha is refused as well: it is ambiguous by design.

Also pinned, because they are OMP's rules and the error should name the entry rather than arrive from the
installer: `id` must be `<name>@<marketplace>` and agree with `marketplace` (one name said twice; a
mismatch installs something other than what is declared); names are ASCII letters, digits, hyphens and
dots, starting and ending with a letter or digit, at most 64 characters, the whole `id` at most 128.

1109 unit tests, 0 failures, 0 errors, 0 ResourceWarnings; 14 mutations red, one per rule.

**Still needed before the installer can be written: OMP's non-interactive install surface.** The profile
describes `/marketplace add <source>` -- a slash command *inside* OMP -- and `omp plugin link` for a local
checkout. What the installer needs is the command-line form of "add this marketplace at this commit, then
install this plugin id", and guessing it would mean writing an installer that runs the wrong commands.
`omp plugin --help` and `omp marketplace --help` from inside a session, or the vendored
`reference/omp/marketplace.md`, settle it.

## 5 November 2026 (later still) -- the eight entries, and why three npm pins moved

Tim's list: `pi-statusline`, `pi-context-prune`, `pi-lens`, `pi-rules`, `mags-codedev`,
`pi-session-stats`, `pi-content-audit`, and "I dont think pi-nvim-bridge is needed". With the profile's
existing EGM entry that is **eight**: one marketplace plugin, four npm packages, three checkouts.
`docs/examples/plugins.toml` was the worked file handed to Tim; it went once he had copied the entries
into the profile repo (5 Nov), and the eight entries now live inline in `tests/unit/test_plugins.py` so
the real-world file is still parsed by a test -- "which entries" is a decision, and a silent
re-appearance should fail a test rather than a session.

**The three repo refs were resolved here**, `git ls-remote <url> main` from the sandbox (egress `packages`
reaches GitHub, and git honours the proxy variables), so they are real shas rather than placeholders.
Resolving a ref is a read; it needs no credential and changes nothing.

**Three npm pins moved forward, deliberately, and this is the judgement call to overrule if you disagree.**
omp-env pinned `@narumitw/pi-statusline 0.38.0`, `pi-context-prune 1.2.0`, `pi-lens 3.8.73`; the registry's
current versions are `0.50.4`, `2.2.0`, `4.4.9` (`pi-rules 0.1.1` is unchanged). The usual rule is "pin
what you tested", which would mean omp-env's numbers -- but those were installed against OMP **18.1.11**
while the image pins **18.13.3**, and the one failure actually observed from that drift was
`pi-statusline`'s `stripTerminalSequences` (4 Nov). An old plugin against a newer OMP is the combination
known to be broken here, so the old pins are the ones with evidence against them. omp-env's numbers are in
the file's comments; going back is one edit each, and `make omp-live` is what settles it.

**Two entries are deliberately absent, both named in the file so their absence is a statement:**
`@isac322/pi-codegraph` (omp-env had 0.3.1, current 0.5.2) was not in Tim's list, and `pi-nvim-bridge` is
not wanted. Each is one entry to add back.

**What this does not settle.** Whether `omp plugin install` takes an npm package name as a target the way
`omp plugin link` takes a path, and whether the marketplace add can be pinned to a commit at all. The
`plugin --help` output says `TARGETS   Packages, paths, or plugin names`, which is suggestive of exactly
the three kinds -- but suggestive is not a specification, and `make omp-plugin-probe` is what answers it.

## 5 November 2026 -- what the plugin probe found: the marketplace cannot be pinned

`make omp-plugin-probe`, against the image's own OMP (18.13.3, matching the Makefile pin). Four findings,
the first of which changes the installer's design.

**1. `omp plugin marketplace add` cannot be pinned to a commit.** All three spellings were refused:

```
$ omp plugin marketplace add google/elephant-goldfish-model@464db0...
✘ Unrecognized source format. Did you mean './google/elephant-goldfish-model@464db0...' (local)
  or 'owner/repo' (GitHub)?
$ omp plugin marketplace add google/elephant-goldfish-model --ref 464db0...
error: Unknown option '--ref'.
```

So the profile's `ref` is something **agentbox** has to honour, not something it can pass through. The
refusal names the way: a **local path is a valid marketplace source**. So the installer clones the catalog
itself, checks out the declared sha, and runs `omp plugin marketplace add <path to that clone>`. Pinning
becomes agentbox's job, which is the right place for it -- the profile declares a sha and agentbox
guarantees it, instead of hoping a `git clone` inside OMP lands on the same commit. (The alternative,
adding it unpinned and then `git checkout` inside OMP's cache, is worse on two counts: it depends on
OMP's cache layout, and that clone is **shallow** -- `.git/shallow` is in the tree -- so the declared sha
may not even be present.)

**2. The install tree is `~/.omp/plugins/`, with a sibling registry file.** From `plugin list --json` and
`plugin doctor --json`:

```
~/.omp/plugins/cache/marketplaces/<name>/      the catalog, as a shallow git clone
~/.omp/plugins/cache/plugins/<id>___<version>/  the installed plugin   (egm___egm___0.1.0)
~/.omp/plugins/node_modules/                    doctor: "Found"
~/.omp/plugins/package.json                     doctor: "Not created yet"
~/.omp/marketplaces.json                        which marketplaces are configured
```

That is the tree agentbox owns and mounts read-only, and it is **two mounts, not one**: the directory and
`marketplaces.json` beside it. A session's `~/.omp` is a tmpfs, so nothing persists there by accident --
which is also why the probe could install for real without touching the host.

**3. `plugin list --json` tracks two kinds, `npm` and `marketplace`**, which lines up with the three
arrival paths (`link` presumably reports as one of the two). `doctor` naming `node_modules` and
`package.json` says the npm kind installs into the same tree.

**4. `--json` is not honoured everywhere.** `plugin install --json` printed `✔ Installed egm from egm
(0.1.0)` -- human text, not JSON -- while `install --dry-run --json`, `list --json` and `doctor --json`
all gave JSON. So the installer reads the **state** (`plugin list --json`) to decide what happened, rather
than parsing what the install printed. Also: `omp plugin <action> --help` prints the same generic help for
every action, so there is no per-action documentation to lean on; behaviour comes from running it.

**Still open, hence a second probe round:** whether `omp plugin install <pkg>@<version>` takes an npm
package, what `omp plugin link <path>` reports and where it puts things, whether the pinned-local-clone
marketplace route actually installs, and -- the one that decides whether this design works at all --
**whether OMP tolerates a read-only `~/.omp/plugins`**. `doctor` says `package.json` is "Not created yet",
which is OMP announcing an intention to write there.

**An unrelated thing the probe turned up, worth checking before it surprises someone.** `node --version`
in the session printed **v22.23.4**, where the image is built on `node:26-bookworm-slim` and
`ab image build claude` reported node 26.13.0 on the same host. [likely] `PATH` puts `/root/.bun/bin`
ahead of `/usr/local/bin` and Bun ships a `node` shim that reports the Node version it emulates. If so,
plugins in an OMP session run under Bun-as-node-22 and not the image's node 26, which would make the
`NODE_MAJOR` move invisible where it matters most. Round 2 asks `which -a node` and friends.

## 5 November 2026 -- probe round 2: the installer's shape, and a node that was not what the tag said

Five answers, two of which change what gets built. (Round 1 is the entry above.)

**1. `node` in a session is REAL node 22, not a Bun shim -- so the image is wrong.** `which -a node` gives
one answer, `/usr/local/bin/node`, it is not a symlink, `/root/.bun/bin` holds only `bun`, and
`/usr/local/bin/node --version` is **v22.23.4**. So my "[likely] Bun's node shim" guess was wrong: the OMP
image genuinely carries node 22, while `NODE_MAJOR := 26`, `OS_BASE_TAG` says `node:26-bookworm-slim`, and
`ab image build claude` reported 26.13.0 on the same host.

[likely] the cause is the **digest overriding the tag**: `OS_BASE` is `$(OS_BASE_TAG)@$(OS_BASE_DIGEST)`,
and podman resolves the digest and ignores the tag, so a digest looked up while `NODE_MAJOR` was still 22
builds node 22 under a name that says 26. `make base-image` *printed* `node --version` and nothing compared
it, which is the same class as omp-env's never-applied `ARG OMP_VERSION`: **a printed version is not a
check.** Fixed where it belongs, in the build: `ARG NODE_MAJOR` after the FROM, the toolchain step now
refuses unless `node --version` matches it (and refuses an empty `NODE_MAJOR`, so an unpassed arg cannot
read as "fine"), the error names the re-pin commands, and the major goes on the image as
`agentbox.node-major` so a stale image can be told apart. Five mutations red. The host needs
`make base-image` (with a digest for node 26, or none) and then both agent images; the build will now
refuse the mismatch instead of shipping it.

**2. The npm kind works, and it is Bun that installs it.** `omp plugin install pi-rules@0.1.1` landed in
`~/.omp/plugins/node_modules/pi-rules`, wrote `~/.omp/plugins/package.json`
(`{"name":"omp-plugins","private":true,"dependencies":{"pi-rules":"0.1.1"}}`) and a **`bun.lock`** -- which
is why Bun is in the image. `plugin list --json` then reports the package with its `manifest.extensions`
and `manifest.skills`. Note the size: the dependency closure it pulled includes OMP's own
`@earendil-works/pi-*`, `@aws-sdk/*`, `@google/genai` and the rest, so the installed tree is large, not a
handful of files.

**3. `omp plugin link <path>` reports as an npm plugin.** Linking the pi-session-stats checkout gave
`path: ~/.omp/plugins/node_modules/pi-session-stats` and it appears in `plugin list --json` under `npm`.
So a `[[repo]]` entry is: clone at the declared sha, then `omp plugin link <clone>` -- and the clone has to
live **inside the tree agentbox owns**, because that path is recorded and followed later.

**4. The pinned-marketplace route works, and the catalog is COPIED rather than cloned.**
`omp plugin marketplace add /tmp/ab-probe-egm` was accepted, `marketplaces.json` recorded
`"sourceType": "local", "sourceUri": "/tmp/ab-probe-egm"`, and `omp plugin install egm@egm` installed from
it. The cache directory is not a git repository at all (`git -C ... log` says "not a git repository"), so
for a local source OMP copies the catalog -- meaning **the commit agentbox checked out is exactly what is
in the cache**, which is the property the whole pinning design needed.

**5. The consequence that shapes the installer: install AT THE PATH A SESSION WILL SEE.** The tree records
absolute paths all over -- `installPath`, `catalogPath`, `sourceUri`, the `node_modules` entries -- so the
install has to happen with the tree mounted where a session mounts it (`~/.omp/plugins`), not at some
host path that is correct only on the host. That settles a question the design had open: the install
container mounts the host-owned tree **at `/home/<user>/.omp/plugins`**, and the catalog clones live inside
that tree so their recorded `sourceUri` is valid in a session too.

**What is still not settled, and the chmod test could not settle it.** Whether OMP tolerates a read-only
`~/.omp/plugins`. `chmod -R a-w` then `plugin list`/`doctor` succeeded, but that proves little: the
session's uid may bypass the mode bits, and the `find` that was meant to show writes used
`marketplaces.json` as its reference point -- which the step-4 install had just rewritten, so the files it
listed (`installed_plugins.json`, `omp-plugins.lock.json`, the `cache/plugins/...` tree) were from the
install, not from step 5. **A badly chosen reference point produced output that looked like an answer.**
The real test is an actual read-only bind mount, and it belongs in the installer's itest -- where it is
also worth knowing that `installed_plugins.json` and `omp-plugins.lock.json` exist, because those two are
state OMP writes *inside* the tree and may need a writable overlay even when the rest is read-only.

## 5 November 2026 -- `ab profile install`: one container, the tree where a session sees it

The installer, built on what the two probe rounds established. `~/.omp` stops being an input the moment
`omp_mounts` moves onto this tree (next commit); this one makes the tree.

**It is not a session, and it does not pretend to be one.** The profile is not a grant, so there is no
project, no lock, no scan, no backup, no gate and no sockguard -- just one hardened container. What it
*does* share is the part that matters: `session.net_create_argv` and `session.proxy_run_argv` are now
module-level and used by both, so the sealed pair of networks (`isolate=strict`, `--internal`) and the
proxy's posture have **one definition**. A copy of those two argv shapes in the installer would have been
a copy of the network isolation, which is exactly the kind of drift that is invisible until it matters.

**`packages` egress with no model-host rule.** `egress.squid_conf` now takes an empty `model_host` and
leaves the `localmodel` acl and its allow line out. The single private address a session may reach is an
exception made *for* the local model server; an install fetches packages and has no business reaching a
model API, and the honest way to say that is to omit the rule rather than point it at something harmless.
Ordering is tested as well as absence: `deny private` must still be the first rule, so dropping an allow
cannot leave a gap above it.

**The tree is mounted at `~/.omp/plugins`, which is where a SESSION mounts it.** This is the probe finding
that shaped everything: OMP records absolute paths (`installPath`, `catalogPath`, `sourceUri`, the
`node_modules` entries), so an install at a host path would write paths that resolve to nothing in a
session -- and look perfectly fine from the host. One mount, read-write, and nothing else; no socket, no
grant, no volume.

**Pinning is agentbox's job, in three lines per catalog.** `omp plugin marketplace add` cannot take a
commit, so the script clones the source, checks out the declared sha, **verifies it**
(`git rev-parse HEAD | grep -Fxq <sha>`) and only then adds that path. The verify line is what makes the
lockfile's claim true rather than hopeful: a clone that resolved something else would otherwise pass
silently. Clones live inside the tree (`<tree>/.agentbox/sources/<name>`) so their recorded paths are
valid in a session too, and the whole script is `set -eu` with every interpolated value shell-quoted --
belt and braces over `plugins.py`'s refusals, which is why a `ref` cannot become a git option.

**What happened is read from state, not from output.** `plugin install --json` prints human text, so the
script ends with `omp plugin list --json` into `<tree>/.agentbox/state.json`, and the lockfile is built
from that. An install that reported nothing is an error, not an empty lockfile: the tree cannot be trusted
if nothing recorded what is in it.

**The tree is replaced, not updated.** OMP's `package.json`, `bun.lock` and `installed_plugins.json` are
cumulative, so installing into a tree that already holds plugins would leave the removed ones installed
and make the lockfile a description of something else. An install means "make the tree be what the file
says". A symlink in place of the tree is refused rather than followed, because this function deletes.

**The lockfile (`<tree>/.agentbox/install.json`) lives with the tree**, because it describes the tree: the
declaration, what OMP reported, the profile hash, and the image it was built against -- id,
`agentbox.cli-version` and `agentbox.node-major`. That last part is the whole point of the exercise: the
drift this item exists to end was plugins installed against one OMP and loaded by another, and a tree can
now say which OMP and which node it was built for.

**Teardown on every path**, in a `finally`: an install leaves no container, no proxy and no networks. It is
named and labelled like everything else agentbox makes (`<prefix>install-<agent>`, project label
`(install)`, which no registry name can collide with), so `make clean` and the leftover checks see it.

Ten mutations red, including the four that matter: the sha checkout removed, the marketplace added by
shorthand instead of the pinned clone, the tree mounted at a host path, and the hardening audit skipped.
One of them was GREEN on the first attempt because the mutation was badly built -- `if False else
hardening.audit(...)` still calls audit -- which is the same lesson as before: **mutate the subject, and
check that the mutation is actually a mutation.**

**A fake-podman fix came out of this.** `run` consumed stdin unconditionally, where real podman only does
for `--interactive`. The installer's container is not interactive, so one unit test blocked on a pipe
nobody was writing to for its full 120-second timeout and then failed with a timeout message instead of
its actual assertion. The fake now matches podman.

**Not yet done, deliberately, and each its own commit:** sessions mounting this tree (`omp_mounts`), the
staleness warning when the lockfile's image does not match the one a session runs, and the itest -- which
is where the one open question gets answered: **does OMP tolerate a read-only `~/.omp/plugins`?** The
chmod stand-in proved nothing (decisions.md, round 2), and `installed_plugins.json` and
`omp-plugins.lock.json` are state OMP writes inside the tree, so a writable overlay for those two is the
fallback if it does not.

## 5 November 2026 -- the first real install: `~/.omp` has to be the container user's own tmpfs

`ab profile install omp` on the host, first run:

```
✘ Failed to add marketplace: Error: EACCES: permission denied, open '/home/vmuser/.omp/marketplaces.json.tmp'
```

My bug, and an obvious one in hindsight. The installer mounted the tree at `~/.omp/plugins` and gave
`$HOME` a tmpfs -- so **podman created `~/.omp` itself**, as the mount point for the tree beneath it,
owned by root inside that tmpfs. The container runs as the host uid under `--userns=keep-id`, so OMP's
first write outside the tree failed. The OMP *adapter* has always done this correctly for sessions
(`SessionSetup(mount_root=C_OMP, tmpfs=[C_OMP])`); the installer is the second place that needs it and did
not have it.

Fixed by giving `~/.omp` its own tmpfs with `mode=0700,U` -- `U` chowning it to the container user -- and
adding it to the `tmpfs_dirs` the audit allows. That is also the right shape for what it holds: everything
OMP writes outside the tree (`marketplaces.json`, `agent.db`, logs, sessions) lands there and goes with
the container, and the one piece that has to survive is copied into the tree by the script. The `.tmp`
suffix in the error is worth keeping in mind too -- it confirms OMP writes that file by rename, which is
why it is copied out at the end rather than bind-mounted. Two mutations red (the tmpfs dropped; the tmpfs
not chowned).

**The other thing the run showed, which is not a bug:** the dry run reported "1 plugin", not the eight
entries. The installer reads `settings.profile_src(agent)` -- the **deployed** profile at
`~/.config/agentbox/omp/profile/plugins.toml` -- and Tim had copied the entries into the profile *repo*,
which is the source the deployed copy comes from. So the installer was right and the file it read was the
old one. Worth writing down because it is the same distinction as everywhere else in the profile
mechanism: the repo is what you edit and review, the deployed profile is what a session (and now an
install) actually reads.

## 6 November 2026 -- `~/.omp` stops being an input

The commit the whole `ab profile install` item was for. A session's plugins now come from the tree
agentbox builds, mounted read-only, and **no code path reads a host OMP install at all.**

**Two mounts replace a whitelist.** `plugin_mounts` returns `<config>/omp/plugins` at `~/.omp/plugins` and
that tree's `.agentbox/marketplaces.json` at `~/.omp/marketplaces.json`, both read-only. What went with
the old one: `PROVISIONED`, `provisioned_targets`, `referenced_versions`, `_version_of`, `_version_dir`,
`_plain`, `check_no_credentials`, `CREDENTIAL_FILES`, `URL_CREDENTIALS`, `VERSION_REF`, `READONLY_FILES`
and the mid-start `unchanged()` recheck -- about 160 lines, and **every one of them existed because
`~/.omp` was a directory agentbox did not control**: a link that had to resolve to
`versions/<v>/<name>` and nothing else, a `pylibs -> .` that would have mounted the whole of `~/.omp`
(keys included), a version directory swapped while a session was starting, a credential file with a second
hard link, a repo with credentials in a remote URL. agentbox builds this tree and replaces it whole, so
the questions do not arise. The deleted tests are the honest measure of that, and the replacement tests
say so where a reader will find them.

**`pylibs` is gone, and so is `PYTHONPATH`.** It was an omp-env directory of python libraries for plugins;
nothing declares one (`plugins.toml` has no `[[pylib]]` section, deliberately), so setting `PYTHONPATH`
into a path that does not exist would be a leftover pretending to be a feature. It comes back as a pinned,
hashed section if a plugin ever needs it.

**`bin/mags-codedev` is gone with it.** MAGS arrives as a `[[repo]]` entry now -- an OMP extension, which
is what it is -- so the wrapper into `repos/mags-codedev` has nothing to wrap. It was already dead in
practice: Tim moved `~/.omp` aside on 5 Nov, so sessions had been getting nothing there for a day. If the
`mags-codedev` **CLI** turns out to be wanted inside a session, the checkout is in the tree
(`.agentbox/sources/mags-codedev`) and the answer is a small feature, not a resurrection of this one.

**A removed setting reports instead of refusing.** `omp_home` is out of `DEFAULTS` and into a new
`config.RETIRED` map, so an `agentbox.toml` that still sets it prints what replaced it and carries on.
Refusing to start would punish exactly the person who was using the feature we removed; ignoring the line
silently would leave them believing it still does something. The behaviour is tested both ways -- a
retired key loads and warns, an unknown key still raises, because that is a typo and not a retirement.

**Staleness, in three questions a session can answer.** `installer.staleness` reports: a tree with no
readable lockfile; a tree built against a different agent CLI version or node major than the image this
session runs; and a `plugins.toml` that has changed since the install. The second is the drift this whole
item existed to end. The third compares the **declaration**, entry by entry, rather than the profile hash
-- editing a skill changes the hash and has nothing to do with plugins, and a warning that cries wolf is
one nobody reads. Each question is answered only when it can be: unreadable labels produce silence, not a
guess, because this reports and decides nothing.

**`Session.image_labels()` is deliberately not cached.** It is read twice per session; a cache would save
one `podman image inspect` and cost the ability to ask twice and get the truth twice -- which is what a
test walking three image states on one session object does, and what a cache quietly breaks.

**The itests changed shape, not depth.** `tests/ompfake.py` builds an installed tree instead of a fake
`~/.omp` (node_modules, a marketplace cache, OMP's own `package.json`/`bun.lock`/`installed_plugins.json`,
and `.agentbox/`), and every itest that used to write `omp_home` into a temp `agentbox.toml` now just
builds a tree in the config dir it already owns -- there is nothing to point anywhere. Two of them needed
care rather than a sed: `test_review_1001` and `test_step3_omp` created `<cfg>/omp` with a bare
`os.makedirs` *after* the fake had already created `<cfg>/omp/plugins`, which would have been a
`FileExistsError` in `setUpModule` and a whole module erroring out. Found by reading, like the four last
time.

**And the question this feature left open is now a test.** `test_step3_omp` writes to the tree, to
`node_modules`, to `.agentbox/` and to `marketplaces.json` from inside a session and requires every one to
fail -- a real read-only bind mount, which is the only thing that can answer it (a `chmod` stand-in
cannot: a uid may bypass the mode bits). It then runs `omp plugin list --json` and `omp plugin doctor
--json` against the read-only tree and requires both to work, because `doctor` had reported
`package.json` as "Not created yet", which was OMP announcing an intention to write there. If OMP turns
out to insist, the fallback is a writable tmpfs over `installed_plugins.json` and `omp-plugins.lock.json`
-- but that is a prediction, and `make itest` is what settles it.

**`make omp-live` keeps its one unique property on purpose.** It is the only check that proves a plugin
actually loads, and it used to get the real plugins for free because `omp_home` defaulted to `~/.omp`. It
now links Tim's real installed tree into its temporary config dir with `cp -al` -- hard links, because the
tree is node_modules-sized, and the session mounts it read-only so nothing can reach the real files
through them. Without a tree it says so and carries on, rather than quietly testing OMP with no plugins.

1150 unit tests, 0 failures, 0 errors, 0 ResourceWarnings; 85/85 guard checks. Eight mutations red,
including the four that matter: the tree mounted read-write, a symlinked tree followed, `--no-profile`
ignoring the tree, and staleness never reporting a mismatch. One was GREEN first time -- the retired-setting
test asserted the map contained the key rather than that an old `agentbox.toml` still loads, which is the
assertion-shaped-hole version of the same lesson as yesterday.

## 6 November 2026 -- the gates passed, and OMP tolerates a read-only tree

`make itest` and `make omp-live` both green on the host, which settles the question that had been open
since the probe and could have forced a redesign: **OMP tolerates a read-only `~/.omp/plugins`.**
`test_step3_omp` writes to the tree, to `node_modules`, to `.agentbox/` and to `marketplaces.json` from
inside a session and requires every one to fail; `omp plugin list --json` and `omp plugin doctor --json`
then run against that read-only tree and must succeed. Both held. So `doctor` reporting `package.json` as
"Not created yet" was a statement about an absent file, not an intention to write one, and the writable
overlay for `installed_plugins.json` and `omp-plugins.lock.json` is not needed. A prediction that turned
out to be unnecessary is still worth having made: it named what would have broken and how it would have
been fixed.

`make omp-live` passing also means the real tree -- hard-linked into a temporary config dir -- mounted
read-only and OMP ran a full turn against it. **What it does not prove is that a particular plugin
loaded.** omp-live asserts the model answers, the browser tool works and the debugger attaches; nothing in
it says "EGM's commands are available". That is the one thing still taken on trust, and the honest way to
close it is an omp-live probe that asks OMP for its plugin list and requires the declared ones to be in
it. Noted rather than written, because the declaration currently has one entry and Tim has not deployed
the eight-entry file yet.

**Two promises in my own documentation were removed rather than left to rot.** `plugins.py` referred to
`ab profile install --resolve` and `--locked` as though they existed. `--locked` should never exist: with
every `ref` already a sha, **the declaration is the lock**, and re-running the install reproduces the
tree -- the lockfile the install writes is a receipt (what OMP reported, which image it was built
against), not an input. `--resolve` is worth having and is on the backlog (TODO 2.1) with its shape
written down: one line per entry, `declared -> current`, in the same hardened container, no clone needed.

## 9 November 2026 -- Claude Code plugins: the profile declares, `ab profile install claude` pins, the session declares in settings

**What failed.** `./build.sh install` in `agentbox-profile-claude` ran `ab profile install claude` and got
`plugins.toml: [[plugin]] #1: unknown key(s) name (allowed: id, marketplace, ref, source)`. The Claude
profile's `plugins.toml` (3 Nov) and agentbox's `plugins.py` (5 Nov) had been written apart: the profile
said `name = "egm@egm"` and put the GitHub shorthand in `marketplace`; agentbox's format, taken from the
OMP profile, says `id`, `marketplace` (the catalog's own name), `source`, `ref`. And `installer.py` only
knew how to install for OMP: `omp plugin ...` commands, a tree at `~/.omp/plugins`.

**How Claude Code loads a plugin, from a probe in a session** (throwaway `CLAUDE_CONFIG_DIR`s under
/tmp, Claude Code 2.1.318):

- `claude plugin marketplace add <dir>` then `claude plugin install egm@egm` writes
  `plugins/known_marketplaces.json`, `plugins/installed_plugins.json` and, in user `settings.json`,
  `extraKnownMarketplaces` (source `directory`, that path) and `enabledPlugins`.
- **`settings.json` alone does it.** A fresh config dir holding only those two keys registered the
  marketplace and installed the plugin at the first interactive start (the `-p` run needed a token to
  get that far; before login the marketplace was registered and the plugin was not).
- **A directory marketplace is read in place.** `plugin list` says `Read from: <the directory>`; no copy
  under `plugins/cache` is made on the settings-driven path (the CLI install path does copy). The
  directory's git HEAD is recorded as `gitCommitSha`. Two consecutive starts against a directory with
  all write bits removed loaded the plugin and touched nothing in it.
- **A `github` or `git` source in `extraKnownMarketplaces`, with or without a `ref`, was not registered**
  in the `-p` runs, token or no token. Whether that is "remote sources are added asynchronously and the
  one-turn run exited first" or "not honoured from user settings" is not settled and does not need to
  be: the pin has to be agentbox's either way, which is what the 5 Nov decision said for OMP.

**So the design is OMP's with less in it.** `ab profile install claude` runs the same hardened container
(sealed networks, squid with `packages` egress and no model-host rule, nothing mounted but the tree) and
its script only clones each catalog at the declared sha into `<tree>/<marketplace>`, verifies HEAD,
checks with python that the clone's `.claude-plugin/marketplace.json` names itself as declared and lists
the plugin, and writes `state.json` from the clones. No agent command runs -- there is no install step,
and `claude plugin install` would write into a config dir no session has. The tree lives at
`~/.config/agentbox/claude/plugins/`, beside OMP's. A session mounts it read-only at
`/run/agentbox/claude/marketplaces` and merges `extraKnownMarketplaces` and `enabledPlugins` into the
copied `settings.json`, generated from the same `plugins.toml` (merged into, not over, anything the
profile's own settings.json declares). Generating rather than committing the keys keeps container
paths out of the profile repo and makes `--no-profile` drop them for nothing.

**Choices made on the way:**

- `plugins.sections_for(agent)`: Claude takes `[[plugin]]` only. An `[[npm]]` or `[[repo]]` entry in a
  Claude profile is refused by name ("cannot be used by this agent"), not installed as nothing.
- **Lockdown loads no Claude plugins**, as it loads no OMP extensions (15 Oct): a plugin can carry
  hooks and MCP servers, and lockdown is "nothing runs unasked". The profile's skills still load there.
- A declared plugin whose catalog the tree lacks is named in a warning and not enabled: an
  `enabledPlugins` entry for a directory that is not there would be a startup error inside the sandbox,
  reported to nobody. Staleness (plugins.toml edited since the install, tree built against another
  Claude Code version) is reported through the same `installer.staleness` OMP uses.
- `session._check_setup` now accepts an adapter mount under the agent's config dir (`state_mount`) as
  well as under `SessionSetup.mount_root` -- the pair `_add_profile_mounts` already allowed and its
  docstring already claimed. Claude's root is `/etc/claude-code`, and only with approvals on, so
  without this a Claude adapter could mount nothing. The adapter makes the mount point itself, under
  the state dir, for the reason `_own_mount_point` gives.
- The Claude profile's `tools/check.py` now applies the installer's rules (all four keys, unknown key
  and OMP-only section are errors, charset and length, shorthand-or-https source, 40-hex ref), so
  `check` cannot pass what `ab profile install` refuses -- which is the failure this started from.

**Not verified from inside a sandbox, and the gate for this item:** one `ab profile install claude` on
the host, then a real session in which `/egm-review` is in the skill list and the session log shows the
mount. The read-only result above used removed write bits, not a bind mount; `make claude-live` is
where that is proved, as `make omp-live` proved it for OMP on 6 Nov. `${CLAUDE_PLUGIN_ROOT}` resolving
to the mounted directory for an in-place plugin is inferred (it is the only copy), not observed.
`make test`: 1150 -> 1179.

## 2026-11-09 · One project is one directory under `projects/`; a project's state knows its root

**What.** The data dir (`~/.local/share/agentbox`) used to hold project state at the top level, beside
agentbox's own directories: `<name>/<agent>/` (state, inbox, `session.lock`), `backups/<name>/` and
`built-images/<name>.json`. It is now

    projects/<name>/project.json        the identity: name, root (real path), created, previous_roots
    projects/<name>/<agent>/            state and inbox, as before
    projects/<name>/backups/            grant snapshots, and agent-state-<stamp>/ from reset-state
    projects/<name>/built-images.json   the gate's record of the project's builds
    projects/<name>/session.lock

`logs/`, `tmp/<session>/`, `ephemeral/<session>/` (keyed by session) and `profiles/<agent>/` stay at the top.
`config.py` documents the layout; `layout.py` holds the migration and the identity. `backup.snapshot`/`retain`
take the project's backups directory, `builtrec.path_for` and `projectlock.path` derive from
`config.project_dir`, and the two scans that walked the data dir skipping a hand-kept list of own names
(`projectlock.projects_with_locks`, `profile.all_inboxes`) read `settings.projects_on_disk()`.

**Migration.** `cli.main` calls `layout.migrate` after loading settings: each of the three old places is
renamed under `projects/<name>/` (same filesystem, atomic; a live session's bind mounts follow the inode).
A name that is not a project name, or whose destination already exists, is left and named in a warning --
guessing is how two projects' state would get merged. `trial/` (tests/trial) joined the own-directory list
so it is not mistaken for a project.

**Identity and a reused name.** `ab project rm` keeps the state so that re-adding reattaches it (29 Oct above,
"The host-owned agent profile, and the inbox": rm prints what stays, add prints what it reattached). That was right for the same project and a quiet merge for a
new project under an old name: it inherited the old memory, inbox, backups and the built-image record, which
decides what the dev tier may run -- cross-project movement, the thing the threat model is about -- and
`purge` did not remove the record either, since it lived outside `<data>/<name>/`. Nothing on disk said which
root a state directory belonged to; the registry entry was the only link and `rm` deletes it. Now
`ab project add` writes `project.json`, and on a name that already has state:

- same root: reattaches, as before, and says so;
- no identity yet (state from before this change): reattaches and writes one -- `add` is the only thing
  that knows the root;
- a different root: refused, naming both roots and the three answers. `--fresh` archives every agent's state
  and the built-image record to `backups/agent-state-<stamp>/` (grant backups stay; they are of the old
  root, and retention ages them out), then writes a new identity. `--reattach` keeps everything and records
  the old root in `previous_roots`. `ab project purge <name> --yes` deletes the directory whole.

`ab init` runs the same check before it writes a tree. A session start compares the registry's root with
the identity (`layout.check_session`) and refuses a mismatch, which only a hand-edited `projects.toml` can
produce; a missing identity is written from the registry there.

**Why now.** Asked where sessions' files go, the listing of the data dir showed twelve project directories
mixed with six of agentbox's own, and reading `cmd_project_purge` against `builtrec.path_for` showed the
record it left behind.

**Not changed.** Session names (`ab-<agent>-<project>-<n>`) and the kept-log names. `ab project set` still
cannot move `root` (rm + add, which now runs the identity check). `make test`: 1179 -> 1203, with
`tests/unit/test_layout.py` covering the layout, the migration (including the collision and the
not-a-name cases, and that `ab` migrates before any command), the identity (same root, different root
refused with nothing saved, `--fresh`, `--reattach`, both flags, legacy state, `ab init`, the session-start
check both ways), purge, and `remove_everywhere`. Not verified here: a real migration of the host's data
dir -- the sandbox has none -- so the first `ab` on the host after this is the gate; it prints what it moved.

## 2026-11-10 · The `mask` setting: a project's own masks, on top of the secret names

**What.** A per-project list, `mask`, set with `--mask PATH` on `ab project add`/`set`/`ab init`
(`--clear-mask` empties it). Two forms:

- a **name** (`data`, `state.json`) masks every file or directory of that name anywhere in the project's
  grants, the way `.env` is masked -- the walk is the secret masks' own (`mask_skip` folders not searched,
  symlinks not followed);
- a **path** (`./data`, `deploy/state`) masks exactly that path under the root. The leading `./` is what says
  "root only", so it is kept, not normalised away.

A directory becomes an empty **read-only** tmpfs (`secrets/` is a writable one; the spec's §4 asks for
EROFS on create, and nothing should be writable into a cover that is dropped at teardown). A file gets the
empty mask file. A path that does not exist is created as an empty directory and masked, so a mask added
before the data appears is not a silent no-op (spec §4). The masks are added to the plan after the secret
masks and the swap: one mount per destination, the later wins, so a masked `.env` is masked and not
substituted. Nested masks collapse to the outer one through the existing N2 rule. A grant root itself is
never masked. Absolute paths, `..`, and the characters a mount option cannot carry are refused by the
validator at store time (`projectkeys._mask_paths`).

**What a mask means elsewhere, all through the existing mechanisms:** `mounts.hidden` includes it, so
`grantcheck` treats it as unreadable -- a compose data bind to it, a database under it and the foreign-uid
warning for it all go; `mounts.covered` includes it, so the gate refuses a spawned container's bind that
reaches it (spec §6.2, the bypass the measurement found) and a build context must carry it empty; the
pre-session backup excludes it with the other covers; and `_write_scan_state` adds every tmpfs cover to the
end-of-session scan's skip list, since a host service writing into a live data directory during the session
would otherwise be reported as the session's change. The history-secrets check is unchanged: it keys on the
secret names.

**Why now.** `ab project add` on life-updates was refused for `data/` (a compose data bind, owned by a
container uid) with no answer short of acknowledging the exposure or moving the application's data layout.
`docs/agentbox-masked-paths.md` had measured exactly this case on 5 Oct and `TODO.md` said the named-volume
default would serve "until a real project forces it". The user asked for both ends -- extra mounts and extra
masks -- so a project of any layout can be made to fit; `--data-ro`/`--data-rw` were already the mount end.

**Why `mask` and not `masked_paths`.** The flag reads as the others do (`--mask data` beside `--mask-skip`,
`--swap`), and the key names the list of what is masked. The spec said its names were not prescriptive.

**Not built.** `ab init --data-bind` with the user-named `--data-backup` (the template half of the 6 Oct
contract); the dev-swap for directories (`dev/data` shown at `data`), which the user's question also
touched -- a directory bind over a cover is a different mechanism and nothing asked for it yet; the spec's
in-session errno probe as an itest. `make test`: 1203 -> 1234.

## 2026-11-10 · Two nouns: `ab project` and `ab session` hold every verb; `ab screen` is the workspace

**What.** The command tree is now

    ab project init <template> <name> [settings]     was: ab init <template> --name <name>
    ab project add | set | show | check | ls | rm | reset-state | purge | accept-*
    ab project selftest <agent> <name> [--json]       was: ab selftest --agent A --project P
    ab session start <agent> <project> [--shell] [--no-tty] [--no-profile] [--timeout] [-- ...]
                                                      was: ab run [--agent A --project P | A P]
    ab session attach <project | session id>          was: ab attach <project>
    ab session ls | preview | kill | gc
    ab screen start <builder> [reviewer] [--layout L] [--agent A]   was: contrib/ab-ws
    ab screen attach | ls | kill <project>; layouts; config
    ab profile | image | keys                         unchanged

Every session verb takes a project name or a session id through the same resolution (`attach` and
`preview` took one or the other). Every required agent argument is a positional with the two choices
(`session start`, `project selftest`, `keys rotate`, `image build`); the optional agent filter on the
`profile` verbs stays a flag, because there it narrows a listing rather than naming the subject.
`--agent`/`--project` flags are gone from `start`; the init name is a positional. No aliases: the launcher
is still pre-release, as the 7 Oct entry said when the `session` namespace was made.

**Why.** The 7 Oct entry kept `run` and `init` at the top level as "the hot-path verbs that create". Two
things changed that reading. `attach` arrived on 29 Oct at the top level and is not a creator verb, so the
rule was already broken; and once there is a `session` noun, "start one" is its most natural verb, with
`add`/`rm` for the record and `start`/`kill` for the process saying something true about each noun. `init`
is project creation and nothing else, so it sits beside `add` as the second way in. `selftest` spins up a
throwaway session to prove one project's boundaries, so it is a check of the project's configuration,
beside `check`. `add` was considered for sessions and rejected: a session is a foreground process, and
`ab session add claude web` would block the terminal until the agent exits.

**`ab screen`.** `contrib/ab-ws` became `agentbox/screen.py`, and a noun with `ab session`'s verbs: a
workspace is one tmux session per project, `ab-<project>`, so `start`, `attach`, `ls` and `kill` name a
project like the session verbs do, and `layouts` and `config` replace the script's `--list` and
`--print-config`. `start` looks both projects up in the registry and refuses an unregistered one with the
`ab project add` message before tmux is touched (the script opened the workspace and let the agent pane
fail); every pane opens in the project's registered root rather than the current directory, so the old
`reviewer_dir` config key is ignored; a project that already has a live session gets an `ab session attach`
pane instead of a `start` that would be refused; a workspace already open is refused with the attach and
kill pointers. `ls` joins the tmux list with the launcher's own liveness, so it shows each workspace's
project and its live session id. The same layouts are read from `<config dir>/screen.toml` (the old
`ab-ws.toml` when the new name is absent, so AB_CONFIG_DIR moves it with the rest). The agent comes from
`--agent` (default claude) or a pane's own `agent` key. The script had been typing `ab run <project>` with
no agent, which `run` has refused since the positional form landed, so it was already stale. tmux is
checked for at run time and named in the refusal; `--dry-run` prints the tmux commands and the tmux query
is one function, which is how the tests cover it without tmux. The design doc's `ab ws` (step 7) is this;
`screen` is the name chosen.

**Churn.** One regex sweep over the launcher, its docs and its tests (`ab run` -> `ab session start`,
`ab attach` -> `ab session attach`, `ab init X --name Y` -> `ab project init X Y`,
`ab selftest --agent A --project P` -> `ab project selftest A P`, `ab-ws`/`ab ws` -> `ab screen`), the
usage generator's command tree, the Makefile's probe target, the integration and live tests' argv, and
the two profile repos' instruction and README mentions of `ab run --no-profile`. Older entries above keep
their spellings; they mean the forms here. `docs/USAGE.md` regenerated.

## 2026-11-10 · `--help` lists everyday commands first and keeps the rest under `advanced`

**What.** Every `--help` now lists its commands in the order a user meets them, not the order the code
grew in, and splits them in two. The listed block holds the everyday ones; an `advanced ... (rarely
needed; --help on each)` section underneath holds the rest, which still parse exactly as before:

    ab               project, session, screen        | advanced: profile, image, keys
    ab project       init, add, ls, show, set, rm    | advanced: check, selftest, accept-grant-exposure,
                                                       accept-history-secrets, reset-state, purge
    ab session       start, attach, ls, kill         | advanced: preview, gc
    ab screen        start, attach, ls, kill         | advanced: layouts, config

argparse lists subcommands in insertion order, so the everyday order is the parser's construction order
(`cli.parser`). The advanced ones are taken out of argparse's listed block after they are added
(`demote`, which edits the subparsers action's own list of shown choices -- setting `help=SUPPRESS`
after the fact prints the sentinel literally) and written into the epilog (`finish`). `docs/gen_usage.py`
follows the same order, so `USAGE.md` reads the same way. `tests/unit/test_cli.HelpIsOrderedForPeople`
pins the order, the split, and that every advanced command still runs.

**Why.** Asked for by the user: alphabetical or accidental order makes a new user read `accept-grant-exposure`
before `add`, and the host-setup nouns (`profile`, `image`, `keys`) are done once per machine, not per day.

## 2026-11-10 · Toulmin integration (gate B) and clone mode, on branch toulmin-gate-b

**What.** `docs/agentbox_toulmin_integration.md` fixed the interface; this builds its section 1 against the
stub `tm` in `tests/toulmin/tm-stub.sh`. New module `agentbox/toulmin.py` (start, token, session mounts, MCP
entry, must-not-mount, mount list and hash, confirm, serve, end). Two project settings: `toulmin` (bool,
posture: cleared by a preset change, False tightest so lockdown refuses it) and `mount` (`checkout` |
`clone`, no looseness order). `session.clone_root` makes the clone and the branch; `mounts.remap` turns the
clone-walked plan into the agent's view at the owner's path; `_write_meta` carries the Toulmin record and
the clone so `kill` and `gc` can send `end` without the launcher; `_guarded` names the exit reason;
`teardown` takes `exit_reason` and sends `end` after the scan, before the lock. `AGENTBOX_AGENT` joins
`AGENTBOX_SESSION`. Adapters gain `mcp_server`/`mcp_remove`. `ab project clean` removes old clones. The
contract's deviations and the artifacts are in `docs/toulmin/artifacts-stub-2026-11-10.md`.

**Decisions on the way.**
- The grant scan runs over the OWNER's tree in clone mode too, so the acknowledgement recorded at `add`
  still matches; the clone can only hold less (nothing untracked or ignored).
- Clone mode with the dev podman tier is refused rather than half-built: the gate resolves bind sources
  on the host, where the owner's path is the checkout, and the grant volume, cover ids, foreign-uid check
  and unmasked-secret walk would all have to translate into the clone. Fail closed, named in TODO.md.
- The serve child's pid file is in the session run dir, not `services/<session>/`: that directory is
  removed early in teardown and `end` must run (after the scan) before `serve` is stopped.
- No pre-session snapshot in clone mode: the owner's checkout is not mounted and the clone is kept.
- The mount list is derived from the audited `podman run` argv itself (every `--mount`/`--tmpfs`), not from
  a second computation, so the hash is over what the container is created with.
- **Audit rules unchanged.** `hardening.py` is untouched. What changed is the allowlist fed to it: the clone
  directory joins the allowed bind sources (the root bind's source in clone mode), and the three session
  mounts (socket directory, bridge, token file) join it too; destinations needed nothing, `/run/agentbox`
  was already allowed. The socket-directory mount passes the existing socket rule because the rule looks at
  the source itself, which is a directory, not the socket in it -- which is the contract's own ask.
- The Claude MCP entry goes into the per-project `.claude.json` (user scope) and is removed at teardown;
  `claude --mcp-config` exists at the pinned 2.1.318 (probed in this sandbox) and would have done as the
  fallback.

**Not verified in this sandbox (no podman):** the agent's in-container view and the `podman inspect` grep;
`tests/integration/test_toulmin.py` produces them on the host. The OMP subprocess-environment probe is
`make omp-env-probe`. `make itest` has not run for this branch.

## 2026-11-11 · `mounts.hidden`: a dev-swap in a read-only grant hides its target too

**What.** `mounts.hidden` counted a dev-swap bind as hiding the real file behind it only when the bind was
writable (`not m.ro`, since 3f96aa8). A `data_ro` grant with `.env` and `.env.dev` binds the stand-in
read-only, so the grant scan reported the real `.env` as an unmasked secret although the agent sees the
stand-in. The clause meant to keep the read-only covers (`.git/hooks`, `.git/config`, compose files) out of
the hidden set, but every one of them binds a path onto itself, which the `src != dst` test already
excludes. The clause is gone. `test_mounts ... test_a_dev_swap_in_a_read_only_grant_hides_its_target`.

**Why.** Found while building reviewer projects (the `review-of` branch, not merged: Tim chose a separate
clone instead). It only ever produced warnings, because `data_ro` findings never refuse, which is likely why
it went unnoticed. **Audit rule change:** the grant scan treats a read-only swap as covering its target.

## 2026-11-11 · Reviewer projects are separate clones, synced on the host (`--reviews`, `ab project sync`)

**What.** A project key `reviews = "<builder>"` and `agentbox/review.py`. `ab project add <name> --reviews
<builder> --preset P` clones the builder's repository next to its root (`<parent of builder root>/<name>`)
unless a root is given (the root positional is optional only with `--reviews`), puts `/reviews/` in the
clone's `.git/info/exclude`, and registers it; if registration then fails, the clone it made is removed. An
existing directory is reused only if it is a clone whose `origin` is the builder's root. `ab project sync
<reviewer | builder> [--branch B]` (an everyday verb): refuse tracked changes in the clone; `git fetch
--prune origin`; `git checkout --detach origin/<builder's current branch>`; copy the reviewer's
`reviews/*.md` to the builder's `.review/` and note when `.review/` is not ignored there. Allowed while
either session is live. `review.check` at add/set/sync: builder registered, not itself a reviewer, this
project nobody's builder, root's origin is the builder's root. `ab project rm` refuses a builder with
reviewers and prints the clone's path for a removed reviewer (rm never deletes files). Session notes tell a
reviewer what it is, that it cannot fetch, and where reviews go. `reviews` is not security-relevant: it
grants nothing in a session.

**Why.** Tim (11 Nov) decided against the read-only shared-root design (`review_of`, branch `review-of`,
not merged) in favour of "a review project having a separate repo which periodically syncs across", with
reviews handed to the builder as files, and asked for the sync to be part of `ab` rather than a script.
A separate clone needs no grant-rule exception: the two projects overlap nothing. A shared hand-over
directory was not an option: the builder reading what the reviewer writes is the overlap F3 refuses, so the
host copies instead. Placing clones in agentbox's data dir was not an option either (a protected path no
grant may enter), and /tmp loses reviews at reboot, so the clone sits next to the builder. Fetch plus a
detached checkout rather than `pull`: the reviewer never commits code, so nothing can conflict.

**Host-side safety (new surface: the host now runs git in, and copies between, two agent-written trees).**
git: `core.hooksPath=/dev/null`, `core.fsmonitor=false`, no submodule recursion, `origin` re-confirmed
before each fetch. Copy: directory fds opened `O_NOFOLLOW|O_DIRECTORY`, files opened `O_NOFOLLOW` relative to
them, `S_ISREG` and a 1 MB cap checked on the open fd, destination opened `O_NOFOLLOW` too; nothing deleted.
Each is mutation-checked in tests/unit/test_review.py (the one surviving mutant, dropping `--detach`, is
equivalent: checking out a remote-tracking ref detaches anyway).
