"""CLI ergonomics and the forgotten-session guards (Tim, 6 Oct):
  - ab session ls shows start time, age and idle time, and flags a long-idle running session;
  - ab session start warns when a session for that project is already running, and takes positional agent/project;
  - ab project rm takes several names."""
import io
import os
import time
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from agentbox import cli, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase


class IdleTest(SettingsCase):
    def test_idle_seconds_tracks_agent_state_mtime(self):
        meta = {"project": "p1", "agent": "claude"}
        self.assertIsNone(session.idle_seconds(self.settings, meta), "no state dir yet")
        sd = os.path.join(self.settings.state_dir("p1", "claude"))
        os.makedirs(sd)
        f = os.path.join(sd, "transcript.jsonl")
        open(f, "w").close()
        old = time.time() - 7200
        os.utime(f, (old, old))
        os.utime(sd, (old, old))
        idle = session.idle_seconds(self.settings, meta)
        self.assertAlmostEqual(idle, 7200, delta=120)

    def test_idle_none_without_meta(self):
        self.assertIsNone(session.idle_seconds(self.settings, {}))
        self.assertIsNone(session.idle_seconds(self.settings, None))

    def test_idle_seconds_uses_ephemeral_dir_for_lockdown(self):
        # A lockdown session writes an ephemeral per-session dir, not the persistent one; idle must read it
        # (audit 2026-10-15 #4), or ab session ls marks a busy lockdown session idle.
        meta = {"project": "p1", "agent": "claude", "session": "abt-claude-p1-1"}
        eph = os.path.join(session.ephemeral_state(self.settings, "abt-claude-p1-1"), "claude")
        os.makedirs(eph)
        f = os.path.join(eph, "transcript.jsonl")
        open(f, "w").close()
        now = time.time()
        os.utime(f, (now, now))
        os.utime(eph, (now, now))
        idle = session.idle_seconds(self.settings, meta, now=now + 5)
        self.assertAlmostEqual(idle, 5, delta=2)


class RunParsing(SettingsCase):
    def setUp(self):
        super().setUp()
        # `ab session start` refuses an unregistered project outright now (no implicit registration), and takes the
        # project's session lock before anything else -- so a parsing test needs a real project to parse at.
        self.project("p1")

    def reaches(self, argv):
        """Run `ab session start ...` far enough to capture what parsing resolved (agent, project, shell, passthrough)."""
        import types
        cap = {}

        class FakeSess:
            name = "abt-claude-p1-1"

            def prepare(self):
                pass

            def agent_command(self, shell=False, extra=()):
                cap.update(shell=shell, extra=list(extra))
                return ["c"]

            def run_captured(self, *a, **k):
                return types.SimpleNamespace(stdout="", stderr="", returncode=0)

            def run_interactive(self, *a, **k):
                return 0

        def fake_load(s, name, **kw):
            cap["project"] = name
            return types.SimpleNamespace(name=name)

        def fake_make(s, agent, project, **kw):
            cap["agent"] = agent
            return FakeSess()

        with unittest.mock.patch("agentbox.lifecycle.sessions", return_value={}), \
             unittest.mock.patch("agentbox.sessionlock.sessions", return_value={}), \
             unittest.mock.patch("agentbox.session.load_project", side_effect=fake_load), \
             unittest.mock.patch("agentbox.session.make_session", side_effect=fake_make):
            # `ab()` closes stdin (so the no-TTY branch reads "" instead of blocking on the terminal) and
            # puts a deadline on the call. The lock is real here: taken, noted, and released by the context
            # manager on the way out, since FakeSess has no teardown to do it.
            cap["rc"] = self.ab(*argv)[0]
        return cap

    def test_shell_flag_after_positionals(self):
        cap = self.reaches(["session", "start", "claude", "p1", "--shell"])
        self.assertEqual((cap["agent"], cap["project"]), ("claude", "p1"))
        self.assertTrue(cap["shell"])

    def test_passthrough_after_double_dash(self):
        cap = self.reaches(["session", "start", "claude", "p1", "--", "--resume", "x"])
        self.assertEqual(cap["extra"], ["--resume", "x"])
        self.assertFalse(cap["shell"])

    def test_shell_and_passthrough_together(self):
        cap = self.reaches(["session", "start", "claude", "p1", "--shell", "--", "-lc", "id"])
        self.assertTrue(cap["shell"])
        self.assertEqual(cap["extra"], ["-lc", "id"])

    def test_too_many_positionals_is_rejected(self):
        rc, out = self.ab("session", "start", "claude", "p1", "surplus")
        self.assertEqual(rc, 2, out)
        self.assertIn("unrecognized arguments", out)


class LsTest(SettingsCase):
    def rows(self, idle_hours):
        sd = self.settings.state_dir("p1", "claude")
        os.makedirs(sd, exist_ok=True)
        f = os.path.join(sd, "x")
        open(f, "w").close()
        t = time.time() - idle_hours * 3600
        os.utime(f, (t, t))
        os.utime(sd, (t, t))
        meta = {"project": "p1", "agent": "claude", "started": time.time() - 30 * 3600}
        return {"abt-s-1": {"meta": meta, "containers": [], "networks": [], "alive": True}}

    def run_ls(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli.main(["session", "ls"])
        return out.getvalue(), err.getvalue()

    def test_ls_shows_age_and_idle_and_flags_long_idle(self):
        with unittest.mock.patch("agentbox.session.sessions", return_value=self.rows(idle_hours=20)):
            out, err = self.run_ls()
        self.assertIn("age 1d6h", out)                 # 30h since start
        self.assertIn("idle", out)
        self.assertIn("<-- idle", out, "a 20h-idle session is flagged in the listing")
        self.assertIn("ab session kill p1", err,
                      "and warned about, naming the project (one per project) rather than the session id")

    def test_ls_does_not_flag_a_busy_session(self):
        with unittest.mock.patch("agentbox.session.sessions", return_value=self.rows(idle_hours=0)):
            out, err = self.run_ls()
        self.assertNotIn("<-- idle", out)
        self.assertEqual(err, "")


class RunGuardTest(SettingsCase):
    def setUp(self):
        super().setUp()
        self.project("p1")

    def test_run_is_refused_when_a_session_is_running_without_a_lock(self):
        """A running session that holds no lock file: one started before agentbox enforced the lock, or one
        whose lock was removed by hand. The lock is the mechanism; this is the backstop that stops it being
        handed out while that session is still writing the project's state."""
        import json
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(
            [{"Names": ["abt-old-1-agent"], "State": "running",
              "Labels": {"agentbox.project": "p1", "agentbox.session": "abt-old-1",
                         "agentbox.role": "agent", "agentbox-test": "1"}}])
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        rc, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(rc, 2)
        self.assertIn("abt-old-1", out)
        self.assertIn("holds no session lock", out)
        self.assertIn("ab session attach p1", out)
        from agentbox import projectlock
        self.assertIsNone(projectlock.read(self.settings, "p1"), "and it did not keep the lock it tried")

    def test_run_takes_positional_agent_and_project(self):
        seen = {}

        def fake_load(s, name, **kw):
            seen["project"] = name
            raise AgentboxError("stop here")

        with unittest.mock.patch("agentbox.session.load_project", side_effect=fake_load):
            with redirect_stderr(io.StringIO()):
                rc = cli.main(["session", "start", "claude", "p1"])
        self.assertEqual(rc, 2)
        self.assertEqual(seen["project"], "p1")

    def test_run_needs_both(self):
        rc, out = self.ab("session", "start", "claude")
        self.assertEqual(rc, 2, out)
        self.assertIn("project", out)

    def test_the_agent_is_a_choice(self):
        rc, out = self.ab("session", "start", "codex", "p1")
        self.assertEqual(rc, 2, out)
        self.assertIn("invalid choice", out)


class ProjectRmTest(SettingsCase):
    def test_rm_multiple(self):
        self.project("p1")
        self.project("p2")
        self.project("p3")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["project", "rm", "p1", "p2"]), 0)
        from agentbox import registry
        left = registry.load(self.settings.registry_path)
        self.assertEqual(set(left), {"p3"})

    def test_rm_is_all_or_nothing(self):
        self.project("p1")
        with redirect_stderr(io.StringIO()):
            rc = cli.main(["project", "rm", "p1", "nope"])
        self.assertEqual(rc, 2)
        from agentbox import registry
        self.assertIn("p1", registry.load(self.settings.registry_path), "a bad name removes none")

    def test_rm_deduplicates_repeated_names(self):
        # `ab project rm web web` used to del the same key twice -> KeyError traceback (audit 2026-10-15 #6).
        from agentbox import registry
        self.project("web")
        with redirect_stdout(io.StringIO()) as out:
            rc = cli.main(["project", "rm", "web", "web"])
        self.assertEqual(rc, 0)
        self.assertIn("removed project web", out.getvalue())
        self.assertNotIn("web", registry.load(self.settings.registry_path))


class BrokenConfigFiles(SettingsCase):
    """A malformed or mistyped config file is a refusal naming the file, exit 2, never a traceback (item 29).
    Every run goes through cli.main so the message is what the terminal would show."""

    def broken(self, *argv):
        code, out = self.ab(*argv)
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("ab: ", out)
        return out

    def test_malformed_agentbox_toml(self):
        path = os.path.join(self.cfg, "agentbox.toml")
        with open(path, "a") as f:
            f.write("this is = not toml = at all\n")
        self.assertIn(path, self.broken("project", "ls"))

    def test_unknown_setting_in_agentbox_toml(self):
        path = os.path.join(self.cfg, "agentbox.toml")
        with open(path, "a") as f:
            f.write('name_prefx = "typo"\n')
        out = self.broken("project", "ls")
        self.assertIn(path, out)
        self.assertIn("name_prefx", out)

    def test_malformed_projects_toml(self):
        with open(self.settings.registry_path, "w") as f:
            f.write("[project.p1\nroot = \n")
        self.assertIn(self.settings.registry_path, self.broken("project", "ls"))

    def test_a_registry_entry_that_is_not_a_table(self):
        with open(self.settings.registry_path, "w") as f:
            f.write('[project]\np1 = "/some/root"\n')
        out = self.broken("project", "ls")
        self.assertIn(self.settings.registry_path, out)
        self.assertIn("p1", out)

    def test_malformed_preset_file(self):
        os.makedirs(self.settings.presets_dir)
        path = os.path.join(self.settings.presets_dir, "mine.toml")
        with open(path, "w") as f:
            f.write("egress = \n")
        self.assertIn(path, self.broken("project", "ls"))


class ProjectNameIsValidatedBeforeRmtree(SettingsCase):
    """`ab project purge` and `ab project clean` work on registered-or-not names, so they take the name
    straight to a path under projects/. The name must still be a project name: `../x` would otherwise walk
    out of the data dir into an rmtree (item 14)."""

    def setUp(self):
        super().setUp()
        # Outside the data dir: a stray directory INSIDE it would be taken for a flat-layout project and moved
        # into projects/ by layout.migrate before the command runs.
        self.victim = self.mkdir("victim")
        self.write("victim/keep.txt", "k\n")

    def test_purge_refuses_a_name_that_is_not_a_project_name(self):
        for bad in ("../../victim", "../../victim/", "a/b", ".", "..", "/tmp", "UPPER"):
            with self.subTest(name=bad):
                code, out = self.ab("project", "purge", bad, "--yes")
                self.assertEqual(code, 2, out)
                self.assertIn("project name", out)
        self.assertTrue(os.path.exists(os.path.join(self.victim, "keep.txt")))

    def test_clean_refuses_a_name_that_is_not_a_project_name(self):
        code, out = self.ab("project", "clean", "../../victim", "--yes")
        self.assertEqual(code, 2, out)
        self.assertIn("project name", out)
        self.assertTrue(os.path.exists(os.path.join(self.victim, "keep.txt")))

    def test_purge_still_works_for_an_unregistered_project(self):
        self.write("data/projects/gone/claude/CLAUDE.md", "m\n")
        code, out = self.ab("project", "purge", "gone", "--yes")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.settings.project_dir("gone")))


class PromoteRefusesLinks(SettingsCase):
    """An inbox item is agent-written. A symlink in it (or the item being one) would be followed by the copy
    into the profile, which every session of every project then loads (item 4). promote refuses before it
    copies anything, and harvest marks the item so the refusal is no surprise."""

    def setUp(self):
        super().setUp()
        from agentbox import profile
        self.prof = self.mkdir("config", "claude", "profile")
        self.inbox = self.settings.inbox_dir("p1", "claude")
        os.makedirs(os.path.join(self.inbox, "deploy"))
        with open(os.path.join(self.inbox, "deploy", "SKILL.md"), "w") as f:
            f.write("# deploy\n")
        self.secret = self.write("elsewhere/id_ed25519", "PRIVATE\n")
        profile.write_sidecar(self.inbox, "deploy", {"project": "p1", "agent": "claude", "added": "2026-11-16T10:00:00"})

    def test_a_link_inside_the_item_refuses_the_promotion_and_copies_nothing(self):
        os.symlink(self.secret, os.path.join(self.inbox, "deploy", "key"))
        code, out = self.ab("profile", "promote", "p1/deploy", "--as", "skills/deploy")
        self.assertEqual(code, 2, out)
        self.assertIn("symlink", out)
        self.assertFalse(os.path.lexists(os.path.join(self.prof, "skills", "deploy")), "nothing copied")

    def test_an_item_that_is_itself_a_link_is_refused(self):
        os.symlink(self.secret, os.path.join(self.inbox, "note.md"))
        code, out = self.ab("profile", "promote", "p1/note.md", "--as", "rules/note.md")
        self.assertEqual(code, 2, out)
        self.assertIn("symlink", out)
        self.assertFalse(os.path.lexists(os.path.join(self.prof, "rules", "note.md")))

    def test_harvest_flags_an_item_that_holds_a_link(self):
        os.symlink(self.secret, os.path.join(self.inbox, "deploy", "key"))
        code, out = self.ab("profile", "harvest", "--agent", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("SYMLINK", out)

    def test_a_plain_item_still_promotes(self):
        code, out = self.ab("profile", "promote", "p1/deploy", "--as", "skills/deploy")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.prof, "skills", "deploy", "SKILL.md")))


class FailuresLeaveNothingHalfDone(SettingsCase):
    """Item 47: a prompt with no stdin, a first promotion into a profile that does not exist yet, and a
    registration that fails after the scaffold each end cleanly, with nothing left to trip over."""

    def test_reset_state_with_no_stdin_is_not_reset(self):
        self.project("p1")
        st = self.settings.state_dir("p1", "claude")
        os.makedirs(st)
        self.write(os.path.join(st, "CLAUDE.md"), "x\n")
        code, out = self.ab("project", "reset-state", "p1", stdin="")
        self.assertEqual(code, 1, out)
        self.assertIn("not reset", out)
        self.assertNotIn("Traceback", out)
        self.assertTrue(os.path.isfile(os.path.join(st, "CLAUDE.md")))

    def test_first_promotion_creates_a_versioned_profile(self):
        from agentbox import profile
        inbox = self.settings.inbox_dir("p1", "claude")
        os.makedirs(os.path.join(inbox, "deploy"))
        self.write(os.path.join(inbox, "deploy", "SKILL.md"), "# d\n")
        profile.write_sidecar(inbox, "deploy", {"project": "p1", "agent": "claude"})
        prof = self.settings.profile_src("claude")
        self.assertFalse(os.path.exists(prof))
        code, out = self.ab("profile", "promote", "p1/deploy", "--as", "skills/deploy")
        self.assertEqual(code, 0, out)
        self.assertTrue(profile.commit_id(prof), "the promotion is a commit")
        self.assertFalse(profile.commit_state(prof)[1], "nothing left uncommitted")

    def test_a_failed_commit_removes_the_copy(self):
        from unittest import mock
        from agentbox import profile
        from agentbox.errors import AgentboxError as E
        inbox = self.settings.inbox_dir("p1", "claude")
        os.makedirs(os.path.join(inbox, "deploy"))
        self.write(os.path.join(inbox, "deploy", "SKILL.md"), "# d\n")
        profile.write_sidecar(inbox, "deploy", {"project": "p1", "agent": "claude"})
        prof = self.mkdir("config", "claude", "profile")
        real = profile._git

        def failing(src, *args, **kw):
            if "commit" in args and "-qm" in args and "profile: add" in args[-1]:
                raise E("git commit failed (simulated)")
            return real(src, *args, **kw)
        with mock.patch.object(profile, "_git", failing):
            code, out = self.ab("profile", "promote", "p1/deploy", "--as", "skills/deploy")
        self.assertEqual(code, 2, out)
        self.assertFalse(os.path.lexists(os.path.join(prof, "skills", "deploy")), "the copy is removed")


class UsageReferenceCoversEveryCommand(SettingsCase):
    """docs/USAGE.md is generated from docs/gen_usage.py's GROUPS; a verb missing there is missing from the
    reference (project clean/sync and profile install were, item 48)."""

    def test_every_subparser_choice_is_in_groups(self):
        import argparse
        import importlib.util
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        spec = importlib.util.spec_from_file_location("gen_usage", os.path.join(here, "docs", "gen_usage.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        listed = {tuple(argv) for _section, cmds in gen.GROUPS for _title, argv in cmds}

        def walk(p, prefix):
            for action in p._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, sub in action.choices.items():
                        yield prefix + (name,)
                        yield from walk(sub, prefix + (name,))
        missing = sorted(" ".join(c) for c in walk(cli.parser(self.settings), ()) if c not in listed)
        self.assertEqual(missing, [], "add these to docs/gen_usage.py GROUPS and regenerate docs/USAGE.md")


if __name__ == "__main__":
    import unittest
    unittest.main()


class HelpIsOrderedForPeople(SettingsCase):
    """`--help` lists the everyday commands first, in the order a user meets them, and keeps the host-setup
    and repair ones in an `advanced` section underneath (10 Nov). argparse lists subcommands in insertion
    order, so the order is the parser's; the advanced ones are taken out of the listed block and still parse."""

    def block(self, out):
        """The listed-commands block of a help text: between the positional header and `options:`."""
        return out.split("options:")[0]

    def test_top_level_everyday_then_advanced(self):
        code, out = self.ab("--help")
        self.assertEqual(code, 0, out)
        listed = self.block(out)
        order = [listed.index(f"\n    {n} ") for n in ("project", "session", "screen")]
        self.assertEqual(order, sorted(order), "project, session, screen, in that order")
        for n in ("profile", "image", "keys"):
            self.assertNotIn(f"\n    {n} ", listed, f"{n} is advanced, not in the everyday block")
        adv = out.split("advanced commands")[1]
        for n in ("profile", "image", "keys"):
            self.assertIn(f"\n  {n} ", adv)
        self.assertNotIn("SUPPRESS", out)

    def test_project_verbs_in_the_order_a_user_meets_them(self):
        code, out = self.ab("project", "--help")
        self.assertEqual(code, 0, out)
        listed = self.block(out)
        everyday = ["init", "add", "ls", "show", "set", "rm"]
        pos = [listed.index(f"\n    {n} ") for n in everyday]
        self.assertEqual(pos, sorted(pos), everyday)
        adv = out.split("advanced verbs")[1]
        for n in ("check", "selftest", "accept-grant-exposure", "accept-history-secrets", "reset-state", "purge"):
            self.assertNotIn(f"\n    {n} ", listed, n)
            self.assertIn(f"\n  {n} ", adv, n)

    def test_session_and_screen_verbs(self):
        code, out = self.ab("session", "--help")
        listed = self.block(out)
        pos = [listed.index(f"\n    {n} ") for n in ("start", "attach", "ls", "kill")]
        self.assertEqual(pos, sorted(pos))
        self.assertIn("preview", out.split("advanced verbs")[1])
        code, out = self.ab("screen", "--help")
        self.assertIn("layouts", out.split("advanced verbs")[1])
        self.assertNotIn("\n    layouts ", self.block(out))

    def test_advanced_commands_still_parse(self):
        for argv in (("project", "purge", "--help"), ("project", "check", "--help"), ("session", "gc", "--help"),
                     ("screen", "config"), ("keys", "ls"), ("profile", "harvest", "--help")):
            with self.subTest(argv=argv):
                code, out = self.ab(*argv)
                self.assertEqual(code, 0, out)
