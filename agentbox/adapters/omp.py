"""OMP (oh-my-pi) adapter.

Only named pieces of the host's ~/.omp are mounted, all read-only (docs/decisions.md, "What the OMP session
mounts from ~/.omp"): plugins, repos and pylibs from their actual link targets (they may sit in different
versions), the version directories those live in or the plugins link into, agent/config.yml,
bin/mags-codedev and the Playwright browser cache. Everything else in ~/.omp (models.yml and its backup,
.mcp.json, mags-codedev.yaml, extensions, sessions, databases, logs, caches, run/) never enters a session.

Per project and persistent, read-write: ~/.omp/agent (sessions, OMP's databases) and ~/.omp/logs, from
~/.local/share/agentbox/<project>/omp/. Generated per session, read-only: agent/models.yml (only providers
with a key file, plus the local model server) and agent/AGENTS.md. The rest of ~/.omp (run/, caches) is the
session's tmpfs home.
"""
import ipaddress
import os
import re
import stat
from urllib.parse import urlsplit

from .. import keys as keysmod
from .. import miniyaml
from ..config import HOME
from ..errors import AgentboxError
from . import Adapter, SessionSetup

C_OMP = os.path.join(HOME, ".omp")                     # ~/.omp inside the session
PROVISIONED = ("plugins", "repos", "pylibs")
READONLY_FILES = ("agent/config.yml", "bin/mags-codedev")
READONLY_DIRS = (".cache/ms-playwright",)
# Files Tim creates himself under ~/.config/agentbox/omp/ (never copied from ~/.omp, never parsed or cleaned:
# he is responsible for what is in them), mounted read-only at OMP's own path when they exist.
USER_FILES = (("mcp.json", "agent/.mcp.json"), ("mags-codedev.yaml", "agent/mags-codedev.yaml"))
# Places in the per-project agent dir OMP loads code or code-running config from. They are covered read-only
# (empty) so one session cannot plant something every later session of the project runs (review 2026-10-01
# #2). Prompt-only dirs (skills, rules, prompts, agents, commands) stay writable: OMP's autolearn writes there.
CODE_DIRS = ("agent/extensions", "agent/hooks", "agent/tools")
CODE_FILES = ("agent/.mcp.json", "agent/mcp.json", "agent/settings.json")
SECRET_HEADER = re.compile(r"auth|key|token|secret|cookie|session", re.I)
LOCAL_PROVIDER = "llama-cpp"
LOCKDOWN_OVERLAY = f"{C_OMP}/agentbox-lockdown.yml"
# Code a repo can ship for OMP (or that OMP reads from Claude's layout). In lockdown each one that exists is
# covered, empty and read-only: --no-extensions does not stop custom tools (itest, 1 Oct).
REPO_CODE = (".omp/extensions", ".omp/hooks", ".omp/tools", ".omp/mcp.json", ".omp/.mcp.json",
             ".claude/extensions", ".claude/hooks", ".claude/tools", ".mcp.json")
LOCKDOWN_SETTINGS = "mcp:\n  enableProjectConfig: false\n"
PATH = f"{C_OMP}/bin:/root/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _plain(path, omp_home, what):
    """A file or dir in ~/.omp that is not a symlink and resolves inside ~/.omp."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise AgentboxError(f"{what} {path} is a symlink; refusing to mount it")
    if not _inside(os.path.realpath(path), os.path.realpath(omp_home)):
        raise AgentboxError(f"{what} {path} resolves outside {omp_home}")
    return os.path.realpath(path)


VERSION_REF = re.compile(r"\.omp/versions/([A-Za-z0-9._-]+)/")


def _version_dir(omp_home, v):
    """~/.omp/versions/<v>, which must be a real directory (not a symlink) directly in versions/."""
    vdir = os.path.join(omp_home, "versions", v)
    if v in ("", ".", "..") or "/" in v or os.path.islink(vdir) or not os.path.isdir(vdir):
        raise AgentboxError(f"{vdir} is not a real version directory; refusing to mount it")
    return vdir


def provisioned_targets(omp_home):
    """{name: real dir} for plugins, repos and pylibs: each link's actual target (they may point into different
    versions). Each must be exactly ~/.omp/versions/<v>/<name> with <v> a real directory (review 2026-10-01
    #3: "anywhere inside ~/.omp" let `pylibs -> .` mount the whole of it, keys included)."""
    omp_home = os.path.realpath(omp_home)
    out = {}
    for name in PROVISIONED:
        link = os.path.join(omp_home, name)
        if not os.path.lexists(link):
            continue
        real = os.path.realpath(link)
        if not os.path.isdir(real):
            raise AgentboxError(f"{link} points to {os.readlink(link) if os.path.islink(link) else link}, "
                                "which is missing; run omp-env install")
        v = _version_of(real, omp_home)
        if not v or real != os.path.join(omp_home, "versions", v, name):
            raise AgentboxError(f"{link} resolves to {real}, not to {omp_home}/versions/<version>/{name}; "
                                "refusing to mount it")
        _version_dir(omp_home, v)
        out[name] = real
    return out


# Credential files in ~/.omp. A hard link to one inside a mounted version would carry it into the sandbox.
CREDENTIAL_FILES = ("agent/models.yml", "agent/models.yml.backup", "agent/mags-codedev.yaml", "agent/.mcp.json",
                    "agent/mcp.json")
URL_CREDENTIALS = re.compile(r"^\s*(?:url|pushurl)\s*=\s*\S*://[^/\s@]+@", re.M | re.I)


def check_no_credentials(omp_home, version_dirs):
    """Refuse if a credential file in ~/.omp has another hard link, or if a git repo in a mounted version has
    credentials in a remote URL (the credential rule: they never enter a sandbox)."""
    for rel in CREDENTIAL_FILES:
        p = os.path.join(omp_home, rel)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            raise AgentboxError(f"{p} has {st.st_nlink - 1} other hard link(s); one may sit in a mounted "
                                "version. Find it with `find ~/.omp -samefile <file>`; refusing to start")
    for vdir in version_dirs:
        repos = os.path.join(vdir, "repos")
        if not os.path.isdir(repos):
            continue
        for name in sorted(os.listdir(repos)):
            cfg = os.path.join(repos, name, ".git", "config")
            try:
                with open(cfg) as f:
                    text = f.read()
            except OSError:
                continue
            if URL_CREDENTIALS.search(text):
                raise AgentboxError(f"{cfg} has credentials in a remote URL; they would enter the sandbox. "
                                    "Remove them (e.g. `git remote set-url`), then start again")


def _version_of(path, omp_home):
    """The version name if `path` (a host path, or a /root/.omp path as omp-env install writes them) lies in
    ~/.omp/versions/<version>/, else None."""
    for prefix in (os.path.join(omp_home, "versions") + "/", "/root/.omp/versions/"):
        if path.startswith(prefix):
            return path[len(prefix):].split("/", 1)[0] or None
    return None


def referenced_versions(omp_home, plugins_dir):
    """Versions the plugins point into: absolute links in node_modules (and @scope/ dirs) and paths in
    package.json / omp-plugins.lock.json. Link targets and file text are read, nothing is followed."""
    found = set()
    nm = os.path.join(plugins_dir, "node_modules")
    entries = []
    if os.path.isdir(nm):
        for e in os.listdir(nm):
            p = os.path.join(nm, e)
            if e.startswith("@") and os.path.isdir(p) and not os.path.islink(p):
                entries += [os.path.join(p, x) for x in os.listdir(p)]
            else:
                entries.append(p)
    for p in entries:
        if os.path.islink(p):
            target = os.readlink(p)
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(os.path.dirname(p), target))
            v = _version_of(target, omp_home)
            if v:
                found.add(v)
    for fn in ("package.json", "omp-plugins.lock.json"):
        try:
            with open(os.path.join(plugins_dir, fn)) as f:
                found |= set(VERSION_REF.findall(f.read()))
        except OSError:
            pass
    return found


def omp_mounts(omp_home, warn):
    """[(host src, container dst)] from the host's ~/.omp, all read-only, and a check that the provisioned
    links still point where they did (run again just before the container starts).

    Each of plugins/repos/pylibs is mounted from its actual target, and every version directory those
    targets live in, or that the plugins' own links point into, is mounted whole at ~/.omp/versions/<v>."""
    omp_home = os.path.realpath(omp_home)
    versions_dir = os.path.join(omp_home, "versions")
    targets = provisioned_targets(omp_home)
    out, versions = [], set()
    for name, real in targets.items():
        v = _version_of(real, omp_home)
        if v:
            versions.add(v)
        out.append((real, os.path.join(C_OMP, name)))
    if "plugins" in targets:
        versions |= referenced_versions(omp_home, targets["plugins"])
    mounted_versions = []
    for v in sorted(versions):
        vdir = os.path.join(versions_dir, v)
        if not os.path.lexists(vdir):
            warn(f"agentbox: {omp_home}/plugins refers to version {v}, which is missing; those plugins will "
                 "not load (run omp-env install)")
            continue
        _version_dir(omp_home, v)
        mounted_versions.append(vdir)
        out.insert(0, (vdir, os.path.join(C_OMP, "versions", v)))
    check_no_credentials(omp_home, mounted_versions)
    if not targets:
        warn(f"agentbox: {omp_home} has no provisioned plugins/repos/pylibs; OMP runs without extensions "
             "(run omp-env install)")
    for rel in READONLY_FILES:
        p = os.path.join(omp_home, rel)
        if os.path.lexists(p):
            src = _plain(p, omp_home, "OMP file")
            if not os.path.isfile(src):
                raise AgentboxError(f"{p} is not a regular file")
            out.append((src, os.path.join(C_OMP, rel)))
    for rel in READONLY_DIRS:
        p = os.path.join(omp_home, rel)
        if os.path.lexists(p):
            out.append((_plain(p, omp_home, "OMP directory"), os.path.join(C_OMP, rel)))

    def unchanged():
        now = provisioned_targets(omp_home)
        for vdir in mounted_versions:
            _version_dir(omp_home, os.path.basename(vdir))
        if now != targets:
            raise AgentboxError(f"{omp_home}: plugins/repos/pylibs changed while the session was starting "
                                "(an omp-env install or rollback?); start it again")
    return out, unchanged


def _walk_strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


def _host_of(url, where):
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise AgentboxError(f"{where}: bad baseUrl {url!r}")
    if parts.scheme not in ("http", "https") or not host:
        raise AgentboxError(f"{where}: baseUrl {url!r} must be http(s)://host/...")
    return host, port or (443 if parts.scheme == "https" else 80)


def _ip(host):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def build_models(seed, *, keys, model_host, model_port, warn):
    """The session's models.yml from the keyless seed.

    Returns (doc, domains, used_keys): providers are kept only when their key file exists, except the local
    model server, which is always there. Each kept key is referenced by its variable name (AB_KEY_<NAME>);
    its API domains are returned for the proxy's model allowlist.
    """
    seed = seed or {}
    if not isinstance(seed, dict) or not isinstance(seed.get("providers", {}), dict):
        raise AgentboxError("OMP model seed: expected a top-level `providers:` mapping")
    doc = {k: v for k, v in seed.items() if k != "providers"}
    providers, domains, used = {}, set(), {}
    have_local = False
    for name, conf in (seed.get("providers") or {}).items():
        where = f"OMP model seed, provider {name!r}"
        keysmod.check_provider(name)
        conf = dict(conf or {})
        if "apiKey" in conf:
            raise AgentboxError(f"{where}: remove `apiKey`; keys live only in ~/.config/agentbox/keys/ "
                                f"(`ab keys set {name}`)")
        for h in (conf.get("headers") or {}):
            if SECRET_HEADER.search(str(h)):
                raise AgentboxError(f"{where}: header {h!r} looks like a credential; use `ab keys set {name}`")
        if any(s.startswith("!") for s in _walk_strings(conf)):
            raise AgentboxError(f"{where}: `!command` values are not allowed in the seed")
        host = port = None
        if conf.get("baseUrl"):
            host, port = _host_of(conf["baseUrl"], where)
        ip = _ip(host) if host else None
        local = ip is not None and str(ip) == str(model_host) and port == int(model_port)
        if local:
            have_local = True
            if name in keys:
                conf["apiKey"] = keysmod.env_name(name)
                used[name] = keys[name]
            else:
                conf["apiKey"] = "none"
                warn(f"agentbox: OMP local model provider {name!r} has no key; if the server needs one, "
                     f"`ab keys set {name}`")
            providers[name] = conf
            continue
        if name not in keys:
            warn(f"agentbox: OMP provider {name!r} left out: no key (`ab keys set {name}`)")
            continue
        want = set(keysmod.PROVIDER_DOMAINS.get(name, []))
        if host and ip is None:
            want.add(host.lower())
        elif ip is not None and not ip.is_loopback:
            warn(f"agentbox: OMP provider {name!r} left out: {host} is not reachable from a session "
                 "(only public names and the local model server are)")
            continue
        if not want and not (ip is not None and ip.is_loopback):
            warn(f"agentbox: OMP provider {name!r} left out: no known API domain and no baseUrl")
            continue
        conf["apiKey"] = keysmod.env_name(name)
        providers[name] = conf
        used[name] = keys[name]
        domains |= want
    if not have_local:
        conf = {"api": "openai-completions", "baseUrl": f"http://{model_host}:{int(model_port)}/v1"}
        if LOCAL_PROVIDER in keys:
            conf["apiKey"] = keysmod.env_name(LOCAL_PROVIDER)
            used[LOCAL_PROVIDER] = keys[LOCAL_PROVIDER]
        else:
            conf["apiKey"] = "none"
        providers = {LOCAL_PROVIDER: conf, **providers}
    for name in sorted(set(keys) - set(used)):
        warn(f"agentbox: key for {name!r} not used: no such provider in the OMP model seed")
    doc["providers"] = providers
    return doc, sorted(domains), used


def _prepare_file(path):
    """An empty regular file as a mount point in the state dir (replacing a symlink the agent may have left)."""
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isfile(path)):
        if os.path.isdir(path) and not os.path.islink(path):
            raise AgentboxError(f"{path} is a directory; expected a file")
        os.unlink(path)
    if not os.path.exists(path):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))


class OmpAdapter(Adapter):
    def omp_home(self, settings):
        return os.path.expanduser(settings.omp_home)

    def prepare_session(self, sess, sandbox_notes):
        s = sess.settings
        state = sess.state_dir
        agent_dir, logs_dir = os.path.join(state, "agent"), os.path.join(state, "logs")
        for d in (state, agent_dir, logs_dir):
            if os.path.islink(d):
                raise AgentboxError(f"{d} is a symlink; refusing to use it as session state")
            os.makedirs(d, mode=0o700, exist_ok=True)

        seed_path = os.path.join(s.seed_dir(self.name), "models.yml")
        seed = None
        if os.path.isfile(seed_path):
            with open(seed_path) as f:
                try:
                    seed = miniyaml.loads(f.read())
                except AgentboxError as e:
                    raise AgentboxError(f"{seed_path}: {e}")
        doc, domains, used = build_models(seed, keys=keysmod.present(s), model_host=s.model_host,
                                          model_port=s.model_port, warn=sess.warn)
        run_dir = s.session_run_dir(sess.name)
        gen = {}
        for fn, text in (("models.yml", miniyaml.dumps(doc)), ("AGENTS.md", sandbox_notes + _omp_notes(doc))):
            path = os.path.join(run_dir, f"omp-{fn}")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            gen[fn] = path
        # ~/.omp is a tmpfs owned by the session user (run/, caches, logs of this session), with the named
        # pieces mounted on top; without it the runtime creates it root-owned and OMP cannot write there.
        setup = SessionSetup(mount_root=C_OMP, tmpfs=[C_OMP], model_domains=domains, keys=used)
        setup.env.update({"PATH": PATH, "PIP_CACHE_DIR": "/tmp/pip-cache"})
        mounts, unchanged = omp_mounts(self.omp_home(s), sess.warn)
        setup.verify.append(unchanged)
        if any(dst == os.path.join(C_OMP, "pylibs") for _src, dst in mounts):
            setup.env["PYTHONPATH"] = os.path.join(C_OMP, "pylibs")
        setup.mounts.append((agent_dir, os.path.join(C_OMP, "agent"), False))
        setup.mounts.append((logs_dir, os.path.join(C_OMP, "logs"), False))
        setup.mounts += [(src, dst, True) for src, dst in mounts]
        for rel in CODE_DIRS:
            setup.ro_empty_dirs.append(os.path.join(C_OMP, rel))
        if sess.project.value("approvals") != "off":
            overlay = os.path.join(run_dir, "omp-lockdown.yml")
            fd = os.open(overlay, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(LOCKDOWN_SETTINGS)
            setup.mounts.append((overlay, LOCKDOWN_OVERLAY, True))
            root = sess.project.root
            setup.project_covers += [os.path.join(root, rel) for rel in REPO_CODE
                                     if os.path.lexists(os.path.join(root, rel))]
        empty = os.path.join(run_dir, "omp-empty")
        os.close(os.open(empty, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        user_dsts = {os.path.join(C_OMP, rel) for fn, rel in USER_FILES
                     if os.path.isfile(os.path.join(s.seed_dir(self.name), fn))
                     and not os.path.islink(os.path.join(s.seed_dir(self.name), fn))}
        for rel in CODE_FILES:
            dst = os.path.join(C_OMP, rel)
            if dst not in user_dsts:
                _prepare_file(os.path.join(agent_dir, os.path.basename(rel)))
                setup.mounts.append((empty, dst, True))
        missing = []
        for fn, rel in USER_FILES:
            src = os.path.join(s.seed_dir(self.name), fn)
            if os.path.isfile(src) and not os.path.islink(src):
                _prepare_file(os.path.join(agent_dir, os.path.basename(rel)))
                setup.mounts.append((src, os.path.join(C_OMP, rel), True))
            else:
                missing.append(fn)
        if missing:
            sess.warn(f"agentbox: no {' or '.join(missing)} in {s.seed_dir(self.name)}; OMP starts without "
                      "them (create them there yourself to use MCP servers or MAGS-CodeDev settings)")
        for fn, path in gen.items():
            _prepare_file(os.path.join(agent_dir, fn))
            setup.mounts.append((path, os.path.join(C_OMP, "agent", fn), True))
        if any(dst == os.path.join(C_OMP, "agent", "config.yml") for _s, dst in mounts):
            _prepare_file(os.path.join(agent_dir, "config.yml"))
        return setup


def _omp_notes(doc):
    names = ", ".join(sorted(doc.get("providers", {}))) or "none"
    return ("\n## OMP in this sandbox\n\n"
            f"- Model providers configured for this session: {names}. Other providers have no key here.\n"
            "- `~/.omp/agent/models.yml`, `config.yml` and this file are read-only and regenerated each session.\n"
            "- `~/.omp/plugins`, `repos`, `pylibs` and `versions` are read-only; install Python packages with "
            "`pip install --user` or into a project virtualenv instead.\n"
            "- The browser tool uses the system Chromium through the proxy.\n")


ADAPTER = OmpAdapter(
    name="omp",
    image="localhost/agentbox-omp:latest",
    command=["omp"],
    model_domains=[],
    env={"NO_UPDATE_NOTIFIER": "1"},
    required_tools=["bash", "git", "omp", "bun", "python", "chromium", "podman", "podman-compose"],
    approvals_off_args=["--approval-mode=yolo"],
    # With approvals on (lockdown), code shipped in the repo (.omp/, .claude/: extensions, hooks, tools, MCP
    # servers) must not run unasked. OMP cannot switch off only the repo's extensions (its native loader
    # reads .omp/ and ~/.omp/agent together), so lockdown drops extension discovery (Tim's plugins too) and
    # the overlay turns off project MCP config (decisions.md, 1 Oct).
    approvals_on_args=["--approval-mode=always-ask", "--no-extensions", f"--config={LOCKDOWN_OVERLAY}"],
    proxy_url_env=["PUPPETEER_PROXY"],      # OMP passes only this to Chromium as --proxy-server
)
