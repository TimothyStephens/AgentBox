"""`plugins.toml`: the file the profile uses to declare what `ab profile install` installs.

**The schema under test is the profile repo's, not one agentbox invented.** `agentbox-profile-omp` already
declares OMP marketplace plugins, pinned by commit sha, with OMP's own naming rules -- and says, in the
file, that the other two arrival paths (an npm package declaring `omp.extensions`, a local checkout linked
with `omp plugin link`) "need a second entry kind first", which is an `ab` change. These tests pin all
three kinds and the rules each carries.

Mostly refusals, and each one has a reason worth stating:

  * **`ref` is a 40-character sha, never a branch or tag.** The profile's own rule, with the profile's own
    reason: marketplace catalogs are mutable, so an unpinned install is not reproducible. Applied to
    `[[repo]]` as well, because the reason does not stop at marketplaces.
  * a `ref` starting with `-` would be read by git as an option; a `..` leaves the tree; a credential in a
    URL is a credential in a sandbox.
  * **an unknown key is an error, not an ignored line** -- a dropped `verison` leaves an entry with no
    version at all, which is the exact failure the file exists to prevent.
  * `id = "<name>@<marketplace>"` must agree with `marketplace`: one name said twice, and a mismatch
    installs something other than what is declared.

`docs/examples/plugins.toml` is parsed here too -- a template that does not parse is worse than none.
"""
import pathlib
import unittest

from agentbox import plugins
from agentbox.errors import AgentboxError

HERE = pathlib.Path(__file__).resolve().parent
EXAMPLE = HERE.parent.parent / "docs" / "examples" / "plugins.toml"

SHA = "464db031e7dd1b9a79803ac8ea45f55c7c20c5bc"
OTHER_SHA = "1" * 40
PLUGIN = (f'[[plugin]]\nid = "egm@egm"\nmarketplace = "egm"\n'
          f'source = "google/elephant-goldfish-model"\nref = "{SHA}"\n')
NPM = '[[npm]]\nname = "pi-lens"\nversion = "3.8.73"\n'
REPO = (f'[[repo]]\nname = "pi-session-stats"\n'
        f'url = "https://github.com/TimothyStephens/pi-session-stats.git"\nref = "{SHA}"\n')


def refuses(case, text, *expect):
    with case.assertRaises(AgentboxError) as cm:
        plugins.parse(text)
    msg = str(cm.exception)
    case.assertIn("plugins.toml", msg)
    for want in expect:
        case.assertIn(want, msg)
    return msg


class Accepts(unittest.TestCase):
    def test_an_empty_file_is_no_plugins_not_an_error(self):
        self.assertEqual(plugins.parse(""), [])
        self.assertEqual(plugins.parse("# nothing declared yet\n"), [])

    def test_the_profiles_own_entry(self):
        """Tim's actual plugins.toml, verbatim: the EGM marketplace plugin."""
        e, = plugins.parse(PLUGIN)
        self.assertEqual((e.kind, e.id, e.marketplace, e.ref), ("plugin", "egm@egm", "egm", SHA))
        self.assertEqual(e.source, "google/elephant-goldfish-model")

    def test_a_marketplace_source_may_be_a_catalog_url(self):
        e, = plugins.parse(PLUGIN.replace('"google/elephant-goldfish-model"',
                                          '"https://example.com/catalog.json"'))
        self.assertEqual(e.source, "https://example.com/catalog.json")

    def test_an_npm_entry(self):
        e, = plugins.parse(NPM)
        self.assertEqual((e.kind, e.name, e.version), ("npm", "pi-lens", "3.8.73"))

    def test_a_scoped_npm_entry(self):
        e, = plugins.parse('[[npm]]\nname = "@narumitw/pi-statusline"\nversion = "0.38.0"\n')
        self.assertEqual(e.name, "@narumitw/pi-statusline")

    def test_a_repo_entry_defaults_to_the_repo_root(self):
        e, = plugins.parse(REPO)
        self.assertEqual((e.kind, e.name, e.ref, e.extension_dir), ("repo", "pi-session-stats", SHA, "."))

    def test_a_repo_entry_with_an_extension_subdirectory(self):
        e, = plugins.parse(REPO + 'extension_dir = "mags-codedev-extension"\n')
        self.assertEqual(e.extension_dir, "mags-codedev-extension")

    def test_all_three_kinds_together_in_a_fixed_order(self):
        got = plugins.parse(NPM + REPO + PLUGIN)
        self.assertEqual([e.kind for e in got], ["plugin", "npm", "repo"],
                         "marketplace plugins first, so the install order does not depend on file order")

    def test_npm_args_are_name_at_version(self):
        got = plugins.parse(NPM + PLUGIN + '[[npm]]\nname = "pi-rules"\nversion = "0.1.1"\n')
        self.assertEqual(plugins.npm_args(got), ["pi-lens@3.8.73", "pi-rules@0.1.1"])

    def test_summary_counts_what_was_declared(self):
        self.assertEqual(plugins.summary(plugins.parse("")), "nothing declared")
        self.assertEqual(plugins.summary(plugins.parse(PLUGIN)), "1 plugin")
        self.assertEqual(plugins.summary(plugins.parse(PLUGIN + NPM + REPO)), "1 plugin, 1 npm, 1 repo")

    def test_the_example_in_docs_parses_and_is_the_list_tim_asked_for(self):
        """The worked example is the 5 Nov list: EGM, four npm packages and three checkouts, every ref a
        real sha (`git ls-remote`). Pinned here because a template that does not parse is worse than none,
        and because "which entries" is a decision -- pi-codegraph and pi-nvim-bridge are deliberately out,
        and a silent re-appearance should fail a test rather than a session."""
        entries = plugins.load(EXAMPLE)
        self.assertEqual(plugins.summary(entries), "1 plugin, 4 npms, 3 repos")
        self.assertEqual([e.label for e in entries],
                         ["egm@egm", "@narumitw/pi-statusline", "pi-context-prune", "pi-lens", "pi-rules",
                          "mags-codedev", "pi-session-stats", "pi-content-audit"])
        mags, = [e for e in entries if e.label == "mags-codedev"]
        self.assertEqual(mags.extension_dir, "mags-codedev-extension")
        for e in entries:
            if e.kind != "npm":
                with self.subTest(entry=e.label):
                    self.assertNotEqual(e.ref, "0" * 40, "a placeholder sha, not a resolved one")


class RefusesMarketplacePlugins(unittest.TestCase):
    def test_a_ref_that_is_not_a_pinned_commit(self):
        """The profile's rule, in the profile's words: a catalog is mutable, so a branch or tag is not a
        pin. An abbreviated sha is refused too -- it is ambiguous by design."""
        for bad in ("main", "v1.0", "HEAD", SHA[:12], SHA.upper(), SHA + "0", ""):
            with self.subTest(ref=bad):
                refuses(self, PLUGIN.replace(f'"{SHA}"', f'"{bad}"'))

    def test_an_id_that_does_not_agree_with_the_marketplace(self):
        refuses(self, PLUGIN.replace('marketplace = "egm"', 'marketplace = "other"'), "said twice")

    def test_an_id_with_no_marketplace_part(self):
        refuses(self, PLUGIN.replace('id = "egm@egm"', 'id = "egm"'), "<name>@<marketplace>")

    def test_a_name_that_breaks_omps_own_rule(self):
        """ASCII letters, digits, hyphens and dots, starting and ending with a letter or digit."""
        for bad in ("-egm", "egm-", ".egm", "eg m", "eg_m", "eg/m", "a" * 65):
            with self.subTest(name=bad):
                refuses(self, PLUGIN.replace('id = "egm@egm"', f'id = "{bad}@egm"'))

    def test_a_marketplace_name_that_breaks_the_rule(self):
        refuses(self, PLUGIN.replace('id = "egm@egm"\nmarketplace = "egm"',
                                     'id = "egm@-egm"\nmarketplace = "-egm"'))

    def test_a_source_that_is_not_shorthand_or_https(self):
        for bad in ("/local/path", "git@github.com:a/b.git", "git://github.com/a/b",
                    "http://github.com/a/b", "file:///etc", "../x"):
            with self.subTest(source=bad):
                refuses(self, PLUGIN.replace('"google/elephant-goldfish-model"', f'"{bad}"'))

    def test_a_missing_key(self):
        for key in ("id", "marketplace", "source", "ref"):
            with self.subTest(missing=key):
                lines = [l for l in PLUGIN.splitlines() if not l.startswith(f"{key} =")]
                refuses(self, "\n".join(lines) + "\n", "missing", key)

    def test_a_typo_in_a_key_is_an_error_not_an_ignored_line(self):
        refuses(self, PLUGIN.replace("marketplace =", "markteplace ="), "unknown key")


class RefusesNpm(unittest.TestCase):
    def test_a_version_range_or_tag(self):
        for bad in ("^3.8.73", "~3.8.73", ">=3.8", "latest", "*", "3.8", "3.x", ""):
            with self.subTest(version=bad):
                refuses(self, NPM.replace('"3.8.73"', f'"{bad}"'))

    def test_a_name_that_is_a_path_or_a_flag(self):
        for bad in ("../pi-lens", "pi lens", "-pi-lens", "/etc/passwd", "@/x", "pi;rm -rf /", "PI-Lens"):
            with self.subTest(name=bad):
                refuses(self, NPM.replace('"pi-lens"', f'"{bad}"'))

    def test_a_missing_version(self):
        refuses(self, '[[npm]]\nname = "pi-lens"\n', "missing", "version")


class RefusesRepos(unittest.TestCase):
    def test_a_transport_that_is_not_https(self):
        for bad in ("git://github.com/x/y.git", "http://github.com/x/y.git", "ssh://git@github.com/x/y",
                    "git@github.com:x/y.git", "file:///etc", "/etc/passwd"):
            with self.subTest(url=bad):
                refuses(self, REPO.replace("https://github.com/TimothyStephens/pi-session-stats.git", bad))

    def test_credentials_in_the_url(self):
        """A credential never enters a sandbox -- and this one would also sit in the profile repo."""
        refuses(self, REPO.replace("https://github.com/", "https://tim:token@github.com/"), "credentials")

    def test_a_ref_that_is_not_a_pinned_commit(self):
        for bad in ("main", "--upload-pack=sh", "-x", "", "main;true", "../main", SHA[:7]):
            with self.subTest(ref=bad):
                refuses(self, REPO.replace(f'"{SHA}"', f'"{bad}"'))

    def test_a_name_that_leaves_the_tree(self):
        for bad in ("..", ".", "../x", "a/b", "/abs", ".hidden", ""):
            with self.subTest(name=bad):
                refuses(self, REPO.replace('"pi-session-stats"', f'"{bad}"'))

    def test_the_npm_directory_name_is_reserved(self):
        """The npm entries install into `node_modules/` of the same tree."""
        refuses(self, REPO.replace('"pi-session-stats"', '"node_modules"'), "node_modules")

    def test_an_extension_dir_that_leaves_the_repo(self):
        for bad in ("../x", "/etc", "a/../..", "a/..", "a//b", ""):
            with self.subTest(extension_dir=bad):
                refuses(self, REPO + f'extension_dir = "{bad}"\n')

    def test_a_name_listed_twice(self):
        refuses(self, REPO + REPO.replace(f'"{SHA}"', f'"{OTHER_SHA}"'), "twice")


class RefusesTheFileItself(unittest.TestCase):
    def test_not_toml(self):
        refuses(self, "this is not toml\n", "not valid TOML")

    def test_an_unknown_section(self):
        """`[[pylib]]` is refused rather than ignored: installing nothing when the file asks for something
        is a failure you would only find inside a session."""
        refuses(self, '[[pylib]]\nname = "numpy"\n', "unknown section")

    def test_a_single_table_instead_of_a_list(self):
        refuses(self, '[npm]\nname = "pi-lens"\nversion = "1.0.0"\n', "[[npm]]")


class Loading(unittest.TestCase):
    def test_a_missing_file_is_no_plugins(self):
        self.assertEqual(plugins.load("/nonexistent/plugins.toml"), [])

    def test_a_symlink_is_refused(self):
        import os
        import shutil
        import tempfile
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(shutil.rmtree, d, True)
        real = os.path.join(d, "real.toml")
        with open(real, "w") as f:
            f.write(NPM)
        link = os.path.join(d, "plugins.toml")
        os.symlink(real, link)
        with self.assertRaises(AgentboxError) as cm:
            plugins.load(link)
        self.assertIn("symlink", str(cm.exception))
