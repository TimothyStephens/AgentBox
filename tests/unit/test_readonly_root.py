"""A read-only project root: the `code` setting and the `planner` preset (docs/readonly-root-proposal.md, step 1;
Tim's decisions D1-D8 as recommended, 18 Nov 2026). The plan is written to a `data_rw` grant outside the root;
the dev tier is refused with a read-only root; clone mode falls back to checkout."""
import os

from agentbox import grantcheck, mounts, presets, projectkeys, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase


class TheSetting(SettingsCase):
    def test_code_is_a_preset_setting_ro_tightest(self):
        k = projectkeys.BY_NAME["code"]
        self.assertEqual((k.kind, k.values, k.security), ("preset", ("ro", "rw"), True))

    def test_every_builtin_states_code_and_only_planner_is_read_only(self):
        for name, values in presets.BUILTIN.items():
            with self.subTest(preset=name):
                self.assertIn("code", values)
                self.assertEqual(values["code"], "ro" if name == "planner" else "rw")

    def test_planner_is_research_read_only_with_packages_egress(self):
        planner, research = presets.BUILTIN["planner"], presets.BUILTIN["research"]
        self.assertEqual({k: v for k, v in planner.items() if k not in ("code", "egress")},
                         {k: v for k, v in research.items() if k not in ("code", "egress")})
        self.assertEqual(planner["egress"], "packages")
        self.assertNotIn("as under every preset", presets.DESCRIPTIONS["research"])

    def test_a_new_user_preset_keeps_a_read_write_root(self):
        os.makedirs(self.settings.presets_dir, exist_ok=True)
        with open(os.path.join(self.settings.presets_dir, "mine.toml"), "w") as f:
            f.write('egress = "packages"\n')
        self.assertEqual(presets.load_presets(self.settings.presets_dir)["mine"]["code"], "rw")

    def test_a_project_may_tighten_lockdown_to_a_read_only_root(self):
        self.project("p1", preset="lockdown", code="ro")
        self.assertEqual(session.load_project(self.settings, "p1").value("code"), "ro")


class ThePlan(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")
        import subprocess
        subprocess.run(["git", "init", "-q", self.root], check=True)
        self.write("proj/.env.dev", "A=1\n")
        self.write("proj/.env", "A=real\n")
        self.write("proj/compose.yaml", "services: {}\n")
        self.notes = self.mkdir("notes")

    def plan(self, code):
        return mounts.plan(root=self.root, data_rw=[self.notes], compose="ro", code=code,
                           swap=mounts.effective_swap(None))

    def by_dst(self, plan):
        return {m.dst: m for m in plan.mounts}

    def test_the_root_is_bound_read_only_and_the_notes_grant_read_write(self):
        m = self.by_dst(self.plan("ro"))
        self.assertTrue(m[self.root].ro)
        self.assertFalse(m[self.notes].ro)
        self.assertFalse(self.by_dst(self.plan("rw"))[self.root].ro, "rw is unchanged")

    def test_masks_still_apply_and_the_swap_is_read_only(self):
        m = self.by_dst(self.plan("ro"))
        env = os.path.join(self.root, ".env")
        self.assertIn(env, m, "the real .env is still hidden behind the dev swap")
        self.assertTrue(m[env].ro)

    def test_no_git_or_compose_covers_inside_a_read_only_root(self):
        m = self.by_dst(self.plan("ro"))
        self.assertNotIn(os.path.join(self.root, ".git", "hooks"), m)
        self.assertNotIn(os.path.join(self.root, "compose.yaml"), m)
        rw = self.by_dst(self.plan("rw"))
        self.assertIn(os.path.join(self.root, ".git", "hooks"), rw, "control: a writable root gets them")


class Validation(SettingsCase):
    def test_a_writable_grant_inside_a_read_only_root_is_refused(self):
        root = self.project("p1", preset="planner")
        inner = os.path.join(root, "plans")
        os.makedirs(inner)
        self.project("p1", preset="planner", data_rw=[inner])
        with self.assertRaises(AgentboxError) as cm:
            session.load_project(self.settings, "p1")
        self.assertIn("read-only", str(cm.exception))


class TheSession(SettingsCase):
    def make(self, **extra):
        self.project("p1", preset="planner", **extra)
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=self.warnings.append)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s

    def setUp(self):
        super().setUp()
        self.warnings = []
        self.token()
        self.notes_dir = self.mkdir("plans-p1")

    def test_the_agent_sees_the_root_read_only_and_the_notes_dir_writable(self):
        s = self.make(data_rw=[self.notes_dir])
        argv = s.run_args(["true"])
        root = s.project.root
        self.assertIn(f"--mount=type=bind,src={root},dst={root},ro", argv)
        self.assertIn(f"--mount=type=bind,src={self.notes_dir},dst={self.notes_dir}", argv)
        notes = session.sandbox_notes(s.project, s.egress, s.podman_live, s.podman_dev)
        self.assertIn("read-only", notes)
        self.assertIn(self.notes_dir, notes)

    def test_the_gate_config_counts_the_root_as_read_only(self):
        s = self.make()
        self.assertIn(os.path.realpath(s.project.root), [os.path.realpath(r) for r in s.ro_roots()])
        import inspect
        self.assertIn("self.ro_roots()", inspect.getsource(session.Session.gate_config),
                      "the gate's ro_roots come from the same list")

    def test_the_root_is_neither_backed_up_nor_scanned(self):
        s = self.make(data_rw=[self.notes_dir])
        self.assertEqual(s.writable_grants(), [self.notes_dir])

    def test_the_dev_tier_is_refused_with_a_read_only_root(self):
        with self.assertRaises(AgentboxError) as cm:
            self.make(podman={"tier": "dev"})
        self.assertIn("read-only", str(cm.exception))

    def test_clone_mode_falls_back_to_checkout_with_a_note(self):
        s = self.make(mount="clone")
        self.assertEqual(s.mount_mode, "checkout")
        self.assertTrue([w for w in self.warnings if "clone" in w and "read-only" in w], self.warnings)


class TheGrantCheck(SettingsCase):
    def test_a_readable_credential_still_refuses_and_live_data_only_warns(self):
        root = self.project("p1", preset="planner")
        with open(os.path.join(root, ".git", "config"), "a") as f:
            f.write('[remote "o"]\n\turl = https://user:tok123@example.com/r.git\n')
        with open(os.path.join(root, "big.sqlite"), "wb") as f:
            f.truncate((grantcheck.LIVE_DATA_MB + 1) << 20)
        entry = {"root": root, "preset": "planner"}
        findings, warnings, _fp = grantcheck.preflight(self.settings, "p1", entry)
        texts = [f["text"] for f in findings]
        self.assertTrue([t for t in texts if "git credentials" in t], "readable, so it can reach the model API")
        self.assertFalse([t for t in texts if "big.sqlite" in t], "live data in a read-only root cannot be damaged")
        self.assertTrue([w for w in warnings if "big.sqlite" in w])


class TheShow(SettingsCase):
    def test_show_says_the_root_is_read_only_and_where_writes_go(self):
        self.project("p1", preset="planner")
        code, out = self.ab("project", "show", "p1")
        self.assertEqual(code, 0, out)
        [line] = [l for l in out.splitlines() if l.strip().startswith("code ")]
        self.assertIn("ro", line)
        self.assertIn("read-only", line)


class HooksIntoTheWritableGrant(SettingsCase):
    """The case the proposal's step 2 exists for: a read-only root's own .git/config pointing core.hooksPath into
    the writable plans grant. The owner's host git would run what the agent wrote there, so the plan must
    cover the target read-only (merged with wt-gitgap, 18 Nov)."""

    def test_the_hooks_path_target_in_the_plans_grant_is_covered_read_only(self):
        import subprocess
        root = self.mkdir("proj")
        subprocess.run(["git", "init", "-q", root], check=True)
        plans = self.mkdir("plans")
        hooks = os.path.join(plans, "hooks")
        subprocess.run(["git", "-C", root, "config", "core.hooksPath", hooks], check=True)
        p = mounts.plan(root=root, data_rw=[plans], code="ro")
        covers = {m.dst: m for m in p.mounts}
        self.assertIn(hooks, covers)
        self.assertTrue(covers[hooks].ro)
