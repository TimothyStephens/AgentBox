"""`ab screen` (agentbox/screen.py, from contrib/ab-ws on 10 Nov): the tmux workspace. Every agent pane
enters the sandbox through `ab session start`; --dry-run prints the tmux commands, so the layout logic is
testable without tmux."""
import os

from agentbox import screen

from .helpers import SettingsCase


class Screen(SettingsCase):
    def test_dry_run_starts_agents_only_through_ab_session_start(self):
        code, out = self.ab("screen", "--dry-run", "web", "rev")
        self.assertEqual(code, 0, out)
        self.assertIn("send-keys -t %1 'ab session start claude web' Enter", out)
        self.assertIn("'ab session start claude rev'", out)
        self.assertNotIn("'ab session start claude rev' Enter", out, "the reviewer is typed, not started")
        self.assertIn("tmux new-session -d -s ab-web", out)
        self.assertIn("tmux attach -t ab-web", out)
        self.assertNotIn("ab run", out)

    def test_the_agent_flag_and_a_panes_own_key(self):
        code, out = self.ab("screen", "--dry-run", "--agent", "omp", "web")
        self.assertEqual(code, 0, out)
        self.assertIn("'ab session start omp web'", out)
        cfg = os.path.join(self.settings.config_dir, screen.CONFIG_NAME)
        with open(cfg, "w") as f:
            f.write('default = "one"\n[[layout]]\nname = "one"\nmin_cols = 0\n[[layout.window]]\nname = "w"\n'
                    'panes = [ { column = 1, run = "builder", agent = "omp" }, { column = 2, run = "shell" } ]\n')
        code, out = self.ab("screen", "--dry-run", "web")
        self.assertEqual(code, 0, out)
        self.assertIn("'ab session start omp web'", out, "the pane's own agent key wins over the default")
        self.assertIn(f"layout 'one' ({cfg})", out)

    def test_list_and_print_config(self):
        code, out = self.ab("screen", "--list")
        self.assertEqual(code, 0, out)
        self.assertIn("built-in defaults", out)
        self.assertIn("<- auto", out)
        code, out = self.ab("screen", "--print-config")
        self.assertEqual(code, 0, out)
        self.assertIn("[[layout]]", out)
        self.assertIn("ab screen --print-config", out)
        self.assertNotIn("ab-ws", out)

    def test_the_old_config_name_is_still_read(self):
        legacy = os.path.join(self.settings.config_dir, screen.LEGACY_CONFIG_NAME)
        with open(legacy, "w") as f:
            f.write('[[layout]]\nname = "legacy"\nmin_cols = 0\n[[layout.window]]\nname = "w"\n'
                    'panes = [ { column = 1, run = "builder" } ]\n')
        self.assertEqual(screen.config_path(self.settings), legacy)
        code, out = self.ab("screen", "--list")
        self.assertEqual(code, 0, out)
        self.assertIn("legacy", out)
        with open(os.path.join(self.settings.config_dir, screen.CONFIG_NAME), "w") as f:
            f.write('[[layout]]\nname = "new"\nmin_cols = 0\n[[layout.window]]\nname = "w"\n'
                    'panes = [ { column = 1, run = "builder" } ]\n')
        self.assertTrue(screen.config_path(self.settings).endswith(screen.CONFIG_NAME), "the new name wins")

    def test_no_builder_is_refused_with_the_spelling(self):
        code, out = self.ab("screen", "--dry-run")
        self.assertEqual(code, 2, out)
        self.assertIn("ab screen <project>", out)

    def test_a_bad_layout_name_is_refused(self):
        code, out = self.ab("screen", "--dry-run", "--layout", "nope", "web")
        self.assertEqual(code, 2, out)
        self.assertIn("no layout named 'nope'", out)
        self.assertIn("ab screen --list", out)
