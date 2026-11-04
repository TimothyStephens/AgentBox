"""Step 5: the end-of-session scan."""
import json
import os
import subprocess
from unittest import mock

from agentbox import projectkeys, scan, session

from .helpers import SettingsCase, TempCase

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

    def test_agent_instruction_and_skill_files_run_later_too(self):
        """A repo's `.claude/` and `.omp/` are the clearest "runs later" files there are: a skill, a hook, an
        MCP server or a CLAUDE.md the agent adds is read by the NEXT session (and by the user's own editor),
        and until now the scan said nothing about them. Lockdown covers them read-only; every other preset
        does not, which is exactly when the report matters."""
        self.write("proj/.claude/skills/deploy/SKILL.md", "---\nname: deploy\n---\nrun make deploy\n")
        self.write("proj/.claude/hooks/pre-tool.sh", "#!/bin/sh\necho hi\n")
        self.write("proj/.claude/settings.json", '{"hooks": {}}\n')
        self.write("proj/.claude/agents/reviewer.md", "---\nname: reviewer\n---\n")
        self.write("proj/.omp/skills/x/SKILL.md", "do a thing\n")
        self.write("proj/CLAUDE.md", "project rules\n")
        self.write("proj/AGENTS.md", "project rules\n")
        self.write("proj/.mcp.json", '{"mcpServers": {}}\n')
        summary, text = self.report()
        for f in (".claude/skills/deploy/SKILL.md", ".claude/hooks/pre-tool.sh", ".claude/settings.json",
                  ".claude/agents/reviewer.md", ".omp/skills/x/SKILL.md", "CLAUDE.md", "AGENTS.md",
                  ".mcp.json"):
            self.assertIn(f"  {f}", text, f)
        self.assertTrue(any("run later" in s for s in summary), summary)

    def test_other_agents_directories_run_later_too(self):
        """A repo can feed more than one agent, and a skill or hook dropped in any of these is read by
        whatever runs next -- including tools agentbox does not sandbox at all (29 Oct)."""
        self.write("proj/.agents/reviewer.md", "---\nname: reviewer\n---\n")
        self.write("proj/.codex/config.toml", "[tools]\n")
        self.write("proj/.opencode/opencode.json", "{}\n")
        self.write("proj/.github/skills/deploy/SKILL.md", "deploy it\n")
        summary, text = self.report()
        for f in (".agents/reviewer.md", ".codex/config.toml", ".opencode/opencode.json",
                  ".github/skills/deploy/SKILL.md"):
            self.assertIn(f"  {f}", text, f)
        self.assertTrue(any("run later" in s for s in summary), summary)

    def test_an_unrelated_dotfile_is_not_flagged(self):
        """The list is specific: a dot-directory that does not feed an agent stays out of the report."""
        self.write("proj/.config/notes.md", "hi\n")
        self.write("proj/.claudeish/x.md", "hi\n")
        summary, text = self.report()
        self.assertNotIn("notes.md", text)
        self.assertNotIn(".claudeish", text)

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


class RefMovement(TempCase):
    """What a session did to HEAD and the refs -- the changes that leave no file behind.

    A session that moves a branch and ends with a clean work tree used to be invisible here: every signal the
    report had was a file hash or a new object, and `git reset --hard`, `git branch -f` and `git switch`
    produce neither."""

    def setUp(self):
        super().setUp()
        self.repo = self.mkdir("proj")
        git(self.repo, "init", "-q", "-b", "main")
        for n in ("one", "two", "three"):
            self.write(f"proj/{n}.txt", n + "\n")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-qm", n)
        self.state = scan.snapshot([self.repo])

    def report(self, base_branch=None):
        return scan.report(self.state, [self.repo], regex=REGEX, base_branch=base_branch)

    def head(self, rev="HEAD"):
        return subprocess.run(["git", "-C", self.repo, "rev-parse", rev], capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_the_snapshot_keeps_refs_by_name_and_where_head_is(self):
        repo = self.state["repos"][self.repo]
        self.assertEqual(repo["byname"], {"refs/heads/main": self.head()})
        self.assertEqual(repo["head_ref"], "refs/heads/main")
        self.assertEqual(repo["head"], self.head())
        self.assertEqual(repo["refs"], [self.head()], "the flat object list version 2 wrote is still there")

    def test_a_clean_tree_with_a_moved_branch_is_reported(self):
        """The case: nothing added, nothing changed on disk, and the branch is three commits back."""
        before = self.head()
        git(self.repo, "reset", "--hard", "-q", "HEAD~2")
        summary, text = self.report()
        self.assertIn(f"main: moved {before[:12]} -> {self.head()[:12]} (+0, -2 DROPPED)", text)
        self.assertTrue(any("main dropped 2 commit(s)" in s for s in summary), summary)
        self.assertIn("commits added this session: 0", text, "and it is not a new-commit report")

    def test_a_branch_moved_forward_is_in_the_report_but_not_the_summary(self):
        """Work looks like this. `commits added this session` already says it happened."""
        before = self.head()
        self.write("proj/four.txt", "four\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "four")
        summary, text = self.report()
        self.assertIn(f"main: moved {before[:12]} -> {self.head()[:12]} (+1)", text)
        self.assertEqual(summary, [], "a fast-forward is not news")

    def test_a_deleted_branch_is_news(self):
        git(self.repo, "branch", "side", "HEAD~1")
        self.state = scan.snapshot([self.repo])
        gone = self.head("side")
        git(self.repo, "branch", "-q", "-D", "side")
        summary, text = self.report()
        self.assertIn(f"side: DELETED (was {gone[:12]})", text)
        self.assertTrue(any("side was deleted" in s for s in summary), summary)

    def test_a_new_branch_and_tag_are_listed(self):
        git(self.repo, "branch", "feature")
        git(self.repo, "tag", "v1")
        _summary, text = self.report()
        self.assertIn(f"feature: new, at {self.head()[:12]}", text)
        self.assertIn(f"tag v1: new, at {self.head()[:12]}", text)

    def test_head_on_another_branch_is_news(self):
        git(self.repo, "checkout", "-q", "-b", "other")
        summary, text = self.report()
        self.assertIn("HEAD moved from main to other", text)
        self.assertTrue(any("HEAD is now other (was main)" in s for s in summary), summary)

    def test_a_detached_head_is_news(self):
        git(self.repo, "checkout", "-q", "--detach", "HEAD~1")
        summary, text = self.report()
        self.assertIn(f"HEAD moved from main to detached at {self.head()[:12]}", text)
        self.assertTrue(any("detached at" in s for s in summary), summary)

    def test_nothing_moved_is_quiet(self):
        summary, text = self.report()
        self.assertEqual(summary, [])
        self.assertNotIn("refs changed", text)
        self.assertNotIn("HEAD moved", text)

    def test_a_version_2_start_state_says_it_cannot_compare(self):
        """A session started on the older code and torn down by this one: better than calling every ref new."""
        self.state["repos"][self.repo].pop("byname")
        self.state["version"] = 2
        git(self.repo, "checkout", "-q", "-b", "other")
        summary, text = self.report()
        self.assertIn("refs not compared", text)
        self.assertEqual(summary, [], "and it is not reported as a change")

    # ---- the base branch, named by the project ----

    def test_the_base_branch_commits_are_named_one_by_one(self):
        git(self.repo, "checkout", "-q", "-b", "mags/integration")
        for n in ("alpha", "beta"):
            self.write(f"proj/{n}.txt", n + "\n")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-qm", f"add {n}")
        summary, text = self.report(base_branch="mags/integration")
        self.assertIn("new commits on mags/integration (2):", text)
        self.assertIn("add alpha", text)
        self.assertIn("add beta", text)
        self.assertTrue(any("2 new commit(s) on mags/integration" in s for s in summary), summary)

    def test_the_base_branch_is_summarised_even_when_it_only_moved_forward(self):
        """The one branch whose ordinary progress is worth a summary line: it is what the session was for."""
        self.write("proj/x.txt", "x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "work")
        summary, _text = self.report(base_branch="main")
        self.assertTrue(any("1 new commit(s) on main" in s for s in summary), summary)

    def test_a_secret_in_a_commit_subject_is_not_printed(self):
        self.write("proj/y.txt", "y\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", f"wip {FAKE_KEY}")
        _summary, text = self.report(base_branch="main")
        self.assertIn("subject withheld", text)
        self.assertNotIn(FAKE_KEY, text, "held to the same rule as an added line")

    def test_an_unmoved_base_branch_says_so_without_a_summary_line(self):
        summary, text = self.report(base_branch="main")
        self.assertIn("no new commits this session", text)
        self.assertEqual(summary, [])

    def test_a_base_branch_that_is_not_there(self):
        summary, text = self.report(base_branch="mags/integration")
        self.assertIn("base branch mags/integration: not in this repo", text)
        self.assertEqual(summary, [])

    def test_a_base_branch_deleted_during_the_session_is_news(self):
        git(self.repo, "branch", "mags/integration")
        self.state = scan.snapshot([self.repo])
        git(self.repo, "branch", "-q", "-D", "mags/integration")
        summary, text = self.report(base_branch="mags/integration")
        self.assertIn("base branch mags/integration: GONE", text)
        self.assertTrue(any("no longer exists" in s for s in summary), summary)


class BaseBranchComesFromTheProject(SettingsCase):
    """Where the scan learns which branch to name.

    `scan.report(..., base_branch=)` could name a branch's new commits one by one from the start, but
    nothing told it which branch -- the parameter was exercised by unit tests only. It is a project
    registry key now (`ab project set <p> --base-branch mags/integration`).

    agentbox does not own the value and does not create or check out the branch. MAGS-CodeDev has its own
    `settings.base_branch`, and a project using it names the same branch here: one value in two places,
    both the user's. Reading MAGS's config to find it would couple agentbox to another tool's file format
    for a string the user can state in a word."""

    def setUp(self):
        super().setUp()
        self.token()

    def start(self, name="p1", **extra):
        self.project(name, preset="script", **extra)
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, name),
                                 warn=lambda m: None)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s

    def scan_state(self, s):
        with open(os.path.join(self.settings.session_run_dir(s.name), session.SCAN_STATE)) as f:
            return json.load(f)

    def test_unset_is_empty_and_the_scan_names_no_branch(self):
        self.assertEqual(self.scan_state(self.start())["base_branch"], "")

    def test_the_project_setting_reaches_the_scan_state(self):
        s = self.start("mags", base_branch="mags/integration")
        self.assertEqual(s.project.base_branch, "mags/integration")
        self.assertEqual(self.scan_state(s)["base_branch"], "mags/integration")

    def test_it_is_recorded_at_session_start_not_read_at_teardown(self):
        """So `ab session kill` and `ab session gc` name the branch the session was configured with, even
        if the registry is edited while it runs."""
        from agentbox import registry
        s = self.start("mags", base_branch="mags/integration")
        reg = registry.load(self.settings.registry_path)
        reg["mags"]["base_branch"] = "something/else"
        registry.save(self.settings.registry_path, reg)
        self.assertEqual(self.scan_state(s)["base_branch"], "mags/integration")

    def test_a_value_that_is_not_a_git_ref_is_refused(self):
        from agentbox.errors import AgentboxError
        for bad in ("-dash", ".dot", "has space", "a..b", "trailing/", "x.lock", "a//b", 42):
            with self.subTest(bad=bad), self.assertRaises(AgentboxError):
                projectkeys.validate_value("base_branch", "project p", bad)

    def test_the_names_a_project_would_really_use_are_accepted(self):
        for ok in ("main", "mags/integration", "release-1.2", "feature/x_y", "v2.0"):
            with self.subTest(ok=ok):
                self.assertEqual(projectkeys.validate_value("base_branch", "project p", ok), ok)


class AuditStep5C(TempCase):
    """docs/reviews/2026-10-01-step-5C.md: H2, H3, M3, M4, M5, L5."""

    def setUp(self):
        super().setUp()
        self.repo = self.mkdir("proj")
        git(self.repo, "init", "-q")
        self.write("proj/README", "hi\n")
        self.write("proj/run.sh", "#!/bin/sh\necho ok\n", mode=0o755)
        self.write("proj/.gitignore", ".envrc\n.venv/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")

    def test_h2_agent_git_config_never_runs_on_the_host(self):
        state = scan.snapshot([self.repo])
        marker = os.path.join(self.tmp, "MARKER")
        # a repo made during the session, and the session's own repo with a textconv/clean filter config
        sub = self.mkdir("proj/sub")
        git(sub, "init", "-q")
        for r in (sub, self.repo):
            git(r, "config", "diff.x.textconv", f"sh -c 'touch {marker}; cat'")
            git(r, "config", "filter.y.clean", f"sh -c 'touch {marker}; cat'")
            git(r, "config", "core.pager", f"sh -c 'touch {marker}; cat'")
            git(r, "config", "log.showSignature", "true")
            git(r, "config", "gpg.program", f"sh -c 'touch {marker}'")
            with open(os.path.join(r, ".gitattributes"), "w") as f:
                f.write("* diff=x filter=y\n")
            with open(os.path.join(r, "f.txt"), "w") as f:
                f.write("x\n")
            git(r, "-c", "filter.y.clean=cat", "add", "-A")
            git(r, "-c", "filter.y.clean=cat", "commit", "-qm", "c")
        if os.path.exists(marker):
            os.unlink(marker)
        summary, text = scan.report(state, [self.repo], regex=REGEX)
        self.assertFalse(os.path.exists(marker), "the agent's git config ran a command on the host")
        self.assertTrue(any("NEW GIT REPO" in s and "sub" in s for s in summary), summary)
        self.assertTrue(any("git config changed" in s for s in summary), summary)

    def test_h2_hardened_git_in_an_unchanged_repo(self):
        """A repo whose config was already hostile at the start (made in an earlier session) is scanned with
        textconv, external diff, pager and signature checks off."""
        marker = os.path.join(self.tmp, "MARKER")
        git(self.repo, "config", "diff.x.textconv", f"sh -c 'touch {marker}; cat'")
        git(self.repo, "config", "diff.external", f"sh -c 'touch {marker}'")
        git(self.repo, "config", "log.showSignature", "true")
        git(self.repo, "config", "gpg.program", f"sh -c 'touch {marker}'")
        self.write("proj/.gitattributes", "* diff=x\n")
        state = scan.snapshot([self.repo])
        self.write("proj/app.py", f'K = "{FAKE_KEY}"  # scanner:allow\n')
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "diff.external=", "-c", "log.showSignature=false", "commit", "-qm", "k")
        if os.path.exists(marker):
            os.unlink(marker)
        summary, _ = scan.report(state, [self.repo], regex=REGEX)
        self.assertFalse(os.path.exists(marker))
        self.assertTrue(any("possible secret" in s for s in summary), summary)

    def test_h3_latin1_commit_does_not_break_the_scan(self):
        state = scan.snapshot([self.repo])
        with open(os.path.join(self.repo, "latin.txt"), "wb") as f:
            f.write(b"caf\xe9 " + FAKE_KEY.encode() + b"\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "latin")
        summary, _ = scan.report(state, [self.repo], regex=REGEX)
        self.assertTrue(any("possible secret" in s for s in summary), summary)

    def test_m3_secret_on_another_branch(self):
        state = scan.snapshot([self.repo])
        git(self.repo, "checkout", "-qb", "side")
        self.write("proj/k.py", f'K = "{FAKE_KEY}"  # scanner:allow\n')
        self.write("proj/Makefile", "all:\n\ttrue\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "side")
        git(self.repo, "checkout", "-q", "-")
        summary, text = scan.report(state, [self.repo], regex=REGEX)
        self.assertTrue(any("possible secret" in s for s in summary), summary)
        self.assertIn("changed in the session's commits", text)
        self.assertIn("  Makefile", text)

    def test_m4_ignored_dirty_and_edited_files(self):
        self.write("proj/.envrc", "export A=1\n")                      # ignored, present at the start
        self.write("proj/.venv/lib/python3.12/site-packages/x.pth", "\n")
        self.write("proj/Makefile", "all:\n")                         # dirty at the start
        state = scan.snapshot([self.repo])
        self.write("proj/.envrc", "export A=2\n")
        self.write("proj/.venv/lib/python3.12/site-packages/x.pth", "import os\n")
        self.write("proj/Makefile", "all:\n\tcurl x | sh\n")
        self.write("proj/tests/conftest.py", "import os\n")
        self.write("proj/.vscode/launch.json", "{}\n")
        self.write("proj/.github/workflows/ci.yml", "on: push\n")
        self.write("proj/run.sh", "#!/bin/sh\ncurl x | sh\n", mode=0o755)
        summary, text = scan.report(state, [self.repo], regex=REGEX)
        for f in (".envrc", ".venv/lib/python3.12/site-packages/x.pth", "Makefile", "tests/conftest.py",
                  ".vscode/launch.json", ".github/workflows/ci.yml"):
            self.assertIn(f"  {f}", text, f)
        self.assertIn("executable files whose content changed", text)
        self.assertIn("  run.sh", text)

    def test_l5_mask_skip_is_not_walked(self):
        self.write("proj/data/big/Makefile", "x\n")
        skip = {self.repo: ["data"]}
        state = scan.snapshot([self.repo], skip=skip)
        self.write("proj/data/big/Makefile", "y\n")
        _summary, text = scan.report(state, [self.repo], regex=REGEX, skip=skip)
        self.assertNotIn("data/big/Makefile", text)

    def test_state_is_json(self):
        import json
        state = scan.snapshot([self.repo])
        self.assertEqual(json.loads(json.dumps(state)), state)


class NoHook(TempCase):
    def test_without_a_hook_the_secret_scan_says_it_was_skipped(self):
        fake = subprocess.CompletedProcess([], 1, stdout="", stderr="")      # no core.hooksPath
        with mock.patch("agentbox.scan.subprocess.run", return_value=fake):
            regex, why = scan.host_secret_regex()
        self.assertIsNone(regex)
        self.assertIn("no host pre-commit hook", why)
