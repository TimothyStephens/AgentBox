"""OMP (oh-my-pi) adapter.

Only named pieces of the host's ~/.omp are mounted, all read-only (docs/decisions.md, "What the OMP session
mounts from ~/.omp"): plugins, repos and pylibs from their actual link targets (they may sit in different
versions), the version directories those live in or the plugins link into, and bin/mags-codedev. Everything
else in ~/.omp (models.yml and its backup, .mcp.json, mags-codedev.yaml, config.yml, extensions, sessions,
databases, logs, caches, run/) never enters a session.

Shrinking that list is the point: `config.yml` and the Playwright cache left it on 4 Nov, so the only
remaining reason a session reads ~/.omp at all is the plugins/repos/pylibs snapshot -- plus
bin/mags-codedev, which is a wrapper into repos/ and so goes with the same decision (TODO.md 1.1).

Per project and persistent, read-write: ~/.omp/agent (sessions, OMP's databases) and ~/.omp/logs, from
~/.local/share/agentbox/<project>/omp/. Generated per session, read-only: agent/models.yml (only providers
with a key file, plus the local model server) and agent/AGENTS.md. The rest of ~/.omp (run/, caches) is the
session's tmpfs home.
"""
import ipaddress
import os
import re
from urllib.parse import urlsplit

from .. import keys as keysmod
from .. import miniyaml
from ..config import HOME
from ..errors import AgentboxError
from . import Adapter, SessionSetup

C_OMP = os.path.join(HOME, ".omp")                     # ~/.omp inside the session
# The installed plugin tree and the marketplace registry beside it, at the paths OMP decides. A session
# mounts them READ-ONLY from `~/.config/agentbox/omp/plugins`, which `ab profile install omp` builds.
# These are the only two things a session gets for plugins, and nothing at all comes from the host's
# ~/.omp any more (5 Nov; installer.py and decisions.md say what that replaced and why).
TREE_DST = os.path.join(C_OMP, "plugins")
MARKETPLACES_DST = os.path.join(C_OMP, "marketplaces.json")
# Files Tim creates himself under ~/.config/agentbox/omp/ (never copied from ~/.omp, never parsed or cleaned:
# he is responsible for what is in them), mounted read-only at OMP's own path when they exist.
#
# `config.yml` joined them on 4 Nov. It used to come from `~/.omp/agent/config.yml` -- the host OMP's OWN
# config -- so changing the default model of the OMP Tim runs himself changed the default model of every
# agentbox session with it. agentbox's copy is now the only one a session sees.
USER_FILES = (("config.yml", "agent/config.yml"),
              ("mcp.json", "agent/.mcp.json"), ("mags-codedev.yaml", "agent/mags-codedev.yaml"))
# Places in the per-project agent dir OMP loads code or code-running config from. They are covered read-only
# (empty) so one session cannot plant something every later session of the project runs (review 2026-10-01
# #2). Prompt-only dirs (skills, rules, prompts, agents, commands) stay writable: OMP's autolearn writes there.
CODE_DIRS = ("agent/extensions", "agent/hooks", "agent/tools")
# Autolearn's own output directory. OMP writes learned skills ONLY here; `agent/skills` holds authored
# skills and autolearn never writes to it, and both are discovered unconditionally
# (oh-my-pi: packages/coding-agent/src/autolearn/managed-skills.ts, discovery/builtin.ts, docs/skills.md).
# That is what makes a read-only profile at agent/skills safe: see docs/decisions.md, 29 Oct.
AUTOLEARN_DIR = "agent/managed-skills"
# The profile's load paths: host-owned skills, rules, prompts, sub-agents and commands, read-only. Authored
# content, which is exactly what the profile is -- so `agent/skills` is the right home for it.
PROFILE_DIRS = ("skills", "rules", "prompts", "agents", "commands")
CODE_FILES = ("agent/.mcp.json", "agent/mcp.json", "agent/settings.json")
SECRET_HEADER = re.compile(r"auth|key|token|secret|cookie|session", re.I)
LOCAL_PROVIDER = "llama-cpp"
LOCKDOWN_OVERLAY = f"{C_OMP}/agentbox-lockdown.yml"
# Code and config a repo can ship for OMP (or that OMP reads from Claude's layout). In lockdown each one that
# exists is covered, empty and read-only: --no-extensions does not stop custom tools (itest, 1 Oct). The
# project's .omp is covered whole rather than by a hand-picked list of its loaders (review step 5C, L1); Claude's
# settings files are where its hooks are declared.
REPO_CODE = (".omp", ".claude/extensions", ".claude/hooks", ".claude/tools", ".claude/settings.json",
             ".claude/settings.local.json", ".mcp.json")
LOCKDOWN_SETTINGS = "mcp:\n  enableProjectConfig: false\n"
PATH = f"{C_OMP}/bin:/root/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def plugin_mounts(settings, warn, plugins=True):
    """[(host src, container dst)] for the installed plugin tree, read-only. [] when there is nothing to
    mount, which is a normal state and not an error.

    **This replaced a whitelist.** Plugins used to come from the host's `~/.omp`, which only
    `omp-env install` produced, and most of `adapters/omp.py` existed because that directory was one
    agentbox did not control: the provisioned-link validation, the `versions/<v>` resolution, the
    referenced-version scan, the credential hard-link check and a mid-start recheck in case an
    `omp-env install` moved something underneath a starting session. agentbox owns this tree, builds it
    itself, and replaces it whole, so none of that is needed: a session mounts two paths read-only.

    `plugins=False` (`ab run --no-profile`, or `profile = "off"`) returns nothing. agentbox's own
    `config.yml` still arrives either way -- the flag turns off the profile and its plugins, not
    agentbox's configuration of the agent."""
    from .. import installer           # local: installer imports session, which imports this package
    if not plugins:
        return []
    tree = installer.tree_dir(settings, "omp")
    if os.path.islink(tree):
        raise AgentboxError(f"{tree} is a symlink; refusing to mount it")
    if not os.path.isdir(tree):
        warn(f"agentbox: no plugins installed ({tree} does not exist); OMP runs on its own built-ins. "
             "`ab profile install omp` installs what the profile's plugins.toml declares")
        return []
    out = [(tree, TREE_DST)]
    mk = os.path.join(tree, installer.MARKETPLACES_COPY)
    if os.path.islink(mk):
        raise AgentboxError(f"{mk} is a symlink; refusing to mount it")
    if os.path.isfile(mk):
        out.append((mk, MARKETPLACES_DST))
    else:
        warn(f"agentbox: the installed tree has no {installer.MARKETPLACES_COPY}, so marketplace plugins "
             f"will not load; `ab profile install omp` writes it")
    return out


def plugin_notes(settings, warn, image_labels, plugins=True):
    """Say what the tree is, and whether it still fits this image. Silent when there are no plugins."""
    from .. import installer, plugins as plugindecl
    if not plugins:
        return
    try:
        declared = plugindecl.load(os.path.join(settings.profile_src("omp"), "plugins.toml"))
    except AgentboxError as e:
        warn(f"agentbox: {e}")       # a broken plugins.toml must not stop a session from starting
        declared = None
    if not os.path.isdir(installer.tree_dir(settings, "omp")):
        return                        # plugin_mounts already said so
    for line in installer.staleness(settings, "omp", image_labels, declared=declared):
        warn(f"agentbox: {line}")


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
    def extra_profile_notes(self, inbox, kept=True):
        """Autolearn still works; what changes is where its output ends up."""
        where = (f"at the end of the session everything in it is moved to `{inbox}` for the user to review "
                 f"with `ab profile harvest` -- so learning something is worth doing, it just does not "
                 f"become a shared skill until a person promotes it"
                 if kept else "this session keeps nothing, so anything learned has to go in your reply")
        return [f"- `{C_OMP}/{AUTOLEARN_DIR}` is where your own learned skills go (autolearn writes there, "
                f"and only there). It starts empty every session and is not kept: {where}. "
                f"`{C_OMP}/agent/skills` and the other authored directories are the shared profile, "
                f"read-only."]

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
        on = not sess.profile_off[0]
        mounts = plugin_mounts(s, sess.warn, plugins=on)
        plugin_notes(s, sess.warn, sess.image_labels(), plugins=on)
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
                      f"them, on its own defaults. Create them there yourself -- config.yml is OMP's own "
                      f"config (a default model, for one), and it used to be read from the host's ~/.omp: "
                      f"`cp ~/.omp/agent/config.yml {s.seed_dir(self.name)}/` if you want what you had.")
        for fn, path in gen.items():
            _prepare_file(os.path.join(agent_dir, fn))
            setup.mounts.append((path, os.path.join(C_OMP, "agent", fn), True))
        return setup


def _omp_notes(doc):
    names = ", ".join(sorted(doc.get("providers", {}))) or "none"
    return ("\n## OMP in this sandbox\n\n"
            f"- Model providers configured for this session: {names}. Other providers have no key here.\n"
            "- Provider keys go through `ab keys` (regenerated into a keyless models.yml that is never mounted). "
            "config.yml comes from agentbox's own config, not from the host's ~/.omp; it and the plugins are "
            "mounted read-only in every session — never put secrets in either.\n"
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
    # The host-owned profile, read-only, at OMP's own load paths. Safe because autolearn writes only to
    # agent/managed-skills (AUTOLEARN_DIR above), never to agent/skills -- the question that held this back
    # until 29 Oct. If an installed OMP ever predates that split, these five mounts are what to drop.
    profile_mounts={d: f"{C_OMP}/agent/{d}" for d in PROFILE_DIRS},
    state_mirrors={f"{C_OMP}/agent": "agent"},      # ~/.omp/agent is a bind of <state>/agent
    # Autolearn's output: empty at the start of every session, harvested into the project's inbox at
    # teardown, and kept by nothing. A learned skill is a proposal, not something later sessions load.
    per_session_dirs=(("omp-managed-skills", f"{C_OMP}/{AUTOLEARN_DIR}"),),
    # The inbox IS wired: a read-write directory at a path OMP does not load from, inside its own agent dir
    # so it is in front of the agent. It is bound after `agent/` itself, so it survives that mount.
    inbox_mount=f"{C_OMP}/agent/inbox",
    proxy_url_env=["PUPPETEER_PROXY"],      # OMP passes only this to Chromium as --proxy-server
)
