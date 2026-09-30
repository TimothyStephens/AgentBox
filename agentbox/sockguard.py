"""Podman access for a session: a private podman API service, and sockguard in front of it.

    agent --(/run/agentbox/podman.sock)--> sockguard --> <run dir>/podman/podman.sock --> podman service

The podman service is started per session by the launcher (no user unit is enabled on ubuntu-dev, and
enabling one would be a systemd change), listens only in the session's 0700 run dir, and is stopped at
teardown. Only the sockguard container mounts it. The agent gets sockguard's socket.

Tier `observe` (step 4): list, inspect, top and stats, only on containers and pods of the project's live
compose project (`io.podman.compose.project=<compose_project>`); container environments are redacted in
inspect. Logs only when the project sets `observe_logs = true`. No writes, no exec, no image pulls. Sockguard's `mode: audit` forwards what the rules would deny,
so it is never used in a session; `discovery_config` is for a test that harvests what a client calls.
"""
import json
import os
import signal
import socket
import subprocess
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
OBSERVE_RULES = (
    _allow("GET", "/_ping", "/libpod/_ping", "/version", "/libpod/version")
    + _allow("HEAD", "/_ping", "/libpod/_ping")
    + _allow("GET", "/containers/json", "/containers/*/json", "/containers/*/top", "/containers/*/stats")
    + _allow("GET", "/libpod/containers/json", "/libpod/containers/*/json", "/libpod/containers/*/top",
             "/libpod/containers/*/stats")
    + _allow("GET", "/libpod/pods/json", "/libpod/pods/*/json")
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
        # logs and top return process and log output; the visibility label limits them to the live stack
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


def start_service(podman_bin, run_dir, timeout, *, slice_name="", unit=""):
    """Start `podman system service` on <run_dir>/podman/podman.sock. Returns (socket path, process); the PID
    is kept in <run_dir>/podman/service.pid for teardown and gc.

    With a slice, it runs in a transient scope under it (`systemd-run --user --scope`, which registers the
    scope and then execs podman in place, so the PID is the service's): its memory and tasks count against
    agentbox.slice like every container of the session. Nothing is written to systemd's configuration."""
    d = os.path.join(run_dir, "podman")
    os.makedirs(d, mode=0o700, exist_ok=True)
    sock = os.path.join(d, "podman.sock")
    log = open(os.path.join(d, "service.log"), "ab")
    argv = [podman_bin, "system", "service", "--time=0", f"unix://{sock}"]
    if slice_name:
        argv = ["systemd-run", "--user", "--scope", "--quiet", f"--slice={slice_name}", f"--unit={unit}",
                "--"] + argv
    proc = subprocess.Popen(argv,
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
    """{pid: session} for every running `podman system service` whose socket is in a session run dir under
    <run_root>/sessions/, whether or not that run dir still exists."""
    prefix = f"unix://{os.path.join(run_root, 'sessions')}/"
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        cmd = _cmdline(int(d))
        if not cmd or "system" not in cmd or "service" not in cmd:
            continue
        for a in cmd:
            if a.startswith(prefix) and a.endswith("/podman/podman.sock"):
                out[int(d)] = a[len(prefix):].split("/", 1)[0]
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


def stop_service(run_dir):
    """Stop the session's podman service, if its PID file names a live `podman system service` on this run
    dir's socket (the PID may have been reused by something else)."""
    try:
        with open(os.path.join(run_dir, "podman", "service.pid")) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    cmd = _cmdline(pid)
    if not cmd or "system" not in cmd or "service" not in cmd or not any(run_dir in c for c in cmd):
        return False
    return kill_service(pid)
