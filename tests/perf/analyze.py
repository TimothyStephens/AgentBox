"""Summarise a tests/perf/run.sh results directory as markdown: python3 tests/perf/analyze.py <dir>.

Per scenario (medians over rounds): wall time; the agentbox functions with the most cumulative time (the
phases); where podman's time goes, by podman subcommand (from ab.log's end lines) and by calling function
(from cProfile's caller table); time spent waiting on child processes versus Python's own; and the steps
timed inside the session. Writes data.json beside it for plotting."""
import glob
import json
import os
import pstats
import re
import statistics
import sys
from collections import defaultdict

END = re.compile(r" = (\d+)ms rc=(\S+) (\[.*\])$")
START = re.compile(r"^\S+ (\[.*\])$")
INS = re.compile(r"^INSESSION (\S+) (\d+) rc=(\d+)")
# Builtins a process blocks in while a child runs or a pipe fills; their tottime is waiting, not Python work.
WAITS = {"waitpid", "poll", "select", "read", "_wait", "sleep", "acquire", "communicate", "wait"}


def med(xs):
    return statistics.median(xs) if xs else 0.0


def short(fn):
    f, line, name = fn
    if f == "~":
        return name
    i = f.rfind("agentbox/")
    return (f[i:] if i >= 0 else os.path.basename(f)) + ":" + name


def podman_key(argv):
    """`podman network create ...` -> `network create`; `podman run ...` -> `run`; flags dropped."""
    words = [a for a in argv[1:4] if not a.startswith("-")]
    two = {"network", "image", "container", "pod", "volume", "system", "secret"}
    return " ".join(words[:2]) if words and words[0] in two else (words[0] if words else "?")


def ablog(path):
    """Per podman subcommand: [total ms, count]. Pairs each end line with the last open start line."""
    out = defaultdict(lambda: [0, 0])
    pending = []
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return out
    for line in lines:
        m = END.search(line)
        if m:
            key = None
            for i in range(len(pending) - 1, -1, -1):
                if pending[i][1:3] == json.loads(m.group(3)):
                    key = podman_key(pending.pop(i))
                    break
            key = key or podman_key(["podman"] + json.loads(m.group(3)))
            out[key][0] += int(m.group(1))
            out[key][1] += 1
            continue
        m = START.match(line)
        if m:
            try:
                pending.append(json.loads(m.group(1)))
            except ValueError:
                pass
    return out


def main(d):
    walls = defaultdict(list)
    rcs = defaultdict(list)
    bad = set()
    for line in open(os.path.join(d, "walls.txt")):
        name, rnd, status, wall = line.split()
        status = status.split("=")[1]
        rcs[name].append(status)
        if status != "ok":       # a refused or failed run is reported, never averaged in
            bad.add((name, rnd))
            continue
        walls[name].append(float(wall.split("=")[1]))
    data = {}
    print(f"# ab profile: {os.path.basename(os.path.normpath(d))}\n")
    try:
        print("```\n" + open(os.path.join(d, "host.txt")).read().strip() + "\n```\n")
    except OSError:
        pass
    print("## Wall time per command (s)\n")
    print("| scenario | ok runs | min | median | max | first ok run | not ok |\n|---|---|---|---|---|---|---|")
    for name in rcs:
        ws = walls.get(name) or [0.0]
        notok = [f"{i + 1}:{st}" for i, st in enumerate(rcs[name]) if st != "ok"]
        print(f"| {name} | {len(walls.get(name, []))} | {min(ws):.1f} | {med(ws):.1f} | {max(ws):.1f} | {ws[0]:.1f} | "
              f"{', '.join(notok) or '-'} |")
    for name in walls:
        cum = defaultdict(list)
        tot = defaultdict(list)
        callers = defaultdict(list)
        waits, totals = [], []
        pod = defaultdict(lambda: [[], []])
        ins = defaultdict(list)
        profs = [p for p in sorted(glob.glob(os.path.join(d, f"{name}.*.prof")))
                 if (name, p.rsplit(".", 2)[1]) not in bad]
        for p in profs:
            st = pstats.Stats(p).stats
            total = max(ct for (_cc, _nc, _tt, ct, _c) in st.values())
            totals.append(total)
            waits.append(sum(tt for fn, (_cc, _nc, tt, _ct, _c) in st.items()
                             if fn[0] == "~" and any(w in fn[2] for w in WAITS)))
            for fn, (_cc, _nc, tt, ct, cl) in st.items():
                if "agentbox/" in fn[0] and "/tests/" not in fn[0]:
                    cum[short(fn)].append(ct)
                    tot[short(fn)].append(tt)
                if fn[0].endswith("agentbox/podman.py") and fn[2] in ("run", "interactive"):
                    for caller, (_a, _b, _t, cct) in cl.items():
                        callers[short(caller)].append(cct)
            base = p[:-len(".prof")]
            for k, (ms, n) in ablog(base + ".ablog").items():
                pod[k][0].append(ms / 1000)
                pod[k][1].append(n)
            try:
                for line in open(base + ".out", errors="replace"):
                    m = INS.match(line.strip())
                    if m:
                        ins[m.group(1)].append(int(m.group(2)) / 1000)
            except OSError:
                pass
        if not profs:
            continue
        runs = len(profs)
        print(f"\n## {name}\n")
        print(f"Profiled total {med(totals):.1f} s (median of {runs}); blocked waiting on child processes or "
              f"pipes {med(waits):.1f} s; the rest ({med(totals) - med(waits):.1f} s) is Python and the "
              "kernel work it asks for directly (file walks, hashing, copies).\n")
        top = sorted(cum.items(), key=lambda kv: -med(kv[1]))[:25]
        print("Cumulative time by agentbox function (median s; includes what it waits on):\n")
        print("| function | cumulative | own |\n|---|---|---|")
        for k, v in top:
            print(f"| `{k}` | {med(v):.2f} | {med(tot[k]):.2f} |")
        own = sorted(tot.items(), key=lambda kv: -med(kv[1]))[:10]
        print("\nOwn (Python) time, top 10 (median s):\n")
        print("| function | own |\n|---|---|")
        for k, v in own:
            print(f"| `{k}` | {med(v):.2f} |")
        if pod:
            print("\npodman, by subcommand (from ab.log; median per command run):\n")
            print("| podman | calls | total s |\n|---|---|---|")
            for k, (secs, ns) in sorted(pod.items(), key=lambda kv: -med(kv[1][0])):
                print(f"| {k} | {med(ns):.0f} | {med(secs):.2f} |")
        if callers:
            print("\npodman, by the agentbox function that called it (median s):\n")
            print("| caller | podman time |\n|---|---|")
            for k, v in sorted(callers.items(), key=lambda kv: -med(kv[1]))[:15]:
                print(f"| `{k}` | {med(v):.2f} |")
        if ins:
            print("\nInside the session (median s, all runs):\n")
            print("| step | median | min | max | n |\n|---|---|---|---|---|")
            for k, v in ins.items():
                print(f"| {k} | {med(v):.2f} | {min(v):.2f} | {max(v):.2f} | {len(v)} |")
        data[name] = {"walls": walls[name], "total": med(totals), "wait": med(waits),
                      "cum": {k: med(v) for k, v in top}, "own": {k: med(v) for k, v in own},
                      "podman": {k: [med(s), med(n)] for k, (s, n) in pod.items()},
                      "callers": {k: med(v) for k, v in callers.items()},
                      "insession": {k: v for k, v in ins.items()}}
    with open(os.path.join(d, "data.json"), "w") as f:
        json.dump(data, f, indent=1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
