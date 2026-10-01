"""Fixed hardening: flags every session container gets, and an audit of the final argv.

The audit runs on the complete `podman run` argv just before it executes, so no adapter,
preset or future code path can slip in a flag that weakens the sandbox.
"""
import os
import stat

from .errors import AgentboxError


def fixed_flags(settings, *, cgroup_parent):
    flags = [
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--read-only-tmpfs=false",
        "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777,notmpcopyup",
        "--userns=keep-id",
        "--init",
        f"--pids-limit={int(settings.agent_pids)}",
        f"--memory={settings.agent_memory}",
        f"--memory-swap={settings.agent_memory}",
        f"--cpus={settings.agent_cpus}",
        f"--oom-score-adj={int(settings.oom_score_adj)}",
    ]
    if cgroup_parent:
        flags.append(f"--cgroup-parent={cgroup_parent}")
    return flags


REQUIRED = ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--userns=keep-id")
REQUIRED_PREFIX = ("--pids-limit=", "--memory=", "--memory-swap=", "--oom-score-adj=")
FORBIDDEN_EXACT = {"--privileged", "-P", "--publish-all", "-v", "--volume", "--device", "--cap-add",
                   "--network", "--net", "--pid", "--ipc", "--uts", "--cgroupns", "--userns",
                   "--security-opt", "--mount", "-p", "--publish", "--add-host", "--env", "-e",
                   "--volumes-from", "--pod", "--group-add", "--env-file"}
FORBIDDEN_PREFIX = ("--privileged=", "--cap-add=", "--device=", "--volume=", "-v=", "--publish=",
                    "--volumes-from=", "--pod=", "--group-add=", "--env-file=", "--pid=", "--ipc=",
                    "--uts=", "--cgroupns=", "--env-host", "--systemd=", "--userns=")


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def audit(argv, *, allowed_mount_srcs, allowed_mount_dsts, networks, tmpfs_dirs=("/tmp",), allowed_sockets=()):
    """Refuse any argv that weakens the fixed hardening. `argv` starts after `podman run`."""
    # Everything before the image is an option; the audit sees options as `--flag=value` only.
    opts = []
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        opts.append(argv[i])
        i += 1
    if i >= len(argv):
        raise AgentboxError("audit: no image in podman argv")
    for flag in REQUIRED:
        if flag not in opts:
            raise AgentboxError(f"audit: missing {flag}")
    for p in REQUIRED_PREFIX:
        if not any(o.startswith(p) for o in opts):
            raise AgentboxError(f"audit: missing {p}...")
    for o in opts:
        if o in FORBIDDEN_EXACT:
            raise AgentboxError(f"audit: {o} must be written as {o}=<value>")
        if o in ("--userns=keep-id", "--read-only-tmpfs=false", "--security-opt=no-new-privileges"):
            continue
        if o.startswith(FORBIDDEN_PREFIX):
            raise AgentboxError(f"audit: forbidden option {o}")
        if o.startswith("--security-opt="):
            raise AgentboxError(f"audit: forbidden option {o}")
        if o.startswith(("--network=", "--net=")):
            val = o.split("=", 1)[1]
            if val != "none" and val not in networks:
                raise AgentboxError(f"audit: network {val!r} is not a session network")
        if o.startswith("--mount="):
            _audit_mount(o[len("--mount="):], allowed_mount_srcs, allowed_mount_dsts, allowed_sockets)
        if o.startswith("--tmpfs="):
            dst, _, tmpfs_opts = o[len("--tmpfs="):].partition(":")
            if dst not in tmpfs_dirs:
                raise AgentboxError(f"audit: unexpected tmpfs {o}")
            if "notmpcopyup" not in tmpfs_opts.split(","):
                raise AgentboxError(f"audit: tmpfs {dst} needs notmpcopyup (copy-up would copy what it hides)")
    return True


def _audit_mount(spec, allowed, allowed_dsts, allowed_sockets=()):
    fields = dict(f.split("=", 1) if "=" in f else (f, "") for f in spec.split(","))
    kind = fields.get("type")
    dst = fields.get("dst") or fields.get("destination") or fields.get("target") or ""
    if not dst.startswith("/") or os.path.normpath(dst) != dst or "/../" in dst + "/":
        raise AgentboxError(f"audit: mount destination {dst!r} is not a clean absolute path")
    if not any(_inside(dst, a) for a in allowed_dsts):
        raise AgentboxError(f"audit: mount destination {dst} is outside the session's grants")
    if kind == "tmpfs":
        # podman copies what is under a tmpfs into it by default, which would un-hide a masked directory
        if "notmpcopyup" not in fields:
            raise AgentboxError(f"audit: tmpfs mount {fields.get('dst')} needs notmpcopyup")
        return
    if kind != "bind":
        raise AgentboxError(f"audit: mount type {kind!r} not allowed")
    src = fields.get("src") or fields.get("source") or ""
    if src == "/dev/null":
        if "ro" not in fields:
            raise AgentboxError("audit: /dev/null masks must be read-only")
        return
    real = os.path.realpath(src)
    if not any(_inside(real, a) for a in allowed):
        raise AgentboxError(f"audit: bind source {src} is outside the session's grants")
    # The only socket a sandbox may get is sockguard's; a raw podman socket would bypass it (review #6).
    try:
        is_socket = stat.S_ISSOCK(os.stat(real).st_mode)
    except OSError:
        is_socket = False
    if is_socket and real not in [os.path.realpath(x) for x in allowed_sockets]:
        raise AgentboxError(f"audit: bind source {src} is a socket other than sockguard's")
    for bad in ("propagation", "bind-propagation", "relabel"):
        if bad in fields:
            raise AgentboxError(f"audit: mount option {bad} not allowed")
