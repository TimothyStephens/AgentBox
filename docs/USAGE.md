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

agentbox: coding agents in hardened podman sandboxes

positional arguments:
  {init,project,run,session,selftest,keys}
    init                scaffold a new project with a grantcheck-clean layout
                        (secrets masked, live data in a named volume), then
                        register it
    project             manage registered projects
    run                 start an agent session in the foreground
    session             manage running sessions
    selftest            prove the sandbox boundaries hold
    keys                model tokens and provider API keys

options:
  -h, --help            show this help message and exit
```

## ab init

### `ab init`

```text
usage: ab init [-h] --name NAME [--upstream UPSTREAM] {pod,python}

positional arguments:
  {pod,python}

options:
  -h, --help           show this help message and exit
  --name NAME          project name; the repo is created at <repos_dir>/<name>
  --upstream UPSTREAM  git URL to add as origin and push to (an off-box CODE
                       backup; it does NOT back up .env or the data volume)
```

## ab run

### `ab run`

```text
usage: ab run [-h] [--agent AGENT] [--project PROJECT] [--with-secrets]
              [--shell] [--no-tty] [--timeout TIMEOUT]
              ...

positional arguments:
  args               after --: extra arguments for the agent or shell

options:
  -h, --help         show this help message and exit
  --agent AGENT      agent name (or give it positionally: `ab run <agent>
                     <project>`)
  --project PROJECT  project name (or give it positionally)
  --with-secrets
  --shell            a bash shell in the sandbox instead of the agent
  --no-tty
  --timeout TIMEOUT  non-interactive runs only (seconds)
```

## ab selftest

### `ab selftest`

```text
usage: ab selftest [-h] --agent AGENT --project PROJECT [--json]

options:
  -h, --help         show this help message and exit
  --agent AGENT
  --project PROJECT
  --json
```

## ab project

### `ab project`

```text
usage: ab project [-h]
                  {add,accept-history-secrets,accept-grant-exposure,show,ls,reset-state,rm}
                  ...

positional arguments:
  {add,accept-history-secrets,accept-grant-exposure,show,ls,reset-state,rm}
    accept-history-secrets
                        accept (after rotating them) the masked file names now
                        in a project's git history
    accept-grant-exposure
                        accept that this project's grant holds secrets or live
                        data the agent can read
    reset-state         wipe a project's agent state (memory, settings,
                        transcripts); asks first, keeps a copy in the backups
                        dir

options:
  -h, --help            show this help message and exit
```

### `ab project add`

```text
usage: ab project add [-h] --preset PRESET [--data-ro DATA_RO]
                      [--data-rw DATA_RW] [--egress {locked,packages,open}]
                      [--extra-domain EXTRA_DOMAIN]
                      [--build-memory BUILD_MEMORY] [--mask-skip MASK_SKIP]
                      [--podman {none,observe,exec,dev}]
                      [--compose-project COMPOSE_PROJECT]
                      [--dev-image PATTERN] [--observe-logs]
                      [--accept-history-secrets]
                      [--i-understand-grant-exposure]
                      name root

positional arguments:
  name
  root

options:
  -h, --help            show this help message and exit
  --preset PRESET
  --data-ro DATA_RO
  --data-rw DATA_RW
  --egress {locked,packages,open}
  --extra-domain EXTRA_DOMAIN
                        add a domain to the egress allowlist
  --build-memory BUILD_MEMORY
                        memory limit for each build in the dev tier (default
                        2g)
  --mask-skip MASK_SKIP
                        folder (relative to root) the mask walk skips
  --podman {none,observe,exec,dev}
                        podman tier (none/observe/dev are built; observe reads
                        the live stack, dev runs the agent's own <project>-dev
                        stack; exec is refused — not built)
  --compose-project COMPOSE_PROJECT
                        the live stack's compose project name (default: the
                        project name)
  --dev-image PATTERN   dev tier: also allow running or building from images
                        matching this name pattern (e.g. localhost/myapp:*);
                        by default only allowlisted registries and this
                        project's builds
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
```

### `ab project show`

```text
usage: ab project show [-h] name

positional arguments:
  name

options:
  -h, --help  show this help message and exit
```

### `ab project ls`

```text
usage: ab project ls [-h]

options:
  -h, --help  show this help message and exit
```

### `ab project rm`

```text
usage: ab project rm [-h] name [name ...]

positional arguments:
  name        one or more project names

options:
  -h, --help  show this help message and exit
```

### `ab project reset-state`

```text
usage: ab project reset-state [-h] [--yes] name

positional arguments:
  name

options:
  -h, --help  show this help message and exit
  --yes       do not ask
```

### `ab project accept-grant-exposure`

```text
usage: ab project accept-grant-exposure [-h] name

positional arguments:
  name

options:
  -h, --help  show this help message and exit
```

### `ab project accept-history-secrets`

```text
usage: ab project accept-history-secrets [-h] name

positional arguments:
  name

options:
  -h, --help  show this help message and exit
```

## ab session

### `ab session`

```text
usage: ab session [-h] {ls,preview,kill,gc} ...

positional arguments:
  {ls,preview,kill,gc}
    ls                  list sessions
    preview             open a session's dev service on 127.0.0.1 (reach it
                        with ssh -L)
    kill                tear down a session
    gc                  remove what dead sessions left behind

options:
  -h, --help            show this help message and exit
```

### `ab session ls`

```text
usage: ab session ls [-h]

options:
  -h, --help  show this help message and exit
```

### `ab session preview`

```text
usage: ab session preview [-h] [--port PORT] [--stop] session [target]

positional arguments:
  session
  target       <service>:<port>, e.g. web:8000

options:
  -h, --help   show this help message and exit
  --port PORT  host port on 127.0.0.1 (default: a free one)
  --stop       remove the session's previews
```

### `ab session kill`

```text
usage: ab session kill [-h] session

positional arguments:
  session

options:
  -h, --help  show this help message and exit
```

### `ab session gc`

```text
usage: ab session gc [-h]

options:
  -h, --help  show this help message and exit
```

## ab keys

### `ab keys`

```text
usage: ab keys [-h] {rotate,set,ls,rm} ...

positional arguments:
  {rotate,set,ls,rm}
    rotate            replace the Claude subscription token
    set               set a provider's API key (pay-per-use: make it dedicated
                      and spend-capped)
    ls                providers with a key (names only, never values)
    rm                remove a provider's key file

options:
  -h, --help          show this help message and exit
```

### `ab keys rotate`

```text
usage: ab keys rotate [-h] agent

positional arguments:
  agent

options:
  -h, --help  show this help message and exit
```

### `ab keys set`

```text
usage: ab keys set [-h] provider

positional arguments:
  provider

options:
  -h, --help  show this help message and exit
```

### `ab keys ls`

```text
usage: ab keys ls [-h]

options:
  -h, --help  show this help message and exit
```

### `ab keys rm`

```text
usage: ab keys rm [-h] provider

positional arguments:
  provider

options:
  -h, --help  show this help message and exit
```
