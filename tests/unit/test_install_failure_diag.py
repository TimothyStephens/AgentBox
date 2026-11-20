"""A failed install reports the proxy's state, its last log lines and who is on the install network, read on the
host before teardown (19 Nov: the OMP install's proxy name stopped resolving after the proxy reported ready)."""
import unittest
from unittest import mock

from agentbox import installer
from .helpers import SettingsCase


class FailureDiag(SettingsCase):
    def test_diagnose_reads_state_logs_and_the_network(self):
        job = installer.Install(self.settings, "omp")
        calls = []

        def run(argv, check=False, **kw):
            calls.append(argv)
            out = {"inspect": "exited 137 OOMKilled=true", "logs": "squid: line a\nsquid: line b",
                   "network": '[{"name": "n", "containers": {"c1": {"name": "ab-install-omp-proxy"}, '
                              '"c2": {"name": "ab-install-omp"}}}]'}[argv[0]]
            return mock.Mock(returncode=0, stdout=out, stderr="")
        job.podman = mock.Mock(run=run)
        text = "\n".join(job.diagnose())
        self.assertIn("exited 137 OOMKilled=true", text)
        self.assertIn("squid: line b", text)
        self.assertIn("ab-install-omp ab-install-omp-proxy", text)
        self.assertTrue(any(a[0] == "inspect" and job.proxy_name in a for a in calls))
        self.assertTrue(any(a[:2] == ["network", "inspect"] and job.int_net in a and "json" in a for a in calls))


if __name__ == "__main__":
    unittest.main()
