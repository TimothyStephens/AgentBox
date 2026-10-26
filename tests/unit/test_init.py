"""`ab init`: the scaffold must produce a project that passes grantcheck with no override, by construction.
The load-bearing assertion (Tim, 6 Oct) is test_fresh_project_is_clean_by_construction: a freshly-init'd
project starts a session with ZERO warnings and ZERO overrides, for both templates."""
import io
import os
import subprocess
import unittest
from contextlib import redirect_stdout

from agentbox import cli, config, grantcheck, initproj, registry

from .helpers import SettingsCase


class InitCase(SettingsCase):
    def setUp(self):
        super().setUp()
        self.repos = self.mkdir("repos")
        os.environ["AB_REPOS_DIR"] = self.repos
        self.addCleanup(os.environ.pop, "AB_REPOS_DIR", None)
        self.settings = config.load()

    def init(self, template, name, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["init", template, "--name", name, *extra])
        return rc, buf.getvalue()

    def entry(self, name):
        return registry.load(self.settings.registry_path)[name]

    def init_rc(self, template, name, *extra):
        """Exit code of `ab init`, whether argparse refused it (SystemExit) or a check did."""
        try:
            return self.init(template, name, *extra)[0]
        except SystemExit as e:
            return e.code


class FreshProjectTest(InitCase):
    def test_fresh_project_is_clean_by_construction(self):
        """For both templates: registered with zero overrides, and grantcheck (what session start runs) finds
        nothing and warns about nothing."""
        for template, preset in (("pod", "build"), ("python", "script")):
            with self.subTest(template=template):
                name = f"demo{template}"
                rc, _ = self.init(template, name)
                self.assertEqual(rc, 0)
                e = self.entry(name)
                self.assertEqual(set(e), {"root", "preset"}, "zero overrides")
                self.assertEqual(e["preset"], preset)
                self.assertEqual(e["root"], os.path.join(self.repos, name))

                covered = cli._mask_dsts(self.settings, e)
                findings, warnings = grantcheck.scan(e["root"], [e["root"]] + e.get("data_rw", []),
                                                     e.get("mask_skip", []), covered=covered)
                self.assertEqual(findings, [], f"{template}: grantcheck findings")
                self.assertEqual(warnings, [], f"{template}: grantcheck warnings")

    def test_secret_layout(self):
        for template in ("pod", "python"):
            with self.subTest(template=template):
                name = f"sec{template}"
                self.init(template, name)
                root = os.path.join(self.repos, name)
                # .env has real-secret placeholders, is 0600, and is gitignored (never committed).
                self.assertEqual(os.stat(os.path.join(root, ".env")).st_mode & 0o777, 0o600)
                tracked = subprocess.run(["git", "-C", root, "ls-files"], stdout=subprocess.PIPE,
                                         text=True).stdout.split()
                self.assertIn(".env.dev", tracked, "the fake twin is committed")
                self.assertNotIn(".env", tracked, ".env is never committed")
                self.assertEqual(subprocess.run(["git", "-C", root, "check-ignore", "-q", ".env"]).returncode, 0)
                self.assertTrue(os.path.isfile(os.path.join(root, "AGENTS.md")))
                # .env and .env.dev carry the same keys (deploy checks this; keep them in step).
                def keys(fn):
                    with open(os.path.join(root, fn)) as f:
                        return {ln.split("=", 1)[0] for ln in f if "=" in ln and not ln.startswith("#")}
                self.assertEqual(keys(".env"), keys(".env.dev"))

    def test_python_data_path_comes_from_config(self):
        """The python template takes its data path from .env/.env.dev (dev -> ./dev/data, prod -> a real
        path), with no ./data in the repo tree and no overlay (Tim, 6 Oct)."""
        self.init("python", "pydata")
        root = os.path.join(self.repos, "pydata")
        self.assertFalse(os.path.isdir(os.path.join(root, "data")), "no ./data in the repo tree")
        self.assertTrue(os.path.isdir(os.path.join(root, "dev", "data")))

        def val(fn, key):
            with open(os.path.join(root, fn)) as f:
                for ln in f:
                    if ln.startswith(key + "="):
                        return ln.split("=", 1)[1].strip()
            return None
        self.assertEqual(val(".env.dev", "DATA_DIR"), "./dev/data", "dev points at the repo's dev data")
        self.assertNotIn(val(".env", "DATA_DIR"), ("./dev/data", None), "prod is a real path, not ./dev/data")
        # The app reads the path from the environment; it does not hardcode it.
        with open(os.path.join(root, "app", "main.py")) as f:
            app = f.read()
        self.assertIn("DATA_DIR", app)
        self.assertIn("os.environ", app)

    def test_pod_data_is_a_named_volume_not_a_bind(self):
        self.init("pod", "voldemo")
        root = os.path.join(self.repos, "voldemo")
        with open(os.path.join(root, "compose.yaml")) as f:
            live = f.read()
        self.assertIn("voldemo_data:/data", live, "live data in a named volume")
        self.assertNotIn("./data", live, "no repo-tree data bind in the live stack")
        self.assertTrue(os.path.isfile(os.path.join(root, "compose.dev.yaml")))
        self.assertFalse(os.path.isdir(os.path.join(root, "data")), "no ./data: --data-bind is deferred")


class OverrideTest(InitCase):
    """ab init shares the project-setting flags with ab project add/set (requirement 1); it is no longer
    limited to the template's bare default preset with zero overrides."""

    def test_init_accepts_override_flags(self):
        rc, _ = self.init("pod", "openpod", "--egress", "open")
        self.assertEqual(rc, 0)
        e = self.entry("openpod")
        self.assertEqual(set(e), {"root", "preset", "egress"})
        self.assertEqual(e["preset"], "build")
        self.assertEqual(e["egress"], "open", "the explicit override, not research/build-open's auto-open")

    def test_init_accepts_an_explicit_preset(self):
        rc, _ = self.init("python", "offline1", "--preset", "analysis-offline")
        self.assertEqual(rc, 0)
        e = self.entry("offline1")
        self.assertEqual(set(e), {"root", "preset"})
        self.assertEqual(e["preset"], "analysis-offline")

    def test_init_with_a_default_open_preset_writes_egress_explicitly(self):
        rc, _ = self.init("python", "researchy", "--preset", "research")
        self.assertEqual(rc, 0)
        e = self.entry("researchy")
        self.assertEqual(e["preset"], "research")
        self.assertEqual(e["egress"], "open", "research's own default-open, written as a project override")


class SettingsCheckedBeforeScaffoldTest(InitCase):
    """F6: the settings are validated BEFORE anything is written, so a bad flag leaves no half-made tree
    (which would then make a corrected retry fail with "already exists; refusing to scaffold over it")."""

    def test_a_bad_setting_writes_nothing(self):
        cases = {
            "ceiling": ["--preset", "lockdown", "--observe-logs"],
            "unknown-preset": ["--preset", "nope"],
            "bad-build-memory": ["--build-memory", "not-a-size"],
            "bad-domain": ["--extra-domain", "not a domain"],
            "bad-dev-image": ["--dev-image", "NOT VALID"],
            "missing-data-ro": ["--data-ro", "/does/not/exist/at/all"],
            "bad-egress": ["--egress", "nope"],
            "bad-compose-project": ["--compose-project", "Not Valid"],
        }
        for label, extra in cases.items():
            with self.subTest(case=label):
                name = f"pre{label.replace('-', '')}"
                rc = self.init_rc("python", name, *extra)
                self.assertNotEqual(rc, 0, "must be refused")
                self.assertFalse(os.path.exists(os.path.join(self.repos, name)),
                                 "a settings error must write nothing")
                self.assertNotIn(name, registry.load(self.settings.registry_path))

    def test_good_settings_still_scaffold_and_register(self):
        rc, _ = self.init("pod", "fine", "--egress", "locked", "--build-memory", "3g")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.repos, "fine")))
        e = self.entry("fine")
        self.assertEqual(e["egress"], "locked")
        self.assertEqual(e["build_memory"], "3g")


class SharedArgsParityTest(InitCase):
    """Requirement 1, functional half: the same override flags passed to `ab init` and to `ab project add`
    produce the same registry entry, modulo root/name."""

    def test_init_and_add_apply_overrides_identically(self):
        overrides = ["--egress", "packages", "--build-memory", "4g", "--mask-skip", "sub"]
        rc, _ = self.init("pod", "viaint", *overrides)
        self.assertEqual(rc, 0)
        via_init = dict(self.entry("viaint"))

        root = self.mkdir("manualroot")
        rc = cli.main(["project", "add", "viaadd", root, "--preset", "build", *overrides])
        self.assertEqual(rc, 0)
        via_add = dict(self.entry("viaadd"))

        for k in ("root", "preset"):
            del via_init[k]
            del via_add[k]
        self.assertEqual(via_init, via_add)


class UpstreamTest(InitCase):
    def test_upstream_is_added_and_pushed(self):
        bare = os.path.join(self.tmp, "upstream.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        rc, out = self.init("python", "pushme", "--upstream", bare)
        self.assertEqual(rc, 0)
        self.assertIn("upstream", out)
        root = os.path.join(self.repos, "pushme")
        origin = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                                stdout=subprocess.PIPE, text=True).stdout.strip()
        self.assertEqual(origin, bare)
        # The bare upstream now has the scaffold commit, and it did NOT receive .env.
        pushed = subprocess.run(["git", "-C", bare, "log", "--oneline"], stdout=subprocess.PIPE, text=True).stdout
        self.assertIn("scaffold", pushed)
        files = subprocess.run(["git", "-C", bare, "ls-tree", "-r", "--name-only", "HEAD"],
                               stdout=subprocess.PIPE, text=True).stdout.split()
        self.assertIn(".env.dev", files)
        self.assertNotIn(".env", files)


class RefusalTest(InitCase):
    def test_refuses_duplicate_name(self):
        self.init("python", "dup")
        rc, _ = self.init("python", "dup")
        self.assertEqual(rc, 2)

    def test_refuses_existing_directory(self):
        os.makedirs(os.path.join(self.repos, "taken"))
        rc, _ = self.init("python", "taken")
        self.assertEqual(rc, 2)
        self.assertNotIn("taken", registry.load(self.settings.registry_path))

    def test_unknown_template_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            cli.main(["init", "rust", "--name", "x"])

    def test_bad_name_leaves_no_tree(self):
        rc, _ = self.init("python", "Bad Name")
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(os.path.join(self.repos, "Bad Name")))


class TemplateOverrideTest(InitCase):
    """A user may replace only the doc files (AGENTS.md, README.md) from <config>/templates/<template>/;
    the .env/compose/structure always comes from the built-in, so the scaffold stays grantcheck-clean."""

    def tfile(self, template, fn, text):
        d = os.path.join(self.settings.config_dir, "templates", template)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fn), "w") as f:
            f.write(text)

    def test_agents_md_override_is_used(self):
        self.tfile("python", "AGENTS.md", "Custom agents doc for {name}.\n")
        self.tfile("python", "README.md", "# {name} readme\n")
        self.init("python", "ov1")
        root = os.path.join(self.repos, "ov1")
        with open(os.path.join(root, "AGENTS.md")) as f:
            self.assertEqual(f.read(), "Custom agents doc for ov1.\n", "override used, {name}-substituted")
        with open(os.path.join(root, "README.md")) as f:
            self.assertEqual(f.read(), "# ov1 readme\n")

    def test_structure_overrides_are_ignored(self):
        # a user tries to slip structure/secrets in through the template dir
        for fn, body in ((".env", "LEAKED=realsecret\n"), (".env.dev", "X=1\n"),
                         ("compose.yaml", "services:\n  evil: {}\n"), ("Containerfile", "FROM scratch\n")):
            self.tfile("pod", fn, body)
        rc, _ = self.init("pod", "ov2")
        self.assertEqual(rc, 0)
        root = os.path.join(self.repos, "ov2")
        with open(os.path.join(root, ".env")) as f:
            self.assertNotIn("LEAKED", f.read(), ".env is the built-in, not the template dir's")
        with open(os.path.join(root, "compose.yaml")) as f:
            self.assertNotIn("evil", f.read(), "compose structure is not overridable")
        with open(os.path.join(root, "Containerfile")) as f:
            self.assertNotIn("FROM scratch", f.read())
        # still clean, zero overrides
        e = self.entry("ov2")
        self.assertEqual(set(e), {"root", "preset"})
        covered = cli._mask_dsts(self.settings, e)
        findings, warnings = grantcheck.scan(e["root"], [e["root"]], e.get("mask_skip", []), covered=covered)
        self.assertEqual((findings, warnings), ([], []), "scaffold stays grantcheck-clean")


if __name__ == "__main__":
    unittest.main()
