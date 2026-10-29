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
import time
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

    def test_ab_run_refuses_a_second_session_rather_than_warning(self):
        """`ab run` used to warn and start anyway. One live session per project is enforced now
        (projectlock.py, 29 Oct): the same liveness signals, but a refusal -- there is nothing left to
        warn about. The lock's own cases are in test_projectlock.py; this pins that `run` goes through it."""
        from agentbox import projectlock
        os.environ["FAKE_PODMAN_PSJSON"] = ps_json()
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        projectlock.acquire(self.settings, "p1", pid=os.getpid(),
                            pid_start=session._start_time(os.getpid()))
        projectlock.note_session(self.settings, "p1", "ab-claude-p1-1")
        import io
        from contextlib import redirect_stderr, redirect_stdout
        err = io.StringIO()
        # stdin too: `ab run` reads it without a TTY, so a run that gets further than expected would
        # block on the real stdin instead of failing.
        with redirect_stdout(io.StringIO()), redirect_stderr(err), \
             unittest.mock.patch("sys.stdin", io.StringIO("")):
            code = cli.main(["run", "claude", "p1", "--no-tty"])
        self.assertEqual(code, 2)
        self.assertIn("ab-claude-p1-1", err.getvalue())
        self.assertIn("ab attach p1", err.getvalue())
        self.assertFalse(hasattr(cli, "_warn_if_live"), "the warning is gone, not just unused")


if __name__ == "__main__":
    unittest.main()


def infra_ps(session_name="ab-claude-p1-1", project="p1"):
    """What a CRASHED session leaves behind: its proxy and sockguard still running, no agent. Both carry
    the project label, which is what makes `project_is_live` -- rightly -- call the project live."""
    return json.dumps([
        {"Names": [f"{session_name}-proxy"], "State": "running",
         "Labels": {"agentbox.project": project, "agentbox.session": session_name,
                    "agentbox.role": "proxy", "agentbox-test": "1"}},
        {"Names": [f"{session_name}-sockguard"], "State": "running",
         "Labels": {"agentbox.project": project, "agentbox.session": session_name,
                    "agentbox.role": "sockguard", "agentbox-test": "1"}}])


class GcAsksADifferentQuestion(SettingsCase):
    """"Is this project live?" and "is this session still doing work?" are opposite kinds of question, and
    `ab session gc` must ask the second one.

    `project_is_live` guards a registry EDIT, so it is pessimistic: any running container labelled with the
    project counts, and an unreadable signal counts too. Reaping needs the opposite: a crashed session
    leaves its proxy, sockguard and networks RUNNING, and those are exactly what gc exists to remove.
    Counting them as "something is live" would make a husk immortal -- and since project_is_live does count
    them, the husk's project would stay unmanageable for good.

    The regression this pins (itest test_f_gc_stops_what_a_crashed_session_left, 29 Oct) was neither of
    those: `_young` returned True for a run dir that does NOT EXIST, so a husk looked like a session being
    started this second and gc skipped it. A missing run dir is the opposite of a session mid-start --
    prepare() creates the dir and writes session.json before anything else exists.
    """

    def setUp(self):
        super().setUp()
        self.root = self.project("p1", preset="script")
        self.name = "ab-claude-p1-1"

    def ps(self, value):
        os.environ["FAKE_PODMAN_PSJSON"] = value
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)

    def podman(self):
        return Podman(self.settings.podman)

    def husk(self):
        """A crashed session: launcher dead, run dir gone, agent removed, infrastructure still up.

        Both podman knobs: liveness is read from `ps --format json`, while teardown removes what
        `ps --format {{.Names}}` lists -- so an assertion about removals is vacuous with only the first."""
        self.ps(infra_ps(self.name))
        os.environ["FAKE_PODMAN_PS"] = f"{self.name}-proxy\n{self.name}-sockguard\n"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PS", None)
        os.environ["FAKE_PODMAN_NETLS"] = f"{self.name}-int\n{self.name}-egr\n"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_NETLS", None)
        self.assertFalse(os.path.isdir(self.settings.session_run_dir(self.name)))

    def removals(self):
        """Only real removals: `ids()` itself runs `volume ls` and `network ls`, so matching on the first
        word alone counted a listing as a removal (it did, until 29 Oct)."""
        kinds = (["rm"], ["pod", "rm"], ["volume", "rm"], ["network", "rm"])
        return [c for c in self.podman_calls() if any(c[:len(k)] == k for k in kinds)]

    # --- the reaping question ---

    def test_a_husk_is_reaped(self):
        self.husk()
        self.assertEqual(session.gc(self.settings, self.podman()), [self.name])
        removed = [" ".join(c) for c in self.removals()]
        self.assertTrue(any(f"{self.name}-proxy" in r for r in removed), removed)
        self.assertTrue(any(f"{self.name}-int" in r for r in removed), removed)

    def test_infrastructure_alone_is_not_alive(self):
        self.husk()
        info = session.sessions(self.settings, self.podman())[self.name]
        alive, why = session.session_is_alive(self.settings, self.name, info)
        self.assertFalse(alive, why)

    def test_a_running_agent_is_left_alone_and_says_how_to_end_it(self):
        self.ps(ps_json(sess=self.name, role="agent"))
        warned = []
        self.assertEqual(session.gc(self.settings, self.podman(), warn=warned.append), [])
        self.assertTrue(any(f"ab session kill {self.name}" in w for w in warned), warned)

    def test_a_session_being_started_is_left_alone(self):
        """Its run dir exists and was touched seconds ago, before any container of it exists."""
        self.ps("[]")
        os.makedirs(self.settings.session_run_dir(self.name), exist_ok=True)
        self.assertEqual(session.gc(self.settings, self.podman()), [])

    def test_an_old_run_dir_with_no_metadata_is_reaped(self):
        self.ps("[]")
        d = self.settings.session_run_dir(self.name)
        os.makedirs(d, exist_ok=True)
        old = time.time() - 600
        os.utime(d, (old, old))
        self.assertEqual(session.gc(self.settings, self.podman()), [self.name])

    def test_an_unreadable_podman_reaps_nothing(self):
        self.husk()
        os.environ["FAKE_PODMAN_FAIL"] = "ps"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        with self.assertRaises(AgentboxError):
            session.gc(self.settings, self.podman())
        self.assertEqual(self.removals(), [], "nothing is removed on a guess")

    # --- _young: absence is an answer, unreadable is not ---

    def test_young_reads_a_missing_run_dir_as_gone_not_starting(self):
        self.assertFalse(session._young(self.settings, self.name))

    def test_young_still_fails_closed_when_it_cannot_tell(self):
        from unittest import mock
        real = os.stat

        def fake(path, *a, **kw):
            if "sessions" in str(path):
                raise OSError(13, "nope")           # EACCES: cannot tell, so do not reap
            return real(path, *a, **kw)

        with mock.patch("agentbox.session.os.stat", side_effect=fake):
            self.assertTrue(session._young(self.settings, self.name))

    # --- the asymmetry, end to end ---

    def test_the_husk_blocks_registry_edits_until_gc_runs(self):
        """Both halves of why this matters: a husk's containers DO make the project live (that is
        project_is_live doing its job), and gc is the way out -- after which the edit works."""
        self.husk()
        self.assertEqual(cli.main(["project", "set", "p1", "--egress", "locked"]), 2)
        self.assertNotIn("egress", registry.load(self.settings.registry_path)["p1"])
        self.assertEqual(session.gc(self.settings, self.podman()), [self.name])
        self.ps("[]")                                # the husk is gone now
        self.assertEqual(cli.main(["project", "set", "p1", "--egress", "locked"]), 0)
        self.assertEqual(registry.load(self.settings.registry_path)["p1"]["egress"], "locked")

    def test_session_kill_removes_a_husk_by_hand(self):
        """The manual way out must always exist, whatever gc decides."""
        self.husk()
        self.assertEqual(cli.main(["session", "kill", self.name]), 0)
        self.assertTrue(self.removals())

    def test_session_kill_finds_a_husk_that_is_only_networks(self):
        """Everything of it is gone except its networks -- which were invisible until the network listing
        was fixed (28 Oct), so `kill` used to answer "no session named ...".""" 
        self.ps("[]")
        os.environ["FAKE_PODMAN_NETJSON"] = json.dumps(
            [{"name": f"{self.name}-int", "labels": {"agentbox.session": self.name,
                                                     "agentbox.project": "p1", "agentbox-test": "1"}}])
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_NETJSON", None)
        self.assertEqual(cli.main(["session", "kill", self.name]), 0)
