"""Session assembly and lifecycle against the fake podman."""
import json
import os

from agentbox import session
from agentbox.errors import AgentboxError
from agentbox.selftest import runner

from .helpers import SettingsCase


def _read(path):
    with open(path) as f:
        return f.read()


class SessionTest(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("p1")
        self.write("projects/p1/.env", "REAL=1\n")
        self.write("projects/p1/.env.dev", "DEV=1\n")
        self.tok = self.token()
        self.seed = self.mkdir("config", "claude", "commands")
        self.write("config/claude/settings.json", '{"theme": "dark"}')
        self.write("config/claude/CLAUDE.md", "My rules.\n")
        self.write("config/claude/commands/x.md", "cmd")

    def make(self, name="p1", **kw):
        p = session.load_project(self.settings, name, **kw)
        return session.make_session(self.settings, "claude", p, warn=lambda m: None)

    def test_argv(self):
        s = self.make()
        s.prepare()
        argv = s.run_args(s.agent_command())
        joined = " ".join(argv)
        self.assertEqual(argv[:2], ["run", "--rm"])
        self.assertIn("--name=abt-unit-claude-p1-1", argv)
        for want in ("--cap-drop=ALL", "--userns=keep-id", "--network=abt-unit-claude-p1-1-int", f"--workdir={self.root}",
                     "--label=agentbox-test=1", "--label=agentbox.owner=p1/abt-unit-claude-p1-1",
                     "--env=CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", "--env=DISABLE_AUTOUPDATER=1",
                     f"--mount=type=bind,src={self.root},dst={self.root}", "--entrypoint=/bin/sh"):
            self.assertIn(want, argv)
        self.assertIn(f"--mount=type=bind,src={self.settings.token_file('claude')},dst=/run/agentbox/token,ro", argv)
        self.assertEqual(argv[-2:], ["claude", "--dangerously-skip-permissions"])
        self.assertNotIn(self.tok, joined, "the token value is never on the command line")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN=", joined.replace("read -r CLAUDE_CODE_OAUTH_TOKEN", ""))

    def test_git_identity_env_only(self):
        self.settings.values.update(git_name="Tim Test", git_email="tim@example.com")
        s = self.make()
        s.prepare()
        argv = s.run_args(s.agent_command())
        for want in ("--env=GIT_AUTHOR_NAME=Tim Test", "--env=GIT_AUTHOR_EMAIL=tim@example.com",
                     "--env=GIT_COMMITTER_NAME=Tim Test via agentbox-claude",
                     "--env=GIT_COMMITTER_EMAIL=tim@example.com"):
            self.assertIn(want, argv)
        self.assertNotIn(".gitconfig", " ".join(argv))

    def test_build_memory_per_project(self):
        self.assertEqual(self.make().project.build_memory, 2 << 30)
        self.project("big", build_memory="3g")
        self.assertEqual(self.make("big").project.build_memory, 3 << 30)

    def test_lockdown_keeps_approvals(self):
        self.project("p2", preset="lockdown")
        s = self.make("p2")
        s.prepare()
        self.assertEqual(s.run_args(s.agent_command())[-1], "claude")

    def test_names_are_unique(self):
        a, b = self.make(), self.make()
        a.prepare()
        b.prepare()
        self.assertEqual((a.name, b.name), ("abt-unit-claude-p1-1", "abt-unit-claude-p1-2"))

    def test_token_file_must_be_private(self):
        self.token(mode=0o644)
        with self.assertRaises(AgentboxError):
            self.make().prepare()

    def test_state_seeded_by_copy_keeping_history(self):
        s = self.make()
        state = s.state_dir
        os.makedirs(os.path.join(state, "projects"), exist_ok=True)
        self.write(os.path.relpath(os.path.join(state, "projects", "hist.jsonl"), self.tmp), "old history")
        self.write(os.path.relpath(os.path.join(state, "settings.json"), self.tmp), '{"stale": true}')
        s.prepare()
        self.assertEqual(_read(os.path.join(state, "projects", "hist.jsonl")), "old history")
        self.assertEqual(json.loads(_read(os.path.join(state, "settings.json"))), {"theme": "dark"})
        md = _read(os.path.join(state, "CLAUDE.md"))
        self.assertTrue(md.startswith("My rules."))
        self.assertIn("# agentbox sandbox", md)
        self.assertTrue(os.path.isfile(os.path.join(state, "commands", "x.md")))
        cj = json.loads(_read(os.path.join(state, ".claude.json")))
        self.assertTrue(cj["projects"][self.root]["hasTrustDialogAccepted"])
        for dirpath, _d, files in os.walk(state):
            for f in files:
                with open(os.path.join(dirpath, f)) as fh:
                    self.assertNotIn(self.tok, fh.read())

    def test_backup_made_before_start(self):
        s = self.make()
        s.prepare()
        snaps = os.listdir(os.path.join(self.settings.backups_dir, "p1"))
        self.assertEqual(len(snaps), 1)
        self.assertIn(s.name, snaps[0])

    def test_dev_swap_in_argv(self):
        s = self.make()
        s.prepare()
        argv = s.run_args(s.agent_command())
        self.assertIn(f"--mount=type=bind,src={self.root}/.env.dev,dst={self.root}/.env", argv)

    def test_teardown_by_session_label(self):
        s = self.make()
        s.prepare()
        os.environ["FAKE_PODMAN_PS"] = f"{s.name}\n"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PS", None)
        s.run_captured(["true"])
        calls = self.podman_calls()
        run_idx = next(i for i, c in enumerate(calls) if c[:1] == ["run"])
        after = calls[run_idx + 1:]
        ps = [c for c in after if c[:1] == ["ps"]]
        self.assertTrue(ps and f"label=agentbox.session={s.name}" in ps[0] and "label=agentbox-test=1" in ps[0])
        self.assertIn(["rm", "-f", "-t", "5", s.name], after)
        self.assertFalse(os.path.exists(self.settings.session_run_dir(s.name)))

    def test_teardown_runs_when_run_fails(self):
        s = self.make()
        s.prepare()
        os.environ["FAKE_PODMAN_FAIL"] = "--entrypoint=/bin/sh"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        s.run_captured(["true"])
        self.assertFalse(os.path.exists(self.settings.session_run_dir(s.name)))

    def test_log_has_no_token(self):
        s = self.make()
        s.prepare()
        s.run_captured(["true"])
        log = _read(os.path.join(self.settings.data_dir, "logs", "ab.log"))
        self.assertIn("--cap-drop=ALL", log)
        self.assertNotIn(self.tok, log)

    def test_slice_required_when_configured(self):
        self.settings.values["cgroup_parent"] = "abt-no-such.slice"
        with self.assertRaises(AgentboxError) as e:
            self.make().prepare()
        self.assertIn("make install-slice", str(e.exception))

    def test_overlapping_project_refused(self):
        self.project("p1sub", git=False)
        from agentbox import registry
        reg = registry.load(self.settings.registry_path)
        reg["p1sub"]["root"] = os.path.join(self.root, ".git")
        registry.save(self.settings.registry_path, reg)
        with self.assertRaises(AgentboxError):
            self.make()

    def test_gc_removes_dead_sessions_only(self):
        dead, live = self.make(), self.make()
        dead.prepare()
        live.prepare()
        meta = os.path.join(self.settings.session_run_dir(dead.name), "session.json")
        m = json.loads(_read(meta))
        m["pid"] = 2 ** 22 + 1
        with open(meta, "w") as f:
            json.dump(m, f)
        removed = session.gc(self.settings, dead.podman)
        self.assertEqual(removed, [dead.name])
        self.assertTrue(os.path.isdir(self.settings.session_run_dir(live.name)))


class ScoreTest(SettingsCase):
    def test_score(self):
        b = runner.Builder()
        b.add("status", [], "posture", runner.posture_check)
        b.add("write", ["/usr/x"], "no /usr", runner.expect("fail"))
        b.add("which", ["git"], "git", runner.expect_not("missing"))
        out = ("c000 CapEff:0000000000000000 NoNewPrivs:1 Seccomp:2\nc001 ok\n"
               "c002 /usr/bin/git\n__done__ ok\n")
        verdicts = [r.verdict for r in runner.score(b.cases, out)]
        self.assertEqual(verdicts, ["PASS", "FAIL", "PASS"])

    def test_missing_answers_fail(self):
        b = runner.Builder()
        b.add("status", [], "posture", runner.posture_check)
        res = runner.score(b.cases, "")
        self.assertEqual([r.verdict for r in res], ["FAIL", "FAIL"])

    def test_posture_rejects_caps(self):
        self.assertEqual(runner.posture_check("CapEff:00000000a80425fb NoNewPrivs:1 Seccomp:2")[0], "FAIL")
        self.assertEqual(runner.posture_check("CapEff:0000000000000000 NoNewPrivs:0 Seccomp:2")[0], "FAIL")

    def test_env_names(self):
        ok = runner._env_names_check("HOME PATH CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
        bad = runner._env_names_check("HOME ANTHROPIC_API_KEY GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual((ok[0], bad[0]), ("PASS", "FAIL"))
        self.assertIn("ANTHROPIC_API_KEY", bad[1])

    def test_scrub(self):
        self.assertEqual(runner.scrub("a TOK b", ["TOK"]), "a [REDACTED] b")
