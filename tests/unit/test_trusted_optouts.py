"""The two trusted opt-outs, `data_env` and `data_bind` (decisions.md, 19 Nov 2026): project settings for an agent
the user trusts with real secrets or live data. They partly reverse 16 Oct ("masks can no longer be turned off by
any route") and replace the 6 Oct `--data-bind` contract.

  data_env  = on: the env files (.env, .env.*, *.env) of the project ROOT grant are the real ones (no mask, no dev
              swap); .netrc, .pgpass, key files, secrets/ and every env file in a data grant stay masked.
  data_bind = on: <root>/data is visible and writable; grantcheck's live-data refusals do not apply under it, and
              the pre-session snapshot and the end-of-session scan leave it out.
"""
import json
import os
import subprocess
from unittest import mock

from agentbox import grantcheck, lifecycle, mounts, projectkeys, registry, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, read_file


def _git(root, *args):
    subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


class TheSettings(SettingsCase):
    def test_both_are_ordered_off_on_security_keys_a_preset_change_clears(self):
        for name in ("data_env", "data_bind"):
            with self.subTest(key=name):
                k = projectkeys.BY_NAME[name]
                self.assertEqual((k.kind, k.values, k.security, k.clears, k.unsettable, k.display),
                                 ("project", ("off", "on"), True, True, True, "always"))
                self.assertTrue(projectkeys.is_looser(name, "on", "off"))

    def test_a_bad_value_is_refused(self):
        self.project("p1", data_env="yes")
        with self.assertRaises(AgentboxError):
            session.load_project(self.settings, "p1")

    def test_default_is_off(self):
        self.project("p1")
        p = session.load_project(self.settings, "p1")
        self.assertEqual((p.data_env, p.data_bind), ("off", "off"))


class Refusals(SettingsCase):
    def refused(self, **extra):
        self.project("p1", **extra)
        with self.assertRaises(AgentboxError) as cm:
            session.load_project(self.settings, "p1")
        return str(cm.exception)

    def test_lockdown_refuses_both(self):
        for key in ("data_env", "data_bind"):
            with self.subTest(key=key):
                msg = self.refused(preset="lockdown", **{key: "on"})
                self.assertIn("ceiling", msg)
                self.assertIn("agent you trust", msg)

    def test_lockdown_accepts_them_off(self):
        self.project("p1", preset="lockdown", data_env="off", data_bind="off")
        session.load_project(self.settings, "p1")

    def test_data_bind_is_refused_with_a_read_only_root(self):
        self.assertIn("read-only", self.refused(preset="planner", data_bind="on"))

    def test_data_env_is_allowed_with_a_read_only_root(self):
        self.project("p1", preset="planner", data_env="on")
        self.assertEqual(session.load_project(self.settings, "p1").data_env, "on")

    def test_data_bind_is_refused_by_a_read_only_data_preset(self):
        self.assertIn("ro-only", self.refused(preset="analysis-offline", data_bind="on"))

    def test_clone_mode_refuses_both(self):
        for key in ("data_env", "data_bind"):
            with self.subTest(key=key):
                self.assertIn("clone", self.refused(mount="clone", **{key: "on"}))

    def test_toulmin_on_a_git_root_means_clone_mode_and_is_refused(self):
        self.assertIn("clone", self.refused(toulmin=True, data_env="on"))

    def test_toulmin_with_checkout_mode_is_allowed(self):
        self.project("p1", toulmin=True, mount="checkout", data_env="on")
        session.load_project(self.settings, "p1")

    def test_a_mask_over_data_is_refused_with_data_bind(self):
        for m in ("data", "./data", "./data/"):
            with self.subTest(mask=m):
                self.assertIn("mask", self.refused(data_bind="on", mask=[m]))

    def test_a_mask_on_another_path_is_fine(self):
        self.project("p1", data_bind="on", mask=["./other/data"])
        session.load_project(self.settings, "p1")


class TheCli(SettingsCase):
    def test_add_set_and_show(self):
        root = self.mkdir("web")
        code, out = self.ab("project", "add", "web", root, "--preset", "script", "--data-env", "on")
        self.assertEqual(code, 0, out)
        self.assertEqual(registry.load(self.settings.registry_path)["web"]["data_env"], "on")
        [line] = [ln for ln in out.splitlines() if ln.strip().startswith("data_env ")]
        self.assertIn("WARNING", line)
        self.assertIn("only for an agent you trust", line)
        [line] = [ln for ln in out.splitlines() if ln.strip().startswith("data_bind ")]
        self.assertIn("off", line)
        self.assertNotIn("WARNING", line)
        code, out = self.ab("project", "set", "web", "--data-bind", "on")
        self.assertEqual(code, 0, out)
        [line] = [ln for ln in out.splitlines() if ln.strip().startswith("data_bind ")]
        self.assertIn("WARNING", line)
        self.assertIn("yours to back up", line)
        code, out = self.ab("project", "set", "web", "--data-env", "off")
        self.assertEqual(code, 0, out)
        self.assertEqual(registry.load(self.settings.registry_path)["web"]["data_env"], "off")

    def test_add_under_lockdown_is_refused_and_nothing_is_saved(self):
        root = self.mkdir("web")
        code, out = self.ab("project", "add", "web", root, "--preset", "lockdown", "--data-env", "on")
        self.assertNotEqual(code, 0)
        self.assertIn("ceiling", out)
        self.assertNotIn("web", registry.load(self.settings.registry_path))

    def test_set_refuses_data_bind_on_a_read_only_root(self):
        root = self.mkdir("web")
        self.assertEqual(self.ab("project", "add", "web", root, "--preset", "planner")[0], 0)
        code, out = self.ab("project", "set", "web", "--data-bind", "on")
        self.assertNotEqual(code, 0)
        self.assertIn("read-only", out)
        self.assertNotIn("data_bind", registry.load(self.settings.registry_path)["web"])

    def test_set_refuses_clone_mode(self):
        root = self.mkdir("web")
        self.assertEqual(self.ab("project", "add", "web", root, "--preset", "script", "--mount", "clone")[0], 0)
        code, out = self.ab("project", "set", "web", "--data-env", "on")
        self.assertNotEqual(code, 0)
        self.assertIn("clone", out)

    def test_a_preset_change_clears_them(self):
        root = self.mkdir("web")
        self.assertEqual(self.ab("project", "add", "web", root, "--preset", "script", "--data-env", "on")[0], 0)
        code, out = self.ab("project", "set", "web", "--preset", "build")
        self.assertEqual(code, 0, out)
        self.assertNotIn("data_env", registry.load(self.settings.registry_path)["web"])
        self.assertIn("data_env", out)

    def test_the_help_of_both_flags_carries_the_warning(self):
        code, out = self.ab("project", "add", "--help")
        flat = " ".join(out.split())
        for flag in ("--data-env", "--data-bind"):
            self.assertIn(flag, flat)
        self.assertEqual(flat.count("only for an agent you trust: the agent can read/print/send these secrets; "
                                    "masks are the safety net you are turning off"), 2, flat)
        self.assertIn("*.pem", flat[flat.index("--data-env on|off"):])


class ThePlan(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")
        self.dw = self.mkdir("dw")
        for rel in ("proj/.env", "proj/.env.dev", "proj/sub/prod.env", "proj/.env.prod", "proj/.netrc",
                    "proj/tls.key", "proj/secrets/token", "dw/.env", "dw/.env.dev"):
            self.write(rel, "X=1\n")

    def plan(self, data_env):
        return mounts.plan(root=self.root, data_rw=[self.dw], swap=mounts.effective_swap(None), data_env=data_env)

    def test_data_env_shows_the_roots_env_files_and_nothing_else(self):
        by = {m.dst: m for m in self.plan(True).mounts}
        for rel in (".env", "sub/prod.env", ".env.prod"):
            self.assertNotIn(os.path.join(self.root, rel), by, f"{rel}: real, so neither masked nor swapped")
        self.assertEqual(by[os.path.join(self.root, ".netrc")].kind, "mask")
        self.assertEqual(by[os.path.join(self.root, "tls.key")].kind, "mask")
        self.assertEqual(by[os.path.join(self.root, "secrets")].kind, "tmpfs")
        dw_env = by[os.path.join(self.dw, ".env")]
        self.assertEqual((dw_env.kind, dw_env.src), ("bind", os.path.join(self.dw, ".env.dev")),
                         "a data grant keeps its dev swap")

    def test_off_masks_and_swaps_as_before(self):
        by = {m.dst: m for m in self.plan(False).mounts}
        self.assertEqual(by[os.path.join(self.root, ".env")].src, os.path.join(self.root, ".env.dev"))
        self.assertEqual(by[os.path.join(self.root, "sub", "prod.env")].kind, "mask")

    def test_no_mount_point_is_created_for_the_swap(self):
        os.remove(os.path.join(self.root, ".env"))
        self.assertIn(os.path.join(self.root, ".env"), self.plan(False).create_files)
        self.assertNotIn(os.path.join(self.root, ".env"), self.plan(True).create_files)

    def test_env_file_names(self):
        for fn in (".env", ".env.prod", "prod.env"):
            self.assertTrue(mounts.env_file(fn), fn)
        for fn in (".env.dev", ".env.example", ".netrc", ".pgpass", "x.key", "x.pem", "env"):
            self.assertFalse(mounts.env_file(fn), fn)


class TheGrantCheck(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("p1")
        self.dw = self.mkdir("dw")
        for rel in ("projects/p1/.env", "projects/p1/deploy/prod.env", "projects/p1/.netrc", "dw/.env",
                    "projects/p1/data/creds.key"):
            self.write(rel, "X=1\n")
        for rel in ("data/live.sqlite", "other/big.db"):
            os.makedirs(os.path.dirname(os.path.join(self.root, rel)), exist_ok=True)
            with open(os.path.join(self.root, rel), "wb") as f:
                f.truncate((grantcheck.LIVE_DATA_MB + 1) << 20)
        with open(os.path.join(self.root, "compose.yaml"), "w") as f:
            f.write("services:\n  db:\n    volumes:\n      - ./data:/var/lib/data\n")

    def texts(self, **extra):
        """What `ab project check` / a session refuses on: preflight, which builds the plan the session would."""
        entry = {"root": self.root, "preset": "python", "data_rw": [self.dw], **extra}
        return "\n".join(f["text"] for f in grantcheck.preflight(self.settings, "p1", entry)[0])

    def scan_texts(self, **kw):
        """The scan with nothing covered, so every secret-named file would be a finding but for the opt-outs."""
        found, _warn = grantcheck.scan(self.root, [self.root, self.dw], **kw)
        return "\n".join(f["text"] for f in found)

    def test_off_live_data_is_a_finding_and_masked_secrets_are_not(self):
        t = self.texts()
        for needle in ("live.sqlite", "big.db", "compose data bind"):
            self.assertIn(needle, t)
        self.assertNotIn(".env", t, "masked, so covered")

    def test_data_env_unmasks_the_root_env_files_and_does_not_refuse_them(self):
        plan = mounts.plan(root=self.root, data_rw=[self.dw], swap=mounts.effective_swap(None), data_env=True)
        unexcused = "\n".join(f["text"] for f in grantcheck.scan(self.root, [self.root, self.dw],
                                                                  covered=mounts.hidden(plan))[0])
        self.assertIn(os.path.join(self.root, ".env"), unexcused, "the plan really shows it")
        t = self.texts(data_env="on")
        self.assertNotIn("p1/.env", t)
        self.assertNotIn("prod.env", t)

    def test_data_env_excuses_only_the_roots_env_file_names(self):
        t = self.scan_texts(env_ok=[self.root])
        self.assertFalse([ln for ln in t.splitlines() if ln.endswith(os.path.join(self.root, ".env"))], t)
        self.assertNotIn("prod.env", t)
        for needle in (".netrc", os.path.join(self.dw, ".env"), "creds.key"):
            self.assertIn(needle, t)

    def test_data_bind_drops_live_data_under_data_only(self):
        t = self.texts(data_bind="on")
        self.assertNotIn("live.sqlite", t)
        self.assertNotIn("compose data bind", t)
        self.assertIn("big.db", t, "live data elsewhere in the root is still a finding")
        self.assertIn("creds.key", self.scan_texts(data_ok=[mounts.data_dir(self.root)]),
                      "a secret under ./data is still a secret")


class TheHistory(SettingsCase):
    def test_without_real_env(self):
        root = self.mkdir("r")
        names = [".env", "sub/.env.prod", "x.key", "secrets/.env", "../dw/.env", "inner/.env"]
        self.assertEqual(session.without_real_env(names, root, [os.path.join(root, "inner")]),
                         ["x.key", "secrets/.env", "../dw/.env", "inner/.env"])

    def test_a_committed_env_does_not_refuse_a_data_env_session(self):
        root = self.project("p1", data_env="on")
        self.write("projects/p1/.env", "K=V\n")
        _git(root, "add", "-f", ".env")
        _git(root, "commit", "-qm", "x")
        self.token()
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda *_: None)
        self.addCleanup(s.teardown)
        s.prepare(do_backup=False)          # would raise "masked file name(s) are in git history" with it off

    def test_the_same_history_refuses_with_data_env_off(self):
        root = self.project("p1")
        self.write("projects/p1/.env.dev", "K=dev\n")
        self.write("projects/p1/.env", "K=V\n")
        _git(root, "add", "-f", ".env")
        _git(root, "commit", "-qm", "x")
        self.token()
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda *_: None)
        self.addCleanup(s.teardown)
        with self.assertRaises(AgentboxError) as cm:
            s.prepare(do_backup=False)
        self.assertIn("git history", str(cm.exception))


class TheSession(SettingsCase):
    def setUp(self):
        super().setUp()
        self.warnings = []
        self.token()

    def make(self, do_backup=False, **extra):
        root = self.project("p1", **extra)
        self.write("projects/p1/.env", "K=real\n")
        self.write("projects/p1/.env.dev", "K=dev\n")
        os.makedirs(os.path.join(root, "data"), exist_ok=True)
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=self.warnings.append)
        self.addCleanup(s.teardown)
        s.prepare(do_backup=do_backup)
        return s

    def run_dir_json(self, s, name):
        return json.loads(read_file(os.path.join(self.settings.session_run_dir(s.name), name)))

    def test_off_no_warning_and_the_swap_stands(self):
        s = self.make()
        self.assertFalse([w for w in self.warnings if "WARNING" in w], self.warnings)
        env = os.path.join(s.project.root, ".env")
        self.assertIn(f"--mount=type=bind,src={env}.dev,dst={env}", s.run_args(["true"]))
        notes = session.sandbox_notes(s.project, s.egress)
        self.assertIn("are masked and read as empty", notes)
        self.assertNotIn("data_env", notes)
        self.assertEqual(self.run_dir_json(s, lifecycle.SCAN_STATE)["trust"], [])

    def test_data_env_warns_records_and_shows_the_real_env(self):
        s = self.make(data_env="on")
        [w] = [w for w in self.warnings if "WARNING" in w]
        self.assertTrue(w.startswith("agentbox: WARNING: data_env is on"), w)
        self.assertIn("only for an agent you trust", w)
        env = os.path.join(s.project.root, ".env")
        self.assertFalse([a for a in s.run_args(["true"]) if f"dst={env}," in a or a.endswith(f"dst={env}")])
        self.assertNotIn(os.path.realpath(env), s.covered_paths())
        self.assertEqual(self.run_dir_json(s, "session.json")["data_env"], "on")
        [rec] = self.run_dir_json(s, lifecycle.SCAN_STATE)["trust"]
        self.assertIn("data_env is on", rec)
        notes = session.sandbox_notes(s.project, s.egress)
        self.assertIn("REAL", notes)
        self.assertNotIn("are masked and read as empty; `.env.example`", notes, "the notes must not say .env is masked")
        self.assertIn("still masked", notes)

    def test_data_env_tells_the_gate(self):
        s = self.make(data_env="on")
        self.assertEqual(s.gate_config("/svc.sock", {})["env_visible"], [os.path.realpath(s.project.root)])
        s2 = self.make()
        self.assertEqual(s2.gate_config("/svc.sock", {})["env_visible"], [])

    def test_data_bind_warns_and_leaves_data_out_of_the_backup_and_the_scan(self):
        calls = {}

        def snapshot(_dir, _name, grants, *, exclude_rel_for, **_kw):
            calls["excludes"] = {g: exclude_rel_for(g) for g in grants}

        with mock.patch.object(session.backup, "snapshot", snapshot), \
                mock.patch.object(session.backup, "retain", lambda *a, **k: None):
            s = self.make(do_backup=True, data_bind="on")
        root = s.project.root
        self.assertIn("data", calls["excludes"][root])
        self.assertTrue([w for w in self.warnings if "WARNING: data_bind is on" in w], self.warnings)
        self.assertTrue([w for w in self.warnings if "not in the pre-session snapshot" in w
                         and "yours to back up" in w], self.warnings)
        doc = self.run_dir_json(s, lifecycle.SCAN_STATE)
        self.assertIn("data", doc["skip"][root])
        self.assertTrue(doc["trust"])
        notes = session.sandbox_notes(s.project, s.egress)
        self.assertIn("LIVE data", notes)
        self.assertIn(os.path.join(root, "data"), notes)

    def test_without_data_bind_data_is_backed_up(self):
        calls = {}

        def snapshot(_dir, _name, grants, *, exclude_rel_for, **_kw):
            calls["excludes"] = {g: exclude_rel_for(g) for g in grants}

        with mock.patch.object(session.backup, "snapshot", snapshot), \
                mock.patch.object(session.backup, "retain", lambda *a, **k: None):
            s = self.make(do_backup=True)
        self.assertNotIn("data", calls["excludes"][s.project.root])

    def test_the_end_of_session_report_repeats_the_warning(self):
        run = self.settings.session_run_dir("abt-unit-x")
        os.makedirs(run)
        root = self.mkdir("r")
        with open(os.path.join(run, lifecycle.SCAN_STATE), "w") as f:
            json.dump({"grants": [root], "skip": {root: []}, "state": {}, "trust": ["data_env is on for p1: x"]}, f)
        seen = []
        with mock.patch.object(lifecycle.scan, "report", lambda *a, **k: ([], "body")):
            lifecycle._scan(self.settings, "abt-unit-x", 0, seen.append)
        self.assertTrue([w for w in seen if "WARNING (recorded at the start): data_env is on" in w], seen)


class TheGate(SettingsCase):
    def setUp(self):
        super().setUp()
        from tests.unit.test_review_step5 import CoversAudit
        self._audit = CoversAudit("setUp")
        self._audit.setUp()
        self.addCleanup(self._audit.doCleanups)
        self.proj = self._audit.proj

    def gate(self, visible):
        g = self._audit.gate()
        g.cfg["env_visible"] = visible
        return g

    def test_a_root_env_file_is_no_late_secret_under_data_env(self):
        app = os.path.join(self.proj, "app")
        self.write(os.path.join(app, ".env"), "K=V")
        self.assertEqual(self.gate([]).unmasked_secret_under(app), os.path.join(app, ".env"))
        self.assertIsNone(self.gate([self.proj]).unmasked_secret_under(app))

    def test_other_secret_names_are_still_late_secrets(self):
        app = os.path.join(self.proj, "app")
        self.write(os.path.join(app, ".netrc"), "machine x")
        self.assertEqual(self.gate([self.proj]).unmasked_secret_under(app), os.path.join(app, ".netrc"))

    def test_an_env_file_in_a_data_grant_inside_the_root_is_still_one(self):
        inner = os.path.join(self.proj, "app", "sub")
        self.write(os.path.join(inner, ".env"), "K=V")
        g = self.gate([self.proj])
        g.cfg["bind_roots"] = list(g.cfg["bind_roots"]) + [inner]
        self.assertEqual(g.unmasked_secret_under(inner), os.path.join(inner, ".env"))
