"""`ab profile install`: the plugins the profile declares, installed in a sandbox.

The facts this is built on came out of `make omp-plugin-probe` (decisions.md, 5 Nov), and the two that
drive almost every assertion here are:

  * **a marketplace cannot be pinned by the CLI**, but a local path is a valid source -- so agentbox
    clones the catalog, checks out the declared sha, and adds *that path*. If this ever stops happening,
    `ref` becomes decoration and the install silently takes whatever HEAD is.
  * **the tree records absolute paths**, so the install has to run with the tree mounted where a SESSION
    will mount it (`~/.omp/plugins`). Mounting it at a host path would record paths that resolve to
    nothing inside a session, and it would look fine from the host.

The rest is the shape of any agentbox container: a sealed pair of networks, one hardened squid, nothing
mounted but the tree, teardown on every path -- and no model-host rule, because an install fetches
packages and has no business reaching a model API.
"""
import json
import os
import unittest
from unittest import mock

from agentbox import installer, plugins
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from .helpers import SettingsCase, read_json

SHA = "464db031e7dd1b9a79803ac8ea45f55c7c20c5bc"
REPO_SHA = "9c7a0c2dd1900dcd98aa7be0d84bbee214967d96"
DECLARED = f'''
[[plugin]]
id = "egm@egm"
marketplace = "egm"
source = "google/elephant-goldfish-model"
ref = "{SHA}"

[[npm]]
name = "pi-lens"
version = "4.4.9"

[[repo]]
name = "pi-session-stats"
url = "https://github.com/TimothyStephens/pi-session-stats.git"
ref = "{REPO_SHA}"
'''
STATE = {"npm": [{"name": "pi-lens", "version": "4.4.9"}],
         "marketplace": [{"id": "egm@egm", "entries": [{"version": "0.1.0"}]}]}
IMAGE_DOC = {"localhost/agentbox-omp:latest": {
    "Id": "sha256:ompharness",
    "Config": {"Labels": {"agentbox.cli-version": "18.13.3", "agentbox.node-major": "26"}}}}


class TheScript(unittest.TestCase):
    """What the container runs, built from the declaration."""

    def script(self, text=DECLARED):
        return installer._script(plugins.parse(text))

    def test_a_marketplace_is_cloned_and_checked_out_before_it_is_added(self):
        """The pin, in three lines: clone, check out the sha, then `marketplace add <that path>`. The
        order is the assertion -- adding the source before the checkout would install whatever HEAD was."""
        lines = [ln for ln in self.script().splitlines() if "egm" in ln or "elephant" in ln]
        self.assertRegex(lines[0], r"^rm -rf ")
        self.assertIn("git clone --quiet https://github.com/google/elephant-goldfish-model.git", lines[1])
        self.assertIn(f"checkout --quiet {SHA}", lines[2])
        self.assertIn("rev-parse HEAD", lines[3])
        self.assertTrue(lines[4].startswith("omp plugin marketplace add "), lines[4])
        self.assertIn("omp plugin install egm@egm", lines[5])

    def test_the_checkout_is_verified_not_assumed(self):
        """`git checkout <sha>` of a sha the clone does not have fails, but a clone that silently resolved
        something else would not -- so the script compares rev-parse HEAD with the declared sha. This is
        the only line that makes the lockfile's claim true."""
        for sha in (SHA, REPO_SHA):
            with self.subTest(sha=sha):
                self.assertIn(f"rev-parse HEAD | grep -Fxq {sha}", self.script())

    def test_the_clones_live_inside_the_tree(self):
        """Their paths are recorded by OMP (`sourceUri`) and followed later, so a clone in /tmp would be a
        marketplace that resolves to nothing in a session."""
        for line in self.script().splitlines():
            if line.startswith(("git clone", "omp plugin marketplace add", "omp plugin link")):
                self.assertIn(f"{installer.C_TREE}/{installer.SOURCES}", line, line)

    def test_a_link_is_preceded_by_a_package_json_check_that_names_the_entry_and_the_ref(self):
        """13 Nov, MAGS-CodeDev: OMP's own message was "package.json not found at <path>" -- no entry
        name, no ref, no hint that the upstream had deleted the extension before the pinned ref."""
        text = self.script(DECLARED.replace(f'ref = "{REPO_SHA}"', f'ref = "{REPO_SHA}"\nextension_dir = "ext"'))
        ext = f"{installer.C_TREE}/{installer.SOURCES}/pi-session-stats/ext"
        lines = text.splitlines()
        link = lines.index(f"omp plugin link {ext}")
        guard = lines[link - 1]
        self.assertTrue(guard.startswith(f"test -f {ext}/package.json || "), guard)
        for must in ("[[repo]] pi-session-stats", "not an OMP extension", REPO_SHA, "plugins.toml"):
            self.assertIn(must, guard)
        self.assertIn("exit 1", guard)

    def test_a_repo_is_linked_from_its_extension_dir(self):
        text = installer._script(plugins.parse(DECLARED.replace(
            f'ref = "{REPO_SHA}"', f'ref = "{REPO_SHA}"\nextension_dir = "ext"')))
        self.assertIn(f"omp plugin link {installer.C_TREE}/{installer.SOURCES}/pi-session-stats/ext", text)

    def test_a_repo_without_an_extension_dir_is_linked_at_its_root(self):
        self.assertIn(f"omp plugin link {installer.C_TREE}/{installer.SOURCES}/pi-session-stats\n",
                      self.script())

    def test_an_npm_entry_is_one_install_at_its_exact_version(self):
        self.assertIn("omp plugin install pi-lens@4.4.9", self.script())

    def test_state_is_written_from_plugin_list_not_from_the_install_output(self):
        """`plugin install --json` prints human text (probe, 5 Nov), so what happened is read back from
        state. Without this the lockfile would record what we hoped for."""
        text = self.script()
        self.assertIn(f"omp plugin list --json > {installer.C_TREE}/{installer.STATE}", text)
        self.assertLess(text.index("omp plugin install"), text.index("omp plugin list --json"))

    def test_marketplaces_json_is_copied_out_not_bind_mounted(self):
        """OMP rewrites that file and a tool that renames over a bind mount fails, so it stays on the
        container's tmpfs and is copied into the tree at the end."""
        self.assertIn(f"cp {os.path.join(installer.C_OMP, 'marketplaces.json')} "
                      f"{installer.C_TREE}/{installer.MARKETPLACES_COPY}", self.script())

    def test_it_stops_at_the_first_failure(self):
        self.assertTrue(self.script().startswith("set -eu\n"))

    def test_declaration_order_is_kept(self):
        """A marketplace plugin can depend on a marketplace being added; the file's order is the install
        order, so a reader can predict what runs."""
        text = self.script()
        self.assertLess(text.index("marketplace add"), text.index("pi-lens@4.4.9"))
        self.assertLess(text.index("pi-lens@4.4.9"), text.index("plugin link"))


class TheContainer(SettingsCase):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(IMAGE_DOC)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        self.job = installer.Install(self.settings, "omp", podman=Podman(self.settings.podman))

    def args(self):
        return self.job.container_args("set -eu\ntrue\n")

    def test_the_tree_is_mounted_where_a_session_mounts_it(self):
        """The finding that shapes the whole design: installPath, catalogPath, sourceUri and the
        node_modules entries are absolute, so installing anywhere else records paths that are wrong in a
        session -- and look right on the host."""
        mounts = [a for a in self.args() if a.startswith("--mount=")]
        self.assertEqual(len(mounts), 1, mounts)
        self.assertIn(f"dst={installer.C_TREE}", mounts[0])
        self.assertIn(f"src={os.path.realpath(self.job.staging)}", mounts[0],
                      "the staging tree is mounted; the real tree is swapped in only on success")
        self.assertNotIn(",ro", mounts[0], "the install writes the tree")

    def test_omp_home_is_a_tmpfs_owned_by_the_container_user(self):
        """The bug the first real run found. Without this, podman creates `~/.omp` as the mount point for
        the tree -- root-owned, inside the HOME tmpfs -- and the container runs as the host uid, so OMP's
        first write there is EACCES on `marketplaces.json.tmp`. It is also where everything OMP writes
        outside the tree goes, which is why it must be writable and must NOT persist."""
        tmpfs = [a for a in self.args() if a.startswith("--tmpfs=")]
        omp = [a for a in tmpfs if a.startswith(f"--tmpfs={installer.C_OMP}:")]
        self.assertEqual(len(omp), 1, tmpfs)
        self.assertIn(",U,", omp[0] + ",", "the tmpfs must be chowned to the container user")
        self.assertIn("mode=0700", omp[0])
        self.assertIn("notmpcopyup", omp[0])
        # And the tree sits inside it, so the order of these two matters to podman, not to us.
        self.assertTrue(installer.C_TREE.startswith(installer.C_OMP + "/"))

    def test_nothing_else_is_mounted_and_no_socket_is_given(self):
        argv = self.args()
        self.assertEqual(len([a for a in argv if a.startswith("--mount=")]), 1)
        for bad in ("podman.sock", "/run/agentbox", "type=volume"):
            self.assertNotIn(bad, " ".join(argv))

    def test_it_runs_on_the_sealed_internal_network_only(self):
        argv = self.args()
        self.assertIn(f"--network={self.job.int_net}", argv)
        self.assertEqual(len([a for a in argv if a.startswith("--network=")]), 1)

    def test_the_proxy_is_the_only_way_out(self):
        env = {a.split("=", 1)[1].split("=", 1)[0]: a.split("=", 2)[-1]
               for a in self.args() if a.startswith("--env=")}
        self.assertEqual(env["HTTPS_PROXY"], f"http://{self.job.proxy_name}:3128")
        self.assertEqual(env["HTTP_PROXY"], f"http://{self.job.proxy_name}:3128")

    def test_it_carries_the_fixed_hardening(self):
        argv = " ".join(self.args())
        for flag in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--userns=keep-id"):
            self.assertIn(flag, argv)

    def test_the_argv_is_audited(self):
        """`hardening.audit` is what refuses a weakened argv, and it runs on this one too -- an install
        container is not exempt from the posture every agentbox container has."""
        with mock.patch("agentbox.hardening.audit", side_effect=AgentboxError("refused")) as audit:
            with self.assertRaises(AgentboxError):
                self.args()
        self.assertEqual(audit.call_count, 1)

    def test_the_proxy_has_no_model_host_rule(self):
        """An install fetches packages. The single private address a session may reach is an exception made
        for the local model server, and an install has no business reaching a model API."""
        conf = self.job.squid_conf()
        self.assertNotIn("localmodel", conf)
        self.assertIn("http_access allow packages", conf)
        self.assertIn("http_access deny private", conf)
        self.assertEqual(conf.splitlines()[0].split(": ")[-1], "packages")

    def test_the_networks_are_isolated_and_the_internal_one_is_internal(self):
        a = self.job.podman
        self.job.start_infra()
        creates = [c for c in self.podman_calls() if c[:2] == ["network", "create"]]
        self.assertEqual(len(creates), 2)
        for c in creates:
            self.assertIn("isolate=strict", c)
            self.assertIn("--ipv6=false", c)
        internal = [c for c in creates if "--internal" in c]
        self.assertEqual([c[-1] for c in internal], [self.job.int_net])
        del a

    def test_teardown_removes_everything_it_made(self):
        self.job.teardown()
        removed = [c for c in self.podman_calls() if c[:1] == ["rm"] or c[:2] == ["network", "rm"]]
        names = [c[-1] for c in removed]
        self.assertEqual(sorted(names), sorted([self.job.name, self.job.proxy_name,
                                                self.job.int_net, self.job.egr_net]))

    def test_it_is_named_and_labelled_like_everything_else_agentbox_makes(self):
        """So `make clean` and the leftover checks see it. The project label is `(install)`, which no
        project can be called."""
        self.assertTrue(self.job.name.startswith(self.settings.name_prefix))
        labels = self.job.labels("install")
        self.assertEqual(labels["agentbox.project"], "(install)")
        self.assertEqual(labels["agentbox.session"], self.job.name)
        for k, v in self.settings.labels.items():
            self.assertEqual(labels[k], v)


class TheRun(SettingsCase):
    """`installer.install` end to end against the fake podman."""

    def setUp(self):
        super().setUp()
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(IMAGE_DOC)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        self.declare(DECLARED)

    def declare(self, text):
        src = self.settings.profile_src("omp")
        os.makedirs(src, mode=0o700, exist_ok=True)
        with open(os.path.join(src, "plugins.toml"), "w") as f:
            f.write(text)

    def seed_state(self, plant=None):
        """The fake podman runs no container, so the state file the real install would write is seeded --
        and `_lock` refusing to invent one when it is missing is its own test below. `plant(tree)` is what
        else the container left in the tree, for the tests about an install that wrote something hostile."""
        # The container writes into the STAGING tree (that is what is mounted); the host swaps it in.
        tree = installer.staging_dir(self.settings, "omp")

        def write(argv, **kw):
            import subprocess
            if argv[:1] == ["run"] and "--rm" in argv:
                self.assertIn(f"src={os.path.realpath(tree)}", " ".join(argv), "the staging tree is mounted")
                os.makedirs(os.path.join(tree, installer.OURS), exist_ok=True)
                with open(os.path.join(tree, installer.STATE), "w") as f:
                    json.dump(STATE, f)
                with open(os.path.join(tree, "installed-marker"), "w") as f:
                    f.write("new")
                if plant is not None:
                    plant(tree)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return write

    def run_install(self, plant=None, **kw):
        pod = Podman(self.settings.podman)
        real = pod.run
        write = self.seed_state(plant)

        self.run_kwargs = None

        def runner(argv, **kwargs):
            if argv[:1] == ["run"] and "--rm" in argv:
                self.run_kwargs = kwargs
                real(["--version"], check=False)          # keep the call log honest about ordering
                return write(argv, **kwargs)
            return real(argv, **kwargs)
        with mock.patch.object(pod, "run", side_effect=runner):
            return installer.install(self.settings, "omp", podman=pod, warn=lambda m: None, **kw)

    def test_the_install_container_gets_an_install_sized_timeout_not_the_question_sized_one(self):
        """13 Nov: the first `ab profile install omp` on a cold host died at `podman run --rm timed out`
        -- the launcher's 120 s default, meant for `podman inspect`, not for clones and npm installs."""
        self.run_install()
        self.assertEqual(self.run_kwargs.get("timeout"), installer.RUN_TIMEOUT)
        self.assertGreaterEqual(installer.RUN_TIMEOUT, 600)

    def test_the_lockfile_records_what_was_declared_and_what_omp_reported(self):
        doc = self.run_install()
        on_disk = read_json(installer.lock_path(self.settings, "omp"))
        self.assertEqual(on_disk, doc)
        self.assertEqual(doc["version"], installer.LOCK_VERSION)
        self.assertEqual(doc["state"], STATE)
        self.assertEqual([d.get("id") or d.get("name") for d in doc["declared"]],
                         ["egm@egm", "pi-lens", "pi-session-stats"])
        self.assertEqual([d["ref"] for d in doc["declared"] if "ref" in d], [SHA, REPO_SHA])

    def test_the_lockfile_records_the_image_it_was_built_against(self):
        """The drift this whole item exists to end: plugins installed against one OMP, loaded by another.
        The tree can now say which OMP (and which node major) it was built for."""
        doc = self.run_install()
        self.assertEqual(doc["image"]["cli_version"], "18.13.3")
        self.assertEqual(doc["image"]["node_major"], "26")
        self.assertEqual(doc["image"]["id"], "sha256:ompharness")
        self.assertEqual(doc["image"]["ref"], "localhost/agentbox-omp:latest")

    def test_the_lockfile_records_the_profile_hash(self):
        doc = self.run_install()
        self.assertTrue(doc["profile_hash"], "no profile hash recorded")

    def test_the_tree_is_replaced_not_updated(self):
        """OMP's package.json, bun.lock and installed_plugins.json are cumulative, so installing into a
        tree that already has plugins would leave removed ones installed and make the lockfile describe
        something else."""
        tree = installer.tree_dir(self.settings, "omp")
        os.makedirs(tree, exist_ok=True)
        with open(os.path.join(tree, "leftover"), "w") as f:
            f.write("from an older install")
        self.run_install()
        self.assertFalse(os.path.exists(os.path.join(tree, "leftover")))
        self.assertTrue(os.path.isfile(os.path.join(tree, "installed-marker")), "the built tree is in place")
        self.assertFalse(os.path.exists(installer.staging_dir(self.settings, "omp")), "no staging dir left")
        self.assertFalse(os.path.exists(tree + ".previous"), "the old tree is gone")

    def test_a_failed_install_keeps_the_previous_tree_and_its_lockfile(self):
        """13 Nov: one dead [[repo]] entry failed the install, and the tree had already been emptied, so
        egm and the npm plugins that had installed fine were gone from sessions until plugins.toml was
        fixed. The build happens beside the tree now; a failure leaves the tree as it was and says so."""
        first = self.run_install()
        tree = installer.tree_dir(self.settings, "omp")
        pod = Podman(self.settings.podman)
        real = pod.run

        def runner(argv, **kw):
            import subprocess
            if argv[:1] == ["run"] and "--rm" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "✘ Failed to link: package.json not found")
            return real(argv, **kw)
        with mock.patch.object(pod, "run", side_effect=runner), self.assertRaises(AgentboxError) as cm:
            installer.install(self.settings, "omp", podman=pod, warn=lambda m: None)
        self.assertIn("package.json not found", str(cm.exception))
        self.assertIn(f"previous tree at {tree} is kept", str(cm.exception))
        self.assertEqual(installer.read_lock(self.settings, "omp"), first, "the lockfile is the first install's")
        self.assertTrue(os.path.isfile(os.path.join(tree, "installed-marker")))
        self.assertFalse(os.path.exists(installer.staging_dir(self.settings, "omp")), "no staging dir left")

    def test_a_first_install_that_fails_leaves_no_tree_at_all(self):
        pod = Podman(self.settings.podman)
        real = pod.run

        def runner(argv, **kw):
            import subprocess
            if argv[:1] == ["run"] and "--rm" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            return real(argv, **kw)
        with mock.patch.object(pod, "run", side_effect=runner), self.assertRaises(AgentboxError) as cm:
            installer.install(self.settings, "omp", podman=pod, warn=lambda m: None)
        self.assertNotIn("previous tree", str(cm.exception))
        self.assertFalse(os.path.exists(installer.tree_dir(self.settings, "omp")))
        self.assertFalse(os.path.exists(installer.staging_dir(self.settings, "omp")))

    def test_a_symlinked_tree_is_refused_rather_than_followed(self):
        tree = installer.tree_dir(self.settings, "omp")
        os.makedirs(os.path.dirname(tree), exist_ok=True)
        os.symlink(self.mkdir("elsewhere"), tree)
        with self.assertRaises(AgentboxError) as cm:
            self.run_install()
        self.assertIn("symlink", str(cm.exception))

    def test_a_symlink_out_of_the_staging_tree_is_refused_and_the_tree_is_not_kept(self):
        """The staging tree is the output of a container that ran plugin install scripts. A link that
        leaves it would be read by the host (state.json), written through by the host (the lockfile), and
        mounted into every later session; so the whole install is refused, and the previous tree stays."""
        first = self.run_install()
        tree = installer.tree_dir(self.settings, "omp")
        outside = self.write("outside/secret", "host file")
        for name, target in (("node_modules/evil", outside),                 # absolute
                             ("node_modules/.bin/up", "../../../../outside"),   # relative, out of the tree
                             (".agentbox/state.json", outside)):               # the file the host reads
            with self.subTest(link=name):
                def plant(staging, name=name, target=target):
                    path = os.path.join(staging, name)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    if os.path.lexists(path):
                        os.unlink(path)
                    os.symlink(target, path)
                with self.assertRaises(AgentboxError) as cm:
                    self.run_install(plant=plant)
                self.assertIn("symlink", str(cm.exception))
                self.assertIn("previous tree", str(cm.exception))
                self.assertEqual(installer.read_lock(self.settings, "omp"), first)
                self.assertFalse(os.path.exists(installer.staging_dir(self.settings, "omp")))
                self.assertFalse(os.path.islink(os.path.join(tree, name)), "the planted link reached the tree")

    def test_a_relative_symlink_inside_the_tree_is_kept(self):
        """npm writes `node_modules/.bin/<cmd> -> ../<pkg>/bin.js`; that is the tree working as intended."""
        def plant(staging):
            os.makedirs(os.path.join(staging, "node_modules", ".bin"))
            os.makedirs(os.path.join(staging, "node_modules", "pi-lens"))
            os.symlink("../pi-lens/cli.js", os.path.join(staging, "node_modules", ".bin", "pi-lens"))
            os.symlink(".", os.path.join(staging, "node_modules", "self"))
        self.run_install(plant=plant)
        tree = installer.tree_dir(self.settings, "omp")
        self.assertTrue(os.path.islink(os.path.join(tree, "node_modules", ".bin", "pi-lens")))

    def test_the_lockfile_is_read_and_written_without_following_links(self):
        """`_lock` on its own: the walk above is the first wall, this is the second. A state.json that is a
        link is not read; a planted install.json.tmp or install.json that is a link is not written
        through -- the host file behind it keeps its content, and the lockfile is a fresh regular file."""
        staging = self.mkdir("staging")
        os.makedirs(os.path.join(staging, installer.OURS))
        victim = self.write("victim.json", json.dumps(STATE))
        entries = plugins.parse(DECLARED)
        pin = {"ref": "x", "id": "", "cli_version": "", "node_major": ""}
        os.symlink(victim, os.path.join(staging, installer.STATE))
        with self.assertRaises(AgentboxError) as cm:
            installer._lock(self.settings, "omp", entries, pin, staging)
        self.assertIn("no readable state", str(cm.exception))
        os.unlink(os.path.join(staging, installer.STATE))
        with open(os.path.join(staging, installer.STATE), "w") as f:
            json.dump(STATE, f)
        lock = os.path.join(staging, installer.LOCKFILE)
        os.symlink(victim, lock + ".tmp")
        os.symlink(victim, lock)
        doc = installer._lock(self.settings, "omp", entries, pin, staging)
        self.assertEqual(read_json(victim), STATE, "the file behind the planted link was written through")
        self.assertFalse(os.path.islink(lock))
        self.assertEqual(read_json(lock), doc)
        self.assertFalse(os.path.lexists(lock + ".tmp"))
        # And `.agentbox` itself replaced by a link: O_NOFOLLOW guards the last component only.
        other = self.mkdir("elsewhere")
        import shutil
        shutil.rmtree(os.path.join(staging, installer.OURS))
        os.symlink(other, os.path.join(staging, installer.OURS))
        with self.assertRaises(AgentboxError) as cm:
            installer._lock(self.settings, "omp", entries, pin, staging)
        self.assertIn("not a directory", str(cm.exception))
        self.assertEqual(os.listdir(other), [])

    def test_a_second_install_of_the_same_agent_is_refused_while_one_runs(self):
        """Two at once would build in the same staging directory and swap over each other."""
        import fcntl
        tree = installer.tree_dir(self.settings, "omp")
        os.makedirs(os.path.dirname(tree), exist_ok=True)
        fd = os.open(tree + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(AgentboxError) as cm:
            self.run_install()
        self.assertIn("another `ab profile install omp` is running", str(cm.exception))
        self.assertEqual([c for c in self.podman_calls() if c[:1] == ["run"]], [], "nothing was started")
        self.assertFalse(os.path.exists(installer.staging_dir(self.settings, "omp")))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.run_install()
        self.assertTrue(os.path.isfile(os.path.join(tree, "installed-marker")))
        self.assertTrue(os.path.isfile(tree + ".lock"), "the lock file stays; removing it would reopen the race")

    def test_an_interrupted_install_removes_its_staging_tree_too(self):
        """Not only an AgentboxError: Ctrl-C or a traceback in the middle must not leave `.building` beside
        the tree for the next install (or a curious session) to find."""
        first = self.run_install()
        tree = installer.tree_dir(self.settings, "omp")
        pod = Podman(self.settings.podman)
        real = pod.run

        def runner(argv, **kw):
            if argv[:1] == ["run"] and "--rm" in argv:
                raise KeyboardInterrupt
            return real(argv, **kw)
        with mock.patch.object(pod, "run", side_effect=runner), self.assertRaises(KeyboardInterrupt):
            installer.install(self.settings, "omp", podman=pod, warn=lambda m: None)
        self.assertFalse(os.path.exists(installer.staging_dir(self.settings, "omp")), "staging dir left")
        self.assertEqual(installer.read_lock(self.settings, "omp"), first, "the previous tree is kept")
        self.assertTrue(os.path.isfile(os.path.join(tree, "installed-marker")))
        names = [c[-1] for c in self.podman_calls() if c[:1] == ["rm"]]
        self.assertIn(installer.Install(self.settings, "omp").proxy_name, names, "teardown did not run")

    def test_a_symlinked_previous_tree_is_refused_before_anything_runs(self):
        tree = installer.tree_dir(self.settings, "omp")
        os.makedirs(os.path.dirname(tree), exist_ok=True)
        os.symlink(self.mkdir("elsewhere"), tree + ".previous")
        with self.assertRaises(AgentboxError) as cm:
            self.run_install()
        self.assertIn("symlink", str(cm.exception))
        self.assertEqual([c for c in self.podman_calls() if c[:1] == ["run"]], [])
        self.assertTrue(os.path.islink(tree + ".previous"), "the link was removed or renamed through")
        with self.assertRaises(AgentboxError):
            installer._swap_in(self.mkdir("staging"), tree)
        self.assertTrue(os.path.islink(tree + ".previous"))

    def test_a_leftover_previous_tree_is_put_back_when_the_tree_is_missing(self):
        """An install interrupted between the two renames of the swap leaves the last good tree at
        `.previous` and nothing at the tree. The next install starts by putting it back, so a failure of
        that install still has a previous tree to keep."""
        first = self.run_install()
        tree = installer.tree_dir(self.settings, "omp")
        os.rename(tree, tree + ".previous")
        pod = Podman(self.settings.podman)
        real = pod.run

        def runner(argv, **kw):
            import subprocess
            if argv[:1] == ["run"] and "--rm" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            return real(argv, **kw)
        warnings = []
        with mock.patch.object(pod, "run", side_effect=runner), self.assertRaises(AgentboxError) as cm:
            installer.install(self.settings, "omp", podman=pod, warn=warnings.append)
        self.assertIn(f"previous tree at {tree} is kept", str(cm.exception))
        self.assertTrue(any("interrupted" in w for w in warnings), warnings)
        self.assertEqual(installer.read_lock(self.settings, "omp"), first)
        self.assertFalse(os.path.lexists(tree + ".previous"))
        # And a successful install after a leftover `.previous` ends with one tree and nothing beside it.
        os.rename(tree, tree + ".previous")
        self.run_install()
        self.assertTrue(os.path.isfile(os.path.join(tree, "installed-marker")))
        self.assertFalse(os.path.lexists(tree + ".previous"))

    def test_no_state_means_no_lockfile_and_an_error(self):
        """An install that reported nothing is not an install that worked."""
        pod = Podman(self.settings.podman)
        with self.assertRaises(AgentboxError) as cm:
            installer.install(self.settings, "omp", podman=pod, warn=lambda m: None)
        self.assertIn("no readable state", str(cm.exception))
        self.assertIsNone(installer.read_lock(self.settings, "omp"))

    def test_a_failed_container_tears_everything_down(self):
        pod = Podman(self.settings.podman)
        real = pod.run

        def runner(argv, **kw):
            import subprocess
            if argv[:1] == ["run"] and "--rm" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "omp plugin install: boom")
            return real(argv, **kw)
        with mock.patch.object(pod, "run", side_effect=runner):
            with self.assertRaises(AgentboxError) as cm:
                installer.install(self.settings, "omp", podman=pod, warn=lambda m: None)
        self.assertIn("boom", str(cm.exception))
        names = [c[-1] for c in self.podman_calls() if c[:1] == ["rm"] or c[:2] == ["network", "rm"]]
        job = installer.Install(self.settings, "omp")
        for n in (job.name, job.proxy_name, job.int_net, job.egr_net):
            self.assertIn(n, names, "an install that failed left something running")

    def test_nothing_declared_is_an_error_not_an_empty_tree(self):
        self.declare("# nothing yet\n")
        with self.assertRaises(AgentboxError) as cm:
            self.run_install()
        self.assertIn("declares no plugins", str(cm.exception))

    def test_dry_run_changes_nothing(self):
        doc = installer.install(self.settings, "omp", podman=Podman(self.settings.podman),
                                warn=lambda m: None, dry_run=True)
        self.assertTrue(doc["dry_run"])
        self.assertIn("omp plugin marketplace add", doc["script"])
        self.assertFalse(os.path.exists(installer.tree_dir(self.settings, "omp")))
        self.assertEqual([c for c in self.podman_calls() if c[:1] == ["run"]], [])

    def test_a_missing_image_says_which_make_target_to_run(self):
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = "{}"
        with self.assertRaises(AgentboxError) as cm:
            self.run_install()
        self.assertIn("make omp-image", str(cm.exception))


class TheCli(SettingsCase):
    def test_install_is_a_profile_subcommand(self):
        code, out = self.ab("profile", "install", "--help")
        self.assertEqual(code, 0)
        self.assertIn("plugins.toml", out)

    def test_it_names_the_agent_explicitly(self):
        """No default agent: an install replaces a tree, so which agent is not something to guess."""
        code, out = self.ab("profile", "install")
        self.assertNotEqual(code, 0)
        self.assertIn("agent", out)

    def test_dry_run_prints_the_script_and_touches_nothing(self):
        src = self.settings.profile_src("omp")
        os.makedirs(src, mode=0o700, exist_ok=True)
        with open(os.path.join(src, "plugins.toml"), "w") as f:
            f.write(DECLARED)
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(IMAGE_DOC)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        code, out = self.ab("profile", "install", "omp", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("omp plugin install egm@egm", out)
        self.assertFalse(os.path.exists(installer.tree_dir(self.settings, "omp")))
