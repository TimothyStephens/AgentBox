"""Shared path and file helpers (`agentbox/fsutil.py`)."""
import os
import stat
import tempfile
import unittest

from agentbox import fsutil


class InsideTest(unittest.TestCase):
    def test_root_itself_and_paths_below_are_inside(self):
        self.assertTrue(fsutil.inside("/a/b", "/a/b"))
        self.assertTrue(fsutil.inside("/a/b/c", "/a/b"))
        self.assertTrue(fsutil.inside("/a/b/c", "/a/b/"))
        self.assertTrue(fsutil.inside("/x", "/"))

    def test_a_sibling_sharing_a_prefix_is_not_inside(self):
        self.assertFalse(fsutil.inside("/a/bc", "/a/b"))
        self.assertFalse(fsutil.inside("/a", "/a/b"))


class WritePrivateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_atomic_write_is_0600_replaces_a_planted_temp_link_and_never_writes_through_it(self):
        victim = os.path.join(self.d, "victim")
        with open(victim, "w") as f:
            f.write("keep")
        path = os.path.join(self.d, "f")
        os.symlink(victim, path + ".tmp")
        os.symlink(victim, path)
        old = os.umask(0)
        try:
            fsutil.write_private(path, "new")
        finally:
            os.umask(old)
        self.assertFalse(os.path.islink(path))
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertEqual(f.read(), "new")
        with open(victim) as f:
            self.assertEqual(f.read(), "keep")
        self.assertFalse(os.path.lexists(path + ".tmp"))

    def test_non_atomic_creates_fresh_and_refuses_anything_there(self):
        path = os.path.join(self.d, "f")
        fsutil.write_private(path, b"x", mode=0o444, atomic=False)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o444)
        link = os.path.join(self.d, "l")
        os.symlink(os.path.join(self.d, "nowhere"), link)
        for p in (path, link):
            with self.assertRaises(FileExistsError):
                fsutil.write_private(p, "y", atomic=False)
        self.assertFalse(os.path.lexists(os.path.join(self.d, "nowhere")))


if __name__ == "__main__":
    unittest.main()
