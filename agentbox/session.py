"""Sessions: build, start and tear down everything one agent session needs."""
import errno
import json
import os
import shutil
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

from . import adapters, backup, cgroup, egress, grantcheck, hardening, mounts, paths, presets, profile, projectkeys, projectlock, registry, tokens
from . import keys as keysmod
from . import buildcap, builtrec, scan, sockguard, userimage
from .config import HOME
from .errors import AgentboxError
from .podman import Podman, set_log

TOKEN_PATH = "/run/agentbox/token"
PROBE_PATH = "/run/agentbox/probe.sh"
LIVE_SOCKET = "/run/agentbox/podman-live.sock"
PKG_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Project:
    name: str
    root: str
    data_ro: list
    data_rw: list
    mask_skip: list
    extra_domains: list
    swap: dict
    eff: dict                      # setting -> (value, source)
    build_memory: int = 2 << 30    # bytes; the gate's per-build limit (step 5)
    history_secrets_accepted: list = field(default_factory=list)
    # the accepted findings' signatures, or (from before those were recorded) their fingerprint string
    grant_exposure_accepted: object = ""
    compose_project: str = ""      # the live stack's compose project (default: the project name)
    observe_logs: bool = False     # observe tier may read the live stack's logs (off: they can hold secrets)
    preset: str = ""
    dev_images: list = field(default_factory=list)   # extra image name patterns the dev tier may run/build from
    compose_project_explicit: bool = False
    profile: str = "on"           # "off": this project's sessions load no host profile and no plugins
    base_branch: str = ""         # the branch the work lands on; the scan names its new commits

    def value(self, key):
        return self.eff[key][0]


def _no_commas(path, what):
    if "," in path or "\n" in path:
        raise AgentboxError(f"{what} {path!r}: paths with commas or newlines cannot be mounted safely")


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def protected_extra(settings):
    from .keys import keys_dir
    return [settings.config_dir, settings.data_dir, settings.run_dir, keys_dir(settings)]


def load_project(settings, name):
    reg = registry.load(settings.registry_path)
    if name not in reg:
        raise AgentboxError(f"no project named {name!r}; add it with `ab project add`")
    # Exactly what `ab project set` validates before saving (audit F11): unknown keys and every stored
    # value, through the settings table's own validators.
    p = projectkeys.validate_entry(name, reg[name], settings=settings)
    eff = presets.effective(name, p, presets.load_presets(settings.presets_dir))
    kw = dict(home=HOME, extra_protected=protected_extra(settings))
    root = paths.check_grant(p.get("root", ""), kind=f"project {name} root", **kw)
    data_ro = [paths.check_grant(d, kind="data_ro", **kw) for d in p.get("data_ro", [])]
    data_rw = [paths.check_grant(d, kind="data_rw", **kw) for d in p.get("data_rw", [])]
    for g in [root] + data_ro + data_rw:
        _no_commas(g, "grant")
    # A hand-written overlap is caught here too, not only where it was registered (audit F3).
    paths.check_overlaps(name, {"root": root, "data_ro": data_ro, "data_rw": data_rw}, reg)
    return Project(name=name, root=root, data_ro=data_ro, data_rw=data_rw,
                   mask_skip=list(p.get("mask_skip", [])), extra_domains=list(p.get("extra_domains", [])),
                   swap=mounts.effective_swap(_check(name, "swap", p.get("swap", {}))), eff=eff,
                   build_memory=cgroup.parse_size(
                       _check(name, "build_memory", p.get("build_memory", settings.build_memory),
                              settings=settings)),
                   history_secrets_accepted=list(p.get("history_secrets_accepted", [])),
                   grant_exposure_accepted=p.get("grant_exposure_accepted") or "",
                   compose_project=_check(name, "compose_project", p.get("compose_project", name)),
                   observe_logs=_check(name, "observe_logs", p.get("observe_logs", False)),
                   preset=str(p.get("preset") or ""),
                   dev_images=_check(name, "dev_images", p.get("dev_images", [])),
                   profile=_check(name, "profile", p.get("profile", "on")),
                   base_branch=_check(name, "base_branch", p["base_branch"]) if p.get("base_branch") else "",
                   # Explicit only when it names a DIFFERENT stack (audit F-4): storing the value the
                   # project would have had anyway waived the ownership check while changing nothing.
                   compose_project_explicit=bool(p.get("compose_project")
                                                 and p["compose_project"] != name))


def _check(name, key, value, **kw):
    """One stored value, through its own validator in the settings table (projectkeys.KEYS)."""
    return projectkeys.validate_value(key, f"project {name}", value, **kw)


def podman_access(project):
    """The compose project the session may observe, or None. (The dev tier is `dev_tier`.) The exec tier on
    the live stack is refused: the gate scopes exec to the session's own dev containers only."""
    podman = project.value("podman") or {}
    tier, live = podman.get("tier", "none"), podman.get("live")
    if "exec" in (tier, live):
        raise AgentboxError(f"project {project.name}: exec on the live stack is not built; use observe for the "
                            "live stack, and the dev tier for a stack of the agent's own")
    if "observe" in (tier, live):
        return project.compose_project
    return None


def dev_tier(project):
    return (project.value("podman") or {}).get("tier") == "dev"


def _history_masked(git_dir, timeout):
    """Paths in one repo's history (all refs) whose names the masks cover. Raises if git cannot tell."""
    try:
        p = subprocess.run(["git", "--no-pager", f"--git-dir={git_dir}", "-c", "core.fsmonitor=false", "log",
                            "--all", "--format=", "--name-only", "--diff-filter=AM", "--no-renames"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AgentboxError(f"cannot read the git history of {git_dir} ({e}); refusing rather than guessing")
    if p.returncode != 0 and "does not have any commits" not in p.stderr:
        raise AgentboxError(f"cannot read the git history of {git_dir}: {p.stderr.strip()[-200:]}")
    names = sorted({line for line in p.stdout.splitlines() if line})
    return [n for n in names if mounts._masked(os.path.basename(n))
            or any(part in mounts.MASK_DIRS for part in n.split("/")[:-1])]


def history_secrets(root, grants, mask_skip=(), timeout=120):
    """Masked file names found in the history of every git repo the session can read: the masks hide the
    working tree, never git's objects. Names are shown relative to the project root (`<repo>/<path>` for a
    nested repo). Raises if a history cannot be read.

    It took the project's `swap` map until the swap targets stopped being part of "is this name masked?"
    (every target must now be a masked name anyway, audit F-12), so the parameter is gone rather than left
    to read as if it still narrowed the search."""
    found = set()
    for g in grants:
        dirs, _outside = mounts.find_git_dirs(g, mounts._rel_skips(root, g, mask_skip))
        for gd in dirs:
            if not any(gd == x or gd.startswith(x.rstrip("/") + "/") for x in grants):
                continue                                    # not mounted, so not readable
            work = os.path.dirname(gd) if os.path.basename(gd) == ".git" else gd
            prefix = os.path.relpath(work, root)
            for n in _history_masked(gd, timeout):
                found.add(n if prefix == "." else os.path.join(prefix, n))
    return sorted(found)


def history_secrets_changes(found, accepted):
    """(new, gone) between a stored history-secrets acknowledgement and what is in the history now."""
    return ([n for n in found if n not in accepted], [n for n in accepted if n not in found])


def history_secrets_message(project, names, accepted=()):
    shown = ", ".join(names[:15]) + (f" (and {len(names) - 15} more)" if len(names) > 15 else "")
    tail = ""
    if accepted:
        new, gone = history_secrets_changes(names, accepted)
        parts = ([f"new since the acknowledgement: {', '.join(new)}"] if new else []) + \
                ([f"no longer in the history: {', '.join(gone)}"] if gone else [])
        tail = " (" + "; ".join(parts) + ")" if parts else ""
    return (f"project {project}: {len(names)} masked file name(s) are in git history: {shown}{tail}. The agent can "
            "read them through git (`git log -p`, `git show`) even though the working-tree files are masked. "
            "Treat these secrets as exposed and rotate them (and consider rewriting the history). To go "
            "ahead once they are rotated, accept them with --accept-history-secrets "
            f"(`ab project accept-history-secrets {project}` for a registered project)")


def git_identity(settings, agent):
    """Author and committer for commits made in the sandbox, as environment variables only; no git config
    file enters the sandbox. The committer is "<name> via agentbox-<agent>" so agent commits stand out."""
    out = {}
    for key, setting in (("NAME", "git_name"), ("EMAIL", "git_email")):
        val = getattr(settings, setting)
        if not val:
            try:
                val = subprocess.run(["git", "config", "--global", f"user.{key.lower()}"], capture_output=True,
                                     text=True, timeout=10).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                val = ""
        if val and "\n" not in val:
            out[f"GIT_AUTHOR_{key}"] = out[f"GIT_COMMITTER_{key}"] = val
    if "GIT_COMMITTER_NAME" in out:
        out["GIT_COMMITTER_NAME"] = f"{out['GIT_COMMITTER_NAME']} via agentbox-{agent}"
    return out


def labels_for(settings, project, session, role):
    out = dict(settings.labels)
    out.update({"agentbox.session": session, "agentbox.project": project,
                "agentbox.owner": f"{project}/{session}", "agentbox.role": role})
    return out


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
                  inbox_kept=True, adapter=None):
    logs = "logs, " if project.observe_logs else ""
    lines = [
        "# agentbox sandbox",
        "",
        "You are running inside an agentbox sandbox (a hardened rootless podman container). Facts:",
        f"- Project `{project.name}` is mounted read-write at `{project.root}` (its real host path).",
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
        "- Secret files (`.env`, `.env.*`, `*.pem`, `*.key`, `secrets/`) are masked and read as empty. Where a "
        "directory has `.env.dev`, that file appears at `.env`: add dev variables there.",
        "- `AGENTBOX_SESSION` is set to this session's id. A script that must only run on the host (a deploy, "
        "an installer, anything that needs the LAN or writes outside a grant) should check it and refuse.",
    ]
    if egress == "none":
        lines.append("- There is no network access in this session.")
    else:
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


class Session:
    def __init__(self, settings, agent, project, *, podman=None, warn=None, no_profile=False):
        self.settings = settings
        self.adapter = adapters.get(agent)
        self.project = project
        self.podman = podman or Podman(settings.podman)
        self.warn = warn or (lambda m: print(m, file=sys.stderr))
        self.no_profile = bool(no_profile)      # `ab run --no-profile`, for this run only
        self.name = None
        self.plan = None
        self.networks = []
        self.env = {}
        self.setup = adapters.SessionSetup()
        self.podman_live = None          # compose project the agent may observe through sockguard
        self.podman_dev = False          # the dev tier: a stack of the agent's own, through the gate
        self.started = time.time()       # reset when the session is reserved; names its kept logs
        self.egress = "none"
        set_log(os.path.join(settings.data_dir, "logs", "ab.log"))

    # ---- naming -----------------------------------------------------------------
    def reserve_name(self):
        base = f"{self.settings.name_prefix}{self.adapter.name}-{self.project.name}-"
        os.makedirs(os.path.join(self.settings.run_dir, "sessions"), mode=0o700, exist_ok=True)
        taken = set(self.podman.ids("container", {"agentbox.project": self.project.name, **self.settings.labels}))
        for n in range(1, 1000):
            cand = f"{base}{n}"
            if any(t == cand or t.startswith(cand + "-") for t in taken):
                continue
            try:
                os.mkdir(self.settings.session_run_dir(cand), 0o700)
            except FileExistsError:
                continue
            self.name = cand
            return cand
        raise AgentboxError("no free session number")

    @property
    def profile_off(self):
        """(off?, why): whether this session loads the host-owned profile, and which switch decided.

        Two switches, because they answer different questions: `--no-profile` is "not this run" (debugging a
        skill, reproducing something without the user's instructions) and `profile = "off"` on the project is
        "not this project, ever". The flag is reported when both say off -- it is the one just typed."""
        if self.no_profile:
            return True, "flag"
        if self.project.profile == "off":
            return True, "project"
        return False, ""

    _image = None                      # set by _prepare_reserved, which also reports a stale layer

    @property
    def image(self):
        """The container image to run: the harness image, or the user's own layer when one is current.

        Resolved once during `prepare` (it asks podman), so reading this is free and every reader -- the
        hardening audit, the argv, the CLI-version check -- sees the same answer."""
        return self._image or self.adapter.image

    def _resolve_image(self):
        """(image to run, note to report or "").

        A user image layer is built on top of the harness image and is profile content like a skill is, so
        `--no-profile` selects the harness image: "no profile" that still ran the user's own build of the
        agent would be a lie.

        With the profile on, the layer is used only when one is built on EXACTLY this harness image from
        exactly this Containerfile (`profile.user_image_ref` keys the tag on both). When the harness image
        has moved on, nothing is tagged under the new key, and the session runs the plain harness image and
        says so -- it never runs a layer built on an older harness, which is the one thing a layer cannot be
        trusted to still fit."""
        off, _why = self.profile_off
        if off:
            return self.adapter.image, ""
        src = self.profile.dir or self.settings.profile_src(self.adapter.name)
        recipe = profile.containerfile(src)
        if recipe is None:
            return self.adapter.image, ""           # no recipe: no layer is expected, nothing to report
        harness_id, _pin = userimage.harness_pin(self.podman, self.adapter.image)
        ref = profile.user_image_ref(self.adapter.name, harness_id, recipe)
        if self.podman.image_exists(ref):
            return ref, ""
        # "Out of date" is only true of a layer that EXISTS and no longer matches. A Containerfile that has
        # never been built is a different thing to say, and saying the wrong one sends the reader looking
        # for an image that was never there (seen in `ab selftest` output, 4 Nov).
        if userimage.any_built(self.podman, self.adapter.name):
            return (self.adapter.image,
                    f"personal layer out of date: `ab image build {self.adapter.name}`. "
                    f"This session runs {self.adapter.image} instead -- a layer built on an older harness "
                    f"image is never run.")
        # The SOURCE path, not the snapshot's: the snapshot is an internal copy, and the file the reader
        # has to go and look at (or delete) is the one in the profile.
        own = os.path.join(self.settings.profile_src(self.adapter.name), profile.CONTAINERFILE)
        return (self.adapter.image,
                f"{own} describes a personal image layer, but none has been built: "
                f"`ab image build {self.adapter.name}`. This session runs {self.adapter.image}.")

    @property
    def state_dir(self):
        """The project's agent state (memory, settings, transcripts), kept across sessions. Lockdown sessions
        start from empty state every time: theirs is per session and removed at teardown (Tim, 2 Oct)."""
        if self.project.preset == "lockdown" and self.name:
            return os.path.join(ephemeral_state(self.settings, self.name), self.adapter.name)
        return self.settings.state_dir(self.project.name, self.adapter.name)

    @property
    def token_file(self):
        return self.settings.token_file(self.adapter.name) if self.adapter.token_env else None

    # ---- host-side preparation ------------------------------------------------------
    def prepare(self, *, do_backup=True):
        cgroup.check(self.settings, self.warn)
        if self.token_file:
            tokens.check_token_file(self.token_file)
        p = self.project
        found = history_secrets(p.root, [p.root] + p.data_rw + p.data_ro, p.mask_skip)
        # The whole found set, not just the additions (audit F-11): a name that left the history means the
        # acknowledgement no longer describes it, and `ab project show` was still listing it to rotate.
        new = found if grantcheck.fingerprint(found) != grantcheck.fingerprint(
            p.history_secrets_accepted) else []
        if new:
            raise AgentboxError(history_secrets_message(p.name, new, p.history_secrets_accepted))
        if found:
            self.warn(f"agentbox: {len(found)} accepted secret file name(s) in {p.name}'s git history "
                      "are readable by the agent (accepted as rotated)")
        self.reserve_name()
        try:
            self._prepare_reserved(do_backup)
        except BaseException:
            self.teardown()                  # nothing is running yet; remove the run dir it reserved
            raise

    profile = None                       # set by _prepare_reserved (profile.Loaded)

    @property
    def mask_file(self):
        return os.path.join(self.settings.session_run_dir(self.name), "mask-empty") if self.name else None

    def _prepare_reserved(self, do_backup):
        p = self.project
        self._write_meta()
        fd = os.open(self.mask_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
        os.close(fd)
        self.plan = mounts.plan(root=p.root, data_ro=p.data_ro, data_rw=p.data_rw,
                                compose=p.value("compose"), mask_skip=p.mask_skip, swap=p.swap)
        exposure, warnings, exposure_fp = grantcheck.preflight(
            self.settings, p.name, {"root": p.root, "data_ro": p.data_ro, "data_rw": p.data_rw,
                                    "mask_skip": p.mask_skip, "swap": p.swap}, plan=self.plan)
        for w in warnings:
            self.warn(f"agentbox: grant warning: {w}")
        if exposure and not grantcheck.matches(p.grant_exposure_accepted, exposure):
            raise AgentboxError(grantcheck.message(p.name, exposure))
        for m in self.plan.mounts:
            _no_commas(m.dst, "mount")
        for n in self.plan.notes:
            self.warn(f"agentbox: {n}")
        mounts.prepare(self.plan)
        self.podman_live = podman_access(p) if isinstance(self, NetSession) else None
        self.podman_dev = dev_tier(p) and isinstance(self, NetSession)
        if self.podman_live and not p.compose_project_explicit:
            self._check_live_stack_is_ours()
        off, why = self.profile_off
        self.profile = profile.load_for_session(self.settings, self.adapter, out=self.warn,
                                                off_reason=why)
        moved = [] if off else migrate_state_to_inbox(self.settings, p.name, self.adapter, self.warn)
        notes = sandbox_notes(p, self.egress, self.podman_live, self.podman_dev,
                              profile=self.profile, inbox=self.inbox_mount,
                              inbox_kept=not self.inbox_is_ephemeral, adapter=self.adapter)
        self.adapter.seed_state(self.settings, self.state_dir, notes, profile_on=not off)
        self.setup = self.adapter.prepare_session(self, notes)
        self._check_setup()
        self._add_profile_mounts(notes)
        self._image, image_note = self._resolve_image()
        _keep(self.log_path("profile"),
              self.profile.log_line().encode() + f"image {self.adapter.name}={self._image}\n".encode())
        if image_note:
            self.warn(f"agentbox: {image_note}")
        if off:
            self.warn(f"agentbox: profile {self.adapter.name}=off (reason={why}): no profile skills, "
                      "instructions or plugins are loaded, and the harness image is used. agentbox's own "
                      "session notes still apply.")
        else:
            self.warn(f"agentbox: profile {self.adapter.name} {self.profile.display}"
                      + (f", loaded: {', '.join(self.profile.names)}" if self.profile.names else
                         " -- nothing to load; put skills in "
                         f"{self.settings.profile_src(self.adapter.name)}")
                      + (f". Moved to the inbox: {', '.join(moved)}" if moved else ""))
        if do_backup:
            self._backup_grants()
        self._write_scan_state()             # after the agent state is seeded: its changes are the session's

    def _backup_grants(self):
        """Snapshot the read-write grants before the session runs, EXCLUDING every covered/masked path.
        The excludes are exactly `covered_paths()` — the same list the gate enforces — so the backup holds
        code, never a plaintext copy of a secret the agent cannot damage (it is masked), and the two can
        never drift apart. Runs after the adapter setup so `covered_paths()` sees the adapter's covers too."""
        p = self.project
        covered = self.covered_paths()

        def covers_rel(g):
            gr = os.path.realpath(g)
            return [os.path.relpath(c, gr) for c in covered
                    if c != gr and c.startswith(gr.rstrip("/") + "/")]
        backup.snapshot(self.settings.backups_dir, p.name, self.name, [p.root] + p.data_rw,
                        skip_rel_for=lambda g: mounts._rel_skips(p.root, g, p.mask_skip),
                        exclude_rel_for=covers_rel,
                        warn_mb=int(self.settings.backup_warn_mb), warn=self.warn)
        backup.retain(self.settings.backups_dir, p.name, keep=int(self.settings.backup_keep),
                      daily_days=int(self.settings.backup_daily_days))

    @property
    def inbox(self):
        """Where this session puts what it wants to keep: read-write, never loaded.

        Per project and persistent -- except under `lockdown`, where it sits inside the session's own
        ephemeral state and goes with it at teardown. Lockdown keeps nothing by design (decisions.md,
        2 Oct), and a review queue that outlived the session would be the single exception to that; the
        agent is told, so it does not write a skill believing it will be kept."""
        if self.inbox_is_ephemeral:
            return os.path.join(self.state_dir, "inbox")
        return self.settings.inbox_dir(self.project.name, self.adapter.name)

    @property
    def inbox_is_ephemeral(self):
        return self.project.preset == "lockdown" and bool(self.name)

    @property
    def inbox_mount(self):
        return self.adapter.inbox_mount

    def _add_profile_mounts(self, notes):
        """The profile's read-only mounts, the generated instruction file, and the inbox.

        Appended after `_check_setup`, with their own equivalent check: these destinations are the agent's
        own config dir (`state_mount`) or the adapter's root, which is exactly the pair `_check_setup`
        allows -- but the adapter does not ask for them, the launcher does, so they are checked here rather
        than pretending an adapter requested them. The inbox goes LAST so that a deeper mount (OMP's inbox
        inside its `agent/` bind) lands on top of the one it sits in."""
        roots = [r for r in (self.adapter.state_mount, self.setup.mount_root) if r]

        def check(dst, what):
            _no_commas(dst, what)
            if not any(_inside(dst, r) and dst != r for r in roots):
                raise AgentboxError(f"{what} {dst} is not under the agent's own root ({', '.join(roots) or 'none'})")

        for src, dst, ro in self.profile.mounts:
            _no_commas(src, "profile mount")
            check(dst, "profile mount")
            self._own_mount_point(src, dst)
            self.setup.mounts.append((src, dst, ro))
        text = self.adapter.session_instructions(self.settings, notes,
                                                 profile_on=not self.profile_off[0])
        if text is not None and self.adapter.instructions_name:
            path = os.path.join(self.settings.session_run_dir(self.name),
                                f"{self.adapter.name}-instructions.md")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            dst = f"{self.adapter.state_mount}/{self.adapter.instructions_name}"
            check(dst, "generated instructions")
            self._own_mount_point(path, dst)
            self.setup.mounts.append((path, dst, True))
        for name, dst in self.adapter.per_session_dirs:
            check(dst, "per-session dir")
            host = os.path.join(self.settings.session_run_dir(self.name), name)
            os.makedirs(host, mode=0o700, exist_ok=True)
            self.setup.mounts.append((host, dst, False))
        if self.inbox_mount:
            check(self.inbox_mount, "inbox mount")
            os.makedirs(self.inbox, mode=0o700, exist_ok=True)
            self.setup.mounts.append((self.inbox, self.inbox_mount, False))

    def host_state_path(self, dst):
        """The host path under the per-project state dir that a container path mirrors, or None.

        The adapter says which container paths are the state dir or part of it (`state_mirrors`): for Claude
        the whole config dir is, for OMP it is `~/.omp/agent`. Anything else -- a tmpfs, another adapter
        mount, a path outside -- is not ours to create or interpret."""
        for prefix, rel in sorted(self.adapter.state_mirrors.items(), key=lambda kv: -len(kv[0])):
            if dst == prefix or not _inside(dst, prefix):
                continue
            return os.path.join(self.state_dir, rel, os.path.relpath(dst, prefix))
        return None

    def _own_mount_point(self, src, dst):
        """Create the mount point for a profile mount ourselves, if it falls inside the per-project state.

        A bind whose destination does not exist gets its mount point created by the container RUNTIME, in
        the middle of the state dir, with whatever owner and mode the runtime chooses. Two things went wrong
        because of that: the end-of-session state scan took its baseline before those files existed and
        reported them as changes afterwards (`CLAUDE.md` on every session, found by `make claude-live`), and
        the next session's migration had to judge a file it had not written and might not be able to read.

        So the launcher makes them, before the scan baseline is taken: an empty file for a file mount, an
        empty directory for a directory one. OMP has always done this for its own file mounts
        (`omp._prepare_file`); the profile's were the ones that did not."""
        host = self.host_state_path(dst)
        if not host:
            return                     # not a path that mirrors the state dir: not ours to make
        os.makedirs(os.path.dirname(host), mode=0o700, exist_ok=True)
        if os.path.isdir(src):
            if os.path.islink(host) or (os.path.lexists(host) and not os.path.isdir(host)):
                os.unlink(host)
            os.makedirs(host, mode=0o700, exist_ok=True)
            return
        if os.path.islink(host) or (os.path.lexists(host) and not os.path.isfile(host)):
            os.unlink(host)
        if not os.path.exists(host):
            os.close(os.open(host, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))

    def _check_setup(self):
        """Adapter mounts: destinations under the adapter's own root, never on top of a grant or /run/agentbox;
        key names that are safe in a path and a shell variable."""
        st = self.setup
        for dst in st.tmpfs + st.ro_empty_dirs:
            _no_commas(dst, "adapter tmpfs")
            if not st.mount_root or not _inside(dst, st.mount_root):
                raise AgentboxError(f"adapter tmpfs {dst} is not under the adapter's root {st.mount_root!r}")
        for src, dst, _ro in st.mounts:
            _no_commas(src, "adapter mount")
            _no_commas(dst, "adapter mount")
            if not st.mount_root or not _inside(dst, st.mount_root) or dst == st.mount_root:
                raise AgentboxError(f"adapter mount {dst} is not under the adapter's root {st.mount_root!r}")
        for path in st.project_covers:
            _no_commas(path, "project cover")
            if not _inside(path, self.project.root) or path == self.project.root:
                raise AgentboxError(f"adapter cover {path} is not inside the project")
        for provider, path in st.keys.items():
            keysmod.env_name(provider)
            _no_commas(os.path.realpath(path), "key file")
        for k, v in st.env.items():
            if not re.match(r"^[A-Z_][A-Z0-9_]*$", k) or "\n" in str(v):
                raise AgentboxError(f"adapter env {k!r} is not a plain variable")

    @property
    def model_domains(self):
        return sorted(set(self.adapter.model_domains) | set(self.setup.model_domains))

    def _check_live_stack_is_ours(self):
        """The observed stack defaults to the project's name, so a project that happens to share a name with
        another stack on the host would observe it. Unless compose_project is set explicitly, every container
        of that compose project must have been brought up from inside the project root (review 2026-10-01 #7)."""
        p = self.project
        try:
            containers = self.podman.containers_json({sockguard.COMPOSE_LABEL: p.compose_project})
        except AgentboxError as e:
            raise AgentboxError(f"project {p.name}: cannot confirm that compose project "
                                f"{p.compose_project!r} is this project's stack: {e}. Refusing rather than "
                                "observing a stack that may not be yours.")
        for c in containers:
            wd = (c.get("Labels") or {}).get("com.docker.compose.project.working_dir", "")
            real = os.path.realpath(wd) if wd else ""
            if not real or not _inside(real, p.root):
                name = (c.get("Names") or ["?"])[0]
                raise AgentboxError(
                    f"project {p.name}: compose project {p.compose_project!r} has container {name} from "
                    f"{wd or 'an unknown directory'}, outside {p.root}; it is not this project's stack. If it "
                    "should be observed anyway, set compose_project explicitly (`--compose-project`)")

    def log_path(self, kind):
        return log_path(self.settings, self.name, self.started, kind)

    def _write_scan_state(self):
        """The end-of-session scan's starting point, kept in the run dir so every teardown (the launcher's,
        `ab session kill`, `ab session gc`) can report (review 2026-10-01 step 5C, M2)."""
        p = self.project
        grants = [p.root] + list(p.data_rw)
        skip = {g: mounts._rel_skips(p.root, g, p.mask_skip) for g in grants}
        doc = {"grants": grants, "skip": skip, "state": scan.snapshot(grants, skip=skip),
               # Recorded at START, so `ab session kill` and `ab session gc` name the branch this session
               # was configured with even if the registry is edited while it runs.
               "base_branch": p.base_branch,
               "agent_state": {"dir": self.state_dir, "files": scan.state_snapshot(self.state_dir)},
               # What the inbox held at the start: teardown reports (and gives provenance to) the difference.
               "inbox": {"project": p.name, "agent": self.adapter.name, "items": profile.items(self.inbox),
                         "preset": p.preset, "egress": p.value("egress"), "dir": self.inbox,
                         "ephemeral": self.inbox_is_ephemeral,
                         # Harvested into the inbox at teardown, then gone with the session's run dir.
                         "harvest": [os.path.join(self.settings.session_run_dir(self.name), n)
                                     for n, _dst in self.adapter.per_session_dirs]}}
        path = os.path.join(self.settings.session_run_dir(self.name), SCAN_STATE)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)

    def _write_meta(self):
        self.started = time.time()
        meta = {"session": self.name, "agent": self.adapter.name, "project": self.project.name,
                "pid": os.getpid(), "pid_start": _start_time(os.getpid()), "started": self.started, "preset_egress": self.project.value("egress")}
        path = os.path.join(self.settings.session_run_dir(self.name), "session.json")
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f)
        os.replace(path + ".tmp", path)

    # ---- the agent container ------------------------------------------------------------
    def allowed_mount_srcs(self, extra=()):
        p = self.project
        out = [p.root] + p.data_ro + p.data_rw + [self.state_dir, self.settings.session_run_dir(self.name)]
        if self.profile and self.profile.dir:
            out.append(os.path.realpath(self.profile.dir))
        if self.adapter.state_mount:
            out.append(os.path.realpath(self.inbox))
        if self.token_file:
            out.append(os.path.realpath(self.token_file))
        out += [os.path.realpath(src) for src, _dst, _ro in self.setup.mounts]
        out += [os.path.realpath(k) for k in self.setup.keys.values()]
        return out + list(extra)

    def network_args(self):
        return ["--network=none"]

    def run_args(self, command, *, interactive=False, tty=False, extra_mounts=()):
        """The complete argv after `podman`, audited."""
        s, a, p = self.settings, self.adapter, self.project
        for check in self.setup.verify:
            check()                          # e.g. OMP's provisioned links still point where they did
        args = ["run", "--rm", f"--name={self.name}", f"--hostname={self.name}"]
        opts = [f"--label={k}={v}" for k, v in labels_for(s, p.name, self.name, "agent").items()]
        if interactive:
            opts.append("--interactive")
        if tty:
            opts.append("--tty")
        opts += hardening.fixed_flags(s, cgroup_parent=s.cgroup_parent)
        opts.append(f"--tmpfs={HOME}:rw,nosuid,nodev,mode=0700,U,notmpcopyup")
        # AGENTBOX_SESSION says "you are inside a session, and which one". A host-only script -- a profile
        # repo's build.sh that deploys or installs, anything that assumes it can reach the LAN or write
        # outside a grant -- can refuse in one line instead of failing halfway through. The id is already a
        # container label and is in the scan state and the inbox provenance; this just lets the session
        # itself see it. First in the dict, so an adapter or the launcher can still override it.
        env = {"AGENTBOX_SESSION": self.name or "", "HOME": HOME, **git_identity(s, a.name), **a.env,
               **self.setup.env, **self.env}
        if tty and os.environ.get("TERM"):
            env["TERM"] = os.environ["TERM"]
        opts += [f"--env={k}={v}" for k, v in env.items()]
        opts.append(f"--workdir={p.root}")
        opts += self.network_args()
        under = self._dir_covers()
        opts += [m.podman_arg(mask_file=self.mask_file) for m in self.plan.mounts
                 if not any(m.dst.startswith(c.rstrip("/") + "/") for c in under)]     # N2: nothing inside a cover
        opts += self.pin_args()
        if a.state_mount:
            opts.append(f"--mount=type=bind,src={self.state_dir},dst={a.state_mount}")
        if self.token_file:
            opts.append(f"--mount=type=bind,src={os.path.realpath(self.token_file)},dst={TOKEN_PATH},ro")
        opts += [mounts.Mount("tmpfs", d).podman_arg() for d in self.setup.tmpfs]
        opts += [mounts.Mount("tmpfs", d, ro=True).podman_arg() for d in self.setup.ro_empty_dirs]
        for path in self.setup.project_covers:
            if os.path.isdir(path) and not os.path.islink(path):
                opts.append(mounts.Mount("tmpfs", path, ro=True).podman_arg())
            else:
                opts.append(mounts.Mount("mask", path).podman_arg(mask_file=self.mask_file))
        for src, dst, ro in self.setup.mounts:
            opts.append(f"--mount=type=bind,src={os.path.realpath(src)},dst={dst}" + (",ro" if ro else ""))
        for provider, path in sorted(self.setup.keys.items()):
            opts.append(f"--mount=type=bind,src={os.path.realpath(path)},"
                        f"dst={keysmod.CONTAINER_DIR}/{provider},ro")
        for src, dst in self.agent_sockets():
            opts.append(f"--mount=type=bind,src={src},dst={dst}")
        extra_srcs = []
        for src, dst in extra_mounts:
            opts.append(f"--mount=type=bind,src={src},dst={dst},ro")
            extra_srcs.append(src)
        opts.append("--entrypoint=/bin/sh")
        hardening.audit(opts + [self.image], allowed_mount_srcs=self.allowed_mount_srcs(extra_srcs),
                        allowed_mount_dsts=[p.root] + p.data_ro + p.data_rw + ["/run/agentbox"]
                        + ([self.setup.mount_root] if self.setup.mount_root else []),
                        networks=self.networks, tmpfs_dirs=("/tmp", HOME),
                        allowed_sockets=[src for src, _dst in self.agent_sockets()], mask_file=self.mask_file)
        return args + opts + [self.image, "-c", self.wrapper(), "agentbox"] + list(command)

    def agent_sockets(self):
        """[(host socket, container path)] the agent gets: the gate for the dev tier, sockguard for observe."""
        out = []
        if self.podman_dev:
            out.append((self.gate_socket, sockguard.AGENT_SOCKET))
            if self.podman_live:
                out.append((self.sockguard_socket, LIVE_SOCKET))
        elif self.podman_live:
            out.append((self.sockguard_socket, sockguard.AGENT_SOCKET))
        return out

    @property
    def gate_socket(self):
        return os.path.join(self.settings.session_run_dir(self.name), "gate", "podman.sock")

    def wrapper(self):
        """Exports the token and provider keys from their read-only mounts just before exec; they are never
        on a command line or in podman's config."""
        files = []
        if self.token_file:
            files.append((self.adapter.token_env, TOKEN_PATH))
        for provider in sorted(self.setup.keys):
            files.append((keysmod.env_name(provider), f"{keysmod.CONTAINER_DIR}/{provider}"))
        return "".join(f'if [ -r {path} ]; then IFS= read -r {var} < {path}; export {var}; fi; '
                       for var, path in files) + 'exec "$@"'

    @property
    def secret_env_names(self):
        """The environment variables that legitimately hold a credential in this session."""
        out = [self.adapter.token_env] if self.token_file else []
        return out + [keysmod.env_name(p) for p in sorted(self.setup.keys)]

    def secret_files(self):
        """Host files whose contents are this session's credentials."""
        return ([self.token_file] if self.token_file else []) + [self.setup.keys[k] for k in sorted(self.setup.keys)]

    def agent_command(self, shell=False, extra=()):
        if shell:
            return ["bash"] + list(extra)
        return self.adapter.agent_argv(self.project.value("approvals")) + list(extra)

    def agent_cli_version(self):
        """The agent CLI version baked into the image this session runs, from the `agentbox.cli-version`
        label that `make <agent>-image` records. None if the image predates the label (rebuild to get it),
        or cannot be read at all -- this reports, it does not decide anything.

        Read as JSON, the same way `ab image build` reads the same label, so the two cannot come to
        disagree about where the pin lives. It was a Go template, which is the habit `podman network ls`
        taught this project to drop."""
        try:
            doc = self.podman.image_json(self.image)
        except AgentboxError:
            return None
        v = (userimage.image_labels(doc).get("agentbox.cli-version") or "").strip()
        return v if v and v not in ("<no value>", "unknown") else None

    # ---- lifecycle ----------------------------------------------------------------------
    def start_infra(self):
        """Overridden by NetSession to bring up networks and sidecars. Base Session is never instantiated
        (make_session always returns a NetSession), so this base no-op is not reached in production."""

    def _guarded(self, body, *, agent_owns_ctrl_c):
        """Run body() and always tear down: on return, error, Ctrl-C, SIGTERM or SIGHUP (terminal closed).
        Teardown itself runs with those signals ignored, so a second signal cannot cut it short."""
        sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        old = {sig: signal.getsignal(sig) for sig in sigs}

        def stop(signum, frame):
            raise KeyboardInterrupt
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN if agent_owns_ctrl_c else stop)
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGHUP, stop)
            return body()
        finally:
            for sig in sigs:
                signal.signal(sig, signal.SIG_IGN)
            try:
                self.teardown()
            finally:
                for sig, h in old.items():
                    signal.signal(sig, h)

    def run_interactive(self, command, tty):
        def body():
            self.start_infra()
            return self.podman.interactive(self.run_args(command, interactive=True, tty=tty))
        return self._guarded(body, agent_owns_ctrl_c=True)   # Ctrl-C belongs to the agent

    def run_captured(self, command, stdin="", timeout=600, before=None):
        def body():
            self.start_infra()
            if before:
                before()
            return self.podman.run(self.run_args(command, interactive=True, extra_mounts=self.probe_mounts),
                                   check=False, input=stdin, timeout=timeout)
        return self._guarded(body, agent_owns_ctrl_c=False)

    probe_mounts = ()

    def pin_args(self):
        """Mounts that pin covers in place; none without the dev tier (nothing can bind past a cover)."""
        return []

    def _dir_covers(self):
        """Directory covers (empty tmpfs): the plan's mask dirs and the adapter's project covers that are dirs.
        Nothing is mounted, pinned or listed beneath one (audit 6 Oct, N2)."""
        out = [m.dst for m in (self.plan.mounts if self.plan else []) if m.kind == "tmpfs"]
        out += [p for p in self.setup.project_covers if os.path.isdir(p) and not os.path.islink(p)]
        return [os.path.realpath(d) for d in out]

    def add_network_cases(self, builder):
        from .selftest import runner
        runner.network_none_cases(builder)

    def teardown(self):
        if not self.name:
            return
        teardown(self.settings, self.podman, self.name, warn=self.warn)


SQUID_READY = "Accepting HTTP Socket connections"


class NetSession(Session):
    """A session on its own internal network, whose only way out is a squid allowlist proxy.

    <session>-int  internal, isolate=strict: the agent and the proxy; no route anywhere else
    <session>-egr  isolate=strict bridge: the proxy only; its route out
    <session>-proxy squid, on both, with this session's egress mode
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.egress = self.project.value("egress")

    @property
    def int_net(self):
        return f"{self.name}-int"

    @property
    def egr_net(self):
        return f"{self.name}-egr"

    @property
    def proxy_name(self):
        return f"{self.name}-proxy"

    def network_args(self):
        self.networks = [self.int_net]
        return [f"--network={self.int_net}"]

    def squid_conf(self, dns=None):
        s = self.settings
        addrs = sorted(set(s.host_addrs) | set(egress.host_addresses()))
        if not addrs:
            raise AgentboxError("cannot tell the VM's own addresses (enumeration failed and host_addrs is empty); "
                                "refusing to start a proxy without a host-address rule")
        return egress.squid_conf(self.egress, model_domains=self.model_domains, model_host=s.model_host,
                                 model_port=s.model_port, extra_domains=self.project.extra_domains,
                                 host_addrs=addrs, dns=dns)

    def net_create_args(self, name, internal):
        # IPv4 only, by decision (docs/decisions.md): no IPv6 destination is routable from the session
        args = ["network", "create", "--ipv6=false", "--opt", "isolate=strict"]
        if internal:
            args.append("--internal")
            # the dev tier's sockguard only lets containers join networks its owner label marks as owned
            args += ["--label", f"{sockguard.DEV_OWNER}={self.name}"]
        for k, v in labels_for(self.settings, self.project.name, self.name, "network").items():
            args += ["--label", f"{k}={v}"]
        return args + [name]

    def proxy_run_args(self, conf_path):
        s = self.settings
        args = ["run", "-d", f"--name={self.proxy_name}", f"--hostname={self.proxy_name}"]
        args += [f"--label={k}={v}" for k, v in labels_for(s, self.project.name, self.name, "proxy").items()]
        args += ["--cap-drop=ALL", "--cap-add=SETUID,SETGID", "--security-opt=no-new-privileges", "--read-only"]
        for d in ("/var/spool/squid", "/var/log/squid", "/run", "/tmp"):
            args.append(f"--tmpfs={d}:rw,nosuid,nodev,notmpcopyup")
        args += [f"--memory={s.squid_memory}", f"--memory-swap={s.squid_memory}", "--pids-limit=256",
                 f"--oom-score-adj={int(s.oom_score_adj)}"]
        if s.cgroup_parent:
            args.append(f"--cgroup-parent={s.cgroup_parent}")
        args += [f"--network={self.egr_net}", f"--network={self.int_net}",
                 f"--mount=type=bind,src={conf_path},dst=/etc/squid/squid.conf,ro",
                 s.squid_image]
        return args

    def start_infra(self):
        pm, s = self.podman, self.settings
        pm.run(self.net_create_args(self.int_net, internal=True))
        pm.run(self.net_create_args(self.egr_net, internal=False))
        gw = pm.run(["network", "inspect", self.egr_net, "--format", "{{range .Subnets}}{{.Gateway}} {{end}}"])
        dns = (gw.stdout or "").split()[0] if (gw.stdout or "").split() else None
        conf = os.path.join(s.session_run_dir(self.name), "squid.conf")
        fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.squid_conf(dns=dns))
        pm.run(self.proxy_run_args(conf))
        self._wait_proxy()
        self.env.update(egress.proxy_env(self.proxy_name))
        if self.podman_live or self.podman_dev:
            self._start_podman_access()
        for var in self.adapter.proxy_url_env:
            self.env[var] = self.env["HTTPS_PROXY"]

    @property
    def sockguard_name(self):
        return f"{self.name}-sockguard"

    @property
    def sockguard_socket(self):
        return os.path.join(self.settings.session_run_dir(self.name), "sg", "sockguard.sock")

    @property
    def sockguard_dev_name(self):
        return f"{self.name}-sockguard-dev"

    @property
    def sockguard_dev_socket(self):
        return os.path.join(self.settings.session_run_dir(self.name), "sgdev", "sockguard.sock")

    @property
    def holder_name(self):
        return f"{self.name}-holder"

    def sockguard_run_args(self, service_sock, conf_path, *, name=None, socket_path=None, role="sockguard"):
        s = self.settings
        name = name or self.sockguard_name
        socket_path = socket_path or self.sockguard_socket
        args = ["run", "-d", f"--name={name}", f"--hostname={name}"]
        args += [f"--label={k}={v}" for k, v in labels_for(s, self.project.name, self.name, role).items()]
        args += ["--network=none", "--userns=keep-id", f"--user={os.getuid()}:{os.getgid()}", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges", "--read-only", f"--memory={s.sockguard_memory}",
                 f"--memory-swap={s.sockguard_memory}", "--pids-limit=128", f"--oom-score-adj={int(s.oom_score_adj)}"]
        if s.cgroup_parent:
            args.append(f"--cgroup-parent={s.cgroup_parent}")
        args += [f"--mount=type=bind,src={service_sock},dst={sockguard.UPSTREAM}",
                 f"--mount=type=bind,src={os.path.dirname(socket_path)},dst={os.path.dirname(sockguard.LISTEN)}",
                 f"--mount=type=bind,src={conf_path},dst=/etc/sockguard/sockguard.yaml,ro",
                 s.sockguard_image, "serve", "-c", "/etc/sockguard/sockguard.yaml"]
        return args

    def sockguard_config(self):
        return sockguard.config(self.podman_live, logs=self.project.observe_logs)

    @property
    def bind_roots(self):
        p = self.project
        return [p.root] + list(p.data_rw) + list(p.data_ro)

    def pin_args(self):
        """Every directory between a grant root and a cover, bound onto itself in the agent's container, so it
        is a mount point and cannot be renamed (EBUSY): renaming a cover's parent would otherwise move the real
        covered file to a path the gate's covered list does not name (audit 5 Oct, H1). Read-only under a
        read-only grant. A rename across one of these boundaries now fails with EXDEV (`git mv` between such
        directories; `mv` falls back to copy and delete)."""
        if not self.podman_dev:
            return []
        roots = [os.path.realpath(r) for r in self.bind_roots]
        ro_roots = {os.path.realpath(r) for r in self.project.data_ro}
        pins = {}
        covered = self.covered_paths()
        for c in covered:
            inside = [r for r in roots if c.startswith(r.rstrip("/") + "/")]
            if not inside:
                continue
            root = max(inside, key=len)
            d = os.path.dirname(c)
            while d != root and d.startswith(root + "/"):
                if not any(d == x or d.startswith(x.rstrip("/") + "/") for x in covered):   # N2
                    pins[d] = root in ro_roots
                d = os.path.dirname(d)
        return [f"--mount=type=bind,src={d},dst={d}" + (",ro" if ro else "")
                for d, ro in sorted(pins.items(), key=lambda kv: kv[0].count("/"))]

    def masked_paths(self):
        """The covers that read as empty in the session (secret masks, mask dirs, the adapter's project
        covers): a build context packed in the session carries them empty."""
        out = {os.path.realpath(m.dst) for m in (self.plan.mounts if self.plan else []) if m.kind in ("mask", "tmpfs")}
        out |= {os.path.realpath(p) for p in self.setup.project_covers}
        return sorted(out)

    def cover_ids(self):
        """{covered path: [st_dev, st_ino]} at session start; the gate refuses binds if one changes (a cover
        moved or was replaced).

        A cover left out of this map is one the gate's rename detection no longer applies to, so an identity
        that cannot be read refuses the session rather than being skipped (audit F-2/F-10 sweep). A path that
        does not exist has no identity to pin and nothing to rename."""
        out = {}
        for c in self.covered_paths():
            try:
                st = os.lstat(c)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    continue
                raise AgentboxError(f"{c}: cannot read this cover's identity ({e}), so the gate could not "
                                    "tell if it were renamed or replaced during the session")
            out[c] = [st.st_dev, st.st_ino]
        return out

    def covered_paths(self):
        """Host paths the agent's own container masks or covers read-only, as real paths (crit 5 Oct): secret
        masks and mask dirs, the dev-swap `.env` (the real file behind the swap), `.git/hooks`/`config`/
        `config.worktree`, compose-file covers and the adapter's project covers. The gate refuses a spawned
        container's bind that resolves onto, under, or over any of these, since those in-container mounts do
        not exist in a container the agent starts. A read-only bind whose destination is itself a grant (a
        data_ro grant) is not a cover: the agent may read it, and the gate keeps it read-only in spawned
        containers (`ro_roots`, audit 5 Oct H3)."""
        covered = self._plan_covered()
        for p in self.setup.project_covers:
            covered.add(os.path.realpath(p))
        under = self._dir_covers()
        return sorted(c for c in covered if not any(c.startswith(d.rstrip("/") + "/") for d in under))

    def _plan_covered(self):
        """The cover/mask paths the session PLAN creates (`mounts.covered`: masks, mask dirs, the `.env`
        dev-swap target, and the read-only git hooks/config and compose covers). The single source both
        `covered_paths()` (what the gate enforces) and the pre-session backup's exclude list are built from,
        so a cover added in one place cannot be missed by the other. `covered_paths()` adds the adapter's
        project covers on top (known only after setup).

        NOT what the grant scan may treat as covered: a read-only cover is readable (audit F-6). The scan
        uses `mounts.hidden`, through `grantcheck.preflight`."""
        if not self.plan:
            return set()
        return set(mounts.covered(self.plan, self.bind_roots))

    def sockguard_dev_config(self):
        return sockguard.dev_config(self.name, bind_roots=self.bind_roots)

    def _start_sockguard(self, service, *, name, socket_path, text, role):
        s, run = self.settings, self.settings.session_run_dir(self.name)
        os.makedirs(os.path.dirname(socket_path), mode=0o700, exist_ok=True)
        conf = os.path.join(run, f"{role}.yaml")
        fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.podman.run(self.sockguard_run_args(service, conf, name=name, socket_path=socket_path, role=role))

        def running():
            st = self.podman.run(["inspect", "-f", "{{.State.Status}}", name], check=False)
            return (st.stdout or "").strip() not in ("exited", "stopped", "dead")
        try:
            sockguard.wait_socket(socket_path, float(s.ready_timeout), role, alive=running)
        except AgentboxError:
            logs = self.podman.run(["logs", name], check=False)
            raise AgentboxError(f"{role} did not start: "
                                f"{' '.join(((logs.stderr or '') + (logs.stdout or '')).strip().splitlines()[-3:])}")

    def holder_run_args(self):
        """A container that only sleeps on the session network: builds join its network namespace."""
        s = self.settings
        args = ["run", "-d", f"--name={self.holder_name}", f"--hostname={self.holder_name}"]
        args += [f"--label={k}={v}" for k, v in labels_for(s, self.project.name, self.name, "holder").items()]
        args += [f"--network={self.int_net}", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
                 "--memory=32m", "--memory-swap=32m", "--pids-limit=16", f"--oom-score-adj={int(s.oom_score_adj)}"]
        if s.cgroup_parent:
            args.append(f"--cgroup-parent={s.cgroup_parent}")
        return args + ["--entrypoint=/bin/sleep", s.squid_image, "infinity"]

    def dev_proxy_env(self):
        """Proxy variables for the agent and every dev container: the session's own subnet and podman's DNS
        names go direct (they are on the same internal network), everything else through squid."""
        p = self.podman.run(["network", "inspect", self.int_net, "--format",
                             "{{range .Subnets}}{{.Subnet}} {{end}}"], check=False)
        subnets = self.int_subnets = (p.stdout or "").split()
        env = egress.proxy_env(self.proxy_name)
        no_proxy = ",".join(["localhost", "127.0.0.1", "::1"] + subnets + [".dns.podman"])
        env["NO_PROXY"] = env["no_proxy"] = no_proxy
        return env

    def build_network(self):
        """(proxy env by the proxy's IP, [DNS servers]) for builds: their RUN steps join the holder's network
        namespace but get their own resolv.conf, where container names do not resolve (trial, 2 Oct). podman's
        DNS for the session network answers on the network's gateway."""
        p = self.podman.run(["inspect", self.proxy_name, "--format", "{{json .NetworkSettings.Networks}}"],
                            check=False)
        try:
            ip = (json.loads(p.stdout or "{}").get(self.int_net) or {}).get("IPAddress") or ""
        except ValueError:
            ip = ""
        g = self.podman.run(["network", "inspect", self.int_net, "--format", "{{range .Subnets}}{{.Gateway}} {{end}}"],
                            check=False)
        dns = (g.stdout or "").split()
        if not ip:
            return None, dns
        url = f"http://{ip}:{egress.PROXY_PORT}"
        no_proxy = ",".join(["localhost", "127.0.0.1"] + list(getattr(self, "int_subnets", [])))
        return {"http_proxy": url, "https_proxy": url, "HTTP_PROXY": url, "HTTPS_PROXY": url,
                "no_proxy": no_proxy, "NO_PROXY": no_proxy}, dns

    def gate_config(self, service, proxy_env):
        s = self.settings
        svc = s.service_dir(self.name)
        build_env, build_dns = self.build_network()
        return {
            "listen": self.gate_socket, "upstream": self.sockguard_dev_socket, "raw": service,
            "session": self.name, "network": self.int_net,
            "labels": labels_for(s, self.project.name, self.name, "dev"),
            "proxy_env": proxy_env, "bind_roots": self.bind_roots, "covered": self.covered_paths(),
            "covered_ids": self.cover_ids(), "ro_roots": [os.path.realpath(r) for r in self.project.data_ro],
            "masked": self.masked_paths(),
            "subnets": self.int_subnets,
            "cgroup_parent": s.cgroup_parent or "", "slice": s.cgroup_parent or "",
            "memory": 1 << 30, "pids": 1024, "memory_max": 4 << 30, "pids_max": 4096,
            "build_memory": int(self.project.build_memory),
            "holder": self.holder_name, "registries": list(sockguard.REGISTRIES),
            "images": list(self.project.dev_images), "spool": os.path.join(service_tmp(s, self.name), "gate"),
            "built_record": builtrec.path_for(s.data_dir, self.project.name),
            "build_proxy_env": build_env, "build_dns": build_dns,
            "log": os.path.join(svc, "gate.log"),
        }

    def _start_podman_access(self):
        s = self.settings
        stamp = int(self.started)
        service, self._service_proc = sockguard.start_service(
            s.podman, s.service_dir(self.name), float(s.ready_timeout), slice_name=s.cgroup_parent,
            unit=f"{self.name}-podman-{stamp}", tmpdir=service_tmp(s, self.name))
        if self.podman_live:
            self._start_sockguard(service, name=self.sockguard_name, socket_path=self.sockguard_socket,
                                  text=self.sockguard_config(), role="sockguard")
        if not self.podman_dev:
            url = f"unix://{sockguard.AGENT_SOCKET}"
            self.env.update({"CONTAINER_HOST": url, "DOCKER_HOST": url})
            return
        self._start_sockguard(service, name=self.sockguard_dev_name, socket_path=self.sockguard_dev_socket,
                              text=self.sockguard_dev_config(), role="sockguard-dev")
        self.podman.run(self.holder_run_args())
        proxy_env = self.dev_proxy_env()
        self.env.update(proxy_env)
        # One spelling per variable for dev containers: sockguard refuses case-variant duplicate keys in a
        # create body. Lower case is the one curl, wget, Python and Go all read.
        container_proxy_env = {k: v for k, v in proxy_env.items() if k.islower()}
        cfg = os.path.join(s.service_dir(self.name), "gate.json")
        fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.gate_config(service, container_proxy_env), f, indent=2)
        os.makedirs(os.path.dirname(self.gate_socket), mode=0o700, exist_ok=True)
        self._gate_proc = sockguard.start_gate(cfg, s.service_dir(self.name), self.gate_socket,
                                               float(s.ready_timeout), slice_name=s.cgroup_parent,
                                               unit=f"{self.name}-gate-{stamp}")
        url = f"unix://{sockguard.AGENT_SOCKET}"
        self.env.update({"CONTAINER_HOST": url, "DOCKER_HOST": url})
        if self.podman_live:
            self.env["AB_PODMAN_LIVE"] = f"unix://{LIVE_SOCKET}"

    def _wait_proxy(self):
        deadline = time.time() + float(self.settings.ready_timeout)
        while time.time() < deadline:
            logs = self.podman.run(["logs", self.proxy_name], check=False)
            text = (logs.stdout or "") + (logs.stderr or "")
            if SQUID_READY in text:
                return
            state = self.podman.run(["inspect", "-f", "{{.State.Status}}", self.proxy_name], check=False)
            if (state.stdout or "").strip() in ("exited", "stopped", "dead"):
                raise AgentboxError(f"egress proxy exited: {' '.join(text.strip().splitlines()[-3:])}")
            time.sleep(0.5)
        raise AgentboxError(f"egress proxy not ready after {self.settings.ready_timeout}s")

    def add_network_cases(self, builder):
        from .selftest import runner
        s = self.settings
        runner.network_cases(builder, self.egress, model=(s.model_host, s.model_port),
                             lan_targets=list(s.lan_targets), model_domains=self.model_domains,
                             extra_domains=self.project.extra_domains)

    def teardown(self):
        if not self.name:
            return
        super().teardown()
        # super().teardown() -> module teardown -> sockguard.stop_service, which now reaps our service/gate
        # Popen handles as well as killing by PID; this poll is a harmless belt-and-braces.
        for proc in (getattr(self, "_service_proc", None), getattr(self, "_gate_proc", None)):
            if proc:
                proc.poll()


def make_session(settings, agent, project, **kw):
    return NetSession(settings, agent, project, **kw)


SCAN_STATE = "scan-state.json"
SIDECAR_LOGS = (("proxy", "proxy"), ("sockguard", "sockguard"), ("sockguard-dev", "sockguard-dev"))


def log_path(settings, session, started, kind):
    """Where a sidecar's log is kept after the session. Session names are reused once a session ends, so
    the start time is part of the name: two sessions never share a log file."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started))
    return os.path.join(settings.data_dir, "logs", f"{session}-{stamp}-{kind}.log")


def _keep(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False                 # a second teardown of the same session: its log is already kept
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return True


def _keep_logs(settings, podman, session, started, warn):
    """The gate's log (API calls and verdicts), the proxy's access log (destinations and verdicts only) and
    sockguard's audit logs, kept for review."""
    gate_log = os.path.join(settings.service_dir(session), "gate.log")
    if os.path.isfile(gate_log):
        with open(gate_log, "rb") as src:
            _keep(log_path(settings, session, started, "gate"), src.read())
    for suffix, kind in SIDECAR_LOGS:
        try:
            logs = podman.run(["logs", f"{session}-{suffix}"], check=False)
            if logs.returncode == 0 and logs.stdout:
                # stdout is squid's access log and sockguard's audit; stderr is squid's cache.log, which says why
                # an upstream connection failed (the egress flake, 2 Oct)
                text = (logs.stdout or "") + (f"\n--- stderr ---\n{logs.stderr}" if logs.stderr else "")
                _keep(log_path(settings, session, started, kind), text.encode())
        except (AgentboxError, OSError) as e:
            warn(f"agentbox: could not keep the {kind} log: {e}")


class ProjectLock:
    """The project's one session slot, held for the life of an `ab run`.

    Released by `teardown()` as well, keyed by session id, so every path that ends a session frees it: a
    normal exit, an exception in prepare(), SIGINT/SIGTERM/SIGHUP (`_guarded` tears down on all three), and
    `ab session kill`. SIGKILL cannot release it -- that is what the stale check is for."""

    def __init__(self, settings, project, pid, pid_start):
        self.settings, self.project, self.pid, self.pid_start = settings, project, pid, pid_start
        self.session = ""

    def note(self, session_id):
        self.session = session_id
        projectlock.note_session(self.settings, self.project, session_id)

    def release(self):
        return projectlock.release(self.settings, self.project, pid=self.pid)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def lock_holder_state(settings, podman, holder):
    """(gone, why) for whoever holds a project's session lock.

    Gone means: its launcher process is not there AND nothing of its session is running or starting. The
    pid is checked with its start time, so a recycled pid is not mistaken for the original.

    A launcher that is still alive is never treated as gone, even with no containers left -- a session in
    prepare() looks exactly like that, and three audits ran into the version of this question that guessed
    (F3, F2, F-1). A hung launcher therefore holds its project until `ab session kill` ends it, which is
    the one action that can actually stop it; tearing down containers it does not have would free nothing.

    Raises if podman cannot be read: a lock is never stolen on a guess."""
    pid, start = holder.get("pid"), holder.get("pid_start")
    if isinstance(pid, int) and pid > 0 and _start_time(pid) is not None and _start_time(pid) == start:
        return False, f"its launcher (pid {pid}) is still running"
    sid = holder.get("session") or ""
    if not sid:
        # Taken, but the session was never named: either a launcher that died in its first moments, or one
        # that is naming its session right now. The pid answers it, and the pid is gone.
        return True, "the session it was taking was never named, and its launcher is gone"
    info = sessions(settings, podman).get(sid)
    if info:
        alive, why = session_is_alive(settings, sid, info)
        if alive:
            return False, f"session {sid} is still alive ({why})"
    return True, f"its launcher is gone and nothing of session {sid} is running"


def take_project_lock(settings, podman, project, warn=lambda m: None):
    """Hold the project's one session slot, reaping a dead holder's session first.

    Returns a ProjectLock. Raises AgentboxError naming `ab attach` when a live session already holds it."""
    pid = os.getpid()
    ok, holder = projectlock.acquire(settings, project, pid=pid, pid_start=_start_time(pid))
    if ok:
        lock = ProjectLock(settings, project, pid, _start_time(pid))
        # Backstop: a live session of this project with no lock file at all. That happens to a session
        # started before the lock existed, and to one whose lock was removed by hand -- and the lock would
        # otherwise be handed out while that session is still writing the project's state.
        other = live_session_of(settings, podman, project)
        if other:
            lock.release()
            raise AgentboxError(
                f"{project} is running (session {other}), although it holds no session lock -- it was "
                f"started before agentbox enforced one, or its lock file was removed. "
                f"`ab attach {project}` joins it, `ab session kill {project}` ends it. One session per "
                f"project: two would share {project}'s agent state and inbox, last writer wins.")
        return lock
    if holder.get("unreadable"):
        raise AgentboxError(projectlock.holder_text(settings, project, holder))
    gone, why = lock_holder_state(settings, podman, holder)
    if not gone:
        sid = holder.get("session") or ""
        raise AgentboxError(
            f"{projectlock.holder_text(settings, project, holder)}; `ab attach {project}` joins it"
            + (f", `ab session kill {project}` ends it (its logs and end-of-session scan are kept)"
               if sid else "")
            + ". One session per project: two would share "
            + f"{project}'s agent state and inbox, last writer wins. [{why}]")
    sid = holder.get("session") or ""
    if sid:
        info = sessions(settings, podman).get(sid)
        left = ((info or {}).get("containers") or []), ((info or {}).get("networks") or [])
        teardown(settings, podman, sid, warn=warn)
        warn(f"agentbox: reaped session {sid} of {project} before starting: {why}. Removed "
             f"{len(left[0])} container(s) and {len(left[1])} network(s)"
             + (f" ({', '.join([c['name'] for c in left[0]] + list(left[1]))})" if any(left) else "")
             + ". Its logs and end-of-session scan were kept.")
    else:
        warn(f"agentbox: cleared a stale session lock on {project}: {why}.")
    projectlock.force_release(settings, project)
    ok, holder = projectlock.acquire(settings, project, pid=pid, pid_start=_start_time(pid))
    if not ok:
        raise AgentboxError(f"{projectlock.holder_text(settings, project, holder)} (it was taken while the "
                            f"stale one was being cleared); try again")
    return ProjectLock(settings, project, pid, _start_time(pid))


def live_session_of(settings, podman, project):
    """The id of the project's live session, or None. One per project, so there is no ambiguity."""
    for sid, info in sorted(sessions(settings, podman).items()):
        if (info.get("meta") or {}).get("project") != project and not any(
                c.get("project") == project for c in info.get("containers") or []):
            continue
        if session_is_alive(settings, sid, info)[0]:
            return sid
    return None


def migrate_state_to_inbox(settings, project, adapter, warn):
    """Move what earlier sessions accumulated in the per-project state into the project's inbox, once.

    Those paths (`skills/`, `agents/`, and Claude's state `CLAUDE.md`) are read-only mounts now, so
    whatever is under them would be shadowed and invisible rather than loaded. Nothing is deleted: it is
    moved to the inbox, where `ab profile harvest` can promote it into the shared profile or dismiss it.
    Returns the names moved."""
    src_dir = settings.state_dir(project, adapter.name)
    inbox = settings.inbox_dir(project, adapter.name)
    moved, removed, unreadable, stuck = [], [], [], []
    for name in adapter.migrate_to_inbox:
        # Always the project's own WRITABLE state (`<data>/<project>/<agent>/<name>`) -- never the profile's
        # read-only snapshot, which is a different path and is what gets mounted ON TOP of this one.
        src = os.path.join(src_dir, name)
        if not os.path.exists(src) or os.path.islink(src):
            continue
        # A bind whose destination does not exist gets its mount point created by the container runtime,
        # so the state dir collects an empty file or directory that the launcher did not write and may not
        # be able to read (the owner and mode are the runtime's to choose; `_own_mount_point` now creates
        # these itself, so this handles the ones earlier sessions left). Neither holds anything, so neither
        # is content to rescue: both are simply removed, as an empty directory always was.
        try:
            empty = (not os.listdir(src)) if os.path.isdir(src) else (os.lstat(src).st_size == 0)
        except OSError:
            empty = False                     # cannot even look: treat it as content and keep it below
        if empty:
            (os.rmdir if os.path.isdir(src) else os.unlink)(src)
            continue
        readable = os.access(src, os.R_OK | (os.X_OK if os.path.isdir(src) else 0))
        try:
            redundant = readable and os.path.isfile(src) and adapter.redundant_state_copy(settings, name, src)
        except OSError:
            redundant = False                 # unreadable mid-check: keep it rather than judge it
        if redundant:
            os.unlink(src)
            removed.append(name)
            continue
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        dst = os.path.join(inbox, f"from-state-{name}")
        n = 2
        while os.path.lexists(dst):
            dst, n = os.path.join(inbox, f"from-state-{name}.{n}"), n + 1
        try:
            shutil.move(src, dst)
        except OSError as e:
            # Renaming a DIRECTORY needs write access to the directory itself (its `..` entry changes), so
            # one a container created cannot be moved by us at all. Leave it exactly where it is and say
            # so: the read-only profile mount covers it, so nothing loads it and the session is unaffected.
            stuck.append(f"{src} ({e.strerror or e})")
            continue
        if not readable:
            # Preserved, not judged: we cannot tell whether it holds anything worth keeping.
            unreadable.append(name)
        moved.append(os.path.basename(dst))
    if moved:
        warn(f"agentbox: moved {', '.join(moved)} out of {project}'s {adapter.name} state into {inbox} "
             f"(those paths are read-only profile mounts now; nothing was deleted). "
             f"`ab profile harvest` reviews them.")
    if removed:
        warn(f"agentbox: removed {', '.join(removed)} from {project}'s {adapter.name} state: it was a copy "
             f"of what the profile already holds, with nothing added, so there was nothing to review. The "
             f"profile's own copy is what sessions load.")
    if unreadable:
        warn(f"agentbox: {', '.join(unreadable)} in {project}'s {adapter.name} state could not be read, so "
             f"it was moved to {inbox} unexamined rather than judged (a file a container wrote can carry an "
             f"owner or mode of its own).")
    if stuck:
        warn(f"agentbox: left in place, unreadable and unmovable: {'; '.join(stuck)}. Renaming a directory "
             f"needs write access to the directory itself, which this one does not give us. The profile's "
             f"read-only mount covers it, so nothing loads it and the session is unaffected; it was most "
             f"likely written from inside a container, and clearing it needs the same privileges that "
             f"created it.")
    return moved


def _harvest_per_session_dirs(box, inbox, warn):
    """Move what a per-session directory collected into the inbox, before the session's run dir goes.

    OMP's autolearn writes learned skills to `agent/managed-skills`, which agentbox gives it empty every
    session: a skill the agent wrote itself is a proposal, so it goes to the review queue rather than
    straight into what later sessions load. Copied, then left to `_inbox_report` to give provenance to --
    so a harvested skill carries the same record as anything else in the inbox."""
    for src in box.get("harvest") or []:
        if not os.path.isdir(src):
            continue
        for name in sorted(os.listdir(src)):
            os.makedirs(inbox, mode=0o700, exist_ok=True)
            dst = os.path.join(inbox, f"learned-{name}")
            n = 2
            while os.path.lexists(dst):
                dst, n = os.path.join(inbox, f"learned-{name}.{n}"), n + 1
            try:
                shutil.move(os.path.join(src, name), dst)
            except OSError as e:
                warn(f"agentbox: could not move {name} out of {src} into the inbox: {e}")


def _inbox_report(settings, doc, session, warn, now=None):
    """Give every inbox item this session added its provenance sidecar, and say how many there are.

    Teardown is the only place the host knows which items are new -- nothing hooks the agent's writes -- so
    it is also where provenance is recorded, from the before/after difference."""
    box = doc.get("inbox") or {}
    if not box.get("project"):
        return
    inbox = box.get("dir") or settings.inbox_dir(box["project"], box["agent"])
    _harvest_per_session_dirs(box, inbox, warn)
    if box.get("ephemeral"):
        # Nothing is recorded or promised: this inbox is about to be deleted with the rest of the session's
        # state. Saying how many items are going is still worth it -- silently dropping the agent's work
        # would be the surprise.
        going = [n for n in profile.items(inbox) if n not in set(box.get("items") or ())]
        if going:
            warn(f"agentbox: {len(going)} item(s) were written to this lockdown session's inbox "
                 f"({', '.join(going[:10])}) and are being DELETED with the rest of its state, as this "
                 f"preset keeps nothing. Copy anything worth keeping out of {inbox} now if the session is "
                 f"still up.")
        return
    try:
        new = profile.record_new_items(settings, box["project"], box["agent"], box.get("items") or [],
                                       session=session, preset=box.get("preset", ""),
                                       egress=box.get("egress", ""), now=now)
    except OSError as e:
        warn(f"agentbox: could not record the inbox's provenance: {e}")
        return
    if new:
        warn(f"agentbox: {len(new)} new item(s) in {inbox}: {', '.join(new[:10])}"
             + (" ..." if len(new) > 10 else "")
             + ". Nothing there is loaded; `ab profile harvest` reviews it.")


def _scan(settings, session, started, warn):
    """The end-of-session scan, from the state recorded at start (scan.py). Never raises."""
    path = os.path.join(settings.session_run_dir(session), SCAN_STATE)
    try:
        with open(path) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        warn(f"agentbox: end-of-session scan skipped: its start state is unreadable ({e})")
        return
    _inbox_report(settings, doc, session, warn)
    try:
        # base_branch comes from the project registry, recorded in the scan state at session start, so
        # `ab session kill` and `gc` name the same branch the launcher would have.
        summary, text = scan.report(doc["state"], doc["grants"], skip=doc.get("skip"),
                                    base_branch=doc.get("base_branch"))
        st = doc.get("agent_state") or {}
        if st.get("dir"):
            s2, lines = scan.state_report(st.get("files") or {}, st["dir"])
            summary, text = summary + s2, text + "\n" + "\n".join(lines)
    except Exception as e:           # the scan informs; it must never stop the teardown (review step 5C, H3)
        warn(f"agentbox: end-of-session scan failed: {e!r}")
        return
    out = log_path(settings, session, started, "scan")
    _keep(out, text.encode())
    if summary:
        warn("agentbox: end-of-session scan (review before you run or push anything):\n"
             + "\n".join(f"  - {line}" for line in summary) + f"\n  full report: {out}")


def _session_started(settings, session):
    try:
        with open(os.path.join(settings.session_run_dir(session), "session.json")) as f:
            return float(json.load(f).get("started") or time.time())
    except (OSError, ValueError, TypeError, AttributeError):
        return time.time()


def teardown(settings, podman, session, warn=None):
    """Remove everything of a session: the launcher's own teardown, `ab session kill` and `ab session gc` all come here, so
    each keeps the logs and runs the end-of-session scan. The service and gate stop first (nothing new can be
    created), the scan runs last, and no failure of either skips the removal."""
    warn = warn or (lambda m: print(m, file=sys.stderr))
    sel = {"agentbox.session": session, **settings.labels}
    started = _session_started(settings, session)
    try:
        sockguard.stop_service(settings.service_dir(session))
    finally:
        try:
            _keep_logs(settings, podman, session, started, warn)
        except Exception as e:
            warn(f"agentbox: could not keep the session's logs: {e!r}")
        shutil.rmtree(settings.service_dir(session), ignore_errors=True)
        # A listing that cannot be read must not look like "nothing to remove": warn per kind, keep going
        # with the others, and never let a failed listing pass for a clean teardown (audit F-2/F-10).
        for kind, argv in (("container", ["rm", "-f", "-v", "-t", "5"]),   # -v: its anonymous volumes too
                           ("pod", ["pod", "rm", "-f"]),
                           ("volume", ["volume", "rm", "-f"]),
                           ("network", ["network", "rm", "-f"])):
            try:
                found = podman.ids(kind, sel)
            except AgentboxError as e:
                warn(f"agentbox: could not list this session's {kind}s, so some may be left behind: {e}. "
                     f"Check with `podman {kind if kind != 'container' else 'ps -a'} --filter "
                     f"label=agentbox.session={session}`.")
                continue
            for target in found:
                podman.run(argv + [target], check=False)
        if settings.cgroup_parent:
            # the per-session build cgroup the gate names; empty once the builds are gone
            build_cg = "/sys/fs/cgroup" + buildcap.slice_cgroup_path(settings.cgroup_parent) + f"/{session}-build"
            try:
                os.rmdir(build_cg)
            except OSError:
                pass
        shutil.rmtree(service_tmp(settings, session), ignore_errors=True)
        _scan(settings, session, started, warn)
        # Whatever ended this session -- its own exit, an exception, a signal, `ab session kill`, `gc` --
        # the project's slot is free again. Silently: a lock that works is not news.
        projectlock.release_for_session(settings, session)
        shutil.rmtree(ephemeral_state(settings, session), ignore_errors=True)   # a lockdown session's state
        run_dir = settings.session_run_dir(session)
        if os.path.isdir(run_dir):
            for fn in os.listdir(run_dir):
                try:
                    os.unlink(os.path.join(run_dir, fn))
                except IsADirectoryError:
                    shutil.rmtree(os.path.join(run_dir, fn), ignore_errors=True)
            os.rmdir(run_dir)


def ephemeral_state(settings, session):
    """A lockdown session's agent state: empty at start, removed at teardown."""
    return os.path.join(settings.data_dir, "ephemeral", session)


def reset_state(settings, project, now=None):
    """Move a project's agent state (every agent's) into the backups dir. Returns (moved from, moved to) or
    None when there is none."""
    src = os.path.join(settings.data_dir, project)
    if not os.path.isdir(src) or not os.listdir(src):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    dst = os.path.join(settings.backups_dir, project, f"agent-state-{stamp}")
    os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
    os.rename(src, dst)
    return src, dst


def service_tmp(settings, session):
    """The session's podman service's TMPDIR: buildah's RUN --mount=type=cache directories live under it, so a
    session never shares a build cache with the host's or another session's builds."""
    return os.path.join(settings.data_dir, "tmp", session)


def _start_time(pid):
    """Process start time in clock ticks since boot (field 22 of /proc/<pid>/stat); None if gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _alive(meta):
    """The launcher that started the session is still running (same PID and the same process)."""
    pid = int(meta.get("pid", 0))
    return pid > 0 and _start_time(pid) is not None and _start_time(pid) == meta.get("pid_start")


def session_metas(settings):
    """[(session, meta or None)] for every session run dir; None means session.json is missing or
    unparseable. Kept separate from sessions() so a podman failure cannot hide this signal, and vice
    versa."""
    out = []
    base = os.path.join(settings.run_dir, "sessions")
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return out
    except OSError as e:
        raise AgentboxError(f"cannot read the session directory {base}: {e}")
    for s in names:
        try:
            with open(os.path.join(settings.session_run_dir(s), "session.json")) as f:
                out.append((s, json.load(f)))
        except (OSError, ValueError):
            out.append((s, None))
    return out


def project_is_live(settings, podman, name):
    """Is anything of this project running? Returns (live, why).

    EVERY signal is consulted, ANY of them says live, and a signal that cannot be READ also says live.

    Three audits found this question answered wrong, each time the same way -- one signal taken for the
    whole truth, and an unreadable signal read as "nothing is running":
      * the launcher PID alone, so a dead launcher hid running containers (F2);
      * then container labels alone, so a session still in prepare() -- session.json written, containers not
        yet created -- was invisible (F-1);
      * and `podman ps` failing, a missing binary or unparseable output all returning "no containers",
        which every caller read as "nothing is running" (F-2, F-10).

    So the signals are: a running container labelled with the project; a session.json naming it whose
    launcher is alive; and any session run dir whose session.json cannot be read -- that one cannot be
    attributed to a project, so it counts for every project rather than being guessed at by parsing the
    session name."""
    reasons = []
    try:
        for c in podman.containers_json(settings.labels):
            labels = c.get("Labels") or {}
            if labels.get("agentbox.project") != name or (c.get("State") or "").lower() != "running":
                continue
            shown = (c.get("Names") or [(c.get("Id") or "")[:12]])[0]
            sess = labels.get("agentbox.session") or ""
            reasons.append(f"container {shown} (session {sess or '?'}) is running"
                           + (f" -- `ab session kill {sess}`" if sess else ""))
    except AgentboxError as e:
        return True, f"cannot tell whether its containers are running: {e}"
    try:
        metas = session_metas(settings)
    except AgentboxError as e:
        return True, f"cannot tell whether a session is starting: {e}"
    for sess, meta in metas:
        if meta is None:
            reasons.append(f"session {sess} has no readable session.json, so it cannot be told apart from a "
                           "session starting right now (`ab session gc` clears one left by a crash)")
        elif meta.get("project") == name and _alive(meta):
            reasons.append(f"session {sess} is running (launcher pid {meta.get('pid')}) -- "
                           f"`ab session kill {sess}`")
    return (True, "; ".join(reasons)) if reasons else (False, "")


def sessions(settings, podman):
    """{session: {'meta': dict|None, 'containers': [..], 'networks': [..], 'alive': bool}}"""
    out = {}
    for c in podman.containers_json(settings.labels):
        s = (c.get("Labels") or {}).get("agentbox.session")
        if s:
            names = c.get("Names") or []
            out.setdefault(s, {"containers": [], "networks": []})["containers"].append(
                {"name": names[0] if names else c.get("Id", "")[:12], "role": c["Labels"].get("agentbox.role"),
                 "state": c.get("State"), "project": c["Labels"].get("agentbox.project")})
    for n in podman.networks_json(settings.labels):
        s = (n.get("labels") or {}).get("agentbox.session")
        if s and n.get("name"):
            out.setdefault(s, {"containers": [], "networks": []})["networks"].append(n["name"])
    sess_dir = os.path.join(settings.run_dir, "sessions")
    if os.path.isdir(sess_dir):
        for s in os.listdir(sess_dir):
            out.setdefault(s, {"containers": [], "networks": []})
    for s, info in out.items():
        meta = None
        try:
            with open(os.path.join(settings.session_run_dir(s), "session.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
        info["meta"] = meta
        info["alive"] = bool(meta) and _alive(meta)
    return out


def _newest_mtime(path):
    newest = None
    try:
        newest = os.stat(path).st_mtime
    except OSError:
        return None
    for dirpath, dirnames, filenames in os.walk(path):
        for n in dirnames + filenames:
            try:
                m = os.lstat(os.path.join(dirpath, n)).st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return newest


def idle_seconds(settings, meta, now=None):
    """Seconds since the agent last wrote to its state (transcripts, memory, settings) — a cheap proxy for
    how long a session has sat without the agent doing anything. None when it can't be told (no meta, or
    the state dir is missing). A forgotten session shows up here and in `ab session ls`."""
    if not meta or not meta.get("project") or not meta.get("agent"):
        return None
    dirs = [settings.state_dir(meta["project"], meta["agent"])]
    if meta.get("session"):          # a lockdown session writes an ephemeral per-session dir, not the persistent one
        dirs.append(os.path.join(ephemeral_state(settings, meta["session"]), meta["agent"]))
    mtimes = [m for m in (_newest_mtime(d) for d in dirs) if m is not None]
    if not mtimes:
        return None
    return max(0.0, (time.time() if now is None else now) - max(mtimes))


def gc_images(settings, podman, now=None, warn=lambda m: None):
    """Session-built images (labelled agentbox.role=dev, with their project and session) older than
    image_keep_days are removed, except the newest image carrying each tag. Never forced: an image a
    container still uses stays. ab session kill leaves images alone (decision, 1 Oct)."""
    now = time.time() if now is None else now
    filters = []
    for k, v in {**settings.labels, "agentbox.role": "dev"}.items():
        filters += ["--filter", f"label={k}={v}"]
    p = podman.run(["images", "--format", "json"] + filters, check=False)
    try:
        images = json.loads(p.stdout or "[]") or []
    except ValueError:
        return []
    newest = {}
    for img in images:
        for tag in img.get("Names") or []:
            if tag not in newest or img.get("Created", 0) > newest[tag].get("Created", 0):
                newest[tag] = img
    keep = {img["Id"] for img in newest.values()}
    cutoff = now - float(settings.image_keep_days) * 86400
    removed = []
    for img in images:
        labels = img.get("Labels") or {}
        if img["Id"] in keep or img.get("Created", now) >= cutoff or not labels.get("agentbox.project"):
            continue
        r = podman.run(["rmi", img["Id"]], check=False)
        if r.returncode == 0:
            removed.append(f"image {img['Id'][:12]} ({labels.get('agentbox.project')})")
            builtrec.remove_everywhere(settings.data_dir, [img["Id"]])   # the gate's record of its builds
        else:
            warn(f"agentbox: kept image {img['Id'][:12]}: {(r.stderr or '').strip()[-120:]}")
    return removed


def _young(settings, session, seconds=60):
    """Was this session's run dir touched just now? Used to tell a session starting right now (no metadata
    yet) from a dead leftover.

    A run dir that does NOT EXIST is not a session starting up -- `prepare()` creates it and writes
    session.json before anything else of the session exists -- so absence is an answer: not young. Any other
    error means "cannot tell", which must still read as young, so gc never reaps a session mid-start.

    (The fail-open sweep made every OSError read as young, including ENOENT, which is how a crashed
    session's husk became unreapable: itest test_f_gc_stops_what_a_crashed_session_left, 29 Oct.)"""
    try:
        return time.time() - os.stat(settings.session_run_dir(session)).st_mtime < seconds
    except FileNotFoundError:
        return False
    except OSError:
        return True


# Containers a session runs for itself. None of them is the agent doing work: a crashed session leaves them
# running, and they are exactly what `ab session gc` exists to remove.
INFRA_ROLES = ("proxy", "sockguard", "gate", "holder", "dev")


def session_is_alive(settings, name, info):
    """Is this SESSION still doing work? Returns (alive, reason).

    Deliberately NOT `project_is_live`, and the difference is the point:

      * `project_is_live` guards a registry EDIT, so it is pessimistic -- any running container labelled
        with the project counts, and a signal it cannot read counts too. That is right there: editing a
        project under a running session is what it must prevent.
      * this guards REAPING, where the pessimistic answer is the wrong one. A crashed session leaves its
        proxy, sockguard and networks running; treating those as "alive" would make the husk immortal, and
        because project_is_live *does* count them, the husk's project would stay unmanageable for good
        (no `ab project set`/`rm` while it is live). `ab session kill <name>` stays the manual way out.

    Alive: the launcher process is alive ("launcher"), an AGENT container is still running ("agent"), or the
    session is being started this second -- its run dir exists and was touched seconds ago, before any
    container of it exists ("starting"). Infrastructure containers on their own are a husk.
    """
    if info["alive"]:
        return True, "launcher"
    if [c for c in info["containers"] if c.get("state") == "running" and c.get("role") == "agent"]:
        return True, "agent"
    if info["meta"] is None and _young(settings, name):
        return True, "starting"
    return False, ""


def gc(settings, podman, warn=lambda m: None):
    """Tear down sessions that are no longer doing work: the launcher is gone and no agent container is
    running, so what is left is a husk -- an orphaned proxy, sockguard, gate or network. `session_is_alive`
    is the predicate, and its docstring says why this is NOT the question `project_is_live` answers.

    A session whose agent is still running is left alone even if its launcher died (`ab session kill` ends
    that one explicitly, and the warning says so). Nothing is reaped on a guess: an unreadable podman makes
    `sessions()` raise, so gc removes nothing rather than deciding a session is dead."""
    removed, kept = [], set()
    for s, info in sessions(settings, podman).items():
        alive, why = session_is_alive(settings, s, info)
        if alive:
            if why == "agent":
                warn(f"agentbox: {s} has no live launcher but its agent is running; left alone "
                     f"(use `ab session kill {s}`)")
            kept.add(s)
            continue
        teardown(settings, podman, s, warn=warn)
        removed.append(s)
    # A podman service whose session is dead, even if its run dir and containers are already gone.
    live = {s for s, info in sessions(settings, podman).items()
            if session_is_alive(settings, s, info)[0]} | kept
    for pid, s in sockguard.services_under(settings.run_dir).items():
        if s not in live:
            sockguard.kill_service(pid)
            warn(f"agentbox: stopped an orphaned podman service of {s} (pid {pid})")
            removed.append(f"{s} (podman service)")
    removed += gc_images(settings, podman, warn=warn)
    return removed
