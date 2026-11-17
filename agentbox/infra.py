"""The argv shapes of a session's isolated networks and egress proxy, shared with `ab profile install`.
Moved out of session.py (item 54); session re-exports it."""
import os

from . import sockguard


SQUID_READY = "Accepting HTTP Socket connections"


# The two argv shapes that bring up an isolated network and the squid in front of it. Module-level, and
# taking their labels rather than reading a project, because there is a SECOND consumer: `ab profile
# install` needs the same sealed pair of networks and the same hardened proxy, and it has no project. A
# copy of these in the installer is a copy of the network isolation -- `isolate=strict`, `--internal`, the
# proxy's cap set -- so it is one definition used twice instead.
def net_create_argv(settings, labels, name, *, internal, dev_owner=""):
    """`podman network create` for a session (or an install) network.

    IPv4 only, by decision (docs/decisions.md): no IPv6 destination is routable from the session.
    `dev_owner` marks the network as owned, which is what the dev tier's sockguard checks before it lets a
    container join one."""
    args = ["network", "create", "--ipv6=false", "--opt", "isolate=strict"]
    if internal:
        args.append("--internal")
        if dev_owner:
            args += ["--label", f"{sockguard.DEV_OWNER}={dev_owner}"]
    for k, v in labels.items():
        args += ["--label", f"{k}={v}"]
    return args + [name]


# Every sidecar agentbox runs (proxy, sockguard, holder) logs to a file in podman's own container directory
# instead of the host's default driver. With journald (Ubuntu's default), each `podman logs` searched the whole
# journal: 3-11 s a call, paid once while waiting for squid and once per sidecar at teardown (tests/perf,
# 16 Nov). The file goes when the container is removed, after teardown has kept what it needs.
SIDECAR_LOG_DRIVER = "--log-driver=k8s-file"
# k8s-file has no size limit of its own (containers.conf log_size_max=-1), and squid writes every request to
# stdout: a long or orphaned session would grow the proxy's log without end, and teardown reads it whole.
# 256 MB is about two million requests; past it conmon starts the file again (review 16 Nov, R-5).
SIDECAR_LOG_SIZE = "--log-opt=max-size=256mb"


def proxy_run_argv(settings, labels, name, *, conf_path, networks):
    """`podman run -d` for the egress proxy: capabilities dropped to what squid needs to drop privileges,
    read-only root with tmpfs for its spool and logs, and on both networks so it is the only route out."""
    s = settings
    args = ["run", "-d", f"--name={name}", f"--hostname={name}"]
    args += [f"--label={k}={v}" for k, v in labels.items()]
    args += ["--cap-drop=ALL", "--cap-add=SETUID,SETGID", "--security-opt=no-new-privileges", "--read-only"]
    for d in ("/var/spool/squid", "/var/log/squid", "/run", "/tmp"):
        args.append(f"--tmpfs={d}:rw,nosuid,nodev,notmpcopyup")
    args += [f"--memory={s.squid_memory}", f"--memory-swap={s.squid_memory}", "--pids-limit=256",
             f"--oom-score-adj={int(s.oom_score_adj)}"]
    if s.cgroup_parent:
        args.append(f"--cgroup-parent={s.cgroup_parent}")
    args += [f"--network={n}" for n in networks]
    args += [SIDECAR_LOG_DRIVER, SIDECAR_LOG_SIZE,
             f"--mount=type=bind,src={conf_path},dst=/etc/squid/squid.conf,ro", s.squid_image]
    return args


def sockguard_run_argv(settings, labels, name, *, service_sock, socket_path, conf_path, cgroup_parent):
    """`podman run -d` for a sockguard in front of a private podman service: no network, read-only, every
    capability dropped, its config read-only. A session's (in its slice) and a deploy's (`ab deploy`: the host's
    default cgroup, D5) share this shape."""
    s = settings
    args = ["run", "-d", f"--name={name}", f"--hostname={name}", SIDECAR_LOG_DRIVER, SIDECAR_LOG_SIZE]
    args += [f"--label={k}={v}" for k, v in labels.items()]
    args += ["--network=none", "--userns=keep-id", f"--user={os.getuid()}:{os.getgid()}", "--cap-drop=ALL",
             "--security-opt=no-new-privileges", "--read-only", f"--memory={s.sockguard_memory}",
             f"--memory-swap={s.sockguard_memory}", "--pids-limit=128", f"--oom-score-adj={int(s.oom_score_adj)}"]
    if cgroup_parent:
        args.append(f"--cgroup-parent={cgroup_parent}")
    args += [f"--mount=type=bind,src={service_sock},dst={sockguard.UPSTREAM}",
             f"--mount=type=bind,src={os.path.dirname(socket_path)},dst={os.path.dirname(sockguard.LISTEN)}",
             f"--mount=type=bind,src={conf_path},dst=/etc/sockguard/sockguard.yaml,ro",
             s.sockguard_image, "serve", "-c", "/etc/sockguard/sockguard.yaml"]
    return args


def holder_run_argv(settings, labels, name, *, network, cgroup_parent):
    """A container that only sleeps on `network`: the gate's builds run their RUN steps in its network
    namespace (gate_images.holder_ns)."""
    s = settings
    args = ["run", "-d", f"--name={name}", f"--hostname={name}", SIDECAR_LOG_DRIVER, SIDECAR_LOG_SIZE]
    args += [f"--label={k}={v}" for k, v in labels.items()]
    args += [f"--network={network}", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
             "--memory=32m", "--memory-swap=32m", "--pids-limit=16", f"--oom-score-adj={int(s.oom_score_adj)}"]
    if cgroup_parent:
        args.append(f"--cgroup-parent={cgroup_parent}")
    return args + ["--entrypoint=/bin/sleep", s.squid_image, "infinity"]
