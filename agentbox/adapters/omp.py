"""OMP (oh-my-pi) adapter.

Only named pieces of the host's ~/.omp are mounted, all read-only (docs/decisions.md, "What the OMP session
mounts from ~/.omp"): the active provisioned version and its plugins/repos/pylibs, agent/config.yml,
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
SECRET_HEADER = re.compile(r"auth|key|token|secret|cookie|session", re.I)
LOCAL_PROVIDER = "llama-cpp"
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


def omp_mounts(omp_home, warn):
    """[(host src, container dst)] from the host's ~/.omp, all read-only. The provisioned dirs are symlinks
    into versions/<active>/; each must resolve into that one version dir."""
    omp_home = os.path.realpath(omp_home)
    out = []
    versions = os.path.join(omp_home, "versions")
    active = None
    for name in PROVISIONED:
        link = os.path.join(omp_home, name)
        if not os.path.lexists(link):
            continue
        real = os.path.realpath(link)
        vdir = os.path.dirname(real)
        if not os.path.isdir(real) or os.path.dirname(vdir) != versions or os.path.basename(real) != name:
            raise AgentboxError(f"{link} does not point into {versions}/<version>/{name}; run omp-env install")
        if active and vdir != active:
            raise AgentboxError(f"{omp_home}: plugins, repos and pylibs point at different versions "
                                "(an install may be running); try again when it has finished")
        active = vdir
        out.append((real, os.path.join(C_OMP, name)))
    if active:
        out.insert(0, (active, os.path.join(C_OMP, "versions", os.path.basename(active))))
    else:
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
    return out


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
        mounts = omp_mounts(self.omp_home(s), sess.warn)
        if any(dst == os.path.join(C_OMP, "pylibs") for _src, dst in mounts):
            setup.env["PYTHONPATH"] = os.path.join(C_OMP, "pylibs")
        setup.mounts.append((agent_dir, os.path.join(C_OMP, "agent"), False))
        setup.mounts.append((logs_dir, os.path.join(C_OMP, "logs"), False))
        setup.mounts += [(src, dst, True) for src, dst in mounts]
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
    required_tools=["bash", "git", "omp", "bun", "python", "chromium", "podman-compose"],
    approvals_off_args=["--approval-mode=yolo"],
    approvals_on_args=["--approval-mode=always-ask"],
    proxy_url_env=["PUPPETEER_PROXY"],      # OMP passes only this to Chromium as --proxy-server
)
