"""Reviewer projects (agentbox/review.py; decisions.md 11 Nov 2026): a separate clone of the builder, synced on
the host by `ab project sync`. Real git, no podman. Both trees are agent-written, so the sync's defences are
tested as much as its happy path: no hooks, no symlinks followed either way, no overwriting tracked changes."""
import os
import subprocess

from agentbox import registry, review, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class ReviewerCase(SettingsCase):
    def setUp(self):
        super().setUp()
        self.b = self.project("build", preset="script")
        self.write("projects/build/app.py", "print(1)\n")
        git(self.b, "add", "-A")
        git(self.b, "commit", "-qm", "first")
        git(self.b, "branch", "-M", "main")
        self.r = os.path.join(os.path.dirname(self.b), "build-review")

    def add_reviewer(self, *extra):
        return self.ab("project", "add", "build-review", "--reviews", "build", "--preset", "script", *extra)

    def commit_in_builder(self, name, text):
        with open(os.path.join(self.b, name), "w") as f:
            f.write(text)
        git(self.b, "add", name)
        git(self.b, "commit", "-qm", f"add {name}")
        return git(self.b, "rev-parse", "HEAD")

    def review(self, name, text="# review\n"):
        with open(os.path.join(self.r, "reviews", name), "w") as f:
            f.write(text)

    def sync(self, *extra):
        return self.ab("project", "sync", "build-review", *extra)


class Add(ReviewerCase):
    def test_add_clones_next_to_the_builder_and_registers(self):
        code, out = self.add_reviewer()
        self.assertEqual(code, 0, out)
        self.assertIn(f"cloned build ({self.b}) to {self.r}", out)
        e = registry.load(self.settings.registry_path)["build-review"]
        self.assertEqual((e["root"], e["reviews"]), (self.r, "build"))
        self.assertEqual(review.origin_of(self.r), self.b)
        self.assertTrue(os.path.isdir(os.path.join(self.r, "reviews")))
        self.assertEqual(git(self.r, "status", "--porcelain"), "", "reviews/ is excluded from the clone's git")
        code, out = self.ab("project", "show", "build-review")
        self.assertRegex(out, r"reviews\s+build")

    def test_an_existing_clone_is_reused_and_anything_else_refused(self):
        review.make_clone(self.b, self.r)
        code, out = self.ab("project", "add", "build-review", self.r, "--reviews", "build", "--preset", "script")
        self.assertEqual(code, 0, out)
        self.assertNotIn("cloned", out)
        other = self.mkdir("not-a-clone")
        code, out = self.ab("project", "add", "rev2", other, "--reviews", "build", "--preset", "script")
        self.assertEqual(code, 2, out)
        self.assertIn("already exists and is not a clone", out)

    def test_a_failed_registration_removes_the_clone_it_made(self):
        code, out = self.add_reviewer("--data-rw", os.path.join(self.tmp, "absent"))   # refused after the clone
        self.assertEqual(code, 2, out)
        self.assertIn("not an existing directory", out)
        self.assertIn(f"removed the clone {self.r}", out)
        self.assertFalse(os.path.exists(self.r), out)
        self.assertNotIn("build-review", registry.load(self.settings.registry_path))

    def test_the_rules(self):
        code, out = self.ab("project", "add", "x", "--preset", "script")
        self.assertEqual(code, 2, out)
        self.assertIn("a root is required", out)
        code, out = self.ab("project", "add", "x", "--reviews", "nope", "--preset", "script")
        self.assertEqual(code, 2, out)
        self.assertIn("no project named nope", out)
        self.assertEqual(self.add_reviewer()[0], 0)
        code, out = self.ab("project", "add", "x", "--reviews", "build-review", "--preset", "script")
        self.assertEqual(code, 2, out)
        self.assertIn("itself reviews", out)
        code, out = self.ab("project", "set", "build-review", "--reviews", "build-review")
        self.assertEqual(code, 2, out)
        code, out = self.ab("project", "set", "build", "--reviews", "build-review")
        self.assertEqual(code, 2, out)
        self.assertIn("build-review reviews it", out)

    def test_set_reviews_on_a_project_that_is_not_a_clone_is_refused(self):
        other = self.project("other", preset="script")
        code, out = self.ab("project", "set", "other", "--reviews", "build")
        self.assertEqual(code, 2, out)
        self.assertIn(f"{other} is not a clone of build's root", out)

    def test_rm(self):
        self.assertEqual(self.add_reviewer()[0], 0)
        code, out = self.ab("project", "rm", "build")
        self.assertEqual(code, 2, out)
        self.assertIn("build-review (reviews build) still review it", out)
        code, out = self.ab("project", "rm", "build-review")
        self.assertEqual(code, 0, out)
        self.assertIn(f"rm -rf {self.r}", out)
        self.assertTrue(os.path.isdir(self.r), "rm never deletes files")


class Sync(ReviewerCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.add_reviewer()[0], 0)

    def test_fetch_detach_and_hand_over(self):
        head = self.commit_in_builder("feature.py", "x = 2\n")
        self.review("2026-11-11-feature.md", "# feature\nscope creep in feature.py\n")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertEqual(git(self.r, "rev-parse", "HEAD"), head)
        self.assertEqual(subprocess.run(["git", "-C", self.r, "symbolic-ref", "-q", "HEAD"]).returncode, 1,
                         "detached")
        with open(os.path.join(self.b, ".review", "2026-11-11-feature.md")) as f:
            self.assertIn("scope creep", f.read())
        self.assertIn("handed to build: .review/2026-11-11-feature.md", out)
        self.assertIn(".review/ is not ignored", out)
        self.assertTrue(os.path.exists(os.path.join(self.r, "reviews", "2026-11-11-feature.md")), "nothing deleted")

    def test_a_builder_name_syncs_its_reviewers_and_branch_is_honoured(self):
        git(self.b, "checkout", "-qb", "feature-x")
        head = self.commit_in_builder("x.py", "1\n")
        git(self.b, "checkout", "-q", "main")
        code, out = self.ab("project", "sync", "build", "--branch", "feature-x")
        self.assertEqual(code, 0, out)
        self.assertEqual(git(self.r, "rev-parse", "HEAD"), head)
        code, out = self.sync("--branch", "nope")
        self.assertEqual(code, 2, out)
        self.assertIn("no branch 'nope'", out)

    def test_a_detached_builder_needs_a_branch(self):
        git(self.b, "checkout", "-q", "--detach")
        code, out = self.sync()
        self.assertEqual(code, 2, out)
        self.assertIn("--branch", out)

    def test_tracked_changes_in_the_clone_are_never_overwritten(self):
        self.commit_in_builder("feature.py", "x = 2\n")
        with open(os.path.join(self.r, "app.py"), "w") as f:
            f.write("reviewer edit\n")
        code, out = self.sync()
        self.assertEqual(code, 2, out)
        self.assertIn("changes to tracked files", out)
        with open(os.path.join(self.r, "app.py")) as f:
            self.assertEqual(f.read(), "reviewer edit\n")

    def test_no_hook_runs(self):
        """The reviewer's .git/hooks is read-only in a session, but the host does not rely on that."""
        marker = os.path.join(self.tmp, "hook-ran")
        for hook in ("post-checkout", "reference-transaction", "post-merge"):
            path = os.path.join(self.r, ".git", "hooks", hook)
            with open(path, "w") as f:
                f.write(f"#!/bin/sh\ntouch {marker}\n")
            os.chmod(path, 0o755)
        self.commit_in_builder("feature.py", "x = 2\n")
        self.assertEqual(self.sync()[0], 0)
        self.assertFalse(os.path.exists(marker))

    def test_a_symlinked_review_is_not_followed(self):
        secret = self.write("host-secret", "TOP-SECRET-CANARY\n")
        os.symlink(secret, os.path.join(self.r, "reviews", "steal.md"))
        os.mkfifo(os.path.join(self.r, "reviews", "fifo.md"))
        self.review("ok.md")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.lexists(os.path.join(self.b, ".review", "steal.md")))
        self.assertIn("skipped steal.md (a symlink or unreadable)", out)
        self.assertIn("skipped fifo.md (not a regular file)", out)
        self.assertTrue(os.path.isfile(os.path.join(self.b, ".review", "ok.md")))

    def test_a_symlinked_handover_dir_or_file_is_refused(self):
        elsewhere = self.mkdir("elsewhere")
        os.symlink(elsewhere, os.path.join(self.b, ".review"))
        self.review("r.md")
        code, out = self.sync()
        self.assertEqual(code, 2, out)
        self.assertIn("a symlink here is refused", out)
        self.assertEqual(os.listdir(elsewhere), [])
        os.unlink(os.path.join(self.b, ".review"))
        os.mkdir(os.path.join(self.b, ".review"))
        target = self.write("victim", "keep\n")
        os.symlink(target, os.path.join(self.b, ".review", "r.md"))
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertIn("skipped r.md (cannot write it", out)
        with open(target) as f:
            self.assertEqual(f.read(), "keep\n", "the builder's symlink was not written through")

    def test_an_oversized_review_is_skipped(self):
        self.review("big.md", "x" * (review.MAX_REVIEW_BYTES + 1))
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertIn("skipped big.md (over 1 MB)", out)

    def test_a_changed_origin_is_refused(self):
        git(self.r, "remote", "set-url", "origin", self.mkdir("somewhere-else"))
        code, out = self.sync()
        self.assertEqual(code, 2, out)
        self.assertIn("is not a clone of build's root", out)

    def test_not_a_reviewer(self):
        self.project("lone", preset="script")
        code, out = self.ab("project", "sync", "lone")
        self.assertEqual(code, 2, out)
        self.assertIn("--reviews lone", out)


class Notes(ReviewerCase):
    def test_the_reviewer_is_told_what_it_is_and_where_reviews_go(self):
        self.assertEqual(self.add_reviewer()[0], 0)
        p = session.load_project(self.settings, "build-review")
        notes = session.sandbox_notes(p, "packages")
        self.assertIn("You are a REVIEWER of project `build`", notes)
        self.assertIn(f"`{self.r}/reviews/`", notes)
        plain = session.sandbox_notes(session.load_project(self.settings, "build"), "packages")
        self.assertNotIn("REVIEWER", plain)
