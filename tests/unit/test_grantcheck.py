"""Track 1 (Tim, 6 Oct): the grant holds code; a grant with secrets or live data refuses to start unless the
exposure is accepted. grantcheck.scan returns (findings, warnings); findings block, warnings only inform."""
import os

from agentbox import cli, grantcheck, registry, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, TempCase, read_file


def texts(findings):
    return [f["text"] for f in findings]


class GrantScan(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")

    def scan(self, **kw):
        return grantcheck.scan(self.root, [self.root], **kw)

    def test_live_data_and_secrets_are_found_covered_ones_are_not(self):
        self.write("proj/app/main.py", "code\n")
        self.write("proj/data/app.db", "x" * (2 << 20))
        self.write("proj/small.db", "x" * 1024)
        self.write("proj/.env", "SECRET=real\n")                     # masked below
        self.write("proj/config/service.pem", "KEY\n")              # uncovered secret
        findings, warnings = self.scan(covered=[os.path.join(self.root, ".env")])
        joined = "\n".join(texts(findings))
        self.assertIn("live data", joined)
        self.assertIn("data/app.db", joined)
        self.assertIn("config/service.pem", joined)
        self.assertNotIn("small.db", joined)
        self.assertNotIn(f"{self.root}/.env", joined, "a masked/covered secret is not flagged")

    def test_empty_secret_and_masked_secrets_dir_pass(self):
        self.write("proj/.env", "")
        self.write("proj/secrets/db.txt", "pw\n")
        findings, _ = self.scan(covered=[os.path.join(self.root, "secrets")])
        self.assertEqual(findings, [])

    def test_git_credentials_in_a_remote_url(self):
        import subprocess
        subprocess.run(["git", "init", "-q", self.root], check=True)
        subprocess.run(["git", "-C", self.root, "remote", "add", "origin",
                        "https://user:ghp_secrettoken@github.com/x/y.git"], check=True)
        findings, _ = self.scan()
        self.assertTrue(any("git credentials" in t for t in texts(findings)), findings)

    def test_compose_data_bind_flags_data_not_code(self):
        self.write("proj/data/app.db", "x")
        self.write("proj/app/main.py", "code\n")
        self.write("proj/compose.yaml",
                   "services:\n  web:\n    volumes:\n      - ./data:/data\n      - ./app:/app\n")
        findings, _ = self.scan()
        joined = texts(findings)
        self.assertTrue(any("compose data bind" in t and "proj/data (" in t for t in joined), joined)
        self.assertFalse(any("/app" in t for t in joined), "a code bind is not flagged")

    def test_mask_skip_is_not_an_exemption(self):
        """H3: mask_skip does not hide a secret from the scan, and the message doesn't offer it."""
        self.write("proj/vendor/big.db", "x" * (2 << 20))
        self.write("proj/vendor/.env", "SECRET=1\n")
        findings, _ = self.scan(mask_skip=["vendor"])
        joined = "\n".join(texts(findings))
        self.assertIn("vendor/big.db", joined)
        self.assertIn("vendor/.env", joined)
        self.assertIn("under mask_skip", joined)
        self.assertNotIn("mask_skip excludes", grantcheck.message("p", findings))

    def test_fingerprint_changes_with_content_not_just_path(self):
        self.write("proj/data/app.db", "x" * (2 << 20))
        f1, _ = self.scan()
        a = grantcheck.fingerprint(f1)
        self.assertEqual(a, grantcheck.fingerprint(self.scan()[0]))
        self.write("proj/data/app.db", "y" * (3 << 20))              # same path, new content
        self.assertNotEqual(a, grantcheck.fingerprint(self.scan()[0]), "content change re-prompts")

    def test_warns_on_foreign_owned_or_unreadable_entries(self):
        import unittest.mock
        self.write("proj/pgdata/PG_VERSION", "16\n")
        real_lstat = os.lstat
        pg = os.path.join(self.root, "pgdata")

        def fake(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if str(path) == pg:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1) + tuple(st)[5:])
            return st
        with unittest.mock.patch("agentbox.grantcheck.os.lstat", side_effect=fake):
            findings, warnings = self.scan()
        self.assertTrue(any("pgdata" in w and "uid" in w for w in warnings), warnings)
        self.assertEqual(findings, [], "a warning does not block on its own")

    def test_uncovered_file_under_secrets_dir_is_a_finding(self):
        """E15: only the covered secrets/ case was tested. An uncovered file under secrets/ must block."""
        self.write("proj/secrets/db.txt", "pw\n")
        findings, _ = self.scan()                                    # no cover
        self.assertTrue(any("secrets/db.txt" in t for t in texts(findings)), findings)

    def test_warns_on_a_foreign_owned_or_unreadable_file(self):
        """E14: only a foreign-owned directory was tested; a foreign-owned or unreadable FILE must warn too."""
        import unittest.mock
        self.write("proj/data/dump", "bytes\n")
        real_lstat = os.lstat
        f = os.path.join(self.root, "data", "dump")

        def fake(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if str(path) == f:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1) + tuple(st)[5:])
            return st
        with unittest.mock.patch("agentbox.grantcheck.os.lstat", side_effect=fake), \
             unittest.mock.patch("agentbox.grantcheck.os.access", return_value=False):
            findings, warnings = self.scan()
        self.assertTrue(any("data/dump" in w and "uid" in w for w in warnings), warnings)

    def test_read_only_data_grants_warn_but_never_block(self):
        """F4: data_ro was not scanned at all, so a read-only grant full of live data or secrets was
        invisible. It is scanned now, as warnings: read-only data grants are the point of
        analysis-offline/operate, so they stay allowed -- but you get told what is in them."""
        ro = self.mkdir("rodata")
        self.write("rodata/customers.db", "x" * (2 << 20))
        self.write("rodata/prod.pem", "KEY\n")
        self.write("proj/app/main.py", "code\n")
        findings, warnings = grantcheck.scan(self.root, [self.root], warn_grants=[ro])
        self.assertEqual(findings, [], "a read-only data grant never blocks")
        joined = "\n".join(warnings)
        self.assertIn("customers.db", joined)
        self.assertIn("prod.pem", joined)
        self.assertIn("read-only", joined.lower())

    def test_a_clean_read_only_grant_warns_about_nothing(self):
        ro = self.mkdir("rodata")
        self.write("rodata/sample.csv", "a,b\n1,2\n")
        findings, warnings = grantcheck.scan(self.root, [self.root], warn_grants=[ro])
        self.assertEqual((findings, warnings), ([], []))

    def test_a_covered_secret_in_a_read_only_grant_is_not_warned_about(self):
        ro = self.mkdir("rodata")
        self.write("rodata/.env", "SECRET=real\n")
        findings, warnings = grantcheck.scan(self.root, [self.root], warn_grants=[ro],
                                             covered=[os.path.join(ro, ".env")])
        self.assertEqual((findings, warnings), ([], []), "the mask covers it in the session too")

    def test_git_credentials_in_config_worktree(self):
        """E16: creds are checked in config AND config.worktree; only config was exercised."""
        gitdir = self.mkdir("proj", ".git")
        self.write("proj/.git/config", "[core]\n")
        self.write("proj/.git/config.worktree",
                   '[remote "o"]\n\turl = https://user:ghp_tok@github.com/x/y.git\n')
        findings, _ = self.scan()
        self.assertTrue(any("git credentials" in t and "config.worktree" in t for t in texts(findings)), findings)

    def test_compose_long_form_source_and_var_and_single_file_and_covered(self):
        """E16: compose long-form `source:`, a `$VAR` source skipped, a bind of a single db file, and a
        covered compose bind not flagged."""
        self.write("proj/db/live.db", "x")
        self.write("proj/onefile.db", "x")
        self.write("proj/compose.yaml",
                   "services:\n"
                   "  a:\n    volumes:\n      - type: bind\n        source: ./db\n        target: /db\n"
                   "  b:\n    volumes:\n      - $HOSTDIR:/x\n"
                   "  c:\n    volumes:\n      - ./onefile.db:/f\n")
        findings, _ = self.scan()
        joined = texts(findings)
        self.assertTrue(any("compose data bind" in t and "proj/db " in t for t in joined), joined)
        self.assertTrue(any("onefile.db" in t for t in joined), "a single-file db bind is flagged")
        self.assertFalse(any("$HOSTDIR" in t or "HOSTDIR" in t for t in joined), "a $VAR source is skipped")
        # the same db dir, once covered, is no longer a finding
        covered_findings, _ = self.scan(covered=[os.path.join(self.root, "db"),
                                                 os.path.join(self.root, "onefile.db")])
        self.assertFalse(any("compose data bind" in t for t in texts(covered_findings)), texts(covered_findings))


class ReadOnlyGrantAtSessionStart(SettingsCase):
    """F4 at the third surface: starting a session reports what a read-only data grant holds, and still
    starts. (add/set are covered in test_project_set.py.)"""

    def test_a_read_only_data_grant_warns_at_session_start(self):
        self.token()
        ro = self.mkdir("rodata")
        with open(os.path.join(ro, "customers.db"), "wb") as f:
            f.write(b"x" * (2 << 20))
        self.project("p1", preset="python", data_ro=[ro])
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        self.assertTrue(any("customers.db" in w for w in warnings), warnings)
        self.assertTrue(any("read-only" in w for w in warnings), warnings)


class CheckCommand(SettingsCase):
    """`ab project check NAME`: read-only. The stored grant-exposure acknowledgement is an opaque
    fingerprint, so there was no way to see WHAT had been accepted, or whether it still described the
    grant. This runs the same scan a session runs and says so, changing nothing."""

    def setUp(self):
        super().setUp()
        self.root = self.project("p1", preset="script")

    def run_check(self, name="p1"):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        before = read_file(self.settings.registry_path, "rb")
        with redirect_stdout(buf):
            try:
                code = cli.main(["project", "check", name])
            except SystemExit as e:
                code = e.code
        self.assertEqual(read_file(self.settings.registry_path, "rb"), before,
                         "check is read-only: the registry must be untouched")
        return code, buf.getvalue()

    def expose(self, mb=2):
        with open(os.path.join(self.root, "data.db"), "wb") as f:
            f.write(b"x" * (mb << 20))

    def accept(self):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["project", "accept-grant-exposure", "p1"]), 0)
        stored = registry.load(self.settings.registry_path)["p1"]["grant_exposure_accepted"]
        return grantcheck.acceptance_fingerprint(stored)     # the short id `show` and `check` print

    def test_a_clean_grant_reports_nothing_and_exits_zero(self):
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("none", out)

    def test_findings_are_listed_in_full_and_refuse(self):
        self.expose()
        code, out = self.run_check()
        self.assertEqual(code, 1, "a session would refuse, so check says so in its exit code")
        self.assertIn("data.db", out)
        self.assertIn("live data", out)
        self.assertIn("not accepted", out)

    def test_an_accepted_exposure_shows_what_it_covers_and_exits_zero(self):
        self.expose()
        fp = self.accept()
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("data.db", out, "the findings the fingerprint stands for")
        self.assertIn(fp, out, "the stored fingerprint, so the opaque number in `show` can be matched")
        self.assertIn("accepted", out)

    def test_a_changed_finding_shows_the_acceptance_no_longer_matches(self):
        self.expose()
        fp = self.accept()
        self.expose(mb=3)                                   # same path, new contents
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("changed", out.lower())
        self.assertIn(fp, out, "what was accepted")
        self.assertIn("accept-grant-exposure", out, "and how to accept the new state")

    def test_a_stale_acceptance_is_called_stale(self):
        self.expose()
        self.accept()
        os.remove(os.path.join(self.root, "data.db"))       # nothing exposed any more
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("stale", out.lower())

    def test_it_works_while_a_session_is_live(self):
        """Read-only, so unlike rm/set/accept-* it is not refused while containers are running -- seeing
        what a live project accepted is exactly when you want it."""
        import json
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(
            [{"Names": ["ab-claude-p1-1-agent"], "State": "running",
              "Labels": {"agentbox.project": "p1", "agentbox.session": "ab-claude-p1-1",
                         "agentbox-test": "1"}}])
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        self.expose()
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertNotIn("while containers", out)

    def test_open_egress_beside_an_accepted_exposure_is_called_out(self):
        self.expose()
        self.accept()
        self.project("p1", preset="script", egress="open",
                     grant_exposure_accepted=registry.load(self.settings.registry_path)["p1"]
                     ["grant_exposure_accepted"])
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("egress is open", out.lower())

    def test_unknown_project(self):
        code, _out = self.run_check("nope")
        self.assertEqual(code, 2)

    def test_show_points_at_check_when_an_acceptance_is_stored(self):
        import io
        from contextlib import redirect_stdout

        def show():
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["project", "show", "p1"])
            return buf.getvalue()

        self.assertNotIn("ab project check", show(), "nothing accepted: nothing to point at")
        self.expose()
        self.accept()
        out = show()
        self.assertIn("grant_exposure_accepted", out)
        self.assertIn("ab project check p1", out)


class OnePreflight(SettingsCase):
    """F-6: `add`/`set`/`check` built their own mount plan and their own "covered" predicate (cli._mask_dsts),
    while a session used another (session._plan_covered). So the verdict `ab project check` printed was not
    the one `ab session start` reached. One plan, one covered computation, one scan entry point.

    Unifying them also separates two senses of "covered" that shared a name. A mask reads EMPTY, so a secret
    behind one is unreachable and not a finding. A read-only cover (`.git/hooks`, `.git/config`, compose
    files) is still fully READABLE -- it only stops writes -- so it must never hide a secret from the scan,
    which is what the session's set did."""

    def setUp(self):
        super().setUp()
        self.token()
        self.root = self.project("p1", preset="script")
        self.write("projects/p1/app.py", "code\n")

    def entry(self):
        return dict(registry.load(self.settings.registry_path)["p1"])

    def preflight(self):
        return grantcheck.preflight(self.settings, "p1", self.entry())

    def start(self):
        """What `ab session start` decides, through the real session path."""
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s, warnings

    def secret_in_a_read_only_cover(self):
        p = os.path.join(self.root, ".git", "hooks", "deploy.key")
        with open(p, "w") as f:
            f.write("PRIVATE KEY\n")
        return p

    def test_a_read_only_cover_does_not_hide_a_secret_from_the_scan(self):
        self.secret_in_a_read_only_cover()
        findings, _warnings, fp = self.preflight()
        self.assertIn("deploy.key", "\n".join(texts(findings)), "a read-only cover is readable")
        self.assertTrue(fp)
        with self.assertRaises(AgentboxError) as cm:
            self.start()
        self.assertIn("deploy.key", str(cm.exception), "and the session refuses on the same finding")

    def test_a_mask_does_hide_one(self):
        """The other half: `.env` IS masked (it reads empty), so it is not a finding -- in both places."""
        self.write("projects/p1/.env", "SECRET=real\n")
        findings, _w, _fp = self.preflight()
        self.assertEqual(texts(findings), [])
        self.start()

    def test_check_and_the_session_reach_the_same_findings(self):
        self.secret_in_a_read_only_cover()
        with open(os.path.join(self.root, "live.db"), "wb") as f:
            f.write(b"x" * (2 << 20))
        findings, _w, fp = self.preflight()
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["grant_exposure_accepted"] = fp
        registry.save(self.settings.registry_path, reg)
        self.start()        # the fingerprint the pre-session scan produced is the one a session checks
        self.assertEqual(len(findings), 2, texts(findings))

    def test_the_two_covered_sets_are_each_computed_once(self):
        from agentbox import mounts
        self.write("projects/p1/.env", "SECRET=real\n")
        s, _w = self.start()
        hooks = os.path.realpath(os.path.join(self.root, ".git", "hooks"))
        env = os.path.realpath(os.path.join(self.root, ".env"))
        hidden = mounts.hidden(s.plan)
        covered = mounts.covered(s.plan, s.bind_roots)
        self.assertIn(env, hidden, "a mask reads empty")
        self.assertNotIn(hooks, hidden, "a read-only cover is readable, so it hides nothing")
        self.assertLess(set(hidden), set(covered))
        self.assertIn(hooks, covered, "the gate still refuses a spawned bind onto a read-only cover")
        self.assertEqual(set(s._plan_covered()), set(covered), "the session has no second computation")
        self.assertIn(hooks, s.covered_paths())
        self.assertNotIn(os.path.realpath(self.root), covered, "a grant root is not a cover")


class MaskClearsAFinding(SettingsCase):
    """A masked directory is unreadable in the session, so what is in it is not an exposure: the compose
    data bind and the live database under `data/` stop being findings once `data` is masked, and the
    foreign-uid warning for the directory goes with them."""

    def test_mask_covers_a_compose_data_bind_and_live_data(self):
        root = self.project("p1", preset="script")
        self.write("projects/p1/data/app.db", "x" * (2 << 20))
        self.write("projects/p1/dev/data/app.db", "x" * (2 << 20))
        self.write("projects/p1/deploy/compose.yaml",
                   "services:\n  web:\n    volumes:\n      - ../data:/data\n")
        entry = {"root": root, "preset": "script"}
        findings, _w, _fp = grantcheck.preflight(self.settings, "p1", entry)
        self.assertEqual(len(findings), 3, texts(findings))
        findings, warnings, fp = grantcheck.preflight(self.settings, "p1", dict(entry, mask=["data"]))
        self.assertEqual(findings, [], texts(findings))
        self.assertEqual(fp, "")
        findings, _w, _fp = grantcheck.preflight(self.settings, "p1", dict(entry, mask=["./data"]))
        self.assertEqual(len(findings), 1, "./data is root-only: dev/data is still live data")
        self.assertIn("dev/data/app.db", texts(findings)[0])
