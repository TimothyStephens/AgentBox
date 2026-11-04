"""`AGENTBOX_SESSION`: a session says so, in one variable.

A host-only script -- a profile repo's `build.sh` that deploys or installs, anything that needs the LAN or
writes outside a grant -- has no cheap way to tell whether it is inside a sandbox. Guessing from the
filesystem is guesswork; this is one `[ -n "$AGENTBOX_SESSION" ]`.

The id is already a container label, and is in the scan state and the inbox provenance, so nothing new is
being tracked here: the session is just allowed to see its own name.
"""
import os
import unittest

from agentbox import session

from .helpers import SettingsCase
from .test_omp import OmpSessionBase


def env_of(args):
    """{name: value} from the `--env=NAME=VALUE` flags of a `podman run` argv."""
    return dict(a[len("--env="):].split("=", 1) for a in args if a.startswith("--env="))


class BothAgentsGetIt(SettingsCase):
    def setUp(self):
        super().setUp()
        self.token()
        self.project("p1", preset="script")

    def start(self, agent="claude"):
        s = session.make_session(self.settings, agent, session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s

    def test_it_is_the_session_id(self):
        s = self.start()
        env = env_of(s.run_args(["true"]))
        self.assertEqual(env.get("AGENTBOX_SESSION"), s.name)
        self.assertTrue(s.name, "and the session is named by then")

    def test_it_is_passed_exactly_once(self):
        s = self.start()
        names = [a for a in s.run_args(["true"]) if a.startswith("--env=AGENTBOX_SESSION=")]
        self.assertEqual(len(names), 1, names)

    def test_it_is_not_treated_as_a_credential_by_the_selftest(self):
        """`envnames` fails a session for a credential-shaped variable name; this one must not trip it."""
        from agentbox.selftest import runner
        self.assertFalse([w for w in runner.SECRET_NAME_WORDS if w in "AGENTBOX_SESSION"])

    def test_the_selftest_checks_it_is_set(self):
        """So `ab selftest` catches it in a real session, not only in this argv-level test."""
        from agentbox.selftest import runner
        s = self.start()
        b = runner.Builder()
        runner.filesystem_cases(b, project=s.project, plan=s.plan, other_roots=[], other_state_dirs=[],
                                secret_envs=[], state_mounts=[], host_protected=[], uid=os.getuid(),
                                required_tools=[])
        envset = [c for c in b.cases if c.op == "envset" and c.args == ["AGENTBOX_SESSION"]]
        self.assertEqual(len(envset), 1, [c.label for c in b.cases if c.op == "envset"])

    def test_the_agent_is_told_about_it(self):
        s = self.start()
        notes = session.sandbox_notes(s.project, s.egress, adapter=s.adapter)
        self.assertIn("AGENTBOX_SESSION", notes)


class OmpGetsItToo(OmpSessionBase):
    def test_it_is_the_session_id(self):
        self.project("p1", preset="python")
        s = session.make_session(self.settings_now(), "omp",
                                 session.load_project(self.settings_now(), "p1"), warn=lambda m: None)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        self.assertEqual(env_of(s.run_args(["true"])).get("AGENTBOX_SESSION"), s.name)


if __name__ == "__main__":
    unittest.main()
