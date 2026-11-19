"""The trusted opt-outs (`data_env`, `data_bind`; decisions.md 19 Nov 2026) from inside a real session.

Pinned from the host check of 19 Nov (a scratch project, `--data-env on --data-bind on`, preset python):
  * `.env` is the REAL file, not the `.env.dev` swap; `sub/.env` (an env file deeper in the root) is real too;
  * the other secret masks still hold: `.netrc`, `tls/server.key` and `secrets/` read empty;
  * `./data` is readable and writable, and a file written there reaches the host;
  * the session warns at start, the end-of-session report repeats the warnings first, and the backup step
    says `./data` is not in the pre-session snapshot; no file under `data/` is in the snapshot.
And the control: the same layout WITHOUT the settings keeps `.env` swapped and `./data` out of nothing.
"""
import os
import shutil
import unittest

from . import harness

ENV = None
IMAGE = "localhost/agentbox-claude:latest"
REAL = f"REAL=real-{harness.RUN}\n"
DEV = "REAL=dev-value\n"
LIVE = f"live-row-{harness.RUN}\n"

PROBE = r"""
set +e
echo "ENV=$(cat .env)"
echo "SUBENV=$(cat sub/.env)"
echo "NETRC_BYTES=$(wc -c < .netrc)"
echo "KEY_BYTES=$(wc -c < tls/server.key)"
echo "SECRETS_LS=$(ls -A secrets | tr '\n' ',')"
echo "DATA=$(cat data/live.txt 2>&1)"
( echo w > data/new.txt ) 2>/dev/null && echo DATA_WRITE=ok || echo DATA_WRITE=refused
"""


def _layout(root):
    w = harness._write
    w(os.path.join(root, ".env"), REAL)
    w(os.path.join(root, ".env.dev"), DEV)
    w(os.path.join(root, ".netrc"), "machine x login y password z\n")
    w(os.path.join(root, "data", "live.txt"), LIVE)


def setUpModule():
    global ENV
    if shutil.which("podman") is None:
        raise unittest.SkipTest("podman not on PATH: an itest needs the host's real podman")
    if harness.podman("image", "exists", IMAGE).returncode != 0:
        raise unittest.SkipTest(f"{IMAGE} missing: run make claude-image")
    ENV = harness.Env()
    print(f"\n[itest run {harness.RUN}: trusted opt-outs]")


def tearDownModule():
    harness.cleanup()


def _run(name):
    p = ENV.ab("session", "start", "claude", name, "--shell", "--no-tty", "--", "-c", PROBE, timeout=600)
    print(f"\n[{name}]\n{p.stdout[-1500:]}\n--stderr--\n{p.stderr[-2000:]}")
    return {l.split("=", 1)[0]: l.split("=", 1)[1] for l in p.stdout.splitlines() if "=" in l}, p


def _snapshot_files(name):
    base = os.path.join(ENV.data, "projects", name, "backups")
    out = []
    for dirpath, _dirs, files in os.walk(base):
        out += [os.path.relpath(os.path.join(dirpath, f), base) for f in files]
    return out


class TrustedOptOuts(unittest.TestCase):
    maxDiff = None

    def test_a_both_on(self):
        root = ENV.make_project("trust", preset="python")
        _layout(root)
        ENV.ab("project", "set", "trust", "--data-env", "on", "--data-bind", "on", check=True)
        out, p = _run("trust")
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertEqual(out.get("ENV"), REAL.strip(), "data_env shows the real .env, not the swap")
        self.assertEqual(out.get("SUBENV"), "SUB=real", "env files deeper in the root are real too")
        self.assertEqual(out.get("NETRC_BYTES"), "0", ".netrc stays masked")
        self.assertEqual(out.get("KEY_BYTES"), "0", "key files stay masked")
        self.assertEqual(out.get("SECRETS_LS"), "", "secrets/ stays masked")
        self.assertEqual(out.get("DATA"), LIVE.strip())
        self.assertEqual(out.get("DATA_WRITE"), "ok")
        self.assertTrue(os.path.isfile(os.path.join(root, "data", "new.txt")), "a write in ./data reaches the host")
        self.assertIn("WARNING: data_env is on for trust", p.stderr)
        self.assertIn("WARNING: data_bind is on for trust", p.stderr)
        self.assertIn("WARNING (recorded at the start): data_env", p.stderr, "the end report repeats it")
        self.assertIn("is not in the pre-session snapshot", p.stderr)
        snap = _snapshot_files("trust")
        self.assertTrue(snap, "a snapshot was taken")
        self.assertFalse([f for f in snap if "/data/" in f or f.startswith("data/")],
                         f"nothing under data/ is in the snapshot: {[f for f in snap if 'data' in f][:5]}")

    def test_b_control_both_off(self):
        root = ENV.make_project("notrust", preset="python")
        _layout(root)
        out, p = _run("notrust")
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertEqual(out.get("ENV"), DEV.strip(), "without data_env the swap applies")
        self.assertEqual(out.get("NETRC_BYTES"), "0")
        self.assertNotIn("WARNING: data_env", p.stderr)
        self.assertNotIn("WARNING: data_bind", p.stderr)

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
