"""`ab selftest`: prove the boundaries hold for one agent and project.

The probe (probe.sh) runs in a container built from exactly the session's spec and only reports
observations; every expectation lives here, where it can be unit-tested.
"""
import os
import secrets as _secrets
import socket
from dataclasses import dataclass, field

from ..config import HOME

PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.sh")
SECRET_NAME_WORDS = ("API_KEY", "APIKEY", "SECRET", "PASSWORD", "PASSWD", "TOKEN", "CREDENTIAL", "PRIVATE_KEY")
SECRET_VALUE_RE = r"(sk-ant-api|sk-proj-|sk-[A-Za-z0-9]{20}|ghp_|github_pat_|AKIA[0-9A-Z]{16}|xox[bap]-)"


@dataclass
class Case:
    id: str
    op: str
    args: list
    label: str
    check: object                       # obs -> (verdict, detail); verdict in PASS/FAIL/SKIP
    after: object = None                # host-side follow-up (obs) -> (verdict, detail) or None


@dataclass
class Result:
    label: str
    verdict: str
    detail: str


def expect(value):
    return lambda obs: ("PASS" if obs == value else "FAIL", obs)


def expect_not(value):
    return lambda obs: ("PASS" if obs != value else "FAIL", obs)


class Builder:
    def __init__(self):
        self.cases = []

    def add(self, op, args, label, check, after=None):
        cid = f"c{len(self.cases):03d}"
        self.cases.append(Case(cid, op, [str(a) for a in args], label, check, after))

    def script(self):
        return "".join(f"{c.id} {c.op} {' '.join(c.args)}\n" for c in self.cases if c.op != "skip")


def posture_check(obs):
    fields = dict(p.split(":", 1) for p in obs.split() if ":" in p)
    ok = (fields.get("CapEff") == "0000000000000000" and fields.get("NoNewPrivs") == "1"
          and fields.get("Seccomp") == "2")
    return ("PASS" if ok else "FAIL", obs)


def filesystem_cases(b, *, project, plan, other_roots, other_state_dirs, secret_envs, state_mounts,
                     host_protected, uid, required_tools):
    b.add("status", [], "posture: CapEff 0, NoNewPrivs 1, Seccomp 2", posture_check)

    marker = os.path.join(project.root, f".abt-selftest-{_secrets.token_hex(4)}")

    def owned(obs):
        if obs != "ok":
            return None
        try:
            st = os.stat(marker)
            verdict = ("PASS" if st.st_uid == uid else "FAIL", f"owner uid {st.st_uid}")
            os.unlink(marker)
            return verdict
        except OSError as e:
            return ("FAIL", f"marker not on host: {e}")
    b.add("write", [marker], "write inside the project root", expect("ok"), after=owned)
    b.add("write", ["/usr/abt-selftest"], "write to /usr refused", expect("fail"))
    b.add("write", ["/etc/abt-selftest"], "write to /etc refused", expect("fail"))
    b.add("write", ["/abt-selftest"], "write to / refused", expect("fail"))
    parent = os.path.dirname(project.root)
    b.add("write", [os.path.join(parent, "abt-selftest-outside")], "write next to the project refused",
          expect("fail"))

    gitdir = os.path.join(project.root, ".git")
    if os.path.isdir(gitdir):
        b.add("write", [os.path.join(gitdir, "hooks", "pre-commit")], "write to .git/hooks refused",
              expect("fail"))
        if os.path.isfile(os.path.join(gitdir, "config")):
            b.add("write", [os.path.join(gitdir, "config")], "write to .git/config refused", expect("fail"))
        b.add("write", [os.path.join(gitdir, "abt-selftest")], "write elsewhere in .git allowed", expect("ok"),
              after=lambda obs: _cleanup(os.path.join(gitdir, "abt-selftest")))

    for d in project.data_ro:
        b.add("write", [os.path.join(d, "abt-selftest")], f"write to read-only data {d} refused", expect("fail"))
        b.add("readable", [d], f"read-only data {d} readable", expect("yes"))
    for d in project.data_rw:
        m = os.path.join(d, "abt-selftest")
        b.add("write", [m], f"write to read-write data {d}", expect("ok"), after=lambda obs, m=m: _cleanup(m))

    for m in plan.mounts:
        if m.kind == "devnull":
            b.add("size", [m.dst], f"masked {os.path.relpath(m.dst, project.root)} reads empty", expect("0"))
        elif m.kind == "tmpfs":
            rel = os.path.relpath(m.dst, project.root)
            b.add("count", [m.dst], f"masked dir {rel} is empty", expect("0"))
            b.add("write", [os.path.join(m.dst, ".abt-selftest")], f"masked dir {rel} is writable", expect("ok"))
        elif m.kind == "bind" and m.src != m.dst:     # dev swap
            want = _sha(m.src)
            b.add("sha", [m.dst], f"{os.path.relpath(m.dst, project.root)} shows "
                  f"{os.path.basename(m.src)}", expect(want))

    for name, root in other_roots:
        b.add("exists", [root], f"other project {name} root not visible", expect("no"))
    for d in other_state_dirs:
        b.add("exists", [d], f"other project state {d} not visible", expect("no"))
    for d in host_protected:
        b.add("exists", [d], f"host path {d} not visible", expect("no"))
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    b.add("exists", [os.path.join(run, "podman", "podman.sock")], "real podman socket not present",
          expect("no"))
    b.add("exists", ["/run/podman/podman.sock"], "rootful podman socket not present", expect("no"))

    b.add("envnames", [], "no credential-like variables except the session's own token and keys",
          lambda obs: _env_names_check(obs, secret_envs))
    b.add("envmatch", [SECRET_VALUE_RE], "no API-key-shaped values in the environment (except the session's keys)",
          lambda obs: _env_names_check(obs, secret_envs, any_name=True))
    for var in secret_envs:
        b.add("envset", [var], f"{var} is set for the agent", expect("set"))
    for m in state_mounts:
        b.add("write", [os.path.join(m, ".abt-selftest")], f"agent state dir {m} writable", expect("ok"))
    for t in required_tools:
        b.add("which", [t], f"tool {t} present", expect_not("missing"))


def _cleanup(path):
    try:
        os.unlink(path)
    except OSError:
        pass
    return None


def _sha(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _env_names_check(obs, allowed, any_name=False):
    bad = [n for n in obs.split() if n not in allowed
           and (any_name or any(w in n.upper() for w in SECRET_NAME_WORDS))]
    return ("PASS" if not bad else "FAIL", "found: " + " ".join(bad) if bad else "none")


def _status(obs):
    return obs.split(" ", 1)[0]


def podman_cases(b, *, socket_path, compose_project, own, canary, logs=False):
    """Tier observe: only the live stack's containers are visible, readable and nothing is writable.
    `own` are the live stack's container names (seen from the host); `canary` is a container the selftest
    created in another compose project, standing in for every other project on the host."""
    if not compose_project:
        b.add("exists", [socket_path], "no podman socket in this session", expect("no"))
        return
    b.add("exists", [socket_path], "podman socket present (observe tier)", expect("yes"))
    want = ",".join(sorted(own))
    for path in ("/libpod/containers/json?all=true", "/containers/json?all=true"):
        b.add("api", ["GET", path, "names"], f"list {path.split('?')[0]} shows only {compose_project}'s containers",
              lambda obs, w=want: ("PASS" if obs == f"200 {w}".rstrip() or (not w and obs in ("200", "200 "))
                                   else "FAIL", obs))
    denied = lambda obs: ("PASS" if _status(obs) == "403" else "FAIL", obs)
    hidden = lambda obs: ("PASS" if _status(obs) in ("403", "404") else "FAIL", obs)
    if canary:
        b.add("api", ["GET", f"/libpod/containers/{canary}/json"], "inspect another project's container refused",
              hidden)
        b.add("api", ["GET", f"/libpod/containers/{canary}/logs?stdout=true"],
              "logs of another project's container refused", hidden)
        b.add("api", ["POST", f"/libpod/containers/{canary}/exec"], "exec on another project's container refused",
              denied)
        b.add("api", ["POST", f"/containers/{canary}/exec"],
              "exec on another project's container refused (Docker API)", denied)
        b.add("api", ["POST", f"/libpod/containers/{canary}/stop"], "stop another project's container refused",
              denied)
        b.add("api", ["DELETE", f"/libpod/containers/{canary}?force=true"],
              "remove another project's container refused", denied)
    b.add("api", ["POST", "/libpod/containers/create"], "create a container refused (observe tier)", denied)
    b.add("api", ["POST", "/libpod/images/pull?reference=docker.io/library/alpine:3.20"], "image pull refused",
          denied)
    b.add("api", ["GET", "/libpod/pods/json"], "pod list refused (not label-scoped)", denied)
    if own:
        mine = sorted(own)[0]
        b.add("api", ["GET", f"/containers/{mine}/json", "env"],
              f"inspect {mine} allowed (Docker API), environment redacted",
              lambda obs: ("PASS" if obs in ("200 empty", "200 none") else "FAIL", obs))
        b.add("api", ["GET", f"/libpod/containers/{mine}/json"],
              f"libpod inspect of {mine} refused (its create command holds -e values)", denied)
        b.add("api", ["GET", f"/containers/{mine}/top?ps_args=eww"], f"top on {mine} refused", denied)
        if logs:
            b.add("api", ["GET", f"/libpod/containers/{mine}/logs?stdout=true&stderr=true"],
                  f"logs of {mine} allowed (observe_logs on)",
                  lambda obs: ("PASS" if _status(obs) == "200" else "FAIL", obs))
        else:
            b.add("api", ["GET", f"/libpod/containers/{mine}/logs?stdout=true&stderr=true"],
                  f"logs of {mine} refused (observe_logs off)", denied)
        b.add("api", ["POST", f"/libpod/containers/{mine}/exec"], f"exec on {mine} refused (observe tier)", denied)
        b.add("api", ["POST", f"/libpod/containers/{mine}/stop"], f"stop {mine} refused (observe tier)", denied)


def network_none_cases(b):
    b.add("ifaces", [], "only loopback (no network in this session)",
          lambda obs: ("PASS" if obs.split() == ["lo"] else "FAIL", obs))


REFUSED = "403"


def refused(obs):
    return ("PASS" if obs == REFUSED else "FAIL", obs)


def allowed(obs):
    """Anything but a squid refusal or no answer at all; a 502/503/504 means allowed but the target is down."""
    ok = obs not in (REFUSED, "000", "noproxy", "") and obs.isdigit()
    return ("PASS" if ok else "FAIL", obs)


def blocked_direct(obs):
    """A direct attempt must fail at the network level. A refusal means a packet reached a host: a leak."""
    ok = obs in ("unreachable", "timeout", "noresolve") or obs.startswith("error:")
    return ("PASS" if ok else "FAIL", obs)


def nip(host):
    return host.replace(".", "-") + ".nip.io"


def network_cases(b, mode, *, model, lan_targets, model_domains, extra_domains=(), live=None):
    """Proxied and direct cases for one egress mode. `live(host, port)` is the host-side baseline."""
    live = live or host_tcp_live
    mhost, mport = model
    b.add("ifaces", [], "only loopback and the session network", lambda obs: (
        "PASS" if "lo" in obs.split() and len(obs.split()) == 2 else "FAIL", obs))

    # --- proxied: refused in every mode --------------------------------------------------
    b.add("get", [f"http://{mhost}:{mport}/v1/models", f"{mhost}:{mport}"],
          f"proxied local model http://{mhost}:{mport}/v1/models allowed", allowed)
    b.add("get", [f"http://{mhost}:22/", f"{mhost}:22"], f"proxied {mhost}:22 refused", refused)
    for t in lan_targets:
        b.add("get", [f"http://{t}/", t], f"proxied LAN {t} refused", refused)
        h, p = t.rsplit(":", 1)
        b.add("get", [f"http://{nip(h)}:{p}/", f"{nip(h)}:{p}"],
              f"proxied public name resolving to {h} refused", refused)
        b.add("get", [f"http://[::ffff:{h}]:{p}/", f"[::ffff:{h}]:{p}"], f"proxied IPv4-mapped {h} refused", refused)
        b.add("connect", [f"{h}:443"], f"proxied CONNECT {h}:443 refused", refused)
    for t in ("127.0.0.1:3128", "0.0.0.0:3128", "localhost:3128", "169.254.169.254:80"):
        b.add("get", [f"http://{t}/", t], f"proxied {t} refused", refused)

    # --- proxied: the model API in every mode ---------------------------------------------
    for d in model_domains:
        b.add("connect", [f"{d}:443"], f"proxied CONNECT {d}:443 allowed ({mode})", allowed)

    # --- proxied: by mode --------------------------------------------------------------------
    want = {"locked": (refused, refused), "packages": (refused, allowed), "open": (allowed, allowed)}[mode]
    b.add("connect", ["example.com:443"], f"proxied CONNECT example.com:443 {'allowed' if mode == 'open' else 'refused'} "
          f"({mode})", want[0])
    b.add("get", ["http://example.com/", "example.com"], f"proxied http://example.com/ "
          f"{'allowed' if mode == 'open' else 'refused'} ({mode})", want[0])
    b.add("connect", ["pypi.org:443"], f"proxied CONNECT pypi.org:443 {'refused' if mode == 'locked' else 'allowed'} "
          f"({mode})", want[1])
    b.add("connect", ["files.pythonhosted.org:443"], f"proxied CONNECT files.pythonhosted.org:443 "
          f"{'refused' if mode == 'locked' else 'allowed'} ({mode})", want[1])
    for d in extra_domains:
        d = d.lstrip(".")
        b.add("connect", [f"{d}:443"], f"proxied CONNECT extra domain {d}:443 "
              f"{'refused' if mode == 'locked' else 'allowed'} ({mode})", want[1])
    b.add("connect", ["[2606:4700:4700::1111]:443"], f"proxied IPv6 literal not reachable ({mode}; IPv4-only networks)",
          lambda obs: ("PASS" if obs.isdigit() and obs not in ("200", "000") else "FAIL", obs))
    b.add("connect", ["example.com:22"], f"proxied CONNECT to a non-443 port refused ({mode})", refused)
    b.add("get", ["http://example.com:8080/", "example.com:8080"], f"proxied HTTP to a non-web port refused ({mode})",
          refused)

    # --- direct, bypassing the proxy ----------------------------------------------------------
    direct = [(mhost, mport)] + [tuple(t.rsplit(":", 1)) for t in lan_targets]
    live_count = 0
    for h, p in direct:
        if live(h, p):
            live_count += 1
            b.add("direct", [h, p], f"direct {h}:{p} unreachable", blocked_direct)
        else:
            b.cases.append(Case(f"s{len(b.cases):03d}", "skip", [], f"direct {h}:{p} (not live from the host)",
                                lambda obs: ("SKIP", "target not answering from the host")))
    if live_count == 0:
        b.cases.append(Case(f"s{len(b.cases):03d}", "skip", [], "at least one live direct target",
                            lambda obs: ("FAIL", "no direct target answers from the host; the direct cases prove nothing")))
    b.add("direct", ["example.com", "80"], "direct example.com:80 unreachable", blocked_direct)
    b.add("direct", ["1.1.1.1", "443"], "direct 1.1.1.1:443 (IP literal) unreachable", blocked_direct)
    b.add("dns", ["example.com"], "direct DNS lookup of a public name fails", expect("failed"))


def score(cases, output):
    """Match probe output to cases. Returns a list of Results."""
    obs = {}
    done = False
    for line in output.splitlines():
        cid, _, rest = line.partition(" ")
        if cid == "__done__":
            done = True
        obs[cid] = rest.strip()
    results = []
    for c in cases:
        if c.op == "skip":
            verdict, detail = c.check("")
            results.append(Result(c.label, verdict, detail))
            continue
        if c.id not in obs:
            results.append(Result(c.label, "FAIL", "no answer from probe"))
            continue
        verdict, detail = c.check(obs[c.id])
        if c.after:
            follow = c.after(obs[c.id])
            if follow and verdict == "PASS":
                verdict, detail = follow
        results.append(Result(c.label, verdict, detail))
    if not done:
        results.append(Result("probe finished", "FAIL", "probe output ended early"))
    return results


def scrub(text, secrets):
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def host_tcp_live(host, port, timeout=4):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False
