"""Pi is a first-class agent (docs/pi-proposal.md): its home isolation, proved from inside a live Pi session.

  * the generated files are read-only: models.json, auth.json (`{}`: `/login` cannot store a key), mcp.json, and
    the session notes; the code places (extensions, npm, git, bin) are empty and read-only;
  * settings.json is the session's own fresh copy with project trust off; PI_OFFLINE=1, PI_TELEMETRY=0;
  * the repository's `.pi` (here: an extension and a settings.json that names a package) is covered empty;
  * the key reaches Pi only as `${AB_KEY_STUBAI}` in models.json, and its value appears in no file under ~/.pi;
  * a SYSTEM.md a session leaves in the per-project agent dir is moved to the inbox before the next session.
"""
import os
import secrets
import unittest

from . import harness

ENV = None
IMAGE = "localhost/agentbox-pi:latest"
RUN = harness.RUN
KEYVAL = f"sk-PI-KEYVAL-{secrets.token_hex(10)}"
SEED = """providers:
  stubai:
    api: openai-completions
    baseUrl: http://127.0.0.1:8766/v1
    models:
      - id: stub-model
        contextWindow: 32000
        maxOut: 2000
"""


def setUpModule():
    global ENV
    if harness.podman("image", "exists", IMAGE).returncode != 0:
        raise unittest.SkipTest(f"{IMAGE} missing: run make pi-image")
    ENV = harness.Env()
    os.makedirs(os.path.join(ENV.cfg, "omp"), exist_ok=True)
    with open(os.path.join(ENV.cfg, "omp", "models.yml"), "w") as f:
        f.write(SEED)
    kdir = os.path.join(ENV.cfg, "keys")
    os.makedirs(kdir, mode=0o700, exist_ok=True)
    kf = os.path.join(kdir, "stubai")
    with open(kf, "w") as f:
        f.write(KEYVAL + "\n")
    os.chmod(kf, 0o600)
    root = ENV.make_project("piiso", preset="python")
    os.makedirs(os.path.join(root, ".pi", "extensions"), exist_ok=True)
    with open(os.path.join(root, ".pi", "extensions", "planted.ts"), "w") as f:
        f.write("export default function () {}\n")
    with open(os.path.join(root, ".pi", "settings.json"), "w") as f:
        f.write('{"packages": ["npm:left-pad@1.3.0"]}\n')
    print(f"\n[itest run {RUN}: pi isolation]")


def tearDownModule():
    harness.cleanup()


SCRIPT = """
set +e
A=~/.pi/agent
for f in models.json auth.json mcp.json AGENTS.md; do ( echo x >> $A/$f ) 2>/dev/null; echo "W_$f=$?"; done
for d in extensions npm git bin; do ( : > $A/$d/planted ) 2>/dev/null; echo "D_$d=$?"; done
echo "AUTH=$(tr -d ' \\n' < $A/auth.json)"
echo "TRUST=$(grep -c '"defaultProjectTrust": "never"' $A/settings.json)"
echo "OFFLINE=$PI_OFFLINE TELEMETRY=$PI_TELEMETRY"
echo "REPO_PI=$(ls -A .pi 2>/dev/null | tr '\\n' ',')"
echo "KEYREF=$(grep -c '${AB_KEY_STUBAI}' $A/models.json)"
HITS=""; if [ -n "$AB_KEY_STUBAI" ]; then HITS="$(grep -rlF -e "$AB_KEY_STUBAI" ~/.pi 2>/dev/null | tr '\\n' ',')"; fi
echo "KEYVAL_HITS=$HITS"
echo "PIVERSION=$(pi --version 2>&1 | head -1)"
echo steer > $A/SYSTEM.md; echo "PLANT=$?"
"""


class PiIsolation(unittest.TestCase):
    maxDiff = None

    def run_in_session(self, script):
        p = ENV.ab("session", "start", "pi", "piiso", "--shell", "--no-tty", "--", "-c", script, timeout=900)
        print(f"\n{p.stdout[-1800:]}\n--stderr--\n{p.stderr[-800:]}")
        return {l.split("=", 1)[0]: l.split("=", 1)[1] for l in p.stdout.splitlines() if "=" in l}, p

    def test_pi_home_is_isolated(self):
        out, p = self.run_in_session(SCRIPT)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        for f in ("models.json", "auth.json", "mcp.json", "AGENTS.md"):
            with self.subTest(file=f):
                self.assertNotEqual(out.get(f"W_{f}"), "0", f"{f} is read-only")
        for d in ("extensions", "npm", "git", "bin"):
            with self.subTest(dir=d):
                self.assertNotEqual(out.get(f"D_{d}"), "0", f"{d} is read-only")
        self.assertEqual(out.get("AUTH"), "{}")
        self.assertEqual(out.get("TRUST"), "1")
        self.assertEqual(out.get("OFFLINE"), "1 TELEMETRY=0")
        self.assertEqual(out.get("REPO_PI"), "", "the repository's .pi is covered empty")
        self.assertEqual(out.get("KEYREF"), "1")
        self.assertEqual(out.get("KEYVAL_HITS"), "", "the key value is in no file under ~/.pi")
        self.assertIn("1.", out.get("PIVERSION", ""))
        self.assertEqual(out.get("PLANT"), "0", "the per-project agent dir itself is writable")
        out2, p2 = self.run_in_session('echo "SYS=$(ls ~/.pi/agent/SYSTEM.md 2>/dev/null)"')
        self.assertEqual(p2.returncode, 0, p2.stderr[-800:])
        self.assertEqual(out2.get("SYS"), "", "a planted SYSTEM.md does not reach the next session")
        self.assertIn("inbox", p2.stderr)

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])
