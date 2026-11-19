"""Pi (`@earendil-works/pi-coding-agent`, CLI `pi`) adapter (docs/pi-proposal.md; decided 18 Nov 2026).

Pi is the project OMP forked from. It is treated like OMP and Claude -- per-project state, the host-owned
profile, the inbox, keys only through `ab keys`, Toulmin through MCP -- with Pi's own file names and rules.
`~/.pi` in a session is a tmpfs of the session user, with on top:

  * **per project, read-write:** `~/.pi/agent` from `<data>/projects/<project>/pi/agent` (Pi's sessions,
    its databases), with the places Pi loads code from covered empty and read-only (CODE_DIRS). Files Pi
    would LOAD from there at the next start (a SYSTEM.md replaces the system prompt, AGENTS.override.md the
    session notes, trust.json trusts a repo, themes) are moved to the inbox at every start, so one session
    cannot steer the next (Tim chose per-project state with that over a fresh dir each session, 18 Nov;
    decisions.md). An empty cover is not an option for SYSTEM.md: an existing file, even empty, IS the prompt;
  * **generated per session, read-only:** `agent/models.json` (P4: the OMP seed
    `~/.config/agentbox/omp/models.yml` through the same `build_models` checks, keys as `${AB_KEY_<NAME>}`;
    the local model server's models are asked from it at start when the seed does not list them, because Pi
    needs a list and cannot discover them itself, `local_models`),
    `agent/AGENTS.md` (the session notes), `agent/auth.json` = `{}` (P2: `/login` cannot store a key) and
    `agent/mcp.json` (no servers, or Toulmin's, `mcp_server`);
  * **copied in fresh at each start:** `agent/settings.json`, from `~/.config/agentbox/pi/settings.json` when
    there is one, else `{}`, with the code-running keys refused and project trust and install telemetry forced
    off. Pi writes it in place, so it cannot be a read-only mount;
  * **the host-owned profile:** skills at `agent/skills`, prompt templates at `agent/prompts`, read-only;
  * **Pi packages** (step 2): the tree `ab profile install pi` builds from the Pi profile's `plugins.toml`
    (installer.py), mounted read-only at `~/.pi/packages`; the generated settings.json lists each declared
    package the tree holds by its path there (`packages`), which Pi loads in place and never modifies. The
    tree's lockfile names the image it was built against, and the session says so when that or the
    declaration has changed since (`package_setup`).

Every session runs `pi --no-approve` (the repo's `.pi`, its packages and `.agents/skills` are never trusted),
with PI_OFFLINE=1 (no package auto-install, update check, tool download) and PI_TELEMETRY=0. Pi has no tool
approvals, so it is refused under a preset with approvals on (P1).
"""
import http.client
import json
import os
import time
import urllib.error
import urllib.request

from .. import keys as keysmod
from .. import miniyaml
from ..config import HOME
from ..errors import AgentboxError
from ..fsutil import write_private
from . import Adapter, SessionSetup
from .omp import _host_of, _ip, _prepare_file, build_models

C_PI = os.path.join(HOME, ".pi")                       # ~/.pi inside the session
C_AGENT = os.path.join(C_PI, "agent")
# Where Pi loads code from in its agent dir: extensions, git and npm packages, the bin dir it puts first on
# the bash tool's PATH, deprecated tools, and its temp dir.
CODE_DIRS = ("agent/extensions", "agent/git", "agent/npm", "agent/bin", "agent/tools", "agent/tmp")
# Prompt and trust files Pi loads from its agent dir at start: moved to the inbox before every session.
LOADED_AT_START = ("SYSTEM.md", "APPEND_SYSTEM.md", "AGENTS.override.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD",
                   "trust.json", "themes")
# settings.json keys that make Pi run, install or fetch something, or move its state: refused in the user's seed
# (packages come from `ab profile install pi`, never from a settings file).
CODE_SETTINGS = ("packages", "extensions", "shellPath", "shellCommandPrefix", "npmCommand", "selfUpdateCommand",
                 "httpProxy", "sessionDir")
FORCED_SETTINGS = {"defaultProjectTrust": "never", "enableInstallTelemetry": False}
REPO_CODE = (".pi",)                                   # a second layer behind --no-approve
SETTINGS_SEED = "settings.json"
# The installed package tree (installer.C_PI_TREE, the same path: the installer mounts it here too).
TREE_DST = os.path.join(C_PI, "packages")
MODELS_SEED = ("omp", "models.yml")                    # P4: the OMP seed, shared
# Pi's ids for agentbox's provider names (OMP's seed spells Gemini `gemini`), and the providers Pi has built in:
# those may appear with only a key (their models are Pi's own); any other provider needs a baseUrl and models.
PI_PROVIDER = {"gemini": "google"}
PI_BUILTIN = ("anthropic", "openai", "deepseek", "google", "mistral", "groq", "xai", "openrouter")
MODEL_FIELDS = {"maxOut": "maxTokens"}                  # OMP's spelling -> Pi's
# The local model server's /models, asked by the launcher on the host at session start (local_models).
DISCOVERY_TIMEOUT = 3                                   # seconds; a session start waits at most this long for it
DISCOVERY_MAX_BYTES = 1 << 20
DISCOVERY_MAX_MODELS = 200


def settings_doc(seed_dir):
    """The user's settings.json for Pi, checked, with the forced values applied; {} plus those when there is none."""
    path = os.path.join(seed_dir, SETTINGS_SEED)
    doc = {}
    if os.path.isfile(path) and not os.path.islink(path):
        try:
            with open(path) as f:
                doc = json.load(f)
        except ValueError as e:
            raise AgentboxError(f"{path}: not valid JSON ({e})")
        if not isinstance(doc, dict):
            raise AgentboxError(f"{path}: expected a JSON object")
        bad = [k for k in CODE_SETTINGS if k in doc]
        if bad:
            raise AgentboxError(f"{path}: {', '.join(bad)} would make Pi run, install or fetch code, or move its "
                                f"state; remove {'it' if len(bad) == 1 else 'them'} (Pi packages come from "
                                "`ab profile install pi`)")
    return {**doc, **FORCED_SETTINGS}


def pi_models(doc, warn, discover=None, seed="~/.config/agentbox/omp/models.yml"):
    """OMP's models document (after build_models, keys named AB_KEY_<NAME>) as Pi's models.json: Pi's provider
    ids, `maxTokens`, and every key as `${AB_KEY_<NAME>}` (a bare value is sent literally since Pi 0.77). A
    provider Pi does not know needs a baseUrl and models, or it is left out with a note.

    `discover(name, conf)` fills a missing list: it returns the model ids, None when it does not apply to that
    provider (only the local model server is asked), or raises OSError/ValueError, and then the note says what
    to add to the seed."""
    providers = {}
    for name, conf in (doc.get("providers") or {}).items():
        pid = PI_PROVIDER.get(name, name)
        conf = dict(conf)
        if conf.get("models"):
            conf["models"] = [{MODEL_FIELDS.get(k, k): v for k, v in m.items()} if isinstance(m, dict) else m
                              for m in conf["models"]]
        key = str(conf.get("apiKey", ""))
        if key.startswith("AB_KEY_"):
            conf["apiKey"] = "${" + key + "}"
        if pid not in PI_BUILTIN and conf.get("baseUrl") and not conf.get("models") and discover is not None:
            try:
                ids = discover(name, conf)
                if ids is not None and not ids:
                    raise ValueError("it lists no models")
            except (OSError, ValueError) as e:
                warn(f"agentbox: Pi provider {name!r} left out: Pi needs its model list, and the model server at "
                     f"{conf['baseUrl']} did not give one ({e}). To use it in Pi, list its models in {seed}:\n"
                     + _seed_snippet(name, conf))
                continue
            if ids is not None:
                conf["models"] = [{"id": i} for i in ids]
        if pid not in PI_BUILTIN and not (conf.get("models") and conf.get("baseUrl")):
            warn(f"agentbox: Pi provider {name!r} left out: a provider Pi does not know needs a baseUrl and models")
            continue
        providers[pid] = conf
    return {"providers": providers}


def _seed_snippet(name, conf):
    """The seed entry that gives Pi the provider's models (a list in the seed is used as it is, by OMP too)."""
    return (f"  providers:\n    {name}:\n      api: {conf.get('api') or 'openai-completions'}\n"
            f"      baseUrl: {conf['baseUrl']}\n      models:\n        - id: <a model id the server serves>\n")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "a redirect, which is not followed", headers, fp)


def fetch_models(url, key=None, timeout=DISCOVERY_TIMEOUT):
    """The model ids an OpenAI-compatible server lists at `url` (`<baseUrl>/models`: `{"data": [{"id": ...}]}`).

    Direct, never through a proxy (the local model server is on the LAN), no redirects followed, a short
    timeout and a size cap. The key, when given, goes only into the Authorization header and is never in an
    error. Raises OSError (network, HTTP) or ValueError (not a model list)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if key:
        req.add_unredirected_header("Authorization", f"Bearer {key}")
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(DISCOVERY_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise OSError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from None
    except http.client.HTTPException as e:
        raise OSError(f"a broken HTTP answer ({type(e).__name__})") from None
    if len(body) > DISCOVERY_MAX_BYTES:
        raise ValueError("its answer is too large for a model list")
    try:
        data = json.loads(body)["data"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("its answer is not an OpenAI-style model list") from None
    if not isinstance(data, list):
        raise ValueError("its answer is not an OpenAI-style model list")
    ids = []
    for m in data:
        i = m.get("id") if isinstance(m, dict) else None
        if isinstance(i, str) and 0 < len(i) <= 200 and i.isprintable() and i not in ids:
            ids.append(i)
    return ids[:DISCOVERY_MAX_MODELS]


def local_models(model_host, model_port, keys):
    """discover() for pi_models: asks only a provider whose baseUrl is the configured local model server
    (`model_host`:`model_port`), with that provider's key file when there is one; None for any other."""
    def discover(name, conf):
        host, port = _host_of(conf["baseUrl"], f"Pi provider {name!r}")
        ip = _ip(host)
        if not model_host or ip is None or str(ip) != str(model_host) or port != int(model_port):
            return None
        key = None
        if keys.get(name):
            with open(keys[name]) as f:
                key = f.readline().strip() or None
        return fetch_models(conf["baseUrl"].rstrip("/") + "/models", key)
    return discover


def package_setup(settings, warn, image_labels, packages=True):
    """([(host src, container dst)], [package paths]) for the installed Pi package tree. ([], []) when there is
    nothing to load, which is a normal state and not an error.

    Both come from the same declaration, read fresh, as Claude's plugins do: the tree is mounted whole, and a
    package is listed only when the profile declares it AND the tree holds it. One the tree lacks is named with
    the command that fixes it and left out (Pi would warn about a missing path inside the sandbox, to nobody).

    `packages=False` (`ab session start --no-profile`, or `profile = "off"`) returns nothing and reads nothing."""
    from .. import installer, plugins as plugindecl   # local: installer imports session, which imports this package
    if not packages:
        return [], []
    try:
        declared = plugindecl.load(os.path.join(settings.profile_src("pi"), "plugins.toml"),
                                   plugindecl.sections_for("pi"))
    except AgentboxError as e:
        warn(f"agentbox: {e}; no Pi packages are loaded this session")   # a broken file must not stop a session
        return [], []
    if not declared:
        return [], []
    tree = installer.tree_dir(settings, "pi")
    if os.path.islink(tree):
        raise AgentboxError(f"{tree} is a symlink; refusing to mount it")
    if not os.path.isdir(tree):
        warn(f"agentbox: no Pi packages installed ({tree} does not exist); Pi runs without "
             f"{', '.join(e.label for e in declared)}. `ab profile install pi` installs what the profile's "
             "plugins.toml declares")
        return [], []
    for line in installer.staleness(settings, "pi", image_labels, declared=declared):
        warn(f"agentbox: {line}")
    paths, missing = [], []
    for e in declared:
        host = installer.pi_package_path(e, tree=tree)
        if os.path.islink(host) or not os.path.isdir(host):
            missing.append(e.label)
            continue
        paths.append(installer.pi_package_path(e, tree=TREE_DST))
    if missing:
        warn(f"agentbox: the installed tree has no {', '.join(missing)}; not loaded this session. "
             "`ab profile install pi` rebuilds the tree from plugins.toml")
    if not paths:
        return [], []
    return [(tree, TREE_DST)], paths


def _move_loaded_files(agent_dir, inbox, warn):
    """Move what Pi would load from its per-project agent dir at start into the inbox (LOADED_AT_START)."""
    moved = []
    for name in LOADED_AT_START:
        src = os.path.join(agent_dir, name)
        if not os.path.lexists(src):
            continue
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        dst = os.path.join(inbox, f"pi-agent-{time.strftime('%Y%m%d-%H%M%S')}-{name}")
        os.rename(src, dst)
        moved.append(name)
    if moved:
        warn(f"agentbox: moved {', '.join(moved)} from Pi's per-project agent dir to the inbox ({inbox}): Pi would "
             "have loaded them at this start, so an earlier session could steer this one. Review them there")


class PiAdapter(Adapter):
    def mcp_server(self, sess, setup, name, server):
        """Toulmin through Pi's built-in MCP: a per-session `mcp.json` with the server, `exposure: direct` so its
        tools reach the model as tools (as with Claude and OMP), mounted read-only over the generated empty one."""
        dst = os.path.join(C_AGENT, "mcp.json")
        path = os.path.join(sess.settings.session_run_dir(sess.name), "pi-mcp.json")
        write_private(path, json.dumps({"mcpServers": {name: {**server, "exposure": "direct"}}}, indent=1))
        setup.mounts[:] = [m for m in setup.mounts if m[1] != dst]
        setup.mounts.append((path, dst, True))

    def prepare_session(self, sess, sandbox_notes):
        s, p = sess.settings, sess.project
        if p.value("approvals") != "off":
            raise AgentboxError(f"project {p.name}: Pi has no tool approvals, so it does not run under preset "
                                f"{p.preset}, which has approvals on; use claude or omp there, or a preset "
                                "with approvals off")
        state = sess.state_dir
        agent_dir = os.path.join(state, "agent")
        for d in (state, agent_dir):
            if os.path.islink(d):
                raise AgentboxError(f"{d} is a symlink; refusing to use it as session state")
            os.makedirs(d, mode=0o700, exist_ok=True)
        _move_loaded_files(agent_dir, sess.inbox, sess.warn)

        seed_path = os.path.join(s.seed_dir(MODELS_SEED[0]), MODELS_SEED[1])
        seed = None
        if os.path.isfile(seed_path) and not os.path.islink(seed_path):
            with open(seed_path) as f:
                try:
                    seed = miniyaml.loads(f.read())
                except AgentboxError as e:
                    raise AgentboxError(f"{seed_path}: {e}")
        doc, domains, used = build_models(seed, keys=keysmod.present(s), model_host=s.model_host,
                                          model_port=s.model_port, warn=sess.warn, label="Pi")
        settings = settings_doc(s.seed_dir(self.name))
        pkg_mounts, pkg_paths = package_setup(s, sess.warn, sess.image_labels(), packages=not sess.profile_off[0])
        if pkg_paths:
            # Added after settings_doc, which refuses `packages` in the user's seed: this list is agentbox's,
            # made from plugins.toml and the tree, and holds local paths only (nothing Pi would fetch).
            settings["packages"] = pkg_paths

        run_dir = s.session_run_dir(sess.name)
        gen = {}
        pi_doc = pi_models(doc, sess.warn, discover=local_models(s.model_host, s.model_port, used), seed=seed_path)
        for fn, text in (("models.json", json.dumps(pi_doc, indent=1)),
                         ("AGENTS.md", sandbox_notes + _pi_notes(pi_doc, pkg_paths)), ("auth.json", "{}\n"),
                         ("mcp.json", json.dumps({"mcpServers": {}}) + "\n")):
            path = os.path.join(run_dir, f"pi-{fn}")
            write_private(path, text)
            gen[fn] = path
        sp = os.path.join(agent_dir, "settings.json")
        if os.path.islink(sp) or (os.path.lexists(sp) and not os.path.isfile(sp)):
            raise AgentboxError(f"{sp} is not a regular file; refusing to write over it")
        write_private(sp, json.dumps(settings, indent=1) + "\n")

        setup = SessionSetup(mount_root=C_PI, tmpfs=[C_PI], model_domains=domains, keys=used)
        setup.env.update({"PI_OFFLINE": "1", "PI_TELEMETRY": "0", "PIP_CACHE_DIR": "/tmp/pip-cache"})
        setup.mounts.append((agent_dir, C_AGENT, False))
        setup.mounts += [(src, dst, True) for src, dst in pkg_mounts]
        for rel in CODE_DIRS:
            setup.ro_empty_dirs.append(os.path.join(C_PI, rel))
        for fn, path in gen.items():
            _prepare_file(os.path.join(agent_dir, fn))
            setup.mounts.append((path, os.path.join(C_AGENT, fn), True))
        root = p.root
        setup.project_covers += [os.path.join(root, rel) for rel in REPO_CODE
                                 if os.path.lexists(os.path.join(root, rel))]
        return setup


def _pi_notes(doc, packages=()):
    names = ", ".join(sorted(doc.get("providers", {}))) or "none"
    pkgs = ""
    if packages:
        pkgs = (f"- Pi packages loaded this session: {', '.join(packages)}. They live in `{TREE_DST}`, read-only, "
                "installed on the host by `ab profile install pi` from the profile's plugins.toml (with npm install "
                "scripts never run); nothing in a session can add or change one.\n")
    return ("\n## Pi in this sandbox\n\n"
            f"- Model providers configured for this session: {names}. Other providers have no key here; keys go "
            "through `ab keys` on the host, and `/login` cannot store one (`~/.pi/agent/auth.json` is read-only).\n"
            "- `~/.pi/agent/models.json`, `auth.json`, `mcp.json` and this file are read-only and regenerated each "
            "session. `~/.pi/agent/settings.json` is a fresh copy each session: what you change there is not kept, "
            "and prompt files you leave in `~/.pi/agent` (SYSTEM.md and the like) are moved out before the next "
            "session starts.\n"
            "- Pi runs offline (`PI_OFFLINE=1`) and does not trust this repository (`--no-approve`): it installs no "
            "packages, checks no updates, and loads nothing from the repository's `.pi` directory. Its extension, "
            "package and bin directories are read-only and empty.\n"
            "- Pi has no permission prompts: every tool call runs. There is no browser tool.\n" + pkgs)


ADAPTER = PiAdapter(
    name="pi",
    image="localhost/agentbox-pi:latest",
    command=["pi", "--no-approve"],
    model_domains=[],
    env={"NO_UPDATE_NOTIFIER": "1", "PI_OFFLINE": "1", "PI_TELEMETRY": "0"},
    required_tools=["bash", "git", "pi", "node", "rg", "fd"],
    approvals_off_args=[],
    approvals_on_args=[],                 # never used: prepare_session refuses approvals on (P1)
    profile_mounts={"skills": f"{C_AGENT}/skills", "prompts": f"{C_AGENT}/prompts"},
    state_mirrors={C_AGENT: "agent"},     # ~/.pi/agent is a bind of <state>/agent
    inbox_mount=f"{C_AGENT}/inbox",
)
