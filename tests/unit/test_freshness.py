"""Decision #1 (30 Sep): make itest warns when token-handling files changed since the last passing claude-live."""
import datetime
import io
import os
import socket
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
        for k, v in (("REPO", self.repo),
                     ("PASS_FILE", os.path.join(self.repo, "tests/live/claude-live-pass.txt")),
                     ("PASS_LOG", os.path.join(self.repo, "tests/live/claude-live-pass.log"))):
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

    def test_recording_without_the_live_runs_opt_in_raises(self):
        """The pass file is the only evidence the real token was exercised end to end, and `make itest`
        trusts it; a line written by anything but the live runner is a false pass. A line once appeared
        that no live run had produced, so writing it is opt-in."""
        with self.assertRaises(RuntimeError) as cm:
            freshness.record("locked")
        self.assertIn("from_live_run", str(cm.exception))
        self.assertFalse(os.path.exists(freshness.PASS_FILE), "nothing written")

    def test_a_pass_appends_a_provenance_line(self):
        """The one-line record says WHICH commit passed and is overwritten by the next run; the log is
        append-only, so each run leaves a traceable line (commit, timestamp, mode, host) and an overwrite is
        visible after the fact."""
        with mock.patch("sys.stdout", io.StringIO()):
            freshness.record("locked", from_live_run=True)
            freshness.record("packages", from_live_run=True)
        with open(freshness.PASS_LOG) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2, "append-only: the second run did not truncate the first")
        head = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        for line, mode in zip(lines, ("locked", "packages")):
            self.assertIn(head, line)
            self.assertIn(f"egress={mode}", line)
            self.assertIn(f"host={socket.gethostname()}", line)
            stamp = line.split()[1]
            datetime.datetime.fromisoformat(stamp)        # a real ISO timestamp, not a date
            self.assertIn("T", stamp, "the log carries the time of day, which the record does not")
        # the one-line record is still the latest pass only
        with open(freshness.PASS_FILE) as f:
            self.assertEqual(len(f.read().splitlines()), 1)

    def test_a_refused_record_logs_nothing(self):
        with self.assertRaises(RuntimeError):
            freshness.record("locked")
        self.assertFalse(os.path.exists(freshness.PASS_LOG))

    def test_an_unrecorded_pass_logs_nothing(self):
        """Uncommitted token-handling changes mean the pass describes no commit, so neither file is written."""
        self.write("agentbox/session.py", "v2\n")
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(freshness.record("locked", from_live_run=True))
        self.assertFalse(os.path.exists(freshness.PASS_LOG))
        self.assertFalse(os.path.exists(freshness.PASS_FILE))

    def test_never_recorded_warns(self):
        self.assertIn("no passing make claude-live", self.check())

    def test_recorded_and_current_is_quiet(self):
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(freshness.record("locked", from_live_run=True))
        self.assertEqual(self.check(), "")
        self.write("agentbox/cli.py", "v2\n")               # not a token-handling file
        self.commit()
        self.assertEqual(self.check(), "")

    def test_token_file_changes_warn_committed_uncommitted_and_new(self):
        with mock.patch("sys.stdout", io.StringIO()):
            freshness.record("locked", from_live_run=True)
        self.write("agentbox/session.py", "v2\n")
        self.assertIn("agentbox/session.py", self.check())   # uncommitted
        self.commit()
        self.assertIn("agentbox/session.py", self.check())   # committed since
        self.write("agentbox/adapters/omp.py", "new\n")
        self.assertIn("agentbox/adapters/omp.py", self.check())   # new, untracked

    def test_record_refused_with_uncommitted_token_changes(self):
        self.write("agentbox/session.py", "dirty\n")
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertFalse(freshness.record("locked", from_live_run=True))
        self.assertIn("NOT recorded", out.getvalue())
        self.assertFalse(os.path.exists(freshness.PASS_FILE))

    def test_unknown_commit_warns(self):
        self.write("tests/live/claude-live-pass.txt", "0123456789abcdef0123456789abcdef01234567 2026-09-30\n")
        self.assertIn("not a commit", self.check())
