"""make itest-oom: push agentbox.slice past its memory cap and check who dies. Run by Tim at a quiet time.

Needs: agentbox.slice installed, MemAvailable > 5 GB (the model server unloaded). Everything it creates is
abt-<run>-*, labelled agentbox-test=1, and removed afterwards; it only reads other containers' state.

The agent runs with oom_score_adj 500, a dev-stack stand-in with 1000. Inside the slice the kernel scales
oom_score_adj to the slice's limit, so the 500-point gap is worth 0.5 x 4400 MB = 2200 MB: the dev container
dies first unless the agent uses more than 2200 MB beyond it.

Scenario 1 (PASS/FAIL): agent ~1.2 GB, dev grows to ~3.9 GB  -> dev killed, agent survives.
Scenario 2 (PASS/FAIL): agent ~3.0 GB, dev grows to ~1.6 GB  -> dev killed, agent survives (the agent is the
  bigger one, but by less than 2200 MB).
Scenario 3 (INFO): agent ~3.6 GB, dev grows to ~0.8 GB -> the slice fills with the agent ~2.9 GB ahead, past
  the margin: the agent is expected to die. Reported, not judged: it shows where the margin ends.
Every scenario also checks the kill stayed inside agentbox.slice; the run checks that no live-stack container
(or any other container outside the tests) restarted.
"""
import json
import os
import re
import subprocess
import sys
import time

from tests.integration import harness
from tests.integration.harness import LABEL, podman

SLICE_DIR = f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/agentbox.slice"
MIN_AVAILABLE_MB = 5000
HOG_IMAGE = "docker.io/library/python:3.12-slim"
RESULTS = []


def result(ok, text):
    tag = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    RESULTS.append(ok)
    print(f"{tag}  {text}", flush=True)


def meminfo(key):
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith(key + ":"):
                return int(line.split()[1]) // 1024
    return 0


def events(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, v = line.split()
                out[k] = int(v)
    except OSError:
        pass
    return out


def vmstat_oom_kills():
    with open("/proc/vmstat") as f:
        for line in f:
            if line.startswith("oom_kill "):
                return int(line.split()[1])
    return 0


def outside_containers():
    """{name: (StartedAt, RestartCount, OOMKilled)} for every container not created by the tests."""
    names = podman("ps", "-a", "--format", "{{.Names}}").stdout.split()
    out = {}
    for n in names:
        if n.startswith("abt-"):
            continue
        r = podman("inspect", "-f", "{{.State.StartedAt}}|{{.RestartCount}}|{{.State.OOMKilled}}|"
                   '{{index .Config.Labels "io.podman.compose.project"}}', n)
        if r.returncode == 0:
            started, restarts, oom, proj = r.stdout.strip().split("|")
            out[n] = (started, restarts, oom, proj)
    return out


def container_cgroup(name):
    pid = podman("inspect", "-f", "{{.State.Pid}}", name).stdout.strip()
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return "/sys/fs/cgroup" + f.read().strip().split("::", 1)[1]
    except (OSError, IndexError):
        return None


def mem_current_mb(cg):
    # the container's own cgroup may be a child ("container") of the libpod scope; use the scope
    for d in (cg, os.path.dirname(cg or "")):
        try:
            with open(os.path.join(d, "memory.current")) as f:
                return int(f.read()) >> 20
        except (OSError, TypeError, ValueError):
            continue
    return 0


def agent_session(env, gb):
    """Start an agent session whose shell holds `gb` GB (node Buffer, pages touched). Returns (proc, name)."""
    size = int(gb * (1 << 30))
    proc = env.ab_bg("session", "start", "claude", "oom", "--shell", "--no-tty", "--",
                     "-c", f"node -e 'const b=Buffer.alloc({size},1); console.log(\"HELD\"); "
                           f"setInterval(()=>b[0]++,1000)'")
    name = harness.wait_for(lambda: podman("ps", "--format", "{{.Names}}", "--filter", f"label={LABEL}",
                                           "--filter", "label=agentbox.role=agent").stdout.split(), timeout=120)
    if not name:
        raise RuntimeError("agent session did not start")
    name = name[0]
    cg = container_cgroup(name)
    harness.wait_for(lambda: mem_current_mb(cg) >= gb * 1024 * 0.9 or None, timeout=120)
    print(f"      agent {name} holds {mem_current_mb(cg)} MB", flush=True)
    return proc, name


def dev_hog(name, gb):
    """A dev-stack stand-in in the slice (what the gate will create): OOM score 1000, own cap 4g."""
    target = int(gb * 1024)
    code = (f"import time\nchunks=[]\nfor i in range({target // 100}):\n    chunks.append(b'x'*(100<<20))\n"
            f"    print('MB', (i+1)*100, flush=True)\n    time.sleep(0.05)\nprint('HOLDING', flush=True)\n"
            f"time.sleep(600)\n")
    r = podman("run", "-d", "--name", name, "--label", "agentbox-test=1", "--label", LABEL,
               "--label", "agentbox.role=dev", "--network", "none", "--cgroup-parent=agentbox.slice",
               "--oom-score-adj=1000", "--memory=4g", "--memory-swap=4g", "--pids-limit=64",
               HOG_IMAGE, "python3", "-c", code)
    if r.returncode != 0:
        raise RuntimeError(f"dev hog did not start: {r.stderr}")


def running(name):
    return podman("inspect", "-f", "{{.State.Running}}", name).stdout.strip() == "true"


def oom_killed(name):
    return podman("inspect", "-f", "{{.State.OOMKilled}}", name).stdout.strip() == "true"


def oom_adj(name):
    pid = podman("inspect", "-f", "{{.State.Pid}}", name).stdout.strip()
    try:
        with open(f"/proc/{pid}/oom_score_adj") as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def scenario(env, n, agent_gb, dev_gb, judged):
    print(f"\n--- scenario {n}: agent {agent_gb} GB, dev container up to {dev_gb} GB ---", flush=True)
    harness.wait_for(lambda: not podman("ps", "-a", "--format", "{{.Names}}", "--filter", f"label={LABEL}",
                                        "--filter", "label=agentbox.role=agent").stdout.split() or None, timeout=120)
    slice_before = events(os.path.join(SLICE_DIR, "memory.events"))
    host_before = vmstat_oom_kills()
    proc, agent = agent_session(env, agent_gb)
    agent_adj = oom_adj(agent)
    dev = f"abt-{harness.RUN}-dev-hog-{n}"
    dev_hog(dev, dev_gb)
    harness.wait_for(lambda: oom_adj(dev) is not None or not running(dev) or None, timeout=30)
    dev_adj = oom_adj(dev)
    # wait until one of them dies or the dev container holds everything it wanted
    harness.wait_for(lambda: (not running(dev) or not running(agent) or
                              "HOLDING" in podman("logs", dev).stdout) or None, timeout=180)
    time.sleep(3)
    slice_after = events(os.path.join(SLICE_DIR, "memory.events"))
    host_after = vmstat_oom_kills()
    dev_log = podman("logs", dev).stdout.split()
    reached = max([int(x) for x in dev_log if x.isdigit()] or [0])
    agent_alive, dev_alive = running(agent), running(dev)
    slice_kills = slice_after.get("oom_kill", 0) - slice_before.get("oom_kill", 0)
    host_kills = host_after - host_before
    print(f"      oom_score_adj: agent {agent_adj}, dev {dev_adj}")
    print(f"      dev reached {reached} MB; dev running={dev_alive} OOMKilled={oom_killed(dev)}; "
          f"agent running={agent_alive}; OOM kills: slice +{slice_kills}, host total +{host_kills}", flush=True)
    tag = f"[{n}: agent {agent_gb} GB, dev {dev_gb} GB]"
    result(agent_adj == 500, f"{tag} the agent runs with oom_score_adj 500 (got {agent_adj})")
    result(slice_kills >= 1, f"{tag} the kill happened inside agentbox.slice (memory.events oom_kill +{slice_kills})")
    result(host_kills == slice_kills, f"{tag} no OOM kill outside the slice (host +{host_kills}, slice +{slice_kills})")
    if judged:
        result(oom_killed(dev) and not dev_alive, f"{tag} the dev container was OOM-killed (OOMKilled is true)")
        result(agent_alive, f"{tag} the agent survived")
    else:
        who = "the agent" if not agent_alive else ("the dev container" if not dev_alive else "nobody")
        result(None, f"{tag} {who} was killed (expected: the agent, ~2.9 GB ahead is past the 2.2 GB margin)")
    podman("rm", "-f", "-t", "2", dev)
    if agent_alive:
        env.ab("session", "kill", agent)
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
    proc.communicate()


def main():
    avail = meminfo("MemAvailable")
    print(f"MemAvailable {avail} MB (free -m 'available'); slice {SLICE_DIR}")
    if not harness.slice_installed():
        print("agentbox.slice is not installed: run make install-slice first")
        return 2
    if avail < MIN_AVAILABLE_MB:
        print(f"refusing to run: available {avail} MB < {MIN_AVAILABLE_MB} MB. Unload the model server and retry.")
        return 2
    if not podman("image", "exists", HOG_IMAGE).returncode == 0:
        print(f"image {HOG_IMAGE} is missing; pull it first: podman pull {HOG_IMAGE}")
        return 2
    with open(os.path.join(SLICE_DIR, "memory.max")) as f:
        print(f"slice memory.max {int(f.read()) >> 20} MB")

    env = harness.Env()
    env.make_project("oom", secrets_files=False)
    before = outside_containers()
    try:
        scenario(env, 1, agent_gb=1.2, dev_gb=3.9, judged=True)
        scenario(env, 2, agent_gb=3.0, dev_gb=1.6, judged=True)
        scenario(env, 3, agent_gb=3.6, dev_gb=0.8, judged=False)
    finally:
        after = outside_containers()
        changed = {n: (before[n], after.get(n)) for n in before if after.get(n) != before[n]}
        media = [n for n, v in before.items() if v[3] == "media_server"]
        media_changed = [n for n in media if n in changed]
        print()
        result(not media_changed, f"no media_server container restarted or was OOM-killed ({len(media)} checked)"
               + (f": {media_changed}" if media_changed else ""))
        result(not changed, f"no other container outside the tests changed ({len(before)} checked)"
               + (f": {sorted(changed)}" if changed else ""))
        print(f"MemAvailable after: {meminfo('MemAvailable')} MB")
        harness.cleanup()
    fails = [r for r in RESULTS if r is False]
    print(f"\nRESULT itest-oom: {sum(r is True for r in RESULTS)} pass, {len(fails)} fail, "
          f"{sum(r is None for r in RESULTS)} info")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
