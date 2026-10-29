# Personalizing agentbox

How to give an agent your settings, plugins, commands and MCP servers, and where each change takes effect
— one project or all of them. (For the session **binaries** — the agent CLI, podman client, compose tool —
and how to upgrade them, see `docs/RUNTIME.md`.) Everything here is read from the code (`agentbox/adapters/claude.py`,
`agentbox/adapters/omp.py`, `agentbox/session.py`, `agentbox/initproj.py`, `agentbox/config.py`); the exact
host and in-session paths are given so there is no guessing.

Three roots hold agentbox's own state (each movable by env var):

- `~/.config/agentbox` (`AB_CONFIG_DIR`) — the registry, per-agent **seeds**, keys.
- `~/.local/share/agentbox` (`AB_DATA_DIR`) — per-project **agent state**, backups, logs.
- `$XDG_RUNTIME_DIR/agentbox` (`AB_RUN_DIR`) — live, per-session generated files (tmpfs).

In a session the agent runs as you (`--userns=keep-id`), so `~` inside the container is your real home path.
That makes some in-session paths *look* like host paths (`~/.omp/...`) even though their source is a mount,
not the host directory — see OMP below.

---

## 1. How Claude and OMP load config / plugins / MCP

The two adapters are deliberately different. **Claude's whole config dir is per-project. OMP's plugins and
`config.yml` are a host-shared, read-only global; only its `agent/` working dir and your `config/omp` seed
are per-project.**

### Claude Code (`adapters/claude.py`)

`CLAUDE_CONFIG_DIR` is set to `/run/agentbox/claude`, backed by a **per-project, read-write** state dir that
persists across sessions of that project. The host `~/.claude` is never used.

| What | Host source | In-session path | Scope | Mode |
|---|---|---|---|---|
| Config dir (`CLAUDE_CONFIG_DIR`) | `~/.local/share/agentbox/<project>/claude/` | `/run/agentbox/claude/` | per-project, persistent | rw |
| `skills/`, `agents/`, `commands/` | `~/.config/agentbox/claude/profile/` (the **profile**) | `/run/agentbox/claude/{skills,agents,commands}` | **global** (every project, every preset) | **ro** |
| `CLAUDE.md` | the profile's own, with the sandbox notes prepended | `/run/agentbox/claude/CLAUDE.md`, generated per session | **global** | **ro** |
| `settings.json` | `~/.config/agentbox/claude/profile/settings.json` | copied into the config dir at every start | **global** | rw copy |
| Inbox (what a session wants to keep) | `~/.local/share/agentbox/<project>/<agent>/inbox/` (under `ephemeral/<session>/` in lockdown) | `/run/agentbox/claude/inbox/` | per-project, persistent, **never loaded** (lockdown: deleted at teardown) | rw |
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
- **Plugins / slash commands:** Claude has no plugin tree like OMP; its reusable pieces are slash commands in
  `commands/`. Put them in the profile (global, read-only) or in the project's `.claude/commands/`.
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
per-project; the toolchain is a host-shared read-only global.

| What | Host source | In-session path | Scope | Mode |
|---|---|---|---|---|
| Plugins | `~/.omp/versions/<v>/plugins` | `~/.omp/plugins` (+ `~/.omp/versions/<v>`) | **global, host-shared** | **ro** |
| Repos / pylibs | `~/.omp/versions/<v>/{repos,pylibs}` | `~/.omp/{repos,pylibs}` | **global, host-shared** | **ro** |
| `agent/config.yml` | `~/.omp/agent/config.yml` | `~/.omp/agent/config.yml` | **global, host-shared** | **ro** |
| `bin/mags-codedev`, `.cache/ms-playwright` | `~/.omp/...` | same | global, host-shared | ro |
| Working dir (sessions, databases) | `~/.local/share/agentbox/<project>/omp/agent/` | `~/.omp/agent` | per-project, persistent | rw |
| Logs | `~/.local/share/agentbox/<project>/omp/logs/` | `~/.omp/logs` | per-project, persistent | rw |
| `models.yml` | generated from the keyless seed `~/.config/agentbox/omp/models.yml` | `~/.omp/agent/models.yml` | per-session | ro |
| `AGENTS.md` | generated (see §4) | `~/.omp/agent/AGENTS.md` | per-session | ro |
| MCP / MAGS (if you create them) | `~/.config/agentbox/omp/mcp.json`, `mags-codedev.yaml` | `~/.omp/agent/.mcp.json`, `~/.omp/agent/mags-codedev.yaml` | **global** | ro |
| Repo config (`.omp/`, `.mcp.json`) | the project repo | the grant | per-project (committed) | rw (ro in lockdown) |
| Inbox (what a session wants to keep) | `~/.local/share/agentbox/<project>/omp/inbox/` | `~/.omp/agent/inbox/` | per-project, persistent, **never loaded** | rw |

- The writable `agent/` dir has the read-only files above (`config.yml`, `models.yml`, `.mcp.json`,
  `mags-codedev.yaml`, `AGENTS.md`) mounted on top of it, and `agent/extensions`, `agent/hooks`,
  `agent/tools` are covered empty read-only (so one session cannot plant code the next runs). Prompt dirs
  (`skills`, `rules`, `prompts`, `agents`, `commands`) stay writable for OMP's autolearn.
- **OMP has the inbox but not yet a read-only profile.** A host-owned profile would be mounted at exactly
  those five prompt dirs, which is where autolearn writes, so `omp.ADAPTER.profile_mounts` is empty for now;
  the inbox, the snapshot and `ab profile harvest` all work for OMP today. The open question (is autolearn's
  output directory configurable?) and the four options are in `docs/TODO.md`.
- **Keys never enter via these files.** `models.yml` is regenerated per session from the keyless seed and
  references `AB_KEY_<PROVIDER>` variables; `config.yml` and `plugins/` are mounted read-only and are *not*
  scanned, so never put a secret in them (see the OMP note in `docs/design.md`).

### The asymmetry in one line

Claude: the config dir is per-project, but what the agent *loads* is **global and read-only** — the host
profile `~/.config/agentbox/claude/profile/`; a session's own additions go to the per-project inbox, which
nothing loads until you promote it. OMP: plugins and `config.yml` are a **global, read-only** toolchain shared by
every project; only `agent/` (per-project working state) and your `~/.config/agentbox/omp/` seed are yours to
change, and a per-project OMP tweak lives in the repo.

---

## 2. The personalization layers, and how to change each

| Layer | Edit here | Affects | Persists |
|---|---|---|---|
| **A. Global scaffold template** | `agentbox/initproj.py` (code today — see §3) | every *future* `ab init` project | — |
| **B. Global agent seed** | `~/.config/agentbox/claude/` or `~/.config/agentbox/omp/` | every session of that agent, all projects | copied/mounted each start |
| **C. OMP global toolchain** | host `~/.omp` (`omp-env install`, `omp plugin link`) | every OMP session, all projects | host-shared, ro |
| **D. Per-project repo files** | files committed in the project (`CLAUDE.md`, `AGENTS.md`, `.mcp.json`, `.claude/`, `.omp/`, `dev/fixtures/`) | that one project | in the repo |
| **E. Per-project agent state** | `~/.local/share/agentbox/<project>/<agent>/` | that one project, across sessions | on disk, `ab project reset-state` wipes it |
| **F. Per-project settings** (preset, egress, grants, podman tier) | `ab project set <name> …` — the same flags `ab init`/`ab project add` take; `--unset KEY` returns one to its preset value | that one project, from its **next** session | `projects.toml` (`ab project show` prints each value's source) |

A settings change applies to the next session, not a running one, and `ab project set` refuses while a
session for that project is live (as do `rm`, `reset-state` and the `accept-*` commands) — end it with
`ab session kill` first. Custom presets are TOML files in `~/.config/agentbox/presets/`; one under a new
name inherits `lockdown`'s values for anything it omits (see `docs/design.md`, Presets).

### (a) Add a Claude plugin / MCP to **one** project

- **MCP server:** create `.mcp.json` in the project repo (layer D) and commit it. Claude reads it from the
  project root in the session.
- **Slash command:** add it to the repo's `.claude/commands/` (layer D), or to the per-project config dir
  `~/.local/share/agentbox/<project>/claude/commands/` (layer E) if you don't want it committed.
- These affect only this project. (In a `lockdown` project, repo `.claude/` hooks/rules are covered off; use
  it on a `pods`/`python` project.)

### (b) Add an OMP plugin (**global**) and an OMP MCP server (**per-project**)

- **Plugin (global):** install it into the host `~/.omp` outside agentbox (`omp-env install` / `omp plugin
  link`, layer C). It then mounts read-only into **every** OMP session. agentbox does not install plugins;
  it mounts the host toolchain as-is.
- **MCP server (per-project):** commit a `.mcp.json` (or `.omp/`) to the project repo (layer D); OMP reads it
  from the project root alongside `~/.omp/agent`. A *global* OMP MCP server instead goes in
  `~/.config/agentbox/omp/mcp.json` (layer B), mounted read-only at `~/.omp/agent/.mcp.json` in every OMP
  session.

### (c) Change the default `AGENTS.md` for **all future** projects

Create `~/.config/agentbox/templates/<template>/AGENTS.md` (and/or `README.md`); `ab init` uses it verbatim,
`{name}`-substituted, for every future project of that template. Only those two doc filenames are read — see
§3. Changing an existing project's `AGENTS.md` is just editing the committed file (layer D).

---

## 3. The scaffold template and its override

The built-in scaffold (`AGENTS.md`, `.gitignore`, `.env`/`.env.dev`, `README.md`, compose, skeleton) is Python
string constants in `agentbox/initproj.py` (`_AGENTS`, `_GITIGNORE`, `_README`, …), filled with the project
name and template. The `.env`/mask/compose/data-volume **structure always comes from these constants** — it is
what keeps a fresh project grantcheck-clean, so it is not user-overridable.

**The doc override (`initproj._template_overrides`, `OVERRIDABLE`):** before writing a project, `ab init`
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
layout or slip a secret into `.env`. When the dir is absent, `ab init` behaves exactly as before. Symlinks in
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
from the project's registry entry and preset at session time, not from `ab init`; writing them into the
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
