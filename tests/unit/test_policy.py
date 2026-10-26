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
            "build-open": {"compose": "rw", "egress": "open", "podman": {"tier": "dev", "live": "observe"},
                           "caps": "opt-in", "secrets": "masked", "approvals": "off", "data": "declared"},
            "script": {"compose": "ro", "egress": "packages", "podman": {"tier": "none"}, "caps": "refused",
                       "secrets": "masked", "approvals": "off", "data": "declared"},
            "research": {"compose": "ro", "egress": "open", "podman": {"tier": "none"}, "caps": "refused",
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

    def test_an_open_preset_is_fail_closed_by_live_reference(self):
        """An open preset declares `egress = "open"` in its definition, but a project is only actually open
        when its own registry entry says so: a hand-written entry with preset = "research" and no egress key
        resolves to `packages`, by design (F8). The registration path materialises the key explicitly."""
        for name in ("research", "build-open"):
            value, source = self.eff({"preset": name})["egress"]
            self.assertEqual(value, "packages", f"{name}: fail-closed without an entry of its own")
            self.assertIn("open only when the project says so", source)
        # with the key on the entry (what init/add/set write), it is really open
        self.assertEqual(self.eff({"preset": "research", "egress": "open"})["egress"], ("open", "project"))

    def test_podman_live_is_validated(self):
        for bad in ("obsrve", "dev", "yes"):
            with self.assertRaises(AgentboxError, msg=bad):
                self.eff({"preset": "python", "podman": {"tier": "none", "live": bad}})
        self.eff({"preset": "python", "podman": {"tier": "none", "live": "observe"}})

    def test_the_exec_tier_is_refused_at_validation(self):
        """F7: exec is not built. It used to be refused only when a session was being prepared, so
        `ab project add --podman exec` registered happily and failed later, at `ab run`."""
        for value in ({"tier": "exec"}, {"tier": "none", "live": "exec"}, {"tier": "dev", "live": "exec"}):
            with self.assertRaises(AgentboxError, msg=value) as cm:
                self.eff({"preset": "python", "podman": value})
            self.assertIn("exec on the live stack is not built", str(cm.exception))

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


class MaterializeOpenEgressTest(TempCase):
    """The open default is an attribute of the preset definition (its own `egress = "open"`), not a name
    list, so a user's own research.toml that says locked stays locked (F8b)."""

    def test_a_preset_declaring_open_gets_egress_written_when_absent(self):
        for name in ("research", "build-open"):
            entry = {"preset": name}
            presets.materialize_open_egress(entry, presets.BUILTIN)
            self.assertEqual(entry["egress"], "open", name)

    def test_an_explicit_egress_is_left_alone(self):
        entry = {"preset": "research", "egress": "packages"}
        presets.materialize_open_egress(entry, presets.BUILTIN)
        self.assertEqual(entry["egress"], "packages")

    def test_presets_that_do_not_declare_open_get_no_egress_written(self):
        for name in ("build", "script", "lockdown", "operate", "analysis-offline"):
            entry = {"preset": name}
            presets.materialize_open_egress(entry, presets.BUILTIN)
            self.assertNotIn("egress", entry, name)

    def test_a_user_preset_that_says_locked_stays_locked(self):
        """F8b, the case a name list got wrong: the user's own research.toml is the definition that counts."""
        self.write("presets/research.toml", 'egress = "locked"\n')
        loaded = presets.load_presets(os.path.join(self.tmp, "presets"))
        entry = {"preset": "research"}
        presets.materialize_open_egress(entry, loaded)
        self.assertNotIn("egress", entry, "no open egress written for a preset that says locked")
        self.assertEqual(presets.effective("p", entry, loaded)["egress"][0], "locked")

    def test_an_unknown_preset_name_writes_nothing(self):
        entry = {"preset": "nope"}
        presets.materialize_open_egress(entry, presets.BUILTIN)
        self.assertNotIn("egress", entry)


class CustomPresetDefaultsTest(TempCase):
    """F8a: a brand-new user preset inherits lockdown's restrictive values for every key it omits. It used
    to inherit BUILTIN["python"], so a minimal file silently got packages egress and approvals off."""

    def test_a_new_preset_inherits_restrictive_defaults(self):
        self.write("presets/web.toml", 'egress = "packages"\n')
        ps = presets.load_presets(os.path.join(self.tmp, "presets"))
        self.assertEqual(ps["web"]["egress"], "packages", "what the file says")
        for key, value in (("compose", "ro"), ("caps", "refused"), ("secrets", "locked"),
                           ("approvals", "on"), ("data", "ro-only")):
            self.assertEqual(ps["web"][key], value, f"{key}: lockdown-equivalent default")
        self.assertEqual(ps["web"]["podman"], {"tier": "none"})

    def test_a_file_named_after_a_builtin_still_customises_that_builtin(self):
        self.write("presets/build.toml", 'egress = "locked"\n')
        ps = presets.load_presets(os.path.join(self.tmp, "presets"))
        self.assertEqual(ps["build"]["egress"], "locked")
        self.assertEqual(ps["build"]["compose"], "rw", "the rest of build's own shape is kept")
        self.assertEqual(ps["build"]["podman"], {"tier": "dev", "live": "observe"})


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


class ExecTierRefusedEndToEnd(SettingsCase):
    """F7 at the registering surfaces, not just when a session is prepared."""

    def test_refused_at_add(self):
        root = self.mkdir("xroot")
        self.assertEqual(cli.main(["project", "add", "x1", root, "--preset", "build",
                                   "--podman", "exec"]), 2)
        self.assertNotIn("x1", registry.load(self.settings.registry_path))

    def test_refused_at_set(self):
        self.project("x2", preset="build")
        self.assertEqual(cli.main(["project", "set", "x2", "--podman", "exec"]), 2)
        self.assertNotIn("podman", registry.load(self.settings.registry_path)["x2"])

    def test_refused_at_init_before_scaffolding(self):
        repos = self.mkdir("repos")
        os.environ["AB_REPOS_DIR"] = repos
        self.addCleanup(os.environ.pop, "AB_REPOS_DIR", None)
        from agentbox import config
        settings = config.load()
        self.assertEqual(cli.main(["init", "pod", "--name", "x3", "--podman", "exec"]), 2)
        self.assertFalse(os.path.exists(os.path.join(repos, "x3")), "nothing written")
        self.assertNotIn("x3", registry.load(settings.registry_path))


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
        cleared = cli._apply_project_settings(entry, ns(preset="analysis-offline"), GRANT_KW, presets.BUILTIN)
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
        cleared = cli._apply_project_settings(entry, ns(preset="build", egress="locked"), GRANT_KW, presets.BUILTIN)
        self.assertEqual(entry["egress"], "locked", "the flag given now wins over the clearing")
        self.assertNotIn("egress", cleared)
        self.assertEqual(cleared, ["observe_logs"])

    def test_switching_to_an_open_preset_applies_its_open_egress(self):
        entry = {"root": "/r", "preset": "script", "egress": "locked"}
        cleared = cli._apply_project_settings(entry, ns(preset="research"), GRANT_KW, presets.BUILTIN)
        self.assertEqual(cleared, ["egress"])
        self.assertEqual(entry["egress"], "open", "cleared, then research's own open default applied")
        self.assertEqual(presets.effective("p", entry, presets.BUILTIN)["egress"], ("open", "project"))

    def test_keeping_the_same_preset_clears_nothing(self):
        entry = {"root": "/r", "preset": "build", "egress": "open", "observe_logs": True}
        self.assertEqual(cli._apply_project_settings(entry, ns(preset="build"), GRANT_KW, presets.BUILTIN), [])
        self.assertEqual(entry["egress"], "open")
        self.assertEqual(entry["observe_logs"], True)

    def test_a_new_project_has_nothing_to_clear(self):
        entry = {"root": "/r"}
        self.assertEqual(cli._apply_project_settings(entry, ns(preset="build"), GRANT_KW, presets.BUILTIN), [])
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
