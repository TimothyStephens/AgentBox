"""The end of a session (lifecycle.teardown), after the 18 Nov stall: a `toulmin-build` launcher sat ~22.5 hours
between writing its end-of-session scan and releasing its project lock, so the next `ab session start` was refused
with "is running". The order is now: remove what runs, do the bookkeeping the next session shares, free the
project, then the long and slow parts (the Toulmin end, the grant scan), output last.

Every test that waits on something runs the teardown in a thread with a deadline, so a regression fails here
instead of hanging the suite."""
import json
import os
import threading
import time
from unittest import mock

from agentbox import lifecycle, projectlock, session
from agentbox.podman import Podman

from .helpers import SettingsCase

SID = "abt-unit-claude-p1-1"


class TeardownCase(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("p1", preset="script")
        self.podman = Podman(self.settings.podman)

    def old_session(self, sid=SID, pid=None, pid_start=None):
        """A session as a launcher leaves it: run dir, session.json, the scan's start state, the lock."""
        run = self.settings.session_run_dir(sid)
        os.makedirs(run)
        pid = os.getpid() if pid is None else pid
        pid_start = session._start_time(pid) if pid_start is None else pid_start
        with open(os.path.join(run, "session.json"), "w") as f:
            json.dump({"session": sid, "agent": "claude", "project": "p1", "pid": pid, "pid_start": pid_start,
                       "started": time.time()}, f)
        with open(os.path.join(run, lifecycle.SCAN_STATE), "w") as f:
            json.dump({"grants": [self.root], "skip": {self.root: []}, "state": {}}, f)
        ok, _ = projectlock.acquire(self.settings, "p1", pid=pid, pid_start=pid_start)
        self.assertTrue(ok)
        projectlock.note_session(self.settings, "p1", sid)
        return run

    def in_thread(self, fn):
        box = {}

        def go():
            try:
                box["result"] = fn()
            except BaseException as e:            # pragma: no cover - reported by the caller
                box["error"] = e
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t, box

    def blocking_scan(self):
        """scan.report that signals it was entered, then waits for `release`."""
        entered, release = threading.Event(), threading.Event()

        def report(*_a, **_k):
            entered.set()
            release.wait(20)
            return ["something to review"], "the report"
        self.addCleanup(release.set)
        return entered, release, report


class TheProjectIsFreeBeforeTheScan(TeardownCase):
    def test_the_lock_is_released_while_the_scan_still_runs(self):
        run = self.old_session()
        entered, release, report = self.blocking_scan()
        with mock.patch.object(lifecycle.scan, "report", report):
            t, box = self.in_thread(lambda: lifecycle.teardown(self.settings, self.podman, SID,
                                                               warn=lambda m: None))
            self.assertTrue(entered.wait(20), "the scan never started")
            self.assertIsNone(projectlock.read(self.settings, "p1"), "the lock is still held during the scan")
            # ... and liveness agrees: the old session no longer counts as the project's live session, but it
            # is still being torn down, so nothing may reap it and its clone stays
            self.assertIsNone(session.live_session_of(self.settings, self.podman, "p1"))
            self.assertEqual(session.sessions_in_use(self.settings, self.podman, "p1"), {SID})
            info = session.sessions(self.settings, self.podman)[SID]
            self.assertEqual(session.session_is_alive(self.settings, SID, info), (True, "stopping"))
            self.assertTrue(os.path.isdir(run), "the run dir keeps its session number taken until the end")
            # a new session of the project can take the lock now
            lock = session.take_project_lock(self.settings, self.podman, "p1")
            lock.note("abt-unit-claude-p1-2")
            release.set()
            t.join(20)
        self.assertFalse(t.is_alive())
        self.assertNotIn("error", box)
        self.assertFalse(os.path.isdir(run))
        holder = projectlock.read(self.settings, "p1")
        self.assertEqual(holder["session"], "abt-unit-claude-p1-2", "the old teardown left the new lock alone")
        logs = os.path.join(self.settings.data_dir, "logs")
        scan_log = [n for n in os.listdir(logs) if n.startswith(SID) and n.endswith("-scan.log")]
        with open(os.path.join(logs, scan_log[0])) as f:
            text = f.read()
        self.assertIn("note: abt-unit-claude-p1-2 of p1 started at", text,
                      "the report says a session started while it ran")

    def test_the_inbox_and_the_agent_state_are_read_before_the_release(self):
        """They are what the next session writes: read after the release, its items and its memory edits
        would be reported as this session's."""
        self.old_session()
        seen = []
        with mock.patch.object(lifecycle, "_inbox_report",
                               lambda *a, **k: seen.append(("inbox", projectlock.read(self.settings, "p1")))), \
                mock.patch.object(lifecycle.scan, "report", lambda *a, **k: ([], "")):
            with open(os.path.join(self.settings.session_run_dir(SID), lifecycle.SCAN_STATE), "w") as f:
                json.dump({"grants": [self.root], "skip": {}, "state": {},
                           "agent_state": {"dir": self.mkdir("state"), "files": {}}}, f)
            real = lifecycle.scan.state_report

            def state_report(*a):
                seen.append(("state", projectlock.read(self.settings, "p1")))
                return real(*a)
            with mock.patch.object(lifecycle.scan, "state_report", state_report):
                lifecycle.teardown(self.settings, self.podman, SID, warn=lambda m: None)
        self.assertEqual([k for k, _ in seen], ["inbox", "state"])
        self.assertTrue(all(holder for _, holder in seen), "the lock was already free")

    def test_the_mcp_entry_goes_before_the_release_and_tm_end_after(self):
        """The next session adds its own `toulmin` MCP entry under the same name: removing ours after it may
        have started would take its entry away. `tm session end` concerns this session only."""
        self.old_session()
        seen = []
        with mock.patch.object(lifecycle, "_mcp_remove",
                               lambda *a: seen.append(("mcp", projectlock.read(self.settings, "p1")))), \
                mock.patch.object(lifecycle, "_toulmin_end",
                                  lambda *a: seen.append(("end", projectlock.read(self.settings, "p1")))), \
                mock.patch.object(lifecycle.scan, "report", lambda *a, **k: ([], "")):
            lifecycle.teardown(self.settings, self.podman, SID, warn=lambda m: None)
        self.assertEqual([k for k, _ in seen], ["mcp", "end"])
        self.assertIsNotNone(seen[0][1], "the MCP entry was removed after the lock was free")
        self.assertIsNone(seen[1][1], "`tm session end` ran before the lock was free")


class BookkeepingBeforeOutput(TeardownCase):
    """The launcher went quiet right after writing its scan report, in the stretch whose next act was writing
    the scan summary to the terminal -- with every signal ignored. A terminal that stops reading (output
    paused, an SSH client asleep) blocks that write for as long as it stays that way."""

    def test_the_scan_summary_comes_after_the_release_the_toulmin_end_and_the_run_dir(self):
        run = self.old_session()
        events = []
        real_end = lifecycle._toulmin_end

        def warn(msg):
            events.append(("warn", msg, projectlock.read(self.settings, "p1") is None, os.path.isdir(run),
                           "end" in [e[0] for e in events]))

        def end(*a):
            events.append(("end",))
            return real_end(*a)
        with mock.patch.object(lifecycle.scan, "report", lambda *a, **k: (["Makefile changed"], "the report")), \
                mock.patch.object(lifecycle, "_toulmin_end", end), \
                mock.patch.object(lifecycle, "_inbox_report", lambda s, d, sess, w: w("agentbox: 1 new item(s)")):
            lifecycle.teardown(self.settings, self.podman, SID, warn=warn)
        said = [e for e in events if e[0] == "warn"]
        summary = [e for e in said if "end-of-session scan (review" in e[1]]
        inbox = [e for e in said if "new item" in e[1]]
        self.assertEqual(len(summary), 1, events)
        self.assertEqual(len(inbox), 1, events)
        for e in summary + inbox:
            _w, msg, lock_free, run_dir_there, ended = e
            self.assertTrue(lock_free, f"printed before the lock was released: {msg}")
            self.assertTrue(ended, f"printed before `tm session end`: {msg}")
            self.assertFalse(run_dir_there, f"printed before the run dir was removed: {msg}")
        self.assertIn("full report:", summary[0][1], "and the summary still names the kept report")

    def test_a_terminal_that_stops_reading_does_not_hold_the_teardown(self):
        run = self.old_session()
        stuck = threading.Event()
        self.addCleanup(stuck.set)

        def warn(_msg):
            stuck.wait(30)                       # the first write blocks, as on a paused terminal
        with mock.patch.object(lifecycle.scan, "report", lambda *a, **k: (["x"], "the report")), \
                mock.patch.object(lifecycle, "OUTPUT_DRAIN", 0.5, create=True), \
                mock.patch("agentbox.progress.DELAY", 0.0):       # so every step writes a line at once
            t, box = self.in_thread(lambda: lifecycle.teardown(self.settings, self.podman, SID, warn=warn))
            t.join(15)
        self.assertFalse(t.is_alive(), "teardown waited on the terminal")
        self.assertNotIn("error", box)
        self.assertIsNone(projectlock.read(self.settings, "p1"))
        self.assertFalse(os.path.isdir(run))

