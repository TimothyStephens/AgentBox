"""Registry edits are refused while a session for that project is live (F3).

`ab project rm` used to succeed and silently orphan the running session: the registry entry went away, so
`ab project show` denied the project existed, the containers kept running, and -- the real hazard -- the
grant's overlap protection went with it, so another project could be registered over the live grant. There
is deliberately no --force: tearing down live work is `ab session kill`'s job (docs/decisions.md, 6 Oct,
"Auto-reaping was rejected").
"""
import os
from unittest import mock

from agentbox import cli, registry
from agentbox.errors import AgentboxError

from .helpers import SettingsCase


def alive(project, name="ab-claude-p1-1"):
    return {name: {"alive": True, "meta": {"project": project}}}


DEAD = {"ab-claude-p1-1": {"alive": False, "meta": {"project": "p1"}}}


class LiveSessionGuard(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("p1", preset="python")

    def names(self):
        return set(registry.load(self.settings.registry_path))

    def test_rm_is_refused_while_a_session_is_live(self):
        with mock.patch("agentbox.session.sessions", return_value=alive("p1")):
            self.assertEqual(cli.main(["project", "rm", "p1"]), 2)
        self.assertIn("p1", self.names(), "refused: the entry stays")

    def test_the_refusal_names_the_session_and_how_to_end_it(self):
        with mock.patch("agentbox.session.sessions", return_value=alive("p1")):
            with self.assertRaises(AgentboxError) as cm:
                cli.cmd_project_rm(self.settings, type("A", (), {"name": ["p1"]})())
        msg = str(cm.exception)
        self.assertIn("ab-claude-p1-1", msg)
        self.assertIn("ab session kill", msg)

    def test_rm_is_all_or_nothing_across_names(self):
        self.project("p2", preset="python")
        with mock.patch("agentbox.session.sessions", return_value=alive("p1")):
            self.assertEqual(cli.main(["project", "rm", "p2", "p1"]), 2)
        self.assertEqual({"p1", "p2"} & self.names(), {"p1", "p2"}, "a blocked name removes none")

    def test_rm_works_once_no_session_is_live(self):
        with mock.patch("agentbox.session.sessions", return_value=DEAD):
            self.assertEqual(cli.main(["project", "rm", "p1"]), 0)
        self.assertNotIn("p1", self.names())

    def test_a_project_cannot_be_registered_over_a_live_grant(self):
        """The point of refusing rm: because the entry stays, the overlap check still covers the grant the
        live session holds, so nothing can be registered on top of it."""
        with mock.patch("agentbox.session.sessions", return_value=alive("p1")):
            self.assertEqual(cli.main(["project", "rm", "p1"]), 2)
            parent = os.path.dirname(self.root)
            rc = cli.main(["project", "add", "sneaky", parent, "--preset", "python"])
        self.assertEqual(rc, 2, "a root containing the live grant must not register")
        self.assertNotIn("sneaky", self.names())

    def test_accept_grant_exposure_is_refused_while_a_session_is_live(self):
        with open(os.path.join(self.root, "data.db"), "wb") as f:
            f.write(b"x" * (2 << 20))
        with mock.patch("agentbox.session.sessions", return_value=alive("p1")):
            self.assertEqual(cli.main(["project", "accept-grant-exposure", "p1"]), 2)
        self.assertNotIn("grant_exposure_accepted", registry.load(self.settings.registry_path)["p1"])

    def test_accept_history_secrets_is_refused_while_a_session_is_live(self):
        with mock.patch("agentbox.session.sessions", return_value=alive("p1")):
            self.assertEqual(cli.main(["project", "accept-history-secrets", "p1"]), 2)
        self.assertNotIn("history_secrets_accepted", registry.load(self.settings.registry_path)["p1"])

    def test_a_session_of_another_project_does_not_block(self):
        with mock.patch("agentbox.session.sessions", return_value=alive("other")):
            self.assertEqual(cli.main(["project", "rm", "p1"]), 0)
        self.assertNotIn("p1", self.names())
