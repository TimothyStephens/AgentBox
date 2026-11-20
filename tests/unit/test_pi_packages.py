"""Pi packages: declared in the Pi profile's plugins.toml, built by `ab profile install pi`, loaded in place.

What this rests on (Pi 1.5.2, docs/pi-proposal.md): Pi installs `npm:` packages under `<agent-dir>/npm` and
`git:` packages under `<agent-dir>/git`, and loads a package its settings.json lists by LOCAL PATH in place,
never modifying it. So agentbox drives npm and git itself, in the same one-container install as OMP's and
Claude's, into a tree a session mounts read-only under `~/.pi`; the generated settings.json lists the paths.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agentbox import installer, plugins, session
from agentbox.adapters import pi
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from .helpers import SettingsCase, read_file, read_json
from .test_omp import OmpSessionBase

SHA = "9c7a0c2dd1900dcd98aa7be0d84bbee214967d96"
DECLARED = f'''
[[npm]]
name = "@example/pi-tools"
version = "1.2.3"

[[repo]]
name = "review-kit"
url = "https://example.com/review-kit.git"
ref = "{SHA}"
extension_dir = "pkg"
'''
HARNESS = "localhost/agentbox-pi:latest"
IMAGE_DOC = {HARNESS: {"Id": "sha256:piharness",
                       "Config": {"Labels": {"agentbox.cli-version": "1.5.2", "agentbox.node-major": "26"}}}}
T = installer.C_PI_TREE
NPM_PATH = f"{pi.TREE_DST}/npm/node_modules/@example/pi-tools"
GIT_PATH = f"{pi.TREE_DST}/git/review-kit/pkg"


def entries(text=DECLARED):
    return plugins.parse(text, plugins.sections_for("pi"))


def fake_tree(settings, *, npm=True, repo=True, lock=True, cli_version="1.5.2", declared=None):
    """A tree as `ab profile install pi` leaves one: the npm prefix, the checkout, and the lockfile."""
    tree = installer.tree_dir(settings, "pi")
    os.makedirs(tree, exist_ok=True)
    if npm:
        d = os.path.join(tree, "npm", "node_modules", "@example", "pi-tools")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "package.json"), "w") as f:
            json.dump({"name": "@example/pi-tools", "version": "1.2.3"}, f)
    if repo:
        os.makedirs(os.path.join(tree, "git", "review-kit", "pkg", "skills"), exist_ok=True)
    if lock:
        doc = {"version": installer.LOCK_VERSION, "agent": "pi",
               "image": {"ref": HARNESS, "cli_version": cli_version, "node_major": "26"},
               "declared": [installer._declared(e) for e in entries(declared or DECLARED)],
               "state": {"npm": [{"name": "@example/pi-tools", "version": "1.2.3"}],
                         "repo": [{"name": "review-kit", "commit": SHA}]}}
        os.makedirs(os.path.join(tree, installer.OURS), exist_ok=True)
        with open(installer.lock_path(settings, "pi"), "w") as f:
            json.dump(doc, f)
    return tree


class TheDeclaration(unittest.TestCase):
    """A Pi profile's plugins.toml takes `[[npm]]` and `[[repo]]`, validated exactly as OMP's are."""

    def test_pi_takes_npm_and_repo_only(self):
        self.assertEqual(plugins.sections_for("pi"), ("npm", "repo"))

    def test_the_two_kinds(self):
        npm, repo = entries()
        self.assertEqual((npm.kind, npm.name, npm.version), ("npm", "@example/pi-tools", "1.2.3"))
        self.assertEqual((repo.kind, repo.name, repo.url, repo.ref, repo.extension_dir),
                         ("repo", "review-kit", "https://example.com/review-kit.git", SHA, "pkg"))

    def test_a_marketplace_entry_is_refused_by_name(self):
        text = DECLARED + f'\n[[plugin]]\nid = "egm@egm"\nmarketplace = "egm"\nsource = "a/b"\nref = "{SHA}"\n'
        with self.assertRaises(AgentboxError) as cm:
            entries(text)
        self.assertIn("[[plugin]]", str(cm.exception))
        self.assertIn("cannot be used by this agent", str(cm.exception))

    def test_the_same_pins_are_required(self):
        bad = {"a version range": DECLARED.replace('"1.2.3"', '"^1.2.3"'),
               "a dist-tag": DECLARED.replace('"1.2.3"', '"latest"'),
               "a branch": DECLARED.replace(f'"{SHA}"', '"main"'),
               "a short sha": DECLARED.replace(f'"{SHA}"', f'"{SHA[:12]}"'),
               "an ssh url": DECLARED.replace("https://example.com/", "git@example.com:"),
               "http": DECLARED.replace("https://", "http://"),
               "credentials": DECLARED.replace("https://", "https://user:tok@"),
               "a dir that leaves the repo": DECLARED.replace('"pkg"', '"../x"'),
               "an unknown key": DECLARED.replace('version = "1.2.3"', 'version = "1.2.3"\nsubdir = "x"')}
        for why, text in bad.items():
            with self.subTest(why=why), self.assertRaises(AgentboxError):
                entries(text)


class TheScript(unittest.TestCase):
    def script(self, text=DECLARED):
        return installer.script_for("pi", entries(text))

    def test_an_npm_package_is_one_npm_install_into_the_prefix_with_scripts_off(self):
        self.assertIn(f"npm install --prefix {T}/npm --no-audit --no-fund --ignore-scripts --legacy-peer-deps "
                      "@example/pi-tools@1.2.3\n", self.script())

    def test_the_prefix_gets_its_own_package_json_first(self):
        text = self.script()
        self.assertLess(text.index(f"> {T}/npm/package.json"), text.index("npm install"))

    def test_a_git_package_is_cloned_checked_out_and_verified_in_order(self):
        clone = f"{T}/git/review-kit"
        lines = self.script().splitlines()
        i = lines.index(f"rm -rf {clone}")
        self.assertEqual(lines[i + 1], f"git clone --quiet https://example.com/review-kit.git {clone}")
        self.assertEqual(lines[i + 2], f"git -C {clone} checkout --quiet {SHA}")
        self.assertEqual(lines[i + 3], f"git -C {clone} rev-parse HEAD | grep -Fxq {SHA}")
        self.assertTrue(lines[i + 4].startswith(f"test -d {clone}/pkg || "), lines[i + 4])
        self.assertIn("[[repo]] review-kit", lines[i + 4])
        self.assertEqual(lines[i + 5], f"if [ -f {clone}/pkg/package.json ]; then npm install --prefix {clone}/pkg "
                                       "--omit=dev --no-audit --no-fund --ignore-scripts --legacy-peer-deps; fi")

    def test_every_npm_install_ignores_scripts(self):
        installs = [ln for ln in self.script().splitlines() if "npm install" in ln]
        self.assertEqual(len(installs), 2)
        for ln in installs:
            self.assertIn("--ignore-scripts", ln)

    def test_no_pi_command_runs(self):
        """`pi install` writes settings, needs PI_OFFLINE off and runs npm with scripts on."""
        self.assertNotRegex(self.script(), r"(^|\s)pi\s")

    def test_state_is_written_last_from_the_tree(self):
        text = self.script()
        last = text[text.rindex("\npython3 -I -c ") + 1:]
        self.assertLess(text.rindex("npm install"), text.rindex("python3 -I -c "))
        self.assertIn(f"{T}/{installer.STATE} npm=@example/pi-tools=1.2.3={T}/npm/node_modules/@example/pi-tools "
                      f"repo=review-kit={SHA}={T}/git/review-kit", last)

    def test_it_stops_at_the_first_failure(self):
        self.assertTrue(self.script().startswith("set -eu\n"))

    def test_the_script_is_valid_shell(self):
        subprocess.run(["sh", "-n", "-c", self.script()], check=True)

    def test_the_state_script_reads_versions_and_refuses_a_wrong_one(self):
        """Run here against a fake prefix: the version recorded is the installed package.json's, and an
        install that resolved something other than the declared version fails."""
        with tempfile.TemporaryDirectory(prefix="abt-pistate-") as d:
            pkg = os.path.join(d, "node_modules", "x")
            os.makedirs(pkg)
            with open(os.path.join(pkg, "package.json"), "w") as f:
                json.dump({"name": "x", "version": "1.0.0"}, f)
            out = os.path.join(d, "state.json")
            ok = subprocess.run([sys.executable, "-I", "-c", installer._PI_STATE, out, f"npm=x=1.0.0={pkg}"],
                                capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(read_json(out)["npm"], [{"name": "x", "version": "1.0.0", "path": pkg}])
            bad = subprocess.run([sys.executable, "-I", "-c", installer._PI_STATE, out, f"npm=x=2.0.0={pkg}"],
                                 capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("not the declared 2.0.0", bad.stderr)


class TheContainer(SettingsCase):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(IMAGE_DOC)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        self.job = installer.Install(self.settings, "pi", podman=Podman(self.settings.podman))

    def args(self):
        return self.job.container_args("set -eu\ntrue\n")

    def test_the_tree_is_mounted_where_a_session_mounts_it(self):
        mounts = [a for a in self.args() if a.startswith("--mount=")]
        self.assertEqual(len(mounts), 1, mounts)
        self.assertIn(f"dst={pi.TREE_DST}", mounts[0])
        self.assertIn(f"src={os.path.realpath(self.job.staging)}", mounts[0])
        self.assertEqual(installer.tree_dst("pi"), pi.TREE_DST, "one path, used by the installer and the adapter")
        self.assertEqual(self.job.tree, os.path.join(self.settings.seed_dir("pi"), "plugins"))

    def test_pi_home_is_a_tmpfs_of_the_container_user_and_the_pi_image_runs(self):
        argv = self.args()
        [t] = [a for a in argv if a.startswith(f"--tmpfs={pi.C_PI}:")]
        self.assertIn(",U,", t)
        self.assertFalse([a for a in argv if a.startswith(f"--tmpfs={installer.C_OMP}:")])
        self.assertIn(HARNESS, argv)
        self.assertNotIn("podman.sock", " ".join(argv))

    def test_the_proxy_has_packages_egress_and_no_model_host(self):
        conf = self.job.squid_conf()
        self.assertEqual(conf.splitlines()[0].split(": ")[-1], "packages")
        self.assertNotIn("localmodel", conf)


class TheRun(SettingsCase):
    """`installer.install(settings, "pi")` end to end against the fake podman."""

    def setUp(self):
        super().setUp()
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(IMAGE_DOC)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        self.write("config/pi/profile/plugins.toml", DECLARED)

    def test_the_lockfile_names_the_pi_image_and_the_declaration(self):
        pod = Podman(self.settings.podman)
        real = pod.run
        staging = installer.staging_dir(self.settings, "pi")
        state = {"npm": [{"name": "@example/pi-tools", "version": "1.2.3"}], "repo": [{"name": "review-kit"}]}

        def runner(argv, **kw):
            if argv[:1] == ["run"] and "--rm" in argv:
                self.assertIn("npm install --prefix", argv[-1])
                os.makedirs(os.path.join(staging, installer.OURS), exist_ok=True)
                with open(os.path.join(staging, installer.STATE), "w") as f:
                    json.dump(state, f)
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real(argv, **kw)
        with mock.patch.object(pod, "run", side_effect=runner):
            doc = installer.install(self.settings, "pi", podman=pod, warn=lambda m: None)
        self.assertEqual(read_json(installer.lock_path(self.settings, "pi")), doc)
        self.assertEqual(doc["agent"], "pi")
        self.assertEqual((doc["image"]["ref"], doc["image"]["cli_version"]), (HARNESS, "1.5.2"))
        self.assertEqual(doc["state"], state)
        self.assertEqual([d["name"] for d in doc["declared"]], ["@example/pi-tools", "review-kit"])

    def test_dry_run_prints_the_pi_script(self):
        doc = installer.install(self.settings, "pi", podman=Podman(self.settings.podman), dry_run=True)
        self.assertIn("--ignore-scripts", doc["script"])
        self.assertFalse(os.path.exists(installer.tree_dir(self.settings, "pi")))


class TheSession(OmpSessionBase):
    """What a Pi session does with the tree: mount it read-only under ~/.pi, list the paths in settings.json."""

    def setUp(self):
        super().setUp()
        self.warned = []
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(IMAGE_DOC)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)

    def declare(self, text=DECLARED):
        self.write("config/pi/profile/plugins.toml", text)

    def start(self, *, no_profile=False):
        self.project("p1", preset="python")
        st = self.settings_now()
        s = session.make_session(st, "pi", session.load_project(st, "p1"), warn=self.warned.append,
                                 no_profile=no_profile)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s

    def mounts(self, s):
        return {a.split("dst=")[1].split(",")[0]: a for a in s.run_args(s.agent_command())
                if a.startswith("--mount=type=bind")}

    def settings_json(self, s):
        return json.loads(read_file(os.path.join(s.state_dir, "agent", "settings.json")))

    def test_the_tree_is_mounted_read_only_under_pi_home(self):
        self.declare()
        tree = fake_tree(self.settings_now())
        s = self.start()
        m = self.mounts(s).get(pi.TREE_DST)
        self.assertTrue(m, self.mounts(s))
        self.assertIn(f"src={tree}", m)
        self.assertTrue(m.endswith(",ro"), m)
        self.assertTrue(pi.TREE_DST.startswith(pi.C_PI + "/"), "under the adapter's mount root")
        self.assertFalse(pi.TREE_DST.startswith(pi.C_AGENT + "/"), "not inside the per-project agent dir")
        self.assertFalse([w for w in self.warned if "package" in w.lower()], self.warned)

    def test_settings_json_lists_each_package_by_its_local_path(self):
        self.declare()
        fake_tree(self.settings_now())
        s = self.start()
        doc = self.settings_json(s)
        self.assertEqual(doc["packages"], [NPM_PATH, GIT_PATH])
        self.assertEqual(doc["defaultProjectTrust"], "never")
        notes = read_file(os.path.join(s.settings.session_run_dir(s.name), "pi-AGENTS.md"))
        self.assertIn(NPM_PATH, notes)
        self.assertIn("--no-approve", s.agent_command())
        self.assertIn("--env=PI_OFFLINE=1", s.run_args(s.agent_command()))

    def test_a_repo_at_its_root_is_listed_at_the_checkout(self):
        text = DECLARED.replace('extension_dir = "pkg"\n', "")
        self.declare(text)
        tree = fake_tree(self.settings_now(), declared=text)
        os.makedirs(os.path.join(tree, "git", "review-kit"), exist_ok=True)
        self.assertEqual(self.settings_json(self.start())["packages"],
                         [NPM_PATH, f"{pi.TREE_DST}/git/review-kit"])

    def test_no_tree_is_a_one_line_note_and_no_packages(self):
        self.declare()
        s = self.start()
        self.assertNotIn(pi.TREE_DST, self.mounts(s))
        self.assertNotIn("packages", self.settings_json(s))
        notes = [w for w in self.warned if "ab profile install pi" in w]
        self.assertEqual(len(notes), 1, self.warned)
        self.assertIn("@example/pi-tools", notes[0])

    def test_nothing_declared_means_nothing_mounted_and_nothing_said(self):
        fake_tree(self.settings_now())
        s = self.start()
        self.assertNotIn(pi.TREE_DST, self.mounts(s))
        self.assertNotIn("packages", self.settings_json(s))
        self.assertFalse([w for w in self.warned if "profile install" in w], self.warned)

    def test_no_profile_mounts_nothing_and_lists_nothing(self):
        self.declare()
        fake_tree(self.settings_now())
        s = self.start(no_profile=True)
        self.assertNotIn(pi.TREE_DST, self.mounts(s))
        self.assertNotIn("packages", self.settings_json(s))
        self.assertFalse([w for w in self.warned if "profile install" in w], self.warned)

    def test_a_declared_package_the_tree_lacks_is_left_out_and_named(self):
        self.declare()
        fake_tree(self.settings_now(), repo=False)
        s = self.start()
        self.assertEqual(self.settings_json(s)["packages"], [NPM_PATH])
        self.assertTrue([w for w in self.warned if "review-kit" in w and "ab profile install pi" in w], self.warned)

    def test_a_broken_plugins_toml_warns_and_the_session_starts(self):
        self.declare('[[npm]]\nname = "x"\nversion = "latest"\n')
        fake_tree(self.settings_now())
        s = self.start()
        self.assertNotIn(pi.TREE_DST, self.mounts(s))
        self.assertTrue([w for w in self.warned if "plugins.toml" in w], self.warned)

    def test_an_edited_plugins_toml_is_reported_as_stale(self):
        self.declare(DECLARED.replace('extension_dir = "pkg"\n', ""))
        fake_tree(self.settings_now())                 # the lock says DECLARED
        self.start()
        self.assertTrue([w for w in self.warned if "plugins.toml has changed" in w], self.warned)

    def test_a_tree_built_against_another_pi_is_reported(self):
        self.declare()
        fake_tree(self.settings_now(), cli_version="1.4.0")
        self.start()
        self.assertTrue([w for w in self.warned if "1.4.0" in w and "1.5.2" in w and "ab profile install pi" in w],
                        self.warned)

    def test_a_symlinked_tree_is_refused(self):
        self.declare()
        real = self.mkdir("elsewhere")
        os.makedirs(self.settings_now().seed_dir("pi"), exist_ok=True)
        os.symlink(real, installer.tree_dir(self.settings_now(), "pi"))
        with self.assertRaises(AgentboxError) as cm:
            self.start()
        self.assertIn("symlink", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class ProfilePackages(TheSession):
    """Pi packages that live IN the profile (`packages/<name>/`, a package.json with a `pi` manifest), as
    pi-essentials does since 20 Nov: snapshotted with the rest of the profile, mounted read-only at
    ~/.pi/profile-packages, and listed in settings.json after the installed tree's. Nothing is fetched for
    them: the profile is the source, as for skills and prompts."""

    def package(self, name, manifest='{"pi": {"extensions": ["./index.ts"]}}'):
        self.write(f"config/pi/profile/packages/{name}/package.json", manifest)
        self.write(f"config/pi/profile/packages/{name}/index.ts", "export default function () {}\n")

    def test_listed_after_the_tree_and_mounted_read_only(self):
        self.declare()
        fake_tree(self.settings_now())
        self.package("pi-essentials")
        s = self.start()
        self.assertEqual(self.settings_json(s)["packages"],
                         [NPM_PATH, GIT_PATH, f"{pi.PROFILE_PACKAGES_DST}/pi-essentials"])
        m = self.mounts(s).get(pi.PROFILE_PACKAGES_DST)
        self.assertTrue(m and m.endswith(",ro"), self.mounts(s))
        self.assertNotIn(f"src={self.settings_now().profile_src('pi')}", m,
                         "the session's snapshot is mounted, not the live profile")
        self.assertFalse(pi.PROFILE_PACKAGES_DST.startswith(pi.C_AGENT + "/"), "not inside the per-project agent dir")

    def test_without_a_plugins_toml(self):
        self.package("pi-essentials")
        self.assertEqual(self.settings_json(self.start())["packages"], [f"{pi.PROFILE_PACKAGES_DST}/pi-essentials"])

    def test_only_directories_with_a_package_json_and_in_name_order(self):
        self.package("zeta")
        self.package("alpha")
        self.write("config/pi/profile/packages/notes/README.md", "not a package\n")
        self.write("config/pi/profile/packages/loose.ts", "export default function () {}\n")
        s = self.start()
        self.assertEqual(self.settings_json(s)["packages"],
                         [f"{pi.PROFILE_PACKAGES_DST}/alpha", f"{pi.PROFILE_PACKAGES_DST}/zeta"])
        self.assertTrue([w for w in self.warned if "notes" in w and "package.json" in w], self.warned)

    def test_no_profile_mounts_and_lists_none(self):
        self.package("pi-essentials")
        s = self.start(no_profile=True)
        self.assertNotIn(pi.PROFILE_PACKAGES_DST, self.mounts(s))
        self.assertNotIn("packages", self.settings_json(s))

    def test_packages_is_a_profile_item_so_a_change_makes_a_new_profile_hash(self):
        from agentbox import profile
        self.assertIn("packages", profile.ITEMS)
