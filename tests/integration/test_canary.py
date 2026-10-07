"""Permanent containment regression test (the canary test).

A dummy project per `ab init` template (pod, python) is given DISTINCT real canary strings — one in the
real `.env`, one in the project's live data — and a different fake value in `.env.dev`. Then, from inside
a session (for the pod/dev-tier project, from containers the agent spawns through the gate; for the
python/tier-none project, from the agent container itself), every known read vector is tried and the REAL
canaries must never surface: not in the agent container, a spawned container, build output, or any file the
session can write. The fake `.env.dev` value is allowed to appear — that is what the agent is meant to see.

Vectors (pod, dev tier): cat/read `.env` and the live data; bind the repo root; symlink-swap a sub-path at
start; `COPY` `.env`/`secrets/` in a build; bind-backed volume driver options; `podman cp`; and mounting the
live stack's named volume (not the session's). The step 6 covers checks (secrets mask, dev-swap, COPY) are
folded in here with canary strings rather than duplicated. Vectors (python, tier none): the agent has no
podman socket, so it can only read the filesystem — the masked `.env` shows the fake value and the real data
dir is outside the grant, unreachable.

Two vectors from the brief are covered by UNIT tests rather than here, because they cannot be set up in
this itest: a genuinely foreign-uid directory needs `podman unshare` (blocked by the repo guard) or root —
the gate's refusal to bind a foreign-uid source is `test_review_step5.test_a_bind_of_a_foreign_uid_dir_is_refused`;
and `mask_skip` is a start-time grantcheck property (a secret under it still blocks the session), not a
runtime read path — `test_grantcheck.test_mask_skip_is_not_an_exemption`. The live stack's own data dir is
represented here by a named volume, which on this host is the real "a container wrote it, you cannot read it"
case (rootless maps container-root to the user, so a bind-created dir is owned by the user, not a subuid).

Score: the real canary is never readable; the fake `.env.dev` value may appear. Runs under `make itest`.
"""
import os
import subprocess
import unittest

from agentbox import config, session

from . import harness

IMG = "docker.io/library/busybox:1.36"
REAL_ENV = f"CANARY-{harness.RUN}-REALENV-SECRET"       # in the real .env; must never surface
REAL_DATA = f"CANARY-{harness.RUN}-REALDATA-LIVE"       # in the live data; must never surface
FAKE_DEV = f"CANARY-{harness.RUN}-FAKEDEV-OK"           # in .env.dev; allowed to appear
LIVEVOL = f"abt-{harness.RUN}-canpod-live"              # a live-stack named volume the session never gets

ENV = POD = PY = None
PODROOT = PYROOT = PYDATA = None


def w(path, text):
    harness._write(path, text)


def setUpModule():
    global ENV, POD, PY, PODROOT, PYROOT, PYDATA
    for img in ("localhost/agentbox-omp:latest", IMG):
        if harness.podman("image", "exists", img).returncode != 0:
            raise unittest.SkipTest(f"{img} not present")
    ENV = harness.Env()
    repos = os.path.join(harness.BASE, "repos")
    os.makedirs(repos, exist_ok=True)
    ENV.env["AB_REPOS_DIR"] = repos
    for k in ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR"):
        os.environ[k] = ENV.env[k]
    os.environ["AB_REPOS_DIR"] = repos

    ENV.ab("init", "pod", "--name", "canpod", check=True)
    ENV.ab("init", "python", "--name", "canpy", check=True)
    PODROOT = os.path.join(repos, "canpod")
    PYROOT = os.path.join(repos, "canpy")

    # --- pod project: real canary in .env (masked) and in the live data (named volume, secrets/, pgdata) ---
    w(os.path.join(PODROOT, ".env"), f"GREETING={REAL_ENV}\n")
    w(os.path.join(PODROOT, ".env.dev"), f"GREETING={FAKE_DEV}\n")
    w(os.path.join(PODROOT, "secrets", "livedata.txt"), REAL_DATA + "\n")     # masked dir
    w(os.path.join(PODROOT, "Containerfile"), f"FROM {IMG}\n")
    # the live stack's data volume, seeded with the canary; the session is never granted it
    harness.podman("volume", "create", "--label", harness.LABEL, LIVEVOL)
    harness.podman("run", "--rm", "-v", f"{LIVEVOL}:/d", IMG, "sh", "-c", f"echo {REAL_DATA} > /d/canary")

    # --- python project: real canary in .env (masked) and in a data dir OUTSIDE the grant (DATA_DIR) ---
    PYDATA = os.path.join(harness.BASE, "canpy-live")
    w(os.path.join(PYDATA, "canary.txt"), REAL_DATA + "\n")
    w(os.path.join(PYROOT, ".env"), f"GREETING={REAL_ENV}\nDATA_DIR={PYDATA}\n")
    w(os.path.join(PYROOT, ".env.dev"), f"GREETING={FAKE_DEV}\nDATA_DIR=./dev/data\n")
    w(os.path.join(PYROOT, "secrets", "livedata.txt"), REAL_DATA + "\n")

    s = config.load()
    POD = session.make_session(s, "omp", session.load_project(s, "canpod"), warn=lambda m: None)
    POD.prepare()
    PY = session.make_session(s, "omp", session.load_project(s, "canpy"), warn=lambda m: None)
    PY.prepare()
    print(f"\n[itest run {harness.RUN}: canary, sessions {POD.name} (pod) + {PY.name} (python)]")


def tearDownModule():
    try:
        for sess in (POD, PY):
            if sess is not None:
                session.teardown(sess.settings, sess.podman, sess.name, warn=lambda m: None)
    finally:
        for img in harness.podman("images", "-q", "--filter", f"label={harness.LABEL}").stdout.split():
            harness.podman("rmi", "-f", img)
        harness.cleanup()


POD_SCRIPT = f"""
cd {{root}}
try() {{ name=$1; shift; out=$("$@" 2>&1); echo "$name=$? $(echo "$out" | tr '\\n' ' ' | tail -c 220)"; }}
R="podman run --rm"
# --- read the masked .env: the agent sees the fake dev value, never the real one ---
try ENVFILE     cat {{root}}/.env
try ENVBIND     $R -v {{root}}/.env:/e {IMG} cat /e
# --- the live data: secrets/ mask, the foreign-uid pgdata dir, and the live named volume ---
try SECRETSBIND $R -v {{root}}/secrets:/s {IMG} cat /s/livedata.txt
echo "SECRETS_LS=$(ls -A {{root}}/secrets | tr '\\n' ',')"
try LIVEVOL     $R -v {LIVEVOL}:/d {IMG} cat /d/canary
# --- bind the repo root (reaches the covers) ---
try ROOTBIND    $R -v {{root}}:/src {IMG} cat /src/.env /src/secrets/livedata.txt
# --- symlink-swap a sub-path at start: create with app bound, swap app -> secrets, then start ---
try CREATE      podman create --name {{swapname}} -v {{root}}/app:/a {IMG} cat /a/livedata.txt
mv {{root}}/app {{root}}/app.real && ln -s secrets {{root}}/app
try SWAPSTART   podman start -a {{swapname}}
echo "SWAPLOGS=$(podman logs {{swapname}} 2>&1 | tr '\\n' ' ')"
rm {{root}}/app && mv {{root}}/app.real {{root}}/app
# --- a build that COPYs the masked paths and the data dir: all arrive empty/absent ---
printf 'FROM {IMG}\\nCOPY .env /e\\nCOPY secrets/ /s/\\nRUN echo "COPYBYTES=$(cat /e /s/* 2>/dev/null | wc -c)"\\n' > Containerfile.copy
podman build --no-cache -t abt-{{run}}-canary-copy -f Containerfile.copy . 2>&1 | grep -oE 'COPYBYTES=[0-9]+' | head -1
# --- bind-backed volume driver options, straight at the gate socket ---
python3 - <<'PY'
import json, socket, http.client
class C(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX); self.sock.connect("/run/agentbox/podman.sock")
c = C("d")
c.request("POST", "/v5.0.0/libpod/volumes/create",
          body=json.dumps({{"Name": "abt-{{run}}-drv", "Options": {{"type": "none", "o": "bind", "device": "{{root}}"}}}}),
          headers={{"Content-Type": "application/json"}})
r = c.getresponse(); print("VOLDRV=%d %s" % (r.status, r.read()[:120].decode(errors="replace")))
PY
# --- podman cp out of a container that mounts the live volume (which it cannot create) ---
try CPVOL       sh -c 'podman create --name {{cpname}} -v {LIVEVOL}:/d {IMG} true && podman cp {{cpname}}:/d/canary /tmp/leak && cat /tmp/leak'
podman rm -f -t 0 {{cpname}} >/dev/null 2>&1 || true
"""


class Canary(unittest.TestCase):
    maxDiff = None

    def _assert_no_real_canary(self, out, where):
        self.assertNotIn(REAL_ENV, out, f"{where}: the real .env canary leaked")
        self.assertNotIn(REAL_DATA, out, f"{where}: the real live-data canary leaked")

    def test_the_real_canary_never_surfaces(self):
        swapname = f"abt-{harness.RUN}-canary-swap"
        script = (POD_SCRIPT.replace("{root}", PODROOT).replace("{swapname}", swapname)
                  .replace("{cpname}", f"abt-{harness.RUN}-canary-cp").replace("{run}", harness.RUN))
        p = POD.run_captured(["bash", "-c", script], timeout=2400)
        out = p.stdout
        print(f"\n--- pod ---\n{out[-4000:]}\n{p.stderr[-600:]}")

        # the headline guarantee: neither real canary anywhere in the pod session's output
        self._assert_no_real_canary(out, "pod session")
        # ...and it is not because nothing ran: the fake dev value IS what the agent sees for .env
        self.assertIn(FAKE_DEV, out, "the fake .env.dev value should be visible to the agent")

        lines = {l.split("=", 1)[0]: l for l in out.splitlines() if "=" in l}
        # .env read directly in the agent container is the masked dev-swap: the fake value, never the real
        self.assertIn(FAKE_DEV, lines.get("ENVFILE", ""), lines.get("ENVFILE"))
        # every bind/mount vector into a spawned container is refused (non-zero); binding .env itself reaches
        # the cover and is refused too (the fake is only ever seen by the agent's own read, above)
        for key in ("ENVBIND", "SECRETSBIND", "LIVEVOL", "ROOTBIND", "SWAPSTART", "CPVOL"):
            self.assertIn(key, lines, key)
            self.assertNotRegex(lines[key], rf"^{key}=0 ", f"{key} should be refused: {lines[key]}")
        self.assertRegex(lines.get("CREATE", ""), r"^CREATE=0 ", "a plain app bind is allowed to create")
        self.assertIn("before it ran", lines.get("SWAPSTART", ""), "swap refused at start, before the entrypoint")
        self.assertRegex(lines.get("SECRETS_LS", "x=y"), r"^SECRETS_LS=\s*$", "secrets/ lists empty (masked)")
        # the build COPY of .env/secrets reached the build without either real canary: .env is a dev-swap,
        # so COPY .env yields the FAKE value (safe); secrets/ is an empty tmpfs. The headline check above
        # already proved no real canary is in `out`, build output included.
        self.assertRegex(out, r"COPYBYTES=\d+", "the COPY build ran")   # succeeds, but with no real content
        self.assertNotIn(f"COPYBYTES={len(REAL_ENV)}", out)             # not the real .env length, either
        # the bind-backed volume driver options were refused at the gate
        self.assertRegex(out, r"VOLDRV=(4\d\d|5\d\d)", "a bind-backed volume (driver options) is refused")

        # --- the python/tier-none project: the agent can only read the filesystem ---
        py_script = f"""
cd {PYROOT}
echo "PY_ENV=$(cat .env 2>&1 | tr '\\n' ' ')"
echo "PY_EXTERNAL=$(cat {PYDATA}/canary.txt 2>&1 | tr '\\n' ' ')"
echo "PY_NOSOCK=$(podman ps 2>&1 | tr '\\n' ' ' | tail -c 80)"
"""
        q = PY.run_captured(["bash", "-c", py_script], timeout=600)
        pyout = q.stdout
        print(f"\n--- python ---\n{pyout}\n{q.stderr[-400:]}")
        self._assert_no_real_canary(pyout, "python session")
        self.assertIn(FAKE_DEV, pyout, "python: the masked .env shows the fake dev value")
        pl = {l.split("=", 1)[0]: l for l in pyout.splitlines() if "=" in l}
        self.assertIn(FAKE_DEV, pl.get("PY_ENV", ""), "python: .env is the fake dev value")
        self.assertNotRegex(pl.get("PY_EXTERNAL", "PY_EXTERNAL=x"), REAL_DATA,
                            "python: the real data dir is outside the grant, unreachable")

        # nothing the session could write holds a real canary: scan the whole data dir (backups, logs, state)
        hits = subprocess.run(["grep", "-rl", "-e", REAL_ENV, "-e", REAL_DATA, ENV.data],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(hits, "", f"a real canary was written somewhere under the data dir:\n{hits}")

    def test_z_nothing_left(self):
        for sess in (POD, PY):
            session.teardown(sess.settings, sess.podman, sess.name, warn=lambda m: None)
        self.assertEqual(harness.leftovers(), [LIVEVOL], "only the seeded live volume remains (cleaned by label)")


if __name__ == "__main__":
    unittest.main()
