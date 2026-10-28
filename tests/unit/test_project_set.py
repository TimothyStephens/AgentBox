"""`ab project set`: change a registered project in place, instead of `ab project rm` + `ab project add`
(F5/F9). It uses the same shared flag definitions as `ab init`/`ab project add`, re-runs the same validation
(preset/lockdown ceiling, build_memory, grantcheck, history secrets), and refuses while a session for the
project is live. `--unset KEY` returns a setting to its preset value; `--clear-<list>` empties a list one.
"""
import json
import os

from agentbox import cli, registry, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase

# What `podman ps --format json` reports for a running container of p1. The liveness guard reads the
# project from the container label, so no session.json is needed to make a session "live" (audit F2).
ALIVE = json.dumps([{"Names": ["ab-claude-p1-1-agent"], "State": "running",
                     "Labels": {"agentbox.project": "p1", "agentbox.session": "ab-claude-p1-1",
                                "agentbox.role": "agent", "agentbox-test": "1"}}])


class SetBase(SettingsCase):
    """Fixture and helpers only, so a second test class can reuse them without inheriting the cases."""

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

    def rc_out(self, *argv):
        """As rc(), plus what the command printed."""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.rc(*argv)
        return code, buf.getvalue()


class SetTest(SetBase):
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

    def test_moving_onto_the_canonical_preset_name_keeps_the_overrides(self):
        """`pods` -> `build` is a rename, not a change of posture: migrating a project to the canonical
        name must not silently drop its egress override."""
        self.project("p1", preset="pods", egress="open", observe_logs=True)
        code, out = self.rc_out("project", "set", "p1", "--preset", "build")
        self.assertEqual(code, 0)
        e = self.entry()
        self.assertEqual(e["preset"], "build")
        self.assertEqual(e["egress"], "open")
        self.assertEqual(e["observe_logs"], True)
        self.assertNotIn("cleared", out)
        self.assertEqual(self.eff("egress"), ("open", "project"))

    def test_switching_to_lockdown_clears_what_would_have_hit_the_ceiling(self):
        self.project("p1", preset="pods", observe_logs=True, egress="open")
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "lockdown"), 0)
        e = self.entry()
        self.assertNotIn("egress", e)
        self.assertNotIn("observe_logs", e)
        self.assertEqual(self.eff("egress")[0], "locked")

    # --- F7: a preset change reports what it replaced, and whether that loosened it ---

    def test_a_preset_change_that_loosens_says_so(self):
        """F7: "cleared: egress" did not say that the project had just gone from locked to open."""
        self.project("p1", preset="script", egress="locked")
        code, out = self.rc_out("project", "set", "p1", "--preset", "research")
        self.assertEqual(code, 0)
        self.assertIn("egress: locked → open", out)
        self.assertIn("loosened", out)

    def test_a_preset_change_that_tightens_says_so(self):
        self.project("p1", preset="research", egress="open")
        code, out = self.rc_out("project", "set", "p1", "--preset", "analysis-offline")
        self.assertEqual(code, 0)
        self.assertIn("egress: open → locked", out)
        self.assertIn("tightened", out)

    def test_an_unordered_setting_is_reported_as_cleared(self):
        self.project("p1", preset="build", extra_domains=["example.com"], compose_project="shop")
        code, out = self.rc_out("project", "set", "p1", "--preset", "script")
        self.assertEqual(code, 0)
        self.assertIn("extra_domains: example.com → (none)", out)
        self.assertIn("cleared", out)

    def test_observe_logs_is_reported_as_a_tightening(self):
        self.project("p1", preset="build", observe_logs=True)
        code, out = self.rc_out("project", "set", "p1", "--preset", "script")
        self.assertEqual(code, 0)
        self.assertIn("observe_logs: true → false", out)
        self.assertIn("tightened", out)

    # --- F1: open egress is written only when an open preset is NEWLY chosen ---

    def test_an_unrelated_set_never_writes_egress(self):
        """F1: materialising the open default ran on every edit, so `ab project set p1 --build-memory 4g`
        on a hand-written research entry silently opened the project's egress. Only newly choosing an open
        preset may write it."""
        self.project("p1", preset="research")                 # hand-written: no egress key of its own
        self.assertEqual(self.rc("project", "set", "p1", "--build-memory", "4g"), 0)
        self.assertNotIn("egress", self.entry(), "an unrelated set must not write egress")
        self.assertEqual(self.eff("egress")[0], "packages", "still fail-closed")

    def test_re_choosing_the_same_open_preset_does_not_write_egress(self):
        self.project("p1", preset="research")
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "research"), 0)
        self.assertNotIn("egress", self.entry(), "not newly chosen: nothing to materialise")

    def test_newly_choosing_an_open_preset_does_write_egress(self):
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "build-open"), 0)
        self.assertEqual(self.entry()["egress"], "open")

    def test_the_accept_commands_never_write_egress(self):
        self.project("p1", preset="research")
        for cmd in ("accept-grant-exposure", "accept-history-secrets"):
            self.assertEqual(self.rc("project", cmd, "p1"), 0, cmd)
            self.assertNotIn("egress", self.entry(), cmd)

    # --- F9: --unset and --clear-<list> ---

    def test_unset_returns_a_key_to_its_preset_value(self):
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "locked"), 0)
        self.assertEqual(self.rc("project", "set", "p1", "--unset", "egress"), 0)
        self.assertNotIn("egress", self.entry())
        self.assertEqual(self.eff("egress"), ("packages", "preset python"))

    def test_unset_on_an_absent_key_says_nothing_changed(self):
        """F10: it used to report "unset (back to the preset): egress" whether or not the project had an
        egress override, so the output claimed a change that never happened."""
        code, out = self.rc_out("project", "set", "p1", "--unset", "egress")
        self.assertEqual(code, 0)
        self.assertIn("nothing to unset", out)
        self.assertNotIn("unset (back to the preset): egress", out)
        # and when it IS stored, it really is unset and says so
        self.rc("project", "set", "p1", "--egress", "locked")
        code, out = self.rc_out("project", "set", "p1", "--unset", "egress")
        self.assertIn("unset (back to the preset): egress", out)
        self.assertNotIn("nothing to unset", out)

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

    # --- F11: the WHOLE entry is validated before saving, and a failed set writes nothing ---

    def test_an_unknown_stored_key_is_refused_where_it_is_written(self):
        """F11: `set` validated only what the command touched, so an unknown key (a typo in a hand edit)
        survived the save and only exploded at the next `ab run`."""
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["egres"] = "open"                       # typo
        registry.save(self.settings.registry_path, reg)
        before = open(self.settings.registry_path, "rb").read()
        code = self.rc("project", "set", "p1", "--build-memory", "4g")
        self.assertEqual(code, 2)
        self.assertEqual(open(self.settings.registry_path, "rb").read(), before,
                         "refused BEFORE saving, not after the entry was already written")
        with self.assertRaises(AgentboxError) as cm:
            session.load_project(self.settings, "p1")     # load is just as strict
        self.assertIn("egres", str(cm.exception))

    def test_a_stored_value_the_command_did_not_touch_is_validated(self):
        for bad in ({"extra_domains": ["not a domain"]}, {"swap": {"notes.txt": ".env"}},
                    {"mask_skip": [""]}, {"dev_images": ["NOT VALID"]},
                    {"observe_logs": "yes"}):
            with self.subTest(bad=bad):
                self.project("p1", preset="build", **bad)
                before = open(self.settings.registry_path, "rb").read()
                self.assertEqual(self.rc("project", "set", "p1", "--build-memory", "4g"), 2)
                self.assertEqual(open(self.settings.registry_path, "rb").read(), before,
                                 "refused before saving")
                with self.assertRaises(AgentboxError):
                    session.load_project(self.settings, "p1")

    def test_a_failed_set_writes_nothing(self):
        before = open(self.settings.registry_path, "rb").read()
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "lockdown", "--observe-logs"), 2)
        self.assertEqual(open(self.settings.registry_path, "rb").read(), before, "registry untouched")

    def test_a_stored_grant_is_rechecked_on_every_set(self):
        """A hand-written data_ro pointing at a protected path is caught by the next `set`, not just at
        session start."""
        self.project("p1", preset="script", data_ro=[os.path.join(os.path.expanduser("~"), ".ssh")])
        self.assertEqual(self.rc("project", "set", "p1", "--build-memory", "4g"), 2)

    # --- re-validation and the live-session guard ---

    def test_reruns_grantcheck_and_refuses_on_a_new_finding(self):
        """An edit that GRANTS something new is refused while the grant holds an unacknowledged finding.
        (An edit that grants nothing new is not: see AcknowledgementsOnSet, audit F-5.)"""
        with open(os.path.join(self.root, "data.db"), "wb") as f:
            f.write(b"x" * (2 << 20))
        self.assertEqual(self.rc("project", "set", "p1", "--podman", "dev"), 2)
        self.assertNotIn("podman", self.entry(), "the refused set must not have saved")
        self.assertEqual(self.rc("project", "set", "p1", "--podman", "dev",
                                   "--i-understand-grant-exposure"), 0)
        self.assertEqual(self.entry()["podman"], {"tier": "dev"})
        self.assertIn("grant_exposure_accepted", self.entry())

    def test_a_read_only_data_grant_warns_but_does_not_block(self):
        """F4: read-only data grants stay allowed -- they are what analysis-offline and operate are for --
        but what they hold is now reported instead of invisible."""
        import io
        from contextlib import redirect_stderr
        ro = self.mkdir("rodata")
        with open(os.path.join(ro, "customers.db"), "wb") as f:
            f.write(b"x" * (2 << 20))
        err = io.StringIO()
        with redirect_stderr(err):
            rc = self.rc("project", "set", "p1", "--preset", "analysis-offline", "--data-ro", ro)
        self.assertEqual(rc, 0, "allowed")
        self.assertIn("customers.db", err.getvalue())
        self.assertIn("read-only data grant", err.getvalue())
        self.assertEqual(self.entry()["data_ro"], [ro])
        self.assertNotIn("grant_exposure_accepted", self.entry(), "a warning is not an exposure ack")

    def test_revalidates_the_preset_ceiling(self):
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "lockdown", "--observe-logs"), 2)
        self.assertEqual(self.entry()["preset"], "python", "refused: nothing saved")

    def test_rejects_an_unknown_preset(self):
        self.assertEqual(self.rc("project", "set", "p1", "--preset", "nope"), 2)

    def test_refuses_while_a_session_is_live(self):
        os.environ["FAKE_PODMAN_PSJSON"] = ALIVE
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "open"), 2)
        self.assertNotIn("egress", self.entry(), "refused: nothing saved")

    def test_the_live_refusal_names_the_session(self):
        os.environ["FAKE_PODMAN_PSJSON"] = ALIVE
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        with self.assertRaises(AgentboxError) as cm:
            cli.cmd_project_set(self.settings, _ns(name="p1", egress="open"))
        self.assertIn("ab-claude-p1-1", str(cm.exception))
        self.assertIn("ab session kill", str(cm.exception))

    def test_succeeds_once_the_session_is_gone(self):
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "locked"), 0)
        self.assertEqual(self.entry()["egress"], "locked")


class AcknowledgementsOnSet(SetBase):
    """F-5: `set` re-ran the grant scan but ignored what had already been accepted, so EVERY edit to a
    project with an accepted exposure demanded --i-understand-grant-exposure again -- including
    `--egress locked`, which only makes the project safer. Now:

      * the findings still match the acceptance    -> the edit proceeds, no flag;
      * they changed and the edit loosens nothing  -> it still proceeds, with the change reported;
      * they changed and the edit loosens something-> refused, naming what changed;
      * --i-understand-grant-exposure              -> re-accepts, printing what is NEWLY accepted.

    A tightening is never blocked: making a project safer must never be gated on an acknowledgement. The
    session's own check is unchanged, so an unacknowledged exposure still refuses to RUN.
    """

    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.root, "data.db")
        with open(self.db, "wb") as f:
            f.write(b"x" * (2 << 20))

    def accept(self):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.rc("project", "accept-grant-exposure", "p1"), 0)
        return self.entry()["grant_exposure_accepted"]

    def second_finding(self):
        """A second finding. Not a `*.pem`: the masks cover those, so the scan rightly ignores them."""
        with open(os.path.join(self.root, "other.db"), "wb") as f:
            f.write(b"x" * (2 << 20))

    def err(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        buf, out = io.StringIO(), io.StringIO()
        with redirect_stderr(buf), redirect_stdout(out):
            code = self.rc(*argv)
        return code, buf.getvalue() + out.getvalue()

    # --- the acceptance still describes the grant ---

    def test_an_unchanged_exposure_needs_no_flag_and_keeps_the_acceptance(self):
        ack = self.accept()
        self.assertEqual(self.rc("project", "set", "p1", "--build-memory", "4g"), 0)
        self.assertEqual(self.entry()["grant_exposure_accepted"], ack, "unchanged, so untouched")
        self.assertEqual(self.entry()["build_memory"], "4g")

    def test_a_loosening_edit_proceeds_too_while_the_acceptance_matches(self):
        self.accept()
        self.assertEqual(self.rc("project", "set", "p1", "--podman", "dev"), 0)

    # --- the acceptance no longer describes the grant ---

    def test_a_changed_exposure_refuses_a_loosening_and_says_what_changed(self):
        self.accept()
        self.second_finding()
        code, text = self.err("project", "set", "p1", "--podman", "dev")
        self.assertEqual(code, 2)
        self.assertIn("other.db", text, "the new finding is named")
        self.assertIn("new since", text.lower())
        self.assertNotIn("podman", self.entry(), "refused: nothing saved")

    def test_a_finding_that_went_away_is_named_too(self):
        self.accept()
        os.unlink(self.db)
        self.second_finding()
        code, text = self.err("project", "set", "p1", "--podman", "dev")
        self.assertEqual(code, 2)
        self.assertIn("data.db", text)
        self.assertIn("no longer", text.lower())

    def test_an_edited_finding_is_reported_as_changed_not_new(self):
        self.accept()
        with open(self.db, "wb") as f:
            f.write(b"y" * (3 << 20))               # same path, new content
        code, text = self.err("project", "set", "p1", "--podman", "dev")
        self.assertEqual(code, 2)
        self.assertIn("data.db", text)
        self.assertIn("changed since", text.lower())

    def test_re_accepting_takes_the_flag_and_prints_what_is_newly_accepted(self):
        ack = self.accept()
        self.second_finding()
        code, text = self.err("project", "set", "p1", "--podman", "dev",
                              "--i-understand-grant-exposure")
        self.assertEqual(code, 0)
        self.assertIn("other.db", text, "what is newly accepted is printed")
        self.assertNotEqual(self.entry()["grant_exposure_accepted"], ack, "the acceptance was updated")
        self.assertEqual(self.entry()["podman"], {"tier": "dev"})

    # --- a tightening is never blocked ---

    def test_tightening_egress_works_with_an_unacknowledged_exposure(self):
        """The whole point of F-5: `--egress locked` makes the project safer, so nothing may gate it."""
        code, text = self.err("project", "set", "p1", "--egress", "locked")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.entry()["egress"], "locked")
        self.assertNotIn("grant_exposure_accepted", self.entry(), "proceeding is not accepting")
        self.assertIn("data.db", text, "the exposure is still reported")
        self.assertIn("will refuse", text.lower(), "and a session still refuses to start")

    def test_tightening_works_when_the_exposure_changed_under_an_acceptance(self):
        self.accept()
        self.second_finding()
        code, text = self.err("project", "set", "p1", "--egress", "locked")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.entry()["egress"], "locked")
        self.assertIn("other.db", text)

    def test_dropping_a_grant_is_a_tightening(self):
        extra = self.mkdir("extra")
        self.assertEqual(self.rc("project", "set", "p1", "--data-rw", extra,
                                 "--i-understand-grant-exposure"), 0)
        self.second_finding()                                   # the acceptance no longer matches
        self.assertEqual(self.rc("project", "set", "p1", "--clear-data-rw"), 0, "a grant removed")
        self.assertEqual(self.entry().get("data_rw", []), [])

    def test_adding_a_grant_is_a_loosening(self):
        extra = self.mkdir("extra")
        self.assertEqual(self.rc("project", "set", "p1", "--data-rw", extra), 2,
                         "a new grant with an unacknowledged exposure is refused")
        self.assertNotIn("data_rw", self.entry())

    # --- add is unchanged: there is no "before" to be safer than ---

    def test_add_still_demands_the_flag(self):
        root = self.mkdir("p2root")
        with open(os.path.join(root, "live.db"), "wb") as f:
            f.write(b"x" * (2 << 20))
        self.assertEqual(self.rc("project", "add", "p2", root, "--preset", "script"), 2)
        self.assertEqual(self.rc("project", "add", "p2", root, "--preset", "script",
                                 "--i-understand-grant-exposure"), 0)

    def test_a_session_still_refuses_on_an_exposure_set_let_through(self):
        self.token()
        self.assertEqual(self.rc("project", "set", "p1", "--egress", "locked"), 0)
        with self.assertRaises(AgentboxError) as cm:
            session.make_session(self.settings, "claude",
                                 session.load_project(self.settings, "p1")).prepare(do_backup=False)
        self.assertIn("data.db", str(cm.exception))


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
