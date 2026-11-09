"""The data directory's project layout (layout.py, config.py): one project is one directory under
`projects/`, a flat data dir migrates into it once, and a project's state knows which root it belongs to, so
a reused name cannot quietly hand one project's memory and built-image record to another."""
import json
import os
from unittest import mock

from agentbox import builtrec, config, layout, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, read_json


class Layout(SettingsCase):
    def test_everything_of_a_project_is_under_its_directory(self):
        s = self.settings
        base = os.path.join(s.data_dir, "projects", "web")
        self.assertEqual(s.project_dir("web"), base)
        self.assertEqual(s.state_dir("web", "claude"), os.path.join(base, "claude"))
        self.assertEqual(s.inbox_dir("web", "omp"), os.path.join(base, "omp", "inbox"))
        self.assertEqual(s.project_backups_dir("web"), os.path.join(base, "backups"))
        self.assertEqual(s.built_record("web"), os.path.join(base, "built-images.json"))
        self.assertEqual(builtrec.path_for(s.data_dir, "web"), s.built_record("web"))
        self.assertEqual(s.project_file("web"), os.path.join(base, "project.json"))

    def test_a_project_named_like_an_own_directory_cannot_collide(self):
        """`logs`, `backups`, `tmp`... are valid project names; their state used to land inside agentbox's
        own directories of the same name."""
        for name in sorted(config.OWN_DIRS):
            state = self.settings.state_dir(name, "claude")
            self.assertEqual(state, os.path.join(self.settings.projects_dir, name, "claude"))
            if name != config.PROJECTS:
                self.assertFalse(state.startswith(os.path.join(self.settings.data_dir, name) + "/"), state)

    def test_agent_state_dirs_skip_the_projects_own_entries(self):
        s = self.settings
        os.makedirs(s.state_dir("p1", "claude"))
        os.makedirs(s.state_dir("p1", "omp"))
        os.makedirs(s.project_backups_dir("p1"))
        layout.write_identity(s, "p1", self.tmp)
        with open(s.built_record("p1"), "w") as f:
            f.write("{}")
        self.assertEqual([a for a, _ in s.agent_state_dirs("p1")], ["claude", "omp"])
        self.assertEqual(s.projects_on_disk(), ["p1"])


class Migration(SettingsCase):
    """A flat data dir (state at `<data>/<name>/`, `backups/<name>/`, `built-images/<name>.json`) moves into
    `projects/<name>/` once, by rename; agentbox's own directories stay; nothing ambiguous is touched."""

    def flat(self):
        d = self.settings.data_dir
        self.write("data/p1/claude/CLAUDE.md", "memory\n")
        self.write("data/p1/claude/inbox/note.md", "n\n")
        self.write("data/p1/session.lock", "{}")
        self.write("data/backups/p1/20261101-120000-abt-unit-claude-p1-1/x/app.py", "code\n")
        self.write("data/built-images/p1.json", json.dumps({"images": {"a" * 64: {"session": "s", "built": 1}}}))
        self.write("data/built-images/p1.json.lock", "")
        self.write("data/logs/abt-unit-claude-p1-1-20261101-120000-scan.log", "scan\n")
        self.write("data/logs/ab.log", "log\n")
        self.write("data/profiles/claude/abc/CLAUDE.md", "p\n")
        self.write("data/tmp/abt-unit-claude-p1-1/x", "")
        self.write("data/trial/p-snapshot.json", "{}")
        return d

    def test_flat_layout_moves_into_projects(self):
        d = self.flat()
        warnings = []
        moved = layout.migrate(self.settings, warnings.append)
        self.assertEqual(len(moved), 3, moved)
        s = self.settings
        self.assertTrue(os.path.isfile(os.path.join(s.state_dir("p1", "claude"), "CLAUDE.md")))
        self.assertTrue(os.path.isfile(os.path.join(s.inbox_dir("p1", "claude"), "note.md")))
        self.assertTrue(os.path.isfile(os.path.join(s.project_dir("p1"), "session.lock")))
        self.assertTrue(os.path.isdir(os.path.join(s.project_backups_dir("p1"),
                                                   "20261101-120000-abt-unit-claude-p1-1")))
        self.assertEqual(set(builtrec.ids(s.built_record("p1"))), {"a" * 64})
        for gone in ("p1", "backups", "built-images"):
            self.assertFalse(os.path.exists(os.path.join(d, gone)), gone)
        for kept in ("logs/ab.log", "profiles/claude/abc/CLAUDE.md", "tmp/abt-unit-claude-p1-1/x",
                     "trial/p-snapshot.json"):
            self.assertTrue(os.path.exists(os.path.join(d, kept)), kept)
        self.assertTrue(warnings and "moved 3" in warnings[0] and "projects/" in warnings[0], warnings)

    def test_a_second_run_moves_nothing(self):
        self.flat()
        layout.migrate(self.settings)
        self.assertEqual(layout.migrate(self.settings), [])

    def test_a_fresh_data_dir_is_a_no_op(self):
        self.assertEqual(layout.migrate(self.settings), [])
        self.assertFalse(os.path.exists(self.settings.projects_dir))

    def test_a_collision_is_left_in_place_and_named(self):
        """Both the old and the new path exist: nothing is merged, somebody has to look."""
        self.flat()
        self.write("data/projects/p1/claude/CLAUDE.md", "newer memory\n")
        warnings = []
        moved = layout.migrate(self.settings, warnings.append)
        self.assertEqual([m for m in moved if m.startswith(os.path.join(self.settings.data_dir, "p1 "))], [])
        self.assertTrue(os.path.isfile(os.path.join(self.settings.data_dir, "p1", "claude", "CLAUDE.md")),
                        "the old state stays")
        self.assertTrue(any("already exists" in w for w in warnings), warnings)
        # the backups and the record did move: their destinations were free
        self.assertTrue(os.path.isdir(self.settings.project_backups_dir("p1")))

    def test_something_that_is_not_a_project_name_is_left(self):
        self.write("data/Not-A-Project/x", "")
        self.write("data/.hidden/x", "")
        warnings = []
        self.assertEqual(layout.migrate(self.settings, warnings.append), [])
        self.assertTrue(os.path.isdir(os.path.join(self.settings.data_dir, "Not-A-Project")))
        self.assertTrue(any("not a project name" in w for w in warnings), warnings)

    def test_ab_migrates_before_running_any_command(self):
        self.flat()
        code, out = self.ab("project", "ls")
        self.assertEqual(code, 0, out)
        self.assertIn("moved 3", out)
        self.assertTrue(os.path.isfile(os.path.join(self.settings.state_dir("p1", "claude"), "CLAUDE.md")))

    def test_the_lock_scan_and_the_inbox_scan_read_the_new_layout(self):
        from agentbox import profile, projectlock
        self.flat()
        layout.migrate(self.settings)
        self.assertEqual(projectlock.projects_with_locks(self.settings), ["p1"])
        self.assertEqual([p for p, _ in profile.all_inboxes(self.settings, "claude")], ["p1"])


class Identity(SettingsCase):
    """`project.json` says which root a project directory's state was made for; `ab project add` refuses a
    different root unless told what to do with the state."""

    def setUp(self):
        super().setUp()
        self.root_a = self.mkdir("roots", "a")
        self.root_b = self.mkdir("roots", "b")
        code, out = self.ab("project", "add", "p1", self.root_a, "--preset", "script")
        self.assertEqual(code, 0, out)
        self.state = self.settings.state_dir("p1", "claude")
        os.makedirs(os.path.join(self.state, "inbox"))
        self.write("data/projects/p1/claude/CLAUDE.md", "memory of a\n")
        self.write("data/projects/p1/claude/inbox/learned.md", "x\n")
        builtrec.add(self.settings.built_record("p1"), "a" * 64, "abt-unit-claude-p1-1")

    def test_add_writes_the_identity(self):
        doc = read_json(self.settings.project_file("p1"))
        self.assertEqual(doc["root"], self.root_a)
        self.assertEqual(doc["name"], "p1")
        self.assertTrue(doc["created"])

    def test_the_same_root_reattaches_as_before(self):
        self.ab("project", "rm", "p1")
        code, out = self.ab("project", "add", "p1", self.root_a, "--preset", "script")
        self.assertEqual(code, 0, out)
        self.assertIn("reattached existing agent state", out)
        self.assertIn("built images: 1 recorded", out)
        self.assertTrue(os.path.isfile(os.path.join(self.state, "CLAUDE.md")))

    def test_a_different_root_is_refused_and_nothing_is_saved(self):
        self.ab("project", "rm", "p1")
        code, out = self.ab("project", "add", "p1", self.root_b, "--preset", "script")
        self.assertEqual(code, 2, out)
        for word in (self.root_a, self.root_b, "--fresh", "--reattach", "built-image record"):
            self.assertIn(word, out)
        code, listed = self.ab("project", "ls")
        self.assertNotIn("p1", listed)
        self.assertEqual(read_json(self.settings.project_file("p1"))["root"], self.root_a, "identity unchanged")
        self.assertTrue(os.path.isfile(os.path.join(self.state, "CLAUDE.md")), "state untouched")

    def test_fresh_archives_the_state_and_the_built_record(self):
        self.ab("project", "rm", "p1")
        code, out = self.ab("project", "add", "p1", self.root_b, "--preset", "script", "--fresh")
        self.assertEqual(code, 0, out)
        self.assertIn("--fresh", out)
        self.assertEqual(self.settings.agent_state_dirs("p1"), [], "no agent state is left")
        self.assertFalse(os.path.exists(self.settings.built_record("p1")), "the record went with it")
        self.assertFalse(os.path.exists(self.settings.built_record("p1") + ".lock"))
        archives = [d for d in os.listdir(self.settings.project_backups_dir("p1")) if d.startswith("agent-state-")]
        self.assertEqual(len(archives), 1, archives)
        archive = os.path.join(self.settings.project_backups_dir("p1"), archives[0])
        self.assertTrue(os.path.isfile(os.path.join(archive, "claude", "CLAUDE.md")))
        self.assertTrue(os.path.isfile(os.path.join(archive, "claude", "inbox", "learned.md")))
        self.assertEqual(set(builtrec.ids(os.path.join(archive, "built-images.json"))), {"a" * 64})
        self.assertEqual(read_json(self.settings.project_file("p1"))["root"], self.root_b)
        self.assertNotIn("reattached", out)

    def test_reattach_keeps_the_state_and_records_the_old_root(self):
        self.ab("project", "rm", "p1")
        code, out = self.ab("project", "add", "p1", self.root_b, "--preset", "script", "--reattach")
        self.assertEqual(code, 0, out)
        self.assertIn("reattached existing agent state", out)
        self.assertTrue(os.path.isfile(os.path.join(self.state, "CLAUDE.md")))
        self.assertEqual(set(builtrec.ids(self.settings.built_record("p1"))), {"a" * 64})
        doc = read_json(self.settings.project_file("p1"))
        self.assertEqual((doc["root"], doc["previous_roots"]), (self.root_b, [self.root_a]))

    def test_an_identity_with_no_state_behind_it_is_simply_replaced(self):
        """Added, never run, removed: there is nothing to merge, so another root takes the name."""
        root = self.mkdir("roots", "c")
        self.ab("project", "add", "p2", root, "--preset", "script")
        self.ab("project", "rm", "p2")
        code, out = self.ab("project", "add", "p2", self.root_b, "--preset", "script")
        self.assertEqual(code, 0, out)
        self.assertEqual(read_json(self.settings.project_file("p2"))["root"], self.root_b)

    def test_both_flags_contradict(self):
        self.ab("project", "rm", "p1")
        code, out = self.ab("project", "add", "p1", self.root_b, "--preset", "script", "--reattach", "--fresh")
        self.assertEqual(code, 2, out)

    def test_state_from_before_the_identity_file_reattaches_and_gets_one(self):
        """A migrated project directory has no project.json; the first `add` is the only thing that knows
        its root, so it is accepted and recorded (as the old behaviour was)."""
        self.ab("project", "rm", "p1")
        os.unlink(self.settings.project_file("p1"))
        code, out = self.ab("project", "add", "p1", self.root_b, "--preset", "script")
        self.assertEqual(code, 0, out)
        self.assertIn("reattached existing agent state", out)
        self.assertEqual(read_json(self.settings.project_file("p1"))["root"], self.root_b)

    def test_init_refuses_a_reused_name_before_writing_anything(self):
        repos = self.mkdir("repos")
        self.ab("project", "rm", "p1")
        with mock.patch.dict(os.environ, {"AB_REPOS_DIR": repos}):
            code, out = self.ab("init", "python", "--name", "p1")
        self.assertEqual(code, 2, out)
        self.assertIn("--fresh", out)
        self.assertFalse(os.path.exists(os.path.join(repos, "p1")), "no tree was scaffolded")

    def test_a_session_refuses_a_registry_root_the_state_does_not_belong_to(self):
        """The registry edited by hand to another root: the state must not be mounted into it."""
        self.token()
        self.ab("project", "rm", "p1")
        self.project("p1", preset="script")          # written straight into the registry, root elsewhere
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"))
        with self.assertRaises(AgentboxError) as cm:
            s.prepare(do_backup=False)
        self.assertIn("--reattach", str(cm.exception))
        self.assertIsNone(s.name, "refused before a session was reserved")

    def test_a_session_writes_a_missing_identity_from_the_registry(self):
        self.token()
        os.unlink(self.settings.project_file("p1"))
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"))
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        self.assertEqual(read_json(self.settings.project_file("p1"))["root"], self.root_a)


class Purge(SettingsCase):
    def test_purge_deletes_the_whole_project_directory(self):
        root = self.mkdir("roots", "a")
        self.ab("project", "add", "p1", root, "--preset", "script")
        self.write("data/projects/p1/claude/CLAUDE.md", "m\n")
        os.makedirs(os.path.join(self.settings.project_backups_dir("p1"), "20261101-120000-abt-unit-claude-p1-1"))
        builtrec.add(self.settings.built_record("p1"), "b" * 64, "abt-unit-claude-p1-1")
        code, out = self.ab("project", "purge", "p1")
        self.assertEqual(code, 1)
        self.assertIn("grant backups, built-image record", out)
        self.assertIn("backups: 1 grant snapshot", out)
        self.assertIn("built images: 1 recorded", out)
        code, out = self.ab("project", "purge", "p1", "--yes")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.settings.project_dir("p1")))
        self.assertEqual(builtrec.ids(self.settings.built_record("p1")), set(),
                         "a re-added p1 does not inherit the images")


class BuiltRecords(SettingsCase):
    def test_remove_everywhere_walks_the_projects(self):
        s = self.settings
        builtrec.add(s.built_record("p1"), "a" * 64, "s1")
        builtrec.add(s.built_record("p2"), "a" * 64, "s2")
        builtrec.add(s.built_record("p2"), "b" * 64, "s2")
        builtrec.remove_everywhere(s.data_dir, ["sha256:" + "a" * 64])
        self.assertEqual(builtrec.ids(s.built_record("p1")), set())
        self.assertEqual(builtrec.ids(s.built_record("p2")), {"b" * 64})

    def test_remove_everywhere_with_no_projects_dir(self):
        builtrec.remove_everywhere(self.settings.data_dir, ["a" * 64])
