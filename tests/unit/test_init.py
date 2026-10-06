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


class FreshProjectTest(InitCase):
    def test_fresh_project_is_clean_by_construction(self):
        """For both templates: registered with zero overrides, and grantcheck (what session start runs) finds
        nothing and warns about nothing."""
        for template, preset in (("pod", "pods"), ("python", "python")):
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

    def test_pod_data_is_a_named_volume_not_a_bind(self):
        self.init("pod", "voldemo")
        root = os.path.join(self.repos, "voldemo")
        with open(os.path.join(root, "compose.yaml")) as f:
            live = f.read()
        self.assertIn("voldemo_data:/data", live, "live data in a named volume")
        self.assertNotIn("./data", live, "no repo-tree data bind in the live stack")
        self.assertTrue(os.path.isfile(os.path.join(root, "compose.dev.yaml")))
        self.assertFalse(os.path.isdir(os.path.join(root, "data")), "no ./data: --data-bind is deferred")


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


if __name__ == "__main__":
    unittest.main()
