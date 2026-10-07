"""Step 1 gate: filesystem and posture selftest cases, against real podman and the real Claude image."""
import json
import os
import unittest

from . import harness
from .harness import FAKE_TOKEN, LABEL, podman

ENV = None


def setUpModule():
    global ENV
    ENV = harness.Env()
    ENV.make_project("p1", extra=["--data-ro", _mk("raw"), "--data-rw", _mk("out")])
    ENV.make_project("p2")
    ENV.make_project("locked1", preset="lockdown")
    print(f"\n[itest run {harness.RUN}: {harness.BASE}, slice={'on' if ENV.slice else 'off'}]")


def tearDownModule():
    harness.cleanup()


def _mk(name):
    d = os.path.join(harness.BASE, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "sample.csv"), "w") as f:
        f.write("a,b\n1,2\n")
    return d


class Step1(unittest.TestCase):
    maxDiff = None

    def assertSelftest(self, project):
        results, p = ENV.selftest("claude", project)
        print(f"\nselftest claude/{project}:\n{harness.report(results)}")
        fails = [r for r in results if r["verdict"] == "FAIL"]
        self.assertFalse(fails, harness.report(fails) + "\n" + p.stderr[-2000:])
        self.assertEqual(p.returncode, 0)
        self.assertNotIn(FAKE_TOKEN, p.stdout + p.stderr)
        return results

    def test_selftest_python_preset(self):
        results = self.assertSelftest("p1")
        labels = " | ".join(r["label"] for r in results)
        for want in ("posture", ".git/hooks", "read-only data", "masked", ".env shows .env.dev",
                     "other project p2 root not visible", "model token and keys not on disk"):
            self.assertIn(want, labels)

    def test_selftest_lockdown(self):
        self.assertSelftest("locked1")

    def test_agent_runs_as_host_user_with_token(self):
        p = ENV.ab("run", "--agent", "claude", "--project", "p2", "--shell", "--no-tty", "--",
                   "-c", 'id -u; test -n "$CLAUDE_CODE_OAUTH_TOKEN" && echo TOKEN_SET; echo "HOME=$HOME"; pwd',
                   timeout=300)
        lines = p.stdout.split()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(str(os.getuid()), lines)
        self.assertIn("TOKEN_SET", lines)
        self.assertIn(f"HOME={os.path.expanduser('~')}", lines)
        self.assertIn(os.path.join(ENV.projects, "p2"), lines)
        self.assertNotIn(FAKE_TOKEN, p.stdout + p.stderr)

    def test_agent_can_commit(self):
        root = os.path.join(ENV.projects, "p2")
        p = ENV.ab("run", "--agent", "claude", "--project", "p2", "--shell", "--no-tty", "--",
                   "-c", "echo change >> README.agent && git add README.agent && git commit -qm agent-commit && "
                         "git log -1 --format='%s|%cn'", timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("agent-commit|", p.stdout)
        self.assertIn(" via agentbox-claude", p.stdout)
        hooks = os.listdir(os.path.join(root, ".git", "hooks"))
        self.assertFalse([h for h in hooks if not h.endswith(".sample")], "no hook was added")

    def test_claude_starts_in_sandbox(self):
        p = ENV.ab("run", "--agent", "claude", "--project", "p2", "--no-tty", "--", "--version", timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Claude Code", p.stdout)

    def test_token_not_in_podman_config_and_kill(self):
        proc = ENV.ab_bg("run", "--agent", "claude", "--project", "p2", "--shell", "--no-tty", "--",
                         "-c", "sleep 120")
        try:
            name = harness.wait_for(lambda: podman("ps", "--format", "{{.Names}}", "--filter", f"label={LABEL}",
                                                   "--filter", "label=agentbox.project=p2",
                                                   "--filter", "label=agentbox.role=agent").stdout.split())
            self.assertTrue(name, "session container did not start")
            name = name[0]
            inspect = podman("inspect", name).stdout
            self.assertNotIn(FAKE_TOKEN, inspect)
            doc = json.loads(inspect)[0]
            env_names = [e.split("=", 1)[0] for e in doc["Config"]["Env"]]
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env_names)
            self.assertEqual(doc["HostConfig"]["OomScoreAdj"], 500)
            self.assertEqual(doc["HostConfig"]["Memory"], 4 * 1024 ** 3)
            if ENV.slice:
                self.assertIn("agentbox.slice", doc["HostConfig"].get("CgroupParent", ""))
            ls = ENV.ab("session", "ls").stdout
            self.assertIn(name, ls)
            self.assertIn("running", ls)
            k = ENV.ab("session", "kill", name)
            self.assertEqual(k.returncode, 0, k.stderr)
            proc.wait(timeout=120)
            self.assertFalse(harness.wait_for(lambda: name not in " ".join(harness.leftovers()) or None,
                                              timeout=60) is None)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

    def test_sigterm_tears_the_session_down(self):
        import signal
        proc = ENV.ab_bg("run", "--agent", "claude", "--project", "p2", "--shell", "--no-tty", "--",
                         "-c", "sleep 300")
        try:
            up = harness.wait_for(lambda: podman("ps", "--format", "{{.Names}}", "--filter", f"label={LABEL}",
                                                 "--filter", "label=agentbox.project=p2",
                                                 "--filter", "label=agentbox.role=agent").stdout.split())
            self.assertTrue(up, "session did not start")
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=120)
            left = [x for x in harness.leftovers() if x.startswith(up[0])]
            self.assertEqual(left, [], "everything the session created is gone after SIGTERM")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

    def test_refusals(self):
        bad = ENV.ab("project", "add", "home", os.path.expanduser("~"), "--preset", "python")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("$HOME", bad.stderr)
        overlap = ENV.ab("project", "add", "p1sub", os.path.join(ENV.projects, "p1", "sub"), "--preset", "python")
        self.assertNotEqual(overlap.returncode, 0)
        self.assertIn("overlaps project p1", overlap.stderr)
        loosen = ENV.ab("project", "add", "l2", _mk("l2root"), "--preset", "lockdown", "--egress", "open")
        self.assertNotEqual(loosen.returncode, 0)
        self.assertIn("ceiling", loosen.stderr)

    def test_project_show(self):
        out = ENV.ab("project", "show", "p1", check=True).stdout
        self.assertIn("egress         packages", out)
        self.assertIn("from preset python", out)

    @unittest.skipUnless(harness.slice_installed(), "agentbox.slice not installed (make install-slice)")
    def test_slice_contains_the_session(self):
        # Seen from the host: inside, the private cgroup namespace shows only "0::/".
        proc = ENV.ab_bg("run", "--agent", "claude", "--project", "p2", "--shell", "--no-tty", "--",
                         "-c", "sleep 120")
        try:
            name = harness.wait_for(lambda: podman("ps", "--format", "{{.Names}}", "--filter", f"label={LABEL}",
                                                   "--filter", "label=agentbox.project=p2",
                                                   "--filter", "label=agentbox.role=agent").stdout.split())
            self.assertTrue(name, "session container did not start")
            pid = podman("inspect", "-f", "{{.State.Pid}}", name[0]).stdout.strip()
            with open(f"/proc/{pid}/cgroup") as f:
                cg = f.read().strip()
            print(f"\nsession {name[0]} pid {pid}: {cg}")
            self.assertIn("/agentbox.slice/", cg)
            slice_dir = cg.split("::", 1)[1].split("/agentbox.slice/")[0] + "/agentbox.slice"
            with open(f"/sys/fs/cgroup{slice_dir}/memory.max") as f:
                self.assertEqual(int(f.read()), 4400 * 1024 * 1024)
            with open(f"/sys/fs/cgroup{slice_dir}/memory.swap.max") as f:
                self.assertEqual(f.read().strip(), "0")
            ENV.ab("session", "kill", name[0])
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
