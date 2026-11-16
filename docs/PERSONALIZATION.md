# Personalizing agentbox

How to give an agent your settings, plugins, commands and MCP servers, and where each change takes effect
— one project or all of them. (For the session **binaries** — the agent CLI, podman client, compose tool —
and how to upgrade them, see `docs/RUNTIME.md`.) Everything here is read from the code (`agentbox/adapters/claude.py`,
`agentbox/adapters/omp.py`, `agentbox/session.py`, `agentbox/initproj.py`, `agentbox/config.py`); the exact
host and in-session paths are given so there is no guessing.

Three roots hold agentbox's own state (each movable by env var):

- `~/.config/agentbox` (`AB_CONFIG_DIR`) — the registry, the per-agent **profiles** (`<agent>/profile/`), the
  installed **plugin trees** (`<agent>/plugins/`), OMP's seed files, keys.
- `~/.local/share/agentbox` (`AB_DATA_DIR`) — `projects/<name>/` (per-project **agent state**, inboxes, backups), logs.
- `$XDG_RUNTIME_DIR/agentbox` (`AB_RUN_DIR`) — live, per-session generated files (tmpfs).

In a session the agent runs as you (`--userns=keep-id`), so `~` inside the container is your real home path.
That makes some in-session paths *look* like host paths (`~/.omp/...`) even though their source is a mount,
not the host directory — see OMP below.

---

## 1. How Claude and OMP load config / plugins / MCP

The two adapters differ in where the agent's own writable state sits, but what either agent **loads** is
the same shape: the host-owned profile `~/.config/agentbox/<agent>/profile/` (read-only) plus the plugin
tree `ab profile install <agent>` builds from that profile's `plugins.toml` (read-only). **Claude's whole
config dir is per-project. OMP's `agent/` working dir is per-project; its plugins, `config.yml` overlay and
`mcp.json` are global, from `~/.config/agentbox/omp/`, never from a host OMP install.**

### Does your own installed agent reach into a session?

**No, for either agent.** The whole host surface a session can read is short, and worth knowing by heart:

1. `~/.config/agentbox/` — the profiles, the installed plugin trees, OMP's seed files, `keys/`,
   `<agent>-oauth-token`, `presets/`, `projects.toml`;
2. `~/.local/share/agentbox/` — `projects/<name>/` (agent state, inboxes, backups, built-image record), `logs/`, `profiles/` snapshots;
3. `$XDG_RUNTIME_DIR/agentbox/` — this session's generated files and sockets;
4. **the grants** — the project root and any `data_ro`/`data_rw`, at their real paths;
5. the agent image.

**Claude.** `$HOME` in a session is a fresh tmpfs, `CLAUDE_CONFIG_DIR` points at the per-project state dir
under (2), and the host's `~/.claude` is never mounted — `ab project selftest` asserts it is not visible.
Nothing you do to your own Claude Code install changes what a session sees.

**OMP.** The host's `~/.omp` is not read at all (5-6 Nov 2026; `omp_home` is a retired setting). Until then
a session mounted the host OMP's `plugins`/`repos`/`pylibs`, so `omp-env install` on the host silently
changed the next session's plugins (which is what made `make omp-live` fail on 4 Nov). Each of those inputs
has an agentbox-owned replacement:

- **Plugins** come from `~/.config/agentbox/omp/plugins/`, which `ab profile install omp` builds from the
  profile's `plugins.toml` against the image's own OMP (see the table below). Installing a plugin into your
  own OMP changes nothing in a session.
- **`config.yml` is agentbox's own** — `~/.config/agentbox/omp/config.yml`, beside `models.yml` and
  `mcp.json`. Your host OMP's default model is not every session's default model. There is no migration,
  by design: if you want what you had, `cp ~/.omp/agent/config.yml ~/.config/agentbox/omp/`, and the
  session says so by name when the file is absent (the OMP profile repo's `seed/` ships keyless copies that
  its `./build.sh deploy` installs when missing).
- **`.cache/ms-playwright` is gone entirely.** The image carries chromium and OMP's browser tool uses
  `PUPPETEER_EXECUTABLE_PATH`, so the host cache was a second copy nothing read. The image no longer sets
  `PLAYWRIGHT_BROWSERS_PATH` either — it pointed into `~/.omp`, which is a tmpfs in a session.

`ab session start --no-profile` (or `profile = "off"`) drops the profile mounts **and** the plugin tree for
either agent; agentbox's own `config.yml` overlay and `mcp.json` still arrive for OMP — the flag turns off
the profile, not agentbox's configuration of the agent.

### Claude Code (`adapters/claude.py`)

`CLAUDE_CONFIG_DIR` is set to `/run/agentbox/claude`, backed by a **per-project, read-write** state dir that
persists across sessions of that project. The host `~/.claude` is never used.

| What | Host source | In-session path | Scope | Mode |
|---|---|---|---|---|
| Config dir (`CLAUDE_CONFIG_DIR`) | `~/.local/share/agentbox/projects/<project>/claude/` | `/run/agentbox/claude/` | per-project, persistent | rw |
| `skills/`, `agents/`, `commands/` | `~/.config/agentbox/claude/profile/` (the **profile**) | `/run/agentbox/claude/{skills,agents,commands}` | **global** (every project, every preset) | **ro** |
| `CLAUDE.md` | the profile's own, with the sandbox notes prepended | `/run/agentbox/claude/CLAUDE.md`, generated per session | **global** | **ro** |
| `settings.json` | `~/.config/agentbox/claude/profile/settings.json` | copied into the config dir at every start (`profile.copy_into_state`), then the plugin keys below are merged in | **global** | rw copy |
| Plugins (marketplace catalogs) | `~/.config/agentbox/claude/plugins/<marketplace>/`, built by `ab profile install claude` from the profile's `plugins.toml` | `/run/agentbox/claude/marketplaces/<marketplace>`, declared to Claude Code in the copied `settings.json` (`extraKnownMarketplaces`, `enabledPlugins`) | **global**, except lockdown (none) | **ro** |
| Inbox (what a session wants to keep) | `~/.local/share/agentbox/projects/<project>/<agent>/inbox/` (under `ephemeral/<session>/` in lockdown) | `/run/agentbox/claude/inbox/` | per-project, persistent, **never loaded** (lockdown: deleted at teardown) | rw |
| Managed policy (lockdown / approvals on) | generated in `<run>/sessions/<s>/claude-managed-settings.json` | `/etc/claude-code/managed-settings.json` | per-session | ro |
| Repo config (`CLAUDE.md`, `.mcp.json`, `.claude/`) | the project repo (its real path) | same real path (the grant) | per-project (committed) | rw (ro where covered) |

- **The profile is host-owned and read-only in the session** (29 Oct). `skills/`, `agents/` and `commands/`
  are mounted from a content-hashed snapshot of `~/.config/agentbox/claude/profile/`, and `CLAUDE.md` is
  generated per session as the sandbox description followed by the profile's own text, so the environment is
  the first thing the agent reads (see §4). A session cannot change any of it: what it wants to keep goes to
  the project's `inbox/`, which nothing loads, and `ab profile harvest` reviews that queue. The hash and the
  profile's git commit are recorded in a `profile` session log, so what a session loaded is answerable
  afterwards. The pre-profile seed (`~/.config/agentbox/claude/{settings.json,CLAUDE.md,commands/}`) is
  **moved** into `profile/` on first use, with a message saying so — nothing reads the old location.
- `settings.json` is the exception: Claude Code writes it itself, so it is copied from the profile into the
  config dir at every start instead of being mounted read-only.
- **Skills and sub-agents a session writes** no longer accumulate in the per-project config dir (those paths
  are read-only mounts now). Anything already there is moved to the project's inbox once, with a message.
- **Your own image layer** (4 Nov): put a `Containerfile` in the profile, beginning `ARG BASE` then
  `FROM ${BASE}`, and `ab image build claude` builds it on the harness image — for a toolchain or CLI you
  want in every session. It is checked before it is tagged (USER/ENTRYPOINT/CMD/PATH unchanged, the pinned
  agent version, `git`/`rg`/`python3`/podman present), and a failed build leaves the one you were using in
  place. When the harness image is rebuilt, `ab session start` says `personal layer out of date` and runs the plain
  harness image until you build again. Only the `Containerfile` and `plugins.toml` affect the image's
  identity — editing a skill does not. `ab session start --no-profile` runs the harness image.
- **Turning the profile off:** `ab session start --no-profile` for one run, or `ab project set <p> --profile off` for
  good. That drops the mounts, the copied `settings.json`, the profile text in `CLAUDE.md`, the plugins and
  the image layer; agentbox's own session notes still apply.
- **Plugins** (9 Nov): declared in the profile's `plugins.toml` as `[[plugin]]` entries (`id`, `marketplace`,
  `source`, `ref`; the same format as OMP's, and Claude takes `[[plugin]]` only). `ab profile install claude`
  clones each marketplace catalog at its sha into `~/.config/agentbox/claude/plugins/<marketplace>`, in the
  same sandboxed container the OMP install uses, and checks the clone names itself as declared and lists the
  plugin. A session mounts that tree read-only at `/run/agentbox/claude/marketplaces` and merges two keys
  into the copied `settings.json`: `extraKnownMarketplaces` with a `directory` source at that path, and
  `enabledPlugins`. Claude Code registers the marketplace and enables the plugin at startup, reading it from
  the mounted directory in place (no copy is made, and a read-only directory is fine), and writes its own
  records under `plugins/` in the per-project config dir. A declared plugin whose catalog is not in the tree
  is named in a warning and not enabled; a tree that no longer matches `plugins.toml`, or was built against
  another Claude Code version, is reported the same way as OMP's. Lockdown sessions load no plugins (as OMP's
  do not, 15 Oct), and `--no-profile` loads none.
- **Slash commands:** reusable pieces that are not a plugin are slash commands in `commands/`. Put them in
  the profile (global, read-only) or in the project's `.claude/commands/`.
- **Memory shortcuts:** a `#` note aimed at the *project's* `CLAUDE.md` works as usual (it is in the grant,
  and the end-of-session scan now reports it); aimed at the *user-level* `CLAUDE.md` it fails, because that
  file is generated and read-only. The session notes say so and point at the inbox.
- **MCP servers:** per-project via the committed repo `.mcp.json`, read by Claude from the project root. There
  is no dedicated *global* Claude-MCP seed slot today (the seed ships `settings.json`/`CLAUDE.md`/`commands/`
  only); a global MCP server would have to be declared in the seed `settings.json` the Claude build reads.
- In **lockdown** (approvals on), the managed policy disables bypass mode and honours only managed hooks and
  permission rules, so a repo's own `.claude/settings.json` hooks and allow-rules do nothing.

### OMP (`adapters/omp.py`)

In a session `~/.omp` is a fresh **tmpfs** owned by you; named pieces are mounted on top. Writable state is
per-project; the plugins and the `config.yml` overlay are agentbox-owned read-only globals.

| What | Host source | In-session path | Scope | Mode |
|---|---|---|---|---|
| Plugins (the installed tree) | `~/.config/agentbox/omp/plugins/`, built by `ab profile install omp` from the profile's `plugins.toml` (`[[plugin]]` marketplace entries pinned by sha, `[[npm]]` packages at exact versions, `[[repo]]` checkouts linked) | `~/.omp/plugins`, plus the tree's `marketplaces.json` at `~/.omp/marketplaces.json` | **global** (lockdown runs OMP with `--no-extensions`, so none load) | **ro** |
| `config.yml` overlay | `~/.config/agentbox/omp/config.yml` | `~/.omp/agentbox-config.yml`, named in `PI_CONFIG_FILES` (ranks above `agent/config.yml`; the lockdown `--config` overlay ranks above it) | **global** | **ro** |
| `agent/config.yml` | per-project state: OMP writes it (its write-and-rename save failed with EBUSY while this path was a mount, 12 Nov) | `~/.omp/agent/config.yml` | per-project, persistent | rw |
| Working dir (sessions, databases) | `~/.local/share/agentbox/projects/<project>/omp/agent/` | `~/.omp/agent` | per-project, persistent | rw |
| Logs | `~/.local/share/agentbox/projects/<project>/omp/logs/` | `~/.omp/logs` | per-project, persistent | rw |
| `models.yml` | generated from the keyless seed `~/.config/agentbox/omp/models.yml` | `~/.omp/agent/models.yml` | per-session | ro |
| `AGENTS.md` | generated (see §4) | `~/.omp/agent/AGENTS.md` | per-session | ro |
| MCP (if you create it) | `~/.config/agentbox/omp/mcp.json` (`mags-codedev.yaml` is no longer read: MAGS-CodeDev dropped that path, 15 Nov) | `~/.omp/agent/.mcp.json` | **global** | ro |
| Chromium for the browser tool | the image (`/usr/bin/chromium`, `PUPPETEER_EXECUTABLE_PATH`) | — | global | — |
| Repo config (`.omp/`, `.mcp.json`) | the project repo | the grant | per-project (committed) | rw (ro in lockdown) |
| Inbox (what a session wants to keep) | `~/.local/share/agentbox/projects/<project>/omp/inbox/` | `~/.omp/agent/inbox/` | per-project, persistent, **never loaded** | rw |
| Profile (`skills`, `rules`, `prompts`, `agents`, `commands`) | `~/.config/agentbox/omp/profile/` | `~/.omp/agent/<same>` | **global** (every project, every preset) | **ro** |
| Autolearn's output | `<run>/sessions/<s>/omp-managed-skills` | `~/.omp/agent/managed-skills` | **per session**, harvested into the inbox at teardown | rw |

- The writable `agent/` dir has the read-only files above (`models.yml`, `.mcp.json`, `AGENTS.md`)
  mounted on top of it, and `agent/extensions`, `agent/hooks`, `agent/tools` are covered empty read-only
  (so one session cannot plant code the next runs). `agent/config.yml` is left writable on purpose: the
  host's values reach the session as the `PI_CONFIG_FILES` overlay instead.
- **Plugins are installed by agentbox, not mounted from a host OMP** (5-6 Nov): declare them in the
  profile's `plugins.toml`, deploy the profile, run `ab profile install omp`. The install runs in one
  hardened container behind the session proxy (`packages` egress, no model-host rule) against the image's
  own OMP, node and Bun, into a tree sessions mount **read-only** — OMP tolerates a read-only
  `~/.omp/plugins` (proved by a real read-only bind in `test_step3_omp`). The tree carries a lockfile naming
  the image it was built against; a session warns when that no longer matches, or when the tree no longer
  matches `plugins.toml`. A failed install keeps the previous tree (15 Nov).
- **OMP loads the host profile too** (29 Oct), read-only at `agent/{skills,rules,prompts,agents,commands}`
  from `~/.config/agentbox/omp/profile/`. Safe because **autolearn writes only to
  `agent/managed-skills`**, never to `agent/skills` (oh-my-pi: `autolearn/managed-skills.ts`,
  `discovery/builtin.ts`, `docs/skills.md`), and both are discovered unconditionally.
- **`agent/managed-skills` is per session.** Empty at the start, and at teardown everything in it moves to
  the project's inbox as `learned-<name>`, with provenance. So autolearn keeps working, and what it produces
  waits for `ab profile harvest` instead of becoming something every later session loads. A lockdown
  session keeps none of it.
- **Keys never enter via these files.** `models.yml` is regenerated per session from the keyless seed and
  references `AB_KEY_<PROVIDER>` variables; the `config.yml` overlay and `plugins/` are mounted read-only
  and are *not* scanned, so never put a secret in them (see the OMP note in `docs/design.md`).

### The asymmetry in one line

Claude: the config dir is per-project, but what the agent *loads* is **global and read-only** — the host
profile `~/.config/agentbox/claude/profile/` and the plugin tree built from it; a session's own additions go
to the per-project inbox, which nothing loads until you promote it. OMP: the same profile-plus-plugin-tree
shape, plus the `config.yml` overlay and `mcp.json` from `~/.config/agentbox/omp/`, all **global and
read-only**; only `agent/` (per-project working state) is the session's to write, and a per-project OMP
tweak lives in the repo.

---

## 2. The personalization layers, and how to change each

| Layer | Edit here | Affects | Persists |
|---|---|---|---|
| **A. Global scaffold template** | `agentbox/initproj.py` (code today — see §3) | every *future* `ab project init` project | — |
| **B. Global profile and seed** | `~/.config/agentbox/<agent>/profile/` (a deployed clone of the profile repo), plus OMP's `~/.config/agentbox/omp/{config.yml,models.yml,mcp.json}` | every session of that agent, all projects | mounted ro each start (`settings.json` copied) |
| **C. Plugins** | the profile's `plugins.toml`, installed by `ab profile install <agent>` into `~/.config/agentbox/<agent>/plugins/` | every session of that agent, all projects (none with `--no-profile`; lockdown loads none) | host-owned tree, ro |
| **D. Per-project repo files** | files committed in the project (`CLAUDE.md`, `AGENTS.md`, `.mcp.json`, `.claude/`, `.omp/`, `dev/fixtures/`) | that one project | in the repo |
| **E. Per-project agent state** | `~/.local/share/agentbox/projects/<project>/<agent>/` | that one project, across sessions | on disk, `ab project reset-state` wipes it |
| **F. Per-project settings** (preset, egress, grants, podman tier) | `ab project set <name> …` — the same flags `ab project init`/`ab project add` take; `--unset KEY` returns one to its preset value | that one project, from its **next** session | `projects.toml` (`ab project show` prints each value's source) |

A settings change applies to the next session, not a running one, and `ab project set` refuses while a
session for that project is live (as do `rm`, `reset-state` and the `accept-*` commands) — end it with
`ab session kill` first. Custom presets are TOML files in `~/.config/agentbox/presets/`; one under a new
name inherits `lockdown`'s values for anything it omits (see `docs/design.md`, Presets).

### (a) Add a Claude plugin / MCP to **one** project

- **MCP server:** create `.mcp.json` in the project repo (layer D) and commit it. Claude reads it from the
  project root in the session.
- **Slash command:** add it to the repo's `.claude/commands/` (layer D). The per-project config dir's
  `commands/` is shadowed by the profile's read-only mount, so a command there is not seen.
- These affect only this project. (In a `lockdown` project, repo `.claude/` hooks/rules are covered off; use
  it on a `pods`/`python` project.)

### (b) Add an OMP plugin (**global**) and an OMP MCP server (**per-project**)

- **Plugin (global):** add a `[[plugin]]` (marketplace, `ref` a 40-char sha), `[[npm]]` (exact version) or
  `[[repo]]` (git checkout at a sha) entry to the OMP profile repo's `plugins.toml`, commit, push and
  `./build.sh deploy`, then `ab profile install omp` (layer C). The tree then mounts read-only into **every**
  OMP session. Nothing you install into your own OMP reaches a session.
- **MCP server (per-project):** commit a `.mcp.json` (or `.omp/`) to the project repo (layer D); OMP reads it
  from the project root alongside `~/.omp/agent`. A *global* OMP MCP server instead goes in
  `~/.config/agentbox/omp/mcp.json` (layer B), mounted read-only at `~/.omp/agent/.mcp.json` in every OMP
  session.

### (c) Change the default `AGENTS.md` for **all future** projects

Create `~/.config/agentbox/templates/<template>/AGENTS.md` (and/or `README.md`); `ab project init` uses it verbatim,
`{name}`-substituted, for every future project of that template. Only those two doc filenames are read — see
§3. Changing an existing project's `AGENTS.md` is just editing the committed file (layer D).

---

## 3. The scaffold template and its override

The built-in scaffold (`AGENTS.md`, `.gitignore`, `.env`/`.env.dev`, `README.md`, compose, skeleton) is Python
string constants in `agentbox/initproj.py` (`_AGENTS`, `_GITIGNORE`, `_README`, …), filled with the project
name and template. The `.env`/mask/compose/data-volume **structure always comes from these constants** — it is
what keeps a fresh project grantcheck-clean, so it is not user-overridable.

**The doc override (`initproj._template_overrides`, `OVERRIDABLE`):** before writing a project, `ab project init`
reads `<config_dir>/templates/<template>/` and, for the **allowlisted doc filenames only**, uses the file
there (verbatim, `{name}`-substituted) instead of the built-in:

```
~/.config/agentbox/templates/pod/AGENTS.md
~/.config/agentbox/templates/pod/README.md
~/.config/agentbox/templates/python/AGENTS.md
~/.config/agentbox/templates/python/README.md
```

`OVERRIDABLE = ("AGENTS.md", "README.md")`. Any other file in that dir — a `.env`, `compose.yaml`,
`Containerfile`, anything — is **ignored**: the built-in wins, so a template dir can never weaken the safe
layout or slip a secret into `.env`. When the dir is absent, `ab project init` behaves exactly as before. Symlinks in
the template dir are skipped. Tests: `test_init.TemplateOverrideTest` proves an `AGENTS.md`/`README.md`
override is used and that a `.env`/`compose.yaml` override is ignored (the scaffold stays grantcheck-clean);
the allowlist is mutation-checked.

---

## 4. The generated environment preamble

The agent is *told* its environment at session start — it does not learn the boundary by hitting a `403`.
`session.sandbox_notes()` generates a preamble **per session** and injects it into what the agent reads:
for Claude it is **prepended** to `CLAUDE.md` (so it leads), and for OMP it is the generated
`~/.omp/agent/AGENTS.md` (`_omp_notes`). It states, from the live project config:

- the project root and its read-only / read-write data grants;
- that the grant holds code and the session refuses to start on an uncovered secret or live database;
- the masks and covers (`.env`/`*.pem`/`*.key`/`secrets/`, read-only `.git/hooks`/`config`, the `.env.dev`
  swap) and that they are enforced for containers the agent starts too;
- the **actual egress mode** (`locked` / `packages` / `open`) with the concrete reachable set, and that private
  and LAN addresses are always refused;
- the exact podman access for the tier (none / observe / dev), or that there is none — and, on the dev
  tier, the project's own `dev_images` patterns, or that no other local image is allowed (28 Oct).

These live facts stay **in the per-session notes, not baked into the committed scaffold**, because they come
from the project's registry entry and preset at session time, not from `ab project init`; writing them into the
committed `AGENTS.md` would duplicate the notes and go stale the moment a preset or `egress` override changes.
Two improvements from the original assessment are now in place, no change to the safe layout:

1. **The preamble leads for Claude** — `seed_state` prepends `sandbox_notes` ahead of the user's `CLAUDE.md`,
   so the environment is the first thing the agent reads.
2. **The scaffolded `AGENTS.md` points at it** — a one line in the `_AGENTS` template tells the agent that
   agentbox injects the real egress mode, data layout and limits each session and to read the "agentbox
   sandbox" section, keeping the committed file portable (it is also read outside agentbox) while making the
   authoritative, live description the per-session one.

Net: the live environment is a per-session generated preamble that now leads what the agent reads; the
committed scaffold stays generic and points at it, and no live facts are baked into it.
