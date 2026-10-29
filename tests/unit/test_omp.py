"""Step 3: the OMP adapter, provider keys and the model config, without podman."""
import io
import json
import os
import stat
from unittest import mock

from agentbox import keys, miniyaml, session
from agentbox.adapters import omp
from agentbox.config import HOME
from agentbox.errors import AgentboxError
from tests import ompfake

from .helpers import SettingsCase, TempCase

FIXTURE_MODELS = os.path.join(ompfake.FIXTURE, "models.redacted.yml")


def seed_without_keys():
    with open(FIXTURE_MODELS) as f:
        text = f.read()
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("apiKey")) + "\n"


class MiniYaml(TempCase):
    def test_reads_the_real_layout(self):
        with open(FIXTURE_MODELS) as f:
            doc = miniyaml.loads(f.read())
        self.assertEqual(list(doc["providers"]), ["llama-cpp", "gemini", "openai", "anthropic", "openrouter"])
        self.assertEqual(doc["providers"]["llama-cpp"]["baseUrl"], "http://10.0.0.2:8080/v1")
        self.assertIs(doc["providers"]["llama-cpp"]["models"][0]["reasoning"], True)
        self.assertEqual(doc["providers"]["openrouter"]["models"][0]["maxTokens"], 8192)

    def test_round_trip_through_json(self):
        with open(FIXTURE_MODELS) as f:
            doc = miniyaml.loads(f.read())
        self.assertEqual(json.loads(miniyaml.dumps(doc)), doc)

    def test_scalars_and_lists(self):
        doc = miniyaml.loads("a: 'it''s'\nb: \"x # y\"  # comment\nc: [1, 'two, three', x]\nd: ~\ne: 1.5\n"
                             "f:\n  - k: v\n    l: w\n  -\n    - nested\n")
        self.assertEqual(doc, {"a": "it's", "b": "x # y", "c": [1, "two, three", "x"], "d": None, "e": 1.5,
                               "f": [{"k": "v", "l": "w"}, ["nested"]]})

    def test_refusals(self):
        for text in ("a: &x 1\n", "a: *x\n", "a: !cmd x\n", "a: |\n  text\n", "a: {b: 1}\n", "---\na: 1\n",
                     "a: 1\na: 2\n", "a:\n\t- x\n", "a: b: c\n", "? a\n", "a: [x, [y]]\n", "a: 1\n  b: 2\n"):
            with self.subTest(text=text), self.assertRaises(AgentboxError):
                miniyaml.loads(text)


class BuildModels(TempCase):
    def build(self, keys_present=(), seed=None, warnings=None):
        seed = miniyaml.loads(seed_without_keys()) if seed is None else seed
        k = {p: f"/keys/{p}" for p in keys_present}
        return omp.build_models(seed, keys=k, model_host="10.0.0.2", model_port=8080,
                                warn=(warnings.append if warnings is not None else lambda m: None))

    def test_no_keys_local_model_only(self):
        warnings = []
        doc, domains, used = self.build(warnings=warnings)
        self.assertEqual(list(doc["providers"]), ["llama-cpp"])
        self.assertEqual(doc["providers"]["llama-cpp"]["apiKey"], "none")
        self.assertEqual(len(doc["providers"]["llama-cpp"]["models"]), 13)
        self.assertEqual((domains, used), ([], {}))
        self.assertTrue(any("'openai' left out: no key" in w for w in warnings))
        self.assertTrue(any("local model provider 'llama-cpp' has no key" in w for w in warnings))

    def test_keyed_providers_included_by_variable_name(self):
        doc, domains, used = self.build(["openai", "gemini", "llama-cpp"])
        self.assertEqual(sorted(doc["providers"]), ["gemini", "llama-cpp", "openai"])
        self.assertEqual(doc["providers"]["openai"]["apiKey"], "AB_KEY_OPENAI")
        self.assertEqual(doc["providers"]["llama-cpp"]["apiKey"], "AB_KEY_LLAMA_CPP")
        self.assertEqual(domains, ["api.openai.com", "generativelanguage.googleapis.com"])
        self.assertEqual(sorted(used), ["gemini", "llama-cpp", "openai"])

    def test_openrouter_domain_from_table_and_base_url(self):
        _doc, domains, _used = self.build(["openrouter"])
        self.assertEqual(domains, ["openrouter.ai"])

    def test_seed_holding_a_key_is_refused(self):
        with open(FIXTURE_MODELS) as f:
            seed = miniyaml.loads(f.read())
        with self.assertRaises(AgentboxError) as cm:
            self.build(seed=seed)
        self.assertIn("remove `apiKey`", str(cm.exception))

    def test_command_values_and_credential_headers_refused(self):
        for conf in ({"baseUrl": "https://x.example.com/v1", "headers": {"Authorization": "x"}},
                     {"baseUrl": "https://x.example.com/v1", "headers": {"X-Title": "!cat /etc/passwd"}}):
            with self.subTest(conf=conf), self.assertRaises(AgentboxError):
                self.build(["x"], seed={"providers": {"x": conf}})

    def test_unknown_provider_needs_a_base_url_and_private_hosts_are_dropped(self):
        warnings = []
        doc, _d, used = self.build(["mystery", "lan"], warnings=warnings, seed={"providers": {
            "mystery": {"api": "openai-completions"},
            "lan": {"baseUrl": "http://192.168.1.5:8000/v1"}}})
        self.assertEqual(list(doc["providers"]), ["llama-cpp"])           # default local provider added
        self.assertEqual(used, {})
        self.assertTrue(any("mystery" in w and "no known API domain" in w for w in warnings))
        self.assertTrue(any("lan" in w and "not reachable" in w for w in warnings))

    def test_custom_provider_by_base_url(self):
        _doc, domains, used = self.build(["acme"], seed={"providers": {
            "acme": {"api": "openai-completions", "baseUrl": "https://llm.acme.example/v1"}}})
        self.assertEqual(domains, ["llm.acme.example"])
        self.assertIn("acme", used)

    def test_no_seed(self):
        doc, domains, _used = self.build(seed={})
        self.assertEqual(doc["providers"]["llama-cpp"]["baseUrl"], "http://10.0.0.2:8080/v1")
        self.assertEqual(domains, [])

    def test_unused_key_is_reported(self):
        warnings = []
        _doc, _d, used = self.build(["cohere"], warnings=warnings)
        self.assertNotIn("cohere", used)
        self.assertTrue(any("'cohere' not used" in w for w in warnings))


class OmpMounts(TempCase):
    """Against the real layout (tests/ompfake.py): plugins -> versions/19760485e9c2, repos and pylibs ->
    versions/c1b51fef8911, a plugin linked to /root/.omp/versions/19760485e9c2/repos/..., legacy unused."""

    def setUp(self):
        super().setUp()
        self.home = ompfake.build(self.mkdir("omp"))

    def mounts(self):
        return omp.omp_mounts(self.home, lambda m: None)

    def test_each_link_mounted_from_its_real_target(self):
        mounts, _check = self.mounts()
        c = os.path.join(HOME, ".omp")
        by_dst = {d: s for s, d in mounts}
        v = lambda ver, name: os.path.join(self.home, "versions", ver, name)
        self.assertEqual(by_dst[f"{c}/plugins"], v(ompfake.PLUGINS_VERSION, "plugins"))
        self.assertEqual(by_dst[f"{c}/repos"], v(ompfake.ACTIVE, "repos"))
        self.assertEqual(by_dst[f"{c}/pylibs"], v(ompfake.ACTIVE, "pylibs"))
        self.assertEqual(sorted(by_dst), sorted([
            f"{c}/versions/{ompfake.ACTIVE}", f"{c}/versions/{ompfake.PLUGINS_VERSION}",
            f"{c}/plugins", f"{c}/repos", f"{c}/pylibs",
            f"{c}/agent/config.yml", f"{c}/bin/mags-codedev", f"{c}/.cache/ms-playwright"]))
        srcs = [s for s, _d in mounts]
        for s in srcs:
            self.assertTrue(ompfake.is_mounted(os.path.relpath(s, self.home)) or
                            os.path.relpath(s, self.home) in (f"versions/{ompfake.ACTIVE}",
                                                              f"versions/{ompfake.PLUGINS_VERSION}"), s)
        # never: an unreferenced version, models.yml and its backup, .mcp.json, sessions, databases, extensions
        for bad in ("agent/models.yml", "agent/models.yml.backup", "agent/.mcp.json", "agent/mags-codedev.yaml",
                    "agent/sessions", "agent/agent.db", "agent/extensions", "versions/legacy-20260905172136",
                    "run", "logs", "puppeteer", ".cache/pip", ".omp-sandbox.json"):
            self.assertFalse(any(s == os.path.join(self.home, bad) or s.startswith(os.path.join(self.home, bad) + "/")
                                 for s in srcs), bad)

    def test_version_reached_only_through_a_plugin_link_is_mounted(self):
        nm = os.path.join(self.home, "versions", ompfake.PLUGINS_VERSION, "plugins", "node_modules")
        os.makedirs(os.path.join(nm, "@scope"))
        os.symlink("/root/.omp/versions/legacy-20260905172136/plugins", os.path.join(nm, "@scope", "old"))
        mounts, _check = self.mounts()
        self.assertIn(os.path.join(HOME, ".omp", "versions", "legacy-20260905172136"), [d for _s, d in mounts])

    def test_missing_target_refused(self):
        os.rename(os.path.join(self.home, "versions", ompfake.ACTIVE, "pylibs"),
                  os.path.join(self.home, "versions", ompfake.ACTIVE, "pylibs.gone"))
        with self.assertRaises(AgentboxError) as cm:
            self.mounts()
        self.assertIn("missing", str(cm.exception))

    def test_link_changed_during_startup_refused(self):
        _mounts, check = self.mounts()
        check()                                              # unchanged: fine
        os.unlink(os.path.join(self.home, "pylibs"))
        os.makedirs(os.path.join(self.home, "versions", ompfake.PLUGINS_VERSION, "pylibs"))
        os.symlink(f"versions/{ompfake.PLUGINS_VERSION}/pylibs", os.path.join(self.home, "pylibs"))
        with self.assertRaises(AgentboxError) as cm:
            check()
        self.assertIn("changed while the session was starting", str(cm.exception))

    def test_link_out_of_omp_refused(self):
        os.unlink(os.path.join(self.home, "pylibs"))
        os.symlink(self.mkdir("elsewhere"), os.path.join(self.home, "pylibs"))
        with self.assertRaises(AgentboxError):
            self.mounts()

    def test_targets_must_be_their_own_versions_entry(self):
        """Review 2026-10-01 #3: `pylibs -> .` or `plugins -> agent` would have mounted all of ~/.omp (keys
        included) or every project's agent dir, because only "inside ~/.omp" was checked."""
        for name, target in (("pylibs", "."), ("plugins", "agent"), ("repos", f"versions/{ompfake.ACTIVE}"),
                             ("pylibs", f"versions/{ompfake.ACTIVE}/repos")):
            with self.subTest(name=name, target=target):
                link = os.path.join(self.home, name)
                old = os.readlink(link)
                os.unlink(link)
                os.symlink(target, link)
                try:
                    with self.assertRaises(AgentboxError):
                        self.mounts()
                finally:
                    os.unlink(link)
                    os.symlink(old, link)

    def test_version_symlink_leading_out_of_versions_refused(self):
        vdir = os.path.join(self.home, "versions", ompfake.ACTIVE)
        elsewhere = os.path.join(self.home, "agent", "stash")
        os.rename(vdir, elsewhere)
        os.symlink(elsewhere, vdir)
        with self.assertRaises(AgentboxError):
            self.mounts()

    def test_version_dir_swapped_during_startup_refused(self):
        _mounts, check = self.mounts()
        vdir = os.path.join(self.home, "versions", ompfake.PLUGINS_VERSION)
        os.rename(vdir, vdir + ".moved")
        os.symlink(os.path.join(self.home, "agent"), vdir)
        with self.assertRaises(AgentboxError):
            check()

    def test_hard_linked_credential_file_refused(self):
        os.link(os.path.join(self.home, "agent", "models.yml"),
                os.path.join(self.home, "versions", ompfake.ACTIVE, "pylibs", "innocent.txt"))
        with self.assertRaises(AgentboxError) as cm:
            self.mounts()
        self.assertIn("hard link", str(cm.exception))

    def test_repo_with_credentials_in_a_remote_url_refused(self):
        git = os.path.join(self.home, "versions", ompfake.ACTIVE, "repos", "mags-codedev", ".git")
        os.makedirs(git)
        with open(os.path.join(git, "config"), "w") as f:
            f.write('[remote "origin"]\n\turl = https://x-access-token:ghp_FAKE@github.com/t/r.git\n')
        with self.assertRaises(AgentboxError) as cm:
            self.mounts()
        self.assertIn("mags-codedev", str(cm.exception))
        with open(os.path.join(git, "config"), "w") as f:
            f.write('[remote "origin"]\n\turl = https://github.com/t/r.git\n')
        self.mounts()

    def test_symlinked_config_refused(self):
        cfg = os.path.join(self.home, "agent", "config.yml")
        os.unlink(cfg)
        os.symlink(os.path.join(self.home, "agent", "models.yml"), cfg)
        with self.assertRaises(AgentboxError):
            self.mounts()

    def test_unprovisioned_home_warns(self):
        for name in ("plugins", "repos", "pylibs"):
            os.unlink(os.path.join(self.home, name))
        warnings = []
        mounts, _check = omp.omp_mounts(self.home, warnings.append)
        self.assertTrue(warnings)
        self.assertFalse(any("/versions/" in d for _s, d in mounts))


class OmpSessionBase(SettingsCase):
    """Fixture and helpers only, so another class can reuse them without inheriting the cases."""

    KEY = "fake-openai-key-for-unit-tests-0123456789"

    def setUp(self):
        super().setUp()
        self.omp_home = ompfake.build(self.mkdir("omp"))
        with open(os.path.join(self.cfg, "agentbox.toml"), "a") as f:
            f.write(f'omp_home = "{self.omp_home}"\n')
        os.makedirs(os.path.join(self.cfg, "omp"))
        with open(os.path.join(self.cfg, "omp", "models.yml"), "w") as f:
            f.write(seed_without_keys())
        self.set_key("openai", self.KEY)

    def set_key(self, provider, value):
        with mock.patch("sys.stderr", io.StringIO()):
            keys.set_key(self.settings_now(), provider, read=lambda prompt: value)

    def settings_now(self):
        from agentbox import config
        return config.load()

    def make(self, preset="python", **extra):
        self.project("p1", preset=preset, **extra)
        s = session.make_session(self.settings_now(), "omp", session.load_project(self.settings_now(), "p1"),
                                 warn=lambda m: None)
        s.prepare(do_backup=False)       # the pre-session backup needs rsync and is not what these test
        self.addCleanup(s.teardown)
        return s


class OmpSession(OmpSessionBase):
    def test_key_reaches_the_agent_only_through_the_wrapper(self):
        s = self.make()
        s.start_infra()
        argv = s.run_args(s.agent_command())
        joined = "\n".join(argv)
        self.assertNotIn(self.KEY, joined)
        self.assertNotIn("--env=AB_KEY_OPENAI", joined)
        self.assertIn(f"--mount=type=bind,src={os.path.realpath(keys.key_file(s.settings, 'openai'))},"
                      "dst=/run/agentbox/keys/openai,ro", argv)
        wrapper = argv[argv.index("-c") + 1]
        self.assertIn("read -r AB_KEY_OPENAI < /run/agentbox/keys/openai; export AB_KEY_OPENAI", wrapper)
        self.assertEqual(s.secret_env_names, ["AB_KEY_OPENAI"])
        for dirpath, _d, files in os.walk(s.settings.session_run_dir(s.name)):
            for fn in files:
                with open(os.path.join(dirpath, fn)) as f:
                    self.assertNotIn(self.KEY, f.read(), fn)

    def test_generated_models_and_proxy_domains(self):
        s = self.make(preset="lockdown")
        with open(os.path.join(s.settings.session_run_dir(s.name), "omp-models.yml")) as f:
            doc = json.load(f)
        self.assertEqual(sorted(doc["providers"]), ["llama-cpp", "openai"])
        self.assertEqual(doc["providers"]["openai"]["apiKey"], "AB_KEY_OPENAI")
        conf = s.squid_conf()
        self.assertIn("acl models dstdomain api.openai.com", conf)          # every mode, even locked
        self.assertIn("http_access allow models", conf)
        self.assertEqual(s.model_domains, ["api.openai.com"])

    def test_mounts_and_approvals(self):
        s = self.make()
        s.start_infra()
        argv = s.run_args(s.agent_command())
        c = os.path.join(HOME, ".omp")
        mounts = [a for a in argv if a.startswith("--mount=")]
        agent_state = os.path.join(s.state_dir, "agent")
        self.assertIn(f"--mount=type=bind,src={agent_state},dst={c}/agent", mounts)
        self.assertIn(f"--mount=type=bind,src={os.path.join(s.state_dir, 'logs')},dst={c}/logs", mounts)
        for fn in ("models.yml", "AGENTS.md", "config.yml"):
            m = [x for x in mounts if x.split("dst=")[1].split(",")[0] == f"{c}/agent/{fn}"]
            self.assertEqual(len(m), 1, fn)
            self.assertTrue(m[0].endswith(",ro"), m[0])
            self.assertTrue(os.path.isfile(os.path.join(agent_state, fn)), fn)
        for m in mounts:
            if f"src={self.omp_home}" in m or f"src={os.path.realpath(self.omp_home)}" in m:
                self.assertTrue(m.endswith(",ro"), m)
        self.assertIn(f"--mount=type=tmpfs,dst={c},tmpfs-mode=0700,U=true,notmpcopyup", mounts)  # user-owned ~/.omp
        self.assertIn("--approval-mode=yolo", argv)                        # python preset: approvals off
        self.assertIn(f"--env=PYTHONPATH={c}/pylibs", argv)

    def test_user_files_mounted_only_when_created(self):
        warnings = []
        self.project("p1")
        s = session.make_session(self.settings_now(), "omp", session.load_project(self.settings_now(), "p1"),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        self.assertEqual([w for w in warnings if "mcp.json" in w],
                         [w for w in warnings if "mcp.json or mags-codedev.yaml" in w])
        self.assertEqual(len([w for w in warnings if "mcp.json" in w]), 1, "one line when both are missing")
        c = os.path.join(HOME, ".omp")
        self.assertFalse([m for m in s.setup.mounts if m[1] == f"{c}/agent/mags-codedev.yaml"])
        mcp = [m for m in s.setup.mounts if m[1] == f"{c}/agent/.mcp.json"]
        self.assertEqual(len(mcp), 1)
        self.assertTrue(mcp[0][0].endswith("omp-empty") and mcp[0][2], "absent: an empty read-only placeholder")
        s.teardown()
        # the host's ~/.omp copies are never used, even though they exist in the fake home
        with open(os.path.join(self.cfg, "omp", "mcp.json"), "w") as f:
            f.write('{"mcpServers": {}}\n')
        s2 = session.make_session(self.settings_now(), "omp", session.load_project(self.settings_now(), "p1"),
                                  warn=warnings.append)
        s2.prepare(do_backup=False)
        self.addCleanup(s2.teardown)
        m = [x for x in s2.setup.mounts if x[1] == f"{c}/agent/.mcp.json"]
        self.assertEqual(m, [(os.path.join(self.cfg, "omp", "mcp.json"), f"{c}/agent/.mcp.json", True)])
        self.assertTrue(any("no mags-codedev.yaml in" in w for w in warnings))

    def test_code_paths_in_agent_state_are_read_only(self):
        """Review 2026-10-01 #2: a session cannot plant an extension, hook, tool or MCP server in the
        project's persistent agent dir for later sessions to run."""
        s = self.make()
        s.start_infra()
        argv = s.run_args(s.agent_command())
        c = os.path.join(HOME, ".omp")
        for d in ("extensions", "hooks", "tools"):
            self.assertIn(f"--mount=type=tmpfs,dst={c}/agent/{d},tmpfs-mode=0700,U=true,notmpcopyup,ro", argv)
        for f in (".mcp.json", "mcp.json", "settings.json"):
            m = [a for a in argv if a.startswith("--mount=") and f"dst={c}/agent/{f}," in a]
            self.assertEqual(len(m), 1, f)
            self.assertTrue(m[0].endswith(",ro") and "omp-empty" in m[0], m[0])

    def test_lockdown_asks(self):
        s = self.make(preset="lockdown")
        self.assertIn("--approval-mode=always-ask", s.agent_command())
        self.assertIn("--no-extensions", s.agent_command())

    def test_lockdown_covers_repo_code(self):
        root = self.project("p1", preset="lockdown")
        for rel in (".omp/tools/t.ts", ".claude/hooks/pre/h.ts", ".mcp.json", ".claude/settings.json"):
            self.write(os.path.relpath(os.path.join(root, rel), self.tmp), "x")
        s = session.make_session(self.settings_now(), "omp", session.load_project(self.settings_now(), "p1"),
                                 warn=lambda m: None)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        s.start_infra()
        argv = s.run_args(s.agent_command())
        # the project's .omp is covered whole (review step 5C, L1)
        self.assertIn(f"--mount=type=tmpfs,dst={root}/.omp,tmpfs-mode=0700,U=true,notmpcopyup,ro", argv)
        self.assertIn(f"--mount=type=bind,src={s.mask_file},dst={root}/.claude/settings.json,ro", argv)
        self.assertIn(f"--mount=type=tmpfs,dst={root}/.claude/hooks,tmpfs-mode=0700,U=true,notmpcopyup,ro", argv)
        self.assertIn(f"--mount=type=bind,src={s.mask_file},dst={root}/.mcp.json,ro", argv)
        self.assertFalse([a for a in argv if f"{root}/.claude/extensions" in a], "only what exists is covered")
        lines = [a for a in s.run_args(s.agent_command()) if "LOCKDOWN" in a or "agentbox-lockdown" in a]
        self.assertTrue(lines)

    def test_python_preset_leaves_repo_code_alone(self):
        root = self.project("p1")
        self.write(os.path.relpath(os.path.join(root, ".omp/tools/t.ts"), self.tmp), "x")
        s = session.make_session(self.settings_now(), "omp", session.load_project(self.settings_now(), "p1"),
                                 warn=lambda m: None)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        self.assertEqual(s.setup.project_covers, [])

    def test_agents_md_describes_the_session(self):
        s = self.make()
        with open(os.path.join(s.settings.session_run_dir(s.name), "omp-AGENTS.md")) as f:
            text = f.read()
        self.assertIn("agentbox sandbox", text)
        self.assertIn("llama-cpp, openai", text)
        self.assertNotIn(self.KEY, text)

    def test_symlink_planted_in_state_is_replaced(self):
        s = self.make()
        s.teardown()
        target = os.path.join(self.tmp, "victim")
        with open(target, "w") as f:
            f.write("keep\n")
        link = os.path.join(s.state_dir, "agent", "models.yml")
        os.unlink(link)
        os.symlink(target, link)
        s2 = session.make_session(self.settings_now(), "omp", session.load_project(self.settings_now(), "p1"),
                                  warn=lambda m: None)
        s2.prepare(do_backup=False)
        self.addCleanup(s2.teardown)
        self.assertFalse(os.path.islink(link))
        with open(target) as f:
            self.assertEqual(f.read(), "keep\n")

    def test_bad_key_file_refuses_and_leaves_nothing(self):
        os.chmod(keys.key_file(self.settings_now(), "openai"), 0o644)
        self.project("p1")
        s = session.make_session(self.settings_now(), "omp", session.load_project(self.settings_now(), "p1"),
                                 warn=lambda m: None)
        with self.assertRaises(AgentboxError):
            s.prepare(do_backup=False)
        sessions_dir = os.path.join(self.settings.run_dir, "sessions")
        self.assertEqual(os.listdir(sessions_dir) if os.path.isdir(sessions_dir) else [], [])


class KeysCommand(SettingsCase):
    def test_set_warns_and_writes_0600(self):
        err = io.StringIO()
        keys.set_key(self.settings, "openai", read=lambda p: "sk-test-value", out=err)
        self.assertIn("spending cap", err.getvalue())
        self.assertIn("made only for agentbox", err.getvalue())
        path = keys.key_file(self.settings, "openai")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertEqual(keys.present(self.settings), {"openai": path})

    def test_refusals(self):
        for bad in ("", "has space"):
            with self.assertRaises(AgentboxError):
                keys.set_key(self.settings, "openai", read=lambda p, b=bad: b, out=io.StringIO())
        with self.assertRaises(AgentboxError):
            keys.key_file(self.settings, "../claude-oauth-token")

    def test_ls_never_prints_values(self):
        from agentbox import cli
        keys.set_key(self.settings, "gemini", read=lambda p: "AIzaFAKEVALUE", out=io.StringIO())
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["keys", "ls"]), 0)
        self.assertIn("gemini", out.getvalue())
        self.assertIn("AB_KEY_GEMINI", out.getvalue())
        self.assertNotIn("AIzaFAKEVALUE", out.getvalue())


class OmpInboxAndProfile(OmpSessionBase):
    """OMP gets the inbox half of the profile design today; its read-only LOAD paths wait on one unknown.

    `agent/{skills,rules,prompts,agents,commands}` are left writable because OMP's autolearn writes there
    (omp.py's own comment, since step 5C). A profile would occupy exactly those paths read-only, so wiring
    it without knowing whether autolearn's output directory can be pointed at the inbox would break
    autolearn silently -- and that cannot be determined from inside the sandbox (~/.omp is off limits and
    OMP is not public). docs/TODO.md carries the question and the options."""

    def test_the_inbox_is_mounted_read_write_at_a_path_omp_does_not_load(self):
        s = self.make()
        argv = s.run_args(s.agent_command())
        host = s.settings.inbox_dir("p1", "omp")
        dst = f"{omp.C_OMP}/agent/inbox"
        self.assertIn(f"--mount=type=bind,src={host},dst={dst}", argv)
        self.assertNotIn(f"--mount=type=bind,src={host},dst={dst},ro", argv)
        self.assertNotIn("inbox", " ".join(omp.CODE_DIRS + omp.CODE_FILES))

    def test_the_inbox_is_mounted_after_the_agent_dir_it_sits_inside(self):
        """`agent/` is itself a bind; a mount inside it has to come later or it would be shadowed."""
        s = self.make()
        argv = s.run_args(s.agent_command())
        agent_dir = [i for i, a in enumerate(argv) if a.endswith(f"dst={omp.C_OMP}/agent")]
        inbox = [i for i, a in enumerate(argv) if a.endswith(f"dst={omp.C_OMP}/agent/inbox")]
        self.assertTrue(agent_dir and inbox, argv)
        self.assertLess(agent_dir[0], inbox[0])

    def test_no_profile_load_paths_are_mounted_yet(self):
        s = self.make()
        self.assertEqual(omp.ADAPTER.profile_mounts, {})
        self.assertEqual(s.profile.mounts, [], "nothing is mounted over autolearn's directories")

    def test_the_profile_is_still_snapshotted_and_logged_for_omp(self):
        """So `ab profile harvest` and the log have something to refer to the moment the paths are wired."""
        src = self.settings_now().profile_src("omp")
        os.makedirs(os.path.join(src, "skills", "x"), exist_ok=True)
        with open(os.path.join(src, "skills", "x", "SKILL.md"), "w") as f:
            f.write("later\n")
        s = self.make()
        self.assertTrue(s.profile.hash)
        self.assertIn(f"hash={s.profile.hash}", open(s.log_path("profile")).read())
        self.assertIn("loaded=(nothing)", open(s.log_path("profile")).read())

    def test_autolearns_own_directories_are_still_writable(self):
        s = self.make()
        argv = " ".join(s.run_args(s.agent_command()))
        for d in ("skills", "rules", "prompts", "agents", "commands"):
            self.assertNotIn(f"dst={omp.C_OMP}/agent/{d},ro", argv, d)
            self.assertNotIn(f"type=tmpfs,dst={omp.C_OMP}/agent/{d}", argv, d)
