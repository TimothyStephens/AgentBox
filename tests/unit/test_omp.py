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

from .helpers import SettingsCase, TempCase, read_file

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


class ThePluginTree(SettingsCase):
    """`plugin_mounts`: the two paths a session reads for plugins, both read-only.

    This replaced a whitelist, and the deleted tests are the measure of what owning the tree bought. They
    checked that a provisioned link pointed at `~/.omp/versions/<v>/<name>` and nothing else, that
    `pylibs -> .` could not mount the whole of `~/.omp` (keys included), that a version directory swapped
    mid-start was caught, that a credential file with a second hard link refused the session, and that a
    repo with credentials in a remote URL refused it -- every one of them there because `~/.omp` was a
    directory agentbox did not control and `omp-env install` could change it underneath a starting
    session. agentbox builds this tree, replaces it whole and mounts it read-only, so those questions do
    not arise. What is left is that it mounts the right two things and refuses a symlink."""

    DECLARED = ("[[plugin]]\n"
                'id = "%s"\n' % ompfake.PLUGIN_ID +
                'marketplace = "%s"\n' % ompfake.MARKETPLACE +
                'source = "google/elephant-goldfish-model"\n' +
                'ref = "%s"\n' % ompfake.SHA)

    def mounts(self, warn=None, **kw):
        return omp.plugin_mounts(self.settings, warn or (lambda m: None), **kw)

    def test_the_tree_and_the_marketplace_registry(self):
        tree = ompfake.build(self.cfg)
        self.assertEqual(self.mounts(), [(tree, omp.TREE_DST),
                                         (os.path.join(tree, ompfake.MARKETPLACES), omp.MARKETPLACES_DST)])

    def test_the_container_paths_are_the_ones_the_installer_used(self):
        """The install records absolute paths, so the two definitions must agree or a session mounts the
        tree somewhere its own recorded paths do not point."""
        from agentbox import installer
        self.assertEqual(omp.TREE_DST, installer.C_TREE)
        self.assertEqual(omp.MARKETPLACES_DST, os.path.join(installer.C_OMP, "marketplaces.json"))

    def test_no_tree_is_a_warning_and_no_mounts(self):
        """A profile that declares plugins nobody has installed is a normal state: OMP runs on its
        built-ins, and the session says which command fixes it."""
        said = []
        self.assertEqual(self.mounts(warn=said.append), [])
        self.assertTrue([w for w in said if "ab profile install omp" in w], said)

    def test_a_tree_without_marketplaces_json_warns_and_still_mounts_the_tree(self):
        tree = ompfake.build(self.cfg)
        os.unlink(os.path.join(tree, ompfake.MARKETPLACES))
        said = []
        self.assertEqual(self.mounts(warn=said.append), [(tree, omp.TREE_DST)])
        self.assertTrue([w for w in said if "marketplace" in w], said)

    def test_a_symlinked_tree_is_refused(self):
        tree = ompfake.tree_path(self.cfg)
        os.makedirs(os.path.dirname(tree), exist_ok=True)
        os.symlink(self.mkdir("elsewhere"), tree)
        with self.assertRaises(AgentboxError) as cm:
            self.mounts()
        self.assertIn("symlink", str(cm.exception))

    def test_a_symlinked_marketplaces_json_is_refused(self):
        tree = ompfake.build(self.cfg)
        mk = os.path.join(tree, ompfake.MARKETPLACES)
        os.unlink(mk)
        os.symlink(os.path.join(self.tmp, "anything"), mk)
        with self.assertRaises(AgentboxError) as cm:
            self.mounts()
        self.assertIn("symlink", str(cm.exception))

    def test_plugins_off_mounts_nothing_and_says_nothing(self):
        ompfake.build(self.cfg)
        said = []
        self.assertEqual(self.mounts(warn=said.append, plugins=False), [])
        self.assertEqual(said, [])


class TheTreesStaleness(SettingsCase):
    """What a session says about a tree that no longer fits. The drift this feature exists to end is
    plugins installed against one OMP and loaded by another, so a session has to be able to say so."""

    def notes(self, labels, *, plugins=True):
        said = []
        omp.plugin_notes(self.settings, said.append, labels, plugins=plugins)
        return said

    def fits(self):
        return {"agentbox.cli-version": ompfake.CLI_VERSION, "agentbox.node-major": ompfake.NODE_MAJOR}

    def declare(self, text=None):
        src = self.settings.profile_src("omp")
        os.makedirs(src, mode=0o700, exist_ok=True)
        with open(os.path.join(src, "plugins.toml"), "w") as f:
            f.write(ThePluginTree.DECLARED if text is None else text)

    def test_a_tree_that_fits_says_nothing(self):
        ompfake.build(self.cfg)
        self.declare()
        self.assertEqual(self.notes(self.fits()), [])

    def test_a_different_agent_version_is_reported(self):
        ompfake.build(self.cfg)
        self.declare()
        said = self.notes({**self.fits(), "agentbox.cli-version": "18.99.0"})
        self.assertEqual(len(said), 1, said)
        self.assertIn(ompfake.CLI_VERSION, said[0])
        self.assertIn("18.99.0", said[0])
        self.assertIn("ab profile install omp", said[0])

    def test_a_different_node_major_is_reported(self):
        """node 22 under a 26 tag was invisible for a day (5 Nov). A tree built against the wrong node is
        the same class of thing, and this is where it surfaces."""
        ompfake.build(self.cfg)
        self.declare()
        said = self.notes({**self.fits(), "agentbox.node-major": "22"})
        self.assertEqual(len(said), 1, said)
        self.assertIn("node major", said[0])

    def test_an_unreadable_label_says_nothing_rather_than_guessing(self):
        """This reports and decides nothing: an image whose labels cannot be read is not evidence."""
        ompfake.build(self.cfg)
        self.declare()
        self.assertEqual(self.notes({}), [])

    def test_an_edited_plugins_toml_is_reported(self):
        ompfake.build(self.cfg)
        self.declare('[[npm]]\nname = "pi-lens"\nversion = "4.4.9"\n')
        said = self.notes(self.fits())
        self.assertEqual(len(said), 1, said)
        self.assertIn("plugins.toml has changed", said[0])

    def test_a_broken_plugins_toml_warns_but_does_not_stop_the_session(self):
        ompfake.build(self.cfg)
        self.declare('[[npm]]\nname = "pi-lens"\nversion = "latest"\n')
        said = self.notes(self.fits())
        self.assertTrue([w for w in said if "not an exact version" in w], said)

    def test_a_tree_with_no_lockfile_is_reported(self):
        tree = ompfake.build(self.cfg)
        os.unlink(os.path.join(tree, ompfake.LOCKFILE))
        self.declare()
        said = self.notes(self.fits())
        self.assertEqual(len(said), 1, said)
        self.assertIn("install.json", said[0])

    def test_nothing_is_said_when_there_is_no_tree_at_all(self):
        """`plugin_mounts` already said it, and saying it twice trains the reader to skim."""
        self.declare()
        self.assertEqual(self.notes(self.fits()), [])

    def test_nothing_is_said_with_the_profile_off(self):
        ompfake.build(self.cfg)
        self.declare()
        self.assertEqual(self.notes(self.fits(), plugins=False), [])


class OmpSessionBase(SettingsCase):
    """Fixture and helpers only, so another class can reuse them without inheriting the cases."""

    KEY = "fake-openai-key-for-unit-tests-0123456789"

    def setUp(self):
        super().setUp()
        # The installed plugin tree, where `ab profile install omp` puts it. There is no setting pointing
        # anywhere else since 5 Nov: the host's ~/.omp is not read at all, so a test that wants plugins
        # puts a tree in the config dir it already controls.
        self.tree = ompfake.build(self.cfg)
        os.makedirs(os.path.join(self.cfg, "omp"), exist_ok=True)
        with open(os.path.join(self.cfg, "omp", "models.yml"), "w") as f:
            f.write(seed_without_keys())
        with open(os.path.join(self.cfg, "omp", "config.yml"), "w") as f:
            f.write("# agentbox's own config.yml\n{}\n")
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
        for fn in ("models.yml", "AGENTS.md"):
            m = [x for x in mounts if x.split("dst=")[1].split(",")[0] == f"{c}/agent/{fn}"]
            self.assertEqual(len(m), 1, fn)
            self.assertTrue(m[0].endswith(",ro"), m[0])
            self.assertTrue(os.path.isfile(os.path.join(agent_state, fn)), fn)
        # config.yml: an overlay beside agent/, named in PI_CONFIG_FILES; agent/config.yml itself is not a
        # mount, so OMP's write-and-rename save works (12 Nov).
        dsts = [x.split("dst=")[1].split(",")[0] for x in mounts]
        self.assertNotIn(f"{c}/agent/config.yml", dsts)
        m = [x for x in mounts if x.split("dst=")[1].split(",")[0] == f"{c}/agentbox-config.yml"]
        self.assertEqual(len(m), 1, "the host config.yml is mounted as the overlay")
        self.assertTrue(m[0].endswith(",ro"), m[0])
        self.assertIn(f"--env=PI_CONFIG_FILES={c}/agentbox-config.yml", argv)
        for m in mounts:
            if f"src={self.tree}" in m or f"src={os.path.realpath(self.tree)}" in m:
                self.assertTrue(m.endswith(",ro"), m)
        self.assertIn(f"--mount=type=tmpfs,dst={c},tmpfs-mode=0700,U=true,notmpcopyup", mounts)  # user-owned ~/.omp
        self.assertIn("--approval-mode=yolo", argv)                        # python preset: approvals off
        # No PYTHONPATH: `pylibs` was an omp-env directory of python libraries for plugins, and nothing
        # declares one (plugins.toml has no [[pylib]] section, deliberately). It comes back with a
        # section, pinned and hashed, if a plugin ever needs it.
        self.assertFalse([a for a in argv if a.startswith("--env=PYTHONPATH=")])

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

    def test_config_yml_comes_from_agentboxs_own_seed_as_an_overlay(self):
        """The point of the 4 Nov move: it used to be mounted from `~/.omp/agent/config.yml`, the host
        OMP's OWN config, so changing the default model of the OMP Tim runs himself changed it for every
        agentbox session. Since 12 Nov it is an overlay (PI_CONFIG_FILES ranks above OMP's own config.yml)
        rather than a mount over `agent/config.yml`: OMP saves settings by write-and-rename, and a rename onto
        a bind mount is EBUSY, which killed the first interactive session that saved anything. Pinned here:
        the overlay comes from the seed, read-only, is named in the env, and `agent/config.yml` is left as
        writable per-project state; no seed, no overlay and no variable."""
        s = self.make()
        c = os.path.join(HOME, ".omp")
        m = [x for x in s.setup.mounts if x[1] == f"{c}/agentbox-config.yml"]
        self.assertEqual(m, [(os.path.join(self.cfg, "omp", "config.yml"), f"{c}/agentbox-config.yml", True)])
        self.assertEqual(s.setup.env.get("PI_CONFIG_FILES"), f"{c}/agentbox-config.yml")
        self.assertFalse([x for x in s.setup.mounts if x[1] == f"{c}/agent/config.yml"],
                         "agent/config.yml is OMP's to write; a mount there makes its save EBUSY")
        s.teardown()
        os.remove(os.path.join(self.cfg, "omp", "config.yml"))
        s2 = session.make_session(self.settings_now(), "omp", session.load_project(self.settings_now(), "p1"),
                                  warn=lambda m: None)
        s2.prepare(do_backup=False)
        self.addCleanup(s2.teardown)
        self.assertFalse([x for x in s2.setup.mounts if x[1] == f"{c}/agentbox-config.yml"])
        self.assertNotIn("PI_CONFIG_FILES", s2.setup.env)

    def test_every_source_a_session_mounts_is_agentboxs_own(self):
        """The property the whole `~/.omp` story was working towards, and it is structural now rather
        than a whitelist: every mount source is under agentbox's config dir, its data dir or this
        session's run dir. Nothing a session reads comes from a directory agentbox does not own -- so
        there is no host OMP install to validate, race, or be surprised by."""
        s = self.make()
        owned = tuple(os.path.realpath(d) + os.sep for d in
                      (self.cfg, self.data, self.settings.session_run_dir(s.name), s.project.root))
        for src, dst, _ro in s.setup.mounts:
            with self.subTest(dst=dst):
                self.assertTrue(os.path.realpath(src).startswith(owned),
                                f"{src} is outside everything agentbox owns")
        self.assertIn(self.tree, [src for src, _d, _ro in s.setup.mounts],
                      "the installed tree is mounted, or this test is vacuous")

    def test_no_code_path_reads_a_host_omp_home(self):
        """Belt to the braces above: the adapter no longer has the words. `omp_home` was a setting, and
        with it went `provisioned_targets`, `referenced_versions`, `_version_dir`, `_plain` and
        `check_no_credentials` -- about 160 lines whose only reason to exist was that ~/.omp belonged to
        omp-env."""
        import inspect
        src = inspect.getsource(omp)
        for gone in ("omp_home", "provisioned_targets", "referenced_versions", "check_no_credentials",
                     "PROVISIONED"):
            self.assertNotIn(gone, src, f"{gone} is back")
        from agentbox import config
        self.assertNotIn("omp_home", config.DEFAULTS)
        self.assertIn("omp_home", config.RETIRED, "an old agentbox.toml must be told, not refused")

    def test_an_old_agentbox_toml_with_omp_home_still_starts(self):
        """Removing a setting must not brick an install that used it. Refusing to start would punish
        whoever was using the feature we removed; ignoring the line silently would leave them believing it
        still does something. So it reports what replaced it and carries on -- and an unknown setting is
        still refused, because that is a typo and not a retirement."""
        import contextlib
        import io
        from agentbox import config
        with open(os.path.join(self.cfg, "agentbox.toml"), "a") as f:
            f.write('omp_home = "/nonexistent"\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            s = config.load()
        self.assertIn("omp_home is no longer used", err.getvalue())
        self.assertIn("ab profile install omp", err.getvalue())
        self.assertEqual(s.name_prefix, "abt-unit-", "the rest of the file still applied")
        with open(os.path.join(self.cfg, "agentbox.toml"), "a") as f:
            f.write('ompp_home = "/typo"\n')
        with self.assertRaises(ValueError):
            config.load()

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
    """OMP's profile load paths, wired 29 Oct once the one open question was answered from the public
    source (oh-my-pi: `packages/coding-agent/src/autolearn/managed-skills.ts`, `discovery/builtin.ts`,
    `docs/skills.md`): **autolearn writes only to `agent/managed-skills`**, never to `agent/skills`, and
    both are discovered unconditionally.

    That is what makes a read-only profile safe here. The five authored directories carry the host-owned
    profile, read-only; autolearn keeps its own directory, writable -- but per session, harvested into the
    project's inbox at teardown, because a skill the agent wrote itself is a proposal, not something later
    sessions should load unreviewed."""

    def argv(self, s):
        return s.run_args(s.agent_command())

    def test_the_inbox_is_mounted_read_write_at_a_path_omp_does_not_load(self):
        s = self.make()
        argv = self.argv(s)
        host = s.settings.inbox_dir("p1", "omp")
        dst = f"{omp.C_OMP}/agent/inbox"
        self.assertIn(f"--mount=type=bind,src={host},dst={dst}", argv)
        self.assertNotIn(f"--mount=type=bind,src={host},dst={dst},ro", argv)
        self.assertNotIn("inbox", " ".join(omp.CODE_DIRS + omp.CODE_FILES + omp.PROFILE_DIRS))

    def test_the_inbox_is_mounted_after_the_agent_dir_it_sits_inside(self):
        """`agent/` is itself a bind; a mount inside it has to come later or it would be shadowed."""
        s = self.make()
        argv = self.argv(s)
        agent_dir = [i for i, a in enumerate(argv) if a.endswith(f"dst={omp.C_OMP}/agent")]
        inbox = [i for i, a in enumerate(argv) if a.endswith(f"dst={omp.C_OMP}/agent/inbox")]
        self.assertTrue(agent_dir and inbox, argv)
        self.assertLess(agent_dir[0], inbox[0])

    # --- the profile, read-only at OMP's authored paths ---

    def profile_with(self, *dirs):
        src = self.settings_now().profile_src("omp")
        for d in dirs:
            os.makedirs(os.path.join(src, d), exist_ok=True)
            with open(os.path.join(src, d, "item.md"), "w") as f:
                f.write(f"profile {d}\n")
        return src

    def test_the_profile_is_mounted_read_only_at_omps_own_load_paths(self):
        self.profile_with(*omp.PROFILE_DIRS)
        s = self.make()
        argv = self.argv(s)
        for d in omp.PROFILE_DIRS:
            with self.subTest(dir=d):
                src = os.path.join(s.profile.dir, d)
                self.assertIn(f"--mount=type=bind,src={src},dst={omp.C_OMP}/agent/{d},ro", argv)

    def test_the_mount_points_are_created_under_the_state_dir(self):
        """`~/.omp/agent` mirrors `<state>/agent`, so these are ours to make -- the same fix as Claude's,
        and the reason the end-of-session state scan does not report them as changes."""
        self.profile_with("skills", "rules")
        s = self.make()
        self.addCleanup(s.teardown)
        agent = os.path.join(s.state_dir, "agent")
        for d in ("skills", "rules"):
            self.assertTrue(os.path.isdir(os.path.join(agent, d)), d)
            self.assertEqual(os.stat(os.path.join(agent, d)).st_uid, os.getuid())
        self.assertFalse(os.path.exists(os.path.join(agent, "prompts")), "not mounted, so not created")

    def test_a_profile_item_omp_does_not_have_is_not_mounted(self):
        self.profile_with("skills")
        s = self.make()
        argv = " ".join(self.argv(s))
        self.assertIn(f"dst={omp.C_OMP}/agent/skills,ro", argv)
        for d in ("rules", "prompts", "agents", "commands"):
            self.assertNotIn(f"dst={omp.C_OMP}/agent/{d},ro", argv, d)

    def test_the_log_records_what_was_loaded(self):
        self.profile_with("skills", "commands")
        s = self.make()
        with open(s.log_path("profile")) as f:
            lines = f.read().splitlines()
        self.assertIn(f"hash={s.profile.hash}", lines[0])
        self.assertIn("agent=omp", lines[0])
        self.assertEqual(sorted(lines[0].split("loaded=")[1].strip().split(",")), ["commands", "skills"])
        self.assertEqual(lines[1], f"image omp={s.image}", "and which image actually ran")

    # --- autolearn's own directory: writable, per session, harvested ---

    def test_autolearns_directory_is_writable_and_empty_and_per_session(self):
        s = self.make()
        argv = self.argv(s)
        host = os.path.join(self.settings_now().session_run_dir(s.name), "omp-managed-skills")
        dst = f"{omp.C_OMP}/{omp.AUTOLEARN_DIR}"
        self.assertIn(f"--mount=type=bind,src={host},dst={dst}", argv, "read-write")
        self.assertNotIn(f"--mount=type=bind,src={host},dst={dst},ro", argv)
        self.assertEqual(os.listdir(host), [], "empty at the start of every session")
        self.assertTrue(host.startswith(self.settings_now().run_dir), "in the session's run dir, not state")

    def test_what_autolearn_writes_ends_up_in_the_inbox_with_provenance(self):
        from agentbox import profile
        s = self.make()
        learned = os.path.join(self.settings_now().session_run_dir(s.name), "omp-managed-skills", "cool-trick")
        os.makedirs(learned)
        with open(os.path.join(learned, "SKILL.md"), "w") as f:
            f.write("something OMP worked out\n")
        s.teardown()
        inbox = self.settings_now().inbox_dir("p1", "omp")
        self.assertEqual(profile.items(inbox), ["learned-cool-trick"])
        self.assertEqual(read_file(os.path.join(inbox, "learned-cool-trick", "SKILL.md")),
                         "something OMP worked out\n")
        doc = profile.read_sidecar(inbox, "learned-cool-trick")
        self.assertEqual((doc["project"], doc["agent"], doc["reviewed"]), ("p1", "omp", False))
        self.assertEqual(doc["session"], s.name)

    def test_a_loose_file_is_harvested_too_not_just_a_directory(self):
        """Autolearn's output need not be a directory -- a single note is as likely -- so the harvest must
        move whatever it finds, not only trees."""
        from agentbox import profile
        s = self.make()
        managed = os.path.join(self.settings_now().session_run_dir(s.name), "omp-managed-skills")
        with open(os.path.join(managed, "one-liner.md"), "w") as f:
            f.write("remember this\n")
        s.teardown()
        inbox = self.settings_now().inbox_dir("p1", "omp")
        self.assertEqual(profile.items(inbox), ["learned-one-liner.md"])
        self.assertEqual(read_file(os.path.join(inbox, "learned-one-liner.md")), "remember this\n")
        self.assertFalse(os.path.exists(os.path.join(managed, "one-liner.md")), "moved, not copied")

    def test_nothing_in_it_survives_into_the_next_session(self):
        s = self.make()
        run_dir = self.settings_now().session_run_dir(s.name)
        os.makedirs(os.path.join(run_dir, "omp-managed-skills", "gone"))
        s.teardown()
        self.assertFalse(os.path.exists(os.path.join(run_dir, "omp-managed-skills")))
        s2 = self.make()
        self.addCleanup(s2.teardown)
        fresh = os.path.join(self.settings_now().session_run_dir(s2.name), "omp-managed-skills")
        self.assertEqual(os.listdir(fresh), [], "the next session starts with an empty one")

    def test_lockdown_keeps_nothing_it_learned(self):
        s = self.make(preset="lockdown")
        self.assertTrue(s.inbox_is_ephemeral)
        learned = os.path.join(self.settings_now().session_run_dir(s.name), "omp-managed-skills", "x")
        os.makedirs(learned)
        with open(os.path.join(learned, "SKILL.md"), "w") as f:
            f.write("not kept\n")
        s.teardown()
        self.assertFalse(os.path.exists(self.settings_now().inbox_dir("p1", "omp")),
                         "no persistent inbox for a lockdown project")
        ephemeral = os.path.join(self.settings_now().data_dir, "ephemeral")
        self.assertEqual(os.listdir(ephemeral) if os.path.isdir(ephemeral) else [], [],
                         "the session's whole state, inbox and harvest included, is gone")

    def test_the_notes_tell_omp_where_its_learned_skills_go(self):
        self.profile_with("skills")
        s = self.make()
        self.addCleanup(s.teardown)
        notes = session.sandbox_notes(s.project, s.egress, profile=s.profile, inbox=s.inbox_mount,
                                      adapter=s.adapter)
        self.assertIn(omp.AUTOLEARN_DIR, notes)
        self.assertIn("ab profile harvest", notes)
        self.assertIn("read-only", notes)
        self.assertNotIn("`#` note", notes, "that is Claude's shortcut, not OMP's")
