"""ab session preview (built 2 Oct after trial 2): 127.0.0.1 only, one session service, removed with the session."""
import subprocess
import unittest

from agentbox import preview
from agentbox.errors import AgentboxError


class Settings:
    oom_score_adj = 500
    cgroup_parent = "agentbox.slice"
    preview_image = "docker.io/library/busybox:1.36"


class FakePodman:
    def __init__(self, containers):
        self.containers = containers
        self.calls = []

    def containers_json(self, labels):
        return [c for c in self.containers if all((c.get("Labels") or {}).get(k) == v for k, v in labels.items())]

    def run(self, argv, check=False):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


DEV = {"agentbox.session": "ab-claude-p-1", "agentbox.role": "dev"}


class Preview(unittest.TestCase):
    def test_forwarder_publishes_on_127_0_0_1_only(self):
        args = preview.forwarder_args(Settings(), "p", "ab-claude-p-1", "p_web_1", 18123, 8000,
                                      {"agentbox.session": "ab-claude-p-1", "agentbox.role": "preview"})
        published = [a for a in args if a.startswith(("--publish", "-p=", "--publish-all", "-P"))]
        self.assertEqual(published, ["--publish=127.0.0.1:18123:8080"])
        self.assertNotIn("0.0.0.0", " ".join(args))
        self.assertIn("--network=ab-claude-p-1-int", args, "the session's internal network only")
        self.assertEqual(sum(a.startswith("--network") for a in args), 1)
        for want in ("--cap-drop=ALL", "--read-only", "--security-opt=no-new-privileges",
                     "--label=agentbox.session=ab-claude-p-1", "--cgroup-parent=agentbox.slice"):
            self.assertIn(want, args)
        self.assertEqual(args[-6:], ["-p", "8080", "-e", "nc", "p_web_1", "8000"])
        for bad in (80, 70000):
            with self.assertRaises(AgentboxError):
                preview.forwarder_args(Settings(), "p", "s", "c", bad, 8000, {})

    def test_targets(self):
        self.assertEqual(preview.parse_target("web:8000"), ("web", 8000))
        for bad in ("web", "web:", ":80", "web:0", "web:99999", "we b:80", "web:8o"):
            with self.subTest(bad=bad), self.assertRaises(AgentboxError):
                preview.parse_target(bad)

    def test_only_the_sessions_own_running_dev_service(self):
        pm = FakePodman([
            {"Names": ["p_web_1"], "State": "running", "Labels": dict(DEV, **{"com.docker.compose.service": "web"})},
            {"Names": ["p_db_1"], "State": "exited", "Labels": dict(DEV, **{"com.docker.compose.service": "db"})},
            {"Names": ["live_web"], "State": "running", "Labels": {"com.docker.compose.service": "web"}},
            {"Names": ["ab-claude-p-1-proxy"], "State": "running",
             "Labels": {"agentbox.session": "ab-claude-p-1", "agentbox.role": "proxy"}}])
        self.assertEqual(preview.find_service(pm, "ab-claude-p-1", "web"), "p_web_1")
        self.assertEqual(preview.find_service(pm, "ab-claude-p-1", "p_web_1"), "p_web_1")
        for svc in ("db", "ab-claude-p-1-proxy", "live_web", "nothere"):
            with self.subTest(svc=svc), self.assertRaises(AgentboxError):
                preview.find_service(pm, "ab-claude-p-1", svc)

    def test_free_port_is_on_loopback(self):
        self.assertTrue(1024 <= preview.free_port() <= 65535)


if __name__ == "__main__":
    unittest.main()
