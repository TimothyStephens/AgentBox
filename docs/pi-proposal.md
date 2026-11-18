# Pi adapter: research and decisions (18 Nov 2026)

Status: **decided** (Tim, 18 Nov); core built, packages next. Folded into design.md ("Adapters") and deleted once
fully built, as docs/deploy-proposal.md was. Pi = `@earendil-works/pi-coding-agent` (CLI `pi`), researched at
**1.5.2** from its published package (docs and dist read as text, nothing run). The first research targeted
`@mariozechner/pi-coding-agent@0.73.1`, which turned out to be deprecated; every row below was re-checked at 1.5.2.
Pi is treated like OMP and Claude (Tim, 18 Nov): per-project state, the host-owned profile, the inbox, keys only
through `ab keys`, Toulmin through MCP, the same CLI and sweeps.

## Pi 1.5.2, and what changed since 0.73.1

| | Pi 1.5.2 | vs 0.73.1 |
|---|---|---|
| Home / state | `~/.pi/agent`; `PI_CODING_AGENT_DIR`, `PI_CODING_AGENT_SESSION_DIR`, new `PI_PACKAGE_DIR` | unchanged (+1 var) |
| Project config | `.pi/` relative to cwd; loads **only after project trust** (except `sessionDir`) | changed |
| Project trust | `trust.json` in agent dir; `defaultProjectTrust` ask/always/never; `-a/--approve`, `-na/--no-approve`; -p mode: ask = untrusted | new |
| Models | `models.json` (JSON + comments), same fields + `oauth`; schema error drops the file, semantic error drops only that provider; apiKey/header: literal, `$NAME`/`${NAME}`, `!cmd` | changed (literal by default) |
| Built-in provider keys | same vars (`GEMINI_API_KEY` for `google` …), many more providers; models.json apiKey-only entry now valid | unchanged + more |
| Key precedence | `--api-key` > auth.json > models.json > env var | changed (models.json above env) |
| Credentials files | `auth.json` (`/login`, `pi auth login`, `!cmd` keys); new `mcp-auth.json` (MCP OAuth) | changed (+1 file) |
| Settings | agent + `.pi/settings.json` (project only if trusted), in place + `settings.json.lock`; code/route keys: `packages`, `extensions`, `shellPath`, `shellCommandPrefix`, `npmCommand`, `selfUpdateCommand`, `httpProxy`, `sessionDir`, `defaultProjectTrust`, `enableInstallTelemetry` | changed (new keys) |
| Context | agent `AGENTS.override.md`/AGENTS.md/CLAUDE.md, then cwd ancestry (trust-independent); `SYSTEM.md`/`APPEND_SYSTEM.md` (agent, or trusted project) | changed (+override) |
| Skills / prompts | agent skills/prompts, `~/.agents/skills`; `.pi/*` and project `.agents/skills` only when trusted; `commands/` still renamed to `prompts/` (also in the repo's `.pi`) | changed (trust) |
| Code places | `extensions`, `git`, `npm` (managed global npm), `bin`, `tools` (deprecated), `tmp/extensions`; built-ins `builtin:mcp`, `builtin:llama.cpp`, `builtin:codemode`, `builtin:tool-search` | changed |
| Non-interactive | `pi -p` / `--mode json` / `--mode rpc`; `--model provider/id`; events `tool_execution_start/update/end`, `agent_end`, `agent_settled` | unchanged (+event) |
| Approvals | **none**; only an extension `tool_call` gate; `--approve` is project trust, not tool approval | unchanged (beware the name) |
| MCP | **built in**: `<agent-dir>/mcp.json` + trusted `.pi/mcp.json`; exposure codemode/deferred/direct/hidden; `--no-mcp`, `-builtin:mcp` | new |
| Codemode | QuickJS JS tool, off unless enabled or an MCP server needs it | new |
| Lockdown | `--no-approve` stops all trust-gated repo loading (incl. package install); `PI_OFFLINE` stops installs; `-ne` disables built-ins too, `-e` still loads | changed (trust is the lever) |
| Startup network | version check, install telemetry, fd/rg download, package install, catalog refresh: off with `PI_OFFLINE=1`; attribution headers need `PI_TELEMETRY=0`; MCP not affected | changed |
| Proxy | undici `EnvHttpProxyAgent` global + undici fetch; `httpProxy` setting; WebSocket via global WebSocket | unchanged (+setting) |
| Runtime | node ≥ 22.19, `dist/bundle/cli.js`, no native modules required; no browser | changed (node floor) |
| Plugins | `pi install npm:|git:|path`; npm → `<agent-dir>/npm`, git → `<agent-dir>/git`; local paths loaded in place, never modified | changed (npm location) |

## Decisions (Tim, 18 Nov 2026)

| # | Decision |
|---|---|
| P1 | Pi has no tool approvals (`--approve` is project trust, and would LOWER safety): Pi is **refused** under a preset with approvals on; every session runs `--no-approve` |
| P2 | Keys only via `ab keys`, written into `models.json` as `${AB_KEY_<NAME>}` (a bare value is sent literally since 0.77); `auth.json` is `{}` and read-only, so `/login` cannot store one |
| P3 | Version 1 = core adapter + Pi packages (`ab profile install pi`, a read-only tree loaded in place) + Toulmin through Pi's built-in MCP (`mcp.json`, `exposure: direct`) |
| P4 | Models come from the OMP seed `~/.config/agentbox/omp/models.yml`, through the same `build_models` checks, translated (`gemini` -> `google`, `maxOut` -> `maxTokens`; a custom provider without a baseUrl and models is left out) |
| P5 | `$` is refused anywhere in the shared model seed, for OMP and Pi: `$NAME` would pull another provider's key into a header or URL |
| P6 | Pi's agent dir is **per project** (like OMP), and the files Pi would load from it at start (SYSTEM.md, APPEND_SYSTEM.md, AGENTS.override.md, AGENTS.MD, CLAUDE.md, CLAUDE.MD, trust.json, themes) are moved to the inbox at every start. Recommended instead: a fresh agent dir per session keeping only sessions, because each Pi release can add a file name that steers the next session; Tim chose per-project (decisions.md) |

## Built (step 1)

`agentbox/adapters/pi.py`, `images/pi/Containerfile`, `make pi-image`, `make pi-live`: `~/.pi` tmpfs; per-project
`~/.pi/agent` with extensions, git, npm, bin, tools and tmp covered empty read-only; generated read-only
`models.json`, `AGENTS.md`, `auth.json`, `mcp.json`; a fresh `settings.json` each start (code-running keys refused,
`defaultProjectTrust = never` and `enableInstallTelemetry = false` forced); the repo's `.pi` covered; PI_OFFLINE=1,
PI_TELEMETRY=0; profile skills and prompts read-only; Toulmin via `mcp_server`.

## Next (step 2): packages

`ab profile install pi` builds the profile's declared Pi packages into a tree agentbox owns (npm packages under a
prefix, git packages checked out), mounted read-only; the generated settings.json names them by local path, which
Pi loads in place and never modifies.

## Only a live run can confirm

Read-only binds of auth.json/models.json/mcp.json with Pi's lock files; `${AB_KEY_X}` keys authenticating; the bash
event in `--mode json`; all provider traffic (incl. OpenAI's websocket transport) through squid; nothing reaching
pi.dev or a package registry; a Toulmin server with direct exposure appearing as tools.
