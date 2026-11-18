"""The local model (another machine, may be off): its selftest line is SKIP with a reason when it does not answer
from the host, and a real check when it does (Tim, 1 Oct)."""
import unittest

from agentbox.selftest import runner


def build(model_live):
    b = runner.Builder()
    runner.network_cases(b, "locked", model=("192.0.2.2", 8080), lan_targets=["192.0.2.10:8088"], model_domains=[],
                         live=lambda h, p: model_live if h == "192.0.2.2" else True)
    return b


class ModelLine(unittest.TestCase):
    LABEL = "proxied local model http://192.0.2.2:8080/v1/models allowed"

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
        [(label, text)] = [(lb, t) for lb, t in found if "pypi.org" in lb]
        self.assertIn("TCP_TUNNEL/000", text)
        self.assertIn("151.101.0.223", text)
        self.assertIn("20013ms", text)
        r = by["proxied CONNECT pypi.org:443 refused (locked)"]
        self.assertIn("dns 151.101.0.223", r.detail)
        self.assertIn("proxy log for pypi.org: 1759400000.123 20001 10.89.3.5 TCP_TUNNEL/000", r.detail)

    def test_targets(self):
        b = build(True)
        targets = {runner.proxied_target(c) for c in b.cases} - {None}
        self.assertTrue({"pypi.org", "example.com", "192.0.2.2", "::ffff:192.0.2.10", "2606:4700:4700::1111"} <= targets,
                        targets)


class CliVersionLine(unittest.TestCase):
    """Regression: every Result needs a detail. A labelled image (truthy cli_ver) must PASS, not crash
    (the two-arg PASS was latent until `make claude-image` added the label, 16 Oct)."""

    def test_pass_when_version_present(self):
        from agentbox import selftest
        r = selftest.cli_version_result("claude", "2.1.266 (Claude Code)")
        self.assertEqual((r.label, r.verdict), ("claude CLI version", "PASS"))
        self.assertEqual(r.detail, "2.1.266 (Claude Code)")

    def test_skip_when_no_label(self):
        from agentbox import selftest
        r = selftest.cli_version_result("omp", "")
        self.assertEqual((r.label, r.verdict), ("omp CLI version", "SKIP"))
        self.assertIn("make omp-image", r.detail)


class GrepTree(unittest.TestCase):
    """Item 26: the host-side token search opens every file under the state, backups and logs; a FIFO there
    (no writer) blocked it forever. Only regular files are read."""

    def test_a_fifo_is_skipped_and_regular_files_are_still_searched(self):
        import os
        import tempfile
        import threading
        from agentbox import selftest
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        os.mkfifo(os.path.join(d, "pipe"))
        with open(os.path.join(d, "hit.txt"), "w") as f:
            f.write("xx tok-123 yy")
        box = []
        t = threading.Thread(target=lambda: box.append(selftest._grep_tree(d, b"tok-123")), daemon=True)
        t.start()
        t.join(5)
        self.assertTrue(box, "_grep_tree blocked on the FIFO")
        self.assertEqual(box[0], [os.path.join(d, "hit.txt")])


class ProbeProtocol(unittest.TestCase):
    """Item 27: the probe used to read its arguments with `read -r id op a b c d`, so a path with a space was
    two arguments and the case probed the wrong path. Each argument now travels base64-encoded (no spaces,
    no shell metacharacters) and probe.sh decodes it. Runs probe.sh locally on ops that need no network."""

    def test_a_path_with_a_space_reaches_the_op_whole(self):
        import os
        import shutil
        import subprocess
        import tempfile
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        sub = os.path.join(d, "dir with space")
        os.mkdir(sub)
        target = os.path.join(sub, "file name.txt")
        b = runner.Builder()
        b.add("write", [target], "write into a spaced path", runner.expect("ok"))
        b.add("exists", [target], "the spaced path exists afterwards", runner.expect("yes"))
        b.add("size", [target], "its size is 0", runner.expect("0"))
        b.add("exists", [os.path.join(d, "dir")], "no file was made at the first word", runner.expect("no"))
        for line in b.script().splitlines():
            self.assertNotIn(d, line, "arguments are encoded, not sent raw")
        p = subprocess.run(["bash", runner.PROBE], input=b.script(), capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        results = runner.score(b.cases, p.stdout)
        self.assertEqual([(r.label, r.verdict) for r in results if r.verdict != "PASS"], [], p.stdout)
        self.assertTrue(os.path.isfile(target))


class CanaryLine(unittest.TestCase):
    """Item 27: with podman access on, the cross-project cases are a guarantee of the design (design.md
    Selftest: every guarantee is checked). A canary that could not be made is a FAIL, not a SKIP that exits 0."""

    def test_missing_canary_fails(self):
        from agentbox import selftest
        r = selftest.canary_result(None)
        self.assertEqual((r.label, r.verdict), ("canary container in another project", "FAIL"))
        self.assertIn(selftest.CANARY_IMAGE, r.detail)

    def test_present_canary_is_no_line(self):
        from agentbox import selftest
        self.assertIsNone(selftest.canary_result("ab-x-canary"))


class UnconfiguredHost(unittest.TestCase):
    """model_host and lan_targets have no committed default (they are the user's network, 18 Nov): with them
    unset the selftest says so instead of probing an empty host, and the missing LAN cases are a FAIL."""

    def results(self, **kw):
        b = runner.Builder()
        runner.network_cases(b, "locked", model=kw.get("model", ("", 8080)), lan_targets=kw.get("lan", []),
                             model_domains=[], live=lambda h, p: True)
        return runner.score(b.cases, "__done__\n")

    def test_no_model_host_is_a_skip_naming_the_setting_and_nothing_probes_an_empty_host(self):
        rs = self.results(lan=["192.0.2.10:8080"])
        [m] = [r for r in rs if "local model" in r.label]
        self.assertEqual(m.verdict, "SKIP")
        self.assertIn("model_host", m.detail)
        self.assertFalse([r for r in rs if r.label.startswith(("proxied :", "direct :"))])

    def test_no_lan_targets_is_a_fail_naming_the_setting(self):
        rs = self.results(model=("192.0.2.2", 8080))
        [m] = [r for r in rs if r.label == "proxied LAN targets refused"]
        self.assertEqual(m.verdict, "FAIL")
        self.assertIn("lan_targets", m.detail)
