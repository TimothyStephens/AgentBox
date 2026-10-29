"""The host-owned agent profile and the inbox (`agentbox/profile.py`).

`~/.config/agentbox/<agent>/profile/` holds what every session LOADS -- skills, sub-agents, commands,
instructions -- and only the host writes it. A session gets a read-only snapshot of it, taken at start, so:

  * it is trusted, being host-owned, and loads under every preset including lockdown, where a repo's own
    `.claude`/`.omp` are covered empty;
  * a session cannot change what later sessions load: those paths are read-only, and what the agent wants
    to keep goes to the project's `inbox/`, which nothing loads;
  * what a session loaded is pinned by the snapshot, so editing the host profile mid-session cannot reach a
    running one, and the hash plus the profile's git commit go in the session log.
"""
import json
import os
import subprocess
import unittest

from agentbox import cli, profile, registry, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, TempCase


class TreeHash(TempCase):
    def setUp(self):
        super().setUp()
        self.src = self.mkdir("profile")
        self.write("profile/skills/deploy/SKILL.md", "---\nname: deploy\n---\nrun make deploy\n")
        self.write("profile/CLAUDE.md", "Write tests first.\n")

    def test_the_same_content_hashes_the_same(self):
        first = profile.tree_hash(self.src)
        self.assertTrue(first)
        self.assertEqual(first, profile.tree_hash(self.src))

    def test_mtime_alone_does_not_change_it(self):
        """Re-saving a file with the same bytes must not invalidate the snapshot -- the hash is content."""
        first = profile.tree_hash(self.src)
        p = os.path.join(self.src, "CLAUDE.md")
        os.utime(p, (1, 1))
        self.assertEqual(profile.tree_hash(self.src), first)

    def test_content_name_and_the_exec_bit_each_change_it(self):
        first = profile.tree_hash(self.src)
        self.write("profile/CLAUDE.md", "Write tests first, always.\n")
        second = profile.tree_hash(self.src)
        self.assertNotEqual(second, first)
        os.rename(os.path.join(self.src, "CLAUDE.md"), os.path.join(self.src, "AGENTS.md"))
        third = profile.tree_hash(self.src)
        self.assertNotIn(third, (first, second))
        os.chmod(os.path.join(self.src, "AGENTS.md"), 0o755)
        self.assertNotEqual(profile.tree_hash(self.src), third)

    def test_a_git_commit_does_not_change_it(self):
        """The profile is a git repo; .git churns on every commit and must stay out of the hash."""
        first = profile.tree_hash(self.src)
        profile.ensure_repo(self.src)
        self.assertTrue(os.path.isdir(os.path.join(self.src, ".git")))
        self.assertEqual(profile.tree_hash(self.src), first)

    def test_a_symlink_anywhere_refuses(self):
        """A link would be FOLLOWED into the snapshot and mounted into every session: `~/.ssh/id_ed25519`
        would be one `ln -s` from the sandbox."""
        secret = self.write("outside.key", "PRIVATE\n")
        os.symlink(secret, os.path.join(self.src, "skills", "link.md"))
        with self.assertRaises(AgentboxError) as cm:
            profile.tree_hash(self.src)
        self.assertIn("symlink", str(cm.exception))
        os.unlink(os.path.join(self.src, "skills", "link.md"))
        os.symlink(self.mkdir("elsewhere"), os.path.join(self.src, "skills", "linkdir"))
        with self.assertRaises(AgentboxError):
            profile.tree_hash(self.src)

    def test_no_profile_is_not_an_error(self):
        self.assertEqual(profile.tree_hash(os.path.join(self.tmp, "nope")), "")


class Snapshot(SettingsCase):
    def setUp(self):
        super().setUp()
        self.src = self.settings.profile_src("claude")
        self.write("config/claude/profile/skills/deploy/SKILL.md", "run make deploy\n")

    def test_the_snapshot_is_the_hash_and_holds_the_content(self):
        dir, digest = profile.snapshot(self.settings, "claude")
        self.assertEqual(os.path.basename(dir), digest)
        self.assertEqual(open(os.path.join(dir, "skills", "deploy", "SKILL.md")).read(),
                         "run make deploy\n")

    def test_an_unchanged_profile_reuses_the_snapshot(self):
        dir, digest = profile.snapshot(self.settings, "claude")
        marker = os.path.join(dir, "skills", "deploy", "SKILL.md")
        before = os.stat(marker).st_ino
        again, again_digest = profile.snapshot(self.settings, "claude")
        self.assertEqual((again, again_digest), (dir, digest))
        self.assertEqual(os.stat(marker).st_ino, before, "not re-copied")
        self.assertEqual(os.listdir(os.path.join(self.settings.profiles_dir, "claude")), [digest])

    def test_a_changed_profile_makes_a_second_snapshot_and_keeps_the_first(self):
        old_dir, old = profile.snapshot(self.settings, "claude")
        self.write("config/claude/profile/skills/deploy/SKILL.md", "run make deploy --prod\n")
        new_dir, new = profile.snapshot(self.settings, "claude")
        self.assertNotEqual(new, old)
        self.assertTrue(os.path.isdir(old_dir), "a running session still mounts the old one")
        self.assertEqual(sorted(os.listdir(os.path.join(self.settings.profiles_dir, "claude"))),
                         sorted([old, new]))

    def test_no_temporary_directory_is_left_behind(self):
        profile.snapshot(self.settings, "claude")
        self.assertFalse([n for n in os.listdir(os.path.join(self.settings.profiles_dir, "claude"))
                          if n.startswith(".")])

    def test_an_empty_profile_dir_loads_nothing(self):
        import shutil
        shutil.rmtree(self.src)
        os.makedirs(self.src)
        self.assertEqual(profile.snapshot(self.settings, "claude"), (None, ""))


class LegacySeedMigration(SettingsCase):
    """The seed used to be `~/.config/agentbox/claude/{settings.json,CLAUDE.md,commands/}`. It moves down
    into `profile/` once, explicitly: if the loader also read the old place, a skill could live in two
    directories with nothing to say which one a session used."""

    def test_the_seed_moves_into_the_profile_and_says_so(self):
        self.write("config/claude/CLAUDE.md", "house style\n")
        self.write("config/claude/settings.json", '{"theme": "dark"}\n')
        self.write("config/claude/commands/ship.md", "ship it\n")
        said = []
        moved = profile.migrate_legacy_seed(self.settings, "claude", out=said.append)
        self.assertEqual(sorted(moved), ["CLAUDE.md", "commands", "settings.json"])
        src = self.settings.profile_src("claude")
        self.assertEqual(open(os.path.join(src, "CLAUDE.md")).read(), "house style\n")
        self.assertEqual(open(os.path.join(src, "commands", "ship.md")).read(), "ship it\n")
        self.assertFalse(os.path.exists(os.path.join(self.settings.config_dir, "claude", "CLAUDE.md")),
                         "moved, not copied: there is one place, not two")
        self.assertTrue(any("profile" in m for m in said), said)

    def test_it_never_overwrites_and_reports_the_clash(self):
        self.write("config/claude/CLAUDE.md", "old\n")
        self.write("config/claude/profile/CLAUDE.md", "new\n")
        said = []
        self.assertEqual(profile.migrate_legacy_seed(self.settings, "claude", out=said.append), [])
        self.assertEqual(open(os.path.join(self.settings.profile_src("claude"), "CLAUDE.md")).read(), "new\n")
        self.assertTrue(any("BOTH" in m for m in said), said)

    def test_omps_own_seeds_are_left_alone(self):
        """models.yml and mcp.json are not profile items: they are read from the seed dir by name."""
        self.write("config/omp/models.yml", "providers: {}\n")
        self.write("config/omp/mcp.json", "{}\n")
        self.assertEqual(profile.migrate_legacy_seed(self.settings, "omp"), [])
        self.assertTrue(os.path.isfile(os.path.join(self.settings.config_dir, "omp", "models.yml")))


class SessionLoadsTheProfile(SettingsCase):
    def setUp(self):
        super().setUp()
        self.token()
        self.root = self.project("p1", preset="script")
        self.write("config/claude/profile/skills/deploy/SKILL.md", "run make deploy\n")
        self.write("config/claude/profile/CLAUDE.md", "Write tests first.\n")
        self.write("config/claude/profile/settings.json", '{"theme": "dark"}\n')

    def start(self, name="p1"):
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, name),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s, warnings

    def mounts(self, s):
        return {m.split("dst=")[1].split(",")[0]: m for m in s.run_args(["true"]) if "--mount=" in m}

    def test_skills_are_mounted_read_only_from_the_snapshot(self):
        s, _w = self.start()
        arg = self.mounts(s)["/run/agentbox/claude/skills"]
        self.assertTrue(arg.endswith(",ro"), arg)
        self.assertIn(os.path.join(self.settings.profiles_dir, "claude", s.profile.hash), arg)

    def test_the_instructions_are_generated_notes_first_then_the_profile(self):
        s, _w = self.start()
        arg = self.mounts(s)["/run/agentbox/claude/CLAUDE.md"]
        self.assertTrue(arg.endswith(",ro"), arg)
        text = open(arg.split("src=")[1].split(",")[0]).read()
        self.assertLess(text.index("agentbox sandbox"), text.index("Write tests first."))

    def test_the_inbox_is_mounted_read_write_and_is_not_a_load_path(self):
        s, _w = self.start()
        arg = self.mounts(s)["/run/agentbox/claude/inbox"]
        self.assertFalse(arg.endswith(",ro"), arg)
        self.assertNotIn("/run/agentbox/claude/inbox", s.adapter.profile_mounts.values())

    def test_settings_json_is_copied_writable_not_mounted(self):
        """Claude Code writes settings.json itself, so a read-only one would fail at every launch."""
        s, _w = self.start()
        self.assertNotIn("/run/agentbox/claude/settings.json", self.mounts(s))
        state = os.path.join(self.settings.state_dir("p1", "claude"), "settings.json")
        self.assertEqual(json.load(open(state))["theme"], "dark")

    def test_the_notes_tell_the_agent_the_paths_are_read_only_and_where_to_write(self):
        s, _w = self.start()
        text = open(self.mounts(s)["/run/agentbox/claude/CLAUDE.md"].split("src=")[1].split(",")[0]).read()
        self.assertIn("read-only", text)
        self.assertIn("/run/agentbox/claude/inbox", text)
        self.assertIn("ab profile harvest", text)

    def test_the_hash_commit_and_dirty_state_are_in_the_session_log(self):
        """Three different questions: what the session loaded (hash), which reviewed state it came from
        (commit), and whether the two had drifted (dirty) -- the last being how you learn that a session
        loaded something in nobody's history."""
        s, _w = self.start()
        log = open(s.log_path("profile")).read()
        self.assertIn(f"hash={s.profile.hash}", log)
        self.assertRegex(log, r"commit=[0-9a-f]{7,} dirty=no")
        self.assertIn("loaded=skills", log)

    def test_an_uncommitted_profile_edit_is_recorded_as_dirty(self):
        self.start()                                     # the first session commits the profile repo
        self.write("config/claude/profile/skills/deploy/SKILL.md", "edited but not committed\n")
        s, warnings = self.start()
        log = open(s.log_path("profile")).read()
        self.assertIn("dirty=yes", log)
        self.assertTrue(s.profile.dirty)
        self.assertNotEqual(s.profile.hash, "", "and the hash moved with the edit")
        self.assertTrue(any("uncommitted changes" in w for w in warnings), warnings)

    def test_a_profile_with_no_commit_yet_is_dirty_not_crashed(self):
        """`ensure_repo` commits what it finds, so this is the odd case of a hand-made empty repo."""
        import shutil
        src = self.settings.profile_src("claude")
        shutil.rmtree(os.path.join(src, ".git"), ignore_errors=True)
        subprocess.run(["git", "-C", src, "init", "-q"], check=True)
        commit, dirty = profile.commit_state(src)
        self.assertEqual(commit, "")
        self.assertTrue(dirty)

    def test_a_second_project_loads_the_same_profile(self):
        self.project("p2", preset="script")
        a, _w = self.start("p1")
        b, _w2 = self.start("p2")
        self.assertEqual(a.profile.hash, b.profile.hash)
        self.assertEqual(a.profile.dir, b.profile.dir)

    def test_lockdown_loads_it_too(self):
        """A repo's own .claude is covered under lockdown; the host-owned profile is not repo-shipped code,
        so it is exactly what should still load there."""
        self.project("locked", preset="lockdown")
        s, _w = self.start("locked")
        self.assertTrue(s.profile.hash)
        self.assertTrue(self.mounts(s)["/run/agentbox/claude/skills"].endswith(",ro"))

    def test_editing_the_host_profile_cannot_reach_a_running_session(self):
        s, _w = self.start()
        mounted = self.mounts(s)["/run/agentbox/claude/skills"].split("src=")[1].split(",")[0]
        self.write("config/claude/profile/skills/deploy/SKILL.md", "rm -rf /\n")
        self.assertEqual(open(os.path.join(mounted, "deploy", "SKILL.md")).read(), "run make deploy\n")

    def test_a_symlinked_profile_refuses_the_session(self):
        os.symlink(self.write("outside.key", "PRIVATE\n"),
                   os.path.join(self.settings.profile_src("claude"), "skills", "link.md"))
        with self.assertRaises(AgentboxError) as cm:
            self.start()
        self.assertIn("symlink", str(cm.exception))


class StateMigratedToInbox(SettingsCase):
    """Skills and sub-agents that earlier sessions accumulated in the per-project state would now sit under
    a read-only mount, invisible. They are moved to the inbox once -- nothing is deleted."""

    def setUp(self):
        super().setUp()
        self.token()
        self.root = self.project("p1", preset="script")
        self.write("config/claude/profile/skills/deploy/SKILL.md", "run make deploy\n")
        self.state = self.settings.state_dir("p1", "claude")

    def start(self):
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s, warnings

    def test_old_skills_and_agents_move_and_are_announced(self):
        os.makedirs(os.path.join(self.state, "skills", "mine"))
        self.write("data/p1/claude/skills/mine/SKILL.md", "my own skill\n")
        self.write("data/p1/claude/CLAUDE.md", "notes an earlier session wrote\n")
        _s, warnings = self.start()
        inbox = self.settings.inbox_dir("p1", "claude")
        self.assertEqual(sorted(profile.items(inbox)), ["from-state-CLAUDE.md", "from-state-skills"])
        self.assertEqual(open(os.path.join(inbox, "from-state-skills", "mine", "SKILL.md")).read(),
                         "my own skill\n")
        self.assertFalse(os.path.exists(os.path.join(self.state, "skills")))
        self.assertTrue(any("moved" in w and "inbox" in w for w in warnings), warnings)

    def test_a_state_claude_md_that_only_copies_the_seed_is_removed_not_queued(self):
        """An inbox full of copies of the seed is noise, and noise is what stops a queue being read. The
        state file `seed_state` used to write is the block plus the seed, so on most projects it holds
        nothing of its own."""
        from agentbox.adapters import claude
        self.write("config/claude/profile/CLAUDE.md", "house style\n")
        block = f"{claude.NOTES_BEGIN}\nold notes\n{claude.NOTES_END}\n"
        self.write("data/p1/claude/CLAUDE.md", block + "\nhouse style\n")
        _s, warnings = self.start()
        self.assertEqual(profile.items(self.settings.inbox_dir("p1", "claude")), [],
                         "nothing to review was queued")
        self.assertFalse(os.path.exists(os.path.join(self.state, "CLAUDE.md")))
        self.assertTrue(any("removed" in w and "copy of what the profile already holds" in w
                            for w in warnings), warnings)

    def test_a_state_claude_md_with_anything_of_its_own_is_queued(self):
        from agentbox.adapters import claude
        self.write("config/claude/profile/CLAUDE.md", "house style\n")
        block = f"{claude.NOTES_BEGIN}\nold notes\n{claude.NOTES_END}\n"
        self.write("data/p1/claude/CLAUDE.md", block + "\nhouse style\nand one more rule\n")
        _s, warnings = self.start()
        inbox = self.settings.inbox_dir("p1", "claude")
        self.assertEqual(profile.items(inbox), ["from-state-CLAUDE.md"])
        self.assertIn("one more rule", open(os.path.join(inbox, "from-state-CLAUDE.md")).read())

    def test_a_state_claude_md_against_no_profile_one_is_queued(self):
        """Nothing in the profile to be a copy OF, so the content is the user's and goes to review."""
        self.write("data/p1/claude/CLAUDE.md", "notes with no profile counterpart\n")
        self.start()
        self.assertEqual(profile.items(self.settings.inbox_dir("p1", "claude")), ["from-state-CLAUDE.md"])

    def test_an_empty_state_dir_is_not_moved(self):
        os.makedirs(os.path.join(self.state, "skills"), exist_ok=True)
        self.start()
        self.assertEqual(profile.items(self.settings.inbox_dir("p1", "claude")), [])

    def test_nothing_to_migrate_is_silent(self):
        _s, warnings = self.start()
        self.assertFalse([w for w in warnings if "moved" in w], warnings)


class LockdownKeepsNothing(SettingsCase):
    """Lockdown's contract is that the session leaves nothing behind (decisions.md, 2 Oct). Its inbox lives
    inside the ephemeral state and goes with it, and the agent is told so -- otherwise it would write a
    skill believing someone would read it."""

    def setUp(self):
        super().setUp()
        self.token()
        self.root = self.project("locked", preset="lockdown")
        self.write("config/claude/profile/skills/deploy/SKILL.md", "run make deploy\n")

    def start(self):
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "locked"),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        return s, warnings

    def test_the_inbox_is_inside_the_ephemeral_state(self):
        s, _w = self.start()
        self.addCleanup(s.teardown)
        self.assertTrue(s.inbox_is_ephemeral)
        self.assertIn(os.path.join("ephemeral", s.name), s.inbox)
        self.assertEqual(s.inbox, os.path.join(s.state_dir, "inbox"))
        self.assertFalse(os.path.exists(self.settings.inbox_dir("locked", "claude")),
                         "no persistent inbox is created for a lockdown project")

    def test_it_is_deleted_with_the_rest_of_the_state(self):
        s, _w = self.start()
        os.makedirs(os.path.join(s.inbox, "learned"), exist_ok=True)
        with open(os.path.join(s.inbox, "learned", "SKILL.md"), "w") as f:
            f.write("will not survive\n")
        inbox = s.inbox
        s.teardown()
        self.assertFalse(os.path.exists(inbox), inbox)
        self.assertFalse(os.path.exists(os.path.join(self.settings.data_dir, "ephemeral", s.name)))

    def test_the_agent_is_told_the_session_keeps_nothing(self):
        s, _w = self.start()
        self.addCleanup(s.teardown)
        path = [m for m in s.run_args(["true"]) if "CLAUDE.md" in m][0].split("src=")[1].split(",")[0]
        text = open(path).read()
        self.assertIn("keeps nothing", text)
        self.assertNotIn("per project). Nothing there is loaded", text, "not the persistent wording")

    def test_teardown_says_what_is_being_deleted(self):
        s, warnings = self.start()
        os.makedirs(os.path.join(s.inbox, "learned"), exist_ok=True)
        with open(os.path.join(s.inbox, "learned", "SKILL.md"), "w") as f:
            f.write("x\n")
        s.teardown()
        self.assertTrue(any("DELETED" in w and "learned" in w for w in warnings), warnings)
        self.assertFalse(any("ab profile harvest" in w for w in warnings),
                         "nothing is queued, so nothing is promised")

    def test_no_provenance_sidecar_is_written(self):
        s, _w = self.start()
        os.makedirs(os.path.join(s.inbox, "learned"), exist_ok=True)
        with open(os.path.join(s.inbox, "learned", "SKILL.md"), "w") as f:
            f.write("x\n")
        kept = os.path.join(self.settings.data_dir, "ephemeral")
        s.teardown()
        self.assertFalse(os.path.exists(kept) and os.listdir(kept), "nothing is left to carry provenance")

    def test_a_normal_preset_still_has_a_persistent_inbox(self):
        self.project("p1", preset="script")
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        self.assertFalse(s.inbox_is_ephemeral)
        self.assertEqual(s.inbox, self.settings.inbox_dir("p1", "claude"))


class InboxProvenance(SettingsCase):
    def setUp(self):
        super().setUp()
        self.token()
        self.root = self.project("p1", preset="research")       # research declares open egress
        self.write("config/claude/profile/skills/deploy/SKILL.md", "x\n")
        self.inbox = self.settings.inbox_dir("p1", "claude")

    def session_that_writes(self, name="learned-skill"):
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        os.makedirs(os.path.join(self.inbox, name), exist_ok=True)
        with open(os.path.join(self.inbox, name, "SKILL.md"), "w") as f:
            f.write("something I learned\n")
        s.teardown()
        return s, warnings

    def test_a_new_item_gets_a_sidecar_and_is_reported(self):
        s, warnings = self.session_that_writes()
        doc = profile.read_sidecar(self.inbox, "learned-skill")
        self.assertEqual((doc["project"], doc["agent"], doc["preset"]), ("p1", "claude", "research"))
        self.assertEqual(doc["session"], s.name)
        self.assertEqual(doc["reviewed"], False)
        self.assertRegex(doc["added"], r"^\d{4}-\d\d-\d\dT")
        self.assertIn(doc["egress"], ("open", "packages"))
        self.assertTrue(any("1 new item" in w for w in warnings), warnings)

    def test_an_item_that_was_already_there_is_not_reported_again(self):
        self.session_that_writes("first")
        _s, warnings = self.session_that_writes("second")
        self.assertTrue(any("second" in w for w in warnings), warnings)
        self.assertFalse(any("first" in w for w in warnings), warnings)
        self.assertEqual(profile.read_sidecar(self.inbox, "first")["session"],
                         profile.read_sidecar(self.inbox, "second")["session"].replace("-2", "-1"))

    def test_nothing_new_says_nothing(self):
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        s.teardown()
        self.assertFalse([w for w in warnings if "new item" in w], warnings)


class Harvest(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("p1", preset="script")
        self.write("config/claude/profile/skills/keep/SKILL.md", "kept\n")

    def put(self, project, name, text="learned\n", **prov):
        inbox = self.settings.inbox_dir(project, "claude")
        os.makedirs(os.path.join(inbox, name), exist_ok=True)
        with open(os.path.join(inbox, name, "SKILL.md"), "w") as f:
            f.write(text)
        doc = {"project": project, "agent": "claude", "session": f"ab-claude-{project}-1",
               "preset": "script", "egress": "packages", "added": "2026-10-29T10:00:00", "reviewed": False}
        doc.update(prov)
        profile.write_sidecar(inbox, name, doc)
        return os.path.join(inbox, name)

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()

    def test_harvest_is_read_only_and_lists_unreviewed_items(self):
        self.put("p1", "deploy")
        before = profile.tree_hash(self.settings.profile_src("claude"))
        code, out = self.run_cli("profile", "harvest", "--agent", "claude")
        self.assertEqual(code, 0)
        self.assertIn("deploy", out)
        self.assertIn("1 unreviewed", out)
        self.assertEqual(profile.tree_hash(self.settings.profile_src("claude")), before,
                         "harvest must not touch the profile")

    def test_an_item_from_a_removed_project_is_still_listed(self):
        self.put("gone", "orphan")
        code, out = self.run_cli("profile", "harvest", "--agent", "claude")
        self.assertIn("orphan", out)
        self.assertIn("project removed", out)

    def test_an_open_egress_item_is_marked(self):
        self.put("p1", "fromweb", egress="open")
        _code, out = self.run_cli("profile", "harvest", "--agent", "claude")
        self.assertIn("OPEN-EGRESS", out)

    def test_identical_items_group_and_same_name_different_content_is_flagged(self):
        self.put("p1", "dup", "same\n")
        self.put("p2", "dup", "same\n")
        self.put("p3", "dup", "DIFFERENT\n")
        _code, out = self.run_cli("profile", "harvest", "--agent", "claude")
        self.assertIn("identical in 2 projects", out)
        self.assertIn("DIFFERENT content elsewhere", out)

    def test_promote_copies_into_the_profile_and_commits_with_provenance(self):
        self.put("p1", "deploy", egress="open")
        code, out = self.run_cli("profile", "promote", "p1/deploy", "--as", "skills/deploy")
        self.assertEqual(code, 0, out)
        dst = os.path.join(self.settings.profile_src("claude"), "skills", "deploy", "SKILL.md")
        self.assertEqual(open(dst).read(), "learned\n")
        log = subprocess.run(["git", "-C", self.settings.profile_src("claude"), "log", "-1", "--format=%B"],
                             capture_output=True, text=True).stdout
        self.assertIn("promoted from p1/deploy", log)
        self.assertIn("egress: open", log)
        self.assertIn("session: ab-claude-p1-1", log)
        doc = profile.read_sidecar(self.settings.inbox_dir("p1", "claude"), "deploy")
        self.assertTrue(doc["reviewed"])
        self.assertEqual(doc["promoted_to"], "skills/deploy")
        self.assertTrue(os.path.isdir(os.path.join(self.settings.inbox_dir("p1", "claude"), "deploy")),
                        "the inbox copy is kept")

    def test_promote_refuses_to_overwrite_or_to_escape(self):
        self.put("p1", "deploy")
        self.run_cli("profile", "promote", "p1/deploy", "--as", "skills/deploy")
        self.assertEqual(self.run_cli("profile", "promote", "p1/deploy", "--as", "skills/deploy")[0], 2)
        self.assertEqual(self.run_cli("profile", "promote", "p1/deploy", "--as", "../escape")[0], 2)
        self.assertEqual(self.run_cli("profile", "promote", "p1/deploy", "--as", "/etc/x")[0], 2)

    def test_promote_without_as_works_only_for_a_plain_skill_directory(self):
        self.put("p1", "deploy")            # holds SKILL.md
        self.assertEqual(self.run_cli("profile", "promote", "p1/deploy")[0], 0)
        inbox = self.settings.inbox_dir("p1", "claude")
        with open(os.path.join(inbox, "note.md"), "w") as f:
            f.write("just a note\n")
        code, out = self.run_cli("profile", "promote", "p1/note.md")
        self.assertEqual(code, 2)

    def test_an_ambiguous_name_is_refused(self):
        self.put("p1", "dup", "a\n")
        self.put("p2", "dup", "b\n")
        code, out = self.run_cli("profile", "promote", "dup", "--as", "skills/dup")
        self.assertEqual(code, 2)

    def test_dismiss_marks_reviewed_and_deletes_nothing(self):
        path = self.put("p1", "nope")
        code, _out = self.run_cli("profile", "dismiss", "p1/nope")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(profile.read_sidecar(self.settings.inbox_dir("p1", "claude"), "nope")["reviewed"])
        _c, out = self.run_cli("profile", "harvest", "--agent", "claude")
        self.assertIn("nothing unreviewed", out)
        _c, out = self.run_cli("profile", "harvest", "--agent", "claude", "--all")
        self.assertIn("nope", out)


class RmAddAndPurge(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("p1", preset="script")
        inbox = self.settings.inbox_dir("p1", "claude")
        os.makedirs(inbox)
        with open(os.path.join(inbox, "learned.md"), "w") as f:
            f.write("x\n")

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()

    def test_rm_keeps_the_state_and_says_what_stays(self):
        code, out = self.run_cli("project", "rm", "p1")
        self.assertEqual(code, 0, out)
        self.assertIn("untouched", out)
        self.assertIn("unreviewed inbox item", out)
        self.assertIn("ab project purge p1", out)
        self.assertTrue(os.path.isdir(self.settings.inbox_dir("p1", "claude")))

    def test_re_adding_reports_the_state_it_reattaches(self):
        self.run_cli("project", "rm", "p1")
        code, out = self.run_cli("project", "add", "p1", self.root, "--preset", "script")
        self.assertEqual(code, 0, out)
        self.assertIn("reattached existing agent state", out)
        self.assertIn("inbox 1 item", out)
        self.assertIn("reset-state", out)

    def test_a_fresh_project_says_nothing_about_state(self):
        root = self.mkdir("fresh")
        _code, out = self.run_cli("project", "add", "fresh", root, "--preset", "script")
        self.assertNotIn("reattached", out)

    def test_purge_counts_unreviewed_items_and_needs_yes(self):
        code, out = self.run_cli("project", "purge", "p1")
        self.assertEqual(code, 1)
        self.assertIn("1 unreviewed inbox item", out)
        self.assertIn("nothing was deleted", out)
        self.assertTrue(os.path.isdir(self.settings.inbox_dir("p1", "claude")))
        code, out = self.run_cli("project", "purge", "p1", "--yes")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.settings.data_dir, "p1")))

    def test_purge_is_refused_while_the_project_is_live(self):
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(
            [{"Names": ["ab-claude-p1-1-agent"], "State": "running",
              "Labels": {"agentbox.project": "p1", "agentbox.session": "ab-claude-p1-1",
                         "agentbox.role": "agent", "agentbox-test": "1"}}])
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        self.assertEqual(self.run_cli("project", "purge", "p1", "--yes")[0], 2)
        self.assertTrue(os.path.isdir(self.settings.inbox_dir("p1", "claude")))

    def test_purge_of_a_project_with_no_state_is_a_no_op(self):
        code, out = self.run_cli("project", "purge", "nothing-here")
        self.assertEqual(code, 0)
        self.assertIn("no agent state", out)


class ConcurrentSessionsShareState(SettingsCase):
    def test_the_run_warning_says_the_state_is_shared(self):
        self.project("p1", preset="script")
        os.environ["FAKE_PODMAN_PSJSON"] = json.dumps(
            [{"Names": ["ab-claude-p1-1-agent"], "State": "running",
              "Labels": {"agentbox.project": "p1", "agentbox.session": "ab-claude-p1-1",
                         "agentbox.role": "agent", "agentbox-test": "1"}}])
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        said = []
        cli._warn_if_live(self.settings, "p1", warn=said.append)
        joined = " ".join(said)
        self.assertIn("SHARE", joined)
        self.assertIn("last writer wins", joined)


if __name__ == "__main__":
    unittest.main()
