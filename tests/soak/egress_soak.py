"""make egress-soak (Tim, 3 Oct): the egress flake, measured.

One session (OMP image, fake OMP home, egress packages, example.com as an extra domain). Inside it, through its
squid: SOAK_SEQ (300) sequential requests, then SOAK_WORKERS (10) parallel workers making SOAK_EACH (30) each,
round-robin over pypi.org, files.pythonhosted.org, registry.npmjs.org, github.com and example.com. Recorded:
  * per request (client side): status, total time, error;
  * per request (squid side, its access log): status, upstream address (v4 / v6 / none), total and DNS time;
  * squid's cache.log errors and its memory and CPU (sampled every 3 s).
Prints the failure rate and p50/p95/max latency per host, by phase, and squid's per host and address family.
A failure is no HTTP answer from the origin: a proxy error (5xx on CONNECT), a timeout or a connection error.
Exit status 0 only with no failures. The raw records go to /tmp/abt-soak-<run>.json.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict

from agentbox import config, session
from tests import ompfake
from tests.integration import harness

HOSTS = {"pypi.org": "/simple/six/", "files.pythonhosted.org": "/", "registry.npmjs.org": "/six",
         "github.com": "/", "example.com": "/"}
SEQ = int(os.environ.get("SOAK_SEQ", 300))
WORKERS = int(os.environ.get("SOAK_WORKERS", 10))
EACH = int(os.environ.get("SOAK_EACH", 30))
TIMEOUT = 30

CLIENT = r'''
import json, sys, threading, time, urllib.request, urllib.error
hosts = json.loads(sys.argv[1]); seq, workers, each, timeout = map(int, sys.argv[2:6])
names = sorted(hosts)
opener = urllib.request.build_opener(urllib.request.ProxyHandler())   # the session's proxy variables
lock = threading.Lock()
def one(phase, i):
    host = names[i % len(names)]
    t0 = time.time(); status, err = None, ""
    try:
        with opener.open(f"https://{host}{hosts[host]}", timeout=timeout) as r:
            status = r.status; r.read(65536)
    except urllib.error.HTTPError as e:
        status = e.code                      # an answer from the origin: the network path worked
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
    rec = {"phase": phase, "i": i, "host": host, "start": t0, "end": time.time(),
           "ms": int((time.time() - t0) * 1000), "status": status, "error": err}
    with lock:
        print("SOAK " + json.dumps(rec), flush=True)
for i in range(seq):
    one("sequential", i)
def worker(w):
    for j in range(each):
        one("parallel", w * each + j)
ts = [threading.Thread(target=worker, args=(w,)) for w in range(workers)]
[t.start() for t in ts]; [t.join() for t in ts]
print("SOAK_DONE", flush=True)
'''

LOG = re.compile(r"^(?P<ts>\d+\.\d+) (?P<client>\S+) (?P<method>\S+) (?P<dest>\S+) (?P<status>\d+) (?P<up>\S+) "
                 r"(?P<code>\S+) (?P<tr>-?\d+|-) (?P<dt>-?\d+|-)\s*$")


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def family(addr):
    return "none" if addr in ("-", "") else ("v6" if ":" in addr else "v4")


def is_failure(rec):
    return rec["status"] is None or (rec["error"] != "") or rec["status"] in (502, 503, 504)


def cgroup_dir(name):
    pid = subprocess.run(["podman", "inspect", "-f", "{{.State.Pid}}", name], capture_output=True,
                         text=True).stdout.strip()
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return "/sys/fs/cgroup" + f.read().strip().split("::", 1)[-1]
    except OSError:
        return None


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def sample_stats(name, out, stop):
    """squid's memory (memory.current, MiB) and CPU (% of one core over each interval), from its cgroup."""
    d, last = None, None
    while not stop.is_set():
        d = d or cgroup_dir(name)
        if d:
            usage = None
            try:
                with open(os.path.join(d, "cpu.stat")) as f:
                    usage = int(dict(l.split() for l in f)["usage_usec"])
            except (OSError, KeyError, ValueError):
                pass
            now = time.time()
            cpu = round(100.0 * (usage - last[1]) / ((now - last[0]) * 1e6), 1) if usage and last else None
            last = (now, usage) if usage else last
            mem = read_int(os.path.join(d, "memory.current"))
            out.append({"t": now, "cpu": cpu, "mem_mib": round(mem / 2**20, 1) if mem else None,
                        "mem_max": read_int(os.path.join(d, "memory.max")),
                        "events": open(os.path.join(d, "memory.events")).read().replace("\n", " ").strip()
                        if os.path.exists(os.path.join(d, "memory.events")) else ""})
        stop.wait(3)


def report(client, squid, cache_errors, stats):
    lines = []
    fails = 0
    lines.append(f"{'phase':<11} {'host':<23} {'n':>4} {'fail':>4} {'p50':>6} {'p95':>6} {'max':>6}  (client, ms)")
    for phase in ("sequential", "parallel"):
        for host in sorted(HOSTS):
            recs = [r for r in client if r["phase"] == phase and r["host"] == host]
            bad = [r for r in recs if is_failure(r)]
            fails += len(bad)
            ms = [r["ms"] for r in recs]
            lines.append(f"{phase:<11} {host:<23} {len(recs):>4} {len(bad):>4} {pct(ms, 50) or 0:>6} "
                         f"{pct(ms, 95) or 0:>6} {max(ms) if ms else 0:>6}")
            for r in bad[:5]:
                lines.append(f"            failed #{r['i']} after {r['ms']} ms: {r['status']} {r['error']}")
    lines.append("")
    lines.append(f"{'host':<23} {'fam':<4} {'n':>4} {'non200':>6} {'tr p50':>7} {'tr p95':>7} {'tr max':>7} "
                 f"{'dns p50':>7} {'dns max':>7}  (squid, ms)")
    by = defaultdict(list)
    for s in squid:
        by[(s["host"], s["family"])].append(s)
    for (host, fam), ss in sorted(by.items()):
        tr = [s["tr"] for s in ss if s["tr"] >= 0]
        dt = [s["dt"] for s in ss if s["dt"] >= 0]
        non200 = sum(1 for s in ss if s["status"] != 200)
        lines.append(f"{host:<23} {fam:<4} {len(ss):>4} {non200:>6} {pct(tr, 50) or 0:>7} {pct(tr, 95) or 0:>7} "
                     f"{max(tr) if tr else 0:>7} {pct(dt, 50) or 0:>7} {max(dt) if dt else 0:>7}")
    lines.append("")
    lines.append(f"squid cache.log errors/warnings after start: {len(cache_errors)}")
    seen = defaultdict(int)
    for e in cache_errors:
        seen[re.sub(r"\d+", "N", e.split("| ", 1)[-1])[:140]] += 1
    for msg, n in sorted(seen.items(), key=lambda kv: -kv[1])[:15]:
        lines.append(f"  {n:>4} x {msg}")
    cpus = [x["cpu"] for x in stats if x.get("cpu") is not None]
    mems = [x["mem_mib"] for x in stats if x.get("mem_mib") is not None]
    if stats:
        lines.append(f"squid resources ({len(stats)} samples): cpu p50 {pct(cpus, 50)}% max {max(cpus) if cpus else None}%"
                     f"; memory p50 {pct(mems, 50)} MiB max {max(mems) if mems else None} MiB "
                     f"(limit {stats[-1].get('mem_max')}); memory.events at end: {stats[-1].get('events')}")
    total = len(client)
    lines.append(f"\nTOTAL: {total} requests, {fails} failures ({100.0 * fails / total if total else 0:.2f}%)")
    return "\n".join(lines), fails


def main():
    env = harness.Env()
    omp_home = ompfake.build(os.path.join(harness.BASE, "omp"))
    with open(os.path.join(env.cfg, "agentbox.toml"), "a") as f:
        f.write(f'omp_home = "{omp_home}"\n')
    env.make_project("soak", preset="python", extra=["--egress", "packages", "--extra-domain", "example.com"],
                     secrets_files=False)
    keys = ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR")
    os.environ.update({k: env.env[k] for k in keys})
    s = config.load()
    sess = session.make_session(s, "omp", session.load_project(s, "soak"), warn=lambda m: None)
    sess.prepare()
    stats, stop = [], threading.Event()
    sampler = threading.Thread(target=sample_stats, args=(sess.proxy_name, stats, stop), daemon=True)
    started = time.time()
    p = sess.run_captured(["python3", "-c", CLIENT, json.dumps(HOSTS), str(SEQ), str(WORKERS), str(EACH),
                           str(TIMEOUT)], timeout=3600, before=sampler.start)
    stop.set()
    client = [json.loads(l[5:]) for l in (p.stdout or "").splitlines() if l.startswith("SOAK ")]
    if "SOAK_DONE" not in (p.stdout or ""):
        print(f"the client did not finish (rc {p.returncode}):\n{(p.stderr or '')[-2000:]}")
    logs = os.path.join(env.data, "logs")
    text = ""
    for fn in os.listdir(logs):
        if fn.endswith("-proxy.log"):
            with open(os.path.join(logs, fn), errors="replace") as f:
                text += f.read()
    access, _, cache = text.partition("\n--- stderr ---\n")
    squid = []
    for line in access.splitlines():
        m = LOG.match(line.strip())
        if m and m["method"] == "CONNECT":
            squid.append({"ts": float(m["ts"]), "host": m["dest"].rsplit(":", 1)[0], "status": int(m["status"]),
                          "up": m["up"], "family": family(m["up"]), "code": m["code"], "tr": int(m["tr"]) if m["tr"] != "-" else -1,
                          "dt": int(m["dt"]) if m["dt"] != "-" else 0})   # "-": answered from squid's DNS cache
    cache_errors = [l for l in cache.splitlines() if re.search(r"ERROR|WARNING|fail|refused|timed? ?out|unreach",
                                                                   l, re.I) and "HTTP requires the use of Via" not in l]
    text, fails = report(client, squid, cache_errors, stats)
    out = f"/tmp/abt-soak-{harness.RUN}.json"
    with open(out, "w") as f:
        json.dump({"started": started, "client": client, "squid": squid, "cache_errors": cache_errors,
                   "access_raw": access.splitlines(),
                   "stats": stats}, f)
    print(text)
    print(f"raw records: {out}")
    harness.cleanup()
    return 1 if fails or len(client) != SEQ + WORKERS * EACH else 0


if __name__ == "__main__":
    sys.exit(main())
