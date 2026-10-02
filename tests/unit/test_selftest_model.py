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


class EgressDiagnostics(unittest.TestCase):
    """Tim, 2 Oct: no retries; a failed proxied check records the proxy's log lines for its host, the DNS
    answer and the time taken, so the next occurrence of the flake can be diagnosed."""

    def test_failed_proxied_check_is_diagnosed(self):
        b = build(True)
        ids = {c.label: c.id for c in b.cases}
        pypi = ids["proxied CONNECT pypi.org:443 refused (locked)"]
        ex = ids["proxied CONNECT example.com:443 refused (locked)"]
        out = f"{pypi} 000 20013ms\n{ex} 403 4ms\n__done__ ok\n"
        results = runner.score(b.cases, out)
        by = {r.label: r for r in results}
        self.assertEqual(by["proxied CONNECT pypi.org:443 refused (locked)"].detail, "000 after 20013ms")
        self.assertEqual(by["proxied CONNECT example.com:443 refused (locked)"].verdict, "PASS")
        log = "1759400000.123 20001 10.89.3.5 TCP_TUNNEL/000 0 CONNECT pypi.org:443 - HIER_NONE/- -\nother line\n"
        found = runner.diagnose_proxied_failures(b.cases, results, log, resolve=lambda h: ["151.101.0.223"])
        [(label, text)] = [(l, t) for l, t in found if "pypi.org" in l]
        self.assertIn("TCP_TUNNEL/000", text)
        self.assertIn("151.101.0.223", text)
        self.assertIn("20013ms", text)
        r = by["proxied CONNECT pypi.org:443 refused (locked)"]
        self.assertIn("dns 151.101.0.223", r.detail)
        self.assertIn("proxy log for pypi.org: 1759400000.123 20001 10.89.3.5 TCP_TUNNEL/000", r.detail)

    def test_targets(self):
        b = build(True)
        targets = {runner.proxied_target(c) for c in b.cases} - {None}
        self.assertTrue({"pypi.org", "example.com", "10.0.0.2", "::ffff:10.0.0.103", "2606:4700:4700::1111"} <= targets,
                        targets)
