"""Step 5: the end-of-session scan."""
import os
import subprocess
from unittest import mock

from agentbox import scan

from .helpers import TempCase

FAKE_KEY = "sk-" + "Abc123" * 5        # built at run time: the host hook would flag a literal one here
REGEX = r"sk-[a-zA-Z0-9_-]{20,}"         # one of the host hook's patterns


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


class Scan(TempCase):
    def setUp(self):
        super().setUp()
        self.repo = self.mkdir("proj")
        git(self.repo, "init", "-q")
        self.write("proj/README", "hi\n")
        self.write("proj/old.sh", "#!/bin/sh\n", mode=0o755)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.state = scan.snapshot([self.repo])

    def report(self):
        return scan.report(self.state, [self.repo], regex=REGEX)

    def test_nothing_changed_is_quiet(self):
        summary, text = self.report()
        self.assertEqual(summary, [])
        self.assertIn("commits added this session: 0", text)

    def test_secret_in_a_session_commit_found_even_with_the_allow_marker(self):
        self.write("proj/app.py", f'API_KEY = "{FAKE_KEY}"  # scanner:allow\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "add key")
        summary, text = self.report()
        self.assertTrue(any("possible secret" in s for s in summary), summary)
        self.assertIn("app.py", text)
        self.assertNotIn(FAKE_KEY, text, "matches are shown only by their first characters")

    def test_files_that_run_later(self):
        self.write("proj/Makefile", "all:\n\techo hi\n")
        self.write("proj/web/package.json", "{}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "build files")
        self.write("proj/.vscode/tasks.json", "{}\n")          # uncommitted
        self.write("proj/.envrc", "export X=1\n")             # untracked
        summary, text = self.report()
        for f in ("Makefile", "web/package.json", ".vscode/tasks.json", ".envrc"):
            self.assertIn(f"  {f}", text, f)
        self.assertTrue(any("run later" in s for s in summary))

    def test_new_executables_but_not_old_ones(self):
        self.write("proj/new.sh", "#!/bin/sh\n", mode=0o755)
        summary, text = self.report()
        self.assertIn("  new.sh", text)
        self.assertNotIn("  old.sh", text)

    def test_git_config_change(self):
        git(self.repo, "config", "core.hooksPath", "/tmp/evil")
        summary, _ = self.report()
        self.assertTrue(any("git config changed" in s for s in summary), summary)

    def test_repo_without_commits_at_start(self):
        repo2 = self.mkdir("proj2")
        git(repo2, "init", "-q")
        state = scan.snapshot([repo2])
        self.write("proj2/k.txt", f'token = "{FAKE_KEY}"  # scanner:allow\n')  # the host hook honours it; the scan must not
        git(repo2, "add", "-A")
        git(repo2, "commit", "-qm", "first")
        summary, _ = scan.report(state, [repo2], regex=REGEX)
        self.assertTrue(any("possible secret" in s for s in summary), summary)

    def test_reads_the_regex_from_the_global_hook(self):
        hooks = self.mkdir("hooks")
        self.write("hooks/pre-commit", f'#!/bin/bash\n{scan.HOOK_VAR}="abc[0-9]+|x\\"y"\n')
        fake = subprocess.CompletedProcess([], 0, stdout=hooks + "\n", stderr="")
        with mock.patch("agentbox.scan.subprocess.run", return_value=fake):
            regex, source = scan.host_secret_regex()
        self.assertEqual(regex, 'abc[0-9]+|x"y')
        self.assertTrue(source.endswith("pre-commit"))


class NoHook(TempCase):
    def test_without_a_hook_the_secret_scan_says_it_was_skipped(self):
        fake = subprocess.CompletedProcess([], 1, stdout="", stderr="")      # no core.hooksPath
        with mock.patch("agentbox.scan.subprocess.run", return_value=fake):
            regex, why = scan.host_secret_regex()
        self.assertIsNone(regex)
        self.assertIn("no host pre-commit hook", why)
