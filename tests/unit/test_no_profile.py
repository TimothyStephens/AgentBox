"""Running without the host-owned profile: `ab run --no-profile` and `profile = "off"` on a project.

The profile is how a session gets the user's skills, sub-agents, commands, instructions and settings, and
(for OMP) the plugins/repos/pylibs snapshot from `~/.omp`. Two reasons to turn all of it off:

  * `--no-profile`, for one run -- reproducing something without the user's instructions in the way, or
    telling an agent's own behaviour apart from a skill's;
  * `profile = "off"` on the project, for a project that should always run bare.

"Off" has to mean off at every door, which is what these tests pin: the read-only profile mounts, the items
copied into state rather than mounted (Claude's settings.json), the profile text in the generated
instruction file, the plugin snapshot, and the image -- a user image layer is built on the harness image and
is profile content too, so `--no-profile` must not select one.

What stays: agentbox's own session notes, which describe the sandbox and are not the user's profile.
"""
import os
import unittest
from unittest import mock

from agentbox import profile, session
from agentbox.adapters import omp

from .helpers import SettingsCase, read_file
from .test_omp import OmpSessionBase

C_CLAUDE = "/run/agentbox/claude"


class ClaudeWithoutTheProfile(SettingsCase):
    def setUp(self):
        super().setUp()
        self.token()
        self.write("config/claude/profile/skills/deploy/SKILL.md", "run make deploy\n")
        self.write("config/claude/profile/CLAUDE.md", "PROFILE_HOUSE_STYLE\n")
        self.write("config/claude/profile/settings.json", '{"theme": "dark"}\n')

    def start(self, name="p1", *, no_profile=False, preset="script", **extra):
        self.project(name, preset=preset, **extra)
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, name),
                                 warn=warnings.append, no_profile=no_profile)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s, warnings

    def mounts(self, s):
        return {m.split("dst=")[1].split(",")[0]: m for m in s.run_args(["true"]) if "--mount=" in m}

    def profile_log(self, s):
        with open(s.log_path("profile")) as f:
            return f.read().splitlines()

    # ---- the flag ----

    def test_with_the_profile_on_the_skill_is_mounted(self):
        """The control: without the flag, the skill is there -- so its absence below means something."""
        s, _w = self.start()
        self.assertIn(f"{C_CLAUDE}/skills", self.mounts(s))
        self.assertFalse(s.profile.off)

    def test_the_flag_mounts_no_profile_skill(self):
        s, _w = self.start(no_profile=True)
        self.assertNotIn(f"{C_CLAUDE}/skills", self.mounts(s))
        self.assertEqual(s.profile.mounts, [])
        self.assertTrue(s.profile.off)

    def test_the_flag_takes_no_snapshot_at_all(self):
        """Not just "mounts nothing": nothing is read, so no snapshot is taken and no hash exists. A run
        that does not load the profile must not be deciding what the profile looks like for the next one."""
        s, _w = self.start(no_profile=True)
        self.assertEqual(s.profile.hash, "")
        self.assertIsNone(s.profile.dir)
        self.assertFalse(os.path.isdir(os.path.join(self.settings.profiles_dir, "claude")))

    def test_the_profiles_settings_json_is_not_copied_into_the_state_either(self):
        """settings.json is a profile item that happens to be COPIED rather than mounted, because Claude
        writes to it. Off is off: the copy does not happen.

        The file still exists -- `prepare_session` writes agentbox's own `skipDangerousModePermissionPrompt`
        into it when approvals are off, which is the launcher's business, not the profile's. What must not
        be there is anything the user's profile said."""
        s, _w = self.start(no_profile=True)
        settings_json = os.path.join(s.state_dir, "settings.json")
        self.assertNotIn("theme", read_file(settings_json) if os.path.exists(settings_json) else "")
        s2, _w2 = self.start("p2")
        self.assertIn("dark", read_file(os.path.join(s2.state_dir, "settings.json")),
                      "and with the profile on, the profile's own settings are there")

    def test_the_generated_instructions_keep_the_sandbox_notes_and_drop_the_profile_text(self):
        s, _w = self.start(no_profile=True)
        md = [m for d, m in self.mounts(s).items() if d == f"{C_CLAUDE}/CLAUDE.md"]
        self.assertTrue(md, self.mounts(s))
        text = read_file(md[0].split("src=")[1].split(",")[0])
        self.assertNotIn("PROFILE_HOUSE_STYLE", text)
        self.assertIn("agentbox", text, "agentbox's own session notes still load")

    def test_the_session_log_says_it_was_off_and_why(self):
        s, warnings = self.start(no_profile=True)
        lines = self.profile_log(s)
        self.assertEqual(lines[0], "profile claude=off reason=flag")
        self.assertEqual(lines[1], "image claude=localhost/agentbox-claude:latest",
                         "and the log says which image actually ran")
        self.assertTrue(any("profile claude=off (reason=flag)" in w for w in warnings), warnings)

    # ---- the project setting ----

    def test_the_project_setting_turns_it_off_with_its_own_reason(self):
        s, _w = self.start("off1", profile="off")
        self.assertTrue(s.profile.off)
        self.assertEqual(s.profile.mounts, [])
        self.assertEqual(self.profile_log(s)[0], "profile claude=off reason=project")

    def test_the_flag_is_the_reason_when_both_say_off(self):
        """The flag was typed just now; the setting has been there all along."""
        s, _w = self.start("off2", no_profile=True, profile="off")
        self.assertEqual(self.profile_log(s)[0], "profile claude=off reason=flag")

    def test_profile_on_is_the_default_and_loads(self):
        s, _w = self.start("on1", profile="on")
        self.assertFalse(s.profile.off)
        self.assertIn(f"{C_CLAUDE}/skills", self.mounts(s))

    # ---- the image ----

    def test_the_harness_image_is_used_and_a_user_layer_is_never_selected(self):
        """A user image layer exists and is current, and `--no-profile` still runs the plain harness image:
        a layer is built on the harness and is profile content, so "no profile" that ran the user's own
        build of the agent would be a lie. The layer is not even looked for -- there is no warning."""
        import json as _json
        harness = "localhost/agentbox-claude:latest"
        self.write("config/claude/profile/Containerfile", "ARG BASE\nFROM ${BASE}\nRUN true\n")
        with open(os.path.join(self.settings.profile_src("claude"), "Containerfile"), "rb") as f:
            recipe = f.read()
        ref = profile.user_image_ref("claude", "sha256:harness1", recipe)
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = _json.dumps({harness: {"Id": "sha256:harness1"},
                                                            ref: {"Id": "sha256:layer1"}})
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        s, warnings = self.start(no_profile=True)
        self.assertEqual(s.image, harness)
        self.assertIn(harness, s.run_args(["true"]))
        self.assertNotIn(ref, s.run_args(["true"]))
        self.assertFalse([w for w in warnings if "out of date" in w], warnings)
        on, _w = self.start("p3")
        self.assertEqual(on.image, ref, "and with the profile on, the layer is what runs")


class TheSettingItself(SettingsCase):
    def test_it_round_trips_through_add_and_show(self):
        root = self.mkdir("projects", "p1")
        code, out = self.ab("project", "add", "p1", root, "--preset", "script", "--profile", "off")
        self.assertEqual(code, 0, out)
        _code, shown = self.ab("project", "show", "p1")
        # The row itself, not just the words: "off" alone matches `approvals off` on this very project.
        rows = [l.split() for l in shown.splitlines() if l.startswith("  profile ")]
        self.assertEqual([r[:2] for r in rows], [["profile", "off"]], shown)
        self.assertIn("from project", " ".join(rows[0]), "and it is shown as the project's own, not a default")

    def test_a_bad_value_is_refused(self):
        root = self.mkdir("projects", "p2")
        code, out = self.ab("project", "add", "p2", root, "--preset", "script", "--profile", "maybe")
        self.assertNotEqual(code, 0)
        self.assertIn("--profile", out)

    def test_unset_brings_the_default_back(self):
        root = self.mkdir("projects", "p3")
        self.ab("project", "add", "p3", root, "--preset", "script", "--profile", "off")
        code, out = self.ab("project", "set", "p3", "--unset", "profile")
        self.assertEqual(code, 0, out)
        self.assertEqual(session.load_project(self.settings, "p3").profile, "on")

    def test_the_lockdown_ceiling_does_not_refuse_turning_it_on(self):
        """The profile has always loaded under lockdown -- it is host-owned, so it is trusted there. An
        ordered key gets ceiling-checked, which would have made the WRITTEN default an error while the
        unwritten one stayed fine; `profile` is not security-relevant, which is what keeps it out."""
        root = self.mkdir("projects", "lk")
        code, out = self.ab("project", "add", "lk", root, "--preset", "lockdown", "--profile", "on")
        self.assertEqual(code, 0, out)
        self.assertEqual(session.load_project(self.settings, "lk").profile, "on")

    def test_it_is_not_cleared_by_a_preset_change(self):
        """How you want the project run is not a property of the preset's posture."""
        root = self.mkdir("projects", "p4")
        self.ab("project", "add", "p4", root, "--preset", "script", "--profile", "off")
        code, out = self.ab("project", "set", "p4", "--preset", "research")
        self.assertEqual(code, 0, out)
        self.assertEqual(session.load_project(self.settings, "p4").profile, "off")


class OmpWithoutTheProfile(OmpSessionBase):
    """OMP has a second half: the installed plugin tree. `--no-profile` drops it too.

    It used to be the host's `~/.omp` snapshot, and the flag's property was "a session reads nothing from
    the host's ~/.omp". Since 5 Nov nothing reads that directory at all, so the property is now about the
    tree agentbox builds: with the flag, a session gets no plugins -- and still gets agentbox's own
    configuration of the agent, which is a different thing from the profile."""

    def make(self, preset="python", *, no_profile=False, **extra):
        self.project("p1", preset=preset, **extra)
        s = session.make_session(self.settings_now(), "omp",
                                 session.load_project(self.settings_now(), "p1"),
                                 warn=lambda m: None, no_profile=no_profile)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s

    def dsts(self, s):
        return {m.split("dst=")[1].split(",")[0] for m in s.run_args(["true"]) if "--mount=" in m}

    def test_the_tree_is_mounted_with_the_profile_on(self):
        """The control. Without it, every test below would pass with the tree never mounted at all."""
        dsts = self.dsts(self.make())
        self.assertIn(omp.TREE_DST, dsts)
        self.assertIn(omp.MARKETPLACES_DST, dsts)

    def test_the_flag_drops_the_tree_and_the_marketplace_registry(self):
        dsts = self.dsts(self.make(no_profile=True))
        self.assertNotIn(omp.TREE_DST, dsts)
        self.assertNotIn(omp.MARKETPLACES_DST, dsts)

    def test_the_project_setting_drops_them_too(self):
        dsts = self.dsts(self.make(profile="off"))
        self.assertNotIn(omp.TREE_DST, dsts)
        self.assertNotIn(omp.MARKETPLACES_DST, dsts)

    def test_nothing_of_the_tree_reaches_the_session_under_the_flag(self):
        """By source, not by destination: a mount of the tree at some other path would still be the tree."""
        s = self.make(no_profile=True)
        tree = os.path.realpath(self.tree)
        self.assertEqual([src for src, _d, _ro in s.setup.mounts
                          if os.path.realpath(src) == tree
                          or os.path.realpath(src).startswith(tree + os.sep)], [])

    def test_agentboxs_own_config_still_arrives(self):
        """`config.yml` comes from `~/.config/agentbox/omp/`, which `--no-profile` does not touch: the flag
        turns off the PROFILE and the plugins, not agentbox's own configuration of the agent."""
        self.assertIn(f"{omp.C_OMP}/agent/config.yml", self.dsts(self.make(no_profile=True)))

    def test_the_profile_load_paths_are_dropped_as_well(self):
        s = self.make(no_profile=True)
        self.assertEqual(s.profile.mounts, [])
        with open(s.log_path("profile")) as f:
            self.assertEqual(f.read().splitlines()[0], "profile omp=off reason=flag")

    def test_the_staleness_notes_are_silent_under_the_flag(self):
        """Nothing is mounted, so a tree that does not fit this image is not this session's problem."""
        said = []
        omp.plugin_notes(self.settings, said.append, {"agentbox.cli-version": "99.0.0"}, plugins=False)
        self.assertEqual(said, [])


if __name__ == "__main__":
    unittest.main()
