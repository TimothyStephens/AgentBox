"""Shared path and file helpers (`agentbox/fsutil.py`)."""
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


if __name__ == "__main__":
    unittest.main()
