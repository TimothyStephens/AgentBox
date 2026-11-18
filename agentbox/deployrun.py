"""`ab deploy`, the run (step 3; docs/design.md "Deploy: the run"): the live stack from the deploy copy.

A deploy is built like a session's dev tier, out of the same parts, with the deploy-mode policy:

    runner (agentbox-base, --network=none) --/run/agentbox/podman.sock--> gate (mode=deploy)
        --> sockguard (deploy profile: owner isolation on agentbox.deploy=<project>) --> podman service

  * the podman service is `sockguard.start_service` on <run>/services/<name>, NOT in agentbox.slice (D5): what
    it builds and starts is the live stack, which must not compete with sessions under their cap;
  * the gate and sockguard are the session's, with the deploy config (gate cfg `mode="deploy"` and the keys
    gate._check_deploy_cfg names; `sockguard.deploy_config`);
  * builds run their RUN steps in a holder's network namespace, as in a session; the holder sits on a
    per-deploy bridge network with ordinary egress (D3), since a live stack's build fetches packages;
  * the runner sees only the deploy copy (read-only, at the same host path, so the paths podman-compose
    resolves are the ones the gate checks) and the gate's socket, and runs
    `podman-compose -p <compose_project> -f <compose file> up -d --build --remove-orphans`.

Everything agentbox starts for the run is named `<prefix>deploy-<project>[-role]` and carries the session labels
with that name (agentbox.session=<name>), and the run writes a session.json naming this process: while it runs
`ab session gc` sees a live launcher and leaves it alone; after a crash gc tears it down like any husk. The
stack itself carries only the deploy labels (no agentbox.session, no agentbox.project), so no teardown, gc or
session check ever reaches it. Teardown is `lifecycle.teardown` on every path (return, error, Ctrl-C, SIGTERM,
SIGHUP), with those signals ignored while it runs; it keeps the gate's and sockguard's logs.

The deploy copy is kept after the run (the stack's binds and podman's build contexts point into it): the newest
KEEP_STAGES and the last successful deploy's are kept, older ones removed.
"""
import contextlib
import fcntl
import json
import os
import pwd
import re
import signal
import subprocess
import time

from . import deploy, hardening, lifecycle, paths, projectkeys, registry, sockguard
from .config import DEPLOY_LOCK, HOME
from .errors import AgentboxError
from .fsutil import rmtree, write_private
from .gate_mounts import DEPLOY_TIMEZONE
from .infra import holder_run_argv, net_create_argv, sockguard_run_argv
from .liveness import _start_time
from .podman import Podman
from .progress import Step
from .project import protected_extra
from .state import service_tmp

RUNNER_IMAGE = "localhost/agentbox-base:latest"     # Makefile `base-image`: podman-remote and podman-compose
RUNNER_TIMEOUT = 3600                               # seconds for the whole `up --build`
KEEP_STAGES = 2
SHIELDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def deploy_name(settings, project):
    return f"{settings.name_prefix}deploy-{project}"


@contextlib.contextmanager
def lock(settings, project):
    """One `ab deploy` per project at a time: an exclusive, non-blocking flock on <project dir>/deploy.lock,
    released by the kernel if this process dies."""
    d = settings.project_dir(project)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(d, DEPLOY_LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentboxError(f"another `ab deploy {project}` is running; one deploy per project at a time")
        yield
    finally:
        os.close(fd)


def checked_binds(settings, project):
    """The project's deploy_binds, checked now (they are host directories that may have changed since they were
    stored): real paths, through paths.check_deploy_binds."""
    reg = registry.load(settings.registry_path)
    return paths.check_deploy_binds(project.name, project.deploy_binds, {"root": project.root}, reg, home=HOME,
                                    extra_protected=protected_extra(settings))


def foreign_objects(settings, podman, project):
    """Refusals before anything starts (decisions.md, step 2 decisions): containers of the stack's compose
    project that a deploy did not make (a stack brought up by hand), and named volumes `<compose_project>_*`
    without the deploy labels, which the gate would refuse mid-run. agentbox never removes or adopts what it did
    not create; it prints the command for the user."""
    cp, problems = project.compose_project, []
    mine = {"agentbox.role": "deploy", "agentbox.deploy": project.name}
    theirs = [c for c in podman.containers_json({sockguard.COMPOSE_LABEL: cp})
              if any((c.get("Labels") or {}).get(k) != v for k, v in mine.items())]
    if theirs:
        names = sorted((c.get("Names") or [(c.get("Id") or "?")[:12]])[0] for c in theirs)
        problems.append(f"compose project {cp} has containers no `ab deploy` made ({', '.join(names)}). Stop them "
                        f"yourself first, from the directory they were started in:  podman-compose -p {cp} down  "
                        "(this keeps their volumes; agentbox never removes what it did not create)")
    vols = [v for v in unlabelled_volumes(podman, project) if v not in project.deploy_adopted]
    if vols:
        problems.append(f"volumes {', '.join(vols)} belong to compose project {cp} but were not made by "
                        f"`ab deploy`. If they are this stack's data, adopt them by name once:  "
                        f"ab deploy {project.name} --adopt-volumes")
    return problems


# Parses the compose file in the base image (PyYAML comes with podman-compose there); agentbox stays stdlib-only.
YAML_TO_JSON = "import json, sys, yaml; json.dump(yaml.safe_load(sys.stdin), sys.stdout, default=str)"


def contract_report(settings, podman, project, root, commit, lan_addr, ports, binds):
    """The contract (deploycheck.py) on the compose file AT THE COMMIT, for `ab deploy` before the review.
    Returns (Report or None, note): None with a note when the check could not run (no compose file, no base
    image, podman failing); the gate still enforces every rule at deploy time."""
    from . import deploycheck, scan
    rel = next((n for n in deploy.compose_names(project.deploy_compose)
                if scan.git(root, "cat-file", "-e", f"{commit}:{n}").returncode == 0), None)
    if rel is None:
        return None, "no compose file in the commit"
    text = scan.git(root, "show", f"{commit}:{rel}")
    tree = scan.git(root, "ls-tree", "-r", "-z", "--name-only", commit)
    if text.returncode != 0 or tree.returncode != 0:
        return None, f"git could not read {rel} at {commit[:12]}"
    name = f"{deploy_name(settings, project.name)}-check-{os.getpid()}"
    labels = {**settings.labels, "agentbox.project": project.name, "agentbox.role": "deploy-check"}
    opts = [f"--label={k}={v}" for k, v in labels.items()]
    opts += hardening.fixed_flags(settings, cgroup_parent="")
    opts += ["--network=none", "--interactive", "--entrypoint=python3"]
    hardening.audit(opts + [RUNNER_IMAGE], allowed_mount_srcs=[], allowed_mount_dsts=[], networks=["none"])
    try:
        p = podman.run(["run", "--rm", f"--name={name}"] + opts + [RUNNER_IMAGE, "-c", YAML_TO_JSON],
                       check=False, input=text.stdout, timeout=120)
    except AgentboxError as e:
        return None, f"the compose file could not be parsed ({e})"
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return None, f"the compose file could not be parsed ({err[-1] if err else f'exit {p.returncode}'})"
    try:
        doc = json.loads(p.stdout or "null")
    except ValueError as e:
        return None, f"the compose file could not be parsed ({e})"
    ctx = {"compose": rel, "lan_addr": lan_addr or project.lan_addr, "ports": list(ports), "binds": list(binds),
           "external": list(project.deploy_external), "adopted": list(project.deploy_adopted),
           "compose_project": project.compose_project}
    return deploycheck.check(doc, ctx, [t for t in tree.stdout.split("\0") if t]), ""


def stack_objects(podman, project):
    """What `ab deploy` made for this project and is still there: containers, networks and volumes carrying
    both deploy labels (the gate stamps them on everything a deploy creates). Read-only."""
    mine = {"agentbox.role": "deploy", "agentbox.deploy": project.name}
    names = lambda rows, key: sorted(str((r.get(key) or [r.get("Id")])[0] if key == "Names" else r.get(key))
                                     for r in rows)
    return {"containers": names(podman.containers_json(mine), "Names"),
            "networks": names(podman.networks_json(mine), "name"),
            "volumes": names(podman.volumes_json(mine), "Name")}


def down(settings, podman, project, objs, *, volumes, out=print):
    """Stop and remove the live stack: containers (10 s to stop), then its networks, then -- only with
    `volumes` -- its own labelled volumes. Adopted and external volumes are never removed. Records a
    `result: down` line (not a deploy: last_deployed is unchanged). Returns the names that could not go."""
    failed = []

    def rm(argv, name):
        if podman.run(argv + [name], check=False, timeout=60).returncode != 0:
            failed.append(name)
    for c in objs["containers"]:
        podman.run(["stop", "-t", "10", c], check=False, timeout=60)
        rm(["rm", "-f"], c)
    for n in objs["networks"]:
        rm(["network", "rm"], n)
    removed_vols = []
    if volumes:
        for v in objs["volumes"]:
            rm(["volume", "rm"], v)
            if v not in failed:
                removed_vols.append(v)
    deploy.record(settings, project.name, {
        "action": "down", "result": "down" if not failed else "failed", "containers": objs["containers"],
        "networks": objs["networks"], "volumes_removed": removed_vols, "error": ", ".join(failed)})
    return failed


def unlabelled_volumes(podman, project):
    """Names of `<compose_project>_*` volumes without the deploy labels (a stack run by hand)."""
    cp = project.compose_project
    mine = {"agentbox.role": "deploy", "agentbox.deploy": project.name}
    return sorted(str(v.get("Name")) for v in podman.volumes_json() if str(v.get("Name") or "").startswith(f"{cp}_")
                  and any((v.get("Labels") or {}).get(k) != val for k, val in mine.items()))


def store_adopted(settings, project, names):
    reg = registry.load(settings.registry_path)
    entry = dict(reg[project])
    entry["deploy_adopted"] = sorted(set(entry.get("deploy_adopted") or []) | set(names))
    reg[project] = projectkeys.validate_entry(project, entry, settings=settings)
    registry.save(settings.registry_path, reg)


def linger_warnings(user=None, run=subprocess.run):
    """Read-only checks that a deployed stack comes back after a logout and a reboot: lingering on, and
    podman-restart.service enabled. Never changes either."""
    user = user or pwd.getpwuid(os.getuid()).pw_name
    out = []

    def ask(argv):
        try:
            p = run(argv, capture_output=True, text=True, timeout=10)
            return (p.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            return None
    linger = ask(["loginctl", "show-user", user, "-p", "Linger"])
    if linger != "Linger=yes":
        out.append(f"lingering is off for {user} ({linger or 'loginctl gave no answer'}): the live stack stops "
                   f"when you log out. `loginctl enable-linger {user}` turns it on (agentbox does not)")
    enabled = ask(["systemctl", "--user", "is-enabled", "podman-restart.service"])
    if enabled != "enabled":
        out.append(f"podman-restart.service is not enabled ({enabled or 'no answer'}): the live stack does not "
                   "come back after a reboot. `systemctl --user enable podman-restart.service` enables it "
                   "(agentbox does not)")
    return out


class Deploy:
    def __init__(self, settings, project, *, commit, lan_addr, ports, binds, podman=None, warn=print, out=print):
        self.s, self.p = settings, project
        self.commit, self.lan_addr, self.ports, self.binds = commit, lan_addr, sorted(set(ports)), list(binds)
        self.podman = podman or Podman(settings.podman)
        self.warn, self.out = warn, out
        self.name = deploy_name(settings, project.name)
        self.stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{commit[:12]}"
        self.stage = None
        self.compose = None
        self.started = None

    # ---- names and paths ----
    @property
    def run_dir(self):
        return self.s.session_run_dir(self.name)

    @property
    def service_dir(self):
        return self.s.service_dir(self.name)

    @property
    def gate_socket(self):
        return os.path.join(self.run_dir, "gate", "podman.sock")

    @property
    def sockguard_socket(self):
        return os.path.join(self.run_dir, "sgdev", "sockguard.sock")

    @property
    def sockguard_name(self):
        return f"{self.name}-sockguard-dev"      # lifecycle.SIDECAR_LOGS keeps its audit log at teardown

    @property
    def holder_name(self):
        return f"{self.name}-holder"

    @property
    def build_net(self):
        return f"{self.name}-build"

    @property
    def runner_name(self):
        return f"{self.name}-runner"

    def labels(self, role):
        out = dict(self.s.labels)
        out.update({"agentbox.session": self.name, "agentbox.project": self.p.name,
                    "agentbox.owner": f"{self.p.name}/{self.name}", "agentbox.role": role})
        return out

    def stack_labels(self):
        """What the gate stamps on the live stack: the deploy label it scopes on, and this run. Never
        agentbox.session or agentbox.project, so no session teardown or liveness check counts the stack."""
        return {**self.s.labels, "agentbox.deploy": self.p.name, "agentbox.deploy-run": self.stamp}

    @property
    def bind_roots(self):
        return [self.stage] + self.binds

    # ---- the deploy copy ----
    def prepare_stage(self):
        base = self.s.deploys_dir(self.p.name)
        os.makedirs(base, mode=0o700, exist_ok=True)
        dst, n = os.path.join(base, self.stamp), 1
        while os.path.lexists(dst):
            n += 1
            dst = os.path.join(base, f"{self.stamp}.{n}")
        self.stage = deploy.stage(self.p.root, self.commit, dst, extra_files=(".env", *self.p.deploy_files))
        self.compose = deploy.compose_file(self.stage, self.p.deploy_compose)
        return self.stage

    def prune_stages(self):
        """Keep the newest KEEP_STAGES copies and the last successful deploy's; remove the rest."""
        base = self.s.deploys_dir(self.p.name)
        keep = set(sorted(os.listdir(base))[-KEEP_STAGES:]) if os.path.isdir(base) else set()
        ok = [r for r in deploy.records(self.s, self.p.name) if r.get("result") == "ok" and r.get("stage")]
        if ok:
            keep.add(os.path.basename(ok[-1]["stage"]))
        for n in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            if n not in keep:
                rmtree(os.path.join(base, n), ignore_errors=True)

    # ---- the parts ----
    def sockguard_config(self):
        return sockguard.deploy_config(self.p.name, bind_roots=self.bind_roots + list(DEPLOY_TIMEZONE))

    def gate_config(self, service):
        s, p = self.s, self.p
        return {
            "mode": "deploy", "listen": self.gate_socket, "upstream": self.sockguard_socket, "raw": service,
            "session": self.name, "labels": self.stack_labels(), "compose_project": p.compose_project,
            "lan_addr": self.lan_addr, "ports": self.ports, "stage": self.stage, "deploy_binds": self.binds,
            # adopted volumes go the external way: used by exact name, never created or removed by the gate
            "external_ok": sorted(set(p.deploy_external) | set(p.deploy_adopted)),
            "memory": 1 << 30, "pids": 1024, "memory_max": 4 << 30, "pids_max": 4096,
            "build_memory": int(p.build_memory), "slice": "", "cgroup_parent": "", "proxy_env": {},
            "bind_roots": [], "holder": self.holder_name, "registries": list(sockguard.REGISTRIES),
            "images": [], "spool": os.path.join(service_tmp(s, self.name), "gate"),
            "built_record": s.deploy_built_record(p.name), "log": os.path.join(self.service_dir, "gate.log"),
        }

    def runner_argv(self):
        """The complete argv after `podman`, audited: agentbox's hardening, no network at all, the copy read-only
        at its own path, the gate's socket, no cgroup parent (D5)."""
        opts = [f"--label={k}={v}" for k, v in self.labels("deploy-runner").items()]
        opts += hardening.fixed_flags(self.s, cgroup_parent="")
        url = f"unix://{sockguard.AGENT_SOCKET}"
        opts += ["--network=none", f"--env=CONTAINER_HOST={url}", f"--env=DOCKER_HOST={url}", "--env=HOME=/tmp",
                 f"--workdir={self.stage}", f"--mount=type=bind,src={self.stage},dst={self.stage},ro",
                 f"--mount=type=bind,src={self.gate_socket},dst={sockguard.AGENT_SOCKET}",
                 "--entrypoint=podman-compose"]
        hardening.audit(opts + [RUNNER_IMAGE], allowed_mount_srcs=[self.stage, self.gate_socket],
                        allowed_mount_dsts=[self.stage, os.path.dirname(sockguard.AGENT_SOCKET)],
                        networks=["none"], allowed_sockets=[self.gate_socket])
        return (["run", "--rm", f"--name={self.runner_name}", f"--hostname={self.runner_name}"] + opts
                + [RUNNER_IMAGE, "-p", self.p.compose_project, "-f", self.compose, "up", "-d", "--build",
                   "--remove-orphans"])

    def write_meta(self):
        """session.json, so `ab session gc` sees this run's launcher alive, and reaps what a crash left."""
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        self.started = time.time()
        write_private(os.path.join(self.run_dir, "session.json"), json.dumps({
            "session": self.name, "agent": "deploy", "project": self.p.name, "kind": "deploy",
            "pid": os.getpid(), "pid_start": _start_time(os.getpid()), "started": self.started}))

    def leftovers(self):
        if os.path.isdir(self.run_dir) or os.path.isdir(self.service_dir):
            return True
        return bool(self.podman.ids("container", {"agentbox.session": self.name, **self.s.labels}))

    def start_sockguard(self, service):
        os.makedirs(os.path.dirname(self.sockguard_socket), mode=0o700, exist_ok=True)
        conf = os.path.join(self.run_dir, "sockguard-dev.yaml")
        write_private(conf, self.sockguard_config())
        self.podman.run(sockguard_run_argv(self.s, self.labels("sockguard-dev"), self.sockguard_name,
                                           service_sock=service, socket_path=self.sockguard_socket,
                                           conf_path=conf, cgroup_parent=""))

        def running():
            st = self.podman.run(["inspect", "-f", "{{.State.Status}}", self.sockguard_name], check=False)
            return (st.stdout or "").strip() not in ("exited", "stopped", "dead")
        try:
            sockguard.wait_socket(self.sockguard_socket, float(self.s.ready_timeout), "sockguard", alive=running)
        except AgentboxError:
            logs = self.podman.run(["logs", self.sockguard_name], check=False)
            raise AgentboxError("the deploy's sockguard did not start: " + " ".join(
                ((logs.stderr or "") + (logs.stdout or "")).strip().splitlines()[-3:]))

    def start_infra(self):
        s = self.s
        timeout = float(s.ready_timeout)
        service, _proc = sockguard.start_service(s.podman, self.service_dir, timeout, slice_name="",
                                                 tmpdir=service_tmp(s, self.name))
        self.start_sockguard(service)
        self.podman.run(net_create_argv(s, self.labels("network"), self.build_net, internal=False))
        self.podman.run(holder_run_argv(s, self.labels("holder"), self.holder_name, network=self.build_net,
                                        cgroup_parent=""))
        cfg = os.path.join(self.service_dir, "gate.json")
        write_private(cfg, json.dumps(self.gate_config(service), indent=2))
        os.makedirs(os.path.dirname(self.gate_socket), mode=0o700, exist_ok=True)
        sockguard.start_gate(cfg, self.service_dir, self.gate_socket, timeout, slice_name="")

    def teardown(self, reason):
        old = {sig: signal.getsignal(sig) for sig in SHIELDED}
        for sig in SHIELDED:
            signal.signal(sig, signal.SIG_IGN)
        try:
            lifecycle.teardown(self.s, self.podman, self.name, warn=self.warn, exit_reason=reason)
        finally:
            for sig, h in old.items():
                signal.signal(sig, h)

    # ---- the run ----
    def run(self):
        """Stage, start, run podman-compose, tear down, record. Returns "ok" or "failed"; an interrupt is
        recorded as a failure and re-raised."""
        if self.leftovers():
            self.warn(f"ab deploy: removing what an earlier deploy of {self.p.name} left behind ({self.name})")
            lifecycle.teardown(self.s, self.podman, self.name, warn=self.warn, exit_reason="gc")
        self.prepare_stage()
        old = {sig: signal.getsignal(sig) for sig in SHIELDED}

        def stop(signum, frame):
            raise KeyboardInterrupt
        result, error, reason = "failed", "", "error"
        try:
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGHUP, stop)
            self.write_meta()
            with Step("starting the deploy's podman service, sockguard and gate", self.warn):
                self.start_infra()
            self.out(f"ab deploy: podman-compose -p {self.p.compose_project} up -d --build --remove-orphans "
                     f"({self.stage})")
            rc = self.podman.run(self.runner_argv(), check=False, capture=False, timeout=RUNNER_TIMEOUT).returncode
            if rc == 0:
                result, reason = "ok", "agent_exit"
            else:
                error = f"podman-compose exited {rc}"
        except KeyboardInterrupt:
            error, reason = "interrupted", "signal"
            raise
        except Exception as e:
            error = str(e) if isinstance(e, AgentboxError) else repr(e)
            raise
        finally:
            try:
                self.teardown(reason)
            finally:
                for sig, h in old.items():
                    signal.signal(sig, h)
                deploy.record(self.s, self.p.name, {
                    "commit": self.commit, "compose_project": self.p.compose_project, "lan": self.lan_addr,
                    "ports": self.ports, "result": result, "error": error, "stage": self.stage,
                    "images": self.images()})
                self.prune_stages()
        return result

    def stack(self):
        try:
            return self.podman.containers_json({"agentbox.deploy": self.p.name, "agentbox.role": "deploy"})
        except AgentboxError as e:
            self.warn(f"ab deploy: could not list the live stack: {e}")
            return []

    def images(self):
        return sorted({c.get("ImageID") or c.get("Image") or "" for c in self.stack()} - {""})

    def report(self):
        rows = self.stack()
        if rows:
            self.out(f"running for {self.p.name} (label agentbox.deploy={self.p.name}):")
            for c in rows:
                name = (c.get("Names") or [(c.get("Id") or "?")[:12]])[0]
                ports = ", ".join(f"{m.get('host_ip')}:{m.get('host_port')}->{m.get('container_port')}"
                                  for m in c.get("Ports") or [] if isinstance(m, dict))
                self.out(f"  {name:<32} {c.get('State') or '?':<10} {ports}")
        else:
            self.out(f"nothing of {self.p.name}'s live stack is listed (podman ps --filter "
                     f"label=agentbox.deploy={self.p.name})")
        for w in linger_warnings():
            self.warn(f"ab deploy: warning: {w}")


def store_lan(settings, project, addr, port):
    """After a successful deploy with --lan: the address, and the port added to deploy_ports."""
    reg = registry.load(settings.registry_path)
    entry = dict(reg[project])
    entry["lan_addr"] = addr
    entry["deploy_ports"] = sorted(set(entry.get("deploy_ports") or []) | {int(port)})
    reg[project] = projectkeys.validate_entry(project, entry, settings=settings)
    registry.save(settings.registry_path, reg)


def check_lan_addr(addr, host_addrs):
    """A stored lan_addr must still be an address of this host."""
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", addr or ""):
        raise AgentboxError(f"stored lan_addr {addr!r} is not an IPv4 address")
    if host_addrs and addr not in host_addrs:
        raise AgentboxError(f"stored lan_addr {addr} is not an address of this host ({', '.join(sorted(host_addrs))}); "
                            "give --lan ADDR:PORT")
    return addr

