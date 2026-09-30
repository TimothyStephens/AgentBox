"""Step 2 gate: the per-session network and squid, proxied and direct cases in every egress mode."""
import os
import unittest

from . import harness
from .harness import FAKE_TOKEN, LABEL, podman

ENV = None
MODES = ("locked", "packages", "open")


def setUpModule():
    global ENV
    ENV = harness.Env()
    for mode in MODES:
        ENV.make_project(f"n-{mode}", extra=["--egress", mode] if mode != "packages" else
                         ["--extra-domain", "example.org"])
    print(f"\n[itest run {harness.RUN}: {harness.BASE}, slice={'on' if ENV.slice else 'off'}]")


def tearDownModule():
    harness.cleanup()


def session_container(project, role="agent"):
    return podman("ps", "--format", "{{.Names}}", "--filter", f"label={LABEL}",
                  "--filter", f"label=agentbox.project={project}", "--filter", f"label=agentbox.role={role}").stdout.split()


def ip_on(container, network):
    return podman("inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
                  container).stdout.strip()


class Step2(unittest.TestCase):
    maxDiff = None

    def check_mode(self, mode):
        results, p = ENV.selftest("claude", f"n-{mode}")
        print(f"\nselftest claude/n-{mode}:\n{harness.report(results)}")
        fails = [r for r in results if r["verdict"] == "FAIL"]
        self.assertFalse(fails, harness.report(fails) + "\n" + p.stderr[-2000:])
        self.assertNotIn(FAKE_TOKEN, p.stdout + p.stderr)
        labels = " | ".join(r["label"] for r in results)
        self.assertIn("proxied LAN", labels)
        self.assertIn("direct DNS lookup", labels)
        logs = os.path.join(ENV.data, "logs")
        proxy_logs = [f for f in os.listdir(logs) if f.startswith(f"abt-{harness.RUN}-claude-n-{mode}-") and
                      f.endswith("-proxy.log")]
        self.assertTrue(proxy_logs, "proxy access log kept")
        with open(os.path.join(logs, proxy_logs[-1])) as f:
            text = f.read()
        self.assertIn(" 403 - TCP_DENIED", text)
        self.assertNotIn("http://", text, "the kept log holds destinations (host:port), not URLs")
        self.assertIn(" GET example.com:80 ", text)
        self.assertNotIn(FAKE_TOKEN, text)

    def test_reused_session_names_keep_separate_logs(self):
        """claude-live, 30 Sep: two sessions in a row reuse one name; the second session's proxy log was
        appended to the first one's file and looked empty to the live test."""
        logs = os.path.join(ENV.data, "logs")
        before = set(os.listdir(logs)) if os.path.isdir(logs) else set()
        for _ in range(2):
            p = ENV.ab("run", "--agent", "claude", "--project", "n-open", "--shell", "--no-tty", "--",
                       "-c", 'hp=${HTTPS_PROXY#http://}; hp=${hp%/}; exec 3<>"/dev/tcp/${hp%:*}/${hp##*:}"; '
                       "printf 'GET http://example.com/ HTTP/1.0\\r\\nHost: example.com\\r\\n\\r\\n' >&3; "
                       "head -1 <&3", timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr)
        new = sorted(f for f in set(os.listdir(logs)) - before if f.endswith("-proxy.log"))
        print("\nnew proxy logs:", new)
        self.assertEqual(len(new), 2, "one log file per session")
        for fn in new:
            with open(os.path.join(logs, fn)) as f:
                self.assertIn("example.com:80", f.read(), fn)

    def test_locked(self):
        self.check_mode("locked")

    def test_packages(self):
        self.check_mode("packages")

    def test_open(self):
        self.check_mode("open")

    def test_sessions_cannot_reach_each_other(self):
        b = ENV.ab_bg("run", "--agent", "claude", "--project", "n-open", "--shell", "--no-tty", "--",
                      "-c", "sleep 150")
        try:
            agent_b = harness.wait_for(lambda: session_container("n-open"), timeout=120)
            self.assertTrue(agent_b, "session B did not start")
            proxy_b = session_container("n-open", "proxy")[0]
            int_b = f"{agent_b[0]}-int"
            targets = [(ip_on(agent_b[0], int_b), 22), (ip_on(proxy_b, int_b), 3128),
                       (ip_on(proxy_b, f"{agent_b[0]}-egr"), 3128)]
            self.assertTrue(all(t[0] for t in targets), targets)
            script = "; ".join(f"(timeout 6 bash -c 'exec 3<>/dev/tcp/{h}/{p}' 2>/dev/null && echo LEAK {h}:{p})"
                               for h, p in targets) + "; echo DONE"
            p = ENV.ab("run", "--agent", "claude", "--project", "n-locked", "--shell", "--no-tty", "--",
                       "-c", script, timeout=300)
            self.assertIn("DONE", p.stdout, p.stderr)
            self.assertNotIn("LEAK", p.stdout)
            # and B's proxy through A's proxy is refused too (a private address)
            q = ENV.ab("run", "--agent", "claude", "--project", "n-locked", "--shell", "--no-tty", "--",
                       "-c", f"curl_like() {{ exec 3<>/dev/tcp/${{HTTP_PROXY#http://}}; }}; "
                             f"hp=${{HTTP_PROXY#http://}}; hp=${{hp%/}}; exec 3<>/dev/tcp/${{hp%:*}}/${{hp##*:}}; "
                             f"printf 'GET http://{targets[1][0]}:3128/ HTTP/1.1\\r\\nHost: x\\r\\nConnection: close\\r\\n\\r\\n' >&3; "
                             f"head -1 <&3", timeout=300)
            self.assertIn(" 403 ", q.stdout, q.stdout + q.stderr)
            ENV.ab("kill", agent_b[0])
        finally:
            if b.poll() is None:
                b.kill()
            b.communicate()

    def test_agent_env_has_proxy_not_model_host(self):
        p = ENV.ab("run", "--agent", "claude", "--project", "n-locked", "--shell", "--no-tty", "--",
                   "-c", 'echo "P=$HTTPS_PROXY"; echo "N=$NO_PROXY"', timeout=300)
        self.assertRegex(p.stdout, r"P=http://abt-\S+-proxy:3128")
        self.assertNotIn("10.0.0.2", p.stdout.split("N=")[1])

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
