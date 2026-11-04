# Runtime: where the session binaries come from

`PERSONALIZATION.md` covers an agent's config, state and plugins. This covers the **executables** `ab run`
puts in a session — the agent CLI and the tools around it: which image they live in, how they are pinned, and
how to upgrade them. All of it is read from `images/*/Containerfile`, the `Makefile`, and the adapters; no
executable is host-mounted (only config/state are — see `PERSONALIZATION.md`).

## The image chain

`node:22-bookworm-slim` → **`agentbox-base`** → **`agentbox-{claude,omp}`** → optionally **your own layer**.
`ab run` uses the agent image's `:latest` tag, or your layer when one is built for that exact image
(`ab image build <agent>`; see `PERSONALIZATION.md`).

| Image | Built by | On |
|---|---|---|
| `localhost/agentbox-base:latest` | `make base-image` → `images/base/Containerfile` | `docker.io/library/node:22-bookworm-slim`, digest-pinnable with `OS_BASE_DIGEST` |
| `localhost/agentbox-claude:latest` | `make claude-image` → `images/claude/Containerfile` | `agentbox-base` |
| `localhost/agentbox-omp:latest` | `make omp-image` → `images/omp/Containerfile` | `agentbox-base` |
| `localhost/agentbox-<agent>-user:<key>` | `ab image build <agent>` → `<profile>/Containerfile` | the agent image above |

Each `make …-image` also tags `:<date>` for history. The agent builds are `--pull=never`, so they layer on
the **local** base and never fetch one; they fail loudly if `agentbox-base` is missing and warn when it is
older than `BASE_WARN_DAYS` (default 14).

Until 4 Nov 2026 the agent images were `FROM localhost/{claudecode,omp-env}` — bases built outside this
repo — so a session ran whatever CLI those happened to hold (56 and 59 days old when it was last checked,
and in OMP's case a version its own Containerfile had not asked for). They install the CLI themselves now.
Node lives in the base rather than in each layer: both agents need it (Claude Code declares
`engines.node >= 22`, OMP installs with npm), and taking it from the official node image means there is no
node checksum of ours to keep current.

## Where each executable lives

| Executable | In-session path | Origin | Pinned where |
|---|---|---|---|
| `claude` (Claude Code CLI) | npm global prefix, on PATH | `npm install -g` in `images/claude/Containerfile` | `CLAUDE_VERSION` in the `Makefile` — **2.1.318** |
| `omp` (OMP CLI) | npm global prefix, on PATH | `npm install -g @oh-my-pi/pi-coding-agent` in `images/omp/Containerfile` | `OMP_VERSION` in the `Makefile` — **18.13.3** |
| `bun` (OMP's extension runtime) | `/root/.bun/bin/bun`, first on PATH | `ADD --checksum` from the GitHub release, unzipped | `BUN_VERSION` + `BUN_SHA256` in the `Makefile` — **1.4.4** (OMP 18.13.3 declares `engines.bun >= 1.4.4`) |
| `node`, `npm` | base image PATH | `node:22-bookworm-slim` | the OS base's digest (`OS_BASE_DIGEST`) |
| `podman` (podman-remote, static) | `/usr/local/bin/podman`, first on PATH | `ADD --checksum` from GitHub in `images/base/Containerfile` | `PODMAN_REMOTE_VERSION` + `PODMAN_REMOTE_SHA256` — **5.7.0** (matches the host podman service major) |
| the compose tool | base image site-packages | `pip --require-hashes` in the base | `images/base/requirements.txt` — **1.5.0** (with `python-dotenv` 1.2.3, `pyyaml` 6.0.3) |
| `debugpy` (OMP only) | image site-packages | `pip --require-hashes` | `images/omp/requirements.txt` — **1.8.22** |
| `git`, `curl`, `jq`, `rsync`, `rg`, `fd`, `gcc`/`g++`/`make`, `python3`+venv | base PATH | `apt-get install` in the base | distro version (apt verifies its own packages) |
| `chromium` + its runtime libraries (OMP only) | `/usr/bin/chromium` | `apt-get install` in the OMP layer | distro version |

Nothing is host-mounted at runtime. The podman client talks over the gate's Unix socket to the **host's**
podman service — see `docs/design.md`, "Podman access".

## How a pin is enforced

A pin that is only written down is not a pin. omp-env declared `ARG OMP_VERSION=18.1.3` *above* its `FROM`,
so the ARG was out of scope where it was used: the build asked for 18.1.3, installed npm's latest, and said
nothing. So:

- every download is verified **before use** — `ADD --checksum` (podman checks it), `pip --require-hashes`,
  apt's own signatures, and npm's integrity metadata for an exact version;
- each agent build then **runs the installed binary and greps its output for the pinned version**, so the
  Containerfile cannot claim a version the image does not have;
- that version goes on the **`agentbox.cli-version`** label, and `make` re-checks the built image against
  its own label;
- `ab selftest` reports the label per session (`PASS  claude CLI version: 2.1.318`), and `ab image build`
  compares a personal layer against both the label and the harness image's own answer.

`tests/unit/test_images.py` holds the Containerfiles to this by reading them: no unchecksummed `ADD`, no
`curl | sh` in a `RUN`, no pip without `--require-hashes`, every ARG in scope where it is used, and every
ARG without a default actually passed by the `Makefile`.

To see what a built image carries:

```bash
podman run --rm --entrypoint "" localhost/agentbox-claude:latest claude --version
podman run --rm --entrypoint "" localhost/agentbox-omp:latest    omp   --version
podman image inspect localhost/agentbox-base:latest --format '{{index .Config.Labels "agentbox.base"}}'
```

## Upgrading

### An agent CLI (`claude` / `omp`)

Edit `CLAUDE_VERSION` or `OMP_VERSION` in the `Makefile` and rebuild that image. The build fails if the
installed binary does not report the new pin, so a typo is caught at build time rather than in a session.

```bash
make claude-image        # or: make omp-image
```

`:latest` is retagged, and `ab run` picks it up on the next session. The image ID changes, so a personal
layer built on the old one goes stale: `ab run` says `personal layer out of date` and runs the plain harness
image until `ab image build <agent>` is run again. Images agentbox built earlier are removed by
`ab session gc` after `image_keep_days`, keeping the newest per tag.

### The OS base, Bun, or the OS packages

```bash
podman pull docker.io/library/node:22-bookworm-slim
podman image inspect docker.io/library/node:22-bookworm-slim --format '{{index .RepoDigests 0}}'
make base-image OS_BASE_DIGEST=sha256:<that>   # then rebuild both agent images
make claude-image omp-image
```

Bun's version and checksum are `BUN_VERSION`/`BUN_SHA256` in the `Makefile`; the checksum comes from the
release's own `SHASUMS256.txt`.

### The podman client (shared by every image)

```bash
# in the Makefile:
#   PODMAN_REMOTE_VERSION := <new version>
#   PODMAN_REMOTE_SHA256  := <that release's shasums entry for podman-remote-static-linux_amd64.tar.gz>
make base-image          # it lives in the base now, so one rebuild serves both agents
make claude-image omp-image
```

The `ADD --checksum` step fails the build if the download does not match the SHA256, so the version and
checksum must be changed together. Keep it on the host podman service's major version.

### The compose tool / debugpy

Edit the version **and its `--hash` line** — the shared entries in `images/base/requirements.txt`, OMP's
`debugpy` in `images/omp/requirements.txt` (`pip install --require-hashes` refuses a mismatch) — then
rebuild the image that holds it (`make base-image` for the shared ones, then both agent images).
