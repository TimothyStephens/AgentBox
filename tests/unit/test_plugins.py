"""`plugins.toml`: the file that says what `ab profile install` installs.

Every string in it becomes argv inside a container that has network (`npm install <name>@<version>`,
`git clone <url>`, `git checkout <ref>`) and several become directory names in a tree that every OMP
session mounts read-only. So these tests are mostly refusals, and each refusal has a reason: a `ref`
starting with `-` is a git option, a `..` in a path leaves the tree, a version range is "whatever npm
published this morning".

The one accepted looseness is deliberate and is tested as such: **a git entry may name a branch.** Four of
Tim's five repos are his own, tracked on `main`; refusing a branch would make the tool useless for them.
The reproducibility lives in the lockfile the installer writes, not in this file.

`docs/examples/plugins.toml` is omp-env's manifest.json converted, and it is parsed here -- a template that
does not parse is worse than none.
"""
import pathlib
import unittest

from agentbox import plugins
from agentbox.errors import AgentboxError

HERE = pathlib.Path(__file__).resolve().parent
EXAMPLE = HERE.parent.parent / "docs" / "examples" / "plugins.toml"

NPM = '[[npm]]\nname = "pi-lens"\nversion = "3.8.73"\n'
GIT = ('[[git]]\nname = "pi-session-stats"\n'
       'url = "https://github.com/TimothyStephens/pi-session-stats.git"\nref = "main"\n')


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
        self.assertEqual(plugins.parse("# nothing yet\n"), [])

    def test_an_npm_entry(self):
        e, = plugins.parse(NPM)
        self.assertEqual((e.kind, e.name, e.version), ("npm", "pi-lens", "3.8.73"))

    def test_a_scoped_npm_entry(self):
        e, = plugins.parse('[[npm]]\nname = "@isac322/pi-codegraph"\nversion = "0.3.1"\n')
        self.assertEqual(e.name, "@isac322/pi-codegraph")

    def test_a_git_entry_defaults_to_the_repo_root(self):
        e, = plugins.parse(GIT)
        self.assertEqual((e.kind, e.name, e.ref, e.extension_dir), ("git", "pi-session-stats", "main", "."))

    def test_a_git_entry_may_name_a_branch(self):
        """The deliberate looseness. The lockfile records the commit; this file records the intent."""
        for ref in ("main", "release/1.2", "v0.3.1", "a" * 40):
            with self.subTest(ref=ref):
                e, = plugins.parse(GIT.replace('ref = "main"', f'ref = "{ref}"'))
                self.assertEqual(e.ref, ref)

    def test_an_extension_subdirectory(self):
        e, = plugins.parse(GIT + 'extension_dir = "mags-codedev-extension"\n')
        self.assertEqual(e.extension_dir, "mags-codedev-extension")

    def test_file_order_is_kept(self):
        got = plugins.parse(NPM + GIT)
        self.assertEqual([e.name for e in got], ["pi-lens", "pi-session-stats"])

    def test_npm_args_are_name_at_version(self):
        got = plugins.parse(NPM + GIT + '[[npm]]\nname = "pi-rules"\nversion = "0.1.1"\n')
        self.assertEqual(plugins.npm_args(got), ["pi-lens@3.8.73", "pi-rules@0.1.1"])

    def test_the_example_in_docs_parses(self):
        entries = plugins.load(EXAMPLE)
        self.assertEqual(len([e for e in entries if e.kind == "npm"]), 5)
        self.assertEqual(len([e for e in entries if e.kind == "git"]), 4)
        mags, = [e for e in entries if e.name == "mags-codedev"]
        self.assertEqual(mags.extension_dir, "mags-codedev-extension")


class RefusesNpm(unittest.TestCase):
    def test_a_version_range_or_tag(self):
        """`^1.2.3` and `latest` resolve to different code on different days. The manifest has the exact
        number, so there is nothing to gain by accepting less."""
        for bad in ("^3.8.73", "~3.8.73", ">=3.8", "latest", "*", "3.8", "3.x", ""):
            with self.subTest(version=bad):
                refuses(self, NPM.replace('"3.8.73"', f'"{bad}"'))

    def test_a_name_that_is_a_path_or_a_flag(self):
        for bad in ("../pi-lens", "pi lens", "-pi-lens", "/etc/passwd", "@/x", "pi;rm -rf /", "PI-Lens"):
            with self.subTest(name=bad):
                refuses(self, NPM.replace('"pi-lens"', f'"{bad}"'))

    def test_a_missing_version(self):
        refuses(self, '[[npm]]\nname = "pi-lens"\n', "version")

    def test_a_typo_in_a_key_is_an_error_not_an_ignored_line(self):
        """The one that matters most: a dropped `verison` leaves an entry with no version at all."""
        refuses(self, '[[npm]]\nname = "pi-lens"\nverison = "3.8.73"\n', "unknown key")


class RefusesGit(unittest.TestCase):
    def test_a_transport_that_is_not_https(self):
        for bad in ("git://github.com/x/y.git", "http://github.com/x/y.git", "ssh://git@github.com/x/y",
                    "git@github.com:x/y.git", "file:///etc", "/etc/passwd"):
            with self.subTest(url=bad):
                refuses(self, GIT.replace("https://github.com/TimothyStephens/pi-session-stats.git", bad))

    def test_credentials_in_the_url(self):
        """A credential never enters a sandbox -- and this one would also be in a file in the profile."""
        refuses(self, GIT.replace("https://github.com/", "https://tim:token@github.com/"), "credentials")

    def test_a_ref_that_would_be_read_as_a_git_option(self):
        for bad in ("--upload-pack=sh", "-x", "", "main;true", "main ref", "../main", "ma..in"):
            with self.subTest(ref=bad):
                refuses(self, GIT.replace('ref = "main"', f'ref = "{bad}"'))

    def test_a_name_that_leaves_the_tree(self):
        for bad in ("..", ".", "../x", "a/b", "/abs", ".hidden", ""):
            with self.subTest(name=bad):
                refuses(self, GIT.replace('"pi-session-stats"', f'"{bad}"'))

    def test_an_extension_dir_that_leaves_the_repo(self):
        for bad in ("../x", "/etc", "a/../..", "a/..", "a//b", ""):
            with self.subTest(extension_dir=bad):
                refuses(self, GIT + f'extension_dir = "{bad}"\n')

    def test_the_npm_directory_name_is_reserved(self):
        """The npm entries install into `node_modules/` of the same tree."""
        refuses(self, GIT.replace('"pi-session-stats"', '"node_modules"'), "node_modules")

    def test_a_name_listed_twice(self):
        refuses(self, GIT + GIT, "twice")

    def test_a_missing_url_or_ref(self):
        refuses(self, '[[git]]\nname = "x"\nref = "main"\n', "missing", "url")
        refuses(self, '[[git]]\nname = "x"\nurl = "https://github.com/a/b.git"\n', "missing", "ref")


class RefusesTheFileItself(unittest.TestCase):
    def test_not_toml(self):
        refuses(self, "this is not toml\n", "not valid TOML")

    def test_an_unknown_section(self):
        """A `[[pylib]]` section is refused rather than ignored: silently installing nothing when the file
        asks for something is how you find out in a session."""
        refuses(self, '[[pylib]]\nname = "numpy"\n', "unknown section")

    def test_a_single_table_instead_of_a_list(self):
        refuses(self, '[npm]\nname = "pi-lens"\nversion = "1.0.0"\n', "[[npm]]")


class Loading(unittest.TestCase):
    def test_a_missing_file_is_no_plugins(self):
        self.assertEqual(plugins.load("/nonexistent/plugins.toml"), [])

    def test_a_symlink_is_refused(self):
        import os
        import tempfile
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        real = os.path.join(d, "real.toml")
        with open(real, "w") as f:
            f.write(NPM)
        link = os.path.join(d, "plugins.toml")
        os.symlink(real, link)
        with self.assertRaises(AgentboxError) as cm:
            plugins.load(link)
        self.assertIn("symlink", str(cm.exception))
