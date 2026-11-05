"""The fail-open sweep (third audit, after F-2/F-10): every safety decision that rested on a swallowed
error now fails closed.

The defect behind two of the audit's findings was not a missing check but a swallowed one -- a reader that
answered "no" when it meant "cannot tell". These are the remaining sites of that shape, each refusing or
reporting instead of assuming the safe-looking answer. (The podman readers and the liveness predicate are in
test_liveness.py.)
"""
import errno
import os
import unittest
from unittest import mock

from agentbox import hardening, mounts, session
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from .helpers import SettingsCase, TempCase


class GitFileThatCannotBeRead(TempCase):
    """A `.git` FILE names the real git directory (worktrees, submodules, --separate-git-dir). If it cannot
    be read, that repository's hooks/ and config are left uncovered -- writable by the agent -- and the
    session used to start anyway."""

    def test_an_unreadable_git_file_refuses_the_plan(self):
        root = self.mkdir("proj")
        self.write("proj/app.py", "code\n")
        gitfile = os.path.join(root, "sub", ".git")
        os.makedirs(os.path.dirname(gitfile), exist_ok=True)
        with open(gitfile, "w") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/x\n")
        os.chmod(gitfile, 0o000)
        self.addCleanup(os.chmod, gitfile, 0o644)
        with self.assertRaises(AgentboxError) as cm:
            mounts.plan(root=root)
        self.assertIn(".git", str(cm.exception))
        self.assertIn("cannot", str(cm.exception).lower())

    def test_a_readable_git_file_is_fine(self):
        root = self.mkdir("proj2")
        with open(os.path.join(root, ".git"), "w") as f:
            f.write("gitdir: /elsewhere/.git\n")
        mounts.plan(root=root)                      # target missing is a note, not a refusal


class HardLinkStatFailure(TempCase):
    """A cover hides a path, not an inode, so every other hard link to a covered file is masked too. If a
    candidate cannot be stat'ed we cannot tell whether it is such a link."""

    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")
        self.write("proj/.env", "SECRET=1\n")
        # a real second link to the masked .env, so hardlink_masks actually walks the tree looking for more
        os.link(os.path.join(self.root, ".env"), os.path.join(self.root, "notes.txt"))
        self.write("proj/other.txt", "x\n")

    def plan_with_lstat_error(self, err):
        real = os.lstat
        target = os.path.join(self.root, "other.txt")

        def fake(path, *a, **kw):
            if str(path) == target:
                raise OSError(err, os.strerror(err))
            return real(path, *a, **kw)

        with mock.patch("agentbox.mounts.os.lstat", side_effect=fake):
            return mounts.plan(root=self.root)

    def test_a_permission_error_refuses(self):
        with self.assertRaises(AgentboxError) as cm:
            self.plan_with_lstat_error(errno.EACCES)
        self.assertIn("other.txt", str(cm.exception))

    def test_a_vanished_file_is_skipped(self):
        """A file that disappeared mid-walk cannot be a live hard link to anything."""
        self.plan_with_lstat_error(errno.ENOENT)


class MountAuditSocketStat(TempCase):
    """The only socket a sandbox may be given is sockguard's. If the bind source cannot be stat'ed, we
    cannot confirm it is not a socket."""

    def test_an_unstattable_bind_source_is_refused(self):
        root = self.mkdir("grant")
        src = os.path.join(root, "thing")
        self.write("grant/thing", "x")
        argv = ["--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--userns=keep-id",
                "--pids-limit=1", "--memory=1g", "--memory-swap=1g", "--oom-score-adj=1",
                f"--mount=type=bind,src={src},dst={src}", "img"]
        kw = dict(allowed_mount_srcs=[root], allowed_mount_dsts=[root], networks=[],
                  allowed_sockets=[])
        hardening.audit(argv, **kw)                                   # fine when stattable
        real = os.stat

        def fake(path, *a, **kw):
            if str(path) == os.path.realpath(src):
                raise OSError(errno.EACCES, "nope")
            return real(path, *a, **kw)

        with mock.patch("agentbox.hardening.os.stat", side_effect=fake):
            with self.assertRaises(AgentboxError) as cm:
                hardening.audit(argv, **kw)
        self.assertIn("cannot", str(cm.exception).lower())

    def test_a_source_that_does_not_exist_is_not_a_socket(self):
        """Nothing there can be a socket, and podman rejects a missing bind source itself -- so this must not
        become the audit's refusal (the other mount rules still apply, and are what these argv are checked
        against elsewhere)."""
        root = self.mkdir("grant")
        src = os.path.join(root, "gone")
        argv = ["--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--userns=keep-id",
                "--pids-limit=1", "--memory=1g", "--memory-swap=1g", "--oom-score-adj=1",
                f"--mount=type=bind,src={src},dst={src}", "img"]
        hardening.audit(argv, allowed_mount_srcs=[root], allowed_mount_dsts=[root], networks=[],
                        allowed_sockets=[])


class CoverIdentities(TempCase):
    """`cover_ids()` is what the gate's rename detection works from: a cover missing from the map is a cover
    the gate can no longer tell was renamed or replaced mid-session. An identity that cannot be read used to
    be skipped silently."""

    class _CoversOnly(session.Session):
        """Only `covered_paths()` matters here, and a real session needs rsync (prepare() makes the backup)."""

        def __init__(self, paths):
            self._paths = paths

        def covered_paths(self):
            return self._paths

    def test_an_unreadable_cover_identity_refuses_the_session(self):
        self.write("proj/.env", "SECRET=1\n")
        env = os.path.join(self.tmp, "proj", ".env")
        s = self._CoversOnly([env])
        self.assertEqual(s.cover_ids()[env][1], os.lstat(env).st_ino)      # readable: pinned
        real = os.lstat

        def fake(path, *a, **kw):
            if str(path) == env:
                raise OSError(errno.EACCES, "nope")
            return real(path, *a, **kw)

        with mock.patch("agentbox.session.os.lstat", side_effect=fake):
            with self.assertRaises(AgentboxError) as cm:
                s.cover_ids()
        self.assertIn(".env", str(cm.exception))

    def test_a_cover_that_does_not_exist_is_skipped(self):
        """Nothing there has an identity to pin, and nothing there can be renamed."""
        self.write("proj/.env", "SECRET=1\n")
        env = os.path.join(self.tmp, "proj", ".env")
        s = self._CoversOnly([os.path.join(self.tmp, "proj", "gone.pem"), env])
        self.assertEqual(list(s.cover_ids()), [env])


class SessionListingsAreStrict(SettingsCase):
    def test_a_failing_network_listing_raises(self):
        """sessions() feeds gc and `ab session ls`; a listing that silently came back empty made a live
        session's network invisible."""
        os.environ["FAKE_PODMAN_FAIL"] = "network"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        with self.assertRaises(AgentboxError):
            session.sessions(self.settings, Podman(self.settings.podman))

    def test_an_unstattable_session_dir_counts_as_starting(self):
        """_young decides whether a session with no metadata is starting right now or is a dead leftover;
        unknown must mean "starting", so gc does not reap a session mid-start."""
        real = os.stat

        def fake(path, *a, **kw):
            if "sessions" in str(path):
                raise OSError(errno.EACCES, "nope")
            return real(path, *a, **kw)

        with mock.patch("agentbox.session.os.stat", side_effect=fake):
            self.assertTrue(session._young(self.settings, "ab-claude-p1-1"))


if __name__ == "__main__":
    unittest.main()
