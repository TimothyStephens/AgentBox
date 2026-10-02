"""The throwaway trial's host-side checks (docs/trial.md). Run by Tim, not by the builder.

    python3 -m tests.trial.check snapshot   # make trial-snapshot: just before the session starts
    python3 -m tests.trial.check check      # make trial-check: after the session has ended

The snapshot records the time and, for every container agentbox did not create (the live stacks), its ID,
start time and restart count. The check then reports:
  * live stacks unchanged: the same containers, not restarted, not recreated;
  * nothing outside the trial project changed under your home since the snapshot (an mtime scan; file
    contents are never read). ab's own state and logs, podman's storage and a few caches are excluded, and
    the exclusions are printed;
  * no session container, pod, volume, network, podman service or gate of the trial project remains;
  * no gate-created volume over the trial root (a grant volume) remains.
Exit status 0 only when every check passes.
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
LIMIT = 200                          # changed paths printed at most


def podman(*args):
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=120)


def snapshot_path(settings):
    return os.path.join(settings.data_dir, "trial", f"{PROJECT}-snapshot.json")


def live_containers():
    """{id: {name, started, restarts}} for every container agentbox did not create (no agentbox.session label).
    Read-only: `podman ps` and `podman inspect`."""
    p = podman("ps", "-a", "--format", "json")
    out = {}
    for c in json.loads(p.stdout or "[]") or []:
        labels = c.get("Labels") or {}
        if "agentbox.session" in labels:
            continue
        cid = c.get("Id")
        doc = json.loads(podman("inspect", "--type", "container", cid).stdout or "[{}]")[0]
        state = doc.get("State") or {}
        out[cid] = {"name": (c.get("Names") or ["?"])[0], "started": state.get("StartedAt"),
                    "restarts": doc.get("RestartCount"), "status": state.get("Status")}
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
    doc = {"time": time.time(), "live": live_containers(), "project": PROJECT, "root": ROOT}
    with open(path, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"snapshot: {len(doc['live'])} live container(s) recorded in {path} at "
          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(doc['time']))}")
    print("now start the session (docs/trial.md, step b)")
    return 0


def check(settings):
    try:
        with open(snapshot_path(settings)) as f:
            snap = json.load(f)
    except OSError:
        print(f"FAIL  no snapshot at {snapshot_path(settings)}: run make trial-snapshot before the session")
        return 1
    fails = 0

    def line(ok, label, details=()):
        nonlocal fails
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        for d in list(details)[:LIMIT]:
            print(f"        {d}")
        if len(details) > LIMIT:
            print(f"        ... and {len(details) - LIMIT} more")

    problems = compare_live(snap["live"], live_containers())
    line(not problems, f"live stacks unchanged ({len(snap['live'])} containers: same IDs, start times, restart counts)",
         problems)
    skip = set(exclusions(settings))
    changed = changed_since(snap["time"], skip)
    line(not changed, f"nothing under {HOME} changed outside {ROOT} since the snapshot (mtime scan)", changed)
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
