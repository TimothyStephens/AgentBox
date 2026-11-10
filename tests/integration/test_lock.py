"""One live session per project, against real podman and a real terminal.

The unit tests drive the lock through the fake podman. These are the cases that need the real thing: a
session started **on a pty**, so `ab session start` takes its interactive path, and then the three ways it can end.

(b) is the one that matters. A closed terminal -- an ssh connection dropped, a tmux pane killed -- is how a
session usually dies, and it arrives as SIGHUP to a launcher that is attached to a tty. If that path leaked,
every dropped connection would leave a project locked and its containers running.

The signal is sent explicitly rather than by closing the pty master: the launcher runs in its own session
(so the test runner's own signals cannot reach it), which means there is no controlling terminal for the
kernel to hang up. Same signal, same handler, no race.
"""
import json
import os
import signal
import unittest

from . import harness
from .harness import LABEL, podman

ENV = None


def setUpModule():
    global ENV
    ENV = harness.Env()
    ENV.make_project("lk1")
    ENV.make_project("lk2")
    print(f"\n[itest run {harness.RUN}: lock cases in {harness.BASE}]")


def tearDownModule():
    harness.cleanup()


def lock_path(project):
    return os.path.join(ENV.data, "projects", project, "session.lock")


def read_lock(project):
    try:
        with open(lock_path(project)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def containers_of(project, role=None):
    """By LABEL, not by name: the agent container is named after the SESSION itself (`<session>`), and only
    the sidecars take a suffix (`<session>-proxy`). Looking for a `-agent` name finds nothing."""
    args = ["ps", "-a", "--format", "{{.Names}}", "--filter", f"label={LABEL}",
            "--filter", f"label=agentbox.project={project}"]
    if role:
        args += ["--filter", f"label=agentbox.role={role}"]
    return podman(*args).stdout.split()


def run_shell(project, script, timeout=300):
    """`ab session start --shell -- -c <script>`, in one place so the form cannot drift.

    `--shell` is the point: without it the passthrough goes to `claude` itself, which then tries to
    authenticate with the harness's fake token and dies with a 401 -- which is what three of these tests
    did on their first real run."""
    return ENV.ab("session", "start", "claude", project, "--shell", "--no-tty", "--",
                  "-c", script, timeout=timeout)


class Lock(unittest.TestCase):
    maxDiff = None

    def start(self, project="lk1"):
        """A session on a pty, running until it is signalled, with the lock taken."""
        proc, master = ENV.ab_pty("session", "start", "claude", project, "--shell", "--",
                                  "-c", "echo ABT_SESSION_UP; sleep 300")
        self.addCleanup(self._cleanup, proc, master, project)
        text = ENV.drain(master, until="ABT_SESSION_UP", timeout=240)
        self.assertIn("ABT_SESSION_UP", text, f"the session did not start on its pty:\n{text}")
        doc = harness.wait_for(lambda: read_lock(project), timeout=60)
        self.assertTrue(doc and doc.get("session"), f"no lock was taken: {doc}")
        return proc, master, doc["session"]

    def _cleanup(self, proc, master, project):
        if proc.poll() is None:
            proc.send_signal(signal.SIGHUP)
            try:
                proc.wait(timeout=120)
            except Exception:
                proc.kill()
        try:
            os.close(master)
        except OSError:
            pass
        ENV.ab("session", "kill", project)                  # whatever is left, named by project
        harness.wait_for(lambda: not containers_of(project) or None, timeout=60)

    # (a)

    def test_a_second_run_is_refused_while_one_is_live(self):
        _proc, _master, sid = self.start()
        p = run_shell("lk1", "echo SHOULD_NOT_RUN", timeout=240)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertNotIn("SHOULD_NOT_RUN", p.stdout, "refused before the agent ran")
        said = p.stdout + p.stderr
        self.assertIn(sid, said, "the refusal names the session that holds it")
        self.assertIn("ab session attach lk1", said)
        self.assertIn("ab session kill lk1", said)
        self.assertEqual(read_lock("lk1")["session"], sid, "and the holder's lock is untouched")

    def test_another_project_is_unaffected(self):
        """The lock is per project, not a global one."""
        self.start("lk1")
        p = run_shell("lk2", "echo OTHER_OK")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("OTHER_OK", p.stdout)
        self.assertIsNone(read_lock("lk2"), "and it released its own lock on the way out")

    # (b) -- the closed-terminal path

    def test_sighup_releases_the_lock_and_leaves_nothing(self):
        proc, master, sid = self.start()
        proc.send_signal(signal.SIGHUP)
        proc.wait(timeout=180)
        self.assertTrue(harness.wait_for(lambda: read_lock("lk1") is None or None, timeout=60),
                        f"SIGHUP left the lock behind: {read_lock('lk1')}")
        left = [x for x in harness.leftovers() if x.startswith(sid)]
        self.assertEqual(left, [], "and nothing of the session is left running")
        again = run_shell("lk1", "echo AFTER_HUP")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("AFTER_HUP", again.stdout, "the project is usable again straight away")

    def test_the_logs_and_the_scan_survive_a_sighup(self):
        """The teardown that releases the lock is the same one that keeps the session's record."""
        proc, _master, sid = self.start()
        proc.send_signal(signal.SIGHUP)
        proc.wait(timeout=180)
        logs = os.listdir(os.path.join(ENV.data, "logs"))
        self.assertTrue([n for n in logs if n.startswith(sid) and n.endswith("-scan.log")], logs)

    # (c)

    def test_sigkill_leaves_a_husk_that_the_next_run_reports(self):
        """SIGKILL cannot run a handler, so the lock stays and the session's containers stay up.

        Two steps, because that is the shape of it: while the AGENT is still running the next `ab session start` is
        refused (a live session is never reaped out from under anyone) -- and once the agent is gone, what
        is left is a husk, which the next run reaps and reports before starting."""
        proc, master, sid = self.start()
        proc.kill()
        proc.wait(timeout=60)
        os.close(master)
        self.assertEqual(read_lock("lk1")["session"], sid, "the lock is still held")

        refused = run_shell("lk1", "echo SHOULD_NOT_RUN", timeout=240)
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("ab session kill lk1", refused.stdout + refused.stderr)

        agent = containers_of("lk1", role="agent")
        self.assertTrue(agent, containers_of("lk1"))
        podman("rm", "-f", "-t", "1", *agent)               # the agent exits; the husk remains
        after = run_shell("lk1", "echo AFTER_REAP")
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        said = after.stdout + after.stderr
        self.assertIn(f"reaped session {sid}", said)
        self.assertIn("container(s)", said, "and says what it removed")
        self.assertIn("AFTER_REAP", after.stdout)

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
