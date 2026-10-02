"""The local model (another machine, may be off): its selftest line is SKIP with a reason when it does not answer
from the host, and a real check when it does (Tim, 1 Oct)."""
import unittest

from agentbox.selftest import runner


def build(model_live):
    b = runner.Builder()
    runner.network_cases(b, "locked", model=("10.0.0.2", 8080), lan_targets=["10.0.0.103:8088"], model_domains=[],
                         live=lambda h, p: model_live if h == "10.0.0.2" else True)
    return b


class ModelLine(unittest.TestCase):
    LABEL = "proxied local model http://10.0.0.2:8080/v1/models allowed"

    def result(self, model_live):
        b = build(model_live)
        return [r for r in runner.score(b.cases, "__done__\n") if r.label == self.LABEL]

    def test_unreachable_model_is_a_skip_with_a_reason(self):
        [r] = self.result(False)
        self.assertEqual(r.verdict, "SKIP")
        self.assertIn("not answering from the host", r.detail)

    def test_reachable_model_is_checked(self):
        [r] = self.result(True)
        self.assertEqual(r.verdict, "FAIL", "no probe answer: the line is a real check, not a skip")


if __name__ == "__main__":
    unittest.main()
