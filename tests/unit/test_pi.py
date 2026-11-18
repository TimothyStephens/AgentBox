"""The Pi adapter (agentbox/adapters/pi.py; docs/pi-proposal.md, decisions P1-P4, 18 Nov 2026)."""
import json
import os

from agentbox import session
from agentbox.adapters import pi
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, read_file
from .test_omp import OmpSessionBase


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
