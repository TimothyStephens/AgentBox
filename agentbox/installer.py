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

**Claude Code's tree is simpler, for a reason the probe of 9 Nov found** (decisions.md). Claude Code loads
a marketplace *in place* when its source is a directory: `settings.json` can declare the marketplace
(`extraKnownMarketplaces`, source `directory`) and the plugin (`enabledPlugins`), and the agent registers
both at startup, reading the plugin from that directory and writing its own records into its config dir --
which in a session is the per-project state, read-write. So there is no `claude plugin install` step to
run here and nothing records an absolute path into the tree. The install is the pin and nothing else: each
catalog cloned into `<tree>/<marketplace>/` at the declared sha, checked, and a lockfile. The session mounts
the tree read-only at `/run/agentbox/claude/marketplaces` and generates the two settings keys from the same
`plugins.toml` (adapters/claude.py). A remote source declared in settings was tried and is not used: a
`github` or `git` source, with or without a `ref`, was silently not registered at startup, and the pin has
to be agentbox's in any case.

**Pi's tree is Claude's shape with npm in it** (18 Nov, docs/pi-proposal.md). Pi loads a package listed in
its settings by LOCAL PATH in place and never modifies it, so the install drives npm and git itself -- npm
packages into a prefix at `<tree>/npm` (install scripts never run), git packages checked out at their sha
under `<tree>/git` with their runtime dependencies -- and the session lists each package's path in the
settings.json it generates (adapters/pi.py). No `pi` command runs here (`_pi_script` says why).
"""
import fcntl
import json
import os
import secrets
import shlex
import shutil
import stat
import time

from . import adapters, egress, hardening, plugins, profile, session as sessionmod
from .config import HOME
from .errors import AgentboxError
from .fsutil import write_private

# Inside the host tree, which is mounted at ~/.omp/plugins. Dotted, so OMP's own discovery has no reason
# to walk it, and one directory rather than four so the tree has exactly one place that is ours.
OURS = ".agentbox"
SOURCES = f"{OURS}/sources"
STATE = f"{OURS}/state.json"
MARKETPLACES_COPY = f"{OURS}/marketplaces.json"
LOCKFILE = f"{OURS}/install.json"
LOCK_VERSION = 1
# The install container clones repositories and runs npm installs behind the proxy; the launcher's 120 s
# podman timeout is for questions, not for that (13 Nov: the first `ab profile install omp` on a cold host
# died at "podman run --rm timed out", the second got through in time).
RUN_TIMEOUT = 1800
# Where the tree is mounted. Not configurable: OMP decides it, and the paths it records must resolve in a
# session as well as in the installer.
C_OMP = os.path.join(HOME, ".omp")
C_TREE = os.path.join(C_OMP, "plugins")
# Claude Code's tree: one directory per marketplace, mounted read-only under the agent's config dir.
# Nothing Claude Code writes records this path except the `settings.json` the launcher generates per
# session, so the install mounts it here for the same reason OMP's is: one definition, used twice.
C_CLAUDE_TREE = "/run/agentbox/claude/marketplaces"
# Pi's tree (18 Nov): npm packages under `npm/`, git checkouts under `git/`, mounted read-only under the
# session's `~/.pi` tmpfs (adapters/pi.py TREE_DST, the same path). Pi records nothing about the tree: the
# session's generated settings.json names each package by its path here, which Pi loads in place.
C_PI = os.path.join(HOME, ".pi")
C_PI_TREE = os.path.join(C_PI, "packages")
TREE_DST = {"omp": C_TREE, "claude": C_CLAUDE_TREE, "pi": C_PI_TREE}


def tree_dst(agent):
    """The container path an agent's installed tree is mounted at, in the installer and in a session."""
    try:
        return TREE_DST[agent]
    except KeyError:
        raise AgentboxError(f"`ab profile install` does not know where {agent} loads plugins from")


def tree_dir(settings, agent):
    """The host-owned installed tree: `~/.config/agentbox/<agent>/plugins`, beside the agent's config.yml
    and mcp.json. One place for everything of this agent's that the host owns."""
    return os.path.join(settings.seed_dir(agent), "plugins")


def staging_dir(settings, agent):
    """Where an install is built: beside the tree, swapped in only when the whole install succeeded. A
    failed install (13 Nov: one dead `[[repo]]` entry) therefore leaves the previous tree, and the plugins
    it holds, exactly as they were, instead of an emptied directory with no lockfile."""
    return tree_dir(settings, agent) + ".building"


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


def staleness(settings, agent, image_labels, declared=None):
    """Lines a session should say out loud about the installed tree. [] when there is nothing to say.

    Three questions, each answered only when it CAN be -- an unreadable anything produces silence rather
    than a guess, because this reports and decides nothing:

      * is there a lockfile? A tree without one was not put there by `ab profile install`.
      * was it built against this image? The drift this whole feature exists to end is plugins installed
        against one OMP and loaded by another, so the agent CLI version and the node major are compared.
      * does it still match what the profile declares? Compared against the DECLARATION, entry by entry,
        not against the profile hash -- editing a skill changes the hash and has nothing to do with the
        plugins, and a warning that cries wolf is one nobody reads.
    """
    lock = read_lock(settings, agent)
    if lock is None:
        return [f"the installed plugin tree at {tree_dir(settings, agent)} has no readable "
                f"{LOCKFILE}, so what is in it cannot be told; `ab profile install {agent}` rebuilds it"]
    out = []
    was = lock.get("image") or {}
    for key, label, what in (("cli_version", "agentbox.cli-version", "agent version"),
                             ("node_major", "agentbox.node-major", "node major")):
        then, now = (was.get(key) or "").strip(), (image_labels.get(label) or "").strip()
        if then and now and then != now:
            out.append(f"plugins were installed against {what} {then}; this session runs {now}. "
                       f"`ab profile install {agent}` rebuilds them against this image")
    if declared is not None:
        if [_declared(e) for e in declared] != (lock.get("declared") or []):
            out.append(f"plugins.toml has changed since the last install; the tree is what the previous "
                       f"one declared. `ab profile install {agent}`")
    return out


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
            ext = os.path.join(path, e.extension_dir) if e.extension_dir != "." else path
            lines += [f"rm -rf {q(path)}",
                      f"git clone --quiet {q(e.url)} {q(path)}",
                      f"git -C {q(path)} checkout --quiet {q(e.ref)}",
                      f"git -C {q(path)} rev-parse HEAD | grep -Fxq {q(e.ref)}"]
            lines += _link_lines(e, ext)
    lines += [f"omp plugin list --json > {q(f'{tree}/{STATE}')}",
              # Copied, not bind-mounted: OMP rewrites this file, and a tool that writes atomically cannot
              # rename over a bind mount. A session gets this copy read-only.
              f"cp {q(os.path.join(C_OMP, 'marketplaces.json'))} {q(f'{tree}/{MARKETPLACES_COPY}')}"]
    return "\n".join(lines) + "\n"


# Claude Code's catalog manifest, and the python that checks a clone IS the marketplace the profile named
# and lists the plugin the profile asked for. Run in the install container, which has python3 from
# agentbox-base. Checked here, at install time, because a session that loads a catalog under the wrong name
# would enable `egm@egm` from a marketplace calling itself something else -- and Claude Code would say so
# only at startup, in a sandbox, to nobody.
CLAUDE_MANIFEST = ".claude-plugin/marketplace.json"
_CLAUDE_CHECK = ("import json,sys; d=json.load(open(sys.argv[1])); m,p=sys.argv[2],sys.argv[3]; "
                 "ok=d.get('name')==m and any(isinstance(x,dict) and x.get('name')==p "
                 "for x in d.get('plugins') or []); "
                 "sys.exit(0 if ok else 'marketplace %r does not name itself %r or has no plugin %r' "
                 "% (sys.argv[1], m, p))")
# What a Claude tree's state.json says: each marketplace directory, the commit it is at, and the plugin
# names its manifest lists. Written by the script, from the clones, so it reports what is in the tree and
# not what was asked for.
_CLAUDE_STATE = ("import json,subprocess,sys; out=[]\n"
                 "for arg in sys.argv[2:]:\n"
                 "    name, path = arg.split('=', 1)\n"
                 "    d = json.load(open(path + '/" + CLAUDE_MANIFEST + "'))\n"
                 "    sha = subprocess.run(['git', '-C', path, 'rev-parse', 'HEAD'], capture_output=True, "
                 "text=True, check=True).stdout.strip()\n"
                 "    out.append({'name': name, 'commit': sha, "
                 "'plugins': [x.get('name') for x in d.get('plugins') or [] if isinstance(x, dict)]})\n"
                 "json.dump({'marketplace': out}, open(sys.argv[1], 'w'), indent=1, sort_keys=True)\n")


def _claude_script(entries, *, tree=C_CLAUDE_TREE):
    """The shell a Claude install runs: clone each catalog at its sha into `<tree>/<marketplace>`, check it
    is the catalog the profile named, and write the state. No agent command runs: the session's generated
    settings.json is what makes Claude Code load the directory."""
    q = shlex.quote
    lines = ["set -eu"]
    seen = {}
    for e in entries:
        path = f"{tree}/{e.marketplace}"
        plugin = e.id.partition("@")[0]
        if e.marketplace in seen:
            # Two plugins from one catalog: one clone, and it must be the same pin, or the second entry
            # silently changes what the first one loads.
            if seen[e.marketplace] != e.ref:
                raise AgentboxError(f"plugins.toml: marketplace {e.marketplace!r} is declared at two different "
                                    f"refs ({seen[e.marketplace]}, {e.ref}); one catalog has one commit")
        else:
            seen[e.marketplace] = e.ref
            lines += [f"rm -rf {q(path)}",
                      f"git clone --quiet {q(clone_url(e.source))} {q(path)}",
                      f"git -C {q(path)} checkout --quiet {q(e.ref)}",
                      f"git -C {q(path)} rev-parse HEAD | grep -Fxq {q(e.ref)}"]
        lines.append(f"python3 -I -c {q(_CLAUDE_CHECK)} {q(f'{path}/{CLAUDE_MANIFEST}')} "
                     f"{q(e.marketplace)} {q(plugin)}")
    lines.append(f"mkdir -p {q(f'{tree}/{OURS}')}")
    lines.append(f"python3 -I -c {q(_CLAUDE_STATE)} {q(f'{tree}/{STATE}')} "
                 + " ".join(q(f"{m}={tree}/{m}") for m in seen))
    return "\n".join(lines) + "\n"


def _link_lines(e, ext):
    """`omp plugin link` needs a package.json at the path; without one it fails with a message that names
    neither the entry nor the ref (13 Nov, MAGS-CodeDev: the extension had been deleted upstream before the
    pinned ref). Say which `[[repo]]` and which ref before OMP gets to say less."""
    q = shlex.quote
    msg = (f"agentbox: [[repo]] {e.name}: no package.json at "
           f"{e.extension_dir if e.extension_dir != '.' else 'the checkout root'} of {e.url} at {e.ref}; "
           f"that is not an OMP extension (fix or remove the entry in plugins.toml)")
    return [f"test -f {q(ext + '/package.json')} || {{ echo {q(msg)} >&2; exit 1; }}",
            f"omp plugin link {q(ext)}"]


# What a Pi tree's state.json says, read from the tree after the installs: each npm package's version from
# its installed package.json (and the install fails if that is not the declared version), each checkout's
# commit. Arguments are `npm=<name>=<version>=<path>` and `repo=<name>=<sha>=<path>`.
_PI_STATE = ("import json,subprocess,sys; out={'npm': [], 'repo': []}\n"
             "for arg in sys.argv[2:]:\n"
             "    kind, name, pin, path = arg.split('=', 3)\n"
             "    if kind == 'npm':\n"
             "        v = json.load(open(path + '/package.json')).get('version')\n"
             "        if v != pin:\n"
             "            sys.exit('npm package %s is at %r, not the declared %s' % (name, v, pin))\n"
             "        out['npm'].append({'name': name, 'version': v, 'path': path})\n"
             "    else:\n"
             "        sha = subprocess.run(['git', '-C', path, 'rev-parse', 'HEAD'], capture_output=True, "
             "text=True, check=True).stdout.strip()\n"
             "        out['repo'].append({'name': name, 'commit': sha, 'path': path})\n"
             "json.dump(out, open(sys.argv[1], 'w'), indent=1, sort_keys=True)\n")
# npm's flags for every install, Pi's own (1.5.2, its package manager for npm sources) plus one:
#   --ignore-scripts  no package's install scripts run, here or anywhere (a package that needs one to build
#                     does not work under agentbox; that is the price, and it is said in design.md);
#   --legacy-peer-deps  no peer dependencies installed: Pi supplies `@earendil-works/pi-*` and `typebox` to
#                     its packages, and a physical copy of them breaks its module mapping (Pi's docs);
#   --no-audit --no-fund  no extra registry calls, no noise.
PI_NPM_FLAGS = "--no-audit --no-fund --ignore-scripts --legacy-peer-deps"


def pi_package_path(e, *, tree=C_PI_TREE):
    """Where an entry's Pi package is in the tree (the installer's and a session's path): an npm package in
    the prefix's node_modules, a git package at its checkout, or the `extension_dir` inside it."""
    if e.kind == "npm":
        return f"{tree}/npm/node_modules/{e.name}"
    path = f"{tree}/git/{e.name}"
    return path if e.extension_dir == "." else f"{path}/{e.extension_dir}"


def _pi_script(entries, *, tree=C_PI_TREE):
    """The shell a Pi install runs: npm and git driven directly, with the image's node, npm and git.

    Not `pi install`: it writes the package into settings.json (there is none worth writing in an install
    container), it would need PI_OFFLINE off, it runs npm with install scripts allowed, and it puts packages
    in `<agent-dir>/npm` and `<agent-dir>/git`, which a session covers empty and read-only. Driving npm and
    git here gives the same layout under the tree, with the pin checked, and nothing executed but npm and git.
    A session lists each package by its path, which Pi loads in place and never modifies."""
    q = shlex.quote
    npm_dir, git_dir = f"{tree}/npm", f"{tree}/git"
    lines = ["set -eu", f"mkdir -p {q(npm_dir)} {q(git_dir)}"]
    state = []
    if any(e.kind == "npm" for e in entries):
        # The prefix's own package.json, as Pi writes one before its first install, so npm installs here
        # and does not go looking for a project above it.
        lines.append(f"printf '%s\\n' {q(json.dumps({'name': 'agentbox-pi-packages', 'private': True}))} "
                     f"> {q(npm_dir + '/package.json')}")
    for e in entries:
        path = pi_package_path(e, tree=tree)
        if e.kind == "npm":
            lines.append(f"npm install --prefix {q(npm_dir)} {PI_NPM_FLAGS} {q(e.name + '@' + e.version)}")
            state.append(f"npm={e.name}={e.version}={path}")
            continue
        clone = f"{git_dir}/{e.name}"
        missing = (f"agentbox: [[repo]] {e.name}: no directory {e.extension_dir} in {e.url} at {e.ref} "
                   "(fix or remove the entry in plugins.toml)")
        lines += [f"rm -rf {q(clone)}",
                  f"git clone --quiet {q(e.url)} {q(clone)}",
                  f"git -C {q(clone)} checkout --quiet {q(e.ref)}",
                  f"git -C {q(clone)} rev-parse HEAD | grep -Fxq {q(e.ref)}",
                  f"test -d {q(path)} || {{ echo {q(missing)} >&2; exit 1; }}",
                  # A package without a package.json is a directory of conventional resource dirs, which
                  # Pi loads as it is; one with a package.json gets its runtime dependencies.
                  f"if [ -f {q(path + '/package.json')} ]; then npm install --prefix {q(path)} --omit=dev "
                  f"{PI_NPM_FLAGS}; fi"]
        state.append(f"repo={e.name}={e.ref}={clone}")
    lines.append(f"mkdir -p {q(f'{tree}/{OURS}')}")
    lines.append(f"python3 -I -c {q(_PI_STATE)} {q(f'{tree}/{STATE}')} " + " ".join(q(a) for a in state))
    return "\n".join(lines) + "\n"


def script_for(agent, entries):
    """The install script for an agent's declaration."""
    if agent == "claude":
        return _claude_script(entries)
    if agent == "pi":
        return _pi_script(entries)
    if agent == "omp":
        return _script(entries)
    raise AgentboxError(f"`ab profile install` has no installer for {agent}")


class Install:
    """One install: its networks, its proxy, its container. Named like a session (`<prefix>install-<agent>-<run>`)
    and labelled the same way, so `make clean`, `ab session gc` and the leftover checks see it."""

    def __init__(self, settings, agent, *, podman=None, warn=None):
        self.settings = settings
        self.agent = agent
        self.adapter = adapters.get(agent)
        self.podman = podman or sessionmod.Podman(settings.podman)
        self.warn = warn or (lambda m: None)
        # A fresh suffix per run (20 Nov): an interrupted install can leave podman's DNS record for its network
        # behind, and a later network of the same name was matched to it, so the proxy's name never resolved.
        self.name = f"{settings.name_prefix}install-{agent}-{secrets.token_hex(3)}"
        self.tree = tree_dir(settings, agent)
        self.staging = staging_dir(settings, agent)
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

    PROXY_WAIT = 30         # seconds an install waits for its proxy's name to resolve inside the container

    def proxy_wait(self):
        """The lines every install script starts with: wait until the proxy's name resolves in the container, and
        fail with what the container saw when it never does. `ab profile install omp` failed 4 of 4 with
        `Could not resolve proxy` on its first clone (19 Nov) while the same proxy answered `--resolve`; this
        either rides out a late name or reports the resolver state instead of a bare curl error."""
        p = shlex.quote(self.proxy_name)
        n = int(self.PROXY_WAIT)
        return (f"i=0; while ! getent hosts {p} >/dev/null 2>&1; do i=$((i+1)); "
                f"if [ $i -ge {n} ]; then echo \"agentbox: the install proxy {self.proxy_name} does not resolve in "
                f"the install container after {n}s; resolv.conf: $(tr '\\n' ' ' </etc/resolv.conf 2>/dev/null); "
                f"hosts: $(tr '\\n' ' ' </etc/hosts 2>/dev/null)\" >&2; exit 97; fi; sleep 1; done; "
                f"if [ $i -gt 0 ]; then echo \"agentbox: the install proxy {self.proxy_name} resolved after ${{i}}s\" "
                f">&2; fi; echo \"agentbox: install proxy: $(getent hosts {p} | head -1); HTTPS_PROXY=$HTTPS_PROXY\" >&2\n")

    def container_args(self, script, *, mount_tree=True):
        """`mount_tree=False` is `--resolve`: it only reads the network and writes nothing, so nothing of the
        host is mounted at all."""
        s = self.settings
        args = ["run", "--rm", f"--name={self.name}", f"--hostname={self.name}"]
        opts = [f"--label={k}={v}" for k, v in self.labels("install").items()]
        opts += hardening.fixed_flags(s, cgroup_parent=s.cgroup_parent)
        opts.append(f"--tmpfs={HOME}:rw,nosuid,nodev,mode=0700,U,notmpcopyup")
        tmpfs_dirs = ["/tmp", HOME]
        if self.agent == "omp":
            # ~/.omp has to be a tmpfs OF THE CONTAINER USER, exactly as the OMP adapter makes it for a
            # session (`SessionSetup(tmpfs=[C_OMP])`). Without this line podman creates `~/.omp` itself, as
            # the mount point for the tree below it -- owned by root inside the HOME tmpfs -- and the
            # container runs as the host uid under `--userns=keep-id`, so OMP's first write there fails:
            #   ✘ Failed to add marketplace: EACCES: permission denied, open '~/.omp/marketplaces.json.tmp'
            # (host, 5 Nov). Everything OMP writes outside the tree lands here and goes with the container;
            # what has to survive is copied into the tree by the script. A Claude install runs no agent
            # command and writes nothing outside the tree, so it needs no second tmpfs.
            opts.append(f"--tmpfs={C_OMP}:rw,nosuid,nodev,mode=0700,U,notmpcopyup")
            tmpfs_dirs.append(C_OMP)
        elif self.agent == "pi":
            # The same for ~/.pi, as the Pi adapter makes it for a session: the tree's mount point is made
            # inside a directory the container user owns, not one podman creates root-owned.
            opts.append(f"--tmpfs={C_PI}:rw,nosuid,nodev,mode=0700,U,notmpcopyup")
            tmpfs_dirs.append(C_PI)
        opts.append(f"--network={self.int_net}")
        env = {"HOME": HOME, "AGENTBOX_SESSION": self.name, **egress.proxy_env(self.proxy_name)}
        opts += [f"--env={k}={v}" for k, v in env.items()]
        # The one thing mounted, read-write, AT THE PATH A SESSION WILL SEE IT AT.
        dst = tree_dst(self.agent)
        srcs, dsts = ([os.path.realpath(self.staging)], [dst]) if mount_tree else ([], [])
        if mount_tree:
            opts.append(f"--mount=type=bind,src={srcs[0]},dst={dst}")
        opts.append(f"--workdir={dst if mount_tree else HOME}")
        opts.append("--entrypoint=/bin/sh")
        hardening.audit(opts + [self.adapter.image], allowed_mount_srcs=srcs,
                        allowed_mount_dsts=dsts, networks=[self.int_net], tmpfs_dirs=tuple(tmpfs_dirs))
        return args + opts + [self.adapter.image, "-c", self.proxy_wait() + script]

    def start_infra(self):
        pm, s = self.podman, self.settings
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        pm.run(sessionmod.net_create_argv(s, self.labels("network"), self.int_net, internal=True))
        pm.run(sessionmod.net_create_argv(s, self.labels("network"), self.egr_net, internal=False))
        gw = pm.run(["network", "inspect", self.egr_net, "--format", "{{range .Subnets}}{{.Gateway}} {{end}}"])
        dns = (gw.stdout or "").split()[0] if (gw.stdout or "").split() else None
        conf = os.path.join(self.run_dir, "squid.conf")
        write_private(conf, self.squid_conf(dns=dns))
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

    def diagnose(self):
        """What the host sees of a failed install before teardown: the proxy's state, its last log lines, and the
        containers on the install network (19 Nov: the OMP install's proxy name did not resolve in the container
        after the proxy had reported ready). Never raises."""
        out = []
        try:
            st = self.podman.run(["inspect", "-f", "{{.State.Status}} {{.State.ExitCode}} "
                                  "OOMKilled={{.State.OOMKilled}}", self.proxy_name], check=False)
            out.append(f"proxy {self.proxy_name}: {(st.stdout or st.stderr or '').strip() or 'not found'}")
            lg = self.podman.run(["logs", "--tail", "15", self.proxy_name], check=False)
            tail = ((lg.stdout or "") + (lg.stderr or "")).strip().splitlines()[-15:]
            out += ["proxy log: " + line for line in tail]
            net = self.podman.run(["network", "inspect", self.int_net, "--format",
                                   "{{range .Containers}}{{.Name}} {{end}}"], check=False)
            out.append(f"on {self.int_net}: {(net.stdout or net.stderr or '').strip() or 'nothing'}")
        except Exception as e:                       # diagnosis must not hide the install's own error
            out.append(f"(could not read the install's state: {e!r})")
        return out

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
    entries = plugins.load(os.path.join(src, "plugins.toml"), plugins.sections_for(agent))
    if not entries:
        raise AgentboxError(f"{os.path.join(src, 'plugins.toml')} declares no plugins; nothing to install")
    job = Install(settings, agent, podman=podman, warn=warn)
    script = script_for(agent, entries)
    if dry_run:
        warn(f"agentbox: would install {plugins.summary(entries)} into {job.tree}")
        return {"dry_run": True, "script": script, "entries": [_declared(e) for e in entries]}

    pin = _image_pin(job)
    # One install at a time per tree, for the whole of it: two at once would build in the same staging
    # directory and swap over each other. The lock file stays (removing it would reopen the race).
    held = _take_lock(job.tree, agent)
    try:
        if os.path.islink(job.tree):
            raise AgentboxError(f"{job.tree} is a symlink; refusing to replace it")
        _recover_previous(job.tree, warn)
        _fresh_tree(job.staging)
        warn(f"agentbox: installing {plugins.summary(entries)} into {job.tree} "
             f"(against {job.adapter.image}, {pin.get('cli_version') or 'unknown version'})")
        try:
            try:
                job.start_infra()
                p = job.podman.run(job.container_args(script), check=False, timeout=RUN_TIMEOUT)
                if p.returncode != 0:
                    tail = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()[-8:]
                    raise AgentboxError("the plugin install failed:\n  " + "\n  ".join(tail + job.diagnose()))
            finally:
                job.teardown()
            _check_links(job.staging)
            doc = _lock(settings, agent, entries, pin, job.staging)
        except BaseException as e:
            # Whatever went wrong -- a refusal, a traceback, Ctrl-C -- the half-built staging tree goes,
            # or the next install would start from it, or a session would find `.building` beside the tree.
            shutil.rmtree(job.staging, ignore_errors=True)
            if not isinstance(e, AgentboxError):
                raise
            kept = (f"\nThe previous tree at {job.tree} is kept as it was."
                    if os.path.isdir(job.tree) else "")
            raise AgentboxError(f"{e}{kept}") from None
        _swap_in(job.staging, job.tree)
    finally:
        os.close(held)
    counts = ", ".join(f"{len(v or [])} {kind}" for kind, v in sorted(doc["state"].items())
                       if isinstance(v, list))
    warn(f"agentbox: installed. The tree reports {counts or 'nothing'}")
    return doc


# ---- `ab profile install <agent> --resolve`: what each pinned entry would be pinned to today -------------
RESOLVE_TAG = "AB-RESOLVE"
# The registry's `latest` dist-tag for one package, read in the container (python3 comes with agentbox-base;
# urllib honours HTTPS_PROXY). A scoped name keeps its `@` and has its `/` escaped, as the registry expects.
_NPM_LATEST = ("import json,sys,urllib.parse,urllib.request; "
               "u='https://registry.npmjs.org/'+urllib.parse.quote(sys.argv[1], safe='@')+'/latest'; "
               "print(json.load(urllib.request.urlopen(u, timeout=60))['version'])")


def _resolve_url(e):
    return clone_url(e.source) if e.kind == "plugin" else e.url


def resolve_script(entries):
    """One line of `AB-RESOLVE<TAB>index<TAB>ok|err<TAB>text` per entry. A git source is asked for its HEAD
    (the default branch: plugins.toml names no branch) with `git ls-remote`, which needs no clone; an npm
    package for its `latest` version. One failing entry does not stop the others."""
    q = shlex.quote
    lines = ["set -u", "err=/tmp/ab-resolve.err"]
    for i, e in enumerate(entries):
        cmd = (f"python3 -I -c {q(_NPM_LATEST)} {q(e.name)}" if e.kind == "npm"
               else f"git ls-remote {q(_resolve_url(e))} HEAD")
        lines.append(f'if out=$({cmd} 2>"$err"); then '
                     f"printf '{RESOLVE_TAG}\\t%s\\tok\\t%s\\n' {i} \"$out\"; else "
                     f"printf '{RESOLVE_TAG}\\t%s\\terr\\t%s\\n' {i} \"$(tr '\\n\\t' '  ' <\"$err\" | tail -c 300)\"; fi")
    return "\n".join(lines) + "\n"


def declared_pin(e):
    return e.version if e.kind == "npm" else e.ref


def parse_resolve(stdout, entries):
    """[{entry, declared, current, error}] in declaration order, from the script's tagged lines."""
    got = {}
    for line in (stdout or "").splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4 and parts[0] == RESOLVE_TAG and parts[1].isdigit():
            got[int(parts[1])] = (parts[2], parts[3].strip())
    out = []
    for i, e in enumerate(entries):
        status, text = got.get(i, ("err", "no answer from the resolve container"))
        current, error = None, None
        if status == "ok":
            current = text.split()[0] if text.split() else ""
            if e.kind != "npm" and not plugins.SHA40.match(current):
                current, error = None, f"no HEAD sha in `git ls-remote` output: {text[:120]!r}"
            elif e.kind == "npm" and not plugins.NPM_VERSION.match(current):
                current, error = None, f"the registry gave {text[:60]!r}, not a version"
        else:
            error = text or "failed"
        out.append({"entry": e, "declared": declared_pin(e), "current": current, "error": error})
    return out


def resolve(settings, agent, *, podman=None, warn=lambda m: None):
    """What every entry in the agent's plugins.toml would be pinned to today. Changes nothing: no tree, no
    lock, no staging directory; the container mounts nothing of the host."""
    src = settings.profile_src(agent)
    entries = plugins.load(os.path.join(src, "plugins.toml"), plugins.sections_for(agent))
    if not entries:
        raise AgentboxError(f"{os.path.join(src, 'plugins.toml')} declares no plugins; nothing to resolve")
    job = Install(settings, agent, podman=podman, warn=warn)
    job.name = f"{settings.name_prefix}resolve-{agent}"          # never the same containers as an install
    job.run_dir = os.path.join(settings.run_dir, "installs", job.name)
    warn(f"agentbox: asking the sources of {plugins.summary(entries)} for their current versions")
    try:
        job.start_infra()
        p = job.podman.run(job.container_args(resolve_script(entries), mount_tree=False), check=False,
                           timeout=RUN_TIMEOUT)
    finally:
        job.teardown()
    if RESOLVE_TAG not in (p.stdout or ""):
        tail = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()[-8:]
        raise AgentboxError("the resolve container failed:\n  " + "\n  ".join(tail))
    return parse_resolve(p.stdout, entries)


def _take_lock(tree, agent):
    """An exclusive flock on `<tree>.lock`, held until the returned fd is closed. A second install of the
    same agent is refused at once rather than queued: the one running will leave the tree the file
    describes, and a queued one would only rebuild the same thing."""
    path = tree + ".lock"
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise AgentboxError(f"cannot open the install lock {path}: {e}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise AgentboxError(f"another `ab profile install {agent}` is running (it holds {path}); "
                            "wait for it, or stop it first")
    return fd


def _recover_previous(tree, warn):
    """An install interrupted between the two renames of `_swap_in` leaves the last good tree at
    `<tree>.previous` and nothing at `<tree>`. Put it back before anything else happens, so a failure of
    this install still has a previous tree to keep, and a session started meanwhile finds one. A symlink at
    `.previous` is not ours (the host writes a directory there and nothing else) and is refused."""
    old = tree + ".previous"
    if os.path.islink(old):
        raise AgentboxError(f"{old} is a symlink; refusing to remove or rename through it")
    if os.path.isdir(old) and not os.path.lexists(tree):
        os.rename(old, tree)
        warn(f"agentbox: an earlier install was interrupted while swapping trees; {old} is back at {tree}")


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


def _swap_in(staging, tree):
    """The built tree takes the tree's place; the old one goes only after the new one is in place. A session
    that has the old tree mounted keeps it (a bind mount follows the inode, not the name).

    `.previous` is only ever a directory this function made. A symlink there would have `rmtree` skip it
    (it refuses links) and the rename of a directory onto it fail, so it is refused by name instead; a
    leftover directory (an interrupted earlier swap, with a tree already in place) is just removed."""
    old = tree + ".previous"
    if os.path.islink(old):
        raise AgentboxError(f"{old} is a symlink; refusing to remove or rename through it")
    shutil.rmtree(old, ignore_errors=True)
    if os.path.lexists(tree):
        os.rename(tree, old)
    os.rename(staging, tree)
    shutil.rmtree(old, ignore_errors=True)


def _fresh_tree(tree):
    """An install starts from an empty tree.

    Not an optimisation: OMP's `package.json`, `bun.lock` and `installed_plugins.json` are cumulative, so
    installing into a tree that already has plugins in it would leave the removed ones installed and make
    the lockfile a description of something else. A symlink anywhere in the path is refused rather than
    followed -- this function deletes."""
    if os.path.islink(tree):
        raise AgentboxError(f"{tree} is a symlink; refusing to replace it")
    parent = os.path.dirname(tree)
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.exists(tree):
        shutil.rmtree(tree)
    os.makedirs(os.path.join(tree, SOURCES), mode=0o700)


def _check_links(root, mounted_at=C_TREE):
    """Refuse a symlink in the staging tree that points out of it: an absolute target, or a relative one
    that resolves above `root`. One exception: `omp plugin link` writes `node_modules/<pkg>` as an ABSOLUTE
    link to `<mounted_at>/.agentbox/sources/<name>` (the tree as the container and every session see it).
    Under `node_modules/` an absolute target below `mounted_at` is judged as the same path inside `root`, so
    it is kept when it stays in the tree (review 16 Nov, R-4: refusing it broke every `[[repo]]` install).
    Nowhere else: the host reads `.agentbox/`, and on the host that absolute path is not the staging tree.

    The tree is the output of a container that ran plugin install scripts, so nothing in it is trusted.
    The host is about to read `state.json` from it, write the lockfile into it, and mount it into every
    later session; a link that leaves the tree would make the first read a host file and the last show it
    to every session. Relative links that stay inside are what npm writes in `node_modules/.bin`, so they
    are kept. Every link is judged by its own target (not followed), so a chain through a second link is
    caught at that link."""
    root = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                continue
            target = os.readlink(path)
            rel = os.path.relpath(path, root).split(os.sep)
            if (os.path.isabs(target) and rel[0] == "node_modules"
                    and os.path.normpath(target).startswith(mounted_at.rstrip("/") + "/")):
                resolved = os.path.join(root, os.path.relpath(os.path.normpath(target), mounted_at))
            elif os.path.isabs(target):
                resolved = None
            else:
                resolved = os.path.normpath(os.path.join(dirpath, target))
            if resolved is None or not (resolved == root or resolved.startswith(root + os.sep)):
                raise AgentboxError(f"the install left a symlink pointing out of the tree: "
                                    f"{os.path.relpath(path, root)} -> {target}; refusing to keep it")


def _own_dir(path):
    """`<tree>/.agentbox` must be the directory `_fresh_tree` made, not something the container put in its
    place: O_NOFOLLOW guards only the last path component, so the directory above it is checked here."""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise AgentboxError(f"the install left no {path} ({e}); nothing is recorded, so the tree cannot be "
                            "trusted")
    if not stat.S_ISDIR(st.st_mode):
        raise AgentboxError(f"{path} is not a directory (the install replaced it); refusing to write the "
                            "lockfile through it")


def _clear(path):
    """Remove what the container may have left at a path only the host writes. A symlink or a file is
    unlinked (the write below creates the file afresh, O_EXCL|O_NOFOLLOW); a directory is refused."""
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
    else:
        raise AgentboxError(f"{path} is a directory; the install put it there, and the lockfile goes here")


def _lock(settings, agent, entries, pin, tree):
    """Write `<tree>/.agentbox/install.json` from what was declared and what OMP reported.

    Both the read and the write refuse to follow a symlink: the tree was written by the install container,
    and a `state.json` pointing at a host file would be read as the state, a `install.json.tmp` pointing at
    one would be truncated."""
    _own_dir(os.path.join(tree, OURS))
    state_path = os.path.join(tree, STATE)
    try:
        if os.path.islink(state_path) or not stat.S_ISREG(os.lstat(state_path).st_mode):
            raise AgentboxError(f"{state_path} is not a regular file")
        fd = os.open(state_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            state = json.load(f)
    except (OSError, ValueError, AgentboxError) as e:
        raise AgentboxError(f"the install produced no readable state at {state_path} ({e}); "
                            "nothing is recorded, so the tree cannot be trusted")
    doc = {"version": LOCK_VERSION, "agent": agent, "installed_at": int(time.time()),
           "profile_hash": profile.tree_hash(settings.profile_src(agent)),
           "image": pin, "declared": [_declared(e) for e in entries], "state": state}
    path = os.path.join(tree, LOCKFILE)
    _clear(path + ".tmp")
    _clear(path)
    write_private(path, json.dumps(doc, indent=1, sort_keys=True))
    return doc
