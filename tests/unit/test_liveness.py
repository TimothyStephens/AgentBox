"""One liveness predicate, fail-closed by construction (third audit, F-1/F-2/F-10).

"Is this project live?" has been answered wrong in three audits running, each time the same way: one signal
was taken for the whole truth, and a signal that could not be read was read as "nothing is running".

  * F3: `rm` succeeded during a live session, taking the grant's overlap protection with it;
  * F2: the launcher PID was the only signal, so a dead launcher hid running containers;
  * F-1: the fix for F2 REPLACED the launcher signal with container labels, so a session still in prepare()
    -- session.json written, containers not yet created -- became invisible again;
  * F-2/F-10: `podman ps` failing, a missing podman binary, or unparseable output all returned "no
    containers", which every caller read as "nothing is running".

So: every signal is consulted, ANY of them says live, and an unreadable signal ALSO says live.
"""
import json
import os
import unittest

from agentbox import cli, registry, session
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from .helpers import SettingsCase


def ps_json(project="p1", sess="ab-claude-p1-1", state="running", role="agent"):
    return json.dumps([{"Names": [f"{sess}-{role}"], "State": state,
                        "Labels": {"agentbox.project": project, "agentbox.session": sess,
                                   "agentbox.role": role, "agentbox-test": "1"}}])


class Liveness(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("p1", preset="script")

    def live(self, name="p1"):
        return session.project_is_live(self.settings, Podman(self.settings.podman), name)

    def ps(self, value):
        os.environ["FAKE_PODMAN_PSJSON"] = value
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)

    def fail_podman(self, substring="ps"):
        os.environ["FAKE_PODMAN_FAIL"] = substring
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)

    def meta(self, sess, project, alive=True):
        d = self.settings.session_run_dir(sess)
        os.makedirs(d, exist_ok=True)
        pid = os.getpid() if alive else 2 ** 21
        meta = {"project": project, "pid": pid,
                "pid_start": session._start_time(pid) if alive else 1}
        with open(os.path.join(d, "session.json"), "w") as f:
            json.dump(meta, f)

    # --- the container signal ---

    def test_a_running_container_is_live(self):
        self.ps(ps_json())
        live, why = self.live()
        self.assertTrue(live)
        self.assertIn("ab-claude-p1-1", why)

    def test_an_exited_container_is_not_live(self):
        self.ps(ps_json(state="exited"))
        self.assertEqual(self.live(), (False, ""))

    def test_another_projects_container_is_not_live_for_this_one(self):
        self.ps(ps_json(project="other"))
        self.assertEqual(self.live(), (False, ""))

    # --- the launcher signal: F-1, the one the F2 fix dropped ---

    def test_a_session_in_prepare_is_live_with_no_containers_yet(self):
        """session.json is written before any container exists, so a session mid-startup must still count."""
        self.ps("[]")
        self.meta("ab-claude-p1-1", "p1", alive=True)
        live, why = self.live()
        self.assertTrue(live, "a live launcher with no containers yet is still a live session")
        self.assertIn("ab-claude-p1-1", why)

    def test_a_dead_launcher_with_no_containers_is_not_live(self):
        self.ps("[]")
        self.meta("ab-claude-p1-1", "p1", alive=False)
        self.assertEqual(self.live(), (False, ""))

    def test_a_live_launcher_of_another_project_is_not_live_for_this_one(self):
        self.ps("[]")
        self.meta("ab-claude-other-1", "other", alive=True)
        self.assertEqual(self.live(), (False, ""))

    # --- unknown state counts as live: F-2, F-10 ---

    def test_a_failing_ps_counts_as_live(self):
        self.fail_podman("ps")
        live, why = self.live()
        self.assertTrue(live, "podman ps failed: the answer is unknown, so it must not be 'nothing running'")
        self.assertIn("cannot tell", why)

    def test_unparseable_ps_output_counts_as_live(self):
        self.ps("{not json at all")
        live, why = self.live()
        self.assertTrue(live)
        self.assertIn("cannot tell", why)

    def test_a_missing_podman_binary_counts_as_live(self):
        live, why = session.project_is_live(self.settings, Podman("/nonexistent/podman"), "p1")
        self.assertTrue(live)
        self.assertIn("cannot tell", why)

    def test_a_session_dir_with_no_readable_meta_counts_as_live(self):
        """It cannot be attributed to a project, so it counts for every project -- with the session named and
        `ab session gc` suggested, rather than guessed at by parsing the session name."""
        self.ps("[]")
        os.makedirs(self.settings.session_run_dir("ab-claude-mystery-1"), exist_ok=True)
        live, why = self.live()
        self.assertTrue(live)
        self.assertIn("ab-claude-mystery-1", why)
        self.assertIn("ab session gc", why)

    def test_an_unparseable_session_json_counts_as_live(self):
        self.ps("[]")
        d = self.settings.session_run_dir("ab-claude-p1-1")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "session.json"), "w") as f:
            f.write("{truncated")
        self.assertTrue(self.live()[0])

    def test_nothing_anywhere_is_not_live(self):
        self.ps("[]")
        self.assertEqual(self.live(), (False, ""))


class PodmanReadsAreStrict(SettingsCase):
    """F-2/F-10: the readers behind every safety decision returned [] on failure."""

    def test_containers_json_raises_on_a_failing_ps(self):
        os.environ["FAKE_PODMAN_FAIL"] = "ps"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        with self.assertRaises(AgentboxError):
            Podman(self.settings.podman).containers_json(self.settings.labels)

    def test_containers_json_raises_on_unparseable_output(self):
        os.environ["FAKE_PODMAN_PSJSON"] = "{{{"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        with self.assertRaises(AgentboxError) as cm:
            Podman(self.settings.podman).containers_json(self.settings.labels)
        self.assertIn("could not be read", str(cm.exception))

    def test_ids_raises_on_failure(self):
        os.environ["FAKE_PODMAN_FAIL"] = "volume"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        with self.assertRaises(AgentboxError):
            Podman(self.settings.podman).ids("volume", self.settings.labels)

    def test_a_missing_binary_is_an_ab_error_not_a_traceback(self):
        with self.assertRaises(AgentboxError) as cm:
            Podman("/nonexistent/podman").run(["ps"])
        self.assertIn("podman", str(cm.exception))


class RegistryEditsRefuseOnUnknownState(SettingsCase):
    """End to end: the commands that refuse on a live session refuse when the state cannot be read."""

    def setUp(self):
        super().setUp()
        self.project("p1", preset="script")

    def names(self):
        return set(registry.load(self.settings.registry_path))

    def test_rm_is_refused_when_ps_fails(self):
        os.environ["FAKE_PODMAN_FAIL"] = "ps"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        self.assertEqual(cli.main(["project", "rm", "p1"]), 2)
        self.assertIn("p1", self.names(), "refused: the entry stays")

    def test_set_is_refused_during_prepare(self):
        os.environ["FAKE_PODMAN_PSJSON"] = "[]"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        d = self.settings.session_run_dir("ab-claude-p1-1")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "session.json"), "w") as f:
            json.dump({"project": "p1", "pid": os.getpid(),
                       "pid_start": session._start_time(os.getpid())}, f)
        self.assertEqual(cli.main(["project", "set", "p1", "--egress", "locked"]), 2)
        self.assertNotIn("egress", registry.load(self.settings.registry_path)["p1"])

    def test_a_missing_podman_binary_is_an_ab_error_not_a_traceback(self):
        os.environ["AB_PODMAN"] = "/nonexistent/podman"
        self.addCleanup(os.environ.pop, "AB_PODMAN", None)
        self.assertEqual(cli.main(["project", "rm", "p1"]), 2)
        self.assertIn("p1", self.names())

    def test_the_warning_on_ab_run_uses_the_same_predicate(self):
        os.environ["FAKE_PODMAN_PSJSON"] = ps_json()
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        warned = []
        cli._warn_if_live(self.settings, "p1", warn=warned.append)
        self.assertTrue(any("ab-claude-p1-1" in w for w in warned), warned)
        os.environ["FAKE_PODMAN_PSJSON"] = "[]"
        warned.clear()
        cli._warn_if_live(self.settings, "p1", warn=warned.append)
        self.assertEqual(warned, [])


if __name__ == "__main__":
    unittest.main()
