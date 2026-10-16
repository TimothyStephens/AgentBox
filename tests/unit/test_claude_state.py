"""Claude state-dir seeding is symlink-safe and the injected sandbox block is refreshed, not stacked
(audit 2026-10-15 #1 and #2). The agent can write its own state dir between sessions, so a session must not
be able to make the next start follow a symlink left there to a host file the user owns."""
import json
import os

from agentbox.adapters import claude

from .helpers import SettingsCase

NOTES = "SANDBOX-NOTES-SENTINEL: you are in a sandbox."


class ClaudeStateSymlinks(SettingsCase):
    def setUp(self):
        super().setUp()
        self.state = self.mkdir("state")
        self.secret = self.write("outside/host-secret.txt", "TOPSECRET-host-file")

    def assert_secret_intact(self):
        self.assertTrue(os.path.isfile(self.secret) and not os.path.islink(self.secret))
        with open(self.secret) as f:
            self.assertEqual(f.read(), "TOPSECRET-host-file")

    def assert_real_file(self, path):
        self.assertTrue(os.path.isfile(path), f"{path} missing")
        self.assertFalse(os.path.islink(path), f"{path} is still a symlink")

    def test_claude_md_symlink_is_not_followed(self):
        md = os.path.join(self.state, "CLAUDE.md")
        os.symlink(self.secret, md)                       # a link the previous session left
        claude.ADAPTER.seed_state(self.settings, self.state, NOTES)
        self.assert_secret_intact()                       # not truncated or overwritten
        self.assert_real_file(md)
        with open(md) as f:
            body = f.read()
        self.assertNotIn("TOPSECRET", body)               # host content not pulled in
        self.assertIn("SANDBOX-NOTES-SENTINEL", body)

    def test_settings_tmp_symlink_is_not_followed(self):
        path = os.path.join(self.state, "settings.json")
        os.symlink(self.secret, path + ".tmp")            # the .tmp sibling, planted as a link
        claude._merge_settings(path, {"skipDangerousModePermissionPrompt": True})
        self.assert_secret_intact()
        self.assert_real_file(path)
        with open(path) as f:
            self.assertIs(json.load(f)["skipDangerousModePermissionPrompt"], True)

    def test_settings_symlink_target_is_not_read_or_written(self):
        path = os.path.join(self.state, "settings.json")
        os.symlink(self.secret, path)
        claude._merge_settings(path, {"skipDangerousModePermissionPrompt": True})
        self.assert_secret_intact()
        self.assert_real_file(path)

    def test_claude_json_symlink_is_not_followed(self):
        path = os.path.join(self.state, ".claude.json")
        os.symlink(self.secret, path)
        claude._mark_onboarded(path, "/some/project", trusted=False)
        self.assert_secret_intact()
        self.assert_real_file(path)
        with open(path) as f:
            self.assertIs(json.load(f)["hasCompletedOnboarding"], True)


class ClaudeSandboxBlock(SettingsCase):
    def setUp(self):
        super().setUp()
        self.state = self.mkdir("state")

    def md(self):
        with open(os.path.join(self.state, "CLAUDE.md")) as f:
            return f.read()

    def test_block_does_not_stack_across_sessions(self):
        for _ in range(3):
            claude.ADAPTER.seed_state(self.settings, self.state, NOTES)
        body = self.md()
        self.assertEqual(body.count("SANDBOX-NOTES-SENTINEL"), 1)
        self.assertEqual(body.count(claude.NOTES_BEGIN), 1)
        self.assertEqual(body.count(claude.NOTES_END), 1)

    def test_user_content_survives_and_appears_once(self):
        md = os.path.join(self.state, "CLAUDE.md")
        with open(md, "w") as f:
            f.write("# My project rules\nUSER-RULE-SENTINEL\n")
        claude.ADAPTER.seed_state(self.settings, self.state, NOTES)
        claude.ADAPTER.seed_state(self.settings, self.state, NOTES)
        body = self.md()
        self.assertEqual(body.count("USER-RULE-SENTINEL"), 1)
        self.assertEqual(body.count("SANDBOX-NOTES-SENTINEL"), 1)
        self.assertTrue(body.index("SANDBOX-NOTES-SENTINEL") < body.index("USER-RULE-SENTINEL"))
