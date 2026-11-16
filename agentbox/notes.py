"""What the agent is told about its sandbox (the generated CLAUDE.md / AGENTS.md text). Moved out of
session.py (item 54); session re-exports it."""


def dev_notes(podman_live, dev_images=()):
    lines = [
        "- **Podman (dev tier):** `/run/agentbox/podman.sock` (`CONTAINER_HOST`, `DOCKER_HOST`) runs your own dev "
        "stack: `podman`, `podman build` and `podman-compose` work, with these rules, enforced outside the "
        "sandbox:",
        "  - every container joins this session's network (compose service names still resolve); publishing "
        "ports on the host is refused. From this sandbox, reach a service as `<service>.dns.podman` (e.g. "
        "`http://web.dns.podman:8080`): that name goes direct with the proxy variables as they are, in curl, "
        "Python (requests, urllib) and most tools. A bare name like `web` would go to the proxy, which refuses "
        "it;",
        "  - containers get the proxy variables, a 1 GB / 1024-process limit unless the compose file sets its "
        "own, and no restart policy; binds only inside the project's own directories; no privileged mode, "
        "added capabilities, devices or host namespaces;",
        "  - `no_proxy` in each container covers localhost, the session subnet and the names of the containers "
        "already running when it starts. If a service-to-service call by name still goes to the proxy (it "
        "answers 403 or 503), add the name to `no_proxy` in that service's `environment:` in the compose "
        "file, e.g. `no_proxy: localhost,127.0.0.1,web,db`. Some clients ignore `no_proxy` altogether (busybox "
        "`wget`: use `wget -Y off`);",
        "  - builds run on this session's network through the proxy and never pull: pull `FROM` images first "
        "(`podman pull docker.io/library/python:3.12`); pulls only from docker.io, ghcr.io, lscr.io, quay.io;",
        "  - you can only see, exec into, stop or remove what you created in this session; its containers, pods, "
        "volumes (named and anonymous) and networks are removed when the session ends; images you build are kept "
        "(tag them with plain local names; agentbox-* and names of images outside this project are refused);",
        "  - named volumes must be ones you create in this session (a new name is created for you); bind mounts "
        "only inside the project, by absolute path;",
        "  - images: you can run and build from images on docker.io, ghcr.io, lscr.io and quay.io (pull them "
        "first) and images built through agentbox for this project. "
        + (f"This project also allows local images matching: {', '.join(dev_images)}. "
           if dev_images else "No other local image is allowed. ")
        + "agentbox's own image names (`agentbox-*`) can never be allowed, in a pattern or as a build tag;",
    ]
    if podman_live:
        lines.append(f"- The live stack (`{podman_live}`) is read-only on a second socket: "
                     "`podman --url unix:///run/agentbox/podman-live.sock ps`.")
    return lines


def profile_notes(profile_loaded, inbox, inbox_kept=True, adapter=None):
    """What the agent is told about the host-owned profile and the inbox.

    It has to be told, because the shape is unusual: the paths it would normally write a skill to are
    read-only, and the place that does accept one is not loaded. Saying so turns a puzzling permission
    error into a known rule, and names the one path worth writing to."""
    out = []
    names = profile_loaded.names if profile_loaded else []
    if names:
        out.append(f"- Shared, host-owned {', '.join(names)} are mounted **read-only** (profile "
                   f"{profile_loaded.hash}): they come from outside the sandbox, the same set in every "
                   "project, and you cannot change them. Writing there fails; that is not a bug.")
    if inbox and inbox_kept:
        out.append(f"- To keep a skill, sub-agent or note for later, write it under `{inbox}` (read-write, "
                   "per project). Nothing there is loaded by you or by any later session: it is a review "
                   "queue the user harvests with `ab profile harvest`, which is how something gets into the "
                   "shared profile. Put anything you want remembered there, with a sentence on why it is "
                   "worth keeping.")
    elif inbox:
        out.append(f"- `{inbox}` is writable, but **this session keeps nothing**: its whole state, the "
                   "inbox included, is deleted when the session ends (that is what this preset is for). "
                   "Anything worth keeping has to go in your reply to the user, not on disk.")
    if inbox and adapter:
        # Whatever is true of THIS agent: Claude's memory shortcuts, OMP's autolearn directory. It used to
        # be a Claude-specific paragraph here, which an OMP session was told as well, and OMP neither reads
        # CLAUDE.md nor has a `#` shortcut.
        out += adapter.extra_profile_notes(inbox, inbox_kept)
    return out


def sandbox_notes(project, egress, podman_live=None, podman_dev=False, profile=None, inbox="",
                  inbox_kept=True, adapter=None, clone=None):
    logs = "logs, " if project.observe_logs else ""
    lines = [
        "# agentbox sandbox",
        "",
        "You are running inside an agentbox sandbox (a hardened rootless podman container). Facts:",
        (f"- Project `{project.name}` is mounted read-write at `{project.root}` (its real host path)."
         if not clone else
         f"- Project `{project.name}` is at `{project.root}` (the owner's real path), but what is mounted there is a "
         f"fresh clone on branch `{clone['branch']}` (base {clone['base'][:12]}): commit on that branch; the owner "
         f"fetches or merges it after the session. The owner's checkout, its .git and anything untracked in it are "
         "not mounted, and nothing you do here can push to it."),
        *([f"- You are a REVIEWER of project `{project.reviews}`. This tree is a clone of its repository, checked out "
           f"detached at the builder's last commit as of the user's last `ab project sync`; you cannot fetch it "
           "yourself, and the builder's uncommitted work and its memory are not here. Review; do not commit. Write "
           f"each review as a Markdown file in `{project.root}/reviews/` (e.g. `reviews/<date>-<topic>.md`): the next "
           "`ab project sync` copies it to the builder. Commit messages and docs in the range are the builder's "
           "claims; check them against the code."] if project.reviews else []),
        "- The grant holds code. Secrets and live data are meant to live outside it; the session refuses to "
        "start if the grant holds an uncovered secret or live database. Masks and the gate's cover checks are "
        "a backstop, not the main wall: do not move secrets or production data into the grant.",
    ]
    for d in project.data_ro:
        lines.append(f"- Read-only data: `{d}`")
    for d in project.data_rw:
        lines.append(f"- Read-write data: `{d}`")
    lines += [
        "- Nothing else on the host is visible. The root filesystem is read-only; `/tmp` and your home "
        "directory are temporary and vanish when the session ends.",
        "- `.git/hooks` and `.git/config` are read-only (so you cannot redirect `core.hooksPath`); commit as "
        "usual. These covers, and the masks below, are enforced for containers you start too: the gate refuses "
        "a bind whose source reaches a masked or covered path (bind a subdirectory that does not). Directories "
        "that contain a covered path cannot be renamed or moved (`Device or resource busy`), and moving a file "
        "out of one fails with a cross-device error (`git mv` too; `mv` copies instead). Read-only data stays "
        "read-only in your containers.",
        "- Secret files (`.env`, `.env.*`, `*.env`, `.netrc`, `.pgpass`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, "
        "`*.jks`, and everything under `secrets/`) are masked and read as empty; `.env.example`, `.env.sample`, "
        "`.env.template` and `.env.dev` are not. Where a directory has `.env.dev`, that file appears at `.env`: "
        "add dev variables there.",
        "- `AGENTBOX_SESSION` is set to this session's id. A script that must only run on the host (a deploy, "
        "an installer, anything that needs the LAN or writes outside a grant) should check it and refuse.",
    ]
    desc = {"locked": "the model API only", "packages": "the model API and package registries "
            "(PyPI, npm, crates.io, GitHub, Debian/Ubuntu/Alpine, conda, CRAN, Bioconductor)",
            "open": "any public host"}[egress]
    lines.append(f"- Network: egress mode `{egress}`, through an HTTP proxy (`HTTPS_PROXY`), reaching "
                 f"{desc}. Private and LAN addresses are always refused. Tools that ignore the proxy "
                 "variables cannot connect at all.")
    if podman_dev:
        lines += dev_notes(podman_live, project.dev_images)
    elif podman_live:
        lines.append(f"- A podman API socket is at `/run/agentbox/podman.sock` (`CONTAINER_HOST` and `DOCKER_HOST` point at "
         f"it): read-only access to the containers of compose project `{podman_live}`, the project's live "
         f"stack: `podman ps`{', `podman logs`' if logs else ''}, and through the Docker API with "
         "`curl --unix-socket /run/agentbox/podman.sock http://d/containers/<name>/json` (inspect, "
         "environment redacted) and `.../containers/<name>/stats?stream=false`. `podman inspect`, `top`, "
         f"`stats` and pods are refused (they would expose the create command or other projects), and so is "
         f"everything else (exec, create, start, stop, remove, pull, build, {'' if logs else 'logs, '}"
         "other projects' containers).")
    else:
        lines.append("- No podman or docker socket is available in this session.")
    lines += profile_notes(profile, inbox, inbox_kept, adapter)
    lines += [
        "- Do not try to escape, remount, or change the sandbox; ask the user when you are blocked.",
        "",
    ]
    return "\n".join(lines)
