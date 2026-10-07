"""Podman access for a session: a private podman API service, and sockguard in front of it.

This diagram is the OBSERVE tier (agent reaches sockguard directly). The DEV tier puts the gate in front:
agent --> gate --> a dev sockguard (owner isolation) --> podman service.

    [observe] agent --(/run/agentbox/podman.sock)--> sockguard --> <service dir>/podman.sock --> podman service

The podman service is started per session by the launcher (no user unit is enabled on ubuntu-dev, and
enabling one would be a systemd change), listens on a socket in <run>/services/<session> — deliberately
OUTSIDE the session's run dir, so no launcher mount rule can reach the raw socket (review #6) — and is
stopped at teardown. Only the sockguard container mounts it. The agent gets sockguard's socket.

Tier `observe` (step 4): list, inspect (Docker API) and per-container stats, only on containers of the
project's live compose project (`io.podman.compose.project=<compose_project>`); environments are redacted.
Logs only when the project sets `observe_logs = true`. No libpod inspect, top or pods (review 2026-10-01). No writes, no exec, no image pulls. Sockguard's `mode: audit` forwards what the rules would deny,
so it is never used in a session; `discovery_config` is for a test that harvests what a client calls.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time

from .errors import AgentboxError

LISTEN = "/run/sg/sockguard.sock"
UPSTREAM = "/run/podman/podman.sock"
AGENT_SOCKET = "/run/agentbox/podman.sock"
COMPOSE_LABEL = "io.podman.compose.project"


def _allow(method, *paths):
    return [{"match": {"method": method, "path": p}, "action": "allow"} for p in paths]


# What the observe tier may call, on both of podman's API surfaces; everything else is denied. The discovery
# test (tests/integration/test_step4_sockguard.py, audit mode) found podman --remote ps/inspect/logs/top/pod
# ps/version call exactly: _ping and libpod/_ping, libpod/version, libpod/containers/json, and a container's
# json, logs and top, libpod/pods/json. `podman stats` calls GET /libpod/containers/stats, which reports every
# container on the host with no label filter; sockguard refuses it in every mode, so stats are per container
# through the Docker API (/containers/<name>/stats), which is scoped.
#
# Review 2026-10-01 #1 and #4 (reproduced in test_step4_sockguard.test_h): libpod inspect returns
# `CreateCommand` with every `-e KEY=VALUE` the container was created with (sockguard redacts only
# Config.Env), so the live stack's secrets leaked; pod list and inspect are not label-scoped (another
# project's pod was readable); `top` runs ps inside a live container and takes caller-chosen ps flags.
# So: lists on both surfaces (no create command in them), inspect and stats through the Docker API only,
# no top, no pods.
OBSERVE_RULES = (
    _allow("GET", "/_ping", "/libpod/_ping", "/version", "/libpod/version")
    + _allow("HEAD", "/_ping", "/libpod/_ping")
    + _allow("GET", "/containers/json", "/containers/*/json", "/containers/*/stats")
    + _allow("GET", "/libpod/containers/json")
)
# Only with the project's `observe_logs = true`: live-stack logs can hold secrets (an apikey= in a logged
# URL), and whatever the agent reads also goes to its model provider.
LOG_RULES = _allow("GET", "/containers/*/logs", "/libpod/containers/*/logs")
DENY_REST = [{"match": {"method": "*", "path": "/**"}, "action": "deny",
              "reason": "agentbox observe tier: reads of the project's live stack only"}]


def observe_rules(logs=False):
    return list(OBSERVE_RULES) + (list(LOG_RULES) if logs else []) + DENY_REST


def config(compose_project, *, mode="enforce", rules=None, logs=False):
    """The sockguard config for one session (JSON, which sockguard's YAML loader reads)."""
    if mode not in ("enforce", "audit"):
        raise AgentboxError(f"sockguard mode {mode!r}")
    if not compose_project or "=" in compose_project or "," in compose_project:
        raise AgentboxError(f"compose project {compose_project!r} cannot be used as a visibility label")
    doc = {
        "listen": {"socket": LISTEN, "socket_mode": "0600"},
        "upstream": {"socket": UPSTREAM, "flavor": "podman"},
        "log": {"level": "info", "format": "json", "access_log": True,
                "audit": {"enabled": True, "format": "json", "output": "stdout"}},
        # observe no longer allows `top`, and allows `logs` only with the project's observe_logs; the
        # visibility label limits what remains to the live stack
        "insecure_allow_read_exfiltration": True,
        "response": {"visible_resource_labels": [f"{COMPOSE_LABEL}={compose_project}"],
                     "redact_container_env": True, "redact_mount_paths": True,
                     "redact_network_topology": True, "redact_sensitive_data": True,
                     "redact_host_topology": True},
        "clients": {"default_profile": "session",
                    "profiles": [{"name": "session", "mode": mode,
                                  "rules": list(rules if rules is not None else observe_rules(logs))}]},
    }
    return json.dumps(doc, indent=2) + "\n"


DEV_OWNER = "agentbox.dev-owner"
REGISTRIES = ["docker.io", "ghcr.io", "lscr.io", "quay.io"]


def _surfaces(method, *paths):
    """Each path on both API surfaces (Docker and libpod)."""
    return _allow(method, *paths, *("/libpod" + p for p in paths))


# The dev tier: the agent's own dev stack. Owner isolation stamps agentbox.dev-owner=<session> on everything
# created and refuses other owners' resources; the gate in front additionally requires role=dev on every
# named resource, so unlabelled containers (live stacks, the session's own proxy and sockguard) are out of
# reach. Not allowed: image deletion, archive/export/commit, checkpoint/restore, kube, secrets, prune.
DEV_RULES = (
    _surfaces("GET", "/_ping", "/version", "/info", "/events", "/containers/json", "/containers/*/json",
              "/containers/*/logs", "/containers/*/top", "/containers/*/stats", "/containers/*/changes",
              "/exec/*/json", "/images/json", "/images/**/json", "/images/**/history", "/networks/json",
              "/networks/*/json", "/volumes/json", "/volumes/*/json")
    + _allow("GET", "/networks", "/networks/*", "/volumes", "/libpod/containers/*/exists",
             "/libpod/containers/*/healthcheck", "/libpod/images/*/exists", "/libpod/networks/*/exists",
             "/libpod/volumes/*/exists", "/libpod/pods/json", "/libpod/pods/*/json", "/libpod/pods/*/exists",
             "/libpod/pods/*/top")
    + _allow("HEAD", "/_ping", "/libpod/_ping")
    + _surfaces("POST", "/containers/create", "/containers/*/start", "/containers/*/stop", "/containers/*/kill",
                "/containers/*/restart", "/containers/*/wait", "/containers/*/attach", "/containers/*/resize",
                "/containers/*/exec", "/exec/*/start", "/exec/*/resize", "/build", "/networks/create",
                "/volumes/create")
    + _allow("POST", "/images/create", "/libpod/images/pull", "/libpod/containers/*/init", "/libpod/exec/*/remove",
             "/libpod/pods/create", "/libpod/pods/*/start", "/libpod/pods/*/stop", "/libpod/pods/*/kill",
             "/libpod/pods/*/restart")
    + _surfaces("DELETE", "/containers/*", "/networks/*", "/volumes/*")
    + _allow("DELETE", "/libpod/pods/*")
    + [{"match": {"method": "*", "path": "/**"}, "action": "deny",
        "reason": "agentbox dev tier: not an operation on the session's own dev stack"}]
)


def dev_config(session, *, bind_roots, mode="enforce", rules=None):
    """Sockguard for the dev tier, behind the gate. Listens on the same in-container path as observe."""
    if mode not in ("enforce", "audit"):
        raise AgentboxError(f"sockguard mode {mode!r}")
    binds = sorted(bind_roots)
    body_policy = {
        "container_create": {"allowed_bind_mounts": binds},
        "libpod_container_create": {"allowed_bind_mounts": binds},
        "container_remove": {"allow_force": True, "allow_remove_volumes": True},
        "exec": {"allow_root_user": True, "allow_privileged": False},
        "image_pull": {"allowed_registries": REGISTRIES, "allow_official": True},
        "build": {"allow_run_instructions": True, "allow_remote_context": False, "allow_host_network": False},
    }
    doc = {
        "listen": {"socket": LISTEN, "socket_mode": "0600"},
        "upstream": {"socket": UPSTREAM, "flavor": "podman", "request_timeout": "off"},
        "log": {"level": "info", "format": "json", "access_log": True,
                "audit": {"enabled": True, "format": "json", "output": "stdout"}},
        # exec with any command: the gate only lets it reach the session's own dev containers
        "insecure_allow_body_blind_writes": True,
        "insecure_allow_read_exfiltration": True,
        "ownership": {"owner": session, "label_key": DEV_OWNER, "allow_unowned_images": True},
        # A profile has its own request-body policy (it does not inherit the root's), so it is set there.
        "clients": {"default_profile": "session",
                    "profiles": [{"name": "session", "mode": mode, "request_body": body_policy,
                                  "rules": list(rules if rules is not None else DEV_RULES)}]},
    }
    return json.dumps(doc, indent=2) + "\n"


def dev_discovery_config(session, *, bind_roots):
    """Deny-all in audit mode for the dev tier: tests only."""
    return dev_config(session, bind_roots=bind_roots, mode="audit",
                      rules=[{"match": {"method": "*", "path": "/**"}, "action": "deny", "reason": "discovery"}])


def discovery_config(compose_project):
    """Deny-all in audit mode: every call is forwarded and logged as would_deny. Tests only."""
    return config(compose_project, mode="audit",
                  rules=[{"match": {"method": "*", "path": "/**"}, "action": "deny", "reason": "discovery"}])


def ping(path, timeout=5):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(b"GET /_ping HTTP/1.0\r\nHost: d\r\n\r\n")
        data = s.recv(64)
        s.close()
        return data.startswith(b"HTTP/1.") and b" 200" in data[:16]
    except OSError:
        return False


def wait_socket(path, timeout, what, alive=lambda: True):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and ping(path):
            return
        if not alive():
            raise AgentboxError(f"{what} exited before its socket came up")
        time.sleep(0.25)
    raise AgentboxError(f"{what} socket {path} not ready after {timeout}s")


def start_service(podman_bin, service_dir, timeout, *, slice_name="", unit="", tmpdir=None):
    """Start `podman system service` on <service_dir>/podman.sock. Returns (socket path, process); the PID is
    kept in <service_dir>/service.pid for teardown and gc. The service dir is <run>/services/<session>, outside
    the session's run dir, so no launcher mount rule can reach the raw socket (review 2026-10-01 #6).

    With a slice, it runs in a transient scope under it (`systemd-run --user --scope`, which registers the
    scope and then execs podman in place, so the PID is the service's): its memory and tasks count against
    agentbox.slice like every container of the session. Nothing is written to systemd's configuration."""
    d = service_dir
    os.makedirs(os.path.dirname(d), mode=0o700, exist_ok=True)
    os.makedirs(d, mode=0o700, exist_ok=True)
    sock = os.path.join(d, "podman.sock")
    log = open(os.path.join(d, "service.log"), "ab")
    argv = [podman_bin, "system", "service", "--time=0", f"unix://{sock}"]
    if slice_name:
        argv = ["systemd-run", "--user", "--scope", "--quiet", f"--slice={slice_name}", f"--unit={unit}",
                "--"] + argv
    env = None
    if tmpdir:
        # buildah keeps RUN --mount=type=cache directories under TMPDIR: a per-session one means no cache is
        # shared with the host's or another session's builds (review 2026-10-01 step 5, RUN mounts)
        os.makedirs(tmpdir, mode=0o700, exist_ok=True)
        env = dict(os.environ, TMPDIR=tmpdir)
    proc = subprocess.Popen(argv, env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    log.close()
    with open(os.path.join(d, "service.pid"), "w") as f:
        f.write(f"{proc.pid}\n")
    wait_socket(sock, timeout, "podman API service", alive=lambda: proc.poll() is None)
    return sock, proc


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return None


def services_under(run_root):
    """{pid: session} for every running `podman system service` or gate whose files are in a session's service
    dir under <run_root>/services/, whether or not that dir still exists."""
    root = os.path.join(run_root, "services") + "/"
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        cmd = _cmdline(int(d))
        if not cmd:
            continue
        if "system" in cmd and "service" in cmd:
            args = [a[len("unix://"):] for a in cmd if a.startswith("unix://") and a.endswith("/podman.sock")]
        elif "agentbox.gate" in cmd:
            args = [a for a in cmd if a.endswith("/gate.json")]
        else:
            continue
        for a in args:
            if a.startswith(root):
                out[int(d)] = a[len(root):].split("/", 1)[0]
    return out


def kill_service(pid):
    """SIGTERM, then SIGKILL after 10 s; reaps it if it is our child."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(40):
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return True
        except ChildProcessError:
            pass
        if not os.path.exists(f"/proc/{pid}"):
            return True
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return True


def _stop(service_dir, pidfile, marker):
    try:
        with open(os.path.join(service_dir, pidfile)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    cmd = _cmdline(pid)
    if not cmd or not all(m in cmd for m in marker) or not any(service_dir in c for c in cmd):
        return False                                 # the PID was reused by something else
    return kill_service(pid)


def stop_service(service_dir):
    """Stop the session's gate and podman service, if their PID files name live processes of this service
    dir (the PIDs may have been reused by something else)."""
    gate = _stop(service_dir, "gate.pid", ("agentbox.gate",))
    service = _stop(service_dir, "service.pid", ("system", "service"))
    return gate or service


def start_gate(cfg_path, service_dir, listen, timeout, *, slice_name="", unit=""):
    """Start the gate (python3 -m agentbox.gate <cfg>) for one session, in the slice like the service."""
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    argv = [sys.executable, "-m", "agentbox.gate", cfg_path]
    if slice_name:
        argv = ["systemd-run", "--user", "--scope", "--quiet", f"--slice={slice_name}", f"--unit={unit}",
                "--"] + argv
    env = dict(os.environ, PYTHONPATH=pkg_root)
    log = open(os.path.join(service_dir, "gate.err"), "ab")
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                            start_new_session=True)
    log.close()
    with open(os.path.join(service_dir, "gate.pid"), "w") as f:
        f.write(f"{proc.pid}\n")
    wait_socket(listen, timeout, "agentbox gate", alive=lambda: proc.poll() is None)
    return proc
