"""Every install script first waits for its proxy's name to resolve, and fails with what the container saw when it
never does (19 Nov: `ab profile install omp` failed 4 of 4 with `Could not resolve proxy: ab-install-omp-proxy` on
its first git clone, while the same proxy answered `--resolve` and the image resolved names in a bare test)."""
import os
import subprocess
import tempfile
import unittest

from agentbox import installer
from .helpers import SettingsCase


class ProxyWait(SettingsCase):
    def script_of(self, agent, **kw):
        job = installer.Install(self.settings, agent)
        args = job.container_args("echo BODY\n", **kw)
        return job, args[-1]

    def test_every_agent_and_resolve_waits_for_the_proxy_first(self):
        for agent in ("claude", "omp", "pi"):
            for kw in ({}, {"mount_tree": False}):
                with self.subTest(agent=agent, **kw):
                    job, script = self.script_of(agent, **kw)
                    self.assertIn(f"getent hosts {job.proxy_name}", script)
                    self.assertLess(script.index("getent hosts"), script.index("echo BODY"))

    def run_preamble(self, resolves_after):
        """Run the real preamble with a fake `getent` that succeeds on its Nth call; `sleep` is a no-op."""
        job, script = self.script_of("omp")
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        count = os.path.join(d, "n")
        with open(os.path.join(d, "getent"), "w") as f:
            f.write(f"#!/bin/sh\nn=$(cat {count} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {count}\n"
                    f"[ $n -ge {resolves_after} ]\n")
        with open(os.path.join(d, "sleep"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        for n in ("getent", "sleep"):
            os.chmod(os.path.join(d, n), 0o755)
        env = dict(os.environ, PATH=d + ":" + os.environ["PATH"])
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env)

    def test_a_late_name_is_waited_for_and_said(self):
        p = self.run_preamble(resolves_after=3)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("BODY", p.stdout)
        self.assertIn("resolved after", p.stderr)

    def test_a_name_that_never_resolves_fails_with_what_the_container_saw(self):
        p = self.run_preamble(resolves_after=10_000)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("BODY", p.stdout)
        self.assertIn("does not resolve", p.stderr)
        self.assertIn("resolv.conf", p.stderr)


if __name__ == "__main__":
    unittest.main()
