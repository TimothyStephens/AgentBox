"""One live session per project (`agentbox/projectlock.py`, `session.take_project_lock`).

Two sessions of one project shared its agent state with no locking -- Claude's `.claude.json` and settings,
OMP's `agent/`, the inbox, all last-writer-wins. `ab run` used to warn about it. The lock makes it
impossible instead, and the warning is gone.

The hard part is not taking the lock, it is deciding that a holder is gone. SIGKILL leaves the file behind,
so a stale lock has to be reapable -- but "nothing is running" has been answered wrongly three times in
this project's audits (F3, F2, F-1), always by treating a signal it could not read as "nothing there". So:
the pid is checked with its start time, a live launcher is never overruled, and an unreadable podman stops
the whole operation rather than freeing a lock on a guess.
"""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from agentbox import cli, projectlock, session
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from .helpers import SettingsCase


def ps_json(session_id, project="p1", state="running", role="agent"):
    """One container of a session, as `podman ps --format json` reports it.

    The AGENT container is named after the session itself; only the sidecars take a suffix
    (`<session>-proxy`). Checked against real podman, 29 Oct: an itest that looked for a `-agent` name
    found nothing."""
    name = session_id if role == "agent" else f"{session_id}-{role}"
    return json.dumps([{"Names": [name], "State": state,
                        "Labels": {"agentbox.project": project, "agentbox.session": session_id,
                                   "agentbox.role": role, "agentbox-test": "1"}}])


DEAD_PID = 2 ** 21          # above the kernel's default pid_max: never a live process


class LockFile(SettingsCase):
    def setUp(self):
        super().setUp()
        self.project("p1", preset="script")

    def test_taking_it_twice_gives_the_holder_the_second_time(self):
        ok, mine = projectlock.acquire(self.settings, "p1", pid=111, pid_start=7)
        self.assertTrue(ok)
        self.assertEqual((mine["pid"], mine["pid_start"]), (111, 7))
        ok, holder = projectlock.acquire(self.settings, "p1", pid=222, pid_start=8)
        self.assertFalse(ok)
        self.assertEqual(holder["pid"], 111, "the first holder is reported, not overwritten")

    def test_it_lives_in_the_projects_state_dir(self):
        projectlock.acquire(self.settings, "p1")
        self.assertEqual(projectlock.path(self.settings, "p1"),
                         os.path.join(self.settings.data_dir, "p1", "session.lock"))

    def test_release_only_removes_our_own(self):
        projectlock.acquire(self.settings, "p1", pid=111, pid_start=7)
        self.assertFalse(projectlock.release(self.settings, "p1", pid=999), "not ours")
        self.assertIsNotNone(projectlock.read(self.settings, "p1"))
        self.assertTrue(projectlock.release(self.settings, "p1", pid=111))
        self.assertIsNone(projectlock.read(self.settings, "p1"))

    def test_release_by_session_id(self):
        """Teardown knows the session, not always the project: a husk's metadata may be gone."""
        projectlock.acquire(self.settings, "p1", pid=111, pid_start=7)
        projectlock.note_session(self.settings, "p1", "ab-claude-p1-1")
        self.assertEqual(projectlock.release_for_session(self.settings, "nope"), [])
        self.assertEqual(projectlock.release_for_session(self.settings, "ab-claude-p1-1"), ["p1"])
        self.assertIsNone(projectlock.read(self.settings, "p1"))

    def test_an_unreadable_lock_counts_as_held(self):
        """Something wrote it. Guessing it away is how two sessions would end up sharing state."""
        projectlock.acquire(self.settings, "p1")
        with open(projectlock.path(self.settings, "p1"), "w") as f:
            f.write("{truncated")
        self.assertEqual(projectlock.read(self.settings, "p1"), {"unreadable": True})
        ok, holder = projectlock.acquire(self.settings, "p1")
        self.assertFalse(ok)
        self.assertTrue(holder["unreadable"])

    def test_the_lock_dirs_scan_skips_the_shared_directories(self):
        for d in ("logs", "backups", "profiles", "ephemeral"):
            os.makedirs(os.path.join(self.settings.data_dir, d), exist_ok=True)
            with open(os.path.join(self.settings.data_dir, d, "session.lock"), "w") as f:
                f.write("{}")
        projectlock.acquire(self.settings, "p1")
        self.assertEqual(projectlock.projects_with_locks(self.settings), ["p1"])


class HolderIsGone(SettingsCase):
    """The judgement that lets a lock be reaped."""

    def setUp(self):
        super().setUp()
        self.project("p1", preset="script")
        self.podman = Podman(self.settings.podman)

    def ps(self, value):
        os.environ["FAKE_PODMAN_PSJSON"] = value
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)

    def state(self, holder):
        return session.lock_holder_state(self.settings, self.podman, holder)

    def test_a_live_launcher_is_never_gone(self):
        me = {"pid": os.getpid(), "pid_start": session._start_time(os.getpid()), "session": ""}
        gone, why = self.state(me)
        self.assertFalse(gone)
        self.assertIn("launcher", why)

    def test_a_live_launcher_with_no_containers_is_still_not_gone(self):
        """A session in prepare() looks exactly like this. Reaping it is the F-1 bug."""
        self.ps("[]")
        me = {"pid": os.getpid(), "pid_start": session._start_time(os.getpid()),
              "session": "ab-claude-p1-1"}
        self.assertFalse(self.state(me)[0])

    def test_a_recycled_pid_does_not_impersonate_the_holder(self):
        """Our pid, but the start time of some older process: not the launcher that took the lock."""
        self.ps("[]")
        gone, _why = self.state({"pid": os.getpid(), "pid_start": 1, "session": "ab-claude-p1-1"})
        self.assertTrue(gone)

    def test_a_dead_launcher_with_a_running_agent_is_not_gone(self):
        self.ps(ps_json("ab-claude-p1-1"))
        gone, why = self.state({"pid": DEAD_PID, "pid_start": 1, "session": "ab-claude-p1-1"})
        self.assertFalse(gone, why)

    def test_a_dead_launcher_with_only_infrastructure_is_gone(self):
        """The husk `ab session gc` exists for: proxy and sockguard up, no agent, no launcher."""
        self.ps(json.dumps([
            {"Names": ["ab-claude-p1-1-proxy"], "State": "running",
             "Labels": {"agentbox.project": "p1", "agentbox.session": "ab-claude-p1-1",
                        "agentbox.role": "proxy", "agentbox-test": "1"}}]))
        gone, why = self.state({"pid": DEAD_PID, "pid_start": 1, "session": "ab-claude-p1-1"})
        self.assertTrue(gone, why)

    def test_a_dead_launcher_that_never_named_its_session_is_gone(self):
        self.ps("[]")
        gone, why = self.state({"pid": DEAD_PID, "pid_start": 1, "session": ""})
        self.assertTrue(gone)
        self.assertIn("never named", why)

    def test_an_unreadable_podman_refuses_to_decide(self):
        """A lock is never stolen on a guess."""
        os.environ["FAKE_PODMAN_FAIL"] = "ps"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        with self.assertRaises(AgentboxError):
            self.state({"pid": DEAD_PID, "pid_start": 1, "session": "ab-claude-p1-1"})


class RunHoldsTheProject(SettingsCase):
    def setUp(self):
        super().setUp()
        self.token()
        self.root = self.project("p1", preset="script")
        os.environ["FAKE_PODMAN_PSJSON"] = "[]"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)

    def lock(self):
        return projectlock.read(self.settings, "p1")

    def husk(self, session_id="ab-claude-p1-9"):
        """A crashed session: infrastructure up, no agent, no launcher. Both podman knobs, because
        liveness is read from `ps --format json` while teardown removes what `ps --format {{.Names}}`
        lists -- an assertion about removals is vacuous with only the first set."""
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps([
            {"Names": [f"{session_id}-proxy"], "State": "running",
             "Labels": {"agentbox.project": "p1", "agentbox.session": session_id,
                        "agentbox.role": "proxy", "agentbox-test": "1"}}])
        os.environ["FAKE_PODMAN_PS"] = f"{session_id}-proxy\n{session_id}-sockguard\n"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PS", None)
        os.environ["FAKE_PODMAN_NETLS"] = f"{session_id}-int\n{session_id}-egr\n"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_NETLS", None)

    def removals(self):
        """Only real removals: `ids()` itself runs `volume ls` and `network ls`, so matching on the first
        word alone counts listings as removals."""
        kinds = (["rm"], ["pod", "rm"], ["volume", "rm"], ["network", "rm"])
        return [c for c in self.podman_calls() if any(c[:len(k)] == k for k in kinds)]

    # --- the refusal ---

    def test_a_second_run_is_refused_and_told_to_attach(self):
        projectlock.acquire(self.settings, "p1", pid=os.getpid(),
                            pid_start=session._start_time(os.getpid()))
        projectlock.note_session(self.settings, "p1", "ab-claude-p1-1")
        code, out = self.ab("run", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2)
        self.assertIn("p1 is running (session ab-claude-p1-1)", out)
        self.assertIn("ab attach p1", out)
        self.assertIn("ab session kill p1", out)
        self.assertNotIn("may already be running", out, "the old warning is gone")

    def test_the_refusal_says_why_one_at_a_time(self):
        projectlock.acquire(self.settings, "p1", pid=os.getpid(),
                            pid_start=session._start_time(os.getpid()))
        _code, out = self.ab("run", "claude", "p1", "--no-tty")
        self.assertIn("agent state and inbox", out)
        self.assertIn("last writer wins", out)

    def test_a_refused_run_leaves_the_lock_alone(self):
        """A live holder's lock is not rewritten, not stolen, not released."""
        projectlock.acquire(self.settings, "p1", pid=os.getpid(),
                            pid_start=session._start_time(os.getpid()))
        projectlock.note_session(self.settings, "p1", "ab-claude-p1-1")
        with open(projectlock.path(self.settings, "p1")) as f:
            before = f.read()
        os.environ["FAKE_PODMAN_PSJSON"] = ps_json("ab-claude-p1-1")
        self.assertEqual(self.ab("run", "claude", "p1", "--no-tty")[0], 2)
        with open(projectlock.path(self.settings, "p1")) as f:
            self.assertEqual(f.read(), before, "someone else's lock is untouched")

    # --- the stale lock ---

    def test_a_stale_lock_is_reaped_and_the_run_proceeds(self):
        """The lock's own reaping, with `gc` out of the way: gc runs on the FIRST use after boot and would
        have cleared this husk itself (the next test covers that), but a stale lock has to be reapable on
        every later run too."""
        self.addCleanup(setattr, cli, "gc_once_per_boot", cli.gc_once_per_boot)
        cli.gc_once_per_boot = lambda *a, **kw: False
        projectlock.acquire(self.settings, "p1", pid=DEAD_PID, pid_start=1)
        projectlock.note_session(self.settings, "p1", "ab-claude-p1-9")
        self.husk()
        code, out = self.ab("run", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        self.assertIn("reaped session ab-claude-p1-9", out)
        self.assertIn("container(s)", out, "it reports what it removed")
        removed = [" ".join(c) for c in self.removals()]
        self.assertTrue(any("ab-claude-p1-9-proxy" in r for r in removed), removed)
        self.assertTrue(any("ab-claude-p1-9-int" in r for r in removed), removed)
        self.assertIsNone(self.lock(), "and the lock is free again afterwards")

    def test_the_first_run_after_boot_gets_there_through_gc_instead(self):
        """Same outcome by the other route: `gc` reaps the husk and its teardown frees the lock."""
        projectlock.acquire(self.settings, "p1", pid=DEAD_PID, pid_start=1)
        projectlock.note_session(self.settings, "p1", "ab-claude-p1-9")
        self.husk()
        code, out = self.ab("run", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        self.assertIn("ab-claude-p1-9", out, "it says what it removed")
        self.assertTrue(any("ab-claude-p1-9-proxy" in " ".join(c) for c in self.removals()))
        self.assertIsNone(self.lock())

    def test_a_stale_lock_with_no_session_id_is_just_cleared(self):
        projectlock.acquire(self.settings, "p1", pid=DEAD_PID, pid_start=1)
        code, out = self.ab("run", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        self.assertIn("stale session lock", out)

    def test_an_unreadable_podman_does_not_free_the_lock(self):
        projectlock.acquire(self.settings, "p1", pid=DEAD_PID, pid_start=1)
        projectlock.note_session(self.settings, "p1", "ab-claude-p1-9")
        os.environ["FAKE_PODMAN_FAIL"] = "ps"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        code, _out = self.ab("run", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2)
        self.assertEqual(self.lock()["session"], "ab-claude-p1-9", "still held")

    # --- release on every exit ---

    def test_the_lock_is_released_on_a_normal_run(self):
        code, out = self.ab("run", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        self.assertIsNone(self.lock())
        self.assertEqual(session.sessions(self.settings, Podman(self.settings.podman)), {},
                         "and nothing is left over")

    def test_the_lock_is_held_while_the_session_runs(self):
        seen = {}
        real = session.Session.run_captured

        def spy(self_, *a, **kw):
            seen["lock"] = projectlock.read(self_.settings, "p1")
            return real(self_, *a, **kw)

        with mock.patch.object(session.Session, "run_captured", spy):
            self.ab("run", "claude", "p1", "--no-tty")
        self.assertTrue(seen["lock"], "held during the run")
        self.assertTrue(seen["lock"]["session"].startswith("abt-unit-claude-p1-"),
                        "and it records which session holds it")

    def test_the_lock_is_released_when_prepare_raises(self):
        with mock.patch.object(session.Session, "_prepare_reserved",
                               side_effect=AgentboxError("boom")):
            code, out = self.ab("run", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2)
        self.assertIn("boom", out)
        self.assertIsNone(self.lock(), "a failed start does not block the project")

    def test_the_lock_is_released_when_load_project_raises_before_any_session_exists(self):
        """Nothing has been named yet, so teardown cannot release it -- `ab run`'s own handle does."""
        with mock.patch.object(session, "load_project", side_effect=AgentboxError("bad entry")):
            code, _out = self.ab("run", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2)
        self.assertIsNone(self.lock())

    def test_kill_releases_the_lock_too(self):
        projectlock.acquire(self.settings, "p1", pid=DEAD_PID, pid_start=1)
        projectlock.note_session(self.settings, "p1", "ab-claude-p1-1")
        os.environ["FAKE_PODMAN_PSJSON"] = ps_json("ab-claude-p1-1")
        code, out = self.ab("session", "kill", "p1")
        self.assertEqual(code, 0, out)
        self.assertIn("killed ab-claude-p1-1", out)
        self.assertIsNone(self.lock())

    # --- the unregistered path ---

    def test_an_unregistered_project_is_not_registered_implicitly(self):
        code, out = self.ab("run", "claude", "nope", "--no-tty")
        self.assertEqual(code, 2)
        self.assertIn("ab project add nope", out)
        self.assertIn("--preset", out)
        self.assertIn("does not register one for you", out)
        self.assertNotIn("nope", os.listdir(self.settings.data_dir) if
                         os.path.isdir(self.settings.data_dir) else [])

    def test_the_suggested_root_is_the_repos_dir_when_it_exists(self):
        repos = self.mkdir("repos")
        os.makedirs(os.path.join(repos, "fresh"))
        os.environ["AB_REPOS_DIR"] = repos
        self.addCleanup(os.environ.pop, "AB_REPOS_DIR", None)
        _code, out = self.ab("run", "claude", "fresh", "--no-tty")
        self.assertIn(f"ab project add fresh {os.path.join(repos, 'fresh')} --preset", out)

    def test_a_placeholder_root_when_there_is_nothing_to_guess(self):
        _code, out = self.ab("run", "claude", "nowhere", "--no-tty")
        self.assertIn("ab project add nowhere <path to its code> --preset", out)


class Attach(SettingsCase):
    """`ab attach <project>`: the way back to the session the lock refuses to duplicate.

    The agent is a container the launcher started, so attaching is `podman attach` on it -- the same stdio
    the launcher has. That is the honest shape of re-attaching to a process; the multiplexed version is
    `ab ws` (design.md, step 7)."""

    def setUp(self):
        super().setUp()
        self.project("p1", preset="script")

    def running(self, role="agent", state="running"):
        os.environ["FAKE_PODMAN_PSJSON"] = ps_json("ab-claude-p1-1", role=role, state=state)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)

    def test_it_attaches_to_the_agent_container(self):
        self.running()
        code, out = self.ab("attach", "p1")
        self.assertEqual(code, 0, out)
        self.assertIn(["attach", "ab-claude-p1-1"], self.podman_calls(),
                      "the agent container carries the session's own name")
        self.assertIn("attaching to p1 (session ab-claude-p1-1)", out)
        self.assertIn("ctrl-p ctrl-q", out, "and says how to detach")

    def test_it_is_named_by_project_not_by_session_id(self):
        """The id is in the logs and the provenance; a user names the project."""
        self.running()
        self.assertEqual(self.ab("attach", "p1")[0], 0)
        self.assertEqual(self.ab("attach", "ab-claude-p1-1")[0], 2, "not an id")

    def test_nothing_running_says_how_to_start_one(self):
        os.environ["FAKE_PODMAN_PSJSON"] = "[]"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        code, out = self.ab("attach", "p1")
        self.assertEqual(code, 2)
        self.assertIn("no session of p1 is running", out)
        self.assertIn("ab run <agent> p1", out)

    def test_an_unregistered_project_gets_the_add_command(self):
        os.environ["FAKE_PODMAN_PSJSON"] = "[]"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        code, out = self.ab("attach", "nope")
        self.assertEqual(code, 2)
        self.assertIn("ab project add nope", out)

    def test_a_session_whose_agent_is_not_running_says_so(self):
        """Alive by its launcher (it may be starting), but there is no terminal to join yet."""
        self.running(role="proxy")
        d = self.settings.session_run_dir("ab-claude-p1-1")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "session.json"), "w") as f:
            json.dump({"project": "p1", "agent": "claude", "pid": os.getpid(),
                       "pid_start": session._start_time(os.getpid())}, f)
        code, out = self.ab("attach", "p1")
        self.assertEqual(code, 2)
        self.assertIn("agent container is not running", out)
        self.assertIn("ab session kill p1", out)


class ReleasedOnASignal(SettingsCase):
    """SIGTERM and SIGHUP end a session through `_guarded`, which tears down; the lock goes with it.

    Run in a child process, because the signal has to arrive at a launcher that is actually waiting."""

    def setUp(self):
        super().setUp()
        self.token()
        self.root = self.project("p1", preset="script")

    REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def launcher(self):
        """An `ab run` in its own process, waiting inside the session (the fake podman's `run` sleeps), so a
        signal reaches a launcher that is really holding the lock."""
        env = dict(os.environ, FAKE_PODMAN_PSJSON="[]", FAKE_PODMAN_RUN_SLEEP="10")
        p = subprocess.Popen([sys.executable, "-c",
                              "from agentbox import cli; cli.main(['run', 'claude', 'p1', '--no-tty'])"],
                             cwd=self.REPO, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(400):
            doc = projectlock.read(self.settings, "p1")
            if doc and doc.get("session"):
                return p
            if p.poll() is not None:
                out, err = p.communicate()
                raise AssertionError(f"the launcher exited early ({p.returncode}):\n{out}\n{err}")
            time.sleep(0.05)
        p.kill()
        raise AssertionError("the launcher never took the lock")

    def test_sigterm_and_sighup_release_the_lock_and_leave_nothing(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                p = self.launcher()
                try:
                    p.send_signal(sig)
                    p.wait(timeout=120)
                finally:
                    if p.poll() is None:
                        p.kill()
                    out, err = p.communicate()
                self.assertIsNone(projectlock.read(self.settings, "p1"),
                                  f"{sig.name} left the lock behind:\n{out}\n{err}")
                sessions_dir = os.path.join(self.settings.run_dir, "sessions")
                self.assertEqual(os.listdir(sessions_dir) if os.path.isdir(sessions_dir) else [], [],
                                 "and no session run dir")

    def test_sigkill_leaves_it_for_the_next_run_to_reap(self):
        """The one case a handler cannot cover -- which is why the stale check exists."""
        p = self.launcher()
        sid = projectlock.read(self.settings, "p1")["session"]
        p.kill()
        p.communicate()
        self.assertEqual(projectlock.read(self.settings, "p1")["session"], sid, "still held")
        gone, why = session.lock_holder_state(self.settings, Podman(self.settings.podman),
                                              projectlock.read(self.settings, "p1"))
        self.assertTrue(gone, why)


if __name__ == "__main__":
    unittest.main()
