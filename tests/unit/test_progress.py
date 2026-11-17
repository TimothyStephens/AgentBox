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


class ReviewR5toR7(TempCase):
    def test_sidecar_logs_are_bounded(self):
        """R-5: k8s-file has no size limit of its own, and squid logs every request to stdout."""
        from agentbox.infra import SIDECAR_LOG_SIZE
        self.assertTrue(SIDECAR_LOG_SIZE.startswith("--log-opt=max-size="))

    def test_the_default_warn_writes_a_line_in_one_call(self):
        """R-6: print writes the text and the newline separately, so a heartbeat could split a warning."""
        import io
        from unittest import mock
        from agentbox.progress import stderr_line
        buf = io.StringIO()
        writes = []
        buf.write = lambda s, w=buf.write: (writes.append(s), w(s))[1]
        with mock.patch("sys.stderr", buf):
            stderr_line("agentbox: x")
        self.assertEqual(writes, ["agentbox: x\n"])

    def test_only_a_missing_rsync_is_told_to_install_rsync(self):
        """R-7: any other OSError in the copy (a full /tmp, a closed stderr) said `apt-get install rsync`."""
        from unittest import mock
        from agentbox.errors import AgentboxError
        src = self.mkdir("proj")
        self.write("proj/a.txt", "x")
        with mock.patch("agentbox.backup.tempfile.TemporaryFile", side_effect=OSError(28, "No space left")):
            with self.assertRaises(AgentboxError) as cm:
                backup.snapshot(self.mkdir("bk"), "s1", [src], step=_Recorder())
        self.assertNotIn("install", str(cm.exception).lower())
        self.assertIn("No space left", str(cm.exception))


class CachesAreNotBackedUp(TempCase):
    """A folder tagged with CACHEDIR.TAG is a rebuildable cache (Cargo tags every target/); toulmin-build's
    56 GB target/ made each pre-session backup 53 GB."""
    SIG = "Signature: 8a477f597d28d172789f06886806bc55\n# cargo\n"

    def setUp(self):
        super().setUp()
        self.src = self.mkdir("proj")
        self.write("proj/src/main.rs", "fn main() {}")
        self.write("proj/target/CACHEDIR.TAG", self.SIG)
        self.write("proj/target/debug/big.rlib", "x" * 1000)
        self.write("proj/crates/a/target/CACHEDIR.TAG", self.SIG)
        self.write("proj/crates/a/target/debug/x.o", "x")
        self.write("proj/notcache/CACHEDIR.TAG", "not the signature")
        self.write("proj/notcache/keep.txt", "keep")

    def test_tagged_folders_are_left_out_and_the_rest_is_kept(self):
        snap = backup.snapshot(self.mkdir("bk"), "s1", [self.src])
        root = os.path.join(snap, self.src.lstrip("/"))
        self.assertTrue(os.path.isfile(os.path.join(root, "src", "main.rs")))
        self.assertTrue(os.path.isfile(os.path.join(root, "notcache", "keep.txt")), "a wrong signature is no tag")
        self.assertFalse(os.path.exists(os.path.join(root, "target")))
        self.assertFalse(os.path.exists(os.path.join(root, "crates", "a", "target")))

    def test_the_walk_skips_what_the_backup_skips_and_follows_no_symlink(self):
        self.write("proj/skipped/target/CACHEDIR.TAG", self.SIG)
        os.symlink(os.path.join(self.src, "target"), os.path.join(self.src, "link"))
        self.assertEqual(backup.cache_dirs(self.src, skip_rel=["skipped"]), ["crates/a/target", "target"])

    def test_a_tagged_grant_root_is_still_backed_up(self):
        self.write("proj/CACHEDIR.TAG", self.SIG)
        self.assertNotIn("", backup.cache_dirs(self.src))
        self.assertNotIn(".", backup.cache_dirs(self.src))


class ReadOnlyFoldersAreRemoved(TempCase):
    """rsync -a keeps a folder's mode, so a toolchain's 0555 bin/ made a snapshot that a plain rmtree could not
    remove: pruning silently kept every snapshot of toulmin-build, and its `.incomplete` leftovers."""

    def tearDown(self):
        for dirpath, dirnames, _ in os.walk(self.tmp):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        super().tearDown()

    def _tree(self, name):
        root = self.mkdir(name)
        self.write(f"{name}/bin/tool", "x")
        self.write(f"{name}/locked/inner/f", "x")
        os.chmod(os.path.join(root, "locked", "inner"), 0o000)
        os.chmod(os.path.join(root, "locked"), 0o500)
        os.chmod(os.path.join(root, "bin"), 0o555)
        return root

    def test_pruning_removes_snapshots_with_read_only_folders(self):
        import datetime
        src = self.mkdir("src")
        self.write("src/bin/tool", "x")
        os.chmod(os.path.join(src, "bin"), 0o555)
        bk = self.mkdir("bk")
        for i in range(4):
            backup.snapshot(bk, "s", [src], now=datetime.datetime(2026, 1, 1, 0, 0, i))
        backup.retain(bk, keep=2, daily_days=0)
        self.assertEqual(sorted(os.listdir(bk)), ["20260101-000002-s", "20260101-000003-s"])

    def test_rmtree_removes_unwritable_and_unreadable_folders(self):
        from agentbox.fsutil import rmtree
        root = self._tree("t")
        rmtree(root)
        self.assertFalse(os.path.exists(root))

    def test_rmtree_does_not_touch_a_folder_a_symlink_points_at(self):
        from agentbox.fsutil import rmtree
        outside = self._tree("outside")
        root = self.mkdir("t2")
        os.symlink(outside, os.path.join(root, "link"))
        rmtree(root)
        self.assertFalse(os.path.exists(root))
        self.assertEqual(os.stat(os.path.join(outside, "bin")).st_mode & 0o777, 0o555)
