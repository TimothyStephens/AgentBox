# `ab` command reference

The full `--help` for every `agentbox` (`ab`) command and subcommand. This file is **generated** from the CLI, so it is exactly what the installed launcher prints. Regenerate it with:

```bash
python3 docs/gen_usage.py > docs/USAGE.md
```

For the two command namespaces and when to reach for each, see `docs/design.md` (Usage) and `docs/PERSONALIZATION.md`. In short: **`ab project`** manages the durable registry entries (configuration), **`ab session`** manages the live sandboxed runs, and `ab run` / `ab init` are the verbs that create them.

## Contents

- [Top level](#top-level)
- [ab init](#ab-init)
- [ab run](#ab-run)
- [ab selftest](#ab-selftest)
- [ab project](#ab-project)
- [ab session](#ab-session)
- [ab keys](#ab-keys)

## Top level

### `ab`

```text
usage: ab [-h] {init,project,run,session,selftest,keys} ...

agentbox: coding agents (Claude Code, OMP) in hardened rootless podman sandboxes, one project per session.

positional arguments:
  {init,project,run,session,selftest,keys}
    init                scaffold a new project with a grantcheck-clean layout
                        (secrets masked, live data in a named volume), then
                        register it
    project             manage registered projects (the saved configuration)
    run                 start an agent session in the foreground
    session             manage running sessions (the live instances)
    selftest            prove the sandbox boundaries hold (posture, egress,
                        mounts, podman scoping)
    keys                model tokens and provider API keys

options:
  -h, --help            show this help message and exit

`ab project` manages the saved configuration; `ab session` manages the live runs; `ab run` and `ab init` are the verbs that create them. Full reference: docs/USAGE.md.

examples:
  ab init pod --name web            scaffold and register a new project
  ab run --agent claude --project web   start a session (also: ab run claude web)
  ab session ls                     list running sessions
  ab project show web               a project's effective settings
```

## ab init

### `ab init`

```text
usage: ab init [-h] --name NAME [--upstream URL] {pod,python}

scaffold a new project with a grantcheck-clean layout (secrets masked, live data in a named volume), then register it

positional arguments:
  {pod,python}    which scaffold to create: `pod` (a compose/dev-stack
                  service, live data in a named volume) or `python` (a
                  script/library, data path from DATA_DIR)

options:
  -h, --help      show this help message and exit
  --name NAME     project name; the repo is created at <repos_dir>/<name> and
                  registered under it
  --upstream URL  git URL to add as origin and push to (an off-box CODE
                  backup; it does NOT back up .env or the data volume)

examples:
  ab init pod --name filelab
  ab init python --name dataproc --upstream git@github.com:me/dataproc.git
```

## ab run

### `ab run`

```text
usage: ab run [-h] [--agent AGENT] [--project PROJECT] [--with-secrets]
              [--shell] [--no-tty] [--timeout SECONDS]
              ...

start an agent session in the foreground

positional arguments:
  args               after `--`: extra arguments passed through to the agent
                     or shell

options:
  -h, --help         show this help message and exit
  --agent AGENT      agent to run: `claude` or `omp` (or give it positionally:
                     `ab run <agent> <project>`)
  --project PROJECT  the registered project to run against (or give it
                     positionally)
  --with-secrets     expose the real secret files in the grant to the agent
                     instead of masking them — only for a project whose real
                     secrets live OUTSIDE the grant (see docs/decisions.md)
  --shell            run a bash shell in the sandbox instead of the agent
  --no-tty           do not allocate a TTY (for non-interactive or piped runs)
  --timeout SECONDS  time limit for a non-interactive run (default 3600;
                     ignored for interactive sessions)

examples:
  ab run --agent claude --project web
  ab run claude web                 positional form of --agent/--project
  ab run claude web --shell         a bash shell in the sandbox instead of the agent
  ab run claude web -- --resume     pass everything after -- to the agent
```

## ab selftest

### `ab selftest`

```text
usage: ab selftest [-h] --agent AGENT --project PROJECT [--json]

prove the sandbox boundaries hold (posture, egress, mounts, podman scoping)

options:
  -h, --help         show this help message and exit
  --agent AGENT      agent to run the checks as (claude or omp)
  --project PROJECT  the registered project to test
  --json             print results as JSON instead of a table

examples:
  ab selftest --agent claude --project web
  ab selftest --agent claude --project web --json
```

## ab project

### `ab project`

```text
usage: ab project [-h]
                  {add,accept-history-secrets,accept-grant-exposure,show,ls,reset-state,rm}
                  ...

manage registered projects (the saved configuration). A project names a code grant, a preset and any extra data grants.

positional arguments:
  {add,accept-history-secrets,accept-grant-exposure,show,ls,reset-state,rm}
    add                 register a project with a preset (validated against
                        overlap and grant exposure)
    accept-history-secrets
                        accept (after rotating them) the masked file names now
                        in a project's git history
    accept-grant-exposure
                        accept that this project's grant holds secrets or live
                        data the agent can read
    show                print every effective setting and whether it came from
                        the preset or an override
    ls                  list registered projects (name, preset, root)
    reset-state         wipe a project's agent state (memory, settings,
                        transcripts); asks first, keeps a copy in the backups
                        dir
    rm                  remove one or more projects from the registry (files,
                        state and backups are untouched)

options:
  -h, --help            show this help message and exit

examples:
  ab project add web ~/repos/web --preset pods
  ab project show web
  ab project ls
  ab project rm web
```

### `ab project add`

```text
usage: ab project add [-h] --preset PRESET [--data-ro DIR] [--data-rw DIR]
                      [--egress MODE] [--extra-domain DOMAIN]
                      [--build-memory SIZE] [--mask-skip DIR] [--podman TIER]
                      [--compose-project NAME] [--dev-image PATTERN]
                      [--observe-logs] [--accept-history-secrets]
                      [--i-understand-grant-exposure]
                      name root

register a project with a preset (validated against overlap and grant exposure)

positional arguments:
  name                  a short name for the project (lower-case letters,
                        digits, '-' or '_')
  root                  path to the project's code directory — the read-write
                        grant; must exist and not overlap another project or a
                        protected path

options:
  -h, --help            show this help message and exit
  --preset PRESET       security profile: `lockdown` (review-only, no
                        network/podman), `pods` (build and run the project's
                        own dev stack), or `python` (scripts/data); or a
                        custom preset in ~/.config/agentbox/presets/. `ab
                        project show` prints what one resolves to
  --data-ro DIR         an extra directory to mount read-only (repeatable);
                        for input data that lives outside the code grant
  --data-rw DIR         an extra directory to mount read-write (repeatable);
                        for output data outside the code grant
  --egress MODE         network egress: `locked` (model API only), `packages`
                        (+ package registries), or `open` (+ any public host);
                        the LAN and host are always refused. Overrides the
                        preset
  --extra-domain DOMAIN
                        add a domain to the egress allowlist (repeatable)
  --build-memory SIZE   memory limit for each build in the dev tier (default
                        2g, e.g. 4g)
  --mask-skip DIR       a folder (relative to root) the secret-mask walk skips
                        for speed on huge trees; NOT an exemption — grantcheck
                        still scans it (repeatable)
  --podman TIER         podman tier: `none`, `observe` (read the live stack),
                        or `dev` (run the agent's own <project>-dev stack).
                        `exec` is refused — not built
  --compose-project NAME
                        the live stack's compose project name for the observe
                        tier (default: the project name)
  --dev-image PATTERN   dev tier: also allow running or building from images
                        matching this name pattern (e.g. localhost/myapp:*);
                        by default only allowlisted registries and this
                        project's builds (repeatable)
  --observe-logs        let the observe tier read the live stack's logs (off
                        by default: logs can hold secrets, and what the agent
                        reads goes to its model provider)
  --accept-history-secrets
                        register even though masked file names (.env, *.key,
                        ...) are in git history
  --i-understand-grant-exposure
                        register even though the grant holds secrets or live
                        data (a grant is for code; move them outside it or add
                        a mask)

examples:
  ab project add web ~/repos/web --preset pods
  ab project add dataproc ~/repos/dataproc --preset python --data-ro ~/data/raw --data-rw ~/data/out
  ab project add svc ~/repos/svc --preset pods --egress open --podman dev
```

### `ab project show`

```text
usage: ab project show [-h] name

print every effective setting and whether it came from the preset or an override

positional arguments:
  name        the registered project to inspect

options:
  -h, --help  show this help message and exit

examples:
  ab project show web
```

### `ab project ls`

```text
usage: ab project ls [-h]

list registered projects (name, preset, root)

options:
  -h, --help  show this help message and exit

examples:
  ab project ls
```

### `ab project rm`

```text
usage: ab project rm [-h] name [name ...]

remove one or more projects from the registry (files, state and backups are untouched)

positional arguments:
  name        one or more registered project names (all-or-nothing: a bad name
              removes none)

options:
  -h, --help  show this help message and exit

examples:
  ab project rm web
  ab project rm web old-thing
```

### `ab project reset-state`

```text
usage: ab project reset-state [-h] [--yes] name

wipe a project's agent state (memory, settings, transcripts); asks first, keeps a copy in the backups dir

positional arguments:
  name        the registered project whose agent state to wipe

options:
  -h, --help  show this help message and exit
  --yes       do not ask for confirmation

examples:
  ab project reset-state web
  ab project reset-state web --yes
```

### `ab project accept-grant-exposure`

```text
usage: ab project accept-grant-exposure [-h] name

accept that this project's grant holds secrets or live data the agent can read

positional arguments:
  name        the registered project to record the acknowledgement for

options:
  -h, --help  show this help message and exit

examples:
  ab project accept-grant-exposure web
```

### `ab project accept-history-secrets`

```text
usage: ab project accept-history-secrets [-h] name

accept (after rotating them) the masked file names now in a project's git history

positional arguments:
  name        the registered project to record the acknowledgement for

options:
  -h, --help  show this help message and exit

examples:
  ab project accept-history-secrets web
```

## ab session

### `ab session`

```text
usage: ab session [-h] {ls,preview,kill,gc} ...

manage running sessions (the live instances). Start one with `ab run`.

positional arguments:
  {ls,preview,kill,gc}
    ls                  list sessions with their project, start time, age and
                        idle time
    preview             open a session's dev service on 127.0.0.1 (reach it
                        from your desktop with ssh -L)
    kill                tear down a session and everything it created (keeps
                        its logs and end-of-session scan)
    gc                  remove containers, networks and sockets left by dead
                        sessions (runs automatically after boot)

options:
  -h, --help            show this help message and exit

examples:
  ab session ls
  ab session preview ab-claude-web-1 web:8000
  ab session kill ab-claude-web-1
  ab session gc
```

### `ab session ls`

```text
usage: ab session ls [-h]

list sessions with their project, start time, age and idle time

options:
  -h, --help  show this help message and exit

examples:
  ab session ls
```

### `ab session preview`

```text
usage: ab session preview [-h] [--port PORT] [--stop] session [SERVICE:PORT]

open a session's dev service on 127.0.0.1 (reach it from your desktop with ssh -L)

positional arguments:
  session       the session name (from `ab session ls`)
  SERVICE:PORT  the dev-stack service (or container) and the port inside it,
                e.g. web:8000; omit with --stop

options:
  -h, --help    show this help message and exit
  --port PORT   host port to publish on 127.0.0.1 (default: a free one,
                printed on start)
  --stop        remove this session's previews instead of opening one

examples:
  ab session preview ab-claude-web-1 web:8000
  ab session preview ab-claude-web-1 web:8000 --port 18080
  ab session preview ab-claude-web-1 --stop
```

### `ab session kill`

```text
usage: ab session kill [-h] session

tear down a session and everything it created (keeps its logs and end-of-session scan)

positional arguments:
  session     the session name to tear down (from `ab session ls`)

options:
  -h, --help  show this help message and exit

examples:
  ab session kill ab-claude-web-1
```

### `ab session gc`

```text
usage: ab session gc [-h]

remove containers, networks and sockets left by dead sessions (runs automatically after boot)

options:
  -h, --help  show this help message and exit

examples:
  ab session gc
```

## ab keys

### `ab keys`

```text
usage: ab keys [-h] {rotate,set,ls,rm} ...

manage model tokens and provider API keys. Keys live in ~/.config/agentbox/keys/ (0600), outside every grant, and reach a session read-only as AB_KEY_<PROVIDER>.

positional arguments:
  {rotate,set,ls,rm}
    rotate            replace the Claude subscription token (from `claude
                      setup-token`)
    set               set a provider's API key (pay-per-use: make it dedicated
                      and spend-capped)
    ls                list providers that have a key (names only, never
                      values)
    rm                remove a provider's key file

options:
  -h, --help          show this help message and exit

examples:
  ab keys rotate claude
  ab keys set openai
  ab keys ls
  ab keys rm openai
```

### `ab keys rotate`

```text
usage: ab keys rotate [-h] agent

replace the Claude subscription token (from `claude setup-token`)

positional arguments:
  agent       the agent whose subscription token to replace (currently:
              claude)

options:
  -h, --help  show this help message and exit

examples:
  ab keys rotate claude
```

### `ab keys set`

```text
usage: ab keys set [-h] provider

set a provider's API key (pay-per-use: make it dedicated and spend-capped)

positional arguments:
  provider    the provider name; its key becomes AB_KEY_<PROVIDER> in sessions
              (you are prompted for the value, which is never on the command
              line)

options:
  -h, --help  show this help message and exit

examples:
  ab keys set openai
```

### `ab keys ls`

```text
usage: ab keys ls [-h]

list providers that have a key (names only, never values)

options:
  -h, --help  show this help message and exit

examples:
  ab keys ls
```

### `ab keys rm`

```text
usage: ab keys rm [-h] provider

remove a provider's key file

positional arguments:
  provider    the provider whose key file to remove

options:
  -h, --help  show this help message and exit

examples:
  ab keys rm openai
```
