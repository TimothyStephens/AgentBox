"""Decision #1 (30 Sep): make itest warns when token-handling files changed since the last passing claude-live."""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.live import freshness


class Freshness(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp(prefix="abt-unit-fresh-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        for rel in ("agentbox/session.py", "agentbox/cli.py", "tests/live/x"):
            self.write(rel, "v1\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.commit()
        for k, v in (("REPO", self.repo), ("PASS_FILE", os.path.join(self.repo, "tests/live/claude-live-pass.txt"))):
            p = mock.patch.object(freshness, k, v)
            p.start()
            self.addCleanup(p.stop)

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def commit(self):
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"],
                       check=True, capture_output=True)

    def check(self):
        return freshness.check(out=io.StringIO())

    def test_never_recorded_warns(self):
        self.assertIn("no passing make claude-live", self.check())

    def test_recorded_and_current_is_quiet(self):
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(freshness.record("locked"))
        self.assertEqual(self.check(), "")
        self.write("agentbox/cli.py", "v2\n")               # not a token-handling file
        self.commit()
        self.assertEqual(self.check(), "")

    def test_token_file_changes_warn_committed_uncommitted_and_new(self):
        with mock.patch("sys.stdout", io.StringIO()):
            freshness.record("locked")
        self.write("agentbox/session.py", "v2\n")
        self.assertIn("agentbox/session.py", self.check())   # uncommitted
        self.commit()
        self.assertIn("agentbox/session.py", self.check())   # committed since
        self.write("agentbox/adapters/omp.py", "new\n")
        self.assertIn("agentbox/adapters/omp.py", self.check())   # new, untracked

    def test_record_refused_with_uncommitted_token_changes(self):
        self.write("agentbox/session.py", "dirty\n")
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertFalse(freshness.record("locked"))
        self.assertIn("NOT recorded", out.getvalue())
        self.assertFalse(os.path.exists(freshness.PASS_FILE))

    def test_unknown_commit_warns(self):
        self.write("tests/live/claude-live-pass.txt", "0123456789abcdef0123456789abcdef01234567 2026-09-30\n")
        self.assertIn("not a commit", self.check())
