"""Agent memory persists per project across sessions; make it visible (Tim, 2 Oct, after trial 2): the
end-of-session scan diffs memory/instruction/settings files of the agent state, `ab project reset-state` wipes
it (asking first, keeping a copy), and lockdown sessions start from empty state every time."""
import os
from unittest import mock

from agentbox import cli, scan, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, TempCase


class StateScan(TempCase):
    def test_changes_are_listed_with_a_diff(self):
        st = self.mkdir("state")
        self.write("state/CLAUDE.md", "rules\n")
        self.write("state/projects/-p/memory/MEMORY.md", "- [a](a.md) — first\n")
        self.write("state/projects/-p/abc.jsonl", "transcript\n")
        self.write("state/projects/-p/memory/service-secret.json", '{"x": "hidden-value-1"}')
        self.write("state/settings.json", '{"a": 1}')
        before = scan.state_snapshot(st)
        self.assertNotIn("projects/-p/abc.jsonl", before, "transcripts are not looked at")
        self.assertNotIn("text", before["projects/-p/memory/service-secret.json"], "credential-like: no content")
        self.write("state/projects/-p/memory/MEMORY.md", "- [a](a.md) — rewritten by the agent\n")
        self.write("state/projects/-p/memory/new.md", "a new memory\n")
        self.write("state/projects/-p/memory/service-secret.json", '{"x": "hidden-value-2"}')
        self.write("state/projects/-p/abc.jsonl", "more transcript\n")
        summary, lines = scan.state_report(before, st)
        text = "\n".join(lines)
        self.assertTrue(summary and "agent memory" in summary[0], summary)
        self.assertIn("projects/-p/memory/MEMORY.md (changed)", text)
        self.assertIn("+- [a](a.md) — rewritten by the agent", text)
        self.assertIn("projects/-p/memory/new.md (new)", text)
        self.assertIn("projects/-p/memory/service-secret.json (changed)", text)
        self.assertNotIn("hidden-value", text, "credential-like files: names only")
        self.assertNotIn("abc.jsonl", text)
        self.assertEqual(scan.state_report(scan.state_snapshot(st), st), ([], []))


class StateLifecycle(SettingsCase):
    def setUp(self):
        super().setUp()
        self.token()
        self.project("p1")
        self.project("lk", preset="lockdown")
        self.warned = []

    def make(self, name):
        return session.make_session(self.settings, "claude", session.load_project(self.settings, name),
                                    warn=self.warned.append)

    def test_memory_change_reported_at_teardown(self):
        s = self.make("p1")
        s.prepare()
        mem = os.path.join(s.state_dir, "projects", "-x", "memory")
        os.makedirs(mem)
        with open(os.path.join(mem, "MEMORY.md"), "w") as f:
            f.write("- remembered\n")
        self.warned.clear()
        s.teardown()
        self.assertTrue(any("agent memory" in w for w in self.warned), self.warned)
        logs = os.path.join(self.settings.data_dir, "logs")
        report = [fn for fn in os.listdir(logs) if fn.endswith("-scan.log")]
        with open(os.path.join(logs, report[-1])) as f:
            self.assertIn("+- remembered", f.read())
        self.assertTrue(os.path.isfile(os.path.join(mem, "MEMORY.md")), "kept for the project")

    def test_settings_json_the_agent_writes_at_startup_is_not_flagged(self):
        """7 Oct: on a fresh project with approvals off, Claude Code writes settings.json
        ({skipDangerousModePermissionPrompt: true}) on first start. The adapter establishes that in the scan
        baseline, so the agent's own startup write is not reported as a change the user made."""
        import json
        s = self.make("p1")                              # python preset: approvals off
        s.prepare()
        settings = os.path.join(s.state_dir, "settings.json")
        self.assertTrue(os.path.isfile(settings), "the approvals-off settings.json is established at start")
        scan_state = os.path.join(self.settings.session_run_dir(s.name), "scan-state.json")
        with open(scan_state) as f:
            self.assertIn("settings.json", json.load(f)["agent_state"]["files"], "it is in the scan baseline")
        # the agent's first-run write produces exactly this; simulate it, then a session that otherwise does
        # not touch settings.json must produce no settings line.
        with open(settings, "w") as f:
            json.dump({"skipDangerousModePermissionPrompt": True}, f, indent=2)
        self.warned.clear()
        s.teardown()
        self.assertFalse(any("settings" in w.lower() for w in self.warned),
                         f"settings.json flagged though the agent only wrote its startup default: {self.warned}")

    def test_a_real_settings_edit_is_still_flagged(self):
        """The suppression is only the startup default: a genuine edit the agent makes is still reported."""
        import json
        s = self.make("p1")
        s.prepare()
        with open(os.path.join(s.state_dir, "settings.json"), "w") as f:
            json.dump({"skipDangerousModePermissionPrompt": True, "env": {"EDITED_BY_AGENT": "1"}}, f, indent=2)
        self.warned.clear()
        s.teardown()
        self.assertTrue(any("settings.json" in w for w in self.warned),
                        f"a real settings.json edit should be flagged: {self.warned}")

    def test_lockdown_starts_empty_and_leaves_nothing(self):
        persistent = self.settings.state_dir("lk", "claude")
        os.makedirs(os.path.join(persistent, "projects", "-x", "memory"))
        with open(os.path.join(persistent, "projects", "-x", "memory", "MEMORY.md"), "w") as f:
            f.write("old memory\n")
        s = self.make("lk")
        s.prepare()
        self.assertNotEqual(s.state_dir, persistent)
        self.assertFalse(os.path.exists(os.path.join(s.state_dir, "projects")), "empty agent state")
        argv = " ".join(s.run_args(s.agent_command()))
        self.assertIn(f"src={s.state_dir},", argv)
        self.assertNotIn(f"src={persistent},", argv)
        state = s.state_dir
        s.teardown()
        self.assertFalse(os.path.exists(state), "removed at teardown")

    def test_reset_state(self):
        st = self.settings.state_dir("p1", "claude")
        os.makedirs(st)
        with open(os.path.join(st, "CLAUDE.md"), "w") as f:
            f.write("x\n")
        with mock.patch("builtins.input", return_value="n"):
            self.assertEqual(cli.main(["project", "reset-state", "p1"]), 1)
        self.assertTrue(os.path.isdir(st), "declined: kept")
        import json
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(
            [{"Names": ["s1-agent"], "State": "running",
              "Labels": {"agentbox.project": "p1", "agentbox.session": "s1", "agentbox-test": "1"}}])
        try:                                  # a running container of p1 blocks the reset (audit F2)
            with self.assertRaises(AgentboxError):
                cli.cmd_project_reset_state(self.settings, type("A", (), {"name": "p1", "yes": True})())
        finally:
            os.environ.pop("FAKE_PODMAN_PSJSON", None)
        with mock.patch("builtins.input", return_value="y"):
            self.assertEqual(cli.main(["project", "reset-state", "p1"]), 0)
        self.assertFalse(os.path.exists(os.path.join(self.settings.data_dir, "p1")))
        kept = os.listdir(os.path.join(self.settings.backups_dir, "p1"))
        self.assertTrue(any(k.startswith("agent-state-") for k in kept), kept)
