"""Claude state-dir seeding is symlink-safe and the injected sandbox block is refreshed, not stacked
(audit 2026-10-15 #1 and #2). The agent can write its own state dir between sessions, so a session must not
be able to make the next start follow a symlink left there to a host file the user owns."""
import json
import os
from unittest import mock

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

    def test_a_claude_md_symlink_in_the_state_dir_is_left_alone(self):
        """CLAUDE.md is generated per session and mounted read-only from the profile now, so nothing writes
        the state copy. A link an earlier session left there must still not be followed -- the migration
        that empties these paths into the inbox skips it rather than moving its target."""
        from agentbox import session
        md = os.path.join(self.state, "CLAUDE.md")
        os.symlink(self.secret, md)                       # a link the previous session left
        claude.ADAPTER.seed_state(self.settings, self.state, NOTES)
        self.assert_secret_intact()                       # not truncated, moved or overwritten
        self.assertTrue(os.path.islink(md), "left exactly as it was found")
        said = []
        with mock.patch.object(self.settings, "state_dir", lambda *a: self.state), \
             mock.patch.object(self.settings, "inbox_dir", lambda *a: self.mkdir("inbox")):
            moved = session.migrate_state_to_inbox(self.settings, "p1", claude.ADAPTER, said.append)
        self.assertEqual(moved, [], "a symlink is not migrated")
        self.assert_secret_intact()

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
    """The same two properties as before the profile existed -- the sandbox block appears once and leads,
    the user's own instructions follow and are not mangled -- now against the generated file, which is built
    fresh each session from the profile's CLAUDE.md instead of being edited in place."""

    def md(self):
        return claude.ADAPTER.session_instructions(self.settings, NOTES)

    def profile_md(self, text):
        self.write("config/claude/profile/CLAUDE.md", text)

    def test_the_block_appears_once_however_often_it_is_generated(self):
        for _ in range(3):
            body = self.md()
            self.assertEqual(body.count("SANDBOX-NOTES-SENTINEL"), 1)
            self.assertEqual(body.count(claude.NOTES_BEGIN), 1)
            self.assertEqual(body.count(claude.NOTES_END), 1)

    def test_a_stale_block_in_the_profile_is_stripped_not_stacked(self):
        """Someone could paste a session's CLAUDE.md into the profile, block and all."""
        self.profile_md(f"{claude.NOTES_BEGIN}\nOLD NOTES\n{claude.NOTES_END}\n\nUSER-RULE-SENTINEL\n")
        body = self.md()
        self.assertEqual(body.count(claude.NOTES_BEGIN), 1)
        self.assertNotIn("OLD NOTES", body)
        self.assertEqual(body.count("USER-RULE-SENTINEL"), 1)

    def test_profile_instructions_survive_and_follow_the_notes(self):
        self.profile_md("# My project rules\nUSER-RULE-SENTINEL\n")
        body = self.md()
        self.assertEqual(body.count("USER-RULE-SENTINEL"), 1)
        self.assertEqual(body.count("SANDBOX-NOTES-SENTINEL"), 1)
        self.assertLess(body.index("SANDBOX-NOTES-SENTINEL"), body.index("USER-RULE-SENTINEL"))

    def test_no_profile_instructions_is_just_the_notes(self):
        self.assertEqual(self.md().count("SANDBOX-NOTES-SENTINEL"), 1)
        self.assertNotIn("USER-RULE", self.md())
