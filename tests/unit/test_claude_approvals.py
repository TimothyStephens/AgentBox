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

    def policy_of(self, s):
        m = [a for a in s.run_args(["true"]) if "dst=/etc/claude-code/managed-settings.json" in a]
        self.assertEqual(len(m), 1)
        with open(m[0].split("src=")[1].split(",")[0]) as f:
            return json.load(f)

    def test_lockdown_carries_the_profiles_deny_rules_and_hooks_not_its_allow_rules(self):
        """`allowManagedPermissionRulesOnly` and `allowManagedHooksOnly` drop the user-scope settings too,
        and in a session that file is the profile's. The profile's deny list and hooks are what the user
        wants in force in every preset, lockdown most of all, so they ride in the managed file; its allow
        rules do not, because the point of the policy is that nothing widens permissions in lockdown."""
        hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.py"}]}]}
        src = self.settings.profile_src("claude")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "settings.json"), "w") as f:
            json.dump({"permissions": {"allow": ["Bash(make *)"], "deny": ["Bash(rm -rf *)", "Read(.env)"]},
                       "hooks": hooks, "theme": "dark"}, f)
        s, _doc = self.make("lockdown")
        policy = self.policy_of(s)
        self.assertEqual(policy["permissions"]["deny"], ["Bash(rm -rf *)", "Read(.env)"])
        self.assertEqual(policy["hooks"], hooks)
        self.assertNotIn("allow", policy["permissions"])
        self.assertNotIn("theme", policy)
        for k, v in claude.MANAGED_APPROVALS_ON.items():
            if k == "permissions":
                self.assertEqual(policy[k]["disableBypassPermissionsMode"], v["disableBypassPermissionsMode"])
            else:
                self.assertEqual(policy[k], v)
        self.assertEqual(claude.MANAGED_APPROVALS_ON["permissions"], {"disableBypassPermissionsMode": "disable"},
                         "the constant was written to")

    def test_lockdown_with_the_profile_off_promotes_nothing_the_agent_wrote(self):
        """With the profile off the state settings.json is never refreshed from the host, so it is the
        agent's to write; a hook planted there must not come back as a managed hook."""
        self.project("p1", preset="lockdown", profile="off")
        state = self.settings.state_dir("p1", "claude")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "settings.json"), "w") as f:
            json.dump({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "evil"}]}]},
                       "permissions": {"deny": ["x"]}}, f)
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare()
        self.addCleanup(s.teardown)
        self.assertEqual(self.policy_of(s), claude.MANAGED_APPROVALS_ON)

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
