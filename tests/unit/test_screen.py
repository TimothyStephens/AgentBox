"""`ab screen` (agentbox/screen.py, from contrib/ab-ws on 10 Nov): tmux workspaces, one per project, with
the verbs `ab session` has. Every agent pane enters the sandbox through `ab session start` (or `attach`
when the project is already live); every pane opens in the project's registered root. --dry-run prints
the tmux commands, and tmux queries are one function, so all of it is testable without tmux."""
import json
import os
from unittest import mock

from agentbox import screen

from .helpers import SettingsCase


def ps_running(session, project, role="agent"):
    return json.dumps([{"Names": [f"{session}-{role}"], "State": "running",
                        "Labels": {"agentbox.project": project, "agentbox.session": session,
                                   "agentbox.role": role, "agentbox-test": "1"}}])


class Start(SettingsCase):
    def setUp(self):
        super().setUp()
        self.web = self.project("web", preset="script")
        self.rev = self.project("rev", preset="script")

    def test_panes_open_in_the_registered_roots_and_start_agents_only_through_ab(self):
        code, out = self.ab("screen", "start", "--dry-run", "web", "rev")
        self.assertEqual(code, 0, out)
        self.assertIn(f"tmux new-session -d -s ab-web -n build -x 200 -y 50 -c {self.web}", out)
        self.assertIn("send-keys -t %1 'ab session start claude web' Enter", out)
        self.assertIn(f"new-window -d -t ab-web: -n review -c {self.rev}", out, "the reviewer's own root")
        self.assertIn("'ab session start claude rev'", out)
        self.assertNotIn("'ab session start claude rev' Enter", out, "the reviewer is typed, not started")
        self.assertIn("tmux attach -t ab-web", out)
        self.assertNotIn("ab run", out)

    def test_an_unregistered_project_is_refused_before_tmux_with_the_add_command(self):
        code, out = self.ab("screen", "start", "--dry-run", "nope")
        self.assertEqual(code, 2, out)
        self.assertIn("ab project add nope", out)
        self.assertNotIn("tmux", out)
        code, out = self.ab("screen", "start", "--dry-run", "web", "nope")
        self.assertEqual(code, 2, out)
        self.assertIn("ab project add nope", out)

    def test_a_project_with_a_live_session_gets_an_attach_pane(self):
        os.environ["FAKE_PODMAN_PSJSON"] = ps_running("ab-claude-web-1", "web")
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        code, out = self.ab("screen", "start", "--dry-run", "web", "rev")
        self.assertEqual(code, 0, out)
        self.assertIn("'ab session attach web' Enter", out)
        self.assertIn("'ab session start claude rev'", out, "the reviewer has no live session")
        self.assertIn("web already has a live session (ab-claude-web-1)", out)

    def test_the_agent_flag_and_a_panes_own_key(self):
        code, out = self.ab("screen", "start", "--dry-run", "--agent", "omp", "web")
        self.assertEqual(code, 0, out)
        self.assertIn("'ab session start omp web'", out)
        cfg = os.path.join(self.settings.config_dir, screen.CONFIG_NAME)
        with open(cfg, "w") as f:
            f.write('default = "one"\n[[layout]]\nname = "one"\nmin_cols = 0\n[[layout.window]]\nname = "w"\n'
                    'panes = [ { column = 1, run = "builder", agent = "omp" }, { column = 2, run = "shell" } ]\n')
        code, out = self.ab("screen", "start", "--dry-run", "web")
        self.assertEqual(code, 0, out)
        self.assertIn("'ab session start omp web'", out, "the pane's own agent key wins over the default")
        self.assertIn(f"layout 'one' ({cfg})", out)

    def test_an_open_workspace_is_not_opened_twice(self):
        with mock.patch.object(screen, "tmux_available", return_value=True), \
                mock.patch.object(screen, "_query", return_value="ab-web\t2\t1\n"):
            code, out = self.ab("screen", "start", "web")
        self.assertEqual(code, 2, out)
        self.assertIn("already open", out)
        self.assertIn("ab screen attach web", out)

    def test_without_tmux_the_refusal_names_it(self):
        with mock.patch.object(screen, "tmux_available", return_value=False):
            code, out = self.ab("screen", "start", "web")
        self.assertEqual(code, 2, out)
        self.assertIn("tmux is not on PATH", out)

    def test_a_bad_layout_name_is_refused(self):
        code, out = self.ab("screen", "start", "--dry-run", "--layout", "nope", "web")
        self.assertEqual(code, 2, out)
        self.assertIn("no layout named 'nope'", out)
        self.assertIn("ab screen --list", out.replace("ab screen layouts", "ab screen --list"))


class ListAttachKill(SettingsCase):
    LISTING = "ab-web\t2\t1\nab-other\t1\t0\nnot-ours\t1\t0\n"

    def setUp(self):
        super().setUp()
        self.project("web", preset="script")

    def test_ls_shows_workspaces_their_project_and_its_live_session(self):
        os.environ["FAKE_PODMAN_PSJSON"] = ps_running("ab-claude-web-1", "web")
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        with mock.patch.object(screen, "tmux_available", return_value=True), \
                mock.patch.object(screen, "_query", return_value=self.LISTING):
            code, out = self.ab("screen", "ls")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("WORKSPACE"))
        web = next(l for l in lines if l.startswith("ab-web "))
        self.assertIn("ab-claude-web-1", web)
        other = next(l for l in lines if l.startswith("ab-other "))
        self.assertIn("(not registered)", other)
        self.assertNotIn("not-ours", out, "only agentbox's own tmux sessions")

    def test_ls_without_tmux_or_without_workspaces(self):
        with mock.patch.object(screen, "tmux_available", return_value=False):
            code, out = self.ab("screen", "ls")
        self.assertEqual((code, "tmux is not on PATH" in out), (0, True), out)
        with mock.patch.object(screen, "tmux_available", return_value=True), \
                mock.patch.object(screen, "_query", return_value=None):
            code, out = self.ab("screen", "ls")
        self.assertEqual(code, 0, out)
        self.assertIn("no workspaces open", out)
        self.assertIn("ab screen start <project>", out)

    def test_attach_and_kill_name_the_open_workspaces_when_refusing(self):
        with mock.patch.object(screen, "tmux_available", return_value=True), \
                mock.patch.object(screen, "_query", return_value=self.LISTING):
            code, out = self.ab("screen", "attach", "nope")
            self.assertEqual(code, 2, out)
            self.assertIn("open: web, other", out)
            self.assertIn("ab screen start nope", out)
            code, out = self.ab("screen", "attach", "--dry-run", "web")
            self.assertEqual(code, 0, out)
            self.assertIn("tmux attach -t ab-web", out)
            code, out = self.ab("screen", "kill", "--dry-run", "web")
            self.assertEqual(code, 0, out)
            self.assertIn("tmux kill-session -t ab-web", out)
            code, out = self.ab("screen", "kill", "nope")
            self.assertEqual(code, 2, out)

    def test_attach_switches_from_inside_tmux(self):
        with mock.patch.object(screen, "tmux_available", return_value=True), \
                mock.patch.object(screen, "_query", return_value=self.LISTING), \
                mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,1,0"}):
            code, out = self.ab("screen", "attach", "--dry-run", "web")
        self.assertEqual(code, 0, out)
        self.assertIn("tmux switch-client -t ab-web", out)


class LayoutsAndConfig(SettingsCase):
    def test_layouts_and_config(self):
        code, out = self.ab("screen", "layouts")
        self.assertEqual(code, 0, out)
        self.assertIn("built-in defaults", out)
        self.assertIn("<- auto", out)
        code, out = self.ab("screen", "config")
        self.assertEqual(code, 0, out)
        self.assertIn("[[layout]]", out)
        self.assertIn("ab screen config", out.replace("ab screen --print-config", "ab screen config"))
        self.assertNotIn("ab-ws", out)
        self.assertNotIn("reviewer_dir", out, "the reviewer's root comes from the registry now")

    def test_the_old_config_name_is_still_read_and_its_old_key_tolerated(self):
        legacy = os.path.join(self.settings.config_dir, screen.LEGACY_CONFIG_NAME)
        with open(legacy, "w") as f:
            f.write('reviewer_dir = "~/repos/{reviewer}"\n[[layout]]\nname = "legacy"\nmin_cols = 0\n'
                    '[[layout.window]]\nname = "w"\npanes = [ { column = 1, run = "builder" } ]\n')
        self.assertEqual(screen.config_path(self.settings), legacy)
        code, out = self.ab("screen", "layouts")
        self.assertEqual(code, 0, out)
        self.assertIn("legacy", out)
        with open(os.path.join(self.settings.config_dir, screen.CONFIG_NAME), "w") as f:
            f.write('[[layout]]\nname = "new"\nmin_cols = 0\n[[layout.window]]\nname = "w"\n'
                    'panes = [ { column = 1, run = "builder" } ]\n')
        self.assertTrue(screen.config_path(self.settings).endswith(screen.CONFIG_NAME), "the new name wins")
