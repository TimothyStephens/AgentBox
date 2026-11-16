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


class SessionBase(SettingsCase):
    """Fixture only: a project with a .env/.env.dev pair, a token and a seeded profile. Test classes subclass
    this, not SessionTest, so the SessionTest cases run once instead of once per subclass."""

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


class SessionTest(SessionBase):
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
        """What the state dir keeps, and where the user's own instructions and commands now come from.

        The fixture writes the PRE-PROFILE seed (`config/claude/{settings.json,CLAUDE.md,commands/}`). The
        profile migration moves it into `config/claude/profile/` at session start, after which `settings.json`
        is still copied into the state dir (Claude Code writes it), while `CLAUDE.md` is generated per
        session and `commands/` is mounted read-only from the profile snapshot -- neither is copied into the
        state dir any more. Every property this test checked still holds; two of them moved to the paths
        where the content is actually delivered (29 Oct)."""
        s = self.make()
        state = s.state_dir
        os.makedirs(os.path.join(state, "projects"), exist_ok=True)
        self.write(os.path.relpath(os.path.join(state, "projects", "hist.jsonl"), self.tmp), "old history")
        self.write(os.path.relpath(os.path.join(state, "settings.json"), self.tmp), '{"stale": true}')
        s.prepare()
        self.assertEqual(_read(os.path.join(state, "projects", "hist.jsonl")), "old history")
        settings = json.loads(_read(os.path.join(state, "settings.json")))
        self.assertEqual(settings["theme"], "dark")                    # the seed overwrote the stale file
        self.assertNotIn("stale", settings)
        # approvals off (python preset): the adapter establishes Claude's startup flag so the scan baseline
        # has it and the agent's own first-run write is not reported as a change (7 Oct)
        self.assertIs(settings["skipDangerousModePermissionPrompt"], True)
        mounted = {dst: src for src, dst, _ro in s.setup.mounts}
        md = _read(mounted["/run/agentbox/claude/CLAUDE.md"])
        from agentbox.adapters import claude
        self.assertTrue(md.startswith(claude.NOTES_BEGIN), "the sandbox block leads")
        self.assertLess(md.index("# agentbox sandbox"), md.index("My rules."), "the preamble leads the user's")
        self.assertEqual(md.count(claude.NOTES_BEGIN), 1, "the sandbox block is not stacked")
        self.assertIn("My rules.", md)                    # the user's instructions follow it
        self.assertEqual(os.path.getsize(os.path.join(state, "CLAUDE.md")), 0,
                         "the state copy is only the mount point the generated file is bound onto")
        self.assertTrue(os.path.isfile(os.path.join(mounted["/run/agentbox/claude/commands"], "x.md")),
                        "the seed's commands are mounted from the profile snapshot")
        cj = json.loads(_read(os.path.join(state, ".claude.json")))
        self.assertTrue(cj["projects"][self.root]["hasTrustDialogAccepted"])
        for dirpath, _d, files in os.walk(state):
            for f in files:
                with open(os.path.join(dirpath, f)) as fh:
                    self.assertNotIn(self.tok, fh.read())

    def test_backup_made_before_start(self):
        s = self.make()
        s.prepare()
        snaps = os.listdir(self.settings.project_backups_dir("p1"))
        self.assertEqual(len(snaps), 1)
        self.assertIn(s.name, snaps[0])

    def test_agent_cli_version_from_image_label(self):
        """Read as JSON through the fake podman, so the real parse runs -- not a mocked `podman.run`."""
        import json as _json
        s = self.make()
        img = s.adapter.image
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = _json.dumps(
            {img: {"Id": "sha256:aaa", "Config": {"Labels": {"agentbox.cli-version": "claude 1.2.3 (Claude Code)"}}}})
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        self.assertEqual(s.agent_cli_version(), "claude 1.2.3 (Claude Code)")
        # an image built before the label exists at all -> None, not a crash
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = _json.dumps({img: {"Id": "sha256:aaa", "Config": {}}})
        self.assertIsNone(s.agent_cli_version())
        # and an image podman does not have (exit 125) is also None: this reports, it decides nothing
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = "{}"
        self.assertIsNone(s.agent_cli_version())

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
        self.assertIn(["rm", "-f", "-v", "-t", "5", s.name], after)     # -v: anonymous volumes too (5C, M1)
        self.assertFalse(os.path.exists(self.settings.session_run_dir(s.name)))

    def test_teardown_runs_when_the_scan_raises(self):
        """Review step 5C, H3: a scan that raises must not leave the session running."""
        from unittest import mock
        s = self.make()
        s.prepare()
        warned = []
        s.warn = warned.append
        with mock.patch("agentbox.scan.report", side_effect=UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "x")):
            s.run_captured(["true"])
        self.assertFalse(os.path.exists(self.settings.session_run_dir(s.name)))
        self.assertTrue(any("scan failed" in w for w in warned), warned)
        calls = self.podman_calls()
        self.assertTrue(any(c[:2] == ["network", "ls"] for c in calls))

    def test_kill_keeps_logs_and_scans(self):
        """Review step 5C, M2: `ab session kill` (module teardown) keeps the gate log and writes the scan report."""
        s = self.make()
        s.prepare()
        os.makedirs(self.settings.service_dir(s.name), exist_ok=True)
        with open(os.path.join(self.settings.service_dir(s.name), "gate.log"), "w") as f:
            f.write('{"verdict": "deny"}\n')
        with open(os.path.join(self.root, "Makefile"), "w") as f:
            f.write("all:\n\tcurl x | sh\n")
        warned = []
        session.teardown(self.settings, s.podman, s.name, warn=warned.append)
        logs = os.listdir(os.path.join(self.settings.data_dir, "logs"))
        self.assertTrue(any(n.startswith(s.name) and n.endswith("-gate.log") for n in logs), logs)
        scan_logs = [n for n in logs if n.startswith(s.name) and n.endswith("-scan.log")]
        self.assertTrue(scan_logs, logs)
        self.assertIn("Makefile", _read(os.path.join(self.settings.data_dir, "logs", scan_logs[0])))
        self.assertTrue(any("run later" in w for w in warned), warned)

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


class CoveredPaths(SessionBase):
    """Critical (5 Oct): the gate needs the set of paths the agent container masks/covers, so it can refuse a
    spawned container's bind that reaches them."""

    def test_covered_paths_lists_masks_and_git_covers_not_grants(self):
        import os
        self.write("projects/p1/.env", "SECRET=1\n")
        self.write("projects/p1/secrets/db.txt", "pw\n")
        data_ro = self.mkdir("projects", "p1", "ro-grant")
        from agentbox import registry
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["data_ro"] = [data_ro]
        registry.save(self.settings.registry_path, reg)
        s = self.make()
        s.prepare()
        covered = set(s.covered_paths())
        root = self.root
        self.assertIn(os.path.realpath(os.path.join(root, ".env")), covered)
        self.assertIn(os.path.realpath(os.path.join(root, "secrets")), covered)
        self.assertIn(os.path.realpath(os.path.join(root, ".git", "hooks")), covered)
        self.assertIn(os.path.realpath(os.path.join(root, ".git", "config")), covered)
        self.assertNotIn(os.path.realpath(data_ro), covered)   # a read-only grant is not a cover
        self.assertNotIn(os.path.realpath(root), covered)
        self.assertEqual(s.covered_paths(), sorted(covered), "a sorted plain list for the gate config")


class CoverPins(SessionBase):
    """Audit 5 Oct, H1: every directory between a grant root and a cover is a mount point in the agent's
    container, so a cover's parent cannot be renamed away from the path the gate checks."""

    def test_pins_and_swap(self):
        import os
        from agentbox import registry
        self.write("projects/p1/dev/.env", "REAL=1\n")
        self.write("projects/p1/tests/fx/a.pem", "x\n")
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["podman"] = {"tier": "dev"}
        registry.save(self.settings.registry_path, reg)
        s = self.make()
        s.prepare()
        covered = s.covered_paths()
        root = self.root
        self.assertIn(os.path.join(root, ".env"), covered, "the dev-swap .env (.env.dev exists) is covered")
        args = " ".join(s.pin_args())
        for d in ("dev", "tests", "tests/fx", ".git"):
            self.assertIn(f"--mount=type=bind,src={root}/{d},dst={root}/{d}", args, d)
        self.assertNotIn(f"src={root},dst={root},", args, "the grant root is already a mount")
        argv = s.run_args(s.agent_command())
        self.assertTrue(any(f"src={root}/dev,dst={root}/dev" in a for a in argv), "pins are in the agent's argv")
        ids = s.cover_ids()
        self.assertEqual(ids[os.path.join(root, "dev", ".env")][1], os.lstat(os.path.join(root, "dev", ".env")).st_ino)

    def test_nothing_pinned_or_bound_inside_a_directory_mask(self):
        import os
        import subprocess
        from agentbox import registry
        vault = os.path.join(self.root, "secrets", "vault")
        os.makedirs(vault)
        subprocess.run(["git", "init", "-q", vault], check=True)
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["podman"] = {"tier": "dev"}
        registry.save(self.settings.registry_path, reg)
        s = self.make()
        s.prepare()
        sec = os.path.join(self.root, "secrets")
        self.assertFalse([c for c in s.covered_paths() if c.startswith(sec + "/")])
        argv = " ".join(s.run_args(s.agent_command()))
        self.assertNotIn(f"dst={sec}/", argv, "no mount or pin inside the secrets/ cover")
        self.assertEqual(argv.count(f"dst={sec},") + argv.count(f"dst={sec} "), 1, "one mount on secrets/")

    def test_no_pins_without_the_dev_tier(self):
        s = self.make()
        s.prepare()
        self.assertEqual(s.pin_args(), [])


class CheckSetup(SessionBase):
    """E13: _check_setup audits what an adapter asks the launcher to mount. None of its refusals had a test:
    an adapter mount or tmpfs outside the adapter's own root, a cover outside the project, a bad env name,
    and a comma/newline in a path."""

    def setup(self, s, **kw):
        from agentbox.adapters import SessionSetup
        st = SessionSetup()
        for k, v in kw.items():
            setattr(st, k, v)
        s.setup = st
        return s

    def make_prepared(self):
        s = self.make()
        s.setup = None
        return s

    def test_adapter_mount_outside_its_root_refused(self):
        s = self.setup(self.make_prepared(), mount_root="/x", mounts=[("/host/src", "/elsewhere/dst", True)])
        with self.assertRaises(AgentboxError) as e:
            s._check_setup()
        self.assertIn("not under the adapter's root", str(e.exception))

    def test_adapter_tmpfs_outside_its_root_refused(self):
        s = self.setup(self.make_prepared(), mount_root="/x", tmpfs=["/y/z"])
        with self.assertRaises(AgentboxError):
            s._check_setup()

    def test_cover_outside_the_project_refused(self):
        s = self.setup(self.make_prepared(), project_covers=["/etc/passwd"])
        with self.assertRaises(AgentboxError) as e:
            s._check_setup()
        self.assertIn("not inside the project", str(e.exception))

    def test_bad_env_name_refused(self):
        s = self.setup(self.make_prepared(), env={"not a name": "v"})
        with self.assertRaises(AgentboxError) as e:
            s._check_setup()
        self.assertIn("plain variable", str(e.exception))

    def test_comma_in_a_mount_path_refused(self):
        s = self.setup(self.make_prepared(), mount_root="/x", mounts=[("/host,src", "/x/dst", True)])
        with self.assertRaises(AgentboxError) as e:
            s._check_setup()
        self.assertIn("commas or newlines", str(e.exception))

    def test_clean_setup_passes(self):
        s = self.setup(self.make_prepared(), mount_root="/x", mounts=[("/host/src", "/x/dst", True)],
                       tmpfs=["/x/t"], project_covers=[os.path.join(self.root, "sub")], env={"OK_NAME": "v"})
        s._check_setup()                                    # no raise


class BackupExcludesCovers(SessionBase):
    """The per-session backup must not copy covered/masked paths (Tim, 7 Oct): the agent cannot damage the
    real .env or secrets/ (they are masked), so backing them up only leaves plaintext secret copies in the
    backups dir. Back up code, not secrets."""

    def test_backup_omits_covered_paths_keeps_code(self):
        import glob
        import subprocess
        canary = "BACKUP-COVERED-CANARY-7oct"
        self.write("projects/p1/.env", f"REAL={canary}\n")            # covered by the dev-swap
        self.write("projects/p1/.env.dev", "REAL=dev-fake\n")         # the fake twin, kept
        self.write("projects/p1/secrets/pw.txt", f"{canary}\n")       # covered by the secrets/ tmpfs
        self.write("projects/p1/app.py", "print('code')\n")          # code, kept
        sess = self.make()
        sess.prepare()

        bdir = self.settings.project_backups_dir("p1")
        snaps = glob.glob(os.path.join(bdir, "*", self.root.lstrip("/")))
        self.assertTrue(snaps, "a snapshot was written")
        snap = snaps[0]

        # Seed the assertion from the gate's own source, not a literal list: every path the gate covers,
        # that lives inside this grant, must be absent from the backup. If the backup's exclude list ever
        # stopped reading covered_paths() (a second copy), a cover would appear here and this fails.
        covers = [c for c in sess.covered_paths() if c.startswith(self.root.rstrip("/") + "/")]
        self.assertTrue(any(c.endswith("/.env") for c in covers) and any(c.endswith("/secrets") for c in covers),
                        f"the fixture should produce .env and secrets/ covers; got {covers}")
        for c in covers:
            rel = os.path.relpath(c, self.root)
            self.assertFalse(os.path.exists(os.path.join(snap, rel)),
                             f"covered path {rel} must not be in the backup (gate covers it)")

        self.assertTrue(os.path.isfile(os.path.join(snap, "app.py")), "code is backed up")
        self.assertTrue(os.path.isfile(os.path.join(snap, ".env.dev")), "the fake .env.dev is backed up")
        hits = subprocess.run(["grep", "-rl", canary, bdir], capture_output=True, text=True).stdout.strip()
        self.assertEqual(hits, "", f"a covered path was copied into the backups dir:\n{hits}")


class GrantExposure(SessionBase):
    """Track 1 (6 Oct): a session refuses to start when the grant holds uncovered secrets or live data."""

    def test_refuses_then_accepts(self):
        import os
        from agentbox import grantcheck, registry
        self.write("projects/p1/data/app.db", "x" * (2 << 20))        # live data in the grant
        with self.assertRaises(AgentboxError) as e:
            self.make().prepare()
        self.assertIn("data/app.db", str(e.exception))
        self.assertIn("accept-grant-exposure", str(e.exception))
        # record the ack and it starts
        exposure, _ = grantcheck.scan(self.root, [self.root], covered=[os.path.join(self.root, ".env"),
                                                                       os.path.join(self.root, "sub")])
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["grant_exposure_accepted"] = grantcheck.fingerprint(exposure)
        registry.save(self.settings.registry_path, reg)
        self.make().prepare()
        # a new exposure invalidates the ack
        self.write("projects/p1/other.sqlite", "y" * (2 << 20))
        with self.assertRaises(AgentboxError):
            self.make().prepare()

    def test_a_masked_secret_does_not_trip_it(self):
        # the fixture's .env/.env.dev and secrets/ are masked; no exposure, no ack needed
        self.make().prepare()

    def test_a_content_change_reprompts_not_just_a_path_change(self):
        """M4 (6 Oct): the ack is over each finding's CONTENT. Accept a finding, rewrite the same file's
        bytes, and the session refuses until it is re-acked — a path-only fingerprint would not catch this."""
        from agentbox import grantcheck, registry
        db = "projects/p1/data/app.db"
        cov = [os.path.join(self.root, ".env"), os.path.join(self.root, "sub")]

        def accept():
            exposure, _ = grantcheck.scan(self.root, [self.root], covered=cov)
            reg = registry.load(self.settings.registry_path)
            reg["p1"]["grant_exposure_accepted"] = grantcheck.fingerprint(exposure)
            registry.save(self.settings.registry_path, reg)

        self.write(db, "x" * (2 << 20))
        accept()
        self.make().prepare()                                   # accepted: starts
        self.write(db, "y" * (3 << 20))                         # SAME path, different content
        with self.assertRaises(AgentboxError):
            self.make().prepare()                               # content changed: re-prompt
        accept()
        self.make().prepare()                                   # re-acked: starts again

    def test_accept_shows_the_full_findings_not_a_count(self):
        """M4: `ab project accept-grant-exposure` prints every finding, not a tally."""
        import io
        from contextlib import redirect_stdout
        from agentbox import cli
        self.write("projects/p1/data/app.db", "x" * (2 << 20))
        self.write("projects/p1/more/other.sqlite", "y" * (2 << 20))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["project", "accept-grant-exposure", "p1"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("data/app.db", out)
        self.assertIn("more/other.sqlite", out)                 # both listed, not "2 findings"


class UserMaskInASession(SessionBase):
    """A masked directory (`mask`, 10 Nov) is a cover like `secrets/`: the gate refuses binds onto it, the
    backup leaves it out, the end-of-session scan skips it, and a build context carries it empty."""

    def test_masked_dir_is_covered_not_backed_up_and_not_scanned(self):
        import glob
        import json
        from agentbox import registry
        canary = "LIVE-DATA-CANARY-10nov"
        self.write("projects/p1/data/app.db", f"{canary}\n")
        self.write("projects/p1/dev/data/app.db", f"{canary}\n")
        self.write("projects/p1/app.py", "print('code')\n")
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["mask"] = ["data"]
        registry.save(self.settings.registry_path, reg)
        sess = self.make()
        sess.prepare()
        data = os.path.realpath(os.path.join(self.root, "data"))
        dev_data = os.path.realpath(os.path.join(self.root, "dev", "data"))
        self.assertIn(data, sess.covered_paths())
        self.assertIn(dev_data, sess.covered_paths())
        self.assertIn(data, sess.masked_paths())
        argv = " ".join(sess.run_args(sess.agent_command()))
        self.assertIn(f"type=tmpfs,dst={data},", argv)
        self.assertIn(",ro", argv.split(f"dst={data},")[1].split(" ")[0], "read-only, not a writable tmpfs")
        snaps = glob.glob(os.path.join(self.settings.project_backups_dir("p1"), "*", self.root.lstrip("/")))
        self.assertTrue(snaps)
        self.assertTrue(os.path.isfile(os.path.join(snaps[0], "app.py")))
        self.assertFalse(os.path.exists(os.path.join(snaps[0], "data")), "a masked directory is not backed up")
        self.assertFalse(os.path.exists(os.path.join(snaps[0], "dev", "data")))
        with open(os.path.join(self.settings.session_run_dir(sess.name), "scan-state.json")) as f:
            state = json.load(f)
        self.assertEqual(sorted(state["skip"][self.root]), ["data", "dev/data"])


class SandboxNotesNameTheMasks(SettingsCase):
    """The notes the agent reads list the secret names the masks actually hide, and say which `.env.*` names
    are exempt (the docs review found them claiming every `.env.*` is masked while `.env.dev` is shown)."""

    def test_notes_match_the_mask_tuple_and_name_the_exemptions(self):
        from agentbox import mounts
        self.project("p1")
        notes = session.sandbox_notes(session.load_project(self.settings, "p1"), "packages")
        for name in mounts.MASK_FILES:
            self.assertIn(f"`{name}`", notes, name)
        for name in mounts.MASK_EXEMPT:
            self.assertIn(f"`{name}`", notes, name)
        self.assertIn("are not", notes)


class LauncherGitRunsSafe(SettingsCase):
    """Review item 15 (16 Nov): the launcher's own git calls on agent-written trees (the history check, the clone
    for clone mode) take the end-of-session scan's safe flags and no system/global config, so a `core.hooksPath`
    or `diff.external` planted in one cannot run on the host."""

    def spy(self):
        import subprocess
        from unittest import mock
        calls, real = [], subprocess.run

        def run(argv, **kw):
            calls.append((argv, kw))
            return real(argv, **kw)
        patcher = mock.patch.object(session.subprocess, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def assert_safe(self, argv, kw):
        from agentbox import scan
        for flag in scan.GIT_SAFE[1::2]:
            self.assertIn(flag, argv, (flag, argv))
        self.assertEqual(kw["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(kw["env"]["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_the_history_check(self):
        root = self.project("p1")
        calls = self.spy()
        session._history_masked(os.path.join(root, ".git"), 60)
        git = [c for c in calls if c[0][:1] == ["git"]]
        self.assertEqual(len(git), 1)
        self.assert_safe(*git[0])

    def test_the_clone(self):
        import subprocess
        root = self.project("p1")
        self.write("projects/p1/a.txt", "x\n")
        subprocess.run(["git", "-C", root, "add", "."], check=True)
        subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "i"], check=True)
        calls = self.spy()
        session.clone_root(self.settings, "p1", "abt-unit-claude-p1-1", root)
        # toulmin.head_of's rev-parse goes through the same subprocess module; it is toulmin.py's to harden
        git = [c for c in calls if c[0][:1] == ["git"] and ("clone" in c[0] or "checkout" in c[0])]
        self.assertEqual(len(git), 2, [c[0] for c in git])
        for argv, kw in git:
            self.assert_safe(argv, kw)


from .test_toulmin import ToulminCase  # noqa: E402  (new classes only below; see item 52's restructure above)


class ToulminRecordInSessionJson(ToulminCase):
    def make(self):
        p = session.load_project(self.settings, "p1")
        return session.make_session(self.settings, "claude", p, warn=lambda m: None)

    def test_the_token_is_not_in_session_json(self):
        """Item 35: session.json is read by every teardown path and by `ab session ls`; the token is not needed
        there and lives only in its 0600 file."""
        s = self.make()
        s.prepare()
        self.addCleanup(s.teardown)
        meta = json.loads(_read(os.path.join(self.settings.session_run_dir(s.name), "session.json")))
        self.assertEqual(meta["toulmin"]["session"], "S4")
        self.assertNotIn("token", meta["toulmin"])
        self.assertNotIn(self.start_msg["token"], json.dumps(meta))

    def test_a_failed_clone_still_ends_the_toulmin_session(self):
        """Item 18: Toulmin starts before clone_root; if the clone fails, teardown only has session.json."""
        from unittest import mock
        s = self.make()
        with mock.patch.object(session, "clone_root", side_effect=AgentboxError("clone failed")), \
                mock.patch.object(session.toulmin, "end") as end:
            with self.assertRaises(AgentboxError):
                s.prepare()
        self.assertTrue(end.called, "teardown reached `tm session end`")
        self.assertEqual(end.call_args[0][0]["toulmin"]["session"], "S4")


class TeardownReportsFailedRemovals(SettingsCase):
    """Item 17: a failed `podman rm` / `network rm` used to be dropped; teardown now says what is left."""

    def test_a_failed_removal_is_warned_with_the_command(self):
        import subprocess
        from agentbox.podman import Podman

        class Failing(Podman):
            def run(self, args, **kw):
                if args[:1] == ["rm"] or args[:2] == ["network", "rm"]:
                    return subprocess.CompletedProcess(args, 125, "", "Error: container is busy")
                return super().run(args, **kw)

        os.environ["FAKE_PODMAN_PS"] = "abt-unit-x-1-proxy\n"
        os.environ["FAKE_PODMAN_NETLS"] = "abt-unit-x-1-int\n"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PS", None)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_NETLS", None)
        warned = []
        session.teardown(self.settings, Failing(self.settings.podman), "abt-unit-x-1", warn=warned.append)
        text = "\n".join(warned)
        self.assertIn("could not remove container abt-unit-x-1-proxy", text)
        self.assertIn("`podman rm -f -v -t 5 abt-unit-x-1-proxy`", text)
        self.assertIn("could not remove network abt-unit-x-1-int", text)
        self.assertIn("container is busy", text)


class PrepareTeardownIsShielded(SessionBase):
    """Item 36: the teardown after a failed prepare() runs with SIGINT/SIGTERM/SIGHUP ignored, like _guarded's."""

    def test_signals_are_ignored_during_the_teardown_and_restored_after(self):
        import signal
        from unittest import mock
        s = self.make()
        seen = {}
        real = s.teardown

        def spy(reason="error"):
            seen.update({sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)})
            real(reason)
        before = signal.getsignal(signal.SIGINT)
        with mock.patch.object(s, "teardown", spy), \
                mock.patch.object(s, "_prepare_reserved", side_effect=AgentboxError("boom")):
            with self.assertRaises(AgentboxError):
                s.prepare()
        self.assertEqual(set(seen.values()), {signal.SIG_IGN}, seen)
        self.assertEqual(signal.getsignal(signal.SIGINT), before)


class TokenDirIsProtected(SettingsCase):
    """Item 39: a `token_dir` outside the config dir could be granted to a project, token and all."""

    def test_a_project_root_holding_the_token_dir_is_refused(self):
        tokdir = self.mkdir("tokens")
        self.settings.values["token_dir"] = tokdir
        self.assertIn(os.path.realpath(tokdir), [os.path.realpath(p) for p in session.protected_extra(self.settings)])
        root = self.project("p1")
        self.settings.values["token_dir"] = os.path.join(root, "tok")
        os.makedirs(os.path.join(root, "tok"))
        with self.assertRaises(AgentboxError):
            session.load_project(self.settings, "p1")


class PluginTreeInTheSessionLog(SessionBase):
    """Item 41: the session's profile log names the plugin install it ran with."""

    def log(self, s):
        return _read(s.log_path("profile"))

    def test_no_tree_says_none(self):
        s = self.make()
        s.prepare()
        self.assertIn("plugins agent=claude none\n", self.log(s))

    def test_an_installed_tree_is_identified_by_its_lockfile_hash(self):
        import hashlib
        from agentbox import installer
        lock = installer.lock_path(self.settings, "claude")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        body = json.dumps({"version": 1, "agent": "claude", "installed_at": 1763251200})
        with open(lock, "w") as f:
            f.write(body)
        s = self.make()
        s.prepare()
        want = f"plugins agent=claude installed_at=1763251200 state={hashlib.sha256(body.encode()).hexdigest()}\n"
        self.assertIn(want, self.log(s))
