"""Step 4: sockguard observe tier, without podman."""
import json
import os
import subprocess
import sys

from agentbox import session, sockguard
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, TempCase


class Config(TempCase):
    def test_observe_config(self):
        doc = json.loads(sockguard.config("shop"))
        self.assertEqual(doc["response"]["visible_resource_labels"], ["io.podman.compose.project=shop"])
        for k in ("redact_container_env", "redact_mount_paths", "redact_network_topology", "redact_sensitive_data"):
            self.assertIs(doc["response"][k], True, k)
        prof = doc["clients"]["profiles"][0]
        self.assertEqual((doc["clients"]["default_profile"], prof["name"], prof["mode"]), ("session", "session", "enforce"))
        rules = prof["rules"]
        self.assertEqual(rules[-1], {"match": {"method": "*", "path": "/**"}, "action": "deny",
                                     "reason": "agentbox observe tier: reads of the project's live stack only"})
        allowed = [r for r in rules if r["action"] == "allow"]
        self.assertTrue(allowed)
        self.assertEqual({r["match"]["method"] for r in allowed}, {"GET", "HEAD"}, "observe allows no writes")
        for r in allowed:
            self.assertNotRegex(r["match"]["path"], r"exec|archive|export|attach|create|pull|build|push|/get$")
        self.assertEqual(doc["listen"]["socket_mode"], "0600")
        self.assertEqual(doc["upstream"]["flavor"], "podman")

    def test_logs_only_when_asked(self):
        paths = lambda doc: [r["match"]["path"] for r in json.loads(doc)["clients"]["profiles"][0]["rules"]
                             if r["action"] == "allow"]
        self.assertFalse([p for p in paths(sockguard.config("shop")) if p.endswith("/logs")])
        self.assertEqual(sorted(p for p in paths(sockguard.config("shop", logs=True)) if p.endswith("/logs")),
                         ["/containers/*/logs", "/libpod/containers/*/logs"])
        rules = json.loads(sockguard.config("shop", logs=True))["clients"]["profiles"][0]["rules"]
        self.assertEqual(rules[-1]["action"], "deny", "deny-all stays last")

    def test_bad_labels_and_modes(self):
        for bad in ("", "a=b", "a,b"):
            with self.subTest(bad=bad), self.assertRaises(AgentboxError):
                sockguard.config(bad)
        with self.assertRaises(AgentboxError):
            sockguard.config("shop", mode="warn")

    def test_discovery_is_audit_and_deny_all(self):
        prof = json.loads(sockguard.discovery_config("shop"))["clients"]["profiles"][0]
        self.assertEqual(prof["mode"], "audit")
        self.assertEqual([r["action"] for r in prof["rules"]], ["deny"])


class Tiers(SettingsCase):
    def setUp(self):
        super().setUp()
        self.token()

    def make(self, **extra):
        self.project("p1", **extra)
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=warnings.append)
        s.prepare()
        self.addCleanup(s.teardown)
        return s, warnings

    def test_default_python_preset_has_no_socket(self):
        s, _w = self.make()
        self.assertIsNone(s.podman_live)
        argv = s.run_args(["true"])
        self.assertFalse([a for a in argv if "podman.sock" in a])
        self.assertIn("No podman or docker socket", session.sandbox_notes(s.project, s.egress))

    def test_observe_mounts_sockguard_only(self):
        s, _w = self.make(podman={"tier": "observe"}, compose_project="shop")
        self.assertEqual(s.podman_live, "shop")
        argv = s.run_args(["true"])
        mounts = [a for a in argv if "podman" in a and a.startswith("--mount")]
        self.assertEqual(mounts, [f"--mount=type=bind,src={s.sockguard_socket},dst=/run/agentbox/podman.sock"])
        self.assertNotIn("/run/user", " ".join(mounts))
        notes = session.sandbox_notes(s.project, s.egress, s.podman_live)
        self.assertIn("compose project `shop`", notes)

    def test_sockguard_container_is_hardened(self):
        s, _w = self.make(podman={"tier": "observe"})
        args = s.sockguard_run_args("/run/x/podman.sock", "/run/x/sockguard.yaml")
        for flag in ("--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
                     "--userns=keep-id", "--pids-limit=128"):
            self.assertIn(flag, args)
        self.assertIn(s.settings.sockguard_image, args)
        self.assertIn("@sha256:", s.settings.sockguard_image, "pinned by digest")
        self.assertIn("--label=agentbox.role=sockguard", args)

    def test_exec_tier_refused_until_the_gate(self):
        self.project("p1", podman={"tier": "exec"})
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        with self.assertRaises(AgentboxError) as cm:
            s.prepare()
        self.assertIn("step 5", str(cm.exception))

    def test_pods_preset_gets_observe_on_live_and_a_warning(self):
        s, warnings = self.make(preset="pods")
        self.assertEqual(s.podman_live, "p1")
        self.assertTrue(any("dev tier" in w for w in warnings))

    def test_lockdown_has_none(self):
        s, _w = self.make(preset="lockdown")
        self.assertIsNone(s.podman_live)

    def test_bad_compose_project_refused(self):
        self.project("p1", compose_project="Bad=Name")
        with self.assertRaises(AgentboxError):
            session.load_project(self.settings, "p1")


class ServiceStop(TempCase):
    def test_does_not_kill_a_reused_pid(self):
        run = self.mkdir("run", "s1")
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        os.makedirs(os.path.join(run, "podman"))
        with open(os.path.join(run, "podman", "service.pid"), "w") as f:
            f.write(f"{other.pid}\n")
        self.assertFalse(sockguard.stop_service(run))
        self.assertIsNone(other.poll(), "an unrelated process was left alone")

    def test_stops_its_own_service(self):
        run = self.mkdir("run", "s2")
        os.makedirs(os.path.join(run, "podman"))
        # a stand-in whose command line looks like `<bin> system service ... unix://<run>/...`
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "system", "service",
                                 f"unix://{run}/podman/podman.sock"])
        with open(os.path.join(run, "podman", "service.pid"), "w") as f:
            f.write(f"{proc.pid}\n")
        self.assertTrue(sockguard.stop_service(run))
        self.assertIsNotNone(proc.poll())

    def test_missing_pidfile(self):
        self.assertFalse(sockguard.stop_service(self.mkdir("run", "s3")))


class OrphanedServices(SettingsCase):
    """Decision 30 Sep: ab gc finds and stops podman services left by a crashed session, even when the
    session's run dir and containers are gone; it leaves a live session's service alone."""

    def fake_service(self, session_name):
        sock = f"unix://{os.path.join(self.settings.run_dir, 'sessions', session_name, 'podman', 'podman.sock')}"
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "system", "service", sock])
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def test_scan_finds_services_by_run_dir(self):
        p = self.fake_service("abt-unit-claude-p1-1")
        found = sockguard.services_under(self.settings.run_dir)
        self.assertEqual(found.get(p.pid), "abt-unit-claude-p1-1")
        self.assertEqual(sockguard.services_under(os.path.join(self.tmp, "other-run")), {})

    def test_gc_stops_an_orphan_with_no_run_dir(self):
        p = self.fake_service("abt-unit-claude-p1-7")
        removed = session.gc(self.settings, session.Podman(self.settings.podman))
        p.wait(timeout=15)
        self.assertIn("abt-unit-claude-p1-7 (podman service)", removed)

    def test_gc_keeps_a_live_sessions_service(self):
        self.project("p1")
        self.token()
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare()
        self.addCleanup(s.teardown)
        p = self.fake_service(s.name)
        session.gc(self.settings, session.Podman(self.settings.podman))
        self.assertIsNone(p.poll(), "the live session's service is still running")
