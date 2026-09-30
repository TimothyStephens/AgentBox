"""Sessions: build, start and tear down everything one agent session needs."""
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field

from . import adapters, backup, cgroup, egress, hardening, mounts, paths, presets, registry, tokens
from .config import HOME
from .errors import AgentboxError
from .podman import Podman, set_log

TOKEN_PATH = "/run/agentbox/token"
PROBE_PATH = "/run/agentbox/probe.sh"
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

    def value(self, key):
        return self.eff[key][0]


def _no_commas(path, what):
    if "," in path or "\n" in path:
        raise AgentboxError(f"{what} {path!r}: paths with commas or newlines cannot be mounted safely")


def protected_extra(settings):
    return [settings.config_dir, settings.data_dir, settings.run_dir]


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
                   build_memory=check_build_memory(settings, name, p.get("build_memory", settings.build_memory)))


SWAP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def check_swap(name, swap):
    """swap maps a file name to its dev stand-in in the same directory: plain file names only."""
    for k, v in dict(swap).items():
        for x in (k, v):
            if not isinstance(x, str) or not SWAP_NAME.match(x) or x in (".", ".."):
                raise AgentboxError(f"project {name}: swap entry {k!r} = {v!r}: use plain file names "
                                    "(no '/', no '..')")
    return dict(swap)


def committed_secrets(root, swap, timeout=30):
    """Paths in the repo's history (all refs) whose names the masks cover: readable through git, masked or not."""
    import subprocess
    if not os.path.isdir(os.path.join(root, ".git")):
        return []
    try:
        p = subprocess.run(["git", "--no-pager", "-C", root, "-c", "core.fsmonitor=false", "log", "--all",
                            "--format=", "--name-only", "--diff-filter=AM"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = sorted({line for line in p.stdout.splitlines() if line})
    return [n for n in names if mounts._masked(os.path.basename(n), swap)
            or any(part in mounts.MASK_DIRS for part in n.split("/")[:-1])]


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
    import subprocess
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


def sandbox_notes(project, egress):
    lines = [
        "# agentbox sandbox",
        "",
        "You are running inside an agentbox sandbox (a hardened rootless podman container). Facts:",
        f"- Project `{project.name}` is mounted read-write at `{project.root}` (its real host path).",
    ]
    for d in project.data_ro:
        lines.append(f"- Read-only data: `{d}`")
    for d in project.data_rw:
        lines.append(f"- Read-write data: `{d}`")
    lines += [
        "- Nothing else on the host is visible. The root filesystem is read-only; `/tmp` and your home "
        "directory are temporary and vanish when the session ends.",
        "- `.git/hooks` and `.git/config` are read-only. Commit as usual.",
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
    lines += [
        "- No podman or docker socket is available in this session.",
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
        return self.settings.state_dir(self.project.name, self.adapter.name)

    @property
    def token_file(self):
        return self.settings.token_file(self.adapter.name) if self.adapter.token_env else None

    # ---- host-side preparation ------------------------------------------------------
    def prepare(self, *, do_backup=True):
        cgroup.check(self.settings, self.warn)
        if self.token_file:
            tokens.check_token_file(self.token_file)
        self.reserve_name()
        self._write_meta()
        p = self.project
        self.plan = mounts.plan(root=p.root, data_ro=p.data_ro, data_rw=p.data_rw,
                                compose=p.value("compose"), secrets=p.value("secrets"),
                                mask_skip=p.mask_skip, swap=p.swap)
        for m in self.plan.mounts:
            _no_commas(m.dst, "mount")
        for n in self.plan.notes:
            self.warn(f"agentbox: {n}")
        if p.value("secrets") != "exposed":
            leaked = committed_secrets(p.root, p.swap)
            if leaked:
                self.warn(f"agentbox: {len(leaked)} masked file name(s) are in {p.name}'s git history and readable "
                          f"there despite the masks: {', '.join(leaked[:10])}" + (" ..." if len(leaked) > 10 else ""))
        if do_backup:
            grants = [p.root] + p.data_rw
            skip = lambda g: mounts._rel_skips(p.root, g, p.mask_skip)
            backup.snapshot(self.settings.backups_dir, p.name, self.name, grants, skip_rel_for=skip,
                            warn_mb=int(self.settings.backup_warn_mb), warn=self.warn)
            backup.retain(self.settings.backups_dir, p.name, keep=int(self.settings.backup_keep),
                          daily_days=int(self.settings.backup_daily_days))
        mounts.prepare(self.plan)
        self.adapter.seed_state(self.settings, self.state_dir, p.root, sandbox_notes(p, self.egress))

    def _write_meta(self):
        meta = {"session": self.name, "agent": self.adapter.name, "project": self.project.name,
                "pid": os.getpid(), "pid_start": _start_time(os.getpid()), "started": time.time(), "preset_egress": self.project.value("egress")}
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
        return out + list(extra)

    def network_args(self):
        return ["--network=none"]

    def run_args(self, command, *, interactive=False, tty=False, extra_mounts=()):
        """The complete argv after `podman`, audited."""
        s, a, p = self.settings, self.adapter, self.project
        args = ["run", "--rm", f"--name={self.name}", f"--hostname={self.name}"]
        opts = [f"--label={k}={v}" for k, v in labels_for(s, p.name, self.name, "agent").items()]
        if interactive:
            opts.append("--interactive")
        if tty:
            opts.append("--tty")
        opts += hardening.fixed_flags(s, cgroup_parent=s.cgroup_parent)
        opts.append(f"--tmpfs={HOME}:rw,nosuid,nodev,mode=0700,U,notmpcopyup")
        env = {"HOME": HOME, **git_identity(s, a.name), **a.env, **self.env}
        if tty and os.environ.get("TERM"):
            env["TERM"] = os.environ["TERM"]
        opts += [f"--env={k}={v}" for k, v in env.items()]
        opts.append(f"--workdir={p.root}")
        opts += self.network_args()
        opts += [m.podman_arg() for m in self.plan.mounts]
        if a.state_mount:
            opts.append(f"--mount=type=bind,src={self.state_dir},dst={a.state_mount}")
        if self.token_file:
            opts.append(f"--mount=type=bind,src={os.path.realpath(self.token_file)},dst={TOKEN_PATH},ro")
        extra_srcs = []
        for src, dst in extra_mounts:
            opts.append(f"--mount=type=bind,src={src},dst={dst},ro")
            extra_srcs.append(src)
        opts.append("--entrypoint=/bin/sh")
        hardening.audit(opts + [a.image], allowed_mount_srcs=self.allowed_mount_srcs(extra_srcs),
                        allowed_mount_dsts=[p.root] + p.data_ro + p.data_rw + ["/run/agentbox"],
                        networks=self.networks, tmpfs_dirs=("/tmp", HOME))
        return args + opts + [a.image, "-c", self.wrapper(), "agentbox"] + list(command)

    def wrapper(self):
        """Exports the token from its read-only mount just before exec; it is never in podman's config."""
        if not self.token_file:
            return 'exec "$@"'
        var = self.adapter.token_env
        return (f'if [ -r {TOKEN_PATH} ]; then IFS= read -r {var} < {TOKEN_PATH}; export {var}; fi; '
                f'exec "$@"')

    def agent_command(self, shell=False, extra=()):
        if shell:
            return ["bash"] + list(extra)
        return self.adapter.agent_argv(self.project.value("approvals")) + list(extra)

    # ---- lifecycle ----------------------------------------------------------------------
    def start_infra(self):
        """Networks and sidecars (step 2). Nothing in step 1."""

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

    def add_network_cases(self, builder):
        from .selftest import runner
        runner.network_none_cases(builder)

    def teardown(self):
        if not self.name:
            return
        teardown(self.settings, self.podman, self.name)


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
        return egress.squid_conf(self.egress, model_domains=self.adapter.model_domains, model_host=s.model_host,
                                 model_port=s.model_port, extra_domains=self.project.extra_domains,
                                 host_addrs=addrs, dns=dns)

    def net_create_args(self, name, internal):
        # IPv4 only, by decision (docs/decisions.md): no IPv6 destination is routable from the session
        args = ["network", "create", "--ipv6=false", "--opt", "isolate=strict"]
        if internal:
            args.append("--internal")
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
                             lan_targets=list(s.lan_targets), model_domains=self.adapter.model_domains,
                             extra_domains=self.project.extra_domains)

    def teardown(self):
        if not self.name:
            return
        # Keep the proxy's access log (URLs and verdicts only) for review, then tear down.
        try:
            logs = self.podman.run(["logs", self.proxy_name], check=False)
            if logs.returncode == 0 and logs.stdout:
                path = os.path.join(self.settings.data_dir, "logs", f"{self.name}-proxy.log")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a") as f:
                    f.write(logs.stdout)
        except AgentboxError:
            pass
        super().teardown()


def make_session(settings, agent, project, **kw):
    return NetSession(settings, agent, project, **kw)


def teardown(settings, podman, session):
    sel = {"agentbox.session": session, **settings.labels}
    for c in podman.ids("container", sel):
        podman.run(["rm", "-f", "-t", "5", c], check=False)
    for n in podman.ids("network", sel):
        podman.run(["network", "rm", "-f", n], check=False)
    run_dir = settings.session_run_dir(session)
    if os.path.isdir(run_dir):
        for fn in os.listdir(run_dir):
            try:
                os.unlink(os.path.join(run_dir, fn))
            except IsADirectoryError:
                import shutil
                shutil.rmtree(os.path.join(run_dir, fn), ignore_errors=True)
        os.rmdir(run_dir)


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


def gc(settings, podman, warn=lambda m: None):
    """Tear down sessions whose launcher is gone. A session whose agent container is still running is left
    alone even if its metadata is missing or unreadable (ab kill removes it explicitly); an orphaned proxy
    or network with no running agent is removed."""
    removed = []
    for s, info in sessions(settings, podman).items():
        agent_running = [c["name"] for c in info["containers"]
                         if c.get("state") == "running" and c.get("role") == "agent"]
        if not info["alive"] and agent_running:
            warn(f"agentbox: {s} has no live launcher but its agent is running; left alone (use `ab kill {s}`)")
            continue
        if not info["alive"]:
            teardown(settings, podman, s)
            removed.append(s)
    return removed
