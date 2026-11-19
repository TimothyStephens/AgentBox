"""The Pi adapter (agentbox/adapters/pi.py; docs/pi-proposal.md, decisions P1-P4, 18 Nov 2026)."""
import json
import os
import textwrap
import urllib.error
import urllib.request
from unittest import mock

from agentbox import session
from agentbox.adapters import pi
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, read_file
from .test_omp import OmpSessionBase

_REAL_FETCH = pi.fetch_models      # before SettingsCase replaces it with a fake for every test


class Models(SettingsCase):
    def test_pi_ids_field_names_and_key_references(self):
        doc = {"providers": {
            "llama-cpp": {"api": "openai-completions", "baseUrl": "http://192.0.2.2:8080/v1", "apiKey": "none",
                          "models": [{"id": "m", "maxOut": 16000, "reasoning": True}]},
            "gemini": {"api": "google-generative-ai", "apiKey": "AB_KEY_GEMINI"},
            "openai": {"api": "openai-completions", "apiKey": "AB_KEY_OPENAI",
                       "models": [{"id": "gpt", "maxOut": 8000}]},
            "custom": {"api": "openai-completions", "apiKey": "AB_KEY_CUSTOM"}}}
        notes = []
        out = pi.pi_models(doc, notes.append)["providers"]
        self.assertEqual(out["google"], {"api": "google-generative-ai", "apiKey": "${AB_KEY_GEMINI}"})
        self.assertNotIn("gemini", out)
        self.assertEqual(out["openai"]["apiKey"], "${AB_KEY_OPENAI}", "a bare value would be sent literally")
        self.assertEqual(out["openai"]["models"], [{"id": "gpt", "maxTokens": 8000}])
        self.assertEqual(out["llama-cpp"]["apiKey"], "none")
        self.assertEqual(out["llama-cpp"]["models"][0]["maxTokens"], 16000)
        self.assertNotIn("custom", out, "Pi would reject the whole file over it")
        self.assertTrue([n for n in notes if "custom" in n])


class LocalModels(SettingsCase):
    """TODO 19 Nov: build_models adds the local model server with no `models` (OMP discovers them), and Pi 1.5.2
    needs a list for a provider it does not know, so a seed that does not list the local models gave Pi no local
    model. Pi's own llama.cpp discovery does not apply (router mode only, a stored /login, network off under
    PI_OFFLINE), so the launcher asks the server and fills the list, or says what to add to the seed."""

    LOCAL = {"api": "openai-completions", "baseUrl": "http://192.0.2.2:8080/v1", "apiKey": "none"}

    def test_discovered_models_fill_the_local_provider(self):
        notes = []
        out = pi.pi_models({"providers": {"llama-cpp": dict(self.LOCAL)}}, notes.append,
                           discover=lambda name, conf: ["model-a", "model-b"])["providers"]
        self.assertEqual(out["llama-cpp"]["models"], [{"id": "model-a"}, {"id": "model-b"}])
        self.assertEqual(out["llama-cpp"]["baseUrl"], "http://192.0.2.2:8080/v1")
        self.assertEqual(notes, [])

    def test_a_failed_query_says_what_to_add_to_the_seed(self):
        def down(name, conf):
            raise OSError("timed out")
        for discover, reason in ((down, "timed out"), (lambda name, conf: [], "lists no models")):
            with self.subTest(reason=reason):
                notes = []
                out = pi.pi_models({"providers": {"llama-cpp": dict(self.LOCAL)}}, notes.append,
                                   discover=discover, seed="/tmp/seed/models.yml")["providers"]
                self.assertNotIn("llama-cpp", out)
                [note] = notes
                self.assertIn(reason, note)
                self.assertIn("/tmp/seed/models.yml", note)
                snippet = note[note.index("  providers:"):]
                from agentbox import miniyaml
                parsed = miniyaml.loads(textwrap.dedent(snippet).replace("<a model id the server serves>", "model-a"))
                self.assertEqual(parsed["providers"]["llama-cpp"],
                                 {"api": "openai-completions", "baseUrl": "http://192.0.2.2:8080/v1",
                                  "models": [{"id": "model-a"}]})

    def test_a_provider_discovery_does_not_apply_to_keeps_the_old_note(self):
        notes = []
        doc = {"providers": {"custom": {"api": "openai-completions", "baseUrl": "https://llm.example/v1"}}}
        out = pi.pi_models(doc, notes.append, discover=lambda name, conf: None)["providers"]
        self.assertNotIn("custom", out)
        self.assertIn("needs a baseUrl and models", notes[0])

    def test_only_the_configured_model_server_is_asked_with_its_key(self):
        key = os.path.join(self.tmp, "llama-cpp")
        with open(key, "w") as f:
            f.write("fake-local-key-0123\n")
        discover = pi.local_models("192.0.2.2", 8080, {"llama-cpp": key})
        self.fetch_models.side_effect = None
        self.fetch_models.return_value = ["model-a"]
        self.assertEqual(discover("llama-cpp", self.LOCAL), ["model-a"])
        self.fetch_models.assert_called_once_with("http://192.0.2.2:8080/v1/models", "fake-local-key-0123")
        self.fetch_models.reset_mock()
        for url in ("http://192.0.2.3:8080/v1", "http://192.0.2.2:8081/v1", "https://llm.example/v1"):
            with self.subTest(url=url):
                self.assertIsNone(discover("other", {"baseUrl": url}))
        self.assertIsNone(pi.local_models("", 8080, {})("llama-cpp", self.LOCAL))
        self.fetch_models.assert_not_called()
        pi.local_models("192.0.2.2", 8080, {})("llama-cpp", self.LOCAL)
        self.fetch_models.assert_called_once_with("http://192.0.2.2:8080/v1/models", None)


class FetchModels(SettingsCase):
    """pi.fetch_models with urllib's opener faked: nothing here opens a socket."""

    def run_fetch(self, answer, key=None):
        seen = {}

        class Opener:
            def open(self, req, timeout=None):
                seen["req"], seen["timeout"] = req, timeout
                if isinstance(answer, Exception):
                    raise answer
                body = answer

                class R:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def read(self, n=-1):
                        return body[:n] if n >= 0 else body
                return R()

        def build_opener(*handlers):
            seen["handlers"] = handlers
            return Opener()

        # SettingsCase fakes pi.fetch_models; _REAL_FETCH is the function itself, taken at import.
        with mock.patch.object(urllib.request, "build_opener", build_opener):
            ids = _REAL_FETCH("http://192.0.2.2:8080/v1/models", key)
        return ids, seen

    def test_reads_ids_directly_with_a_short_timeout_and_the_key_in_the_header(self):
        body = json.dumps({"object": "list", "data": [{"id": "model-a"}, {"id": "model-b"}, {"id": "model-a"},
                                                       {"id": ""}, {"nope": 1}, "x"]}).encode()
        ids, seen = self.run_fetch(body, key="fake-local-key-0123")
        self.assertEqual(ids, ["model-a", "model-b"])
        self.assertEqual(seen["timeout"], pi.DISCOVERY_TIMEOUT)
        self.assertEqual(seen["req"].get_header("Authorization"), "Bearer fake-local-key-0123")
        [proxy] = [h for h in seen["handlers"] if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(proxy.proxies, {}, "the LAN model server is never asked through a proxy")
        self.assertTrue([h for h in seen["handlers"] if h is pi._NoRedirect])

    def test_failures_are_oserror_or_valueerror_without_the_key(self):
        cases = ((urllib.error.URLError("refused"), OSError),
                 (urllib.error.HTTPError("http://192.0.2.2:8080/v1/models", 401, "no", {}, None), OSError),
                 (b"not json", ValueError), (b'{"data": {"id": "x"}}', ValueError),
                 (b"x" * (pi.DISCOVERY_MAX_BYTES + 1), ValueError))
        for answer, exc in cases:
            with self.subTest(answer=str(answer)[:40]), self.assertRaises(exc) as cm:
                self.run_fetch(answer, key="fake-local-key-0123")
            self.assertNotIn("fake-local-key-0123", str(cm.exception))




class Settings(SettingsCase):
    def write_seed(self, doc):
        d = self.settings.seed_dir("pi")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump(doc, f)
        return d

    def test_no_seed_still_forces_trust_and_telemetry_off(self):
        self.assertEqual(pi.settings_doc(self.settings.seed_dir("pi")),
                         {"defaultProjectTrust": "never", "enableInstallTelemetry": False})

    def test_a_seed_cannot_turn_them_back_on(self):
        d = self.write_seed({"defaultModel": "x", "defaultProjectTrust": "always", "enableInstallTelemetry": True})
        self.assertEqual(pi.settings_doc(d), {"defaultModel": "x", "defaultProjectTrust": "never",
                                              "enableInstallTelemetry": False})

    def test_code_running_keys_are_refused(self):
        for key in pi.CODE_SETTINGS:
            with self.subTest(key=key):
                d = self.write_seed({key: ["x"]})
                with self.assertRaises(AgentboxError):
                    pi.settings_doc(d)


class PiSession(OmpSessionBase):
    def make_pi(self, preset="python", **extra):
        self.project("p1", preset=preset, **extra)
        s = session.make_session(self.settings_now(), "pi", session.load_project(self.settings_now(), "p1"),
                                 warn=self.warned.append)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s

    def setUp(self):
        super().setUp()
        self.warned = []

    def mounts(self, argv):
        return {a.split("dst=")[1].split(",")[0]: a for a in argv if a.startswith("--mount=type=bind")}

    def test_the_session_layout(self):
        s = self.make_pi()
        argv = s.run_args(s.agent_command())
        m = self.mounts(argv)
        for fn in ("models.json", "AGENTS.md", "auth.json", "mcp.json"):
            with self.subTest(fn=fn):
                self.assertTrue(m[f"{pi.C_AGENT}/{fn}"].endswith(",ro"), fn)
        self.assertFalse(m[pi.C_AGENT].endswith(",ro"), "the per-project agent dir is Pi's to write")
        self.assertIn("--env=PI_OFFLINE=1", argv)
        self.assertIn("--env=PI_TELEMETRY=0", argv)
        for rel in pi.CODE_DIRS:
            with self.subTest(code_dir=rel):
                [cover] = [a for a in argv if a.startswith(f"--mount=type=tmpfs,dst={os.path.join(pi.C_PI, rel)},")]
                self.assertTrue(cover.endswith(",ro"), cover)
        self.assertIn("--no-approve", argv[argv.index("localhost/agentbox-pi:latest"):])
        self.assertNotIn(self.KEY, "\n".join(argv))
        wrapper = argv[argv.index("-c") + 1]
        self.assertIn("read -r AB_KEY_OPENAI < /run/agentbox/keys/openai; export AB_KEY_OPENAI", wrapper)
        models = json.loads(read_file(os.path.join(s.settings.session_run_dir(s.name), "pi-models.json")))
        self.assertEqual(models["providers"]["openai"]["apiKey"], "${AB_KEY_OPENAI}")
        settings = json.loads(read_file(os.path.join(s.state_dir, "agent", "settings.json")))
        self.assertEqual(settings["defaultProjectTrust"], "never")

    def seed_without_local_models(self):
        with open(os.path.join(self.cfg, "omp", "models.yml"), "w") as f:
            f.write("providers:\n  openai:\n    api: openai-completions\n")

    def pi_models_json(self, s):
        return json.loads(read_file(os.path.join(s.settings.session_run_dir(s.name), "pi-models.json")))

    def test_the_local_server_is_asked_for_its_models_when_the_seed_lists_none(self):
        self.seed_without_local_models()
        self.fetch_models.side_effect = None
        self.fetch_models.return_value = ["model-a"]
        s = self.make_pi()
        self.fetch_models.assert_called_once_with("http://192.0.2.2:8080/v1/models", None)
        local = self.pi_models_json(s)["providers"]["llama-cpp"]
        self.assertEqual(local["models"], [{"id": "model-a"}])
        self.assertEqual(local["baseUrl"], "http://192.0.2.2:8080/v1")
        self.assertFalse([w for w in self.warned if "llama-cpp" in w and "left out" in w])

    def test_an_unreachable_local_server_leaves_a_note_with_the_seed_entry(self):
        self.seed_without_local_models()
        s = self.make_pi()             # SettingsCase's fake fetch fails, as an unreachable server does
        self.assertNotIn("llama-cpp", self.pi_models_json(s)["providers"])
        [note] = [w for w in self.warned if "llama-cpp" in w and "left out" in w]
        self.assertIn(os.path.join(self.cfg, "omp", "models.yml"), note)
        self.assertIn("models:", note)
        self.assertNotIn(self.KEY, note)
        agents = read_file(os.path.join(s.settings.session_run_dir(s.name), "pi-AGENTS.md"))
        self.assertIn("configured for this session: openai.", agents, "the notes name only what Pi got")

    def test_a_seed_that_lists_the_local_models_is_used_as_it_is(self):
        s = self.make_pi()
        self.fetch_models.assert_not_called()
        self.assertTrue(self.pi_models_json(s)["providers"]["llama-cpp"]["models"])

    def test_refused_under_approvals_on(self):
        with self.assertRaises(AgentboxError) as cm:
            self.make_pi(preset="lockdown")
        self.assertIn("approvals", str(cm.exception))

    def test_files_pi_would_load_at_start_are_moved_to_the_inbox(self):
        s = self.make_pi()
        agent = os.path.join(s.state_dir, "agent")
        for name in ("SYSTEM.md", "AGENTS.override.md", "trust.json"):
            with open(os.path.join(agent, name), "w") as f:
                f.write("steer the next session")
        s.teardown()
        s2 = self.make_pi()
        for name in ("SYSTEM.md", "AGENTS.override.md", "trust.json"):
            self.assertFalse(os.path.exists(os.path.join(agent, name)), name)
        self.assertTrue([f for f in os.listdir(s2.inbox) if f.endswith("SYSTEM.md")])
        self.assertTrue([w for w in self.warned if "inbox" in w and "SYSTEM.md" in w])

    def test_the_repos_pi_dir_is_covered(self):
        root = self.project("p1", preset="python")
        os.makedirs(os.path.join(root, ".pi", "extensions"))
        s = self.make_pi()
        self.assertIn(os.path.join(root, ".pi"), s.setup.project_covers)

    def test_toulmin_gets_pi_mcp_with_direct_exposure(self):
        s = self.make_pi()
        server = {"type": "stdio", "command": "/run/agentbox/bin/tm-bridge", "args": ["/run/agentbox/toulmin/sock"]}
        pi.ADAPTER.mcp_server(s, s.setup, "toulmin", server)
        [(src, dst, ro)] = [m for m in s.setup.mounts if m[1] == f"{pi.C_AGENT}/mcp.json"]
        self.assertTrue(ro)
        self.assertEqual(json.loads(read_file(src)), {"mcpServers": {"toulmin": {**server, "exposure": "direct"}}})


class SeedDollar(OmpSessionBase):
    def test_a_dollar_in_the_shared_seed_is_refused_for_both_agents(self):
        from agentbox.adapters import omp
        seed = {"providers": {"openai": {"baseUrl": "https://api.openai.com/v1",
                                         "headers": {"X-Forward": "${AB_KEY_ANTHROPIC}"}}}}
        for label in ("OMP", "Pi"):
            with self.subTest(label=label), self.assertRaises(AgentboxError) as cm:
                omp.build_models(seed, keys={"openai": "/k"}, model_host="", model_port=8080,
                                 warn=lambda m: None, label=label)
            self.assertIn("$", str(cm.exception))


class SettingsSeedStays(SettingsCase):
    """Found while building Pi packages (19 Nov): profile.migrate_legacy_seed moved ~/.config/agentbox/pi/settings.json
    into pi/profile/ like a pre-profile Claude seed, so the Pi settings seed vanished at the first session and its
    refusals never saw the file. Pi never had a pre-profile layout: nothing of its seed dir is migrated."""

    def test_pis_settings_seed_is_not_migrated_into_the_profile(self):
        from agentbox import profile
        d = self.settings.seed_dir("pi")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "settings.json"), "w") as f:
            f.write('{"defaultModel": "x"}\n')
        self.assertEqual(profile.migrate_legacy_seed(self.settings, "pi"), [])
        self.assertTrue(os.path.isfile(os.path.join(d, "settings.json")))
        self.assertEqual(pi.settings_doc(d)["defaultModel"], "x")
