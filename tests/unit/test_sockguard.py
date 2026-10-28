"""Step 4: sockguard observe tier, without podman."""
import json
import os
import subprocess
import sys
from unittest import mock

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

    def test_config_refuses_a_bad_mode_or_unsafe_project_name(self):
        """E17: an invalid mode, or a compose project name that would break the visibility label (empty, or
        containing '=' or ',')."""
        with self.assertRaises(AgentboxError):
            sockguard.config("shop", mode="yolo")
        for bad in ("", "a=b", "a,b"):
            with self.subTest(bad=bad), self.assertRaises(AgentboxError):
                sockguard.config(bad)

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
        s.prepare(do_backup=False)          # the pre-session backup needs rsync and is not what these test
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
        """Since F7 the refusal lands when the project is loaded (and when one is registered), not only
        once a session is being prepared; session.podman_access keeps its own check behind that."""
        self.project("p1", podman={"tier": "exec"})
        with self.assertRaises(AgentboxError) as cm:
            session.load_project(self.settings, "p1")
        self.assertIn("exec on the live stack is not built", str(cm.exception))

    def test_pods_preset_gets_the_dev_tier_and_observe_on_live(self):
        s, _warnings = self.make(preset="pods")
        self.assertEqual(s.podman_live, "p1")
        self.assertTrue(s.podman_dev)
        argv = s.run_args(["true"])
        socks = [a for a in argv if "podman" in a and a.startswith("--mount")]
        self.assertEqual(sorted(socks), sorted([
            f"--mount=type=bind,src={s.gate_socket},dst=/run/agentbox/podman.sock",
            f"--mount=type=bind,src={s.sockguard_socket},dst=/run/agentbox/podman-live.sock"]))
        notes = session.sandbox_notes(s.project, s.egress, s.podman_live, s.podman_dev)
        self.assertIn("dev tier", notes)
        self.assertIn("podman-live.sock", notes)

    def test_the_dev_notes_state_this_projects_own_image_patterns(self):
        """F-9: the notes said other local images are refused "unless the project lists them", which does not
        say what this project lists -- so the agent could not tell whether an image was allowed without
        trying it. They now name the patterns, or say there are none."""
        s, _w = self.make(preset="pods")
        self.assertIn("no other local image", session.sandbox_notes(
            s.project, s.egress, s.podman_live, s.podman_dev).lower())
        s2, _w2 = self.make(preset="pods", dev_images=["localhost/myapp:*", "localhost/db"])
        notes = session.sandbox_notes(s2.project, s2.egress, s2.podman_live, s2.podman_dev)
        self.assertIn("localhost/myapp:*", notes)
        self.assertIn("localhost/db", notes)
        self.assertIn("agentbox", notes, "and that agentbox's own names can never be listed")

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
        with open(os.path.join(run, "service.pid"), "w") as f:
            f.write(f"{other.pid}\n")
        self.assertFalse(sockguard.stop_service(run))
        self.assertIsNone(other.poll(), "an unrelated process was left alone")

    def test_stops_its_own_service(self):
        run = self.mkdir("run", "s2")
        os.makedirs(os.path.join(run, "podman"))
        # a stand-in whose command line looks like `<bin> system service ... unix://<run>/...`
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "system", "service",
                                 f"unix://{run}/podman/podman.sock"])
        with open(os.path.join(run, "service.pid"), "w") as f:
            f.write(f"{proc.pid}\n")
        self.assertTrue(sockguard.stop_service(run))
        self.assertIsNotNone(proc.poll())

    def test_missing_pidfile(self):
        self.assertFalse(sockguard.stop_service(self.mkdir("run", "s3")))


class OrphanedServices(SettingsCase):
    """Decision 30 Sep: ab session gc finds and stops podman services left by a crashed session, even when the
    session's run dir and containers are gone; it leaves a live session's service alone."""

    def fake_service(self, session_name):
        sock = f"unix://{os.path.join(self.settings.run_dir, 'services', session_name, 'podman.sock')}"
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "system", "service", sock])
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def test_scan_finds_services_by_run_dir(self):
        p = self.fake_service("abt-unit-claude-p1-1")
        found = sockguard.services_under(self.settings.run_dir)
        self.assertEqual(found.get(p.pid), "abt-unit-claude-p1-1")
        self.assertEqual(sockguard.services_under(os.path.join(self.tmp, "other-run")), {})

    def test_scan_finds_a_gate_too(self):
        cfg = os.path.join(self.settings.run_dir, "services", "abt-unit-claude-p1-3", "gate.json")
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "agentbox.gate", cfg])
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.assertEqual(sockguard.services_under(self.settings.run_dir).get(p.pid), "abt-unit-claude-p1-3")

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


class RawSocketNeverMounted(SettingsCase):
    """Review 2026-10-01 #6: the raw podman socket lives outside the session run dir, and the argv audit
    refuses any socket as a bind source except sockguard's."""

    def test_service_dir_is_outside_the_session_run_dir(self):
        svc, run = self.settings.service_dir("abt-x-1"), self.settings.session_run_dir("abt-x-1")
        self.assertFalse(svc.startswith(run.rstrip("/") + "/") or svc == run)

    def test_audit_refuses_other_sockets(self):
        import socket as _socket
        from agentbox import hardening
        d = self.mkdir("grant")
        path = os.path.join(d, "raw.sock")
        srv = _socket.socket(_socket.AF_UNIX)
        srv.bind(path)
        self.addCleanup(srv.close)
        argv = ["--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--userns=keep-id",
                "--pids-limit=1", "--memory=1g", "--memory-swap=1g", "--oom-score-adj=1",
                f"--mount=type=bind,src={path},dst={d}/raw.sock", "img"]
        with self.assertRaises(AgentboxError) as cm:
            hardening.audit(argv, allowed_mount_srcs=[d], allowed_mount_dsts=[d], networks=[])
        self.assertIn("socket", str(cm.exception))
        hardening.audit(argv, allowed_mount_srcs=[d], allowed_mount_dsts=[d], networks=[], allowed_sockets=[path])


class LiveStackOwnership(SettingsCase):
    """Review 2026-10-01 #7: a defaulted compose project must be this project's own stack."""

    def setUp(self):
        super().setUp()
        self.token()

    def make(self, workdir, **extra):
        root = self.project("shop", podman={"tier": "observe"}, **extra)
        ps = [{"Names": ["shop_web_1"], "Labels": {"io.podman.compose.project": "shop",
               "com.docker.compose.project.working_dir": workdir or root}}]
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(ps)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "shop"),
                                 warn=lambda m: None)
        self.addCleanup(s.teardown)
        return s

    def test_another_directorys_stack_refused(self):
        s = self.make(os.path.join(self.tmp, "another-repo"))
        with self.assertRaises(AgentboxError) as cm:
            s.prepare(do_backup=False)
        self.assertIn("not this project's stack", str(cm.exception))

    def test_own_stack_fine(self):
        self.make(None).prepare(do_backup=False)

    def test_a_compose_project_equal_to_the_default_waives_nothing(self):
        """F-4: the waiver was `"compose_project" in entry`, so storing the value the project would have
        anyway -- `compose_project = "shop"` on project `shop`, which `ab project add --compose-project shop`
        writes -- turned the ownership check off while changing nothing about what is observed."""
        s = self.make(os.path.join(self.tmp, "another-repo"), compose_project="shop")
        with self.assertRaises(AgentboxError) as cm:
            s.prepare(do_backup=False)
        self.assertIn("not this project's stack", str(cm.exception))

    def test_a_compose_project_that_differs_is_trusted(self):
        """Naming a DIFFERENT stack is the deliberate act the waiver is for: you cannot have meant the
        project's own stack, so its containers need not come from inside the project root."""
        s = self.make(os.path.join(self.tmp, "another-repo"), compose_project="other-stack")
        ps = [{"Names": ["other_web_1"], "Labels": {"io.podman.compose.project": "other-stack",
               "com.docker.compose.project.working_dir": os.path.join(self.tmp, "another-repo")}}]
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(ps)
        s.prepare(do_backup=False)


class GcOncePerBoot(SettingsCase):
    """Review 2026-10-01 (doc claims): gc runs by itself on the first use after boot, once."""

    def test_first_use_runs_gc_then_not_again(self):
        from agentbox import cli
        with mock.patch.object(session, "gc", return_value=[]) as gc:
            self.assertTrue(cli.gc_once_per_boot(self.settings))
            self.assertFalse(cli.gc_once_per_boot(self.settings))
        self.assertEqual(gc.call_count, 1)


class ImageGc(SettingsCase):
    """Decision 1 Oct: ab session gc removes a project's session-built images older than 7 days, keeping the newest
    image of each tag."""

    def test_old_untagged_go_newest_per_tag_stays(self):
        day = 86400
        now = 100 * day
        dev = {"agentbox.role": "dev", "agentbox.project": "p1", "agentbox.session": "s"}
        imgs = [
            {"Id": "a" * 64, "Names": ["localhost/app:latest"], "Created": now - 30 * day, "Labels": dev},
            {"Id": "b" * 64, "Names": [], "Created": now - 30 * day, "Labels": dev},
            {"Id": "c" * 64, "Names": [], "Created": now - 2 * day, "Labels": dev},
            {"Id": "d" * 64, "Names": ["localhost/app:v1"], "Created": now - 20 * day, "Labels": dev},
            {"Id": "e" * 64, "Names": ["localhost/app:v1"], "Created": now - 10 * day, "Labels": dev},
        ]
        os.environ["FAKE_PODMAN_IMAGES"] = json.dumps(imgs)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGES", None)
        removed = session.gc_images(self.settings, session.Podman(self.settings.podman), now=now)
        rmi = [c[1] for c in self.podman_calls() if c[:1] == ["rmi"]]
        self.assertEqual(sorted(rmi), ["b" * 64, "d" * 64])
        self.assertEqual(len(removed), 2)
        images_call = [c for c in self.podman_calls() if c[:1] == ["images"]][0]
        self.assertIn("label=agentbox.role=dev", images_call)
        self.assertIn("label=agentbox-test=1", images_call)
