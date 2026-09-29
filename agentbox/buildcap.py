"""Build-time memory cap: the part of the step 5 gate that puts every build under agentbox.slice.

Builds run inside the podman service, not under `ab`, so they only get a cap if the build request
carries one. Measured on this host (docs/decisions.md, 29 Sep):
  * the build API's `cgroupparent` is a cgroupfs path, not a systemd unit name;
  * a child path under the slice is capped by the slice only while the slice is active; while it is
    idle, the runtime creates the path itself and the build runs with memory.max = max, silently.
So before every build the gate calls `rewrite_build_query`, which refuses unless the slice is active
with a finite cap, drops any cgroup/memory settings the client sent, and sets its own.
"""
import os
import subprocess

from .errors import AgentboxError

CLIENT_KEYS = ("cgroupparent", "memory", "memswap", "cpusetcpus", "cpuquota", "cpuperiod", "cpushares")


def slice_cgroup_path(name, uid=None):
    """The slice's path relative to the cgroup2 root, as the build API wants it."""
    uid = os.getuid() if uid is None else uid
    return f"/user.slice/user-{uid}.slice/user@{uid}.service/{name}"


def slice_state(name, *, root="/sys/fs/cgroup", uid=None, systemctl=True):
    """(active, why). Active means systemd has the slice running and its cgroup has a finite memory.max."""
    d = root + slice_cgroup_path(name, uid)
    if systemctl:
        try:
            p = subprocess.run(["systemctl", "--user", "show", name, "-p", "ActiveState", "--value"],
                               capture_output=True, text=True, timeout=15)
            state = p.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            state = "unknown"
        if state != "active":
            return False, f"{name} is {state or 'unknown'}, not active"
    try:
        with open(os.path.join(d, "memory.max")) as f:
            mem = f.read().strip()
    except OSError:
        return False, f"{d} does not exist"
    if not mem.isdigit():
        return False, f"{name} has no memory cap (memory.max = {mem})"
    return True, f"{name} active, memory.max {int(mem) >> 20} MB"


def rewrite_build_query(pairs, *, slice_name, session, memory_bytes, state=slice_state):
    """Force the build's cgroup and memory. `pairs` is the parsed query [(k, v), ...]. Refuses if the slice
    is not active or the client tried to choose its own cgroup parent."""
    for k, _ in pairs:
        if k.lower() == "cgroupparent":
            raise AgentboxError("build refused: the client may not choose a cgroup parent")
    active, why = state(slice_name)
    if not active:
        raise AgentboxError(f"build refused: {why}; an uncapped build is not allowed")
    kept = [(k, v) for k, v in pairs if k.lower() not in CLIENT_KEYS]
    return kept + [
        ("cgroupparent", f"{slice_cgroup_path(slice_name)}/{session}-build"),
        ("memory", str(int(memory_bytes))),
        ("memswap", str(int(memory_bytes))),
    ]
