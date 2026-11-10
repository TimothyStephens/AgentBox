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
            self.assertEqual(m[masked].kind, "mask", masked)
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
        # A legal custom swap: the destination is a masked name and the stand-in is not (audit F6).
        self.write("proj/.env.local", "X=1\n")
        m = self.by_dst(mounts.plan(root=self.root, swap={".env": ".env.dev", ".env.local": ".env.local.dev"}))
        self.assertEqual(m[".env.local"].kind, "mask")

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
        self.mkdir("proj", ".git", "modules", "lib", "objects")
        self.mkdir("proj", ".git", "modules", "lib", "refs")
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
        self.assertEqual(mounts.Mount("mask", "/a/.env").podman_arg(mask_file="/run/s/mask-empty"),
                         "--mount=type=bind,src=/run/s/mask-empty,dst=/a/.env,ro")
        self.assertEqual(mounts.Mount("bind", "/a", "/a", ro=True).podman_arg(), "--mount=type=bind,src=/a,dst=/a,ro")


class NothingInsideADirectoryMask(MountPlanTest):
    """Audit 6 Oct, N2: a git dir or compose file under secrets/ must not get a real bind over the empty
    cover (podman would mount it after the tmpfs and show the real file)."""

    def test_no_mount_under_a_masked_dir(self):
        vault = os.path.join(self.root, "secrets", "vault")
        os.makedirs(vault)
        subprocess.run(["git", "init", "-q", vault], check=True)
        self.write("proj/secrets/compose.yaml", "services: {}\n")
        plan = mounts.plan(root=self.root, compose="ro")
        under = [d for d in self.by_dst(plan) if d.startswith("secrets/")]
        self.assertEqual(under, [], "nothing is mounted inside the secrets/ mask")
        self.assertEqual(self.by_dst(plan)["secrets"].kind, "tmpfs")
        self.assertFalse([d for d in plan.create_dirs if "/secrets/" in d])


class HardLinks(MountPlanTest):
    """Audit 6 Oct, N4: another name of a covered file is masked too."""

    def test_other_names_masked(self):
        os.link(os.path.join(self.root, "secrets", "db.txt"), os.path.join(self.root, "keep-db.txt"))
        os.link(os.path.join(self.root, ".env"), os.path.join(self.root, "sub", "copy-of-env"))
        os.link(os.path.join(self.root, ".git", "config"), os.path.join(self.root, "gitcfg"))
        plan = mounts.plan(root=self.root)
        d = self.by_dst(plan)
        for rel in ("keep-db.txt", "sub/copy-of-env", "gitcfg"):
            self.assertEqual(d.get(rel).kind if d.get(rel) else None, "mask", rel)
        self.assertNotIn("keep.txt", d)


class UserMasks(TempCase):
    """The project's own masks (`mask`, decisions.md 10 Nov): a NAME covers every file or directory of that
    name in every grant, a PATH (`./x` or with a '/') covers one path under the root. Empty, read-only, and
    on top of the secret masks -- never a substitution."""

    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")
        for rel in ("data/app.db", "dev/data/app.db", "sub/repo/data/x.db", "deploy/state/s.json",
                    "data.txt", "notes/data.txt", "app.py", ".env", ".env.dev", "node_modules/data/x"):
            self.write(os.path.join("proj", rel), "x\n")

    def by_dst(self, plan):
        return {os.path.relpath(m.dst, self.root): m for m in plan.mounts}

    def test_a_name_masks_every_directory_and_file_of_that_name(self):
        p = mounts.plan(root=self.root, mask=["data", "data.txt"])
        m = self.by_dst(p)
        for d in ("data", "dev/data", "sub/repo/data"):
            self.assertEqual((m[d].kind, m[d].ro), ("tmpfs", True), d)
        for fn in ("data.txt", "notes/data.txt"):
            self.assertEqual(m[fn].kind, "mask", fn)
        self.assertNotIn("node_modules/data", m, "the walk skips what the secret-mask walk skips")
        self.assertNotIn("app.py", m)
        self.assertEqual(p.create_dirs, [])

    def test_a_path_masks_only_that_path_under_the_root(self):
        m = self.by_dst(mounts.plan(root=self.root, mask=["./data", "deploy/state"]))
        self.assertEqual(m["data"].kind, "tmpfs")
        self.assertEqual(m["deploy/state"].kind, "tmpfs")
        self.assertNotIn("dev/data", m, "./data is root-only")
        self.assertNotIn("sub/repo/data", m)

    def test_a_missing_path_is_created_and_masked(self):
        p = mounts.plan(root=self.root, mask=["./future"])
        full = os.path.join(self.root, "future")
        self.assertEqual(p.create_dirs, [full])
        self.assertEqual(self.by_dst(p)["future"].kind, "tmpfs")
        self.assertTrue(any("does not exist yet" in n for n in p.notes), p.notes)
        mounts.prepare(p)
        self.assertTrue(os.path.isdir(full))

    def test_a_masked_file_wins_over_the_dev_swap(self):
        m = self.by_dst(mounts.plan(root=self.root, mask=[".env"]))
        self.assertEqual(m[".env"].kind, "mask", "masked, not substituted by .env.dev")

    def test_nothing_is_mounted_inside_a_masked_directory(self):
        self.write("proj/data/secrets/k.txt", "x\n")
        self.write("proj/data/.env", "x\n")
        m = self.by_dst(mounts.plan(root=self.root, mask=["./data"]))
        self.assertEqual([k for k in m if k.startswith("data/")], [])

    def test_nested_masks_collapse_to_the_outer_one(self):
        m = self.by_dst(mounts.plan(root=self.root, mask=["./data", "data/sub"]))
        self.assertIn("data", m)
        self.assertNotIn("data/sub", m)

    def test_masks_apply_in_a_read_only_data_grant_too(self):
        ro = self.mkdir("ro")
        self.write("ro/data/live.db", "x\n")
        m = {m.dst: m for m in mounts.plan(root=self.root, data_ro=[ro], mask=["data"]).mounts}
        self.assertEqual(m[os.path.join(ro, "data")].kind, "tmpfs")

    def test_a_grant_root_is_never_masked_and_masked_paths_are_hidden(self):
        p = mounts.plan(root=self.root, mask=["./.", "proj"])    # "./." normalises to the root itself
        self.assertNotIn(self.root, [m.dst for m in p.mounts if m.kind == "tmpfs"])
        p = mounts.plan(root=self.root, mask=["data"])
        self.assertIn(os.path.realpath(os.path.join(self.root, "data")), mounts.hidden(p))
        self.assertIn(os.path.realpath(os.path.join(self.root, "dev", "data")), mounts.covered(p, [self.root]))
