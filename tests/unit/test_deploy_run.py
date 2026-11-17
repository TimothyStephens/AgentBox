"""`ab deploy`, step 3: the settings, the pre-run refusals, the confirmation, and the run against the fake podman
(deployrun.py, cli_deploy.py; docs/deploy-proposal.md "Step 3 (built)"). The podman service, sockguard's socket
and the gate process are mocked: they need a real podman; their argv and the gate's config are what is checked."""
import io
import json
import os
import types
from unittest import mock

from agentbox import deploy, deployrun, gate, projectkeys, registry, session, sockguard
from agentbox.errors import AgentboxError

from .helpers import read_file
from .test_deploy import DeployCase
from .test_gate_deploy import DFake

LAN = "10.0.0.103"


class TTY(io.StringIO):
    def isatty(self):
        return True


class Pipe(io.StringIO):
    def isatty(self):
        return False


class RunCase(DeployCase):
    def setUp(self):
        super().setUp()
        # a slice is configured, so "no --cgroup-parent" below means the deploy left it out on purpose (D5)
        with open(os.path.join(self.cfg, "agentbox.toml"), "a") as f:
            f.write('cgroup_parent = "agentbox.slice"\n')
        path = os.path.join(self.cfg, "agentbox.toml")
        text = read_file(path).replace('cgroup_parent = ""\n', "")
        with open(path, "w") as f:
            f.write(text)
        from agentbox import config
        self.settings = config.load()
        self.gate_cfgs, self.service_calls, self.gate_calls = [], [], []
        patches = [
            mock.patch.object(sockguard, "start_service", side_effect=self._service),
            mock.patch.object(sockguard, "start_gate", side_effect=self._gate),
            mock.patch.object(sockguard, "wait_socket", return_value=None),
            mock.patch.object(deployrun, "linger_warnings", return_value=[]),
            mock.patch("agentbox.egress.host_addresses", return_value={LAN}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _service(self, podman_bin, service_dir, timeout, **kw):
        self.service_calls.append((service_dir, kw))
        os.makedirs(service_dir, mode=0o700, exist_ok=True)
        return os.path.join(service_dir, "podman.sock"), None

    def _gate(self, cfg_path, service_dir, listen, timeout, **kw):
        self.gate_calls.append(kw)
        with open(cfg_path) as f:
            self.gate_cfgs.append(json.load(f))

    def deploy(self, *extra, answer="y\n", stdin=None):
        with mock.patch("sys.stdin", stdin if stdin is not None else TTY(answer)):
            return self.ab("deploy", "web", *extra, stdin=None)

    def runs(self):
        return [c for c in self.podman_calls() if c[:1] == ["run"]]

    def runner(self):
        found = [c for c in self.runs() if "--rm" in c and any(a.endswith("-runner") for a in c)]
        self.assertEqual(len(found), 1, self.runs())
        return found[0]

    def stages(self):
        d = self.settings.deploys_dir("web")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def entry(self):
        return registry.load(self.settings.registry_path)["web"]

    def set_entry(self, **kw):
        reg = registry.load(self.settings.registry_path)
        reg["web"].update(kw)
        registry.save(self.settings.registry_path, reg)


class Settings(RunCase):
    def test_the_five_keys_are_project_security_settings_a_preset_change_keeps(self):
        for name in ("lan_addr", "deploy_ports", "deploy_binds", "deploy_files", "deploy_external"):
            k = projectkeys.BY_NAME[name]
            self.assertEqual((k.kind, k.security, k.clears, k.unsettable), ("project", True, False, True), name)

    def test_validators(self):
        v = projectkeys.validate_value
        self.assertEqual(v("lan_addr", "p", "10.0.0.103"), "10.0.0.103")
        for bad in ("0.0.0.0", "127.0.0.1", "10.0.0.256", "10.0.0", "010.0.0.1", "::1", 5):
            with self.assertRaises(AgentboxError, msg=bad):
                v("lan_addr", "p", bad)
        self.assertEqual(v("deploy_ports", "p", [8095, 80, 8095]), [80, 8095])
        for bad in ([0], [65536], ["80"], [True], 80):
            with self.assertRaises(AgentboxError, msg=bad):
                v("deploy_ports", "p", bad)
        self.assertEqual(v("deploy_files", "p", ["config/app.json", ".env.prod"]), ["config/app.json", ".env.prod"])
        for bad in (["/etc/passwd"], ["../x"], ["a/../b"], ["./a"], [".git/config"], ["a//b"]):
            with self.assertRaises(AgentboxError, msg=bad):
                v("deploy_files", "p", bad)
        self.assertEqual(v("deploy_external", "p", ["media", "proxy_net"]), ["media", "proxy_net"])
        with self.assertRaises(AgentboxError):
            v("deploy_external", "p", ["a/b"])

    def test_project_set_stores_them_and_show_prints_them(self):
        media = self.mkdir("media")
        code, out = self.ab("project", "set", "web", "--lan-addr", LAN, "--deploy-port", "8095", "--deploy-bind",
                            media, "--deploy-file", "config/prod.json", "--deploy-external", "media_vol")
        self.assertEqual(code, 0, out)
        e = self.entry()
        self.assertEqual((e["lan_addr"], e["deploy_ports"], e["deploy_binds"], e["deploy_files"],
                          e["deploy_external"]), (LAN, [8095], [media], ["config/prod.json"], ["media_vol"]))
        code, out = self.ab("project", "show", "web")
        self.assertIn("deploy_binds", out)
        self.assertIn(media, out)
        code, out = self.ab("project", "set", "web", "--clear-deploy-ports")
        self.assertEqual(code, 0, out)
        self.assertNotIn("deploy_ports", self.entry())

    def test_a_deploy_bind_may_not_be_the_root_another_projects_grant_or_a_protected_path(self):
        other = self.project("other", preset="build")
        for bad in (self.root, os.path.join(self.root, "sub"), other, os.path.dirname(other), self.data):
            os.makedirs(bad, exist_ok=True)
            code, out = self.ab("project", "set", "web", "--deploy-bind", bad)
            self.assertNotEqual(code, 0, bad)
            self.assertNotIn("deploy_binds", self.entry())

    def test_lan_is_stored_only_after_a_successful_deploy(self):
        os.environ["FAKE_PODMAN_FAIL"] = "podman-compose"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 1, out)
        self.assertNotIn("lan_addr", self.entry())
        os.environ.pop("FAKE_PODMAN_FAIL")
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.entry()["lan_addr"], self.entry()["deploy_ports"]), (LAN, [8095]))
        # a later deploy may omit --lan: the stored address and ports are used
        self.write("projects/web/app.py", "print('v2')\n")
        self.commit("v2")
        code, out = self.deploy()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.gate_cfgs[-1]["lan_addr"], LAN)
        self.assertEqual(self.gate_cfgs[-1]["ports"], [8095])


class Refusals(RunCase):
    def assert_nothing_started(self, out):
        self.assertEqual(self.runs(), [], out)
        self.assertEqual(self.service_calls, [])
        self.assertEqual(self.stages(), [])
        self.assertEqual(deploy.records(self.settings, "web"), [])

    def test_no_lan_address_anywhere(self):
        code, out = self.deploy()
        self.assertEqual(code, 2)
        self.assertIn("no LAN address", out)
        self.assert_nothing_started(out)

    def test_a_stored_lan_address_that_is_not_this_hosts(self):
        self.set_entry(lan_addr="10.0.0.99", deploy_ports=[8095])
        code, out = self.deploy()
        self.assertEqual(code, 2)
        self.assertIn("not an address of this host", out)
        self.assert_nothing_started(out)

    def test_a_stack_brought_up_by_hand_is_refused_with_the_down_command(self):
        ps = [{"Names": ["web_web_1"], "Labels": {"io.podman.compose.project": "web"}},
              {"Names": ["web_db_1"], "Labels": {"io.podman.compose.project": "web", "agentbox.deploy": "web",
                                                 "agentbox.role": "deploy"}}]
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(ps)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 2)
        self.assertIn("podman-compose -p web down", out)
        self.assertIn("web_web_1", out)
        self.assertNotIn("web_db_1", out)
        self.assert_nothing_started(out)
        self.assertFalse([c for c in self.podman_calls() if c[:1] == ["rm"] or c[:2] == ["volume", "rm"]])

    def test_unlabelled_volumes_of_the_compose_project_are_refused(self):
        vols = [{"Name": "web_data", "Labels": {}}, {"Name": "other_data", "Labels": {}},
                {"Name": "web_cache", "Labels": {"agentbox.deploy": "web", "agentbox.role": "deploy"}}]
        os.environ["FAKE_PODMAN_VOLJSON"] = json.dumps(vols)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_VOLJSON", None)
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 2)
        self.assertIn("web_data", out)
        self.assertNotIn("other_data", out)
        self.assertNotIn("web_cache", out)
        self.assert_nothing_started(out)

    def test_a_deploy_bind_that_is_no_longer_safe_is_refused_at_deploy_time(self):
        media = self.mkdir("media")
        self.set_entry(deploy_binds=[media])
        os.rmdir(media)
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 2)
        self.assertIn("not an existing directory", out)
        self.assert_nothing_started(out)

    def test_one_deploy_per_project(self):
        with deployrun.lock(self.settings, "web"):
            code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertNotEqual(code, 0)
        self.assertIn("another `ab deploy web` is running", out)
        self.assert_nothing_started(out)

    def test_a_dirty_tree_starts_nothing(self):
        self.write("projects/web/app.py", "print('dirty')\n")
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 2)
        self.assert_nothing_started(out)


class ExternalObjects(DeployCase):
    """`external:` volumes and networks: enforced in the gate (cfg `external_ok`, from deploy_external)."""

    def g(self, external=(), objects=None):
        stage = self.mkdir("stage")
        return DFake(objects, stage=stage, deploy_binds=[], external_ok=list(external))

    def body(self, **kw):
        out = {"image": "x", "name": "web_web_1", "labels": {"io.podman.compose.project": "shop"},
               "Networks": {"shop_default": {}}}
        out.update(kw)
        return out

    def create(self, g, body):
        from .test_review_step5 import decide
        return decide(g, "POST", "/v5/libpod/containers/create", body)

    def test_an_unlisted_external_volume_is_refused_naming_deploy_external(self):
        g = self.g(objects={"volumes/media": {"Name": "media", "Labels": {}}})
        with self.assertRaises(gate.Deny) as cm:
            self.create(g, self.body(volumes=[{"Name": "media", "Dest": "/media"}]))
        self.assertIn("deploy_external", str(cm.exception))

    def test_a_listed_external_volume_is_used_never_created(self):
        g = self.g(["media"], objects={"volumes/media": {"Name": "media", "Labels": {}}})
        self.create(g, self.body(volumes=[{"Name": "media", "Dest": "/media"}]))
        self.assertEqual(g.created, [])
        g = self.g(["media"])                                   # listed but missing: refused, not created
        with self.assertRaises(gate.Deny):
            self.create(g, self.body(volumes=[{"Name": "media", "Dest": "/media"}]))
        self.assertEqual(g.created, [])

    def test_external_networks(self):
        ext = {"networks/proxy": {"name": "proxy", "id": "id-proxy", "labels": {}, "driver": "bridge"}}
        with self.assertRaises(gate.Deny) as cm:
            self.create(self.g(objects=ext), self.body(Networks={"proxy": {}}))
        self.assertIn("deploy_external", str(cm.exception))
        self.create(self.g(["proxy"], objects=ext), self.body(Networks={"proxy": {}}))

    def test_an_external_object_can_be_read_on_the_raw_service_and_nothing_else(self):
        from .test_review_step5 import decide
        ext = {"volumes/media": {"Name": "media", "Labels": {}}}
        g = self.g(["media"], objects=ext)
        self.assertEqual(decide(g, "GET", "/v5/libpod/volumes/media/json", None)[2], "ext_read")
        with self.assertRaises((gate.Deny, gate.NotFound)):
            decide(g, "DELETE", "/v5/libpod/volumes/media", None)
        with self.assertRaises((gate.Deny, gate.NotFound)):
            decide(self.g(objects=ext), "GET", "/v5/libpod/volumes/media/json", None)


class Confirmation(DeployCase):
    def c(self, same):
        return {"commit": "a" * 40, "last": ("a" if same else "b") * 40}

    def test_the_rules(self):
        yes, no = types.SimpleNamespace(yes=True), types.SimpleNamespace(yes=False)
        from agentbox.cli_deploy import confirm
        with self.assertRaises(AgentboxError) as cm:                      # --yes on a terminal
            confirm(yes, self.c(True), "web", LAN, stdin=TTY(""))
        self.assertIn("for scripts", str(cm.exception))
        with self.assertRaises(AgentboxError) as cm:                      # --yes with changes to review
            confirm(yes, self.c(False), "web", LAN, stdin=Pipe(""))
        self.assertIn("only redeploys", str(cm.exception))
        with self.assertRaises(AgentboxError):                            # --yes on a first deploy
            confirm(yes, {"commit": "a" * 40, "last": None}, "web", LAN, stdin=Pipe(""))
        self.assertTrue(confirm(yes, self.c(True), "web", LAN, stdin=Pipe("")))
        with self.assertRaises(AgentboxError) as cm:                      # no terminal, no --yes
            confirm(no, self.c(True), "web", LAN, stdin=Pipe("y\n"))
        self.assertIn("no terminal", str(cm.exception))
        with mock.patch("sys.stdout", io.StringIO()) as shown:
            self.assertTrue(confirm(no, self.c(False), "web", LAN, stdin=TTY("y\n")))
            self.assertFalse(confirm(no, self.c(False), "web", LAN, stdin=TTY("\n")))
            self.assertFalse(confirm(no, self.c(False), "web", LAN, stdin=TTY("")))
        self.assertIn(f"Deploy {'a' * 12} of web to {LAN}? [y/N]", shown.getvalue())


class TheRun(RunCase):
    def test_answering_no_starts_nothing(self):
        code, out = self.deploy("--lan", f"{LAN}:8095", answer="n\n")
        self.assertEqual(code, 1)
        self.assertIn("not deployed", out)
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.service_calls, [])
        self.assertEqual(deploy.records(self.settings, "web"), [])

    def test_the_full_run(self):
        media = self.mkdir("media")
        self.set_entry(deploy_binds=[media], deploy_external=["media_vol"], deploy_files=["config/prod.json"])
        self.write("projects/web/config/prod.json", "{}")
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 0, out)
        name = "abt-unit-deploy-web"
        [stage_name] = self.stages()
        stage = os.path.join(self.settings.deploys_dir("web"), stage_name)
        self.assertTrue(os.path.isfile(os.path.join(stage, ".env")))
        self.assertTrue(os.path.isfile(os.path.join(stage, "config/prod.json")))
        # the podman service: per deploy, in the deploy's service dir, NOT in agentbox.slice (D5)
        [(svc_dir, kw)] = self.service_calls
        self.assertEqual(svc_dir, self.settings.service_dir(name))
        self.assertEqual(kw["slice_name"], "")
        self.assertEqual(self.gate_calls, [{"slice_name": ""}])
        runs = self.runs()
        self.assertFalse([a for c in runs for a in c if a.startswith("--cgroup-parent")], runs)
        # sockguard: the deploy profile, scoped to the deploy label
        [sg] = [c for c in runs if f"--name={name}-sockguard-dev" in c]
        self.assertIn("--network=none", sg)
        conf = json.loads(sockguard.deploy_config("web", bind_roots=[stage, media]))
        self.assertEqual(conf["ownership"]["label_key"], "agentbox.deploy")
        self.assertEqual(conf["ownership"]["owner"], "web")
        # the build holder on the deploy's own (non-internal) network
        nets = [c for c in self.podman_calls() if c[:2] == ["network", "create"]]
        self.assertEqual(len(nets), 1)
        self.assertNotIn("--internal", nets[0])
        self.assertEqual(nets[0][-1], f"{name}-build")
        [holder] = [c for c in runs if f"--name={name}-holder" in c]
        self.assertIn(f"--network={name}-build", holder)
        # the gate: deploy mode, with every key step 2 needs, and a config the gate itself accepts
        [cfg] = self.gate_cfgs
        self.assertEqual(cfg["mode"], "deploy")
        self.assertEqual((cfg["compose_project"], cfg["lan_addr"], cfg["ports"], cfg["stage"], cfg["deploy_binds"],
                          cfg["external_ok"]), ("web", LAN, [8095], stage, [media], ["media_vol"]))
        self.assertEqual(cfg["labels"]["agentbox.deploy"], "web")
        self.assertNotIn("agentbox.session", cfg["labels"])
        self.assertNotIn("agentbox.project", cfg["labels"])
        self.assertEqual(cfg["holder"], f"{name}-holder")
        self.assertEqual(cfg["built_record"], self.settings.deploy_built_record("web"))
        self.assertNotEqual(cfg["built_record"], self.settings.built_record("web"))
        self.assertEqual(cfg["raw"], os.path.join(self.settings.service_dir(name), "podman.sock"))
        self.assertTrue(gate.Gate(cfg).deploy)
        # the runner
        r = self.runner()
        self.assertEqual(r[:2], ["run", "--rm"])
        self.assertIn("--network=none", r)
        self.assertIn(f"--mount=type=bind,src={stage},dst={stage},ro", r)
        gate_sock = os.path.join(self.settings.session_run_dir(name), "gate", "podman.sock")
        self.assertIn(f"--mount=type=bind,src={gate_sock},dst=/run/agentbox/podman.sock", r)
        self.assertIn("--env=CONTAINER_HOST=unix:///run/agentbox/podman.sock", r)
        for flag in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--userns=keep-id"):
            self.assertIn(flag, r)
        self.assertIn("--label=agentbox.session=" + name, r)
        i = r.index(deployrun.RUNNER_IMAGE)
        self.assertEqual(r[i - 1], "--entrypoint=podman-compose")
        self.assertEqual(r[i + 1:], ["-p", "web", "-f", os.path.join(stage, "compose.yaml"), "up", "-d", "--build",
                                     "--remove-orphans"])
        # teardown: by the deploy's session label; the run and service dirs are gone
        self.assertIn(["ps", "-a", "--format", "{{.Names}}", "--filter", f"label=agentbox.session={name}",
                       "--filter", "label=agentbox-test=1"], self.podman_calls())
        self.assertFalse(os.path.exists(self.settings.session_run_dir(name)))
        self.assertFalse(os.path.exists(self.settings.service_dir(name)))
        # the record, the stored LAN, and what is running
        [rec] = deploy.records(self.settings, "web")
        self.assertEqual((rec["result"], rec["lan"], rec["ports"], rec["compose_project"], rec["stage"]),
                         ("ok", LAN, [8095], "web", stage))
        self.assertEqual(deploy.last_deployed(self.settings, "web"), self.git("rev-parse", "HEAD"))
        self.assertIn("label=agentbox.deploy=web", " ".join(" ".join(c) for c in self.podman_calls()))

    def test_a_failed_compose_run_is_recorded_and_torn_down(self):
        os.environ["FAKE_PODMAN_FAIL"] = "podman-compose"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 1)
        self.assertIn("no rollback", out)
        [rec] = deploy.records(self.settings, "web")
        self.assertEqual((rec["result"], rec["error"]), ("failed", "podman-compose exited 1"))
        self.assertIsNone(deploy.last_deployed(self.settings, "web"))
        self.assertFalse(os.path.exists(self.settings.session_run_dir("abt-unit-deploy-web")))

    def _direct(self):
        p = session.load_project(self.settings, "web")
        return deployrun.Deploy(self.settings, p, commit=self.git("rev-parse", "HEAD"), lan_addr=LAN, ports=[8095],
                                binds=[], warn=lambda m: None, out=lambda m: None)

    def test_teardown_and_record_on_a_start_failure(self):
        with mock.patch.object(sockguard, "start_gate", side_effect=AgentboxError("gate did not start")):
            with self.assertRaises(AgentboxError):
                self._direct().run()
        [rec] = deploy.records(self.settings, "web")
        self.assertEqual((rec["result"], rec["error"]), ("failed", "gate did not start"))
        self.assertEqual([c for c in self.runs() if "--rm" in c], [])        # the runner never ran
        self.assertFalse(os.path.exists(self.settings.session_run_dir("abt-unit-deploy-web")))

    def test_teardown_and_record_on_ctrl_c(self):
        torn = []
        real = deployrun.lifecycle.teardown
        with mock.patch.object(sockguard, "start_gate", side_effect=KeyboardInterrupt), \
                mock.patch.object(deployrun.lifecycle, "teardown",
                                  side_effect=lambda *a, **kw: (torn.append(kw.get("exit_reason")), real(*a, **kw))):
            with self.assertRaises(KeyboardInterrupt):
                self._direct().run()
        self.assertEqual(torn, ["signal"])
        [rec] = deploy.records(self.settings, "web")
        self.assertEqual((rec["result"], rec["error"]), ("failed", "interrupted"))
        self.assertFalse(os.path.exists(self.settings.session_run_dir("abt-unit-deploy-web")))

    def test_old_copies_are_pruned_but_the_last_good_one_is_kept(self):
        d = self.settings.deploys_dir("web")
        for n in ("20200101-000000-aaa", "20200102-000000-bbb", "20200103-000000-ccc"):
            os.makedirs(os.path.join(d, n))
        deploy.record(self.settings, "web", {"result": "ok", "commit": "x" * 40,
                                             "stage": os.path.join(d, "20200101-000000-aaa")})
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 0, out)
        left = self.stages()
        self.assertEqual(len(left), 2, left)                 # the new one and the previous; the last ok one is now new
        self.assertIn("20200103-000000-ccc", left)

    def test_leftovers_of_a_crashed_deploy_are_torn_down_first(self):
        os.makedirs(self.settings.session_run_dir("abt-unit-deploy-web"))
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 0, out)
        self.assertIn("left behind", out)


class Lingering(DeployCase):
    def test_warnings_from_read_only_checks(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = {"loginctl": "Linger=no\n", "systemctl": "disabled\n"}[argv[0]]
            return types.SimpleNamespace(stdout=out, returncode=0)
        warned = deployrun.linger_warnings("tim", run=run)
        self.assertEqual(len(warned), 2)
        self.assertEqual(calls, [["loginctl", "show-user", "tim", "-p", "Linger"],
                                 ["systemctl", "--user", "is-enabled", "podman-restart.service"]])

        def ok(argv, **kw):
            return types.SimpleNamespace(stdout="Linger=yes\n" if argv[0] == "loginctl" else "enabled\n")
        self.assertEqual(deployrun.linger_warnings("tim", run=ok), [])


class ObservedStack(DeployCase):
    """A deployed stack runs from the copy under the project's data dir; the observe tier's ownership check
    accepts it there only with the deploy labels."""

    def test_deployed_containers_count_as_the_projects_own(self):
        self.token()
        reg = registry.load(self.settings.registry_path)
        reg["web"]["podman"] = {"tier": "observe"}
        registry.save(self.settings.registry_path, reg)
        wd = os.path.join(self.settings.deploys_dir("web"), "20260101-000000-abc")
        for labels, ok in (({"agentbox.deploy": "web", "agentbox.role": "deploy"}, True), ({}, False)):
            ps = [{"Names": ["web_web_1"], "Labels": {"io.podman.compose.project": "web",
                                                    "com.docker.compose.project.working_dir": wd, **labels}}]
            with mock.patch.dict(os.environ, {"FAKE_PODMAN_PSJSON": json.dumps(ps)}):
                s = session.make_session(self.settings, "claude", session.load_project(self.settings, "web"),
                                         warn=lambda m: None)
                if ok:
                    s._check_live_stack_is_ours()
                else:
                    with self.assertRaises(AgentboxError):
                        s._check_live_stack_is_ours()


class AdoptVolumes(RunCase):
    """Tim, 17 Nov: a hand-run stack's volumes are adopted by name, once, after a confirmation; podman cannot
    add labels to an existing volume, so without this the data of a migrated stack could never carry over."""
    VOLS = [{"Name": "web_data", "Labels": {}}, {"Name": "other_data", "Labels": {}}]

    def setUp(self):
        super().setUp()
        os.environ["FAKE_PODMAN_VOLJSON"] = json.dumps(self.VOLS)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_VOLJSON", None)

    def adopt(self, answer, tty=True):
        from agentbox import cli_deploy, session
        p = session.load_project(self.settings, "web")
        with mock.patch("sys.stdout", io.StringIO()):
            return cli_deploy._adopt(self.settings, p, stdin=(TTY if tty else Pipe)(answer))

    def test_confirmed_names_are_stored_and_no_longer_refused(self):
        from agentbox import deployrun, session
        from agentbox.podman import Podman
        self.assertEqual(self.adopt("y\n"), 0)
        p = session.load_project(self.settings, "web")
        self.assertEqual(p.deploy_adopted, ["web_data"], "only this stack's prefix")
        self.assertEqual(deployrun.foreign_objects(self.settings, Podman(self.settings.podman), p), [])

    def test_the_refusal_names_the_adoption_command(self):
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 2)
        self.assertIn("ab deploy web --adopt-volumes", out)

    def test_no_or_no_terminal_adopts_nothing(self):
        from agentbox import session
        self.assertEqual(self.adopt("n\n"), 1)
        with self.assertRaises(AgentboxError):
            self.adopt("y\n", tty=False)
        self.assertEqual(session.load_project(self.settings, "web").deploy_adopted, [])

    def test_adopted_volumes_reach_the_gate_as_usable_by_name(self):
        self.set_entry(deploy_adopted=["web_data"])
        code, out = self.deploy("--lan", f"{LAN}:8095")
        self.assertEqual(code, 0, out)
        [cfg] = self.gate_cfgs
        self.assertIn("web_data", cfg["external_ok"])
