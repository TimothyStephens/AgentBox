"""A linked worktree's own git dir (`<repo>/.git/worktrees/<name>`: HEAD, commondir, gitdir, no objects/refs)
is a git dir for the covers (19 Nov 2026). With `extensions.worktreeConfig` on, git reads `config.worktree` there
and in the main git dir; a `core.hooksPath` planted in either runs on the owner's next git command. So each
`config.worktree` is covered read-only, created empty first when it does not exist yet; with the extension off
git never reads it and nothing is added. No hooks/ dir is made in a worktree's git dir (git uses the common one)."""
import os
import subprocess
import unittest

from agentbox import mounts

from .helpers import TempCase

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


class WorktreeCovers(TempCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(self.mkdir("proj"))
        self.git("init", "-q", self.root)
        self.write(os.path.join("proj", "a.txt"), "a\n")
        self.git("-C", self.root, "add", "a.txt")
        self.git("-C", self.root, "commit", "-qm", "init")
        self.git("-C", self.root, "worktree", "add", "-q", os.path.join(self.root, "wt"))
        self.wtdir = os.path.join(self.root, ".git", "worktrees", "wt")

    def git(self, *args):
        subprocess.run(GIT + list(args), check=True, capture_output=True)

    def ro_dsts(self, plan):
        return {m.dst for m in plan.mounts if m.kind == "bind" and m.ro}

    def test_the_worktree_git_dir_is_found(self):
        found, _outside = mounts.find_git_dirs(self.root)
        self.assertIn(self.wtdir, found)
        self.assertIn(os.path.join(self.root, ".git"), found, "the common dir is still followed")

    def test_an_existing_worktree_config_is_covered(self):
        self.git("-C", self.root, "config", "extensions.worktreeConfig", "true")
        self.git("-C", os.path.join(self.root, "wt"), "config", "--worktree", "core.editor", "vi")
        p = mounts.plan(root=self.root)
        self.assertIn(os.path.join(self.wtdir, "config.worktree"), self.ro_dsts(p))

    def test_a_missing_worktree_config_is_created_and_covered_when_git_would_read_it(self):
        self.git("-C", self.root, "config", "extensions.worktreeConfig", "true")
        p = mounts.plan(root=self.root)
        for d in (self.wtdir, os.path.join(self.root, ".git")):
            cfg = os.path.join(d, "config.worktree")
            with self.subTest(gitdir=d):
                self.assertIn(cfg, p.create_files)
                self.assertIn(cfg, self.ro_dsts(p))

    def test_with_the_extension_off_nothing_is_added(self):
        p = mounts.plan(root=self.root)
        self.assertFalse([f for f in p.create_files if f.endswith("config.worktree")])
        self.assertNotIn(os.path.join(self.wtdir, "config.worktree"), self.ro_dsts(p))

    def test_no_hooks_dir_is_made_in_a_worktree_git_dir(self):
        p = mounts.plan(root=self.root)
        self.assertNotIn(os.path.join(self.wtdir, "hooks"), p.create_dirs)
        self.assertNotIn(os.path.join(self.wtdir, "hooks"), self.ro_dsts(p))
        self.assertIn(os.path.join(self.root, ".git", "hooks"), self.ro_dsts(p), "the common hooks stay covered")


if __name__ == "__main__":
    unittest.main()
