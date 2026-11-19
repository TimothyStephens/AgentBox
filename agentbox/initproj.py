"""`ab project init <template> <n>`: scaffold a new project whose layout passes grantcheck with no override,
by construction. The safe shape is the only thing this can make:

  * real secrets live in `.env` (0600, gitignored) and are MASKED in sessions; the agent sees the committed
    fake twin `.env.dev` AS `.env`;
  * live data lives in a podman NAMED VOLUME (`<name>_data`), outside the repo tree, so a repo-level `rm` or
    `git clean` cannot take it, and no session is ever granted it;
  * `AGENTS.md` states the ENFORCED reality, not rules the agent must choose to obey.

After writing the tree it runs `ab project add --preset {build|script}` itself (`pods`/`python` are kept as
aliases of those two), so the project is registered and grant-checked the moment it exists. A freshly-init'd
project with no override flags starts a session with zero warnings and zero overrides (tests assert exactly
that). `ab project init` shares its override flags (--preset, --egress, --data-ro, ...) with `ab project add`/`set`
(agentbox/cli.py's `_add_project_setting_args`), so a project can be fully configured at init time too.

The templates never put live data in `./data`. A project that keeps it there anyway can opt in with the
project setting `data_bind = on` (`--data-bind on`, here as on `add`/`set`; decisions.md, 19 Nov 2026, which
replaced the 6 Oct deferred contract of a masked `./data` plus a `--data-backup` target): `./data` is then
visible and WRITABLE to the agent, not a grantcheck finding, and left out of the per-session snapshot. That is
only for an agent the user trusts. The per-session rsync snapshot is NOT a data backup (same disk, only as
fresh as the last session start), so `./data` is the user's to back up; `ab project show` and every session
start say so.
"""
import os
import subprocess

from . import registry
from .errors import AgentboxError
from .session import git_user

TEMPLATES = ("pod", "python")
PRESET = {"pod": "build", "python": "script"}
# The ONLY scaffold files a user template dir may replace: documentation, never .env/.env.dev, the masks,
# the compose/data-volume structure or anything else that keeps a fresh project grantcheck-clean. Any other
# file in the template dir is ignored, so the safe layout always comes from the built-in constants.
OVERRIDABLE = ("AGENTS.md", "README.md")

_AGENTS = """\
# {name}

This project runs in an agentbox sandbox. These are enforced outside your control, not requests:
- .env is masked; you see .env.dev as .env. Do not try to read real secrets — you can't, and the
  values you see are fake. Real runs against live credentials happen outside this sandbox.
- {data_line}
- You may build, run and test the dev stack (compose.dev.yaml) freely; it only affects this project.
- compose.yaml is the LIVE stack: the user deploys it with `ab deploy` from a reviewed commit. Keep it to the
  rules in the session notes ("Deploying this project"); `/run/agentbox/bin/deploy-check` checks it.
- Network egress is restricted to the project's allowlist.
Workflow: develop against dev/fixtures/; the human captures real samples and verifies live on the host.
agentbox injects your real egress mode, data layout and limits at the start of each session — read the
"agentbox sandbox" section it adds for the authoritative, live description of this environment.
"""

_AGENTS_DATA = {
    "pod": "the {name}_data volume holds live data and is unreachable to you. Develop against\n"
           "  dev/fixtures/ and dev/data, never live data or live credentials.",
    "python": "live data and live credentials are unreachable to you. Develop against dev/fixtures/,\n"
              "  never live data.",
}

_GITIGNORE = ".env\ndev/fixtures/private/\n"

_ENV_REAL = """\
# Real secrets. 0600, gitignored, and MASKED in agentbox sessions: the agent never sees these values.
# Fill them in on the host; they are used only for live/deploy runs outside the sandbox.
{body}"""

_ENV_DEV = """\
# Fake dev twin of .env, committed. In agentbox sessions this is what the agent sees AS .env.
# Low-value throwaway values only — never a real secret. Keep the SAME keys as .env.
{body}"""

_ENV_KEYS = {
    "pod": [("APP_SECRET_KEY", "change-me-to-a-real-secret", "dev-not-a-secret"),
            ("DATABASE_URL", "postgres://app:CHANGE_ME@db:5432/app", "postgres://app:devpass@db:5432/app")],
    "python": [("APP_SECRET_KEY", "change-me-to-a-real-secret", "dev-not-a-secret"),
               ("API_TOKEN", "change-me", "dev-token"),
               # Data path comes from config, never hardcoded: dev points at ./dev/data (in the repo,
               # fake data); the real path lives only in the masked .env and is used outside the sandbox.
               ("DATA_DIR", "/var/lib/change-me/data", "./dev/data")],
}

_COMPOSE = """\
# Live stack. Runs on the host via deploy, never inside an agentbox session.
# Live data is a NAMED VOLUME ({name}_data) outside the repo tree, so a repo-level rm or
# `git clean` cannot take it, and no agent session is ever granted it. Config from the real .env.
services:
  web:
    build: .
    env_file: .env
    ports:
      - "8000:8000"
    volumes:
      - data:/data      # podman-compose names it {name}_data (the compose project's prefix)
    restart: always   # rootless podman-restart.service starts only `always` containers at boot
volumes:
  data:
"""

_COMPOSE_DEV = """\
# Dev stack: build and run this freely inside an agentbox session. Its own project name
# ({name}-dev), no published ports (reach it with `ab session preview`), dev data and dev config.
# From a session: podman-compose -p {name}-dev -f compose.dev.yaml up -d --build
name: {name}-dev
services:
  web:
    build: .
    env_file: .env.dev
    volumes:
      - ./dev/data:/data
"""

_CONTAINERFILE = """\
FROM docker.io/library/python:3.12-slim
WORKDIR /app
COPY app/ ./app/
ENV PORT=8000
CMD ["python", "-m", "app.main"]
"""

_APP_POD = '''\
"""Minimal web app skeleton. Replace with the real app; config comes from the environment (.env.dev)."""
import http.server
import os
import signal

PORT = int(os.environ.get("PORT", "8000"))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok\\n")


def _stop(signum, frame):
    # PID 1 in a container gets no default SIGTERM action: without this, `podman stop` waits 10 s and SIGKILLs.
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    with http.server.HTTPServer(("0.0.0.0", PORT), Handler) as server:
        server.serve_forever()
'''

_TEST_POD = '''\
"""A placeholder test. Replace with real ones as the app grows; run them in the dev stack."""
import os


def test_fixtures_present():
    here = os.path.dirname(os.path.dirname(__file__))
    assert os.path.isdir(os.path.join(here, "dev", "fixtures"))
'''

_APP_PY = '''\
"""Minimal library/CLI skeleton. Replace with the real code.

The data path comes from the environment (DATA_DIR in .env/.env.dev), never hardcoded: in a session you
see .env.dev (DATA_DIR=./dev/data, fake data in the repo); the real path lives only in the masked .env and
is used outside the sandbox. There is no ./data in the repo tree.
"""
import os


def data_dir() -> str:
    """Where this app reads and writes its data. Set by DATA_DIR; falls back to the dev corpus."""
    return os.environ.get("DATA_DIR", "./dev/data")


def greet(name: str) -> str:
    return f"hello, {name}"


if __name__ == "__main__":
    print(greet("world"))
    print(f"data dir: {data_dir()}")
'''

_TEST_PY = '''\
import os

from app.main import data_dir, greet


def test_greet():
    assert greet("world") == "hello, world"


def test_data_dir_comes_from_env():
    os.environ["DATA_DIR"] = "/tmp/example-data"
    try:
        assert data_dir() == "/tmp/example-data"
    finally:
        del os.environ["DATA_DIR"]
'''

_README = """\
# {name}

Scaffolded by `ab project init {template}`. It runs as an agentbox session (the agent) and, for the `pod`
template, as a live stack on the host (you, via deploy).

## The cycle

**Capture** — real samples go in `dev/fixtures/` (and private ones in `dev/fixtures/private/`, which is
gitignored). The agent develops against these, never against live data.

**Develop** — start a session (`ab session start <agent> {name}`). Inside it the agent{dev_stack}
tests against `dev/fixtures/`, and commits. It sees `.env.dev` as `.env`; the real `.env` is masked.

**Verify** — you run against live credentials and live data on the host, outside the sandbox.{deploy}

## What the agent cannot see

- The real `.env` (masked; the agent sees the fake `.env.dev`).
- {data_doc}

## Backups

- `git`{upstream_doc} backs up the CODE only.
- `.env` is gitignored and never leaves the host.
- {data_backup}
"""


def _env_block(template, use_dev):
    lines = []
    for key, real, dev in _ENV_KEYS[template]:
        lines.append(f"{key}={dev if use_dev else real}")
    return "\n".join(lines) + "\n"


def _files(template, name):
    data_line = _AGENTS_DATA[template].format(name=name)
    files = {
        ".gitignore": (_GITIGNORE, None),
        ".env": (_ENV_REAL.format(body=_env_block(template, use_dev=False)), 0o600),
        ".env.dev": (_ENV_DEV.format(body=_env_block(template, use_dev=True)), None),
        "AGENTS.md": (_AGENTS.format(name=name, data_line=data_line), None),
        "dev/fixtures/sample.txt": ("one sample row per line; replace with a real captured sample\n", None),
    }
    if template == "pod":
        files.update({
            "compose.yaml": (_COMPOSE.format(name=name), None),
            "compose.dev.yaml": (_COMPOSE_DEV.format(name=name), None),
            "Containerfile": (_CONTAINERFILE, None),
            "app/__init__.py": ("", None),
            "app/main.py": (_APP_POD, None),
            "tests/test_app.py": (_TEST_POD, None),
            "dev/data/.gitkeep": ("", None),
            "README.md": (_README.format(
                name=name, template=template,
                dev_stack=f" brings up the dev stack (`compose.dev.yaml`, its own `{name}-dev` project),",
                deploy=f" Deploy the live stack with the real `.env`; uploads land in the `{name}_data`"
                       " volume, which no session is granted.",
                data_doc=f"Live data in the `{name}_data` podman volume (outside the repo tree).",
                upstream_doc=" (and the upstream, if set)",
                data_backup=f"Live data lives in the `{name}_data` volume. agentbox does not back it up; "
                            "snapshot the volume yourself."), None),
        })
    else:
        files.update({
            "app/__init__.py": ("", None),
            "app/main.py": (_APP_PY, None),
            "tests/test_main.py": (_TEST_PY, None),
            "dev/data/.gitkeep": ("", None),
            "README.md": (_README.format(
                name=name, template=template, dev_stack="",
                deploy="", data_doc="The real data directory (`DATA_DIR` in the masked `.env`); in a session "
                                    "`DATA_DIR` is `./dev/data`.",
                upstream_doc=" (and the upstream, if set)",
                data_backup="Live data lives at the real `DATA_DIR` (outside this repo); agentbox does not "
                            "back it up."), None),
        })
    return files


def _git(dest, *args, **kw):
    return subprocess.run(["git", "-C", dest, *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def _git_identity(settings):
    """The scaffold commit's author: the same lookup sessions use, with a fallback so the commit is made."""
    name, email = git_user(settings)
    return name or "agentbox", email or "agentbox@localhost"


def _template_overrides(settings, template, name):
    """Doc files (only the OVERRIDABLE allowlist) a user may replace from
    `<config_dir>/templates/<template>/`, `{name}`-substituted. Returns {rel: (content, mode)}. Only the
    allowlisted filenames are ever read; a `.env`, `compose.yaml` or anything else in that dir is ignored, so
    the project's structure always comes from the built-in constants (PERSONALIZATION.md §3)."""
    out = {}
    d = os.path.join(settings.config_dir, "templates", template)
    for fn in OVERRIDABLE:
        p = os.path.join(d, fn)
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p) as f:
                out[fn] = (f.read().replace("{name}", name), None)
    return out


def make(settings, a, *, out=print, warn=None):
    """`a` is the parsed `ab project init` Namespace: `.template`/`.name`/`.upstream`, plus the project-setting
    override flags it shares with `ab project add`/`set` (`cli._add_project_setting_args`)."""
    warn = warn or (lambda m: None)
    template, name, upstream = a.template, a.name, a.upstream
    if template not in TEMPLATES:
        raise AgentboxError(f"unknown template {template!r}; choose one of {', '.join(TEMPLATES)}")
    registry.check_name(name)
    reg = registry.load(settings.registry_path)
    if name in reg:
        raise AgentboxError(f"project {name} already exists; pick another name")

    repos_dir = os.path.realpath(os.path.expanduser(settings.repos_dir))
    dest = os.path.join(repos_dir, name)
    if os.path.exists(dest):
        raise AgentboxError(f"{dest} already exists; refusing to scaffold over it")

    # Every setting is checked BEFORE anything is written, so a bad flag leaves no tree behind for a
    # corrected retry to trip over. The template's default preset is resolved first, since the checks
    # (lockdown ceiling included) are relative to it.
    from . import cli, layout
    a.preset = a.preset or PRESET[template]
    cli.precheck_project_settings(settings, a)
    # The name may have state on disk from an earlier project (layout.py); refused here, before the tree
    # is written, with the same --reattach/--fresh answer `ab project add` takes.
    layout.check_registration(settings, name, dest, reattach=getattr(a, "reattach", False),
                              fresh=getattr(a, "fresh", False))

    files = _files(template, name)
    files.update(_template_overrides(settings, template, name))   # only OVERRIDABLE docs; structure stays built-in
    os.makedirs(dest, mode=0o700, exist_ok=True)
    try:
        for rel, (content, mode) in files.items():
            path = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            if mode is not None:
                os.chmod(path, mode)
        os.makedirs(os.path.join(dest, "dev", "fixtures", "private"), exist_ok=True)

        who, email = _git_identity(settings)
        _git(dest, "init", "-q")
        _git(dest, "config", "user.name", who)
        _git(dest, "config", "user.email", email)
        _git(dest, "add", "-A")
        _git(dest, "commit", "-q", "-m", f"ab project init {template}: {name} scaffold")
        if upstream:
            _git(dest, "remote", "add", "origin", upstream)
            try:
                _git(dest, "push", "-u", "origin", "HEAD")
            except subprocess.CalledProcessError as e:
                raise AgentboxError(f"pushed nothing: `git push` to {upstream} failed:\n{e.stdout}")
    except BaseException:
        # Leave no half-made tree behind if anything above fails.
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        raise

    # Register and grant-check the project the moment it exists. `a` already carries the project-setting
    # override flags (ab project init shares their definition with ab project add/set) and its preset; only `root`
    # is init-specific, filled in here.
    a.root = dest
    try:
        cli.cmd_project_add(settings, a)
    except BaseException:
        # A refusal at registration (grantcheck, overlap, ...) would leave a fresh tree that makes the corrected
        # retry fail with "already exists". Removed only if the project did not get registered: once it is in
        # the registry, its root is the project's and stays (audit item 47).
        if name not in registry.load(settings.registry_path):
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
            warn(f"ab: removed the scaffold {dest}, since {name} was not registered")
        raise

    out(f"\nscaffolded {template} project {name} at {dest}")
    out(f"  start:  ab session start <agent> {name}")
    if template == "pod":
        out(f"  data:   live data is the podman volume {name}_data (outside the repo); sessions never see it")
    out(f"  secrets: real values go in {dest}/.env (gitignored, masked); the agent sees .env.dev as .env")
    if upstream:
        out(f"  upstream: pushed to {upstream} — this backs up CODE only, NOT .env"
            + (f" or the {name}_data volume" if template == "pod" else ""))
    else:
        out("  upstream: none set (pass --upstream <url> to add an off-box CODE backup)")
    out("  note: the upstream and `git` back up code only. .env is gitignored; "
        + (f"the {name}_data volume is yours to snapshot." if template == "pod"
           else "there is no live-data volume in this template."))
    return 0
