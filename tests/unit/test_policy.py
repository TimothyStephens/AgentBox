"""Registry, presets and the path validator."""
import argparse
import os
import unittest

from agentbox import cli, presets, registry, session
from agentbox.errors import AgentboxError
from agentbox.paths import check_grant

from .helpers import SettingsCase, TempCase


class RegistryTest(TempCase):
    def test_round_trip(self):
        path = os.path.join(self.tmp, "projects.toml")
        projects = {
            "helper": {"root": "/x/helper", "preset": "pods"},
            "data-proc": {"root": '/x/we"ird\\path', "preset": "python", "data_ro": ["/d/raw"],
                          "data_rw": [], "egress": "open", "swap": {".env": ".env.dev", "a.env": "a.dev.env"}},
        }
        registry.save(path, projects)
        self.assertEqual(registry.load(path), projects)

    def test_missing_file_is_empty(self):
        self.assertEqual(registry.load(os.path.join(self.tmp, "nope.toml")), {})

    def test_names(self):
        for bad in ("", "A", "has space", "../x", "x" * 41, "-lead"):
            with self.assertRaises(AgentboxError, msg=bad):
                registry.check_name(bad)
        registry.check_name("life-updates_2")


class PresetTest(unittest.TestCase):
    P = presets.BUILTIN

    def eff(self, project, **kw):
        return presets.effective("t", project, self.P, **kw)

    def test_preset_values_and_provenance(self):
        e = self.eff({"preset": "python"})
        self.assertEqual(e["egress"], ("packages", "preset python"))
        self.assertEqual(e["approvals"], ("off", "preset python"))

    def test_project_override_wins(self):
        e = self.eff({"preset": "python", "egress": "locked"})
        self.assertEqual(e["egress"], ("locked", "project"))

    def test_pods_defaults_to_packages(self):
        self.assertEqual(self.eff({"preset": "pods"})["egress"][0], "packages")

    def test_open_only_when_the_project_says_so(self):
        self.assertEqual(self.eff({"preset": "pods", "egress": "open"})["egress"], ("open", "project"))
        custom = dict(self.P, wide=dict(self.P["python"], egress="open"))
        e = presets.effective("t", {"preset": "wide"}, custom)
        self.assertEqual(e["egress"][0], "packages")

    def test_lockdown_is_a_ceiling(self):
        self.assertEqual(self.eff({"preset": "lockdown"})["egress"][0], "locked")
        for loosen in ({"egress": "packages"}, {"egress": "open"}, {"approvals": "off"}, {"compose": "rw"},
                       {"caps": "opt-in"}, {"secrets": "masked"}, {"podman": {"tier": "dev"}}):
            with self.assertRaises(AgentboxError, msg=loosen):
                self.eff({"preset": "lockdown", **loosen})
        with self.assertRaises(AgentboxError):
            self.eff({"preset": "lockdown", "data_rw": ["/x"]})

    def test_bad_values(self):
        with self.assertRaises(AgentboxError):
            self.eff({"preset": "python", "egress": "wide"})
        with self.assertRaises(AgentboxError):
            self.eff({"preset": "nope"})

    def test_the_lockdown_ceiling_covers_podman_live_and_observe_logs(self):
        """F1: the ceiling checked podman.tier but not podman.live, so lockdown + live=observe handed a
        review-only project read access to the live stack; observe_logs is not a preset setting at all, so
        the loop over overrides never saw it."""
        for loosen in ({"podman": {"tier": "none", "live": "observe"}},
                       {"podman": {"tier": "none", "live": "exec"}},
                       {"observe_logs": True}):
            with self.assertRaises(AgentboxError, msg=loosen):
                self.eff({"preset": "lockdown", **loosen})
        # the tight values stay legal
        self.eff({"preset": "lockdown", "podman": {"tier": "none"}, "observe_logs": False})

    def test_unknown_preset_error_lists_every_known_name(self):
        with self.assertRaises(AgentboxError) as cm:
            self.eff({"preset": "nope"})
        for name in self.P:
            self.assertIn(name, str(cm.exception), name)

    # --- the new preset shapes (build/build-open/script/research/operate/analysis-offline) ---

    EXPECTED_NAMES = {"lockdown", "build", "pods", "build-open", "script", "python",
                      "research", "operate", "analysis-offline"}

    def test_exactly_these_builtin_presets_exist(self):
        self.assertEqual(set(self.P), self.EXPECTED_NAMES)

    def test_new_preset_values(self):
        cases = {
            "build": {"compose": "rw", "egress": "packages", "podman": {"tier": "dev", "live": "observe"},
                      "caps": "opt-in", "secrets": "masked", "approvals": "off", "data": "declared"},
            "build-open": {"compose": "rw", "egress": "packages", "podman": {"tier": "dev", "live": "observe"},
                           "caps": "opt-in", "secrets": "masked", "approvals": "off", "data": "declared"},
            "script": {"compose": "ro", "egress": "packages", "podman": {"tier": "none"}, "caps": "refused",
                       "secrets": "masked", "approvals": "off", "data": "declared"},
            "research": {"compose": "ro", "egress": "packages", "podman": {"tier": "none"}, "caps": "refused",
                         "secrets": "masked", "approvals": "off", "data": "declared"},
            "operate": {"compose": "ro", "egress": "packages", "podman": {"tier": "none", "live": "observe"},
                        "caps": "refused", "secrets": "masked", "approvals": "off", "data": "ro-only"},
            "analysis-offline": {"compose": "ro", "egress": "locked", "podman": {"tier": "none"},
                                 "caps": "refused", "secrets": "masked", "approvals": "off", "data": "ro-only"},
        }
        for name, want in cases.items():
            with self.subTest(preset=name):
                self.assertEqual(self.P[name], want)

    def test_pods_and_python_are_true_aliases_not_redirects(self):
        """pods/python are separate BUILTIN entries holding the same values as build/script, so their own
        provenance string ("preset pods") is unchanged, not renamed to "preset build"."""
        self.assertEqual(self.P["pods"], self.P["build"])
        self.assertEqual(self.P["python"], self.P["script"])
        e = self.eff({"preset": "pods"})
        self.assertEqual(e["egress"], ("packages", "preset pods"))

    # A frozen snapshot of what "pods"/"python" resolved to before this change (docs/design.md's preset
    # table, pre-redesign). Pins that the redesign did not alter either alias's resolution.
    OLD_PODS = {"compose": "rw", "egress": "packages", "podman": {"tier": "dev", "live": "observe"},
                "caps": "opt-in", "secrets": "masked", "approvals": "off", "data": "declared"}
    OLD_PYTHON = {"compose": "ro", "egress": "packages", "podman": {"tier": "none"}, "caps": "refused",
                  "secrets": "masked", "approvals": "off", "data": "declared"}

    def test_pods_and_python_resolve_identically_before_and_after(self):
        self.assertEqual(self.P["pods"], self.OLD_PODS)
        self.assertEqual(self.P["python"], self.OLD_PYTHON)

    def test_research_and_build_open_never_carry_open_in_builtin(self):
        """The egress-open-only-via-project-override rule depends on this: if a BUILTIN preset itself ever
        held "open", effective() would silently downgrade it to "packages" for every project using it with
        no override of its own."""
        self.assertEqual(self.P["research"]["egress"], "packages")
        self.assertEqual(self.P["build-open"]["egress"], "packages")

    def test_podman_live_is_validated(self):
        for bad in ("obsrve", "dev", "yes"):
            with self.assertRaises(AgentboxError, msg=bad):
                self.eff({"preset": "python", "podman": {"tier": "none", "live": bad}})
        # legal values do not raise
        self.eff({"preset": "python", "podman": {"tier": "none", "live": "observe"}})
        self.eff({"preset": "python", "podman": {"tier": "none", "live": "exec"}})

    def test_operate_resolves_as_observe_only_no_dev_tier(self):
        e = self.eff({"preset": "operate"})
        self.assertEqual(e["podman"][0], {"tier": "none", "live": "observe"})
        with self.assertRaises(AgentboxError):
            self.eff({"preset": "operate", "data_rw": ["/x"]})   # data = ro-only


class DescriptionsTest(unittest.TestCase):
    def test_every_builtin_preset_has_a_description(self):
        for name in presets.BUILTIN:
            self.assertIn(name, presets.DESCRIPTIONS, name)
            self.assertTrue(presets.DESCRIPTIONS[name], name)

    def test_aliases_say_so(self):
        self.assertIn("build", presets.DESCRIPTIONS["pods"].lower())
        self.assertIn("script", presets.DESCRIPTIONS["python"].lower())


class OpenOnChoiceTest(unittest.TestCase):
    def test_default_open_presets_get_egress_written_when_absent(self):
        for name in presets.DEFAULT_OPEN_PRESETS:
            entry = {"preset": name}
            presets.open_on_choice(entry)
            self.assertEqual(entry["egress"], "open", name)

    def test_an_explicit_egress_is_left_alone(self):
        entry = {"preset": "research", "egress": "packages"}
        presets.open_on_choice(entry)
        self.assertEqual(entry["egress"], "packages")

    def test_other_presets_get_no_egress_written(self):
        for name in ("build", "script", "lockdown", "operate", "analysis-offline"):
            entry = {"preset": name}
            presets.open_on_choice(entry)
            self.assertNotIn("egress", entry, name)


class CustomPresetTest(TempCase):
    def test_custom_preset_file(self):
        self.write("presets/web.toml", 'egress = "locked"\napprovals = "on"\n')
        ps = presets.load_presets(os.path.join(self.tmp, "presets"))
        self.assertEqual(ps["web"]["egress"], "locked")
        self.assertEqual(ps["web"]["compose"], "ro")

    def test_lockdown_cannot_be_redefined(self):
        self.write("presets/lockdown.toml", 'egress = "open"\n')
        with self.assertRaises(AgentboxError):
            presets.load_presets(os.path.join(self.tmp, "presets"))


class LockdownCeilingEndToEnd(SettingsCase):
    """F1 at the two surfaces that matter: starting a session (load_project) and registering one (add)."""

    def test_refused_at_load(self):
        self.project("lk", preset="lockdown", podman={"tier": "none", "live": "observe"})
        with self.assertRaises(AgentboxError) as cm:
            session.load_project(self.settings, "lk")
        self.assertIn("lockdown", str(cm.exception))

    def test_observe_logs_refused_at_load(self):
        self.project("lk2", preset="lockdown", observe_logs=True)
        with self.assertRaises(AgentboxError):
            session.load_project(self.settings, "lk2")

    def test_refused_at_add(self):
        root = self.mkdir("lkroot")
        self.assertEqual(cli.main(["project", "add", "lk3", root, "--preset", "lockdown",
                                   "--observe-logs"]), 2)
        self.assertNotIn("lk3", registry.load(self.settings.registry_path))
        self.assertEqual(cli.main(["project", "add", "lk3", root, "--preset", "lockdown"]), 0)


class PathTest(TempCase):
    def setUp(self):
        super().setUp()
        self.home = self.mkdir("home", "u")
        for d in (".ssh", ".config/containers", ".local/share/containers", "repos/a", "repos/b/sub", "data"):
            self.mkdir("home", "u", d)

    def check(self, path, **kw):
        return check_grant(path, home=self.home, **kw)

    def test_ok(self):
        self.assertEqual(self.check(os.path.join(self.home, "repos/a")), os.path.join(self.home, "repos/a"))

    def test_refuses_root_home_and_ancestors(self):
        for p in ("/", self.home, os.path.dirname(self.home), self.tmp):
            with self.assertRaises(AgentboxError, msg=p):
                self.check(p)

    def test_refuses_system_dirs(self):
        for p in ("/etc", "/usr/share", "/var/tmp", "/run", "/boot", "/dev"):
            if os.path.isdir(p):
                with self.assertRaises(AgentboxError, msg=p):
                    self.check(p)

    def test_refuses_protected_and_their_parents(self):
        for p in (".ssh", ".config/containers", ".local/share/containers", ".config", ".local/share"):
            with self.assertRaises(AgentboxError, msg=p):
                self.check(os.path.join(self.home, p))

    def test_extra_protected(self):
        with self.assertRaises(AgentboxError):
            self.check(os.path.join(self.home, "data"), extra_protected=[os.path.join(self.home, "data")])

    def test_symlink_into_forbidden_is_resolved(self):
        link = os.path.join(self.home, "repos", "sneaky")
        os.symlink(os.path.join(self.home, ".ssh"), link)
        with self.assertRaises(AgentboxError):
            self.check(link)

    def test_overlap_with_other_projects(self):
        b = os.path.join(self.home, "repos/b")
        with self.assertRaises(AgentboxError):
            self.check(os.path.join(b, "sub"), other_roots=[("b", b)])
        with self.assertRaises(AgentboxError):
            self.check(os.path.join(self.home, "repos"), other_roots=[("b", b)])
        self.check(os.path.join(self.home, "repos/a"), other_roots=[("b", b)])

    def test_missing_dir(self):
        with self.assertRaises(AgentboxError):
            self.check(os.path.join(self.home, "nope"))


def ns(**kw):
    """A parsed `ab project add`/`set` namespace with every shared flag at its real default (taken from the
    shared definition itself, so this helper cannot drift from the CLI)."""
    p = argparse.ArgumentParser()
    cli._add_project_setting_args(p, preset_required=False)
    a = p.parse_args([])
    a.name = "p"
    for k, v in kw.items():
        setattr(a, k, v)
    return a


GRANT_KW = dict(home="/home/nobody", extra_protected=[], other_roots=[])


class PresetChangeTest(unittest.TestCase):
    """F2: a new preset means a new security posture, so stored security-relevant overrides from the old one
    are cleared rather than silently outliving it (a leftover egress=open from `research` is the case that
    matters). Overrides given in the same command win over the clearing."""

    def test_changing_the_preset_clears_stored_security_overrides(self):
        entry = {"root": "/r", "preset": "research", "egress": "open", "observe_logs": True,
                 "extra_domains": ["x.com"], "podman": {"tier": "dev"}, "data_ro": ["/d"],
                 "data_rw": ["/w"], "build_memory": "4g"}
        cleared = cli._apply_project_settings(entry, ns(preset="analysis-offline"), GRANT_KW)
        self.assertEqual(cleared, ["egress", "podman", "data_ro", "data_rw", "extra_domains",
                                   "observe_logs"])
        self.assertEqual(entry["preset"], "analysis-offline")
        for key in cleared:
            self.assertNotIn(key, entry)
        self.assertEqual(entry["build_memory"], "4g", "not security-relevant: kept")
        eff = presets.effective("p", entry, presets.BUILTIN)
        self.assertEqual(eff["egress"][0], "locked", "research -> analysis-offline resolves to locked")
        self.assertEqual(eff["podman"][0], {"tier": "none"})

    def test_overrides_given_in_the_same_command_are_kept(self):
        entry = {"root": "/r", "preset": "research", "egress": "open", "observe_logs": True}
        cleared = cli._apply_project_settings(entry, ns(preset="build", egress="locked"), GRANT_KW)
        self.assertEqual(entry["egress"], "locked", "the flag given now wins over the clearing")
        self.assertNotIn("egress", cleared)
        self.assertEqual(cleared, ["observe_logs"])

    def test_switching_to_an_open_preset_applies_its_open_egress(self):
        entry = {"root": "/r", "preset": "script", "egress": "locked"}
        cleared = cli._apply_project_settings(entry, ns(preset="research"), GRANT_KW)
        self.assertEqual(cleared, ["egress"])
        self.assertEqual(entry["egress"], "open", "cleared, then research's own open default applied")
        self.assertEqual(presets.effective("p", entry, presets.BUILTIN)["egress"], ("open", "project"))

    def test_keeping_the_same_preset_clears_nothing(self):
        entry = {"root": "/r", "preset": "build", "egress": "open", "observe_logs": True}
        self.assertEqual(cli._apply_project_settings(entry, ns(preset="build"), GRANT_KW), [])
        self.assertEqual(entry["egress"], "open")
        self.assertEqual(entry["observe_logs"], True)

    def test_a_new_project_has_nothing_to_clear(self):
        entry = {"root": "/r"}
        self.assertEqual(cli._apply_project_settings(entry, ns(preset="build"), GRANT_KW), [])
        self.assertEqual(entry, {"root": "/r", "preset": "build"})


class SharedArgsTest(unittest.TestCase):
    """Requirement 1's parity scaffolding: `ab init`, `ab project add` and `ab project set` must define the
    project-setting flags identically. This only exercises the shared add-arg function directly; the
    functional half (same overrides produce the same registry entry through each command) lives in
    test_init.py once `ab init` is wired to the same function."""

    def _actions(self, **kw):
        p = argparse.ArgumentParser()
        cli._add_project_setting_args(p, **kw)
        return {a.dest: a for a in p._actions if a.dest != "help"}

    def test_every_shared_flag_is_identical_across_callers(self):
        add_actions = self._actions(preset_required=True)
        set_actions = self._actions(preset_required=False)
        init_actions = self._actions(preset_required=False, preset_default="build")
        self.assertEqual(set(add_actions), set(set_actions))
        self.assertEqual(set(add_actions), set(init_actions))
        for dest, a in add_actions.items():
            with self.subTest(dest=dest):
                b, c = set_actions[dest], init_actions[dest]
                self.assertEqual(a.option_strings, b.option_strings)
                self.assertEqual(a.option_strings, c.option_strings)
                self.assertEqual(type(a), type(b))
                self.assertEqual(type(a), type(c))
                self.assertEqual(getattr(a, "choices", None), getattr(b, "choices", None))
                self.assertEqual(getattr(a, "choices", None), getattr(c, "choices", None))
                if dest != "preset":
                    self.assertEqual(a.default, b.default)
                    self.assertEqual(a.default, c.default)
                    self.assertEqual(a.required, b.required)
                    self.assertEqual(a.required, c.required)

    def test_preset_required_and_default_vary_as_intended(self):
        add_actions = self._actions(preset_required=True)
        set_actions = self._actions(preset_required=False)
        init_actions = self._actions(preset_required=False, preset_default="build")
        self.assertTrue(add_actions["preset"].required)
        self.assertIsNone(add_actions["preset"].default)
        self.assertFalse(set_actions["preset"].required)
        self.assertIsNone(set_actions["preset"].default)
        self.assertFalse(init_actions["preset"].required)
        self.assertEqual(init_actions["preset"].default, "build")


if __name__ == "__main__":
    unittest.main()
