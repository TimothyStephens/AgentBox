"""The throwaway trial's host-side checks (docs/trial.md). Run by Tim, not by the builder.

    python3 -m tests.trial.check snapshot   # make trial-snapshot: just before the session starts
    python3 -m tests.trial.check check      # make trial-check: after the session has ended

The snapshot records the time and, for every container of a live compose project (not agentbox's), its ID,
start time, restart count and bind-mount sources. It warns about containers running from the old launchers'
images (localhost/claudecode, localhost/omp-env) outside agentbox. The check refuses to run while a session
of the trial project is still running, then reports:
  * live stacks unchanged: the same containers, not restarted, not recreated;
  * nothing outside the trial project changed under your home since the snapshot (an mtime scan; file
    contents are never read). ab's own state and logs, podman's storage and a few caches are excluded, and
    the exclusions are printed. Changes under the live stacks' bind-mount sources ("written by live stacks,
    expected") and under ~/.claude and ~/.config/claude-container ("host-side Claude configs, written outside
    the session") are listed apart; only the rest fails the check;
  * no session container, pod, volume, network, podman service or gate of the trial project remains;
  * no gate-created volume over the trial root (a grant volume) remains.
Long lists are cut per directory ("... and N more under <dir>"). Exit status 0 only when every check passes.
"""
import json
import os
import subprocess
import sys
import time

from agentbox import config, sockguard

HOME = os.path.expanduser("~")
PROJECT = os.environ.get("AB_TRIAL_PROJECT", "abt-trial")
ROOT = os.environ.get("AB_TRIAL_ROOT", os.path.join(HOME, "repos", "abt-trial"))
PER_DIR = 5                          # changed paths printed per directory before "... and N more under <dir>"
COMPOSE_LABELS = ("com.docker.compose.project", "io.podman.compose.project")
OLD_LAUNCHERS = ("localhost/claudecode", "localhost/omp-env")
HOST_CLAUDE = (os.path.join(HOME, ".claude"), os.path.join(HOME, ".config", "claude-container"))


def podman(*args):
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=120)


def snapshot_path(settings):
    return os.path.join(settings.data_dir, "trial", f"{PROJECT}-snapshot.json")


def all_containers():
    p = podman("ps", "-a", "--format", "json")
    return json.loads(p.stdout or "[]") or []


def is_live(c):
    """A container of a live compose project, not agentbox's."""
    labels = c.get("Labels") or {}
    return "agentbox.session" not in labels and any(labels.get(k) for k in COMPOSE_LABELS)


def old_launchers(containers):
    """Running containers from the old launchers' images, outside agentbox (warned about at snapshot time)."""
    return [f"{(c.get('Names') or ['?'])[0]} ({c.get('Image')})" for c in containers
            if c.get("State") == "running" and "agentbox.session" not in (c.get("Labels") or {})
            and str(c.get("Image") or "").startswith(OLD_LAUNCHERS)]


def live_containers(containers=None):
    """{id: {name, started, restarts, binds}} for every container of a live compose project.
    Read-only: `podman ps` and `podman inspect`."""
    out = {}
    for c in containers if containers is not None else all_containers():
        if not is_live(c):
            continue
        cid = c.get("Id")
        doc = json.loads(podman("inspect", "--type", "container", cid).stdout or "[{}]")[0]
        state = doc.get("State") or {}
        out[cid] = {"name": (c.get("Names") or ["?"])[0], "started": state.get("StartedAt"),
                    "restarts": doc.get("RestartCount"), "status": state.get("Status"),
                    "binds": sorted(m.get("Source") for m in doc.get("Mounts") or []
                                    if m.get("Type") == "bind" and m.get("Source"))}
    return out


def exclusions(settings):
    """Paths under $HOME the mtime scan skips: the trial project, ab's own state and logs, and places that
    change on their own (podman's storage, which the session's containers use; caches; shell history)."""
    return sorted({os.path.realpath(p) for p in (
        ROOT, settings.data_dir, settings.config_dir, settings.backups_dir,
        os.path.join(HOME, ".local", "share", "containers"), os.path.join(HOME, ".cache"),
        os.path.join(HOME, ".local", "state"), os.path.join(HOME, "tmp"),
        os.path.join(HOME, ".bash_history"), os.path.join(HOME, ".lesshst"), os.path.join(HOME, ".viminfo"),
        os.path.join(HOME, ".python_history"), os.path.join(HOME, ".tmux"),
    ) if p} | {os.path.realpath(p) for p in os.environ.get("AB_TRIAL_EXCLUDE", "").split(":") if p})


def changed_since(since, skip, home=HOME):
    """Paths under `home` (not following symlinks) whose mtime is after `since`. Metadata only."""
    out = []
    for dirpath, dirnames, filenames in os.walk(home, followlinks=False, onerror=lambda e: None):
        real_dir = os.path.realpath(dirpath)
        dirnames[:] = [d for d in dirnames if os.path.join(real_dir, d) not in skip]
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            if os.path.join(real_dir, name) in skip:
                continue
            try:
                if os.lstat(p).st_mtime > since:
                    out.append(p)
            except OSError:
                pass
    return sorted(out)


def under(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def split_changes(changed, live_binds):
    """(failing, written by live stacks, host-side Claude configs)."""
    live = [os.path.realpath(b) for b in live_binds]
    claude = [os.path.realpath(p) for p in HOST_CLAUDE]
    fail, by_live, by_claude = [], [], []
    for p in changed:
        real = os.path.realpath(p)
        if under(real, claude):
            by_claude.append(p)
        elif under(real, live):
            by_live.append(p)
        else:
            fail.append(p)
    return fail, by_live, by_claude


def truncate(paths, per_dir=PER_DIR):
    """At most `per_dir` paths per directory, then "... and N more under <dir>"."""
    by_dir = {}
    for p in paths:
        by_dir.setdefault(os.path.dirname(p), []).append(p)
    out = []
    for d in sorted(by_dir):
        items = sorted(by_dir[d])
        out += items[:per_dir]
        if len(items) > per_dir:
            out.append(f"... and {len(items) - per_dir} more under {d}")
    return out


def running_sessions(settings):
    """Sessions of the trial project whose launcher is alive or whose containers still run."""
    from agentbox import session
    from agentbox.podman import Podman
    out = []
    for name, info in session.sessions(settings, Podman(settings.podman)).items():
        if f"-{PROJECT}-" not in name:
            continue
        if info.get("alive") or any(c.get("state") == "running" for c in info.get("containers") or []):
            out.append(name)
    return out


def remains(settings):
    """[(kind, name)] of the trial project's session resources still present."""
    sel = ["--filter", f"label=agentbox.project={PROJECT}"]
    out = []
    for kind, args, fmt in (("container", ("ps", "-a"), "{{.Names}}"), ("pod", ("pod", "ps"), "{{.Name}}"),
                            ("volume", ("volume", "ls"), "{{.Name}}"), ("network", ("network", "ls"), "{{.Name}}")):
        for name in podman(*args, "--format", fmt, *sel).stdout.split():
            out.append((kind, name))
    for pid, session in sockguard.services_under(settings.run_dir).items():
        if f"-{PROJECT}-" in session:
            out.append(("process", f"{pid} ({session})"))
    return out


def grant_volumes():
    """Volumes whose bind device is the trial root or inside it (the gate's grant volumes), whatever their labels."""
    root = os.path.realpath(ROOT)
    out = []
    for name in podman("volume", "ls", "--format", "{{.Name}}").stdout.split():
        doc = json.loads(podman("volume", "inspect", name).stdout or "[{}]")[0]
        dev = (doc.get("Options") or {}).get("device") or ""
        if dev and (os.path.realpath(dev) == root or os.path.realpath(dev).startswith(root + "/")):
            out.append(f"{name} (device {dev})")
    return out


def compare_live(before, after):
    problems = []
    for cid, b in before.items():
        a = after.get(cid)
        if a is None:
            problems.append(f"{b['name']}: gone (removed or recreated)")
        elif (a["started"], a["restarts"]) != (b["started"], b["restarts"]):
            problems.append(f"{b['name']}: started {b['started']} -> {a['started']}, "
                            f"restarts {b['restarts']} -> {a['restarts']}")
    for cid, a in after.items():
        if cid not in before:
            problems.append(f"{a['name']}: new since the snapshot")
    return problems


def snapshot(settings):
    path = snapshot_path(settings)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    containers = all_containers()
    doc = {"time": time.time(), "live": live_containers(containers), "project": PROJECT, "root": ROOT}
    with open(path, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"snapshot: {len(doc['live'])} container(s) of live compose projects recorded in {path} at "
          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(doc['time']))}")
    for w in old_launchers(containers):
        print(f"WARNING: {w} is running outside agentbox (an old launcher: root in its container, LAN access). "
              "Stop it before the trial, or its writes will show up in the check")
    print("now start the session (docs/trial.md, step b)")
    return 0


def check(settings):
    try:
        with open(snapshot_path(settings)) as f:
            snap = json.load(f)
    except OSError:
        print(f"FAIL  no snapshot at {snapshot_path(settings)}: run make trial-snapshot before the session")
        return 1
    running = running_sessions(settings)
    if running:
        print(f"REFUSED: session(s) of {PROJECT} still running: {', '.join(running)}. End them first "
              "(exit the agent, or ab kill), then rerun make trial-check")
        return 2
    fails = 0

    def line(ok, label, details=(), word=None):
        nonlocal fails
        fails += not ok
        print(f"{word or ('PASS' if ok else 'FAIL')}  {label}")
        for d in details:
            print(f"        {d}")

    problems = compare_live(snap["live"], live_containers())
    line(not problems, f"live stacks unchanged ({len(snap['live'])} containers: same IDs, start times, restart counts)",
         problems)
    skip = set(exclusions(settings))
    changed = changed_since(snap["time"], skip)
    binds = [b for c in snap["live"].values() for b in c.get("binds") or []]
    fail, by_live, by_claude = split_changes(changed, binds)
    line(not fail, f"nothing under {HOME} changed outside {ROOT} since the snapshot (mtime scan)", truncate(fail))
    if by_live:
        line(True, f"written by live stacks (expected): {len(by_live)} path(s) under their bind mounts",
             truncate(by_live), word="INFO")
    if by_claude:
        line(True, f"host-side Claude configs (written outside the session): {len(by_claude)} path(s)",
             truncate(by_claude), word="INFO")
    print("        excluded: " + ", ".join(sorted(skip)))
    left = remains(settings)
    line(not left, f"no session container, pod, volume, network, podman service or gate of {PROJECT} remains",
         [f"{k}: {n}" for k, n in left])
    grants = grant_volumes()
    line(not grants, f"no gate-created volume over {ROOT} remains", grants)
    print(f"\n{'all checks passed' if not fails else f'{fails} check(s) failed'}")
    return 1 if fails else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["snapshot"], ["check"]):
        sys.exit("usage: python3 -m tests.trial.check snapshot|check")
    settings = config.load()
    return snapshot(settings) if argv == ["snapshot"] else check(settings)


if __name__ == "__main__":
    sys.exit(main())
