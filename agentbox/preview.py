"""ab preview: open one dev-tier service of a running session from your desktop browser (design: "Preview and
deploy"; built 2 Oct after trial 2).

    ab preview <session> <service>:<port> [--port N]    -> http://127.0.0.1:N/ on this host
    ab preview <session> --stop                         -> remove the session's previews

A forwarder container joins the session's internal network and publishes one port on 127.0.0.1 only; it
relays each connection to <service>:<port> (busybox `nc -lk -e`). From the desktop: `ssh -N -L N:127.0.0.1:N
<this host>`. It is never bound to 0.0.0.0 or a LAN address. It carries the session's labels, so the session's
teardown (and `ab kill`, `ab gc`) removes it; a preview exists only while its session does. The service must
be one of that session's own dev containers.
"""
import getpass
import os
import re
import socket

from .errors import AgentboxError

BIND = "127.0.0.1"                  # the only address a preview listens on
INNER_PORT = 8080                   # the forwarder's own port inside its container (unprivileged)
SERVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
COMPOSE_SERVICE_LABELS = ("com.docker.compose.service", "io.podman.compose.service")


def parse_target(target):
    service, sep, port = (target or "").rpartition(":")
    if not sep or not SERVICE.match(service) or not port.isdigit() or not 0 < int(port) < 65536:
        raise AgentboxError(f"preview target {target!r}: give <service>:<port>, e.g. web:8000")
    return service, int(port)


def free_port():
    s = socket.socket()
    try:
        s.bind((BIND, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def find_service(podman, session, service):
    """The running dev container of `session` named `service` (container name or compose service)."""
    sel = {"agentbox.session": session, "agentbox.role": "dev"}
    found = []
    for c in podman.containers_json(sel):
        labels = c.get("Labels") or {}
        names = [n.lstrip("/") for n in c.get("Names") or []]
        if service in names or any(labels.get(k) == service for k in COMPOSE_SERVICE_LABELS):
            found.append(c)
    running = [c for c in found if c.get("State") == "running"]
    if not running:
        raise AgentboxError(f"session {session} has no running dev container {service!r}"
                            + (" (it exists but is not running)" if found else ""))
    if len(running) > 1:
        raise AgentboxError(f"{service!r} matches {len(running)} containers of {session}; use a container name")
    return (running[0].get("Names") or ["?"])[0].lstrip("/")


def forwarder_args(settings, project, session, container, port, inner_port, labels):
    """The forwarder's argv after `podman`: published on 127.0.0.1 only, hardened like the session's
    sidecars, on the session's internal network only."""
    if not 1024 <= port <= 65535:
        raise AgentboxError(f"preview port {port}: use 1024-65535")
    args = ["run", "-d", f"--name={session}-preview-{port}", f"--hostname={session}-preview"]
    args += [f"--label={k}={v}" for k, v in labels.items()]
    args += [f"--network={session}-int", f"--publish={BIND}:{port}:{INNER_PORT}",
             "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--memory=32m",
             "--memory-swap=32m", "--pids-limit=64", f"--oom-score-adj={int(settings.oom_score_adj)}"]
    if settings.cgroup_parent:
        args.append(f"--cgroup-parent={settings.cgroup_parent}")
    args += ["--pull=never", "--entrypoint=nc", settings.preview_image,
             "-lk", "-p", str(INNER_PORT), "-e", "nc", container, str(inner_port)]
    return args


def start(settings, podman, session_name, target, port=None):
    """Start a preview; returns (port, desktop command)."""
    from . import session as sessionmod
    info = sessionmod.sessions(settings, podman).get(session_name)
    if not info or not info.get("alive"):
        raise AgentboxError(f"no running session named {session_name} (a preview lives only with its session)")
    project = (info.get("meta") or {}).get("project")
    if not project:
        raise AgentboxError(f"session {session_name} has no project recorded")
    service, inner = parse_target(target)
    container = find_service(podman, session_name, service)
    port = int(port) if port else free_port()
    labels = sessionmod.labels_for(settings, project, session_name, "preview")
    labels["agentbox.preview"] = f"{service}:{inner}"
    p = podman.run(forwarder_args(settings, project, session_name, container, port, inner, labels), check=False)
    if p.returncode != 0:
        raise AgentboxError(f"preview not started: {(p.stderr or p.stdout or '').strip()[-300:]}")
    desk = f"ssh -N -L {port}:{BIND}:{port} {getpass.getuser()}@{socket.gethostname()}"
    return port, desk


def stop(settings, podman, session_name):
    """Remove a session's previews; returns their names."""
    names = podman.ids("container", {"agentbox.session": session_name, "agentbox.role": "preview"})
    for n in names:
        podman.run(["rm", "-f", "-t", "1", n], check=False)
    return names
