"""Critical (5 Oct, life-updates spec 6.2): the agent's in-container masks and read-only covers are absent in
a container it starts through the gate, so a `podman run -v $PWD:/src` would reach the real masked data. The
gate must refuse a spawned container's bind that reaches a covered path. This is the spec's 5.2 bypass test,
run from a gate-spawned container (its 5.1 in-container test is not sufficient on its own), plus .git/config.

Covered paths used here are the ones that exist for any project: the `secrets/` mask dir (seeded with a
sentinel), `.env` (masked when there is no .env.dev), and `.git/hooks` / `.git/config`. `app/` is a sibling
that is not covered and must stay bindable (the dev stack legitimately binds sub-directories).
"""
import os
import unittest

from agentbox import config, session

from . import harness

ENV = None
SESS = None
ROOT = None
IMG = "docker.io/library/busybox:1.36"


def setUpModule():
    global ENV, SESS, ROOT
    for img in ("localhost/agentbox-omp:latest", IMG):
        if harness.podman("image", "exists", img).returncode != 0:
            raise unittest.SkipTest(f"{img} not present")
    ENV = harness.Env()
    from tests import ompfake
    omp_home = ompfake.build(os.path.join(harness.BASE, "omp"))
    with open(os.path.join(ENV.cfg, "agentbox.toml"), "a") as f:
        f.write(f'omp_home = "{omp_home}"\n')
    ROOT = ENV.make_project("cov", preset="python", extra=["--podman", "dev"], secrets_files=False)
    harness._write(os.path.join(ROOT, "secrets", "db-password.txt"), "SENTINEL-SECRET\n")
    harness._write(os.path.join(ROOT, ".env"), "SENTINEL-ENV\n")       # no .env.dev: a real mask
    harness._write(os.path.join(ROOT, "app", "main.py"), "print('ok')\n")
    keys = ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR")
    SESS_ENV = {k: ENV.env[k] for k in keys}
    os.environ.update(SESS_ENV)
    s = config.load()
    SESS = session.make_session(s, "omp", session.load_project(s, "cov"), warn=lambda m: None)
    SESS.prepare()
    print(f"\n[itest run {harness.RUN}: step 6 covers, session {SESS.name}]")


def tearDownModule():
    try:
        if SESS is not None:
            session.teardown(SESS.settings, SESS.podman, SESS.name, warn=lambda m: None)
    finally:
        harness.cleanup()


# Each probe starts a container through the gate (podman talks to /run/agentbox/podman.sock). A refused bind
# must stop the container being created and never expose the sentinel.
SCRIPT = f"""
cd {{root}}
try() {{ name=$1; shift; out=$("$@" 2>&1); echo "$name=$? $(echo "$out" | tr '\\n' ' ' | tail -c 180)"; }}
try ROOT        podman run --rm -v {{root}}:/src {IMG} cat /src/secrets/db-password.txt
try SECRETS     podman run --rm -v {{root}}/secrets:/s {IMG} cat /s/db-password.txt
try ENV         podman run --rm -v {{root}}/.env:/e {IMG} cat /e
try GITDIR      podman run --rm -v {{root}}/.git:/g {IMG} cat /g/config
try GITCONFIG   podman run --rm -v {{root}}/.git/config:/c {IMG} true
try GITHOOKS    podman run --rm -v {{root}}/.git/hooks:/h {IMG} true
try REL         podman run --rm -v ./secrets:/s {IMG} cat /s/db-password.txt
try DOTDOT      podman run --rm -v {{root}}/app/../secrets:/s {IMG} true
try APP         podman run --rm -v {{root}}/app:/a {IMG} cat /a/main.py
try COMPAT      curl -s -o /dev/null -w 'COMPAT=%{{http_code}}\\n' --unix-socket /run/agentbox/podman.sock \
  -X POST -H 'Content-Type: application/json' \
  -d '{{"Image":"{IMG}","HostConfig":{{"Binds":["{{root}}/secrets:/s"]}}}}' \
  http://d/v5.0.0/containers/create
# .git/config is read-only in the agent container too, and a spawned container cannot write it
echo "AGENT_APPEND=$(printf x >> {{root}}/.git/config 2>&1 && echo WROTE || echo refused)"
try HOOKSPATH   podman run --rm -v {{root}}/app:/a {IMG} sh -c 'echo "[core]" >> /a/../.git/config'
"""


class Covers(unittest.TestCase):
    maxDiff = None

    def test_a_spawned_container_cannot_reach_a_covered_path(self):
        p = SESS.run_captured(["bash", "-c", SCRIPT.replace("{root}", ROOT)], timeout=900)
        out = p.stdout
        print(f"\n{out[-2500:]}\n{p.stderr[-800:]}")
        self.assertNotIn("SENTINEL-SECRET", out, "the masked secret never reached a spawned container")
        self.assertNotIn("SENTINEL-ENV", out, "the masked .env never reached a spawned container")
        lines = {l.split("=", 1)[0]: l for l in out.splitlines() if "=" in l}
        for key in ("ROOT", "SECRETS", "ENV", "GITDIR", "GITCONFIG", "GITHOOKS", "REL", "DOTDOT", "HOOKSPATH"):
            self.assertIn(key, lines, key)
            self.assertNotRegex(lines[key], rf"^{key}=0 ", f"{key} should be refused: {lines[key]}")
        self.assertRegex(lines["APP"], r"^APP=0 .*ok", f"a non-covered sub-dir must stay bindable: {lines['APP']}")
        self.assertIn("COMPAT=403", out, "the Docker-API Binds path is checked too")
        self.assertIn("AGENT_APPEND=refused", out, ".git/config is read-only in the agent container")
        left = harness.podman("ps", "-a", "--format", "{{.Names}}", "--filter",
                              f"label=agentbox.session={SESS.name}", "--filter", "label=agentbox.role=dev").stdout
        self.assertEqual(left.split(), [], "no spawned container was created by a refused bind")

    def test_z_nothing_left(self):
        session.teardown(SESS.settings, SESS.podman, SESS.name, warn=lambda m: None)
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
