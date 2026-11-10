# `ab` command reference

The full `--help` for every `agentbox` (`ab`) command and subcommand. This file is **generated** from the CLI, so it is exactly what the installed launcher prints. Regenerate it with:

```bash
python3 docs/gen_usage.py > docs/USAGE.md
```

For the two command namespaces and when to reach for each, see `docs/design.md` (Usage) and `docs/PERSONALIZATION.md`. In short: **`ab project`** manages the durable registry entries (configuration), **`ab session`** manages the live sandboxed runs, and `ab session start` / `ab project init` are the verbs that create them.

## Contents

- [Top level](#top-level)
- [ab project](#ab-project)
- [ab session](#ab-session)
- [ab screen](#ab-screen)
- [ab profile](#ab-profile)
- [ab image](#ab-image)
- [ab keys](#ab-keys)

## Top level

### `ab`

The launcher. Two nouns: `ab project` is the saved configuration (init, add, set, ...), `ab session` is the live run (start, attach, kill, ...). `ab screen` is the tmux workspace.

```text
usage: ab [-h] {project,profile,image,session,screen,keys} ...

agentbox: coding agents (Claude Code, OMP) in hardened rootless podman sandboxes, one project per session.

positional arguments:
  {project,profile,image,session,screen,keys}
    project             manage registered projects (the saved configuration)
    profile             the host-owned agent profile: what every session
                        loads, and the inbox it feeds
    image               your own image layer on top of an agent's harness
                        image
    session             the live runs: start, attach, list, preview, kill, gc
    screen              open the tmux workspace: agent panes (each an `ab
                        session start`) and host shells, in a layout from
                        ~/.config/agentbox/screen.toml
    keys                model tokens and provider API keys

options:
  -h, --help            show this help message and exit

Two nouns: `ab project` is the saved configuration (init, add, set, ...), `ab session` is the live run (start, attach, kill, ...). `ab screen` opens the tmux workspace. Full reference: docs/USAGE.md.

examples:
  ab project init pod web              scaffold and register a new project
  ab session start claude web          start a session
  ab session ls                     list running sessions
  ab project show web               a project's effective settings
```

## ab project

### `ab project`

Manage the durable registry entries — a project names a code grant, a preset and any extra data grants. One project has many sessions over time.

```text
usage: ab project [-h]
                  {init,add,set,check,accept-history-secrets,accept-grant-exposure,show,ls,reset-state,purge,rm,selftest}
                  ...

manage registered projects (the saved configuration). A project names a code grant, a preset and any extra data grants.

positional arguments:
  {init,add,set,check,accept-history-secrets,accept-grant-exposure,show,ls,reset-state,purge,rm,selftest}
    init                scaffold a new project with a grantcheck-clean layout
                        (secrets masked, live data in a named volume), then
                        register it
    add                 register a project with a preset (validated against
                        overlap and grant exposure)
    set                 change a registered project's settings in place (re-
                        validated; refused while a session for it is live)
    check               show what the grant scan finds now and whether it
                        matches what was accepted (read-only)
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
    purge               delete a project's agent state and inbox outright
                        (asks for --yes; the only command that deletes them)
    rm                  remove one or more projects from the registry (files,
                        state and backups are untouched)
    selftest            prove the sandbox boundaries hold for a project
                        (posture, egress, mounts, podman scoping), in a
                        throwaway session

options:
  -h, --help            show this help message and exit

examples:
  ab project add web ~/repos/web --preset build
  ab project set web --egress open
  ab project show web
  ab project ls
  ab project rm web
```

### `ab project init`

Scaffold a new project with a safe-by-construction layout (secrets masked, live data in a named volume) and register it, so a fresh project starts a session with no warnings.

```text
usage: ab project init [-h] [--upstream URL] [--preset PRESET] [--data-ro DIR]
                       [--data-rw DIR] [--egress MODE] [--extra-domain DOMAIN]
                       [--build-memory SIZE] [--base-branch BRANCH]
                       [--profile on|off] [--mask-skip DIR] [--mask PATH]
                       [--podman TIER] [--compose-project NAME]
                       [--dev-image PATTERN] [--swap TARGET=STANDIN]
                       [--observe-logs | --no-observe-logs]
                       [--accept-history-secrets]
                       [--i-understand-grant-exposure] [--reattach | --fresh]
                       {pod,python} name

scaffold a new project with a grantcheck-clean layout (secrets masked, live data in a named volume), then register it

positional arguments:
  {pod,python}          which scaffold to create: `pod` (a compose/dev-stack
                        service, live data in a named volume) or `python` (a
                        script/library, data path from DATA_DIR)
  name                  project name; the repo is created at
                        <repos_dir>/<name> and registered under it

options:
  -h, --help            show this help message and exit
  --upstream URL        git URL to add as origin and push to (an off-box CODE
                        backup; it does NOT back up .env or the data volume)
  --preset PRESET       security profile (default: the template's own — pod:
                        build, python: script). One of: lockdown — review-
                        only: no network beyond the model API, no podman
                        access, approvals on; build — build, run and test the
                        project's own pod/dev stack (packages egress); pods —
                        alias of build; build-open — like build, with open web
                        egress for fetching arbitrary sites; script — scripts
                        and data processing, no podman access (packages
                        egress); python — alias of script; research — review,
                        planning and open web research; no podman access; the
                        code grant is read-write, as under every preset;
                        operate — inspect a live stack (observe tier); data
                        grants are read-only; analysis-offline — sensitive
                        data, no egress beyond the model API, no podman
                        access, data read-only. A file in
                        ~/.config/agentbox/presets/ adds one of your own (it
                        inherits lockdown's values for every key it omits).
                        `ab project show` prints what a preset resolves to,
                        and where each value came from
  --data-ro DIR         an extra directory to mount read-only (repeatable);
                        for input data that lives outside the code grant
  --data-rw DIR         an extra directory to mount read-write (repeatable);
                        for output data outside the code grant
  --egress MODE         network egress: `locked` (model API only), `packages`
                        (+ package registries), or `open` (+ any public host);
                        the LAN and host are always refused. Overrides the
                        preset (default: inherited from the preset)
  --extra-domain DOMAIN
                        add a domain to the egress allowlist (repeatable)
  --build-memory SIZE   memory limit for each build in the dev tier (default:
                        2g, e.g. 4g)
  --base-branch BRANCH  the branch this project's work lands on (e.g.
                        mags/integration). The end-of-session scan names that
                        branch's new commits one by one instead of reporting a
                        count. agentbox neither creates nor checks out the
                        branch; when a tool like MAGS-CodeDev has its own
                        base_branch setting, give the same name here
  --profile on|off      whether this project's sessions load the host-owned
                        profile (skills, sub-agents, commands, instructions,
                        settings) and the agent's plugins (default: on). `off`
                        also means the harness image rather than any user
                        image layer; agentbox's own session notes still apply.
                        `ab session start --no-profile` does the same for one
                        run
  --mask-skip DIR       a folder (relative to root) the secret-mask walk skips
                        for speed on huge trees; NOT an exemption — grantcheck
                        still scans it (repeatable)
  --mask PATH           mask a file or directory in sessions, on top of the
                        secret masks: it exists, reads empty and is read-only,
                        in the agent's container and in any it starts. A bare
                        name (`data`) masks every file or directory of that
                        name anywhere in the grants, as `.env` is; `./data` or
                        `deploy/state` masks that one path under the root. A
                        masked directory is left out of the pre-session backup
                        and the end-of-session scan: it is yours to back up
                        (repeatable)
  --podman TIER         podman tier: `none`, `observe` (read the live stack),
                        or `dev` (run the agent's own <project>-dev stack).
                        `exec` is refused — not built (default: inherited from
                        the preset)
  --compose-project NAME
                        the live stack's compose project name for the observe
                        tier (default: the project name)
  --dev-image PATTERN   dev tier: also allow running or building from images
                        matching this name pattern (e.g. localhost/myapp:*);
                        by default only allowlisted registries and this
                        project's builds (repeatable)
  --swap TARGET=STANDIN
                        show the agent `STANDIN` in place of the masked file
                        `TARGET`, in every directory that has one
                        (repeatable), e.g. --swap .env.local=.env.dev. TARGET
                        must be a name the masks cover (.env, .env.*, *.pem,
                        *.key) and STANDIN must not be, so a swap can never
                        uncover a secret. Merged with the default
                        .env=.env.dev; a mapping for the same target replaces
                        it
  --observe-logs, --no-observe-logs
                        let the observe tier read the live stack's logs
                        (default: off; logs can hold secrets, and what the
                        agent reads goes to its model provider)
  --accept-history-secrets
                        register even though masked file names (.env, *.key,
                        ...) are in git history
  --i-understand-grant-exposure
                        register even though the grant holds secrets or live
                        data (a grant is for code; move them outside it or add
                        a mask)
  --reattach            the name has agent state on disk that was made for a
                        different root: carry it over to this root on purpose
                        (memory, inbox, backups and built-image record
                        included)
  --fresh               the name has agent state on disk: archive it first
                        (every agent's state and the built-image record go to
                        <project>/backups/agent-state-<stamp>/) and start
                        empty

examples:
  ab project init pod filelab
  ab project init python dataproc --upstream git@github.com:me/dataproc.git
```

### `ab project add`

Register a project with a preset, validating it against overlap and grant exposure.

```text
usage: ab project add [-h] --preset PRESET [--data-ro DIR] [--data-rw DIR]
                      [--egress MODE] [--extra-domain DOMAIN]
                      [--build-memory SIZE] [--base-branch BRANCH]
                      [--profile on|off] [--mask-skip DIR] [--mask PATH]
                      [--podman TIER] [--compose-project NAME]
                      [--dev-image PATTERN] [--swap TARGET=STANDIN]
                      [--observe-logs | --no-observe-logs]
                      [--accept-history-secrets]
                      [--i-understand-grant-exposure] [--reattach | --fresh]
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
  --preset PRESET       security profile. One of: lockdown — review-only: no
                        network beyond the model API, no podman access,
                        approvals on; build — build, run and test the
                        project's own pod/dev stack (packages egress); pods —
                        alias of build; build-open — like build, with open web
                        egress for fetching arbitrary sites; script — scripts
                        and data processing, no podman access (packages
                        egress); python — alias of script; research — review,
                        planning and open web research; no podman access; the
                        code grant is read-write, as under every preset;
                        operate — inspect a live stack (observe tier); data
                        grants are read-only; analysis-offline — sensitive
                        data, no egress beyond the model API, no podman
                        access, data read-only. A file in
                        ~/.config/agentbox/presets/ adds one of your own (it
                        inherits lockdown's values for every key it omits).
                        `ab project show` prints what a preset resolves to,
                        and where each value came from
  --data-ro DIR         an extra directory to mount read-only (repeatable);
                        for input data that lives outside the code grant
  --data-rw DIR         an extra directory to mount read-write (repeatable);
                        for output data outside the code grant
  --egress MODE         network egress: `locked` (model API only), `packages`
                        (+ package registries), or `open` (+ any public host);
                        the LAN and host are always refused. Overrides the
                        preset (default: inherited from the preset)
  --extra-domain DOMAIN
                        add a domain to the egress allowlist (repeatable)
  --build-memory SIZE   memory limit for each build in the dev tier (default:
                        2g, e.g. 4g)
  --base-branch BRANCH  the branch this project's work lands on (e.g.
                        mags/integration). The end-of-session scan names that
                        branch's new commits one by one instead of reporting a
                        count. agentbox neither creates nor checks out the
                        branch; when a tool like MAGS-CodeDev has its own
                        base_branch setting, give the same name here
  --profile on|off      whether this project's sessions load the host-owned
                        profile (skills, sub-agents, commands, instructions,
                        settings) and the agent's plugins (default: on). `off`
                        also means the harness image rather than any user
                        image layer; agentbox's own session notes still apply.
                        `ab session start --no-profile` does the same for one
                        run
  --mask-skip DIR       a folder (relative to root) the secret-mask walk skips
                        for speed on huge trees; NOT an exemption — grantcheck
                        still scans it (repeatable)
  --mask PATH           mask a file or directory in sessions, on top of the
                        secret masks: it exists, reads empty and is read-only,
                        in the agent's container and in any it starts. A bare
                        name (`data`) masks every file or directory of that
                        name anywhere in the grants, as `.env` is; `./data` or
                        `deploy/state` masks that one path under the root. A
                        masked directory is left out of the pre-session backup
                        and the end-of-session scan: it is yours to back up
                        (repeatable)
  --podman TIER         podman tier: `none`, `observe` (read the live stack),
                        or `dev` (run the agent's own <project>-dev stack).
                        `exec` is refused — not built (default: inherited from
                        the preset)
  --compose-project NAME
                        the live stack's compose project name for the observe
                        tier (default: the project name)
  --dev-image PATTERN   dev tier: also allow running or building from images
                        matching this name pattern (e.g. localhost/myapp:*);
                        by default only allowlisted registries and this
                        project's builds (repeatable)
  --swap TARGET=STANDIN
                        show the agent `STANDIN` in place of the masked file
                        `TARGET`, in every directory that has one
                        (repeatable), e.g. --swap .env.local=.env.dev. TARGET
                        must be a name the masks cover (.env, .env.*, *.pem,
                        *.key) and STANDIN must not be, so a swap can never
                        uncover a secret. Merged with the default
                        .env=.env.dev; a mapping for the same target replaces
                        it
  --observe-logs, --no-observe-logs
                        let the observe tier read the live stack's logs
                        (default: off; logs can hold secrets, and what the
                        agent reads goes to its model provider)
  --accept-history-secrets
                        register even though masked file names (.env, *.key,
                        ...) are in git history
  --i-understand-grant-exposure
                        register even though the grant holds secrets or live
                        data (a grant is for code; move them outside it or add
                        a mask)
  --reattach            the name has agent state on disk that was made for a
                        different root: carry it over to this root on purpose
                        (memory, inbox, backups and built-image record
                        included)
  --fresh               the name has agent state on disk: archive it first
                        (every agent's state and the built-image record go to
                        <project>/backups/agent-state-<stamp>/) and start
                        empty

examples:
  ab project add web ~/repos/web --preset build
  ab project add dataproc ~/repos/dataproc --preset script --data-ro ~/data/raw --data-rw ~/data/out
  ab project add notes ~/repos/notes --preset research
  ab project add svc ~/repos/svc --preset build-open --podman dev
```

### `ab project set`

Change a registered project's settings in place, re-validated, with the same flags `ab project init`/`ab project add` take. Refused while a session for the project is live.

```text
usage: ab project set [-h] [--preset PRESET] [--data-ro DIR] [--data-rw DIR]
                      [--egress MODE] [--extra-domain DOMAIN]
                      [--build-memory SIZE] [--base-branch BRANCH]
                      [--profile on|off] [--mask-skip DIR] [--mask PATH]
                      [--podman TIER] [--compose-project NAME]
                      [--dev-image PATTERN] [--swap TARGET=STANDIN]
                      [--observe-logs | --no-observe-logs]
                      [--accept-history-secrets]
                      [--i-understand-grant-exposure] [--unset KEY]
                      [--clear-data-ro] [--clear-data-rw]
                      [--clear-extra-domains] [--clear-dev-images]
                      [--clear-mask-skip] [--clear-mask]
                      name

change a registered project's settings in place (re-validated; refused while a session for it is live)

positional arguments:
  name                  the registered project to change (its root cannot be
                        changed: use `ab project rm` + `ab project add` to
                        move a code grant)

options:
  -h, --help            show this help message and exit
  --preset PRESET       security profile (default: the project's current
                        preset). One of: lockdown — review-only: no network
                        beyond the model API, no podman access, approvals on;
                        build — build, run and test the project's own pod/dev
                        stack (packages egress); pods — alias of build; build-
                        open — like build, with open web egress for fetching
                        arbitrary sites; script — scripts and data processing,
                        no podman access (packages egress); python — alias of
                        script; research — review, planning and open web
                        research; no podman access; the code grant is read-
                        write, as under every preset; operate — inspect a live
                        stack (observe tier); data grants are read-only;
                        analysis-offline — sensitive data, no egress beyond
                        the model API, no podman access, data read-only. A
                        file in ~/.config/agentbox/presets/ adds one of your
                        own (it inherits lockdown's values for every key it
                        omits). `ab project show` prints what a preset
                        resolves to, and where each value came from
  --data-ro DIR         an extra directory to mount read-only (repeatable);
                        for input data that lives outside the code grant
  --data-rw DIR         an extra directory to mount read-write (repeatable);
                        for output data outside the code grant
  --egress MODE         network egress: `locked` (model API only), `packages`
                        (+ package registries), or `open` (+ any public host);
                        the LAN and host are always refused. Overrides the
                        preset (default: inherited from the preset)
  --extra-domain DOMAIN
                        add a domain to the egress allowlist (repeatable)
  --build-memory SIZE   memory limit for each build in the dev tier (default:
                        2g, e.g. 4g)
  --base-branch BRANCH  the branch this project's work lands on (e.g.
                        mags/integration). The end-of-session scan names that
                        branch's new commits one by one instead of reporting a
                        count. agentbox neither creates nor checks out the
                        branch; when a tool like MAGS-CodeDev has its own
                        base_branch setting, give the same name here
  --profile on|off      whether this project's sessions load the host-owned
                        profile (skills, sub-agents, commands, instructions,
                        settings) and the agent's plugins (default: on). `off`
                        also means the harness image rather than any user
                        image layer; agentbox's own session notes still apply.
                        `ab session start --no-profile` does the same for one
                        run
  --mask-skip DIR       a folder (relative to root) the secret-mask walk skips
                        for speed on huge trees; NOT an exemption — grantcheck
                        still scans it (repeatable)
  --mask PATH           mask a file or directory in sessions, on top of the
                        secret masks: it exists, reads empty and is read-only,
                        in the agent's container and in any it starts. A bare
                        name (`data`) masks every file or directory of that
                        name anywhere in the grants, as `.env` is; `./data` or
                        `deploy/state` masks that one path under the root. A
                        masked directory is left out of the pre-session backup
                        and the end-of-session scan: it is yours to back up
                        (repeatable)
  --podman TIER         podman tier: `none`, `observe` (read the live stack),
                        or `dev` (run the agent's own <project>-dev stack).
                        `exec` is refused — not built (default: inherited from
                        the preset)
  --compose-project NAME
                        the live stack's compose project name for the observe
                        tier (default: the project name)
  --dev-image PATTERN   dev tier: also allow running or building from images
                        matching this name pattern (e.g. localhost/myapp:*);
                        by default only allowlisted registries and this
                        project's builds (repeatable)
  --swap TARGET=STANDIN
                        show the agent `STANDIN` in place of the masked file
                        `TARGET`, in every directory that has one
                        (repeatable), e.g. --swap .env.local=.env.dev. TARGET
                        must be a name the masks cover (.env, .env.*, *.pem,
                        *.key) and STANDIN must not be, so a swap can never
                        uncover a secret. Merged with the default
                        .env=.env.dev; a mapping for the same target replaces
                        it
  --observe-logs, --no-observe-logs
                        let the observe tier read the live stack's logs
                        (default: off; logs can hold secrets, and what the
                        agent reads goes to its model provider)
  --accept-history-secrets
                        register even though masked file names (.env, *.key,
                        ...) are in git history
  --i-understand-grant-exposure
                        register even though the grant holds secrets or live
                        data (a grant is for code; move them outside it or add
                        a mask)
  --unset KEY           drop a stored setting so the preset's value applies
                        again (repeatable); one of: compose, egress, podman,
                        caps, secrets, approvals, data, data_ro, data_rw,
                        extra_domains, compose_project, observe_logs,
                        dev_images, mask_skip, swap, mask, base_branch,
                        build_memory, profile
  --clear-data-ro       empty the data_ro list (the same as --unset data_ro)
  --clear-data-rw       empty the data_rw list (the same as --unset data_rw)
  --clear-extra-domains
                        empty the extra_domains list (the same as --unset
                        extra_domains)
  --clear-dev-images    empty the dev_images list (the same as --unset
                        dev_images)
  --clear-mask-skip     empty the mask_skip list (the same as --unset
                        mask_skip)
  --clear-mask          empty the mask list (the same as --unset mask)

examples:
  ab project set web --egress open
  ab project set web --preset analysis-offline
  ab project set web --unset egress --clear-extra-domains
```

### `ab project show`

Print a project's effective settings and whether each came from the preset or an override.

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

### `ab project check`

Read-only: run the grant scan a session runs, print the findings, and say whether they still match what was accepted. Exits 1 if a session would refuse.

```text
usage: ab project check [-h] name

show what the grant scan finds now and whether it matches what was accepted (read-only)

positional arguments:
  name        the registered project to scan

options:
  -h, --help  show this help message and exit

examples:
  ab project check web
```

### `ab project ls`

List the registered projects.

```text
usage: ab project ls [-h]

list registered projects (name, preset, root)

options:
  -h, --help  show this help message and exit

examples:
  ab project ls
```

### `ab project rm`

Remove projects from the registry; their files, state and backups are left untouched.

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

Wipe a project's agent state (memory, settings, transcripts), keeping a copy.

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

### `ab project purge`

```text
usage: ab project purge [-h] [--yes] name

delete a project's agent state and inbox outright (asks for --yes; the only command that deletes them)

positional arguments:
  name        the project whose agent state to delete (registered or not)

options:
  -h, --help  show this help message and exit
  --yes       go ahead and delete

examples:
  ab project purge web
  ab project purge web --yes
```

### `ab project accept-grant-exposure`

Record that you accept the secrets/live data grantcheck found in a project's grant.

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

Record that you accept (after rotating) the masked file names in a project's git history.

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

### `ab project selftest`

Spin up a throwaway session and prove the sandbox boundaries hold (posture, egress, mounts, podman scoping).

```text
usage: ab project selftest [-h] [--json] {claude,omp} name

prove the sandbox boundaries hold for a project (posture, egress, mounts, podman scoping), in a throwaway session

positional arguments:
  {claude,omp}  agent to run the checks as
  name          the registered project to test

options:
  -h, --help    show this help message and exit
  --json        print results as JSON instead of a table

examples:
  ab project selftest claude web
  ab project selftest claude web --json
```

## ab session

### `ab session`

The live runs — start one, attach to it, list, preview, kill and garbage-collect.

```text
usage: ab session [-h] {start,attach,ls,preview,kill,gc} ...

the live runs. `start` creates one in the foreground (one per project); every other verb names a project or a session id.

positional arguments:
  {start,attach,ls,preview,kill,gc}
    start               start an agent session in the foreground (one per
                        project)
    attach              re-attach a terminal to the session already running
                        for a project
    ls                  list sessions with their project, start time, age and
                        idle time
    preview             open a session's dev service on 127.0.0.1 (reach it
                        from your desktop with ssh -L)
    kill                tear down a session and everything it created (keeps
                        its logs and end-of-session scan)
    gc                  remove what dead sessions left (containers, networks,
                        sockets) and storage nothing can use: session-built
                        images, superseded personal image layers, unused
                        profile snapshots (runs automatically after boot)

options:
  -h, --help            show this help message and exit

examples:
  ab session start claude web
  ab session attach web
  ab session ls
  ab session preview web web:8000
  ab session kill web
  ab session gc
```

### `ab session start`

Start an agent session against a project in the foreground — build the sandbox, run the agent, tear it down on exit.

```text
usage: ab session start [-h] [--shell] [--no-tty] [--no-profile]
                        [--timeout SECONDS]
                        {claude,omp} project

start an agent session in the foreground (one per project)

positional arguments:
  {claude,omp}       the agent to run
  project            the registered project to run against; anything after a
                     `--` is passed through to the agent or shell

options:
  -h, --help         show this help message and exit
  --shell            run a bash shell in the sandbox instead of the agent
  --no-tty           do not allocate a TTY (for non-interactive or piped runs)
  --no-profile       for this run only: load no host-owned profile (skills,
                     sub-agents, commands, instructions, settings) and no
                     plugins, and use the harness image. agentbox's own
                     session notes still apply. `ab project set <p> --profile
                     off` makes it permanent
  --timeout SECONDS  time limit for a non-interactive run (default 3600;
                     ignored for interactive sessions)

examples:
  ab session start claude web
  ab session start claude web --shell         a bash shell in the sandbox instead of the agent
  ab session start claude web -- --resume     pass everything after -- to the agent
  ab session attach web                       re-attach to the session already running
```

### `ab session attach`

Re-attach a terminal to the session already running for a project (or a session id).

```text
usage: ab session attach [-h] project

re-attach a terminal to the session already running for a project

positional arguments:
  project     the project whose running session to attach to (one per
              project), or the session id itself

options:
  -h, --help  show this help message and exit

examples:
  ab session attach web
  ab session attach ab-claude-web-1   by session id
```

### `ab session ls`

List sessions with their project, start time, age and idle time.

```text
usage: ab session ls [-h]

list sessions with their project, start time, age and idle time

options:
  -h, --help  show this help message and exit

examples:
  ab session ls
```

### `ab session preview`

Publish one dev-stack service of a session on 127.0.0.1 so you can reach it over ssh -L.

```text
usage: ab session preview [-h] [--port PORT] [--stop] session [SERVICE:PORT]

open a session's dev service on 127.0.0.1 (reach it from your desktop with ssh -L)

positional arguments:
  session       the project whose session to preview, or the session id itself
                (`ab session ls` shows both)
  SERVICE:PORT  the dev-stack service (or container) and the port inside it,
                e.g. web:8000; omit with --stop

options:
  -h, --help    show this help message and exit
  --port PORT   host port to publish on 127.0.0.1 (default: a free one,
                printed on start)
  --stop        remove this session's previews instead of opening one

examples:
  ab session preview web web:8000
  ab session preview web web:8000 --port 18080
  ab session preview web --stop
```

### `ab session kill`

Tear down a session and everything it created, keeping its logs and end-of-session scan.

```text
usage: ab session kill [-h] session

tear down a session and everything it created (keeps its logs and end-of-session scan)

positional arguments:
  session     the project whose session to tear down, or the session id itself
              (`ab session ls` shows both)

options:
  -h, --help  show this help message and exit

examples:
  ab session kill web
  ab session kill ab-claude-web-1   by session id
```

### `ab session gc`

Remove containers, networks and sockets left by dead sessions (also runs after boot).

```text
usage: ab session gc [-h]

remove what dead sessions left (containers, networks, sockets) and storage nothing can use: session-built images, superseded personal image layers, unused profile snapshots (runs automatically after boot)

options:
  -h, --help  show this help message and exit

examples:
  ab session gc
```

## ab screen

### `ab screen`

Open the tmux workspace: agent panes (each an `ab session start`) and host shells, in a layout from ~/.config/agentbox/screen.toml.

```text
usage: ab screen [-h] [--agent {claude,omp}] [--layout LAYOUT] [--config FILE]
                 [--list] [--print-config] [--dry-run]
                 [builder] [reviewer]

open the tmux workspace: agent panes (each an `ab session start`) and host shells, in a layout from ~/.config/agentbox/screen.toml

positional arguments:
  builder               builder project (a registered project name)
  reviewer              reviewer project (a separate clone, registered too)

options:
  -h, --help            show this help message and exit
  --agent {claude,omp}  the agent the agent panes start (default: claude; a
                        pane's own `agent` key wins)
  --layout LAYOUT       layout name (default: from the config, usually auto)
  --config FILE         config file (default: <config dir>/screen.toml)
  --list                list layouts, and which one auto would pick
  --print-config        print a commented starter config
  --dry-run             print the tmux commands, run nothing

examples:
  ab screen web
  ab screen web web-review --layout wide
  ab screen --list
  ab screen --print-config > ~/.config/agentbox/screen.toml
```

## ab profile

### `ab profile`

```text
usage: ab profile [-h] {harvest,promote,install,dismiss} ...

The host-owned profile (~/.config/agentbox/<agent>/profile/) holds the skills, sub-agents, commands and instructions every session loads, read-only, in every project and under every preset. Sessions cannot write it; what they want to keep goes to the project's inbox, which nothing loads. These commands review that queue.

positional arguments:
  {harvest,promote,install,dismiss}
    harvest             list what sessions have put in their inboxes,
                        unreviewed first (read-only)
    promote             copy an inbox item into the profile and commit it,
                        with its provenance in the commit message
    install             install the plugins the profile declares, in a
                        sandbox, into a tree sessions mount read-only
    dismiss             mark an inbox item reviewed and leave it where it is
                        (deletes nothing)

options:
  -h, --help            show this help message and exit

examples:
  ab profile harvest
  ab profile harvest --agent claude --all
  ab profile promote web/skills-deploy --as skills/deploy
  ab profile dismiss web/notes.md
```

### `ab profile harvest`

```text
usage: ab profile harvest [-h] [--agent {claude,omp}] [--all]

list what sessions have put in their inboxes, unreviewed first (read-only)

options:
  -h, --help            show this help message and exit
  --agent {claude,omp}  only this agent's profile (default: both)
  --all                 include items already reviewed

examples:
  ab profile harvest
  ab profile harvest --agent claude --all
```

### `ab profile promote`

```text
usage: ab profile promote [-h] [--agent {claude,omp}] [--as PATH] item

copy an inbox item into the profile and commit it, with its provenance in the commit message

positional arguments:
  item                  the item, as <project>/<name> (or just <name> when
                        unambiguous)

options:
  -h, --help            show this help message and exit
  --agent {claude,omp}  whose profile (default: claude)
  --as PATH             where it goes inside the profile, e.g. skills/deploy
                        (required unless the item is plainly a skill
                        directory)

examples:
  ab profile promote web/skills-deploy --as skills/deploy
```

### `ab profile dismiss`

```text
usage: ab profile dismiss [-h] [--agent {claude,omp}] item

mark an inbox item reviewed and leave it where it is (deletes nothing)

positional arguments:
  item                  the item, as <project>/<name> (or just <name> when
                        unambiguous)

options:
  -h, --help            show this help message and exit
  --agent {claude,omp}  whose profile (default: claude)

examples:
  ab profile dismiss web/notes.md
```

## ab image

### `ab image`

```text
usage: ab image [-h] {build} ...

A personal layer for what you want in every session -- a language toolchain, a linter, a private CLI -- without it going into agentbox's own images, and without a session being able to put it there. Write a Containerfile in your profile (~/.config/agentbox/<agent>/profile/Containerfile); it must begin `ARG BASE` then `FROM ${BASE}`, and `ab image build` supplies BASE as the current harness image.

The built image is checked before it is tagged: USER, ENTRYPOINT, CMD and PATH must match the harness image, and the image must still report the pinned agent version and have git, rg, python3 and the podman client. A build that fails any of those is discarded, so what you were using stays in place.

The tag covers the harness image ID and the Containerfile. When the harness image is rebuilt your layer no longer matches it, and `ab session start` says `personal layer out of date` and runs the plain harness image -- it never runs a layer built on an older harness. Build it again and the next session picks it up.

positional arguments:
  {build}
    build     build your profile's Containerfile on top of the agent's harness
              image

options:
  -h, --help  show this help message and exit

examples:
  ab image build claude
  ab image build omp --force
```

### `ab image build`

```text
usage: ab image build [-h] [--force] {claude,omp}

build your profile's Containerfile on top of the agent's harness image

positional arguments:
  {claude,omp}  whose harness image to build on

options:
  -h, --help    show this help message and exit
  --force       rebuild even when an image for this harness and Containerfile
                already exists

examples:
  ab image build claude
  ab image build omp --force
```

## ab keys

### `ab keys`

Manage model tokens and provider API keys; they live outside every grant and reach a session read-only as AB_KEY_<PROVIDER>.

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

Replace the Claude subscription token.

```text
usage: ab keys rotate [-h] {claude,omp}

replace the Claude subscription token (from `claude setup-token`)

positional arguments:
  {claude,omp}  the agent whose subscription token to replace (currently:
                claude)

options:
  -h, --help    show this help message and exit

examples:
  ab keys rotate claude
```

### `ab keys set`

Set a provider's API key (use a dedicated, spend-capped key).

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

List providers that have a key (names only, never values).

```text
usage: ab keys ls [-h]

list providers that have a key (names only, never values)

options:
  -h, --help  show this help message and exit

examples:
  ab keys ls
```

### `ab keys rm`

Remove a provider's key file.

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
