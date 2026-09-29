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
        return "".join(f"{c.id} {c.op} {' '.join(c.args)}\n" for c in self.cases)


def posture_check(obs):
    fields = dict(p.split(":", 1) for p in obs.split() if ":" in p)
    ok = (fields.get("CapEff") == "0000000000000000" and fields.get("NoNewPrivs") == "1"
          and fields.get("Seccomp") == "2")
    return ("PASS" if ok else "FAIL", obs)


def filesystem_cases(b, *, project, plan, other_roots, other_state_dirs, token_env, state_mount,
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

    b.add("envnames", [], "no credential-like variables except the session token",
          lambda obs: _env_names_check(obs, token_env))
    b.add("envmatch", [SECRET_VALUE_RE], "no API-key-shaped values in the environment",
          lambda obs: ("PASS" if not obs.strip() else "FAIL", obs or "none"))
    if token_env:
        b.add("envset", [token_env], f"{token_env} is set for the agent", expect("set"))
    if state_mount:
        b.add("write", [os.path.join(state_mount, ".abt-selftest")], "agent state dir writable", expect("ok"))
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


def _env_names_check(obs, token_env):
    bad = [n for n in obs.split() if n != token_env and any(w in n.upper() for w in SECRET_NAME_WORDS)]
    return ("PASS" if not bad else "FAIL", "found: " + " ".join(bad) if bad else "none")


def network_none_cases(b):
    b.add("ifaces", [], "only loopback (no network in this session)",
          lambda obs: ("PASS" if obs.split() == ["lo"] else "FAIL", obs))


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
