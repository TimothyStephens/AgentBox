"""`ab profile install <agent>`: install the plugins the profile declares, in a sandbox, once.

**Why this exists.** OMP's plugins used to come from the host's `~/.omp/{plugins,repos,pylibs}`, which
only `omp-env install` produced -- on the host, with `--network=host` and `~/.omp` read-write. That left
agentbox depending on a tool it is meant to replace, and it left a drift nobody could see: plugins
installed against whatever OMP the host had, meeting the OMP the image pins. Here they are installed
**against the image's own OMP, node and Bun**, into a host-owned tree that sessions mount read-only.

**What the probe settled, and what each fact forced** (docs/decisions.md, 5 Nov):

- `omp plugin marketplace add` cannot be pinned to a commit, but **a local path is a valid source**. So
  agentbox clones the catalog itself, checks out the declared sha, and adds that path. Pinning is
  agentbox's job, which is where the profile's `ref` always meant to put it.
- for a local source **OMP copies the catalog** rather than cloning it, so the commit checked out here is
  exactly what ends up in the cache. That is what makes the pin mean anything.
- the tree records **absolute paths** -- `installPath`, `catalogPath`, `sourceUri`, the `node_modules`
  entries -- so the install must run with the tree mounted **where a session will mount it**
  (`~/.omp/plugins`). Installing at a host path would record paths that are wrong inside a session.
- `plugin install --json` prints human text, so what happened is read back from **state**
  (`omp plugin list --json`), not from what the install said.

**The container.** One run, no project: the profile is not a grant and an install is not a session, so
there is no lock, no scan, no backup and no gate -- just the same sealed pair of networks and the same
hardened squid a session gets (`session.net_create_argv`, `session.proxy_run_argv`, one definition used
twice), `packages` egress for npm and GitHub, **no model-host rule** (an install has no business reaching
a model API), and nothing mounted but the tree itself.

**Everything we write lives under `<tree>/.agentbox/`**: the catalog clones and checkouts (`sources/`, so
their recorded paths are valid in a session too), OMP's `marketplaces.json` copied out at the end (a
session gets it read-only; copied rather than bind-mounted, because a tool that rewrites a file atomically
cannot rename over a bind mount), the state OMP reported, and `install.json` -- the lockfile, which lives
with the tree because it describes the tree.
"""
import json
import os
import shlex
import time

from . import adapters, egress, hardening, plugins, profile, session as sessionmod
from .config import HOME
from .errors import AgentboxError

# Inside the host tree, which is mounted at ~/.omp/plugins. Dotted, so OMP's own discovery has no reason
# to walk it, and one directory rather than four so the tree has exactly one place that is ours.
OURS = ".agentbox"
SOURCES = f"{OURS}/sources"
STATE = f"{OURS}/state.json"
MARKETPLACES_COPY = f"{OURS}/marketplaces.json"
LOCKFILE = f"{OURS}/install.json"
LOCK_VERSION = 1
# Where the tree is mounted. Not configurable: OMP decides it, and the paths it records must resolve in a
# session as well as in the installer.
C_OMP = os.path.join(HOME, ".omp")
C_TREE = os.path.join(C_OMP, "plugins")


def tree_dir(settings, agent):
    """The host-owned installed tree: `~/.config/agentbox/<agent>/plugins`, beside the agent's config.yml
    and mcp.json. One place for everything of this agent's that the host owns."""
    return os.path.join(settings.seed_dir(agent), "plugins")


def lock_path(settings, agent):
    return os.path.join(tree_dir(settings, agent), LOCKFILE)


def read_lock(settings, agent):
    """The lockfile of the installed tree, or None. Never raises: this is read on the session path, where
    an unreadable lockfile means "cannot tell", and the caller reports rather than refuses."""
    try:
        with open(lock_path(settings, agent)) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def clone_url(source):
    """The https URL to clone a marketplace source from. GitHub shorthand (`owner/repo`) is what
    `/marketplace add` takes and what the profile uses; a URL is already one."""
    return source if source.startswith("https://") else f"https://github.com/{source}.git"


def _script(entries, *, tree=C_TREE):
    """The shell the install container runs: one line per step, in declaration order.

    Every interpolated value is shell-quoted even though `plugins.py` has already refused quotes, spaces
    and leading dashes -- the validator is the rule and this is the belt, and the two together are why a
    `ref` cannot become a git option.

    Marketplace catalogs and checkouts are cloned to a fixed depth-free clone and then checked out at the
    declared sha: `--depth 1` cannot reach an arbitrary commit, which is the mistake that would make the
    pin silently become "whatever HEAD was"."""
    q = shlex.quote
    src = f"{tree}/{SOURCES}"
    lines = ["set -eu",
             f"mkdir -p {q(src)}",
             # `omp plugin list --json` is the only reliable report (install prints human text), so the
             # run ends by writing it where the host can read it.
             ]
    for e in entries:
        if e.kind == "plugin":
            path = f"{src}/{e.marketplace}"
            lines += [f"rm -rf {q(path)}",
                      f"git clone --quiet {q(clone_url(e.source))} {q(path)}",
                      f"git -C {q(path)} checkout --quiet {q(e.ref)}",
                      f"git -C {q(path)} rev-parse HEAD | grep -Fxq {q(e.ref)}",
                      f"omp plugin marketplace add {q(path)}",
                      f"omp plugin install {q(e.id)}"]
        elif e.kind == "npm":
            lines.append(f"omp plugin install {q(e.name + '@' + e.version)}")
        else:
            path = f"{src}/{e.name}"
            lines += [f"rm -rf {q(path)}",
                      f"git clone --quiet {q(e.url)} {q(path)}",
                      f"git -C {q(path)} checkout --quiet {q(e.ref)}",
                      f"git -C {q(path)} rev-parse HEAD | grep -Fxq {q(e.ref)}",
                      f"omp plugin link {q(os.path.join(path, e.extension_dir) if e.extension_dir != '.' else path)}"]
    lines += [f"omp plugin list --json > {q(f'{tree}/{STATE}')}",
              # Copied, not bind-mounted: OMP rewrites this file, and a tool that writes atomically cannot
              # rename over a bind mount. A session gets this copy read-only.
              f"cp {q(os.path.join(C_OMP, 'marketplaces.json'))} {q(f'{tree}/{MARKETPLACES_COPY}')}"]
    return "\n".join(lines) + "\n"


class Install:
    """One install: its networks, its proxy, its container. Named like a session (`<prefix>install-<agent>`)
    and labelled the same way, so `make clean`, `ab session gc` and the leftover checks see it."""

    def __init__(self, settings, agent, *, podman=None, warn=None):
        self.settings = settings
        self.agent = agent
        self.adapter = adapters.get(agent)
        self.podman = podman or sessionmod.Podman(settings.podman)
        self.warn = warn or (lambda m: None)
        self.name = f"{settings.name_prefix}install-{agent}"
        self.tree = tree_dir(settings, agent)
        self.run_dir = os.path.join(settings.run_dir, "installs", self.name)

    # The install is not a project, but the labels have the same shape so every sweep that looks for
    # agentbox's own objects finds these too. "(install)" cannot collide with a project name: the registry
    # refuses a name with brackets in it.
    def labels(self, role):
        return sessionmod.labels_for(self.settings, "(install)", self.name, role)

    @property
    def int_net(self):
        return f"{self.name}-int"

    @property
    def egr_net(self):
        return f"{self.name}-egr"

    @property
    def proxy_name(self):
        return f"{self.name}-proxy"

    def squid_conf(self, dns=None):
        s = self.settings
        addrs = sorted(set(s.host_addrs) | set(egress.host_addresses()))
        if not addrs:
            raise AgentboxError("cannot tell the VM's own addresses; refusing to start a proxy without a "
                                "host-address rule")
        # `packages` for npm and GitHub, no model domains and NO MODEL HOST: an install fetches packages
        # and has no business reaching a model API, so the one private-address exception is left out.
        return egress.squid_conf("packages", model_domains=[], model_host="", model_port=s.model_port,
                                 host_addrs=addrs, dns=dns)

    def container_args(self, script):
        s = self.settings
        args = ["run", "--rm", f"--name={self.name}", f"--hostname={self.name}"]
        opts = [f"--label={k}={v}" for k, v in self.labels("install").items()]
        opts += hardening.fixed_flags(s, cgroup_parent=s.cgroup_parent)
        opts.append(f"--tmpfs={HOME}:rw,nosuid,nodev,mode=0700,U,notmpcopyup")
        opts.append(f"--network={self.int_net}")
        env = {"HOME": HOME, "AGENTBOX_SESSION": self.name, **egress.proxy_env(self.proxy_name)}
        opts += [f"--env={k}={v}" for k, v in env.items()]
        # The one thing mounted, read-write, AT THE PATH A SESSION WILL SEE IT AT.
        opts.append(f"--mount=type=bind,src={os.path.realpath(self.tree)},dst={C_TREE}")
        opts.append(f"--workdir={C_TREE}")
        opts.append("--entrypoint=/bin/sh")
        hardening.audit(opts + [self.adapter.image], allowed_mount_srcs=[os.path.realpath(self.tree)],
                        allowed_mount_dsts=[C_TREE], networks=[self.int_net], tmpfs_dirs=("/tmp", HOME))
        return args + opts + [self.adapter.image, "-c", script]

    def start_infra(self):
        pm, s = self.podman, self.settings
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        pm.run(sessionmod.net_create_argv(s, self.labels("network"), self.int_net, internal=True))
        pm.run(sessionmod.net_create_argv(s, self.labels("network"), self.egr_net, internal=False))
        gw = pm.run(["network", "inspect", self.egr_net, "--format", "{{range .Subnets}}{{.Gateway}} {{end}}"])
        dns = (gw.stdout or "").split()[0] if (gw.stdout or "").split() else None
        conf = os.path.join(self.run_dir, "squid.conf")
        fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.squid_conf(dns=dns))
        pm.run(sessionmod.proxy_run_argv(s, self.labels("proxy"), self.proxy_name,
                                         conf_path=conf, networks=[self.egr_net, self.int_net]))
        self._wait_proxy()

    def _wait_proxy(self):
        deadline = time.time() + float(self.settings.ready_timeout)
        while time.time() < deadline:
            logs = self.podman.run(["logs", self.proxy_name], check=False)
            text = (logs.stdout or "") + (logs.stderr or "")
            if sessionmod.SQUID_READY in text:
                return
            state = self.podman.run(["inspect", "-f", "{{.State.Status}}", self.proxy_name], check=False)
            if (state.stdout or "").strip() in ("exited", "stopped", "dead"):
                raise AgentboxError(f"the install proxy exited: {' '.join(text.strip().splitlines()[-3:])}")
            time.sleep(0.5)
        raise AgentboxError(f"the install proxy was not ready after {self.settings.ready_timeout}s")

    def teardown(self):
        """Always, on every path. An install leaves nothing running: not the container, not the proxy, not
        the networks."""
        for name in (self.name, self.proxy_name):
            self.podman.run(["rm", "-f", "-t", "2", name], check=False)
        for net in (self.int_net, self.egr_net):
            self.podman.run(["network", "rm", "-f", net], check=False)


def install(settings, agent, *, podman=None, warn=lambda m: None, dry_run=False):
    """Install what the agent's profile declares. Returns the lockfile document.

    The tree is replaced, not updated: an install is "make the tree be what the file says", so a plugin
    removed from `plugins.toml` is gone from the tree rather than left behind to be discovered later."""
    src = settings.profile_src(agent)
    entries = plugins.load(os.path.join(src, "plugins.toml"))
    if not entries:
        raise AgentboxError(f"{os.path.join(src, 'plugins.toml')} declares no plugins; nothing to install")
    job = Install(settings, agent, podman=podman, warn=warn)
    script = _script(entries)
    if dry_run:
        warn(f"agentbox: would install {plugins.summary(entries)} into {job.tree}")
        return {"dry_run": True, "script": script, "entries": [_declared(e) for e in entries]}

    pin = _image_pin(job)
    _fresh_tree(job.tree)
    warn(f"agentbox: installing {plugins.summary(entries)} into {job.tree} "
         f"(against {job.adapter.image}, {pin.get('cli_version') or 'unknown version'})")
    try:
        job.start_infra()
        p = job.podman.run(job.container_args(script), check=False)
        if p.returncode != 0:
            tail = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()[-8:]
            raise AgentboxError("the plugin install failed:\n  " + "\n  ".join(tail))
    finally:
        job.teardown()
    doc = _lock(settings, agent, entries, pin, job.tree)
    warn(f"agentbox: installed. {len(doc['state'].get('npm', []))} npm and "
         f"{len(doc['state'].get('marketplace', []))} marketplace plugin(s) reported by OMP")
    return doc


def _declared(entry):
    return {k: v for k, v in entry.__dict__.items() if k != "label"}


def _image_pin(job):
    """What the tree is being built against: the image, its id, the agent CLI version and the node major
    its labels record. This is what the staleness check compares, so a tree can say which OMP it was
    built for rather than being trusted blindly."""
    out = {"ref": job.adapter.image}
    try:
        doc = job.podman.image_json(job.adapter.image)
    except AgentboxError as e:
        raise AgentboxError(f"cannot inspect {job.adapter.image} ({e}); run `make {job.agent}-image` first")
    from . import userimage
    labels = userimage.image_labels(doc)
    out["id"] = doc.get("Id") or ""
    out["cli_version"] = (labels.get("agentbox.cli-version") or "").strip()
    out["node_major"] = (labels.get("agentbox.node-major") or "").strip()
    return out


def _fresh_tree(tree):
    """An install starts from an empty tree.

    Not an optimisation: OMP's `package.json`, `bun.lock` and `installed_plugins.json` are cumulative, so
    installing into a tree that already has plugins in it would leave the removed ones installed and make
    the lockfile a description of something else. A symlink anywhere in the path is refused rather than
    followed -- this function deletes."""
    import shutil
    if os.path.islink(tree):
        raise AgentboxError(f"{tree} is a symlink; refusing to replace it")
    parent = os.path.dirname(tree)
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.exists(tree):
        shutil.rmtree(tree)
    os.makedirs(os.path.join(tree, SOURCES), mode=0o700)


def _lock(settings, agent, entries, pin, tree):
    """Write `<tree>/.agentbox/install.json` from what was declared and what OMP reported."""
    state_path = os.path.join(tree, STATE)
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError) as e:
        raise AgentboxError(f"the install produced no readable state at {state_path} ({e}); "
                            "nothing is recorded, so the tree cannot be trusted")
    doc = {"version": LOCK_VERSION, "agent": agent, "installed_at": int(time.time()),
           "profile_hash": profile.tree_hash(settings.profile_src(agent)),
           "image": pin, "declared": [_declared(e) for e in entries], "state": state}
    path = os.path.join(tree, LOCKFILE)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)
    return doc
