"""Decision #2 (30 Sep): a project whose git history holds masked file names is refused until accepted.

Git history cannot be masked, so a `.env` ever committed is readable by the agent. `ab project add` refuses
such a project unless --accept-history-secrets is given; the accepted names are stored on the project, and a
session refuses any name in history that was not accepted.
"""
import io
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agentbox import cli, registry, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase


def commit_then_untrack(repo, rel, text="REAL=1\n"):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    subprocess.run(["git", "-C", repo, "add", "-f", rel], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "oops"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", repo, "rm", "-q", "--cached", rel], check=True)


def ab(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class HistorySecrets(SettingsCase):
    def setUp(self):
        super().setUp()
        self.token()

    def make(self, name="p1"):
        return session.make_session(self.settings, "claude", session.load_project(self.settings, name),
                                    warn=lambda m: None)

    def test_clean_history_is_fine(self):
        root = self.project("p1")
        commit_then_untrack(root, ".env.example")         # exempt from masking
        self.make().prepare()

    def test_session_refused_before_anything_is_created(self):
        root = self.project("p1")
        commit_then_untrack(root, ".env")
        with self.assertRaises(AgentboxError) as cm:
            self.make().prepare()
        msg = str(cm.exception)
        self.assertIn(".env", msg)
        self.assertIn("rotate", msg)
        self.assertIn("accept-history-secrets", msg)
        self.assertFalse(os.listdir(os.path.join(self.settings.run_dir, "sessions"))
                         if os.path.isdir(os.path.join(self.settings.run_dir, "sessions")) else [])
        self.assertEqual(self.podman_calls(), [])

    def test_accepted_names_pass_new_ones_do_not(self):
        root = self.project("p1", history_secrets_accepted=[".env"])
        commit_then_untrack(root, ".env")
        self.make().prepare()
        commit_then_untrack(root, "tls/server.key")
        with self.assertRaises(AgentboxError) as cm:
            self.make().prepare()
        self.assertIn("tls/server.key", str(cm.exception))
        self.assertNotIn(".env,", str(cm.exception))

    def test_nested_repo_and_secrets_dir(self):
        root = self.project("p1")
        sub = os.path.join(root, "vendor", "lib")
        subprocess.run(["git", "init", "-q", sub], check=True)
        commit_then_untrack(sub, "secrets/db.txt")
        found = session.history_secrets(root, [root], {".env": ".env.dev"})
        self.assertEqual(found, ["vendor/lib/secrets/db.txt"])

    def test_unreadable_history_refuses(self):
        root = self.project("p1")
        commit_then_untrack(root, "README")
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 120)):
            with self.assertRaises(AgentboxError) as cm:
                session.history_secrets(root, [root], {})
        self.assertIn("refusing rather than guessing", str(cm.exception))

    def test_project_add_refuses_then_accepts(self):
        root = self.mkdir("projects", "p2")
        subprocess.run(["git", "init", "-q", root], check=True)
        commit_then_untrack(root, ".env.local")
        rc, _out, err = ab("project", "add", "p2", root, "--preset", "python")
        self.assertEqual(rc, 2)
        self.assertIn(".env.local", err)
        self.assertIn("rotate", err)
        self.assertNotIn("p2", registry.load(self.settings.registry_path))
        rc, out, err = ab("project", "add", "p2", root, "--preset", "python", "--accept-history-secrets")
        self.assertEqual(rc, 0, err)
        self.assertEqual(registry.load(self.settings.registry_path)["p2"]["history_secrets_accepted"],
                         [".env.local"])
        self.assertIn("rotate", err)
        self.make("p2").prepare()

    def test_accept_command_for_registered_project(self):
        root = self.project("p1")
        commit_then_untrack(root, "id.pem")
        rc, out, _err = ab("project", "accept-history-secrets", "p1")
        self.assertEqual(rc, 0)
        self.assertIn("id.pem", out)
        self.make().prepare()
        rc, out, _err = ab("project", "show", "p1")
        self.assertIn("id.pem", out)
