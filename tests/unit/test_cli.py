"""CLI ergonomics and the forgotten-session guards (Tim, 6 Oct):
  - ab ls shows start time, age and idle time, and flags a long-idle running session;
  - ab run warns when a session for that project is already running, and takes positional agent/project;
  - ab project rm takes several names."""
import io
import os
import time
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from agentbox import cli, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase


class IdleTest(SettingsCase):
    def test_idle_seconds_tracks_agent_state_mtime(self):
        meta = {"project": "p1", "agent": "claude"}
        self.assertIsNone(session.idle_seconds(self.settings, meta), "no state dir yet")
        sd = os.path.join(self.settings.state_dir("p1", "claude"))
        os.makedirs(sd)
        f = os.path.join(sd, "transcript.jsonl")
        open(f, "w").close()
        old = time.time() - 7200
        os.utime(f, (old, old))
        os.utime(sd, (old, old))
        idle = session.idle_seconds(self.settings, meta)
        self.assertAlmostEqual(idle, 7200, delta=120)

    def test_idle_none_without_meta(self):
        self.assertIsNone(session.idle_seconds(self.settings, {}))
        self.assertIsNone(session.idle_seconds(self.settings, None))


class LsTest(SettingsCase):
    def rows(self, idle_hours):
        sd = self.settings.state_dir("p1", "claude")
        os.makedirs(sd, exist_ok=True)
        f = os.path.join(sd, "x")
        open(f, "w").close()
        t = time.time() - idle_hours * 3600
        os.utime(f, (t, t))
        os.utime(sd, (t, t))
        meta = {"project": "p1", "agent": "claude", "started": time.time() - 30 * 3600}
        return {"abt-s-1": {"meta": meta, "containers": [], "networks": [], "alive": True}}

    def run_ls(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli.main(["ls"])
        return out.getvalue(), err.getvalue()

    def test_ls_shows_age_and_idle_and_flags_long_idle(self):
        with unittest.mock.patch("agentbox.session.sessions", return_value=self.rows(idle_hours=20)):
            out, err = self.run_ls()
        self.assertIn("age 1d6h", out)                 # 30h since start
        self.assertIn("idle", out)
        self.assertIn("<-- idle", out, "a 20h-idle session is flagged in the listing")
        self.assertIn("ab kill abt-s-1", err, "and warned about, with how to end it")

    def test_ls_does_not_flag_a_busy_session(self):
        with unittest.mock.patch("agentbox.session.sessions", return_value=self.rows(idle_hours=0)):
            out, err = self.run_ls()
        self.assertNotIn("<-- idle", out)
        self.assertEqual(err, "")


class RunGuardTest(SettingsCase):
    def setUp(self):
        super().setUp()
        self.project("p1")

    def test_run_warns_when_a_session_for_the_project_is_already_running(self):
        running = {"abt-old-1": {"meta": {"project": "p1", "agent": "claude"},
                                 "containers": [], "networks": [], "alive": True}}
        err = io.StringIO()
        with unittest.mock.patch("agentbox.session.sessions", return_value=running), \
             unittest.mock.patch("agentbox.session.load_project", side_effect=AgentboxError("stop here")):
            with redirect_stderr(err):
                rc = cli.main(["run", "--agent", "claude", "--project", "p1"])
        self.assertEqual(rc, 2)
        self.assertIn("already running (abt-old-1)", err.getvalue())

    def test_run_takes_positional_agent_and_project(self):
        seen = {}

        def fake_load(s, name, **kw):
            seen["project"] = name
            raise AgentboxError("stop here")

        with unittest.mock.patch("agentbox.session.sessions", return_value={}), \
             unittest.mock.patch("agentbox.session.load_project", side_effect=fake_load):
            with redirect_stderr(io.StringIO()):
                rc = cli.main(["run", "claude", "p1"])
        self.assertEqual(rc, 2)
        self.assertEqual(seen["project"], "p1")

    def test_run_needs_both(self):
        with redirect_stderr(io.StringIO()):
            rc = cli.main(["run", "claude"])
        self.assertEqual(rc, 2)


class ProjectRmTest(SettingsCase):
    def test_rm_multiple(self):
        self.project("p1")
        self.project("p2")
        self.project("p3")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["project", "rm", "p1", "p2"]), 0)
        from agentbox import registry
        left = registry.load(self.settings.registry_path)
        self.assertEqual(set(left), {"p3"})

    def test_rm_is_all_or_nothing(self):
        self.project("p1")
        with redirect_stderr(io.StringIO()):
            rc = cli.main(["project", "rm", "p1", "nope"])
        self.assertEqual(rc, 2)
        from agentbox import registry
        self.assertIn("p1", registry.load(self.settings.registry_path), "a bad name removes none")


if __name__ == "__main__":
    import unittest
    unittest.main()
