"""`make omp-live`'s reporting (`tests/live/omp_live.py`), which the first host run showed was too quiet.

The run failed with one line -- `FAIL 1. OMP answered AB_OK through the proxy` -- and nothing about which
of the three completely different things had happened:

  * the model was never reached (it is down, or its host is not allowed);
  * the model answered but slowly, and the run was cut off;
  * OMP answered something other than what was asked for.

Reading the raw sections told us stdout had been empty and that two model calls took 67s and 80s, but only
by eye. A live check that cannot say why it failed makes the next person re-run it to learn what the last
run already knew.

The other half: a timeout used to raise out of `main`, so the first attempt died on step 1 and never ran
the key-leak scan or the teardown check. It is still a FAIL naming the command -- the harness guarantee --
but one the rest of the run survives.
"""
import unittest

from tests.live.omp_live import Run, model_calls

# Real `make omp-live` proxy access-log lines (host, 4 Nov 2026): two model calls to the LAN server and
# five denied CONNECTs to provider endpoints OMP probes at startup.
ACCESS = """\
1793812697.332 10.89.5.3 CONNECT zenmux.ai:443 403 - TCP_DENIED/HIER_NONE 102 101
1793812697.332 10.89.5.3 CONNECT api.kilo.ai:443 403 - TCP_DENIED/HIER_NONE 117 116
1793812698.302 10.89.5.3 - -:- 0 - NONE_NONE/HIER_NONE 0 -
1793812764.646 10.89.5.3 POST 10.0.0.2:8080 200 10.0.0.2 TCP_MISS/HIER_DIRECT 66343 -
1793812844.039 10.89.5.3 POST 10.0.0.2:8080 200 10.0.0.2 TCP_MISS/HIER_DIRECT 79305 -
"""


class Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class WhyItFailed(unittest.TestCase):
    def test_an_empty_stdout_says_so(self):
        """What actually happened on the host: OMP reached the model twice and printed nothing."""
        why = Run(Proc(stdout="", stderr="agentbox: ...", returncode=0), 147.3).why("AB_OK")
        self.assertIn("stdout was empty", why)
        self.assertIn("exit 0", why)
        self.assertIn("147s", why)

    def test_a_wrong_answer_says_so_and_is_not_confused_with_an_empty_one(self):
        why = Run(Proc(stdout="Sure! AB_OK.\n", returncode=0), 12.0).why("AB_OK")
        self.assertIn("no AB_OK line", why)
        self.assertNotIn("empty", why)
        self.assertIn("1 line(s)", why)

    def test_a_timeout_says_so_rather_than_looking_like_a_wrong_answer(self):
        why = Run(None, 900.4, timed_out="`ab run ...` did not finish in 900s").why("AB_OK")
        self.assertIn("did not finish in time", why)
        self.assertIn("900s", why)

    def test_a_timed_out_run_still_answers_every_question_asked_of_it(self):
        """So the leak scan and the teardown check still run: `main` reads these on a timed-out run too."""
        r = Run(None, 900.0, timed_out="...")
        self.assertEqual((r.stdout, r.stderr, r.returncode), ("", "", None))


class ModelCalls(unittest.TestCase):
    def test_it_counts_the_model_requests_and_their_bytes(self):
        self.assertEqual(model_calls(ACCESS), (2, 66343 + 79305))

    def test_denied_connects_are_not_model_calls(self):
        """The five 403s are OMP probing provider endpoints it has no key for -- not the model answering.
        Counting them would have made "the model was never reached" impossible to report."""
        denied_only = "\n".join(l for l in ACCESS.splitlines() if "403" in l)
        self.assertEqual(model_calls(denied_only), (0, 0))

    def test_no_traffic_at_all_is_zero_not_a_crash(self):
        self.assertEqual(model_calls(""), (0, 0))
        self.assertEqual(model_calls("garbage\n-:- 0 - NONE_NONE/HIER_NONE 0 -\n"), (0, 0))


if __name__ == "__main__":
    unittest.main()
