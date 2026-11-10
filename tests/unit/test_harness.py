"""The test harness's own guarantees (`tests/unit/helpers.py`, `tests/integration/harness.py`).

Two ways a CLI test can fail to produce a verdict, both of which happened in this project:

  * **stdin.** `ab session start` reads stdin when it has no TTY. A test whose refusal stops working then reads from
    whatever stdin the test runner had -- a terminal, in a developer's shell -- and blocks instead of
    failing. So every in-process `ab` gets an empty, already-at-EOF stdin unless it asks for otherwise.
  * **waiting.** A session that gets further than a test meant it to waits on something real
    (`_wait_proxy` polls for `ready_timeout`, 60s by default). Several of those in one run is a suite that
    looks hung. So every in-process `ab` has a deadline, and passing it is a FAIL that names the command
    and everything printed up to that point -- never a run with no verdict.

The tests below pin both, because a harness guarantee nothing checks is a guarantee that quietly lapses.
"""
import io
import os
import time
import unittest
from unittest import mock

from .helpers import AbTimeout, SettingsCase


class StdinIsClosedByDefault(SettingsCase):
    def setUp(self):
        super().setUp()
        self.project("p1", preset="script")

    def test_a_command_that_reads_stdin_sees_eof(self):
        seen = {}

        def fake_cmd(_s, _a):
            import sys
            seen["read"] = sys.stdin.read()
            seen["isatty"] = sys.stdin.isatty()
            return 0

        with mock.patch("agentbox.cli.cmd_project_ls", fake_cmd):
            code, _out = self.ab("project", "ls")
        self.assertEqual(code, 0)
        self.assertEqual(seen["read"], "", "at EOF, so a read returns rather than waiting")
        self.assertFalse(seen["isatty"])

    def test_a_test_can_still_feed_it_something(self):
        seen = {}

        def fake_cmd(_s, _a):
            import sys
            seen["read"] = sys.stdin.read()
            return 0

        with mock.patch("agentbox.cli.cmd_project_ls", fake_cmd):
            self.ab("project", "ls", stdin="yes\n")
        self.assertEqual(seen["read"], "yes\n")

    def test_a_blocking_stdin_ends_as_a_failure_not_a_hang(self):
        """The case the default prevents, with the default opted out of: a stdin that never reaches EOF.

        Without the deadline this blocks for as long as the test runner is willing to wait. With it, the
        run ends as a FAIL that names the command."""
        read_fd, write_fd = os.pipe()                 # a writer that never writes and never closes
        self.addCleanup(os.close, write_fd)
        stream = os.fdopen(read_fd)
        self.addCleanup(stream.close)

        def fake_cmd(_s, _a):
            import sys
            sys.stdin.read()                          # would never return
            return 0

        started = time.time()
        with mock.patch("agentbox.cli.cmd_project_ls", fake_cmd), mock.patch("sys.stdin", stream):
            with self.assertRaises(AbTimeout) as cm:
                self.ab("project", "ls", stdin=None, timeout=2)
        self.assertLess(time.time() - started, 30, "fast, not at the mercy of the runner's patience")
        self.assertIn("project ls", str(cm.exception), "the command is named")


class ADeadlineNotAHang(SettingsCase):
    def test_a_command_that_never_returns_is_a_failure_naming_it(self):
        def slow(_s, _a):
            print("got this far")
            time.sleep(60)
            return 0

        started = time.time()
        with mock.patch("agentbox.cli.cmd_project_ls", slow):
            with self.assertRaises(AbTimeout) as cm:
                self.ab("project", "ls", timeout=2)
        self.assertLess(time.time() - started, 30)
        msg = str(cm.exception)
        self.assertIn("project ls", msg, "which command")
        self.assertIn("did not finish in 2s", msg, "and that it ran out of time")
        self.assertIn("got this far", msg, "and what it had printed -- never a bare timeout")

    def test_the_alarm_is_cleared_afterwards(self):
        """A leaked alarm would fire in whatever test ran next, which is worse than no alarm at all."""
        import signal
        self.ab("project", "ls")
        self.assertEqual(signal.alarm(0), 0, "no alarm left pending")
        self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL,
                         "and the handler is the one we found")

    def test_a_normal_command_is_unaffected(self):
        code, out = self.ab("project", "ls")
        self.assertEqual(code, 0, out)


class TheIntegrationHarnessToo(unittest.TestCase):
    """`tests/integration/harness.py` runs `ab` as a subprocess; the same two rules, by other means."""

    def test_ab_closes_stdin_unless_input_is_given(self):
        import inspect

        from tests.integration import harness
        src = inspect.getsource(harness.Env.ab)
        self.assertIn("subprocess.DEVNULL", src)
        self.assertIn("input", src)

    def test_a_timeout_is_an_assertion_naming_the_command_and_the_output(self):
        import subprocess

        from tests.integration import harness
        env = harness.Env.__new__(harness.Env)      # no temp dirs needed: the call is mocked
        env.env, env.cfg = dict(os.environ), "/nowhere"
        boom = subprocess.TimeoutExpired(cmd="ab session start", timeout=7, output="partial out", stderr="partial err")
        with mock.patch("subprocess.run", side_effect=boom):
            with self.assertRaises(AssertionError) as cm:
                env.ab("session", "start", "claude", "web", timeout=7)
        msg = str(cm.exception)
        self.assertIn("ab session start claude web", msg)
        self.assertIn("did not finish in 7s", msg)
        self.assertIn("partial out", msg)
        self.assertIn("partial err", msg)
        self.assertNotIn("TimeoutExpired", msg, "a verdict, not a traceback")


if __name__ == "__main__":
    unittest.main()
