import os
import subprocess

from agentbox import mounts

from .helpers import TempCase


class MountPlanTest(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")
        subprocess.run(["git", "init", "-q", self.root], check=True)
        for rel in (".env", ".env.local", ".env.example", ".env.sample", ".env.template", "app.pem", "tls/x.key",
                    "sub/.env", "sub/.env.dev", "deep/a/b/.env.production", "secrets/db.txt",
                    "node_modules/pkg/.env", ".venv/.env", "big/data/.env", "compose.yml", "svc/compose.yaml",
                    "keep.txt", ".env.dev"):
            self.write(os.path.join("proj", rel), "SECRET=1\n")
        os.symlink("/etc/hostname", os.path.join(self.root, "link.pem"))

    def by_dst(self, plan):
        return {os.path.relpath(m.dst, self.root): m for m in plan.mounts}

    def test_masks(self):
        p = mounts.plan(root=self.root, mask_skip=["big"])
        m = self.by_dst(p)
        for masked in (".env.local", "app.pem", "tls/x.key", "deep/a/b/.env.production"):
            self.assertEqual(m[masked].kind, "devnull", masked)
        for exempt in (".env.example", ".env.sample", ".env.template", ".env.dev", "sub/.env.dev", "keep.txt"):
            self.assertNotIn(exempt, m, exempt)
        for skipped in ("node_modules/pkg/.env", ".venv/.env", "big/data/.env", "link.pem"):
            self.assertNotIn(skipped, m, skipped)
        self.assertEqual(m["secrets"].kind, "tmpfs")
        self.assertNotIn("secrets/db.txt", m)

    def test_dev_swap(self):
        m = self.by_dst(mounts.plan(root=self.root))
        self.assertEqual((m[".env"].kind, m[".env"].src), ("bind", os.path.join(self.root, ".env.dev")))
        self.assertFalse(m[".env"].ro)
        self.assertEqual(m["sub/.env"].src, os.path.join(self.root, "sub", ".env.dev"))

    def test_dev_swap_creates_missing_env(self):
        root = self.mkdir("proj2")
        self.write("proj2/.env.dev", "A=1\n")
        p = mounts.plan(root=root)
        self.assertEqual(p.create_files, [os.path.join(root, ".env")])
        mounts.prepare(p)
        st = os.stat(os.path.join(root, ".env"))
        self.assertEqual((st.st_size, st.st_mode & 0o777), (0, 0o600))

    def test_custom_swap_masks_target_without_dev_file(self):
        self.write("proj/secrets.env", "X=1\n")
        m = self.by_dst(mounts.plan(root=self.root, swap={".env": ".env.dev", "secrets.env": "secrets.dev.env"}))
        self.assertEqual(m["secrets.env"].kind, "devnull")

    def test_with_secrets_exposes(self):
        m = self.by_dst(mounts.plan(root=self.root, secrets="exposed"))
        self.assertNotIn(".env.local", m)
        self.assertNotIn(".env", m)

    def test_git_overlays(self):
        p = mounts.plan(root=self.root)
        m = self.by_dst(p)
        self.assertTrue(m[".git/hooks"].ro)
        self.assertTrue(m[".git/config"].ro)
        self.assertEqual(m["."].kind, "bind")
        self.assertFalse(m["."].ro)

    def test_git_hooks_created_when_missing_and_submodules(self):
        import shutil
        shutil.rmtree(os.path.join(self.root, ".git", "hooks"))
        sub = self.mkdir("proj", ".git", "modules", "lib")
        self.write("proj/.git/modules/lib/HEAD", "ref: refs/heads/main\n")
        self.write("proj/.git/modules/lib/config", "[core]\n")
        p = mounts.plan(root=self.root)
        self.assertIn(os.path.join(self.root, ".git", "hooks"), p.create_dirs)
        m = self.by_dst(p)
        self.assertTrue(m[".git/modules/lib/hooks"].ro)
        self.assertTrue(m[".git/modules/lib/config"].ro)
        self.assertTrue(os.path.isdir(sub))

    def test_git_file_is_noted(self):
        root = self.mkdir("wt")
        self.write("wt/.git", "gitdir: /elsewhere\n")
        self.assertTrue(mounts.plan(root=root).notes)

    def test_compose_ro(self):
        m = self.by_dst(mounts.plan(root=self.root, compose="ro"))
        self.assertTrue(m["compose.yml"].ro)
        self.assertTrue(m["svc/compose.yaml"].ro)
        m = self.by_dst(mounts.plan(root=self.root, compose="rw"))
        self.assertNotIn("compose.yml", m)

    def test_data_grants(self):
        ro, rw = self.mkdir("raw"), self.mkdir("out")
        self.write("raw/.env", "X")
        self.write("raw/.env.dev", "Y")
        m = {x.dst: x for x in mounts.plan(root=self.root, data_ro=[ro], data_rw=[rw]).mounts}
        self.assertTrue(m[ro].ro)
        self.assertFalse(m[rw].ro)
        self.assertTrue(m[os.path.join(ro, ".env")].ro, "a dev swap inside a read-only grant stays read-only")

    def test_tmpfs_never_copies_up(self):
        self.assertIn(",notmpcopyup", mounts.Mount("tmpfs", "/a/secrets").podman_arg())

    def test_podman_arg(self):
        self.assertEqual(mounts.Mount("devnull", "/a/.env").podman_arg(),
                         "--mount=type=bind,src=/dev/null,dst=/a/.env,ro")
        self.assertEqual(mounts.Mount("bind", "/a", "/a", ro=True).podman_arg(), "--mount=type=bind,src=/a,dst=/a,ro")
