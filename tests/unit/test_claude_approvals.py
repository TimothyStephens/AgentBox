"""Review 2026-10-01 #2: with approvals on, Claude neither pre-trusts the folder nor accepts bypass mode, and a
managed policy keeps a repo's own hooks and permission rules from applying."""
import json
import os

from agentbox import session
from agentbox.adapters import claude

from .helpers import SettingsCase


class ClaudeApprovals(SettingsCase):
    def setUp(self):
        super().setUp()
        self.token()

    def make(self, preset):
        self.project("p1", preset=preset)
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare()
        self.addCleanup(s.teardown)
        with open(os.path.join(s.state_dir, ".claude.json")) as f:
            return s, json.load(f)

    def test_lockdown_does_not_pretrust_and_gets_the_managed_policy(self):
        s, doc = self.make("lockdown")
        self.assertIs(doc["bypassPermissionsModeAccepted"], False)
        self.assertIs(doc["projects"][s.project.root]["hasTrustDialogAccepted"], False)
        argv = s.run_args(["true"])
        m = [a for a in argv if "dst=/etc/claude-code/managed-settings.json" in a]
        self.assertEqual(len(m), 1)
        self.assertTrue(m[0].endswith(",ro"))
        src = m[0].split("src=")[1].split(",")[0]
        with open(src) as f:
            policy = json.load(f)
        self.assertEqual(policy, claude.MANAGED_APPROVALS_ON)
        self.assertNotIn("--dangerously-skip-permissions", s.agent_command())

    def test_approvals_off_keeps_trust_and_no_policy(self):
        s, doc = self.make("python")
        self.assertIs(doc["bypassPermissionsModeAccepted"], True)
        self.assertIs(doc["projects"][s.project.root]["hasTrustDialogAccepted"], True)
        self.assertFalse([a for a in s.run_args(["true"]) if "/etc/claude-code" in a])

    def test_switching_to_lockdown_revokes_earlier_trust(self):
        self.make("python")
        from agentbox import registry
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["preset"] = "lockdown"
        registry.save(self.settings.registry_path, reg)
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare()
        self.addCleanup(s.teardown)
        with open(os.path.join(s.state_dir, ".claude.json")) as f:
            doc = json.load(f)
        self.assertIs(doc["projects"][s.project.root]["hasTrustDialogAccepted"], False)
