"""Sessions: build, start and tear down everything one agent session needs."""
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field

from . import adapters, backup, cgroup, egress, hardening, mounts, paths, presets, registry, tokens
from . import keys as keysmod
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
    history_secrets_accepted: list = field(default_factory=list)

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
                   history_secrets_accepted=list(p.get("history_secrets_accepted", [])))


SWAP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


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
    import subprocess
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
        self.setup = adapters.SessionSetup()
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

    def _prepare_reserved(self, do_backup):
        p = self.project
        self._write_meta()
        self.plan = mounts.plan(root=p.root, data_ro=p.data_ro, data_rw=p.data_rw,
                                compose=p.value("compose"), secrets=p.value("secrets"),
                                mask_skip=p.mask_skip, swap=p.swap)
        for m in self.plan.mounts:
            _no_commas(m.dst, "mount")
        for n in self.plan.notes:
            self.warn(f"agentbox: {n}")
        if do_backup:
            grants = [p.root] + p.data_rw
            skip = lambda g: mounts._rel_skips(p.root, g, p.mask_skip)
            backup.snapshot(self.settings.backups_dir, p.name, self.name, grants, skip_rel_for=skip,
                            warn_mb=int(self.settings.backup_warn_mb), warn=self.warn)
            backup.retain(self.settings.backups_dir, p.name, keep=int(self.settings.backup_keep),
                          daily_days=int(self.settings.backup_daily_days))
        mounts.prepare(self.plan)
        notes = sandbox_notes(p, self.egress)
        self.adapter.seed_state(self.settings, self.state_dir, p.root, notes)
        self.setup = self.adapter.prepare_session(self, notes)
        self._check_setup()

    def _check_setup(self):
        """Adapter mounts: destinations under the adapter's own root, never on top of a grant or /run/agentbox;
        key names that are safe in a path and a shell variable."""
        st = self.setup
        for dst in st.tmpfs:
            _no_commas(dst, "adapter tmpfs")
            if not st.mount_root or not _inside(dst, st.mount_root):
                raise AgentboxError(f"adapter tmpfs {dst} is not under the adapter's root {st.mount_root!r}")
        for src, dst, _ro in st.mounts:
            _no_commas(src, "adapter mount")
            _no_commas(dst, "adapter mount")
            if not st.mount_root or not _inside(dst, st.mount_root) or dst == st.mount_root:
                raise AgentboxError(f"adapter mount {dst} is not under the adapter's root {st.mount_root!r}")
        for provider, path in st.keys.items():
            keysmod.env_name(provider)
            _no_commas(os.path.realpath(path), "key file")
        for k, v in st.env.items():
            if not re.match(r"^[A-Z_][A-Z0-9_]*$", k) or "\n" in str(v):
                raise AgentboxError(f"adapter env {k!r} is not a plain variable")

    @property
    def model_domains(self):
        return sorted(set(self.adapter.model_domains) | set(self.setup.model_domains))

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
        out += [os.path.realpath(src) for src, _dst, _ro in self.setup.mounts]
        out += [os.path.realpath(k) for k in self.setup.keys.values()]
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
        env = {"HOME": HOME, **git_identity(s, a.name), **a.env, **self.setup.env, **self.env}
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
        opts += [mounts.Mount("tmpfs", d).podman_arg() for d in self.setup.tmpfs]
        for src, dst, ro in self.setup.mounts:
            opts.append(f"--mount=type=bind,src={os.path.realpath(src)},dst={dst}" + (",ro" if ro else ""))
        for provider, path in sorted(self.setup.keys.items()):
            opts.append(f"--mount=type=bind,src={os.path.realpath(path)},"
                        f"dst={keysmod.CONTAINER_DIR}/{provider},ro")
        extra_srcs = []
        for src, dst in extra_mounts:
            opts.append(f"--mount=type=bind,src={src},dst={dst},ro")
            extra_srcs.append(src)
        opts.append("--entrypoint=/bin/sh")
        hardening.audit(opts + [a.image], allowed_mount_srcs=self.allowed_mount_srcs(extra_srcs),
                        allowed_mount_dsts=[p.root] + p.data_ro + p.data_rw + ["/run/agentbox"]
                        + ([self.setup.mount_root] if self.setup.mount_root else []),
                        networks=self.networks, tmpfs_dirs=("/tmp", HOME))
        return args + opts + [a.image, "-c", self.wrapper(), "agentbox"] + list(command)

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
        return egress.squid_conf(self.egress, model_domains=self.model_domains, model_host=s.model_host,
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
        for var in self.adapter.proxy_url_env:
            self.env[var] = self.env["HTTPS_PROXY"]

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
