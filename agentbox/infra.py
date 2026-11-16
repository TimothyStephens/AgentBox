"""The argv shapes of a session's isolated networks and egress proxy, shared with `ab profile install`.
Moved out of session.py (item 54); session re-exports it."""
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
    args += [SIDECAR_LOG_DRIVER, f"--mount=type=bind,src={conf_path},dst=/etc/squid/squid.conf,ro", s.squid_image]
    return args
