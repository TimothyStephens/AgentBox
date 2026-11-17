# `ab deploy` — proposal (draft, 17 Nov 2026)

Status: **decided 17 Nov (D1-D5 as recommended, decisions.md); being built in the order below.** Once the decisions below are made, the agreed parts move into
`docs/design.md` ("Preview and deploy", build step 6) and this file is deleted.

## What design.md already fixes

`ab deploy <project> --lan 10.0.0.103:<port>`: validate the compose file (binds inside the project, no
privileged or host networking, ports only on the address named), check the real `.env` has every variable
name in `.env.dev` (names only, never values), show the diff since the last deploy, ask for confirmation,
start the live `<project>` stack with a restart policy, "through a sockguard deploy profile, never the raw
socket". It also checks lingering and `podman-restart.service` and warns if either is missing.

## Why this is not a YAML check plus `podman-compose up`

The compose file, the Containerfile and the code are agent-written. `ab deploy` is the moment that code starts
running on the LAN with real secrets and real data. A YAML check before an `up` on the raw socket leaves gaps:
- podman-compose features the checker does not model (`x-podman`, `podman_args`, extends, profiles,
  variable substitution with the real `.env`);
- build steps (`FROM` any image, `RUN --mount`);
- a file changed between the review and the `up`.

The gate already enforces a policy on every API call podman-compose makes for the dev tier, including builds.
Deploy is a second policy on the same machinery.

## The proposal

1. **Host only.** Refused inside a session (`AGENTBOX_SESSION`), like `ab profile install`.
2. **What is deployed is a commit, not the working tree.**
   - The project's root must be a git repo with a clean tracked tree.
   - The deploy copies `git archive <commit>` into a deploy staging directory, plus the real `.env` (and any
     other untracked file the user names in a per-project `deploy_files` list).
   - Everything is built and started from that copy, so nothing the agent does after the diff was shown can
     change what runs.
3. **The diff.**
   - The diff shown is `git diff <last deployed commit>..<commit>`. For the first deploy, the whole tree's
     file list, with `compose.yaml` and every `Containerfile` shown in full.
   - `compose.yaml`, `Containerfile*`, lockfiles and `.env.dev` variable names are listed first.
   - Then a y/N prompt. `--yes` skips it only when stdin is not a terminal and the diff is empty, i.e. a
     redeploy of the same commit.
4. **The `.env` check.**
   - Every name in `.env.dev` must be in `.env`, or the deploy is refused and the missing names are listed.
   - Names in `.env` that are not in `.env.dev` are listed as a note.
   - Values are never read into output or logs.
5. **The run.**
   - A short-lived runner container (the `agentbox-base` image, which has `podman-remote` and
     `podman-compose`) runs `podman-compose -p <compose_project> up -d --build --remove-orphans` against the
     staging copy.
   - Its only podman access is a gate in **deploy mode** on a per-deploy podman service, the same shape as a
     session's dev tier.
   - The runner reaches no network except through that gate; builds use the gate's build path.
6. **The deploy-mode gate policy.** It differs from the dev tier in what a live stack needs:

   | | dev tier (today) | deploy mode |
   |---|---|---|
   | Compose project / labels | `<project>-dev`, session-owned | `<compose_project>`, `agentbox.deploy=<project>` |
   | Networks | pinned to the session's internal network | the stack's own networks, normal egress; never host, `ns:`, `container:` |
   | Published ports | refused | only `HostIp` = the `--lan` address, only the project's allowed host ports |
   | Binds | inside the grants, covers enforced | inside the staging copy only, plus a per-project `deploy_binds` allowlist (a media library, a data dir) |
   | Named volumes | only ones the session created | the stack's own (`<compose_project>_*`), created or reused |
   | Restart policy | stripped | forced to `unless-stopped` |
   | Privileged, caps, devices, host namespaces, security opts | refused | refused (unchanged) |
   | Images | built ones and allowlisted | the same build-path checks: `FROM` registries, no `localhost/` image from another project |
   | Removal | own containers only | the stack's own containers (label-scoped), for `--remove-orphans` and recreation |
   | cgroup | agentbox.slice | the host default, not agentbox.slice (a live service should not be OOM-killed with sessions) |

7. **The record.**
   - Each deploy appends `{time, commit, compose_project, lan, image ids, result}` to
     `projects/<name>/deploys.jsonl`.
   - "The diff since the last deploy" reads it.
   - A failed deploy leaves what podman-compose left running and says so; there is no automatic rollback.
     `ab deploy <p> --commit <sha>` is the manual one.
8. **Settings (projectkeys).**
   - `deploy_ports` (host ports; `--lan addr:port` adds one).
   - `deploy_binds` (absolute host paths, validated like `data_ro`: not inside another project, not the
     backups directory).
   - `deploy_files` (untracked files copied into the staging copy; `.env` is always copied).
   - `lan_addr` (stored from `--lan`, so later deploys need only `ab deploy <p>`).
9. **Who may deploy.** Projects with a `compose.yaml` at the root; any preset. Lockdown has no compose access
   in sessions, but deploy is the user's own act.

## Decisions needed

| # | Question | Recommendation |
|---|---|---|
| D1 | Enforcement: deploy-mode gate on every API call, or a static check of `podman-compose config` then the raw socket | Gate. The static check misses build steps and podman-compose extras |
| D2 | Deploy a commit (clean tree, `git archive` copy) or the working tree | Commit: the diff you review is what runs, and the agent cannot change it afterwards |
| D3 | Live stack egress: the stack's own networks (normal internet), or through a squid proxy like a session | Normal networks. It is a service you chose to run; the review is the control. Squid can be a later per-project option |
| D4 | Binds outside the project: refused, or allowed from a per-project `deploy_binds` list | Allowlist: a live stack often needs a data or media path |
| D5 | cgroup: the host default, or agentbox.slice | Host default: a live service should not compete with sessions' cap |

## Size and order

- **Large.** It is a second policy in the gate (security-reviewed code), a runner container, a per-deploy podman
  service, the staging copy, the record, four settings, the CLI and tests.
- **Proposed order**, each step committed with its tests:
  1. the `.env` and diff checks and the staging copy (no podman);
  2. the deploy-mode gate policy (unit tests against the fake podman);
  3. the runner and the end-to-end path;
  4. an itest that deploys a two-service stack and checks that each refusal bites (privileged, a bind outside,
     a port on 0.0.0.0, host networking, a foreign volume);
  5. design.md and USAGE.
- **Verification:** `make itest` on the host, then one real deploy, which is yours to run.
