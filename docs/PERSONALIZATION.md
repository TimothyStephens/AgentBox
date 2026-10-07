# Personalizing agentbox

How to give an agent your settings, plugins, commands and MCP servers, and where each change takes effect
— one project or all of them. Everything here is read from the code (`agentbox/adapters/claude.py`,
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
| `settings.json`, `CLAUDE.md`, `commands/` | `~/.config/agentbox/claude/` (the **seed**) | copied into the config dir at every start | **global** (every project) | rw copy |
| Managed policy (lockdown / approvals on) | generated in `<run>/sessions/<s>/claude-managed-settings.json` | `/etc/claude-code/managed-settings.json` | per-session | ro |
| Repo config (`CLAUDE.md`, `.mcp.json`, `.claude/`) | the project repo (its real path) | same real path (the grant) | per-project (committed) | rw (ro where covered) |

- `settings.json`, `CLAUDE.md` and `commands/` are **copied from the global seed** `~/.config/agentbox/claude/`
  into the per-project config dir at every start (`seed_state`, `SEEDED`). A file the seed does not ship is
  left as the project left it last session (it is not wiped). The sandbox description is appended to
  `CLAUDE.md` each start (see §4).
- **Plugins / slash commands:** Claude has no plugin tree like OMP; its reusable pieces are slash commands in
  `commands/`. Put them in the seed (global) or in the project's `.claude/commands/`.
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

- The writable `agent/` dir has the read-only files above (`config.yml`, `models.yml`, `.mcp.json`,
  `mags-codedev.yaml`, `AGENTS.md`) mounted on top of it, and `agent/extensions`, `agent/hooks`,
  `agent/tools` are covered empty read-only (so one session cannot plant code the next runs). Prompt dirs
  (`skills`, `rules`, `prompts`, `agents`, `commands`) stay writable for OMP's autolearn.
- **Keys never enter via these files.** `models.yml` is regenerated per session from the keyless seed and
  references `AB_KEY_<PROVIDER>` variables; `config.yml` and `plugins/` are mounted read-only and are *not*
  scanned, so never put a secret in them (see the OMP note in `docs/design.md`).

### The asymmetry in one line

Claude: edit **per-project** by default (the config dir is per-project); the only global lever is the seed
`~/.config/agentbox/claude/`. OMP: plugins and `config.yml` are a **global, read-only** toolchain shared by
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

Today this is **not** a config edit: the scaffolded `AGENTS.md` is a string constant (`_AGENTS`) in
`agentbox/initproj.py`, so changing it for all future `ab init` projects means editing that source (layer A).
Changing an existing project's `AGENTS.md` is just editing the committed file (layer D). See §3 for the
proposed knob that would make the global change a single config edit.

---

## 3. Is there a global default-`AGENTS.md` template?

**No editable one.** The scaffolded `AGENTS.md`, `.gitignore`, `.env`/`.env.dev`, `README.md`, compose and
skeleton are all Python string constants in `agentbox/initproj.py` (`_AGENTS`, `_GITIGNORE`, `_README`, …),
filled with the project name and template. There is no file under `~/.config/agentbox` that `ab init` reads,
so personalizing the scaffold today means editing code.

**Proposed knob (not built):** have `ab init` read an override from the config dir before falling back to the
built-in constant — the smallest version:

- look for `~/.config/agentbox/templates/<template>/AGENTS.md` (and optionally other scaffold files); if
  present, use it verbatim (with `{name}` substitution) instead of `_AGENTS`; otherwise use the built-in.
- one new config dir, no schema, no behavior change when the dir is absent. A user edits one file; `ab init`
  stays grantcheck-clean because the override only replaces documentation files, never the `.env`/mask/compose
  structure.

This keeps the safe layout fixed in code and makes only the human-facing text user-overridable.

---

## 4. Design note — a generated preamble in `AGENTS.md` (assessment, not a build)

**The request:** make the scaffolded `AGENTS.md` carry a generated preamble stating the project's actual
egress mode, data layout and the agent's real do/can't-do boundaries, so the agent is *told* its environment
instead of discovering it by hitting the gate.

**What already exists.** `session.sandbox_notes()` generates exactly this preamble **per session** and injects
it into what the agent reads: it is appended to `CLAUDE.md` for Claude (`seed_state`) and is the generated
`~/.omp/agent/AGENTS.md` for OMP (`_omp_notes`). It already states, from the live project config:

- the project root and its read-only / read-write data grants;
- that the grant holds code and the session refuses to start on an uncovered secret or live database;
- the masks and covers (`.env`/`*.pem`/`*.key`/`secrets/`, read-only `.git/hooks`/`config`, the `.env.dev`
  swap) and that they are enforced for containers the agent starts too;
- the **actual egress mode** (`locked` / `packages` / `open`) with the concrete reachable set, and that private
  and LAN addresses are always refused;
- the exact podman access for the tier (none / observe / dev), or that there is none.

So the agent *is* told its environment at session start; it does not have to learn the boundary by hitting a
`403`. **This belongs in the per-session notes, not baked into the committed scaffold**, because the live
facts (egress mode, data grants, podman tier) come from the project's registry entry and preset at session
time, not from `ab init`. Writing them into the committed `AGENTS.md` at scaffold time would duplicate the
session notes and go stale the moment a preset or `egress` override changes.

**Two small, optional improvements worth considering (still no build here):**

1. **Lead with it for Claude.** The preamble is currently *appended* after the user's `CLAUDE.md`. Prepending
   it (or a one-line pointer to it) would make the environment the first thing the agent reads.
2. **Have the scaffolded `AGENTS.md` point at it** rather than restate boundaries generically: a single line
   such as "agentbox injects your real egress mode, data layout and limits at session start — read the
   sandbox section" keeps the committed file portable (it is also read outside agentbox) while making clear
   the authoritative, live description arrives per session.

Net: the capability the note asks for is already present and correct as a per-session generated preamble; the
only gap is prominence and a pointer, not a missing feature. No change is proposed to bake live facts into the
committed scaffold.
