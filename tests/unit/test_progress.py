"""Progress lines for long steps (progress.py) and the backup's rsync progress (backup.py)."""
import os
import time
import unittest

from agentbox import backup
from agentbox.progress import Step, elapsed

from .helpers import TempCase


class StepLines(unittest.TestCase):
    def test_a_fast_step_prints_nothing(self):
        out = []
        with Step("doing x", out.append, delay=5):
            pass
        self.assertEqual(out, [])

    def test_a_slow_step_says_what_it_does_how_far_it_is_and_how_long_it_took(self):
        out = []
        with Step("backing up /p", out.append, delay=0.02, heartbeat=0.05) as st:
            st.progress(0.42, "42 of 100 files and folders")
            time.sleep(0.3)
        self.assertEqual(out[0], "agentbox: backing up /p ...")
        self.assertTrue(any("42%" in line and "42 of 100 files and folders" in line for line in out[1:-1]), out)
        self.assertRegex(out[-1], r"^agentbox: backing up /p: done in \d")

    def test_a_step_that_fails_says_it_stopped(self):
        out = []
        with self.assertRaises(RuntimeError):
            with Step("doing x", out.append, delay=0.01, heartbeat=10):
                time.sleep(0.1)
                raise RuntimeError("boom")
        self.assertRegex(out[-1], r"^agentbox: doing x: stopped after ")

    def test_nothing_is_printed_after_the_step_ends(self):
        out = []
        with Step("doing x", out.append, delay=0.05):
            pass
        time.sleep(0.15)
        self.assertEqual(out, [])

    def test_elapsed(self):
        self.assertEqual(elapsed(3.21), "3.2s")
        self.assertEqual(elapsed(125), "2m05s")


class _Recorder:
    def __init__(self, raise_at=None):
        self.calls, self.raise_at = [], raise_at

    def __call__(self, grant):
        rec = self

        class Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def progress(self, frac=None, detail=None):
                rec.calls.append((frac, detail))
                if rec.raise_at is not None and frac is not None and frac >= rec.raise_at:
                    raise KeyboardInterrupt
        return Ctx()


class BackupProgress(TempCase):
    def setUp(self):
        super().setUp()
        self.src = self.mkdir("proj")
        for i in range(50):
            self.write(f"proj/d{i % 5}/f{i}.txt", "x" * 1000)
        self.backups = self.mkdir("backups", "p")

    def test_the_backup_reports_its_share_of_the_file_list_and_still_reads_the_stats(self):
        rec = _Recorder()
        warned = []
        snap = backup.snapshot(self.backups, "s1", [self.src], step=rec, warn_mb=0, warn=warned.append)
        self.assertTrue(os.path.isfile(os.path.join(snap, self.src.lstrip("/"), "d0", "f0.txt")))
        fracs = [f for f, _ in rec.calls]
        self.assertTrue(fracs, "rsync reported progress")
        self.assertAlmostEqual(fracs[-1], 1.0)
        self.assertIn("files and folders", rec.calls[-1][1])
        rc, out, _err = backup._run_rsync(backup.rsync_argv(self.src, self.mkdir("again")), lambda *a: None)
        self.assertEqual(rc, 0)
        self.assertGreater(backup._total_size(out), 0, "--stats still parses with the progress lines in")

    def test_a_linked_backup_still_reaches_100_percent(self):
        """With --link-dest the unchanged files are not copied, and rsync's byte percentage leaves them out;
        the file-list share does not."""
        backup.snapshot(self.backups, "s1", [self.src])
        rec = _Recorder()
        import datetime
        backup.snapshot(self.backups, "s2", [self.src], step=rec,
                        now=datetime.datetime.now() + datetime.timedelta(seconds=5))
        self.assertAlmostEqual(rec.calls[-1][0], 1.0)

    def test_an_interrupted_backup_leaves_no_half_written_snapshot(self):
        with self.assertRaises(KeyboardInterrupt):
            backup.snapshot(self.backups, "s1", [self.src], step=_Recorder(raise_at=0.0))
        self.assertEqual(os.listdir(self.backups), [])


if __name__ == "__main__":
    unittest.main()
