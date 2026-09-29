"""The agentbox.slice cap: refuse to start without it, warn when it is nearly full."""
import os
import subprocess

from .errors import AgentboxError


def slice_dir(name, uid=None):
    uid = os.getuid() if uid is None else uid
    return f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/{name}"


def unit_props(name):
    """FragmentPath and MemoryMax of a user unit. systemd reports any slice as "loaded", even with no
    unit file, so installation is judged by the unit file and a finite MemoryMax."""
    try:
        p = subprocess.run(["systemctl", "--user", "show", name, "-p", "FragmentPath", "-p", "MemoryMax"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def usage(name):
    """(memory.current, memory.max or None, pids.current, pids.max or None) of the slice, if it exists."""
    d = slice_dir(name)
    def num(v):
        return int(v) if v and v.isdigit() else None
    return (num(_read(os.path.join(d, "memory.current"))), num(_read(os.path.join(d, "memory.max"))),
            num(_read(os.path.join(d, "pids.current"))), num(_read(os.path.join(d, "pids.max"))))


def check(settings, warn):
    name = settings.cgroup_parent
    if not name:
        return
    props = unit_props(name)
    if not props.get("FragmentPath") or props.get("MemoryMax", "infinity") in ("", "infinity"):
        raise AgentboxError(f"{name} is not installed with a memory cap; run `make install-slice` in the "
                            "agentbox repo first, or set cgroup_parent = \"\" in agentbox.toml to run without it")
    mem, mem_max, pids, pids_max = usage(name)
    frac = float(settings.slice_warn_fraction)
    if mem is not None and mem_max and mem > frac * mem_max:
        warn(f"agentbox: {name} is using {mem >> 20} MB of its {mem_max >> 20} MB cap; "
             "a new session may be OOM-killed")
    if pids is not None and pids_max and pids > frac * pids_max:
        warn(f"agentbox: {name} has {pids} of {pids_max} tasks")
