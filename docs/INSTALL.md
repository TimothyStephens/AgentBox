# Installing agentbox

How to install the `ab` launcher on this host, and exactly where everything it uses lives.

## Prerequisites

- **ubuntu-dev** (Ubuntu), rootless **podman 5.7.0** with netavark, `python3`, `git` and **`rsync`**.
  `vmuser` has no root. rsync is a hard requirement, not an optional extra: every session takes an
  `rsync --link-dest` snapshot of its read-write grants before starting (the session's only undo), so
  without it `ab run` refuses. It is also needed to run agentbox's own test suite.
- **No other image is needed.** agentbox builds its own chain — `agentbox-base` →
  `agentbox-{claude,omp}` → optionally your own layer — from `node:26-bookworm-slim` upward, and installs
  each agent CLI at a pin named in the Makefile. It no longer layers on `localhost/claudecode` or
  `localhost/omp-env`; see `docs/RUNTIME.md` for what each image contains and how the pin is enforced.

## Steps

Run these yourself; each touches your own config, `~/.local/bin`, or your user systemd units.

1. **The cgroup slice** every agentbox container runs under (one kernel-enforced memory/swap/task cap for all
   sessions together):
   ```bash
   make install-slice
   ```
   Installs `deploy/agentbox.slice` to `~/.config/systemd/user/agentbox.slice` and reloads the user daemon.

2. **The base image** — the toolchain every session shares, whichever agent runs: node 26, git, curl, jq,
   unzip, rsync, python3 + venv, ripgrep, fd, a C/C++ toolchain, the checksum-pinned podman client and the
   compose tool. Build it before the agent images; theirs are `--pull=never` and will say so if it is
   missing.
   ```bash
   podman pull docker.io/library/node:26-bookworm-slim
   podman image inspect docker.io/library/node:26-bookworm-slim --format '{{index .RepoDigests 0}}'
   make base-image OS_BASE_DIGEST=sha256:<the digest that printed>
   ```
   `make base-image` on its own works too, and warns that the OS base is not pinned: a working build, not a
   reproducible one. Pin the digest once and keep it in the Makefile — a tag moves, and the agent builds
   are `--pull=never`, so they would not see it move. The image records what it was built from:
   `podman image inspect localhost/agentbox-base:latest --format '{{index .Config.Labels "agentbox.base"}}'`.

3. **The agent images**, which install each agent CLI at the pin the Makefile names (`CLAUDE_VERSION`,
   `OMP_VERSION`, plus `BUN_VERSION` for OMP's extension runtime):
   ```bash
   make claude-image omp-image
   ```
   Produces `localhost/agentbox-claude:latest` and `localhost/agentbox-omp:latest` (each also tagged
   `:<date>`). Each build **asserts** that the installed binary reports its pin, and the version goes on the
   `agentbox.cli-version` label that `ab selftest` reports — so an image cannot ship a version its label
   denies. Fails loudly if `agentbox-base` is missing, and warns if it is older than `BASE_WARN_DAYS`.

   To move a pin, edit the Makefile variable and rebuild. Personal layers built on the old image go stale
   and say so (`ab run` prints `personal layer out of date`); `ab image build <agent>` again.

4. **The launcher on PATH:**
   ```bash
   make install                 # BINDIR=~/somewhere to change the location
   ```
   Writes `~/.local/bin/agentbox` and `~/.local/bin/ab` (a symlink to it). It refuses to overwrite an `ab`
   or `agentbox` that is not its own (ApacheBench also installs `ab`); remove such a file first or just use
   `agentbox`. It warns if another `ab` on PATH shadows its own, or is shadowed by it.

5. **Model credentials:**
   - Claude: `ab keys rotate claude` (store a `claude setup-token` token).
   - Pay-per-use providers: `ab keys set <provider>`, once each. Use dedicated, spend-capped keys.
   - A local model server that needs a key: `ab keys set llama-cpp`.

6. **OMP model list:** copy `~/.omp/agent/models.yml` to `~/.config/agentbox/omp/models.yml` with every
   `apiKey` line removed. Only providers that have a key (step 5) are offered in a session; keys are injected
   as `AB_KEY_<PROVIDER>`, never written into this file.

7. **Optional OMP MCP / MAGS config:** create `~/.config/agentbox/omp/mcp.json` or `mags-codedev.yaml`
   yourself; they are mounted read-only as written. See `docs/PERSONALIZATION.md` for the full map of what to
   edit to personalize Claude or OMP, and whether a change affects one project or all.

8. **Register a project and check it:**
   ```bash
   ab init pod --name demo            # scaffold + register a grantcheck-clean project, OR:
   ab project add <name> <root> --preset python|pods|lockdown
   ab selftest --agent claude --project <name>
   ```

## Where everything is installed

Every directory can be moved with an environment variable, so a test or a second install never touches your
real config.

| What | Default location | Override | Holds |
|---|---|---|---|
| Launcher | `~/.local/bin/agentbox`, `~/.local/bin/ab` | `BINDIR=` (make install) | the CLI and its `ab` alias |
| cgroup slice | `~/.config/systemd/user/agentbox.slice` | — | the memory/swap/task cap unit |
| Base image | `localhost/agentbox-base:latest` (+`:<date>`) | — | the shared toolchain the agent images build on |
| Agent images | `localhost/agentbox-{claude,omp}:latest` (+`:<date>`) | — | the session images (podman storage) |
| Your own layer | `localhost/agentbox-<agent>-user:<key>` | — | optional, built by `ab image build <agent>` from a `Containerfile` in your profile; the key covers the harness image and that file |
| Config | `~/.config/agentbox` | `AB_CONFIG_DIR` | `projects.toml`, `agentbox.toml`, `presets/`, `keys/`, `<agent>-oauth-token`, per-agent seeds (`claude/`, `omp/`) |
| Data | `~/.local/share/agentbox` | `AB_DATA_DIR` | per-project agent state `<project>/<agent>/`, `backups/`, `logs/` |
| Run (tmpfs) | `$XDG_RUNTIME_DIR/agentbox` | `AB_RUN_DIR` | live session metadata and generated configs under `sessions/`, podman sockets under `services/` — gone on reboot |
| Scaffold root | `~/repos` | `AB_REPOS_DIR` | where `ab init <t> --name <n>` creates `<name>/` |
| podman binary | `podman` on PATH | `AB_PODMAN` | the host podman the launcher drives |

Nothing is installed system-wide and nothing needs root. The host's own `~/.claude` and the real contents of
`~/.omp` outside the whitelisted pieces are never used in a session (see `docs/PERSONALIZATION.md`).

## Updating and uninstalling

- **Upgrade the agent CLIs or the podman/compose tooling:** see `docs/RUNTIME.md`.
- **Re-install the launcher** after a code change: `make install` again (it replaces its own files).
- **Remove leftovers from crashed test runs:** `make clean` (by label; never touches live stacks).
- **Uninstall:** delete `~/.local/bin/agentbox` and `~/.local/bin/ab`; optionally remove
  `~/.config/systemd/user/agentbox.slice` (then `systemctl --user daemon-reload`), the two
  `localhost/agentbox-*` images, and the config/data directories above.

## Tests

- `make test` — fast unit checks, no podman.
- `make itest` — real podman, `abt-` names, cleans up after itself; ends with a warning if `make claude-live`
  needs rerunning.
- `make claude-live`, `make omp-live` — run by you, with your real token, keys and `~/.omp`.
- `make verify-sockguard` — checks the pinned sockguard image's signature (needs cosign).
