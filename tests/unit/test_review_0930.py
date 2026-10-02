"""Findings of the 30 Sep review of steps 1-2 (docs/reviews/2026-09-30-steps-1-2.md), one test per finding."""
import json
import time
import os
import subprocess
from unittest import mock

from agentbox import egress, hardening, mounts, registry, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, TempCase


class F3HostAddressesFailClosed(SettingsCase):
    """#3: if the VM's addresses cannot be enumerated, the proxy must not start without a host-address rule."""

    def setUp(self):
        super().setUp()
        self.project("p1")
        self.token()

    def make(self):
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare()
        return s

    def test_enumeration_failure_refuses(self):
        s = self.make()
        with mock.patch("agentbox.egress.subprocess.run", side_effect=OSError("no ip")), \
             mock.patch("agentbox.egress.socket.getaddrinfo", side_effect=OSError("no dns")):
            self.assertEqual(egress.host_addresses(), [])
            self.settings.values["host_addrs"] = []
            with self.assertRaises(AgentboxError):
                s.squid_conf()

    def test_static_host_addresses_always_denied(self):
        s = self.make()
        with mock.patch("agentbox.egress.host_addresses", return_value=[]):
            text = s.squid_conf()
        acl = next(line for line in text.splitlines() if line.startswith("acl hostaddr"))
        self.assertIn("10.0.0.103", acl.split())


class F4IPv4OnlyNetworks(SettingsCase):
    """#4: the session networks are IPv4-only by decision, so no IPv6 destination is routable from the proxy.
    (A 2000::/3 deny acl is not used: squid's dst acl matches if ANY address of a name matches, so it
    would refuse every dual-stack host.) The integration test proves a proxied IPv6 literal fails."""

    def test_networks_created_without_ipv6(self):
        self.project("p1")
        self.token()
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare()
        for internal in (True, False):
            args = s.net_create_args("n", internal=internal)
            self.assertIn("--ipv6=false", args)
        text = s.squid_conf()
        acl = next(line for line in text.splitlines() if line.startswith("acl private dst"))
        self.assertNotIn("2000::/3", acl.split())


class F6NestedRepos(TempCase):
    """Found while testing #6: a nested repo anywhere in the grant (vendored checkout, second clone) had
    writable hooks, because only the root's .git was protected."""

    def test_nested_repo_hooks_readonly(self):
        root = self.mkdir("proj")
        subprocess.run(["git", "init", "-q", root], check=True)
        sub = os.path.join(root, "vendor", "lib")
        subprocess.run(["git", "init", "-q", sub], check=True)
        bare = os.path.join(root, "mirror.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        m = {x.dst: x for x in mounts.plan(root=root).mounts}
        for g in (os.path.join(root, ".git"), os.path.join(sub, ".git"), bare):
            self.assertTrue(m[os.path.join(g, "hooks")].ro, g)
            self.assertTrue(m[os.path.join(g, "config")].ro, g)


class F6GitFileOverlays(TempCase):
    """#6: a .git *file* whose real git dir is inside a grant must still get read-only hooks and config."""

    def test_gitdir_inside_grant(self):
        root = self.mkdir("proj")
        real = self.mkdir("proj", "gitstore")
        subprocess.run(["git", "init", "-q", "--separate-git-dir", real, os.path.join(root, "wt")], check=True)
        wt = os.path.join(root, "wt")
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
        m = {x.dst: x for x in mounts.plan(root=root).mounts}
        # the worktree lives inside the grant: its real git dir must be protected
        self.assertTrue(m[os.path.join(real, "hooks")].ro)
        self.assertTrue(m[os.path.join(real, "config")].ro)

    def test_project_root_is_a_gitfile_checkout(self):
        base = self.mkdir("b")
        real = self.mkdir("b", "store")
        subprocess.run(["git", "init", "-q", "--separate-git-dir", real, os.path.join(base, "proj")], check=True)
        proj = os.path.join(base, "proj")
        # the real git dir is outside the grant: nothing to overlay, and the note says why
        plan = mounts.plan(root=proj)
        self.assertFalse([x for x in plan.mounts if x.dst.startswith(real)])
        self.assertTrue(any("not mounted" in n for n in plan.notes))
        # inside the grant: protected
        plan = mounts.plan(root=base)
        m = {x.dst: x for x in plan.mounts}
        self.assertTrue(m[os.path.join(real, "hooks")].ro)


class F7GcKeepsRunningSessions(SettingsCase):
    """#7: gc must not tear down a session whose containers are running, even if its metadata is gone."""

    def setUp(self):
        super().setUp()
        self.project("p1")
        self.token()

    def test_running_container_without_meta_survives(self):
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare()
        os.unlink(os.path.join(self.settings.session_run_dir(s.name), "session.json"))
        ps = [{"Names": [s.name], "State": "running",
               "Labels": {"agentbox.session": s.name, "agentbox.role": "agent", "agentbox.project": "p1"}}]
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(ps)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        self.assertEqual(session.gc(self.settings, s.podman), [])

    def test_meta_written_atomically(self):
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        with mock.patch("agentbox.session.os.replace", wraps=os.replace) as rep:
            s.prepare()
        self.assertTrue(any(str(c.args[1]).endswith("session.json") for c in rep.call_args_list))


class F8SwapAndMountDestinations(SettingsCase):
    """#8: swap names must be plain file names, and the audit must check mount destinations too."""

    def test_swap_traversal_refused(self):
        self.project("p1", swap={"../../../tmp/abt-escape": ".env.dev"})
        with self.assertRaises(AgentboxError):
            session.load_project(self.settings, "p1")
        self.project("p2", swap={".env": "../outside.env"})
        with self.assertRaises(AgentboxError):
            session.load_project(self.settings, "p2")

    def test_audit_checks_destinations(self):
        base = hardening.fixed_flags(self.settings, cgroup_parent="") + ["img"]
        ok = ["--mount=type=bind,src=/proj/.env.dev,dst=/proj/.env"]
        hardening.audit(ok + base, allowed_mount_srcs=["/proj"], networks=[], allowed_mount_dsts=["/proj"])
        for bad in ("--mount=type=bind,src=/proj/.env.dev,dst=/etc/cron.d/x",
                    "--mount=type=bind,src=/proj/x,dst=/proj/../etc/x",
                    "--mount=type=bind,src=/dev/null,dst=/usr/bin/git,ro",
                    "--mount=type=tmpfs,dst=/etc,notmpcopyup"):
            with self.assertRaises(AgentboxError, msg=bad):
                hardening.audit([bad] + base, allowed_mount_srcs=["/proj"], networks=[],
                                allowed_mount_dsts=["/proj"])


class F9ProxyLogHasNoPaths(TempCase):
    """#9: the kept access log records destinations (host:port), not URL paths that can carry data."""

    def test_logformat(self):
        text = egress.squid_conf("open", model_domains=["api.anthropic.com"], model_host="10.0.0.2",
                                 model_port=8080, host_addrs=["10.0.0.103"])
        fmt = next(line for line in text.splitlines() if line.startswith("logformat"))
        self.assertNotIn("%ru", fmt)
        self.assertNotIn("%>ru", fmt)
        self.assertIn("strip_query_terms on", text)


class OrphansAndSignals(SettingsCase):
    """Found while checking an interrupted itest-oom run (30 Sep): a --no-tty session killed by SIGTERM left
    its proxy running, and gc must still remove a proxy whose agent is gone."""

    def setUp(self):
        super().setUp()
        self.project("p1")
        self.token()

    def make(self):
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare()
        return s

    def test_gc_removes_orphan_proxy(self):
        s = self.make()
        os.unlink(os.path.join(self.settings.session_run_dir(s.name), "session.json"))
        ps = [{"Names": [f"{s.name}-proxy"], "State": "running",
               "Labels": {"agentbox.session": s.name, "agentbox.role": "proxy", "agentbox.project": "p1"}}]
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(ps)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        # review step 5C, L7: a run dir without session.json under a minute old is a session being started
        self.assertEqual(session.gc(self.settings, s.podman), [])
        old = time.time() - 120
        os.utime(self.settings.session_run_dir(s.name), (old, old))
        self.assertEqual(session.gc(self.settings, s.podman), [s.name])

    def test_sigterm_during_a_captured_run_tears_down(self):
        import signal
        s = self.make()

        def body_that_gets_sigterm():
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("SIGTERM did not interrupt the run")
        with self.assertRaises(KeyboardInterrupt):
            s._guarded(body_that_gets_sigterm, agent_owns_ctrl_c=False)
        self.assertFalse(os.path.exists(self.settings.session_run_dir(s.name)), "torn down")
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL, "handlers restored")
