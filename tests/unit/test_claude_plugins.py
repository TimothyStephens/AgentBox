"""Claude Code plugins: declared in the profile, pinned by `ab profile install claude`, loaded in place.

The facts these rest on came out of a probe in a session on 9 Nov (decisions.md), with throwaway config
dirs under /tmp:

  * `settings.json` alone makes Claude Code register a marketplace (`extraKnownMarketplaces`) and enable
    a plugin (`enabledPlugins`) at startup; a marketplace whose source is a `directory` is read IN PLACE
    (no cache copy), survives the directory being read-only across starts, and the directory's git HEAD
    is recorded as the plugin's `gitCommitSha`;
  * a `github` or `git` source in settings, with or without a `ref`, was silently not registered;
  * everything Claude Code writes about plugins lands in its own config dir, which in a session is the
    per-project state -- so nothing in the tree is written, and no path inside it is recorded.

So the tree is the pin and nothing else, the session mounts it read-only under the config dir, and the
two settings keys are generated from the same `plugins.toml` the install read.
"""
import json
import os
import unittest

from agentbox import installer, plugins, session
from agentbox.adapters import claude
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from .helpers import SettingsCase, read_json

SHA = "464db031e7dd1b9a79803ac8ea45f55c7c20c5bc"
SHA2 = "0123456789abcdef0123456789abcdef01234567"
DECLARED = f'''
[[plugin]]
id = "egm@egm"
marketplace = "egm"
source = "google/elephant-goldfish-model"
ref = "{SHA}"
'''
TWO = DECLARED + f'''
[[plugin]]
id = "other@tools"
marketplace = "tools"
source = "https://example.com/tools.git"
ref = "{SHA2}"
'''
HARNESS = "localhost/agentbox-claude:latest"
IMAGE_DOC = {HARNESS: {"Id": "sha256:claudeharness",
                       "Config": {"Labels": {"agentbox.cli-version": "2.1.318", "agentbox.node-major": "26"}}}}
C_TREE = claude.MARKETPLACES_DST


def fake_tree(settings, *, marketplaces=("egm",), lock=True, cli_version="2.1.318", declared=None):
    """A tree as `ab profile install claude` leaves one: a catalog clone per marketplace, and the lockfile."""
    tree = installer.tree_dir(settings, "claude")
    for m in marketplaces:
        path = os.path.join(tree, m, installer.CLAUDE_MANIFEST)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"name": m, "plugins": [{"name": m, "source": "./"}]}, f)
    if lock:
        entries = plugins.parse(declared if declared is not None else DECLARED)
        doc = {"version": installer.LOCK_VERSION, "agent": "claude",
               "image": {"ref": HARNESS, "cli_version": cli_version, "node_major": "26"},
               "declared": [installer._declared(e) for e in entries],
               "state": {"marketplace": [{"name": m, "commit": SHA, "plugins": [m]} for m in marketplaces]}}
        os.makedirs(os.path.join(tree, installer.OURS), exist_ok=True)
        with open(installer.lock_path(settings, "claude"), "w") as f:
            json.dump(doc, f)
    return tree


class TheDeclaration(unittest.TestCase):
    """A Claude profile's plugins.toml takes `[[plugin]]` only."""

    def test_claude_takes_marketplace_plugins_only(self):
        self.assertEqual(plugins.sections_for("claude"), ("plugin",))
        self.assertEqual(plugins.sections_for("omp"), plugins.SECTIONS)
        self.assertEqual(plugins.sections_for("someone-else"), plugins.SECTIONS,
                         "an unknown agent is not narrowed: the rule can only take sections away")

    def test_an_omp_only_section_is_refused_by_name_not_installed_as_nothing(self):
        text = DECLARED + '\n[[npm]]\nname = "pi-lens"\nversion = "4.4.9"\n'
        with self.assertRaises(AgentboxError) as cm:
            plugins.parse(text, plugins.sections_for("claude"))
        self.assertIn("[[npm]]", str(cm.exception))
        self.assertIn("cannot be used by this agent", str(cm.exception))
        self.assertEqual(len(plugins.parse(text)), 2, "and OMP still takes both")

    def test_the_profiles_own_entry(self):
        [e] = plugins.parse(DECLARED, plugins.sections_for("claude"))
        self.assertEqual((e.kind, e.id, e.marketplace, e.ref), ("plugin", "egm@egm", "egm", SHA))

    def test_the_old_claude_profile_spelling_is_refused_with_the_allowed_keys(self):
        """What `./build.sh install` hit on the host (9 Nov): `name` and a shorthand in `marketplace`."""
        old = f'[[plugin]]\nname = "egm@egm"\nmarketplace = "google/elephant-goldfish-model"\nref = "{SHA}"\n'
        with self.assertRaises(AgentboxError) as cm:
            plugins.parse(old, plugins.sections_for("claude"))
        self.assertIn("unknown key(s) name", str(cm.exception))


class TheScript(unittest.TestCase):
    def script(self, text=DECLARED):
        return installer.script_for("claude", plugins.parse(text, plugins.sections_for("claude")))

    def test_each_catalog_is_cloned_into_the_tree_at_its_sha_and_verified(self):
        lines = self.script().splitlines()
        self.assertEqual(lines[0], "set -eu")
        self.assertEqual(lines[1], f"rm -rf {C_TREE}/egm")
        self.assertEqual(lines[2], f"git clone --quiet https://github.com/google/elephant-goldfish-model.git {C_TREE}/egm")
        self.assertEqual(lines[3], f"git -C {C_TREE}/egm checkout --quiet {SHA}")
        self.assertEqual(lines[4], f"git -C {C_TREE}/egm rev-parse HEAD | grep -Fxq {SHA}")

    def test_the_catalog_is_checked_to_be_the_one_named_and_to_list_the_plugin(self):
        """A clone that is a marketplace under another name, or one without the plugin, fails the install
        rather than failing Claude Code's startup inside a sandbox."""
        text = self.script()
        check = [l for l in text.splitlines() if l.startswith("python3 -I -c") and installer.CLAUDE_MANIFEST in l]
        self.assertEqual(len(check), 1, text)
        self.assertTrue(check[0].endswith(f"{C_TREE}/egm/{installer.CLAUDE_MANIFEST} egm egm"), check[0])
        self.assertLess(text.index("rev-parse HEAD"), text.index(installer.CLAUDE_MANIFEST))

    def test_no_agent_command_runs(self):
        """The directory is loaded in place by the session's generated settings; there is nothing for
        `claude plugin install` to do here, and running it would write into a config dir no session has."""
        self.assertNotIn("claude ", self.script())

    def test_state_is_written_from_the_clones_at_the_end(self):
        """The lockfile's `state` is read from the tree (each clone's HEAD and manifest), not copied from
        the declaration, so it can only agree with the declaration if the install did what it said."""
        text = self.script()
        self.assertTrue(text.rstrip("\n").endswith(f"{C_TREE}/{installer.STATE} egm={C_TREE}/egm"), text)
        self.assertLess(text.index("rev-parse HEAD"), text.index(installer.STATE))
        self.assertIn("rev-parse", text[text.index("python3 -I -c '"):], "the state reads each clone's HEAD")

    def test_two_plugins_from_one_catalog_share_one_clone(self):
        same = DECLARED + f'\n[[plugin]]\nid = "second@egm"\nmarketplace = "egm"\n' \
                          f'source = "google/elephant-goldfish-model"\nref = "{SHA}"\n'
        text = self.script(same)
        self.assertEqual(text.count("git clone"), 1)
        self.assertEqual(text.count(installer.CLAUDE_MANIFEST + " egm "), 2, text)

    def test_one_catalog_at_two_refs_is_refused(self):
        drift = DECLARED + f'\n[[plugin]]\nid = "second@egm"\nmarketplace = "egm"\n' \
                           f'source = "google/elephant-goldfish-model"\nref = "{SHA2}"\n'
        with self.assertRaises(AgentboxError) as cm:
            self.script(drift)
        self.assertIn("two different refs", str(cm.exception))

    def test_two_catalogs_are_two_clones_in_declaration_order(self):
        text = self.script(TWO)
        self.assertLess(text.index(f"{C_TREE}/egm"), text.index(f"{C_TREE}/tools"))
        self.assertIn("git clone --quiet https://example.com/tools.git", text)
        self.assertIn(f"egm={C_TREE}/egm tools={C_TREE}/tools", text)

    def test_an_unknown_agent_has_no_installer(self):
        with self.assertRaises(AgentboxError):
            installer.script_for("pi", [])


class TheContainer(SettingsCase):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(IMAGE_DOC)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        self.job = installer.Install(self.settings, "claude", podman=Podman(self.settings.podman))

    def args(self):
        return self.job.container_args("set -eu\ntrue\n")

    def test_the_tree_is_mounted_where_a_session_mounts_it(self):
        mounts = [a for a in self.args() if a.startswith("--mount=")]
        self.assertEqual(len(mounts), 1, mounts)
        self.assertIn(f"dst={claude.MARKETPLACES_DST}", mounts[0])
        self.assertIn(f"src={os.path.realpath(self.job.tree)}", mounts[0])
        self.assertEqual(installer.tree_dst("claude"), claude.MARKETPLACES_DST,
                         "one definition, used by the installer and the adapter")

    def test_the_tree_lives_beside_omps(self):
        self.assertEqual(self.job.tree, os.path.join(self.settings.seed_dir("claude"), "plugins"))

    def test_no_omp_home_tmpfs_and_the_claude_image(self):
        argv = self.args()
        self.assertFalse([a for a in argv if a.startswith(f"--tmpfs={installer.C_OMP}:")], argv)
        self.assertIn(HARNESS, argv)
        self.assertNotIn("podman.sock", " ".join(argv))


class TheSession(SettingsCase):
    """What a Claude session does with the tree: mount it read-only, declare it in settings.json."""

    def setUp(self):
        super().setUp()
        self.token()
        self.write("config/claude/profile/skills/deploy/SKILL.md", "run make deploy\n")
        self.write("config/claude/profile/settings.json", '{"theme": "dark"}\n')
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(IMAGE_DOC)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)

    def declare(self, text=DECLARED):
        self.write("config/claude/profile/plugins.toml", text)

    def start(self, name="p1", *, no_profile=False, preset="script", **extra):
        self.project(name, preset=preset, **extra)
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, name),
                                 warn=warnings.append, no_profile=no_profile)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s, warnings

    def mounts(self, s):
        return {m.split("dst=")[1].split(",")[0]: m for m in s.run_args(["true"]) if "--mount=" in m}

    def settings_json(self, s):
        return read_json(os.path.join(s.state_dir, "settings.json"))

    def test_the_tree_is_mounted_read_only_under_the_config_dir(self):
        self.declare()
        tree = fake_tree(self.settings)
        s, warnings = self.start()
        m = self.mounts(s).get(C_TREE)
        self.assertTrue(m, self.mounts(s))
        self.assertIn(f"src={tree}", m)
        self.assertIn(",ro", m)
        self.assertFalse([w for w in warnings if "plugin" in w], warnings)

    def test_settings_json_declares_the_marketplace_and_enables_the_plugin(self):
        """The two keys the probe found do the work, generated from plugins.toml rather than committed to
        the profile: container paths stay out of the profile repo, and `--no-profile` drops them for free."""
        self.declare()
        fake_tree(self.settings)
        s, _w = self.start()
        doc = self.settings_json(s)
        self.assertEqual(doc["extraKnownMarketplaces"],
                         {"egm": {"source": {"source": "directory", "path": f"{C_TREE}/egm"}}})
        self.assertEqual(doc["enabledPlugins"], {"egm@egm": True})
        self.assertEqual(doc["theme"], "dark", "merged into the profile's copy, not written over it")

    def test_the_profiles_own_marketplace_declarations_are_kept(self):
        self.write("config/claude/profile/settings.json",
                   '{"extraKnownMarketplaces": {"mine": {"source": {"source": "directory", "path": "/x"}}},'
                   ' "enabledPlugins": {"p@mine": true}}\n')
        self.declare()
        fake_tree(self.settings)
        s, _w = self.start()
        doc = self.settings_json(s)
        self.assertEqual(sorted(doc["extraKnownMarketplaces"]), ["egm", "mine"])
        self.assertEqual(doc["enabledPlugins"], {"egm@egm": True, "p@mine": True})

    def test_the_mount_point_is_made_by_the_launcher_in_the_state_dir(self):
        self.declare()
        fake_tree(self.settings)
        s, _w = self.start()
        self.assertTrue(os.path.isdir(os.path.join(s.state_dir, claude.MARKETPLACES_NAME)))

    def test_no_tree_is_a_warning_naming_the_command_and_nothing_is_declared(self):
        self.declare()
        s, warnings = self.start()
        self.assertNotIn(C_TREE, self.mounts(s))
        self.assertNotIn("enabledPlugins", self.settings_json(s))
        self.assertTrue([w for w in warnings if "ab profile install claude" in w and "egm@egm" in w], warnings)

    def test_nothing_declared_means_nothing_mounted_and_nothing_said(self):
        fake_tree(self.settings)
        s, warnings = self.start()
        self.assertNotIn(C_TREE, self.mounts(s))
        self.assertNotIn("enabledPlugins", self.settings_json(s))
        self.assertFalse([w for w in warnings if "plugin" in w], warnings)

    def test_a_declared_plugin_the_tree_lacks_is_not_enabled_and_is_named(self):
        """Enabling a plugin whose directory is not there is a startup error inside the sandbox, to
        nobody. The one that IS there still loads."""
        self.declare(TWO)
        fake_tree(self.settings, marketplaces=("egm",), declared=TWO)
        s, warnings = self.start()
        doc = self.settings_json(s)
        self.assertEqual(doc["enabledPlugins"], {"egm@egm": True})
        self.assertEqual(sorted(doc["extraKnownMarketplaces"]), ["egm"])
        self.assertTrue([w for w in warnings if "other@tools" in w and "ab profile install claude" in w], warnings)

    def test_a_broken_plugins_toml_warns_and_the_session_starts(self):
        self.declare("[[plugin]]\nname = 'nope'\n")
        fake_tree(self.settings)
        s, warnings = self.start()
        self.assertNotIn(C_TREE, self.mounts(s))
        self.assertTrue([w for w in warnings if "plugins.toml" in w], warnings)

    def test_an_edited_plugins_toml_is_reported_as_stale(self):
        self.declare(TWO)
        fake_tree(self.settings, marketplaces=("egm", "tools"))   # lock says DECLARED, file says TWO
        _s, warnings = self.start()
        self.assertTrue([w for w in warnings if "plugins.toml has changed" in w], warnings)

    def test_a_tree_built_against_another_agent_version_is_reported(self):
        self.declare()
        fake_tree(self.settings, cli_version="2.0.0")
        _s, warnings = self.start()
        self.assertTrue([w for w in warnings if "2.0.0" in w and "2.1.318" in w], warnings)

    def test_no_profile_mounts_nothing_and_declares_nothing(self):
        self.declare()
        fake_tree(self.settings)
        s, warnings = self.start(no_profile=True)
        self.assertNotIn(C_TREE, self.mounts(s))
        doc = self.settings_json(s)
        self.assertNotIn("enabledPlugins", doc)
        self.assertNotIn("extraKnownMarketplaces", doc)
        self.assertFalse([w for w in warnings if "ab profile install" in w], warnings)

    def test_lockdown_loads_no_plugins_as_omp_does(self):
        """decisions.md, 15 Oct: lockdown is "nothing runs unasked", and a plugin can carry hooks and MCP
        servers. The profile's skills still load; the plugins do not."""
        self.declare()
        fake_tree(self.settings)
        s, _w = self.start(preset="lockdown")
        self.assertIn("/run/agentbox/claude/skills", self.mounts(s))
        self.assertNotIn(C_TREE, self.mounts(s))
        self.assertNotIn("enabledPlugins", self.settings_json(s))

    def test_a_symlinked_tree_is_refused(self):
        self.declare()
        real = self.mkdir("elsewhere")
        os.symlink(real, installer.tree_dir(self.settings, "claude"))
        with self.assertRaises(AgentboxError) as cm:
            self.start()
        self.assertIn("symlink", str(cm.exception))

    def test_the_mount_is_not_on_the_config_dir_itself(self):
        """`_check_setup` lets an adapter mount under the agent's config dir now; a mount ON it would hide
        the state. The tree's path is strictly inside."""
        self.assertTrue(C_TREE.startswith(claude.ADAPTER.state_mount + "/"))
        self.assertNotEqual(C_TREE, claude.ADAPTER.state_mount)
