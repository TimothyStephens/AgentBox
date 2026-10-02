"""make trial-check's comparisons (tests/trial/check.py), on a fake home: no podman, nothing of Tim's read."""
import os
import time

from tests.trial import check

from .helpers import TempCase


class TrialCheck(TempCase):
    def test_mtime_scan_reports_changes_outside_the_exclusions(self):
        home = self.mkdir("home")
        for rel in ("repos/abt-trial/app.py", "repos/other/Makefile", ".local/share/agentbox/logs/x.log",
                    ".bashrc", "notes/a.txt"):
            self.write(os.path.join("home", rel), "x")
        old = time.time() - 3600
        for dirpath, dirnames, filenames in os.walk(home):
            for n in dirnames + filenames:
                os.utime(os.path.join(dirpath, n), (old, old))
        since = time.time() - 60
        self.write("home/repos/abt-trial/app.py", "changed")             # inside the trial project: fine
        self.write("home/.local/share/agentbox/logs/x.log", "more")      # ab's own logs: fine
        self.write("home/repos/other/Makefile", "changed")               # outside: reported
        os.utime(os.path.join(home, ".bashrc"), None)                    # outside: reported
        skip = {os.path.realpath(os.path.join(home, p)) for p in ("repos/abt-trial", ".local/share/agentbox")}
        changed = check.changed_since(since, skip, home=home)
        rel = sorted(os.path.relpath(p, home) for p in changed)
        self.assertIn("repos/other/Makefile", rel)
        self.assertIn(".bashrc", rel)
        self.assertFalse([p for p in rel if p.startswith(("repos/abt-trial", ".local/share/agentbox"))], rel)
        self.assertNotIn("notes/a.txt", rel)

    def test_live_stack_comparison(self):
        before = {"a": {"name": "media_web", "started": "t1", "restarts": 0},
                  "b": {"name": "deploy_app", "started": "t1", "restarts": 0}}
        self.assertEqual(check.compare_live(before, dict(before)), [])
        after = {"a": {"name": "media_web", "started": "t2", "restarts": 1},
                 "c": {"name": "deploy_app", "started": "t3", "restarts": 0}}
        problems = check.compare_live(before, after)
        self.assertEqual(len(problems), 3, problems)       # a restarted, b gone, c new (b recreated)
