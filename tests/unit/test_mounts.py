import os
import subprocess

from agentbox import mounts
from agentbox.errors import AgentboxError

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

    def test_a_dev_swap_in_a_read_only_grant_hides_its_target(self):
        """The agent sees the stand-in whether the swap mount is read-only or not, so the grant scan must not
        report the real .env behind it (11 Nov: `hidden` required `not m.ro`, which no read-only cover needs --
        they all bind a path onto itself)."""
        ref = self.mkdir("ref")
        self.write("ref/.env", "REAL=1\n")
        self.write("ref/.env.dev", "DEV=1\n")
        p = mounts.plan(root=self.root, data_ro=[ref])
        env = os.path.join(ref, ".env")
        swap = next(m for m in p.mounts if m.dst == env)
        self.assertEqual((swap.kind, swap.ro), ("bind", True))
        self.assertIn(os.path.realpath(env), mounts.hidden(p))
        self.assertNotIn(os.path.realpath(os.path.join(self.root, ".git", "hooks")), mounts.hidden(p),
                         "a read-only cover is still readable, so still not hidden")

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


class WidenedSecretNames(TempCase):
    """Review item 28 (16 Nov): the mask list grew by `*.env`, `.netrc`, `.pgpass`, `*.p12`, `*.pfx`, `*.jks`.
    The dev swap and the exact-name exemptions are unchanged by it."""

    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")
        for rel in ("prod.env", ".netrc", ".pgpass", "tls/cert.p12", "tls/store.pfx", "jvm/keystore.jks",
                    "example.env", ".env", ".env.dev", ".env.example", "notes.txt", "schema.sql"):
            self.write(os.path.join("proj", rel), "SECRET=1\n")

    def test_the_new_names_are_masked_and_the_exemptions_and_swap_still_hold(self):
        m = {os.path.relpath(x.dst, self.root): x for x in mounts.plan(root=self.root).mounts}
        for masked in ("prod.env", ".netrc", ".pgpass", "tls/cert.p12", "tls/store.pfx", "jvm/keystore.jks",
                       "example.env"):
            self.assertEqual(m[masked].kind, "mask", masked)
        for exempt in (".env.example", ".env.dev", "notes.txt", "schema.sql"):
            self.assertNotIn(exempt, m, exempt)
        self.assertEqual((m[".env"].kind, m[".env"].src), ("bind", os.path.join(self.root, ".env.dev")))

    def test_grantcheck_uses_the_same_tuple(self):
        from agentbox import grantcheck
        self.assertIs(grantcheck.SECRET_FILES, mounts.MASK_FILES)


class GitConfigPathsAreCovered(TempCase):
    """Review item 15 (16 Nov): `.git/config` was read-only, but a `core.hooksPath` or `include.path` ALREADY in
    it (husky's `.husky`, a repo's `.githooks`) named a writable path the owner's git runs or reads from."""

    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")
        subprocess.run(["git", "init", "-q", self.root], check=True)

    def cfg(self, key, value):
        subprocess.run(["git", "-C", self.root, "config", key, value], check=True)

    def by_dst(self, p):
        return {os.path.relpath(m.dst, self.root): m for m in p.mounts}

    def test_an_in_grant_hooks_path_is_covered_read_only_and_counts_as_covered(self):
        self.cfg("core.hooksPath", ".husky")
        self.mkdir("proj", ".husky")
        p = mounts.plan(root=self.root)
        m = self.by_dst(p)
        self.assertEqual((m[".husky"].kind, m[".husky"].ro, m[".husky"].src), ("bind", True, os.path.join(self.root, ".husky")))
        self.assertIn(os.path.join(self.root, ".husky"), mounts.covered(p, [self.root]))
        self.assertNotIn(os.path.join(self.root, ".husky"), mounts.hidden(p), "readable, so not hidden from the scan")

    def test_a_missing_hooks_path_is_created_and_covered(self):
        """Otherwise it is the agent's to create, and the owner's next commit runs it."""
        self.cfg("core.hooksPath", "tools/hooks")
        p = mounts.plan(root=self.root)
        self.assertIn(os.path.join(self.root, "tools", "hooks"), p.create_dirs)
        self.assertTrue(self.by_dst(p)["tools/hooks"].ro)
        mounts.prepare(p)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "tools", "hooks")))

    def test_an_include_is_resolved_against_the_config_file_and_covered(self):
        self.cfg("include.path", "../.gitconfig.local")          # relative to .git/, so the root
        self.write("proj/.gitconfig.local", "[user]\n\tname = x\n")
        m = self.by_dst(mounts.plan(root=self.root))
        self.assertEqual((m[".gitconfig.local"].kind, m[".gitconfig.local"].ro), ("bind", True))

    def test_a_missing_include_is_created_empty_and_covered(self):
        self.cfg("include.path", "../local.inc")
        p = mounts.plan(root=self.root)
        self.assertIn(os.path.join(self.root, "local.inc"), p.create_files)
        self.assertTrue(self.by_dst(p)["local.inc"].ro)

    def test_an_included_file_that_sets_hooks_path_is_followed(self):
        self.cfg("include.path", "../extra.inc")
        self.write("proj/extra.inc", "[core]\n\thooksPath = .githooks\n")
        self.mkdir("proj", ".githooks")
        m = self.by_dst(mounts.plan(root=self.root))
        self.assertTrue(m[".githooks"].ro)
        self.assertTrue(m["extra.inc"].ro)

    def test_include_if_and_tilde_paths_are_read(self):
        self.cfg("includeIf.gitdir:/x/.path", "../cond.inc")
        found = mounts.git_config_targets(os.path.join(self.root, ".git"))
        self.assertEqual([(k, t) for _c, k, t in found], [("includeif.gitdir:/x/.path", os.path.join(self.root, "cond.inc"))])
        self.cfg("core.hooksPath", "~/hooks")
        found = dict((k, t) for _c, k, t in mounts.git_config_targets(os.path.join(self.root, ".git")))
        self.assertEqual(found["core.hookspath"], os.path.join(os.path.expanduser("~"), "hooks"))

    def test_a_target_outside_the_grant_or_a_symlink_is_left_to_grantcheck(self):
        outside = self.mkdir("elsewhere")
        self.cfg("core.hooksPath", outside)
        self.cfg("include.path", "../link.inc")
        os.symlink("/etc/hostname", os.path.join(self.root, "link.inc"))
        p = mounts.plan(root=self.root)
        dsts = [m.dst for m in p.mounts]
        self.assertNotIn(outside, dsts)
        self.assertNotIn(os.path.join(self.root, "link.inc"), dsts)
        self.assertEqual(p.create_files, [])

    def test_a_repo_without_these_keys_is_unchanged(self):
        p = mounts.plan(root=self.root)
        self.assertEqual(sorted(os.path.relpath(m.dst, self.root) for m in p.mounts), [".", ".git/config", ".git/hooks"])


class GitConfigThatCannotBeParsed(TempCase):
    """A `core.hooksPath` the launcher cannot read is one it cannot cover (item 15): the plan refuses, as it
    does for an unreadable `.git` file, rather than starting with the hooks path writable."""

    def test_a_bad_config_refuses_the_plan(self):
        root = self.mkdir("proj")
        subprocess.run(["git", "init", "-q", root], check=True)
        with open(os.path.join(root, ".git", "config"), "a") as f:
            f.write("[core\n")
        with self.assertRaises(AgentboxError) as cm:
            mounts.plan(root=root)
        self.assertIn("cannot read this git config", str(cm.exception))


class DevSwapMountPoint(TempCase):
    """Item 37: the empty `.env` mount point is created only where we may write, and only as a file."""

    def test_a_read_only_grant_gets_no_created_env_and_a_note(self):
        root, ref = self.mkdir("proj"), self.mkdir("ref")
        self.write("ref/.env.dev", "DEV=1\n")
        p = mounts.plan(root=root, data_ro=[ref])
        self.assertEqual(p.create_files, [])
        self.assertNotIn(os.path.join(ref, ".env"), [m.dst for m in p.mounts])
        self.assertTrue(any("read-only" in n and ".env" in n for n in p.notes), p.notes)
        mounts.prepare(p)
        self.assertFalse(os.path.lexists(os.path.join(ref, ".env")))

    def test_a_read_write_grant_still_gets_its_mount_point(self):
        root = self.mkdir("proj")
        self.write("proj/.env.dev", "DEV=1\n")
        self.assertEqual(mounts.plan(root=root).create_files, [os.path.join(root, ".env")])

    def test_a_directory_at_the_target_is_skipped_with_a_note(self):
        root = self.mkdir("proj")
        self.mkdir("proj", ".env")
        self.write("proj/.env.dev", "DEV=1\n")
        p = mounts.plan(root=root)
        self.assertNotIn(os.path.join(root, ".env"), [m.dst for m in p.mounts])
        self.assertEqual(p.create_files, [])
        self.assertTrue(any("not a regular file" in n for n in p.notes), p.notes)
