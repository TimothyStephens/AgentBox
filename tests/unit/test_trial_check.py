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

    def test_changes_split_and_truncated(self):
        home = check.HOME
        changed = [f"{home}/.claude/projects/x.jsonl", f"{home}/.config/claude-container/c.json",
                   "/srv/stackdata/config/db.sqlite", f"{home}/repos/someproj/notes.md"] + \
                  [f"{home}/repos/other/f{i}" for i in range(9)]
        fail, by_live, by_claude = check.split_changes(changed, ["/srv/stackdata/config"])
        self.assertEqual(by_live, ["/srv/stackdata/config/db.sqlite"])
        self.assertEqual(len(by_claude), 2)
        self.assertEqual(len(fail), 10)
        out = check.truncate(fail)
        self.assertIn(f"... and 4 more under {home}/repos/other", out)
        self.assertIn(f"{home}/repos/someproj/notes.md", out)

    def test_only_live_compose_containers_and_old_launchers(self):
        cs = [{"Id": "1", "Names": ["site_web"], "Labels": {"com.docker.compose.project": "site"}, "State": "running"},
              {"Id": "2", "Names": ["ab-claude-x-1"], "Labels": {"agentbox.session": "s",
                                                                "io.podman.compose.project": "x"}, "State": "running"},
              {"Id": "3", "Names": ["old_one"], "Labels": {}, "Image": "localhost/claudecode:latest",
               "State": "running"},
              {"Id": "4", "Names": ["loose"], "Labels": {}, "Image": "docker.io/library/busybox:1.36", "State": "exited"}]
        self.assertEqual([c["Id"] for c in cs if check.is_live(c)], ["1"])
        self.assertEqual(check.old_launchers(cs), ["old_one (localhost/claudecode:latest)"])

    def test_live_stack_comparison(self):
        before = {"a": {"name": "media_web", "started": "t1", "restarts": 0},
                  "b": {"name": "deploy_app", "started": "t1", "restarts": 0}}
        self.assertEqual(check.compare_live(before, dict(before)), [])
        after = {"a": {"name": "media_web", "started": "t2", "restarts": 1},
                 "c": {"name": "deploy_app", "started": "t3", "restarts": 0}}
        problems = check.compare_live(before, after)
        self.assertEqual(len(problems), 3, problems)       # a restarted, b gone, c new (b recreated)
