"""tests/manual/omp_env_probe.sh prints an environment to a terminal and a log, so it must never print a
value that could be a secret: not from the session shell's env, and not from whatever the model pastes back.
Runs the script here with a fake `omp` on PATH (decisions.md, 11 Nov 2026: the first run leaked the keys)."""
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "manual", "omp_env_probe.sh")
SECRET = "supersecretvalue"

FAKE_OMP = """#!/bin/sh
case "$1" in
  --version) echo omp/0.0 ;;
  --help) echo "  -p, --print   non-interactive" ;;
  -p) echo "a careless model pastes values:"; echo "AB_KEY_X=%s"; echo "AGENTBOX_SESSION=ab-x" ;;
esac
""" % SECRET


class ProbeRedacts(unittest.TestCase):
    def run_probe(self, fake_omp):
        d = tempfile.mkdtemp(prefix="abt-probe-")
        path = os.path.join(d, "omp")
        with open(path, "w") as f:
            f.write(fake_omp)
        os.chmod(path, 0o755)
        env = {"PATH": d + ":/usr/bin:/bin", "HOME": d, "AGENTBOX_SESSION": "ab-x", "AGENTBOX_AGENT": "omp",
               "AB_KEY_X": SECRET, "GIT_AUTHOR_EMAIL": "who@example.org", "HTTPS_PROXY": "http://p:3128"}
        p = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=60)
        return p.stdout + p.stderr

    def test_the_session_env_block_shows_names_and_safe_values_only(self):
        out = self.run_probe(FAKE_OMP.replace("  -p, --print   non-interactive", "no print mode"))
        self.assertNotIn(SECRET, out)
        self.assertIn(f"AB_KEY_X=<redacted, {len(SECRET)} chars>", out)
        self.assertIn("AGENTBOX_SESSION=ab-x", out)
        self.assertIn("HTTPS_PROXY=http://p:3128", out)
        self.assertIn("GIT_AUTHOR_EMAIL=<redacted, 15 chars>", out, "AUTHOR matches 'auth'; over-redaction is fine")
        self.assertNotIn("who@example.org", out)
        self.assertIn("PWD=", out)
        self.assertIn("omp has no -p/--print mode", out)

    def test_the_models_answer_is_redacted_too(self):
        out = self.run_probe(FAKE_OMP)
        self.assertNotIn(SECRET, out)
        self.assertIn("a careless model pastes values:", out)
        self.assertEqual(out.count(f"AB_KEY_X=<redacted, {len(SECRET)} chars>"), 2, out)


if __name__ == "__main__":
    unittest.main()
