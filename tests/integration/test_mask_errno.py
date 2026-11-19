"""The masked-paths spec's in-session errno probe (docs/agentbox-masked-paths.md §5.1, the §4 contract, the
§5.3 host sentinel), run where §3.2 says it must run: in the session itself, not in a container it starts
(there the covers are absent and only file ownership applies, so a probe would see EACCES for the wrong
reason; the container side is test_step6_covers).

The project masks `./data` (a non-empty directory on the host), `./later` (a path that does not exist before the
session), the bare name `state.json` (two files) and `cfg/.env` (a swap target: the mask must win), next to the
standard secret masks from `make_project` (`.env` swapped for `.env.dev`, `sub/.env`, `tls/server.key`,
`secrets/`). From inside the session, per masked path:

  * a masked directory exists, lists empty (dotfiles included), hides the real file and any `..` route to it;
    creating, mkdir and chmod in or on it fail with EROFS (read-only tmpfs), rmdir and rename of it with EBUSY
    (it is a mount point; §3.2's measured table);
  * a masked file reads zero bytes; opening it for writing and chmod fail with EROFS (read-only bind of the
    empty mask file), unlink and rename with EBUSY (a mount point);
  * a sibling (`dev/data`) is untouched and writable (§4.6);
  * the root `.env` shows `.env.dev` (the swap, §3.1), while the masked `cfg/.env` reads empty (§4: a path
    that is both a swap target and a mask is a mask).
And on the host after the session (§5.3, §4.7): the sentinel under `data/` is still there with the same bytes
and mtime, and nothing a probe tried to create reached the host (`later/`, if left as the mount point, is empty).
"""
import errno
import hashlib
import os
import shutil
import unittest

from . import harness

ENV = None
ROOT = None
NAME = "maskp"
IMAGE = "localhost/agentbox-claude:latest"
SENTINEL = f"live-data-sentinel-{harness.RUN}\n"
STATE = '{"state": "real"}\n'
BEFORE = {}

PROBE = r"""
set +e
cd @ROOT@
python3 - <<'PY'
import errno, os, pathlib

def res(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))

def out(k, v):
    print(f"{k}={v}")

def touch(p):
    pathlib.Path(p).touch()

def append(p):
    with open(p, "a") as f:
        f.write("x")

def read(p):
    with open(p, "rb") as f:
        return f.read()

for d in ("data", "later"):
    K = d.upper()
    out(f"{K}_ISDIR", os.path.isdir(d))
    out(f"{K}_LS", ",".join(sorted(os.listdir(d))) if os.path.isdir(d) else "<none>")
    out(f"{K}_TOUCH", res(lambda: touch(f"{d}/probe")))
    out(f"{K}_MKDIR", res(lambda: os.mkdir(f"{d}/sub")))
    out(f"{K}_CHMOD", res(lambda: os.chmod(d, 0o777)))
    out(f"{K}_RMDIR", res(lambda: os.rmdir(d)))
    out(f"{K}_RENAME", res(lambda: os.rename(d, f"{d}.moved")))
out("DATA_LIVE", os.path.lexists("data/live.db"))
out("DATA_DOT", os.path.lexists("data/.hidden"))
out("DATA_TRAVERSE", os.path.lexists("data/../data/live.db"))

for k, p in (("STATE", "state.json"), ("APPSTATE", "app/state.json"), ("CFGENV", "cfg/.env"),
             ("SUBENV", "sub/.env"), ("KEY", "tls/server.key")):
    out(f"{k}_EXISTS", os.path.lexists(p))
    out(f"{k}_BYTES", len(read(p)) if os.path.isfile(p) else -1)
    out(f"{k}_WRITE", res(lambda: append(p)))
    out(f"{k}_CHMOD", res(lambda: os.chmod(p, 0o666)))
    out(f"{k}_UNLINK", res(lambda: os.unlink(p)))
    out(f"{k}_RENAME", res(lambda: os.rename(p, p + ".moved")))

out("ROOTENV", read(".env").decode().strip())
out("SECRETS_LS", ",".join(sorted(os.listdir("secrets"))))
out("SECRETS_WRITE", res(lambda: touch("secrets/probe")))     # informational: secrets/ is a writable tmpfs

os.makedirs("dev/data", exist_ok=True)
out("SIBLING_TOUCH", res(lambda: touch("dev/data/probe")))
out("SIBLING_SEEN", os.path.exists("dev/data/probe"))
out("SIBLING_UNLINK", res(lambda: os.unlink("dev/data/probe")))
PY
"""


def _digest(path):
    if not os.path.lexists(path):
        return None                     # a probe removed it: the mask failed, reported as "changed on the host"
    st = os.stat(path)
    return hashlib.sha256(harness.read_file(path, "rb")).hexdigest(), st.st_mtime_ns


def setUpModule():
    global ENV, ROOT
    if shutil.which("podman") is None:
        raise unittest.SkipTest("podman not on PATH: an itest needs the host's real podman")
    if harness.podman("image", "exists", IMAGE).returncode != 0:
        raise unittest.SkipTest(f"{IMAGE} missing: run make claude-image")
    ENV = harness.Env()
    ROOT = ENV.make_project(NAME, preset="python", extra=[
        "--mask", "./data", "--mask", "./later", "--mask", "state.json", "--mask", "cfg/.env"])
    w = harness._write
    w(os.path.join(ROOT, "data", "live.db"), SENTINEL)
    w(os.path.join(ROOT, "data", ".hidden"), "dotfile under the mask\n")
    w(os.path.join(ROOT, "state.json"), STATE)
    w(os.path.join(ROOT, "app", "state.json"), STATE)
    w(os.path.join(ROOT, "cfg", ".env"), "CFG=real-secret\n")
    w(os.path.join(ROOT, "cfg", ".env.dev"), "CFG=dev-value\n")
    for rel in ("data/live.db", "data/.hidden", "state.json", "app/state.json", "cfg/.env", "sub/.env",
                "tls/server.key", "secrets/db-password.txt"):
        BEFORE[rel] = _digest(os.path.join(ROOT, rel))
    print(f"\n[itest run {harness.RUN}: in-session mask errno probe, project {ROOT}]")


def tearDownModule():
    harness.cleanup()


class MaskErrnoInSession(unittest.TestCase):
    maxDiff = None

    def test_masked_paths_in_the_session(self):
        self.assertFalse(os.path.lexists(os.path.join(ROOT, "later")), "later/ must not exist before the session")
        p = ENV.ab("session", "start", "claude", NAME, "--shell", "--no-tty", "--", "-c",
                   PROBE.replace("@ROOT@", ROOT), timeout=600)
        print(f"\n{p.stdout[-3000:]}\n--stderr--\n{p.stderr[-1000:]}")
        self.assertEqual(p.returncode, 0, p.stderr[-1000:])
        out = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in p.stdout.splitlines() if "=" in l}

        want = {}
        # Masked directories: a cover, not a deletion (§4.1); empty, dotfiles included (§4.2); read-only with
        # EROFS (§3.2, §4.3: a read-only tmpfs); EBUSY for rmdir, and rename, of a mount point (§3.2).
        for k in ("DATA", "LATER"):
            want.update({f"{k}_ISDIR": "True", f"{k}_LS": "", f"{k}_TOUCH": "EROFS", f"{k}_MKDIR": "EROFS",
                         f"{k}_CHMOD": "EROFS", f"{k}_RMDIR": "EBUSY", f"{k}_RENAME": "EBUSY"})
        # The real contents are hidden, including by a `..` route (§4.4, §5.1).
        want.update({"DATA_LIVE": "False", "DATA_DOT": "False", "DATA_TRAVERSE": "False"})
        # Masked files -- the project's own (bare name, both places; a path that is also a swap target) and
        # the secret masks: present, zero bytes, read-only (EROFS), and a mount point (EBUSY).
        for k in ("STATE", "APPSTATE", "CFGENV", "SUBENV", "KEY"):
            want.update({f"{k}_EXISTS": "True", f"{k}_BYTES": "0", f"{k}_WRITE": "EROFS",
                         f"{k}_CHMOD": "EROFS", f"{k}_UNLINK": "EBUSY", f"{k}_RENAME": "EBUSY"})
        # The root .env, with no project mask on it, is still the .env.dev swap (§3.1); secrets/ lists empty.
        want.update({"ROOTENV": "GREETING=dev-value", "SECRETS_LS": ""})
        # A sibling of the mask is untouched (§4.6).
        want.update({"SIBLING_TOUCH": "ok", "SIBLING_SEEN": "True", "SIBLING_UNLINK": "ok"})
        for k, v in want.items():
            with self.subTest(probe=k):
                self.assertEqual(out.get(k), v, f"{k}: want {v}, got {out.get(k)}")

        # §5.3 / §4.7: the mask did not pass writes through or clobber the host's files.
        for rel, before in BEFORE.items():
            with self.subTest(host=rel):
                self.assertEqual(_digest(os.path.join(ROOT, rel)), before, f"{rel} changed on the host")
        self.assertEqual(sorted(os.listdir(os.path.join(ROOT, "data"))), [".hidden", "live.db"])
        self.assertEqual(sorted(os.listdir(os.path.join(ROOT, "secrets"))), ["db-password.txt"])
        if os.path.lexists(os.path.join(ROOT, "later")):          # the mount point agentbox created
            self.assertEqual(os.listdir(os.path.join(ROOT, "later")), [], "a probe write reached later/")
        for rel in ("data.moved", "later.moved", "state.json.moved", "app/state.json.moved", "cfg/.env.moved",
                    "sub/.env.moved", "tls/server.key.moved", "dev/data/probe"):
            with self.subTest(leftover=rel):
                self.assertFalse(os.path.lexists(os.path.join(ROOT, rel)))

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])
