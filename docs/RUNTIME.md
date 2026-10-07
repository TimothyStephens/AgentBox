# Runtime: where the session binaries come from

`PERSONALIZATION.md` covers an agent's config, state and plugins. This covers the **executables** `ab run`
puts in a session — the agent CLI and the tools around it: which image they live in, how they are pinned, and
how to upgrade them. All of it is read from `images/*/Containerfile`, the `Makefile`, and the adapters; no
executable is host-mounted (only config/state are — see `PERSONALIZATION.md`).

## The two session images

`ab run` runs the agent in the image named by its adapter (`agentbox/adapters/*.py`), always the `:latest`
tag:

| Agent | Session image | Built by | Base image (unchanged by agentbox) |
|---|---|---|---|
| Claude Code | `localhost/agentbox-claude:latest` | `make claude-image` → `images/claude/Containerfile` | `localhost/claudecode:latest` |
| OMP | `localhost/agentbox-omp:latest` | `make omp-image` → `images/omp/Containerfile` | `localhost/omp-env:latest` (Tim's `~/repos/omp-env`) |

Each `make …-image` also tags `:<date>` (e.g. `:20261007`) for history; `ab run` uses `:latest`. The build is
`--pull=never`, so it layers on the **local** base image — it never fetches a base from a registry. The base
images are built **outside this repo** and must already exist (README setup); agentbox does not define them.

## Where each executable lives

| Executable | In-session path | Origin | Pinned where |
|---|---|---|---|
| `claude` (Claude Code CLI, Node) | base image PATH | **baked into the base `localhost/claudecode`** | not pinned by agentbox — the base image's version |
| `omp` (OMP CLI, Bun) | base image PATH (`ENTRYPOINT`/`CMD` is `omp`) | **baked into the base `localhost/omp-env`** | not pinned by agentbox — the base image's version |
| `podman` (podman-remote, static) | `/usr/local/bin/podman` (first on PATH) | **installed by agentbox's Containerfile**, `ADD --checksum` from GitHub, then `install` | `PODMAN_REMOTE_VERSION` + `PODMAN_REMOTE_SHA256` in the `Makefile` — **5.7.0** (matches the host podman service major version) |
| `podman-compose` | image site-packages | installed by agentbox (`pip --require-hashes`) | `images/{claude,omp}/requirements.txt` — **1.5.0** (also `python-dotenv` 1.2.3, `pyyaml` 6.0.3) |
| `debugpy` (OMP only) | image site-packages | installed by agentbox (`pip --require-hashes`) | `images/omp/requirements.txt` — **1.8.22** |
| `python3`, `python3-venv`, `curl`, (OMP) `chromium` | base PATH | `apt-get install` in agentbox's Containerfile | distro version (not pinned) |

So: **the agent CLIs are image-baked in the base images; the podman client and the Python tooling agentbox
adds are pinned (checksum / pip hashes) in this repo.** Nothing is host-mounted at runtime. The podman client
talks over the gate's Unix socket to the **host's** podman 5.7.0 service (not a binary in the image) — see
`docs/design.md`, "Podman access".

To see the exact agent CLI version a built image carries (not fixed in this repo, it is the base's):

```bash
podman run --rm localhost/agentbox-claude:latest claude --version
podman run --rm localhost/agentbox-omp:latest    omp   --version
```

## Drift visibility (build-time + reporting)

The agent CLI is unpinned and lives in a base built outside this repo, and `make …-image` is
`--pull=never`, so a stale base would silently ship a stale CLI. There is no hash pin (a tampered binary is
out of scope); instead the drift is made visible:

- **`make claude-image` / `omp-image`** fail loudly if the base image is missing (they never build on
  nothing), and before building print the base's CLI version and image age, e.g.
  `>> base localhost/claudecode:latest: CLI '2.1.266 (Claude Code)', image 28 day(s) old`, with a
  `WARNING` when the base is older than `BASE_WARN_DAYS` (default 14, override on the command line).
- Each build records the base's CLI version as the **`agentbox.cli-version` label** on the adapter image.
- **`ab selftest`** surfaces it as a line (`PASS  claude CLI: 2.1.266 (Claude Code)`), or a `SKIP` telling you
  to rebuild if the image predates the label — so you always know which agent version a session ran.

## Upgrading

### The agent CLI (`claude` / `omp`)

Because the CLI lives in the base image, upgrade the base, then rebuild the agentbox image (which layers on
the local base, `--pull=never`):

```bash
# Claude Code: rebuild localhost/claudecode with the new CLI (its own build, outside this repo), then:
make claude-image

# OMP: update and rebuild Tim's base (~/repos/omp-env) with the new OMP, then:
make omp-image
```

`make …-image` retags `:latest` to the new build; `ab run` picks it up on the next session. Session images
agentbox built earlier are removed by `ab gc` after `image_keep_days` (config), keeping the newest per tag.

### The podman client (shared by both images)

Edit the pin in the `Makefile`, then rebuild both images:

```bash
# in the Makefile:
#   PODMAN_REMOTE_VERSION := <new version>
#   PODMAN_REMOTE_SHA256  := <that release's shasums entry for podman-remote-static-linux_amd64.tar.gz>
make claude-image omp-image
```

The `ADD --checksum` step fails the build if the download does not match the SHA256, so the version and
checksum must be changed together. Keep it on the host podman service's major version.

### The compose tool / debugpy

Edit the version **and its `--hash` line** in `images/claude/requirements.txt` and `images/omp/requirements.txt`
(`pip install --require-hashes` refuses a mismatch), then `make claude-image omp-image`.
