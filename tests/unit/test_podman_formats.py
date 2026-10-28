"""What podman 5.7 actually accepts for `--format` (host, 28 Oct).

`sessions()` ran

    podman network ls --format '{{.Name}} {{index .Labels "agentbox.session"}}'

which podman 5.7 refuses: "error calling index: cannot index slice/array with type string". The call passed
`check=False`, so it failed silently and network-based session detection had NEVER worked -- and the
fail-open sweep turned that silent failure into a raise, which broke `ab session ls`/`kill`/`gc`/`preview`
on the host. The unit tests could not catch it because the fake podman answered any `--format` with the
same canned string.

So: networks are listed as `--format json` and the labels are read in Python, the fixture beside this file
is real `podman network ls --format json` output from the host, and the fake podman now REFUSES the template
real podman refuses. The last test keeps `tests/manual/podman_formats.sh` -- which runs every `--format` the
code issues against real podman -- in step with the code, since that script is the only thing that can
check this class for real.
"""
import ast
import json
import os
import pathlib
import unittest

from agentbox import session
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from .helpers import SettingsCase

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
# tests/unit/fixtures/podman_network_ls.json is `podman network ls --format json` from ubuntu-dev, podman
# 5.7, verbatim -- trimmed by Tim to agentbox's own networks and podman's default one, nothing edited and
# nothing added (29 Oct). It holds two sessions' networks (`-egr` and `-int` each) and the label-less
# `podman` network.
#
# Keeping it real earned its keep immediately: a network with no labels has NO `labels` KEY AT ALL, where
# the hand-made entry it replaced had `"labels": {}`. The parser has to read a missing key, which the
# invented shape never exercised.
NETWORK_LS_JSON = (HERE / "fixtures" / "podman_network_ls.json").read_text()
MANUAL_SCRIPT = REPO / "tests" / "manual" / "podman_formats.sh"

# The template podman 5.7 rejects on `network ls`, and the error it gives.
REJECTED = '{{.Name}} {{index .Labels "agentbox.session"}}'


class NetworkListing(SettingsCase):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_PODMAN_NETJSON"] = NETWORK_LS_JSON
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_NETJSON", None)
        os.environ["FAKE_PODMAN_PSJSON"] = "[]"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_PSJSON", None)
        self.podman = Podman(self.settings.podman)

    def test_the_fake_refuses_the_template_real_podman_refuses(self):
        """Without this the unit suite would accept the broken command again."""
        with self.assertRaises(AgentboxError) as cm:
            self.podman.run(["network", "ls", "--format", REJECTED])
        self.assertIn("cannot index slice/array with type string", str(cm.exception))

    def test_networks_json_reads_the_real_shape(self):
        """Keys in this report are lowercase (`name`, `labels`) -- not `ps --format json`'s capitalised ones."""
        nets = self.podman.networks_json(self.settings.labels)
        self.assertEqual([n["name"] for n in nets[:2]],
                         ["ab-claude-agentbox-1-egr", "ab-claude-agentbox-1-int"])
        self.assertEqual(nets[0]["labels"]["agentbox.session"], "ab-claude-agentbox-1")

    def test_sessions_finds_each_sessions_own_networks(self):
        """The whole point of the listing: `ab session ls`, `kill`, `gc` and `preview` work off this. The
        host output holds two sessions, so it also pins that their networks are not pooled."""
        found = session.sessions(self.settings, self.podman)
        for n in ("1", "2"):
            self.assertEqual(sorted(found[f"ab-claude-agentbox-{n}"]["networks"]),
                             [f"ab-claude-agentbox-{n}-egr", f"ab-claude-agentbox-{n}-int"])

    def test_a_network_with_no_labels_key_at_all_is_ignored(self):
        """podman's own default network has no `labels` key whatsoever in the real report -- not an empty
        one. Reading it must not raise, and must not invent a session. (The fake ignores --filter, since the
        fixture is the answer podman gives for the already-filtered query.)"""
        nets = self.podman.networks_json(self.settings.labels)
        plain = [n for n in nets if n["name"] == "podman"]
        self.assertEqual(len(plain), 1)
        self.assertNotIn("labels", plain[0], "the fixture must keep the real shape, or this tests nothing")
        found = session.sessions(self.settings, self.podman)
        self.assertNotIn("podman", found)
        self.assertNotIn("", found)
        self.assertNotIn(None, found)

    def test_a_failing_listing_still_raises(self):
        os.environ["FAKE_PODMAN_FAIL"] = "network"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        with self.assertRaises(AgentboxError):
            session.sessions(self.settings, self.podman)

    def test_unparseable_output_raises(self):
        os.environ["FAKE_PODMAN_NETJSON"] = "{not json"
        with self.assertRaises(AgentboxError) as cm:
            self.podman.networks_json(self.settings.labels)
        self.assertIn("could not be read as JSON", str(cm.exception))


def formats_in_source():
    """Every (argv-so-far, --format value) pair the code issues, read out of the source.

    Each one is a list literal with "--format" followed by the format, e.g.
    `["network", "ls", "--format", "json"]`, so the pairs can be collected exactly rather than guessed at.
    """
    out = []
    for path in sorted((REPO / "agentbox").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.List):
                continue
            for i, el in enumerate(node.elts[:-1]):
                if isinstance(el, ast.Constant) and el.value == "--format":
                    nxt = node.elts[i + 1]
                    if isinstance(nxt, ast.Constant) and isinstance(nxt.value, str):
                        words = [e.value for e in node.elts[:i]
                                 if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                        out.append((path.name, words, nxt.value))
    return out


class TheManualScriptCoversEveryFormat(unittest.TestCase):
    """`tests/manual/podman_formats.sh` is the only check against real podman, so it must not go stale: a
    `--format` added to the code and not to the script would be exactly the gap that hid this bug."""

    def test_every_format_in_the_code_is_in_the_script(self):
        self.assertTrue(MANUAL_SCRIPT.is_file(), f"{MANUAL_SCRIPT} is missing")
        script = MANUAL_SCRIPT.read_text()
        found = formats_in_source()
        self.assertTrue(found, "no --format calls found: the collector is broken")
        for where, words, fmt in found:
            with self.subTest(where=where, words=" ".join(words)):
                self.assertIn(fmt, script, f"{where}: `--format {fmt}` is not checked by podman_formats.sh")

    # Tim runs this script on the host, where live services are, so every podman subcommand in it has to be
    # one that only reads. Checked against the invocations themselves, not the prose around them.
    READ_ONLY = {("--version",), ("ps",), ("images",), ("inspect",), ("image", "inspect"),
                 ("network", "ls"), ("network", "inspect"), ("pod", "ps"), ("volume", "ls")}

    def podman_subcommands(self):
        """Every podman subcommand the script invokes: the words after `"$PODMAN"`, and the argv each
        `check`/`skip` line passes (which `check` forwards to podman as `"$@"`)."""
        import re
        out = []
        for line in MANUAL_SCRIPT.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            words = []
            if '"$PODMAN"' in line:
                words = line.split('"$PODMAN"', 1)[1]
            m = re.match(r'check\s+"[^"]*"\s+(?:\'[^\']*\'|"[^"]*")\s+(.*)$', line)
            if m:
                words = m.group(1)
            if not words:
                continue
            for stop in ("--format", "|", ")", "2>", "&&"):
                words = words.split(stop, 1)[0]
            parts = [w.strip('"\'') for w in words.split() if not w.startswith(('"$', '$', '-f'))]
            if parts:
                out.append(tuple(parts))
        return out

    def test_the_script_only_ever_reads(self):
        self.assertTrue(os.access(MANUAL_SCRIPT, os.X_OK), "not executable")
        found = self.podman_subcommands()
        self.assertTrue(found, "no podman invocations found: the collector is broken")
        for cmd in found:
            with self.subTest(cmd=" ".join(cmd)):
                self.assertTrue(cmd[:2] in self.READ_ONLY or cmd[:1] in self.READ_ONLY,
                                f"`podman {' '.join(cmd)}` is not a read-only command")

    def test_no_label_indexing_on_network_ls(self):
        """The specific shape podman 5.7 rejects, wherever it might come back."""
        for where, words, fmt in formats_in_source():
            if words[:2] == ["network", "ls"]:
                self.assertNotIn(".Labels", fmt, f"{where}: podman 5.7 cannot index .Labels on network ls")


if __name__ == "__main__":
    unittest.main()
