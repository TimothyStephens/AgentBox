"""A registered project as a session sees it: the registry entry validated into a Project, its podman tiers,
and clone mode's per-session clone of its root. Moved out of session.py (item 54); session re-exports it."""
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from . import cgroup, mounts, paths, presets, projectkeys, registry, scan, toulmin
from .config import HOME
from .errors import AgentboxError


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
    mask: list = field(default_factory=list)   # the project's own masks (names and root-relative paths)
    toulmin: bool = False         # the Toulmin integration (toulmin.py): tm start/confirm/serve/end around the session
    mount: str = ""               # "checkout" | "clone" | "" (decide from toulmin and the root; see Session.mount_mode)
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
    reviews: str = ""             # the builder this project reviews (review.py): its root is a clone of the builder's
    lan_addr: str = ""            # `ab deploy`: the LAN address the live stack publishes on
    deploy_ports: list = field(default_factory=list)      # host ports it may publish
    deploy_binds: list = field(default_factory=list)      # host directories it may bind (checked by `ab deploy`)
    deploy_files: list = field(default_factory=list)      # untracked files copied into the deploy copy
    deploy_external: list = field(default_factory=list)   # external volumes/networks it may use
    deploy_adopted: list = field(default_factory=list)    # hand-run stack volumes adopted by name

    def value(self, key):
        return self.eff[key][0]


def _no_commas(path, what):
    if "," in path or "\n" in path:
        raise AgentboxError(f"{what} {path!r}: paths with commas or newlines cannot be mounted safely")


def mount_mode_of(root, mount, toulmin):
    """The rule behind Session.mount_mode, for callers that hold a registry entry rather than a session
    (review.sync): the stored `mount` wins; otherwise clone when Toulmin is on and the root is a git repository."""
    if mount:
        return mount
    return "clone" if toulmin and os.path.isdir(os.path.join(root, ".git")) else "checkout"


def _git_env():
    """The repo's own config as the only one for the launcher's git calls on agent-written trees: no system
    or global config (a `core.hooksPath` or `diff.external` there would run on the host), no prompts."""
    return dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")


def clone_root(settings, project_name, session, root):
    """Clone mode: `git clone --no-hardlinks <root> projects/<name>/clones/<session>/`, then a session branch.
    Returns {"clone", "branch", "base"}; base is the commit the clone was made from. The clone is kept after
    the session (`ab project clean` removes it). Nothing here touches the owner's repository: a clone reads
    it, and no push, merge or branch deletion is ever made there."""
    if not os.path.isdir(os.path.join(root, ".git")):
        raise AgentboxError(f"project {project_name}: mount = clone needs a git repository at {root} (no .git); "
                            "use mount = checkout, or `git init` it")
    base_dir = settings.clones_dir(project_name)
    os.makedirs(base_dir, mode=0o700, exist_ok=True)
    clone = os.path.join(base_dir, session)
    if os.path.lexists(clone):
        raise AgentboxError(f"{clone} already exists; `ab project clean {project_name}` removes old clones")
    branch = f"agentbox/{session}"
    # scan.GIT_SAFE: the clone is made from an agent-written tree, so nothing its config names may run here
    p = subprocess.run(["git", *scan.GIT_SAFE, "clone", "-q", "--no-hardlinks", root, clone], capture_output=True,
                       text=True, env=_git_env())
    if p.returncode != 0:
        raise AgentboxError(f"clone mode: `git clone` failed: {p.stderr.strip()[-400:]}")
    base = toulmin.head_of(clone)
    if not base:
        shutil.rmtree(clone, ignore_errors=True)
        raise AgentboxError(f"project {project_name}: {root} has no commits yet, so there is nothing to clone a "
                            "session branch from; commit first, or use mount = checkout")
    p = subprocess.run(["git", "-C", clone, *scan.GIT_SAFE, "checkout", "-q", "-b", branch], capture_output=True,
                       text=True, env=_git_env())
    if p.returncode != 0:
        shutil.rmtree(clone, ignore_errors=True)
        raise AgentboxError(f"clone mode: could not create branch {branch} in the clone: {p.stderr.strip()[-300:]}")
    return {"clone": clone, "branch": branch, "base": base}


def protected_extra(settings):
    """Launcher-owned paths no grant may include. The token directory is its own setting (`token_dir`), so
    it can sit outside the config dir and was not covered by it (item 39)."""
    from .keys import keys_dir
    return [settings.config_dir, settings.data_dir, settings.run_dir, keys_dir(settings),
            os.path.dirname(settings.token_file("claude"))]


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
                   mask=_check(name, "mask", p.get("mask", [])),
                   toulmin=_check(name, "toulmin", p.get("toulmin", False)),
                   mount=_check(name, "mount", p["mount"]) if p.get("mount") else "",
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
                   reviews=_check(name, "reviews", p["reviews"]) if p.get("reviews") else "",
                   lan_addr=p.get("lan_addr", ""), deploy_ports=list(p.get("deploy_ports", [])),
                   deploy_binds=list(p.get("deploy_binds", [])), deploy_files=list(p.get("deploy_files", [])),
                   deploy_external=list(p.get("deploy_external", [])),
                   deploy_adopted=list(p.get("deploy_adopted", [])),
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
