"""`ab project set`: change a registered project in place, instead of `ab project rm` + `ab project add`
(F5/F9). It uses the same shared flag definitions as `ab init`/`ab project add`, re-runs the same validation
(preset/lockdown ceiling, build_memory, grantcheck, history secrets), and refuses while a session for the
project is live. `--unset KEY` returns a setting to its preset value; `--clear-<list>` empties a list one.
"""
import os
from unittest import mock

from agentbox import cli, registry, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase

ALIVE = {"ab-claude-p1-1": {"alive": True, "meta": {"project": "p1"}}}


class SetTest(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("p1", preset="python")

    def entry(self, name="p1"):
        return registry.load(self.settings.registry_path)[name]

    def eff(self, key, name="p1"):
        return session.load_project(self.settings, name).eff[key]

    def rc(self, *argv):
        """Exit code of an `ab` run, whether argparse refused it (SystemExit) or a check did (returns 2)."""
        try:
            return cli.main(list(argv))
        except SystemExit as e:
            return e.code

    def test_unknown_project(self):
        self.assertEqual(self.rc("project", "set", "nope", "--egress", "locked"), 2)

    def test_replaces_a_scalar_setting_when_given(self):
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "locked"), 0)
        self.assertEqual(self.entry()["egress"], "locked")
        self.assertEqual(self.eff("egress"), ("locked", "project"))

    def test_leaves_unset_fields_untouched(self):
        other = self.mkdir("outside")
        self.project("p1", preset="python", data_ro=[other])
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "locked"), 0)
        self.assertEqual(self.entry()["data_ro"], [other], "untouched: --data-ro was not given")

    def test_replaces_the_whole_list_when_a_list_flag_is_given(self):
        old, new = self.mkdir("olddata"), self.mkdir("newdata")
        self.project("p1", preset="python", data_ro=[old])
        self.assertEqual(self.rc("project", "set", "p1", "--data-ro", new), 0)
        self.assertEqual(self.entry()["data_ro"], [new])

    def test_observe_logs_can_be_turned_back_off(self):
        self.project("p1", preset="pods", observe_logs=True)
        self.assertEqual(self.rc("project", "set", "p1", "--no-observe-logs"), 0)
        self.assertEqual(self.entry()["observe_logs"], False)

    def test_choosing_an_open_preset_writes_egress_explicitly(self):
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "research"), 0)
        e = self.entry()
        self.assertEqual(e["preset"], "research")
        self.assertEqual(e["egress"], "open")
        self.assertEqual(self.eff("egress"), ("open", "project"), "project-sourced, not a live preset ref")

    def test_switching_preset_clears_the_stale_open_override(self):
        """F2 end to end: the open egress `research` wrote must not outlive it."""
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "research"), 0)
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "analysis-offline"), 0)
        self.assertNotIn("egress", self.entry())
        self.assertEqual(self.eff("egress")[0], "locked", "analysis-offline really is offline")

    def test_switching_to_lockdown_clears_what_would_have_hit_the_ceiling(self):
        self.project("p1", preset="pods", observe_logs=True, egress="open")
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "lockdown"), 0)
        e = self.entry()
        self.assertNotIn("egress", e)
        self.assertNotIn("observe_logs", e)
        self.assertEqual(self.eff("egress")[0], "locked")

    # --- F9: --unset and --clear-<list> ---

    def test_unset_returns_a_key_to_its_preset_value(self):
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "locked"), 0)
        self.assertEqual(self.rc("project", "set", "p1", "--unset", "egress"), 0)
        self.assertNotIn("egress", self.entry())
        self.assertEqual(self.eff("egress"), ("packages", "preset python"))

    def test_unset_takes_several_keys(self):
        self.project("p1", preset="pods", observe_logs=True, build_memory="4g")
        self.assertEqual(self.rc("project", "set", "p1", "--unset", "observe_logs",
                                   "--unset", "build_memory"), 0)
        e = self.entry()
        self.assertNotIn("observe_logs", e)
        self.assertNotIn("build_memory", e)

    def test_unset_rejects_a_key_that_is_not_a_setting(self):
        for bad in ("root", "preset", "nonsense"):
            self.assertEqual(self.rc("project", "set", "p1", "--unset", bad), 2, bad)

    def test_unset_and_the_same_flag_in_one_command_is_refused(self):
        self.assertEqual(self.rc("project", "set", "p1", "--unset", "egress", "--egress", "open"), 2)
        self.assertNotIn("egress", self.entry(), "refused: nothing saved")

    def test_unsetting_egress_on_an_open_preset_does_not_write_it_back(self):
        """--unset means "no override of my own", so the fail-closed resolution applies: a preset-sourced
        open demotes to packages. Materialising it again would make --unset a no-op on exactly the presets
        where it matters most."""
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "research"), 0)
        self.assertEqual(self.entry()["egress"], "open")
        self.assertEqual(self.rc("project", "set", "p1", "--unset", "egress"), 0)
        self.assertNotIn("egress", self.entry())
        self.assertEqual(self.eff("egress")[0], "packages")

    def test_clear_flags_empty_the_list_settings(self):
        ro, rw = self.mkdir("ro"), self.mkdir("rw")
        self.project("p1", preset="pods", data_ro=[ro], data_rw=[rw], extra_domains=["x.com"],
                     dev_images=["localhost/x:*"])
        rc = self.rc("project", "set", "p1", "--clear-data-ro", "--clear-data-rw",
                       "--clear-extra-domains", "--clear-dev-images")
        self.assertEqual(rc, 0)
        e = self.entry()
        for key in ("data_ro", "data_rw", "extra_domains", "dev_images"):
            self.assertNotIn(key, e, key)

    # --- re-validation and the live-session guard ---

    def test_reruns_grantcheck_and_refuses_on_a_new_finding(self):
        with open(os.path.join(self.root, "data.db"), "wb") as f:
            f.write(b"x" * (2 << 20))
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "locked"), 2)
        self.assertNotIn("egress", self.entry(), "the refused set must not have saved")
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "locked",
                                   "--i-understand-grant-exposure"), 0)
        self.assertEqual(self.entry()["egress"], "locked")
        self.assertIn("grant_exposure_accepted", self.entry())

    def test_revalidates_the_preset_ceiling(self):
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "lockdown", "--observe-logs"), 2)
        self.assertEqual(self.entry()["preset"], "python", "refused: nothing saved")

    def test_rejects_an_unknown_preset(self):
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "nope"), 2)

    def test_refuses_while_a_session_is_live(self):
        with mock.patch("agentbox.session.sessions", return_value=ALIVE):
            self.assertEqual(self.rc("project", "set", "p1", "--egress", "open"), 2)
        self.assertNotIn("egress", self.entry(), "refused: nothing saved")

    def test_the_live_refusal_names_the_session(self):
        with mock.patch("agentbox.session.sessions", return_value=ALIVE):
            with self.assertRaises(AgentboxError) as cm:
                cli.cmd_project_set(self.settings, _ns(name="p1", egress="open"))
        self.assertIn("ab-claude-p1-1", str(cm.exception))
        self.assertIn("ab session kill", str(cm.exception))

    def test_succeeds_once_the_session_is_gone(self):
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "locked"), 0)
        self.assertEqual(self.entry()["egress"], "locked")


def _ns(**kw):
    """A parsed `ab project set` namespace, defaults taken from the real parser."""
    import argparse
    p = argparse.ArgumentParser()
    cli._add_project_setting_args(p, preset_required=False)
    cli._add_project_unset_args(p)
    a = p.parse_args([])
    for k, v in kw.items():
        setattr(a, k, v)
    return a
