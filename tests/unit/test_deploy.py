"""`ab deploy`, step 1: what is decided before podman (deploy.py, cli_deploy.py; design.md)."""
import os
import subprocess
from unittest import mock

from agentbox import deploy
from agentbox.errors import AgentboxError

from .helpers import SettingsCase

COMPOSE = "services:\n  web:\n    build: .\n    ports: ['8095:80']\n"


class DeployCase(SettingsCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)                  # these run inside a session too; deploy is host-only
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENTBOX_SESSION", None)
        self.root = self.project("web", preset="build")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.write("projects/web/compose.yaml", COMPOSE)
        self.write("projects/web/Containerfile", "FROM docker.io/library/python:3.12-slim\n")
        self.write("projects/web/app.py", "print('v1')\n")
        self.write("projects/web/.env.dev", "DB_URL=sqlite:///dev.db\nexport API_KEY=dev\n# comment\n")
        self.write("projects/web/.gitignore", ".env\n")
        self.write("projects/web/.env", "DB_URL=postgres://real\nAPI_KEY=REAL-SECRET-VALUE\nEXTRA=1\n")
        self.commit("v1")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.root, *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")


class EnvNames(DeployCase):
    def test_names_only_and_missing_ones_listed(self):
        self.assertEqual(deploy.env_check(self.root), ([], ["EXTRA"]))
        self.write("projects/web/.env", "DB_URL=x\n")
        self.assertEqual(deploy.env_check(self.root), (["API_KEY"], []))

    def test_no_real_env_is_every_name_missing(self):
        os.remove(os.path.join(self.root, ".env"))
        self.assertEqual(deploy.env_check(self.root)[0], ["API_KEY", "DB_URL"])

    def test_no_value_reaches_the_output(self):
        code, out = self.ab("deploy", "web", "--check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("REAL-SECRET-VALUE", out)
        self.assertNotIn("postgres://real", out)
        self.assertIn("EXTRA", out)


class TheCommit(DeployCase):
    def test_a_dirty_tracked_tree_is_refused(self):
        self.write("projects/web/app.py", "print('edited')\n")
        code, out = self.ab("deploy", "web", "--check")
        self.assertEqual(code, 2, out)
        self.assertIn("uncommitted changes", out)
        self.assertIn("app.py", out)

    def test_untracked_files_do_not_count(self):
        self.write("projects/web/notes.txt", "scratch")
        self.assertEqual(self.ab("deploy", "web", "--check")[0], 0)

    def test_a_named_commit_needs_no_clean_tree(self):
        first = self.git("rev-parse", "HEAD")
        self.write("projects/web/app.py", "print('edited')\n")
        code, out = self.ab("deploy", "web", "--check", "--commit", first[:10])
        self.assertEqual(code, 0, out)
        self.assertIn(first, out)

    def test_no_repo_or_no_such_commit_is_refused(self):
        with self.assertRaises(AgentboxError):
            deploy.resolve_commit(self.root, "nope")
        with self.assertRaises(AgentboxError):
            deploy.resolve_commit(self.root, "--output=/tmp/x")
        with self.assertRaises(AgentboxError):
            deploy.resolve_commit(self.mkdir("plain"))

    def test_a_commit_without_a_compose_file_is_refused(self):
        self.git("rm", "-q", "compose.yaml")
        self.commit("no compose")
        code, out = self.ab("deploy", "web", "--check")
        self.assertEqual(code, 2, out)
        self.assertIn("no compose file", out)

    def test_missing_env_names_are_refused(self):
        self.write("projects/web/.env", "DB_URL=x\n")
        code, out = self.ab("deploy", "web", "--check")
        self.assertEqual(code, 2, out)
        self.assertIn("API_KEY", out)


class TheReview(DeployCase):
    def test_a_first_deploy_shows_the_files_that_decide_what_runs_in_full(self):
        text, files = deploy.review(self.root, None, self.git("rev-parse", "HEAD"))
        self.assertIn("First deploy", text)
        self.assertIn("--- compose.yaml ---", text)
        self.assertIn("ports: ['8095:80']", text)
        self.assertIn("--- Containerfile ---", text)
        self.assertIn("  app.py", text)
        self.assertIn("app.py", files)

    def test_later_deploys_show_the_diff_since_the_last_one(self):
        first = self.git("rev-parse", "HEAD")
        deploy.record(self.settings, "web", {"commit": first, "result": "ok"})
        self.write("projects/web/compose.yaml", COMPOSE + "    privileged: true\n")
        self.write("projects/web/app.py", "print('v2')\n")
        second = self.commit("v2")
        c = deploy.check(self.settings, "web", self.root)
        self.assertEqual(c["last"], first)
        self.assertIn(f"{first[:12]} -> {second[:12]}", c["review"])
        self.assertIn("+    privileged: true", c["review"])
        self.assertIn("M app.py", c["review"])
        self.assertNotIn("print('v2')", c["review"], "only the key files are shown in full")

    def test_a_failed_deploy_is_not_the_last_deploy(self):
        first = self.git("rev-parse", "HEAD")
        deploy.record(self.settings, "web", {"commit": first, "result": "ok"})
        deploy.record(self.settings, "web", {"commit": "f" * 40, "result": "failed"})
        self.assertEqual(deploy.last_deployed(self.settings, "web"), first)

    def test_the_same_commit_again_says_no_change(self):
        first = self.git("rev-parse", "HEAD")
        deploy.record(self.settings, "web", {"commit": first, "result": "ok"})
        self.assertIn("No change", deploy.review(self.root, first, first)[0])

    def test_a_last_deploy_no_longer_in_history_reads_as_a_first_deploy(self):
        text, _ = deploy.review(self.root, "a" * 40, self.git("rev-parse", "HEAD"))
        self.assertIn("not in this repository any more", text)
        self.assertIn("First deploy", text)

    def test_git_runs_nothing_the_repo_config_names(self):
        """The repo is agent-written: an external diff or textconv in its config must not run on the host."""
        marker = os.path.join(self.tmp, "ran")
        self.git("config", "diff.external", f"sh -c 'touch {marker}'")
        self.write("projects/web/.gitattributes", "*.yaml diff=x\n")
        self.git("config", "diff.x.textconv", f"sh -c 'touch {marker}; cat'")
        first = self.git("rev-parse", "HEAD")
        self.write("projects/web/compose.yaml", COMPOSE + "# v2\n")
        second = self.commit("v2")
        deploy.review(self.root, first, second)
        self.assertFalse(os.path.exists(marker))


class TheCopy(DeployCase):
    def test_the_copy_is_the_commit_plus_the_real_env(self):
        first = self.git("rev-parse", "HEAD")
        self.write("projects/web/app.py", "print('edited after review')\n")
        dst = deploy.stage(self.root, first, os.path.join(self.tmp, "stage"))
        with open(os.path.join(dst, "app.py")) as f:
            self.assertEqual(f.read(), "print('v1')\n")
        self.assertEqual(os.stat(os.path.join(dst, ".env")).st_mode & 0o777, 0o600)
        self.assertEqual(deploy.compose_file(dst), os.path.join(dst, "compose.yaml"))

    def test_untracked_files_other_than_named_ones_are_not_copied(self):
        self.write("projects/web/scratch.db", "live rows")
        dst = deploy.stage(self.root, self.git("rev-parse", "HEAD"), os.path.join(self.tmp, "stage"))
        self.assertFalse(os.path.exists(os.path.join(dst, "scratch.db")))

    def test_a_symlinked_env_is_refused_and_the_copy_removed(self):
        os.remove(os.path.join(self.root, ".env"))
        os.symlink("/etc/passwd", os.path.join(self.root, ".env"))
        dst = os.path.join(self.tmp, "stage")
        with self.assertRaises(AgentboxError):
            deploy.stage(self.root, self.git("rev-parse", "HEAD"), dst)
        self.assertFalse(os.path.exists(dst))

    def test_a_named_file_outside_the_project_is_refused(self):
        with self.assertRaises(AgentboxError):
            deploy.stage(self.root, self.git("rev-parse", "HEAD"), os.path.join(self.tmp, "s"),
                         extra_files=["../outside"])

    def test_tracked_symlinks_stay_links_and_cannot_be_written_through(self):
        os.symlink("/etc", os.path.join(self.root, "etc-link"))
        sha = self.commit("a link")
        dst = deploy.stage(self.root, sha, os.path.join(self.tmp, "stage"))
        self.assertTrue(os.path.islink(os.path.join(dst, "etc-link")))
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            link = tarfile.TarInfo("l")
            link.type, link.linkname = tarfile.SYMTYPE, "/tmp"
            t.addfile(link)
            f = tarfile.TarInfo("l/planted")
            f.size = 1
            t.addfile(f, io.BytesIO(b"x"))
        buf.seek(0)
        with tarfile.open(fileobj=buf) as t, self.assertRaises(AgentboxError):
            deploy._extract(t, self.mkdir("x"))


class TheCommand(DeployCase):
    def test_it_refuses_inside_a_session(self):
        with mock.patch.dict(os.environ, {"AGENTBOX_SESSION": "ab-claude-web-1"}):
            code, out = self.ab("deploy", "web", "--check")
        self.assertNotEqual(code, 0)
        self.assertIn("runs on the host", out)

    def test_without_check_and_with_no_lan_address_it_is_refused_before_anything_starts(self):
        # Step 3 built the run, so this no longer says "not built yet": with no --lan and no stored lan_addr
        # the deploy is refused after the review, and podman is only asked read-only questions.
        code, out = self.ab("deploy", "web")
        self.assertEqual(code, 2)
        self.assertIn("no LAN address", out)
        self.assertFalse([c for c in self.podman_calls() if c[:1] == ["run"]
                          and "--label=agentbox.role=deploy-check" not in c], "only the contract check's parse")

    def test_lan_must_be_an_address_of_this_host(self):
        with self.assertRaises(AgentboxError):
            deploy.parse_lan("10.0.0.99:8095", {"192.0.2.10"})
        with self.assertRaises(AgentboxError):
            deploy.parse_lan("192.0.2.10", {"192.0.2.10"})
        self.assertEqual(deploy.parse_lan("192.0.2.10:8095", {"192.0.2.10"}), ("192.0.2.10", 8095))

    def test_check_records_nothing(self):
        self.ab("deploy", "web", "--check")
        self.assertEqual(deploy.records(self.settings, "web"), [])


class CopyModes(DeployCase):
    def test_a_non_root_container_user_can_read_the_copy_but_no_other_host_user_can_enter_it(self):
        os.chmod(os.path.join(self.root, "app.py"), 0o755)
        self.write("projects/web/sub/data.txt", "x")
        sha = self.commit("modes")
        dst = deploy.stage(self.root, sha, os.path.join(self.tmp, "stage"))
        mode = lambda *p: os.stat(os.path.join(dst, *p)).st_mode & 0o7777
        self.assertEqual(mode(), 0o700)
        self.assertEqual(mode("sub"), 0o755)
        self.assertEqual(mode("sub", "data.txt"), 0o644)
        self.assertEqual(mode("app.py"), 0o755)
        self.assertEqual(mode(".env"), 0o600, "the real secrets stay owner-only")
