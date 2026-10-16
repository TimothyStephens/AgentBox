"""Sessions: build, start and tear down everything one agent session needs."""
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

from . import adapters, backup, cgroup, egress, grantcheck, hardening, mounts, paths, presets, registry, tokens
from . import keys as keysmod
from . import buildcap, builtrec, scan, sockguard
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
    grant_exposure_accepted: str = ""
    compose_project: str = ""      # the live stack's compose project (default: the project name)
    observe_logs: bool = False     # observe tier may read the live stack's logs (off: they can hold secrets)
    preset: str = ""
    dev_images: list = field(default_factory=list)   # extra image name patterns the dev tier may run/build from
    compose_project_explicit: bool = False

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


def load_project(settings, name, *, with_secrets=False):
    reg = registry.load(settings.registry_path)
    if name not in reg:
        raise AgentboxError(f"no project named {name!r}; add it with `ab project add`")
    p = reg[name]
    unknown = set(p) - registry.PROJECT_KEYS - set(presets.SETTINGS)
    if unknown:
        raise AgentboxError(f"project {name}: unknown settings {', '.join(sorted(unknown))}")
    eff = presets.effective(name, p, presets.load_presets(settings.presets_dir), with_secrets=with_secrets)
    others = [(n, q["root"]) for n, q in reg.items() if n != name and q.get("root")]
    kw = dict(home=HOME, extra_protected=protected_extra(settings), other_roots=others)
    root = paths.check_grant(p.get("root", ""), kind=f"project {name} root", **kw)
    data_ro = [paths.check_grant(d, kind="data_ro", **kw) for d in p.get("data_ro", [])]
    data_rw = [paths.check_grant(d, kind="data_rw", **kw) for d in p.get("data_rw", [])]
    for g in [root] + data_ro + data_rw:
        _no_commas(g, "grant")
    return Project(name=name, root=root, data_ro=data_ro, data_rw=data_rw,
                   mask_skip=list(p.get("mask_skip", [])), extra_domains=list(p.get("extra_domains", [])),
                   swap=check_swap(name, p.get("swap", mounts.DEFAULT_SWAP)), eff=eff,
                   build_memory=check_build_memory(settings, name, p.get("build_memory", settings.build_memory)),
                   history_secrets_accepted=list(p.get("history_secrets_accepted", [])),
                   grant_exposure_accepted=str(p.get("grant_exposure_accepted") or ""),
                   compose_project=check_compose_project(name, p.get("compose_project", name)),
                   observe_logs=_bool(name, "observe_logs", p.get("observe_logs", False)),
                   preset=str(p.get("preset") or ""),
                   dev_images=check_dev_images(name, p.get("dev_images", [])),
                   compose_project_explicit="compose_project" in p)


IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/:@*?-]*$")


def check_dev_images(name, patterns):
    """Image name patterns (fnmatch, full names such as localhost/myapp:*) the dev tier may also use."""
    if not isinstance(patterns, list) or not all(isinstance(p, str) and IMAGE_PATTERN.match(p) for p in patterns):
        raise AgentboxError(f"project {name}: dev_images must be a list of image name patterns "
                            "(e.g. localhost/myapp:*)")
    return list(patterns)


SWAP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
COMPOSE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def check_compose_project(name, value):
    if not isinstance(value, str) or not COMPOSE_NAME.match(value):
        raise AgentboxError(f"project {name}: compose_project {value!r}: lower-case letters, digits, '-', '_'")
    return value


def _bool(name, key, value):
    if not isinstance(value, bool):
        raise AgentboxError(f"project {name}: {key} must be true or false")
    return value


def podman_access(project, warn):
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


def check_swap(name, swap):
    """swap maps a file name to its dev stand-in in the same directory: plain file names only."""
    for k, v in dict(swap).items():
        for x in (k, v):
            if not isinstance(x, str) or not SWAP_NAME.match(x) or x in (".", ".."):
                raise AgentboxError(f"project {name}: swap entry {k!r} = {v!r}: use plain file names "
                                    "(no '/', no '..')")
    return dict(swap)


def _history_masked(git_dir, swap, timeout):
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
    return [n for n in names if mounts._masked(os.path.basename(n), swap)
            or any(part in mounts.MASK_DIRS for part in n.split("/")[:-1])]


def history_secrets(root, grants, swap, mask_skip=(), timeout=120):
    """Masked file names found in the history of every git repo the session can read: the masks hide the
    working tree, never git's objects. Names are shown relative to the project root (`<repo>/<path>` for a
    nested repo). Raises if a history cannot be read."""
    found = set()
    for g in grants:
        dirs, _outside = mounts.find_git_dirs(g, mounts._rel_skips(root, g, mask_skip))
        for gd in dirs:
            if not any(gd == x or gd.startswith(x.rstrip("/") + "/") for x in grants):
                continue                                    # not mounted, so not readable
            work = os.path.dirname(gd) if os.path.basename(gd) == ".git" else gd
            prefix = os.path.relpath(work, root)
            for n in _history_masked(gd, swap, timeout):
                found.add(n if prefix == "." else os.path.join(prefix, n))
    return sorted(found)


def history_secrets_message(project, names):
    shown = ", ".join(names[:15]) + (f" (and {len(names) - 15} more)" if len(names) > 15 else "")
    return (f"project {project}: {len(names)} masked file name(s) are in git history: {shown}. The agent can "
            "read them through git (`git log -p`, `git show`) even though the working-tree files are masked. "
            "Treat these secrets as exposed and rotate them (and consider rewriting the history). To go "
            "ahead once they are rotated, accept them with --accept-history-secrets "
            f"(`ab project accept-history-secrets {project}` for a registered project)")


def check_build_memory(settings, name, value):
    """A build's memory limit: positive, and within the slice's cap (a build over it could never finish)."""
    size = cgroup.parse_size(value)
    if size <= 0:
        raise AgentboxError(f"project {name}: build_memory must be positive")
    cap = cgroup.slice_memory_max(settings.cgroup_parent)
    if cap is not None and size > cap:
        raise AgentboxError(f"project {name}: build_memory {value} is over the {settings.cgroup_parent} cap "
                            f"({cap >> 20} MB)")
    return size


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


def dev_notes(podman_live):
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
        "first) and images built through agentbox for this project; other local images are refused unless the project lists "
        "them;",
    ]
    if podman_live:
        lines.append(f"- The live stack (`{podman_live}`) is read-only on a second socket: "
                     "`podman --url unix:///run/agentbox/podman-live.sock ps`.")
    return lines


def sandbox_notes(project, egress, podman_live=None, podman_dev=False):
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
        lines += dev_notes(podman_live)
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
    lines += [
        "- Do not try to escape, remount, or change the sandbox; ask the user when you are blocked.",
        "",
    ]
    return "\n".join(lines)


class Session:
    def __init__(self, settings, agent, project, *, with_secrets=False, podman=None, warn=None):
        self.settings = settings
        self.adapter = adapters.get(agent)
        self.project = project
        self.with_secrets = with_secrets
        self.podman = podman or Podman(settings.podman)
        self.warn = warn or (lambda m: print(m, file=sys.stderr))
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
        if p.value("secrets") != "exposed":
            found = history_secrets(p.root, [p.root] + p.data_rw + p.data_ro, p.swap, p.mask_skip)
            new = [n for n in found if n not in p.history_secrets_accepted]
            if new:
                raise AgentboxError(history_secrets_message(p.name, new))
            if found:
                self.warn(f"agentbox: {len(found)} accepted secret file name(s) in {p.name}'s git history "
                          "are readable by the agent (accepted as rotated)")
        self.reserve_name()
        try:
            self._prepare_reserved(do_backup)
        except BaseException:
            self.teardown()                  # nothing is running yet; remove the run dir it reserved
            raise

    @property
    def mask_file(self):
        return os.path.join(self.settings.session_run_dir(self.name), "mask-empty") if self.name else None

    def _prepare_reserved(self, do_backup):
        p = self.project
        self._write_meta()
        fd = os.open(self.mask_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
        os.close(fd)
        self.plan = mounts.plan(root=p.root, data_ro=p.data_ro, data_rw=p.data_rw,
                                compose=p.value("compose"), secrets=p.value("secrets"),
                                mask_skip=p.mask_skip, swap=p.swap)
        exposure, warnings = grantcheck.scan(p.root, [p.root] + p.data_rw, p.mask_skip,
                                             covered=self._plan_covered())
        for w in warnings:
            self.warn(f"agentbox: grant warning: {w}")
        if exposure and grantcheck.fingerprint(exposure) != p.grant_exposure_accepted:
            if self.with_secrets:
                # The exposure here is exactly what --with-secrets turned the masks off to reveal, so the
                # default `accept-grant-exposure` remedy does not apply (it evaluates the masked config and
                # finds nothing). State the real requirement instead of a dead-end remedy.
                found = "\n".join(f"  - {f['text'] if isinstance(f, dict) else f}" for f in exposure)
                raise AgentboxError(
                    f"project {p.name}: --with-secrets turns the masks off, giving the agent read-write to "
                    f"real secret/data files in the grant:\n{found}\n"
                    "--with-secrets is only for a project whose real secrets live OUTSIDE the grant. For these "
                    "repo-local files, run WITHOUT --with-secrets — they are masked and the agent sees "
                    "`.env.dev` as `.env`. A key the agent itself needs goes through `ab keys set <provider>` "
                    "(kept outside every grant, mounted read-only, exported as AB_KEY_<PROVIDER>); a pod's own "
                    "runtime secret belongs in the real `.env` used at deploy, never in a session. "
                    "(`ab project accept-grant-exposure` does not apply: it evaluates the masked default.)")
            raise AgentboxError(grantcheck.message(p.name, exposure))
        for m in self.plan.mounts:
            _no_commas(m.dst, "mount")
        for n in self.plan.notes:
            self.warn(f"agentbox: {n}")
        mounts.prepare(self.plan)
        self.podman_live = podman_access(p, self.warn) if isinstance(self, NetSession) else None
        self.podman_dev = dev_tier(p) and isinstance(self, NetSession)
        if self.podman_live and not p.compose_project_explicit:
            self._check_live_stack_is_ours()
        notes = sandbox_notes(p, self.egress, self.podman_live, self.podman_dev)
        self.adapter.seed_state(self.settings, self.state_dir, notes)
        self.setup = self.adapter.prepare_session(self, notes)
        self._check_setup()
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
        for c in self.podman.containers_json({sockguard.COMPOSE_LABEL: p.compose_project}):
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
               "agent_state": {"dir": self.state_dir, "files": scan.state_snapshot(self.state_dir)}}
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
        env = {"HOME": HOME, **git_identity(s, a.name), **a.env, **self.setup.env, **self.env}
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
        hardening.audit(opts + [a.image], allowed_mount_srcs=self.allowed_mount_srcs(extra_srcs),
                        allowed_mount_dsts=[p.root] + p.data_ro + p.data_rw + ["/run/agentbox"]
                        + ([self.setup.mount_root] if self.setup.mount_root else []),
                        networks=self.networks, tmpfs_dirs=("/tmp", HOME),
                        allowed_sockets=[src for src, _dst in self.agent_sockets()], mask_file=self.mask_file)
        return args + opts + [a.image, "-c", self.wrapper(), "agentbox"] + list(command)

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
        """The agent CLI version baked into the adapter image, read from the `agentbox.cli-version` label that
        `make <agent>-image` records. None if the image predates the label (rebuild to get it)."""
        r = self.podman.run(["image", "inspect", self.adapter.image, "--format",
                             '{{index .Labels "agentbox.cli-version"}}'], check=False)
        v = (r.stdout or "").strip()
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
        moved or was replaced)."""
        out = {}
        for c in self.covered_paths():
            try:
                st = os.lstat(c)
            except OSError:
                continue
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
        """The cover/mask paths the session PLAN creates, as real paths: secret masks and mask dirs, the
        `.env` dev-swap target (the real file behind the swap), and the read-only git hooks/config and
        compose covers. This is the single source both `covered_paths()` (what the gate enforces) and the
        pre-session backup's exclude list are built from, so a cover added in one place cannot be missed by
        the other. `covered_paths()` adds the adapter's project covers on top (known only after setup)."""
        roots = {os.path.realpath(r) for r in self.bind_roots}
        covered = set()
        for m in (self.plan.mounts if self.plan else []):
            dst = os.path.realpath(m.dst)
            if m.kind in ("mask", "tmpfs"):
                covered.add(dst)
            elif m.kind == "bind" and dst not in roots and (m.ro or os.path.realpath(m.src) != dst):
                # git hooks/config and compose covers (ro, dst==src); the .env dev-swap (dst != src): a spawned
                # container binding the root would see the real file, not the empty/dev substitute
                covered.add(dst)
        return covered

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
    try:
        summary, text = scan.report(doc["state"], doc["grants"], skip=doc.get("skip"))
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
    import shutil
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
        for c in podman.ids("container", sel):
            podman.run(["rm", "-f", "-v", "-t", "5", c], check=False)     # -v: its anonymous volumes too
        for pod in podman.ids("pod", sel):
            podman.run(["pod", "rm", "-f", pod], check=False)
        for v in podman.ids("volume", sel):
            podman.run(["volume", "rm", "-f", v], check=False)
        for n in podman.ids("network", sel):
            podman.run(["network", "rm", "-f", n], check=False)
        if settings.cgroup_parent:
            # the per-session build cgroup the gate names; empty once the builds are gone
            build_cg = "/sys/fs/cgroup" + buildcap.slice_cgroup_path(settings.cgroup_parent) + f"/{session}-build"
            try:
                os.rmdir(build_cg)
            except OSError:
                pass
        shutil.rmtree(service_tmp(settings, session), ignore_errors=True)
        _scan(settings, session, started, warn)
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
    p = podman.run(["network", "ls", "--format", "{{.Name}} {{index .Labels \"agentbox.session\"}}"]
                   + sum((["--filter", f"label={k}={v}"] for k, v in settings.labels.items()), []), check=False)
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out.setdefault(parts[1], {"containers": [], "networks": []})["networks"].append(parts[0])
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
    try:
        return time.time() - os.stat(settings.session_run_dir(session)).st_mtime < seconds
    except OSError:
        return False


def gc(settings, podman, warn=lambda m: None):
    """Tear down sessions whose launcher is gone. A session whose agent container is still running is left
    alone even if its metadata is missing or unreadable (ab session kill removes it explicitly); an orphaned proxy
    or network with no running agent is removed."""
    removed, kept = [], set()
    for s, info in sessions(settings, podman).items():
        agent_running = [c["name"] for c in info["containers"]
                         if c.get("state") == "running" and c.get("role") == "agent"]
        if not info["alive"] and agent_running:
            warn(f"agentbox: {s} has no live launcher but its agent is running; left alone (use `ab session kill {s}`)")
            kept.add(s)
            continue
        if not info["alive"] and info["meta"] is None and _young(settings, s):
            continue                     # being started right now: session.json is not written yet
        if not info["alive"]:
            teardown(settings, podman, s, warn=warn)
            removed.append(s)
    # A podman service whose session is dead, even if its run dir and containers are already gone.
    live = {s for s, info in sessions(settings, podman).items() if info["alive"]} | kept
    for pid, s in sockguard.services_under(settings.run_dir).items():
        if s not in live:
            sockguard.kill_service(pid)
            warn(f"agentbox: stopped an orphaned podman service of {s} (pid {pid})")
            removed.append(f"{s} (podman service)")
    removed += gc_images(settings, podman, warn=warn)
    return removed
