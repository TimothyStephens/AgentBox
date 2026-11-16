"""ab.log times every podman call (16 Nov, for profiling): a start line with a millisecond timestamp, and an
end line with the elapsed milliseconds and the exit code, so the log alone says which podman commands a
session start or teardown spends its time in."""
import json
import os
import re
import sys
import tempfile
import unittest

from agentbox import podman as podman_mod
from agentbox.podman import Podman

START = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} (\[.*\])$")
END = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} = (\d+)ms rc=(-?\d+|timeout|error) (\[.*\])$")


class PodmanLogTiming(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="abt-unit-log-")
        self.log = os.path.join(d, "logs", "ab.log")
        old = podman_mod._LOG
        podman_mod.set_log(self.log)
        self.addCleanup(podman_mod.set_log, old)
        # A stand-in binary: python itself, so `run` really executes something that takes measurable time.
        self.p = Podman(sys.executable)

    def lines(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def test_a_call_logs_a_start_and_an_end_line_with_its_duration_and_exit_code(self):
        self.p.run(["-c", "import time, sys; time.sleep(0.05); sys.exit(3)"], check=False)
        start, end = self.lines()
        argv = [sys.executable, "-c", "import time, sys; time.sleep(0.05); sys.exit(3)"]
        self.assertEqual(json.loads(START.match(start).group(1)), argv)
        m = END.match(end)
        self.assertIsNotNone(m, end)
        self.assertGreaterEqual(int(m.group(1)), 50)
        self.assertEqual(m.group(2), "3")
        self.assertEqual(json.loads(m.group(3)), argv[1:3])

    def test_a_timeout_is_logged_as_one_before_the_error(self):
        with self.assertRaises(Exception):
            self.p.run(["-c", "import time; time.sleep(5)"], timeout=0.2)
        self.assertRegex(self.lines()[-1], r" = \d+ms rc=timeout ")

    def test_interactive_calls_are_timed_too(self):
        self.p.interactive(["-c", "pass"])
        self.assertRegex(self.lines()[-1], r" = \d+ms rc=0 ")


if __name__ == "__main__":
    unittest.main()
