"""Opt-in Playwright (decisions.md, 19 Nov): one agentbox.toml key decides what `make base-image` builds, and the
image's `agentbox.playwright` label -- not the key -- decides what a session is told and what the selftest checks.

What is held here:
  * the key: off by default, a real TOML boolean or a refusal (`"false"` read as truthy would add a few hundred
    MB to every image; read as falsy it would drop what the user asked for);
  * the Makefile's helper prints exactly `1` or `0`, from the same file the launcher reads;
  * the label: a version means present; `none` (an opted-out build) and no label (an image built before the
    option) both mean absent;
  * the selftest: a real headless launch when present, and a SKIP -- never a FAIL -- when absent, saying which
    of the two ways it is absent;
  * the session notes: a Playwright paragraph only when the image has it.
"""
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agentbox import config, imageopts, session
from agentbox.errors import AgentboxError
from agentbox.selftest import runner

from .helpers import SettingsCase


class TheSetting(SettingsCase):
    def write_toml(self, extra):
        with open(os.path.join(self.cfg, "agentbox.toml"), "a") as f:
            f.write(extra)

    def helper(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = imageopts.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_off_by_default(self):
        self.assertIs(config.DEFAULTS["image_playwright"], False)
        self.assertIs(config.load().image_playwright, False)
        self.assertEqual(self.helper("playwright"), (0, "0\n", ""))

    def test_true_in_agentbox_toml_turns_it_on(self):
        self.write_toml("image_playwright = true\n")
        self.assertIs(config.load().image_playwright, True)
        self.assertEqual(self.helper("playwright"), (0, "1\n", ""))

    def test_false_is_off(self):
        self.write_toml("image_playwright = false\n")
        self.assertEqual(self.helper("playwright"), (0, "0\n", ""))

    def test_a_quoted_or_numeric_value_is_refused_not_guessed(self):
        for bad in ('"false"', '"true"', "1", "0"):
            with self.subTest(value=bad):
                with self.assertRaises(AgentboxError) as e:
                    imageopts.flag({"image_playwright": json.loads(bad)}, "image_playwright")
                self.assertIn("TOML boolean", str(e.exception))

    def test_the_helper_exits_2_with_the_reason_on_a_bad_value(self):
        """make's $(shell) then hands the recipe an empty PLAYWRIGHT, which it refuses; the reason is on stderr."""
        self.write_toml('image_playwright = "yes"\n')
        code, out, err = self.helper("playwright")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("image_playwright must be true or false", err)

    def test_the_helper_exits_2_on_a_broken_toml(self):
        self.write_toml("image_playwright = \n")
        code, out, err = self.helper("playwright")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("agentbox.toml", err)

    def test_the_helper_refuses_an_unknown_option(self):
        code, out, err = self.helper("firefox")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage", err)


class TheLabel(unittest.TestCase):
    def test_a_version_means_present(self):
        self.assertEqual(imageopts.playwright_version({"agentbox.playwright": "1.65.2"}), "1.65.2")

    def test_none_no_label_and_no_labels_mean_absent(self):
        for labels in ({"agentbox.playwright": "none"}, {"agentbox.playwright": ""}, {"agentbox.cli-version": "x"},
                       {}, None, {"agentbox.playwright": "<no value>"}):
            with self.subTest(labels=labels):
                self.assertIsNone(imageopts.playwright_version(labels))


class TheSelftestLine(unittest.TestCase):
    LABEL = "Playwright launches headless Chromium"

    def results(self, version, configured, output="__done__ ok\n"):
        b = runner.Builder()
        runner.playwright_cases(b, version, configured)
        return b, runner.score(b.cases, output)

    def test_absent_and_not_asked_for_is_a_skip_saying_so(self):
        b, [r] = self.results(None, configured=False)
        self.assertEqual((r.label, r.verdict), (self.LABEL, "SKIP"))
        self.assertEqual(r.detail, "not installed (image_playwright = false)")
        self.assertEqual(b.script(), "", "nothing is sent to the probe")

    def test_asked_for_but_not_built_is_a_skip_that_says_how_to_get_it(self):
        _b, [r] = self.results(None, configured=True)
        self.assertEqual(r.verdict, "SKIP", "an image not rebuilt yet is not a broken sandbox")
        self.assertIn("make base-image", r.detail)
        self.assertIn("image_playwright = true", r.detail)

    def test_present_is_a_real_launch_in_the_probe(self):
        b, _ = self.results("1.65.2", configured=False)
        [case] = b.cases
        self.assertEqual(case.op, "playwright")
        self.assertIn("1.65.2", case.label)
        self.assertEqual(b.script(), f"{case.id} playwright \n")

    def test_present_passes_on_a_rendered_page_and_fails_otherwise(self):
        b, _ = self.results("1.65.2", configured=True)
        cid = b.cases[0].id
        [ok] = runner.score(b.cases, f"{cid} ok abt-selftest\n__done__ ok\n")
        self.assertEqual(ok.verdict, "PASS")
        [bad] = runner.score(b.cases, f"{cid} error:Error: BrowserType.launch: crashed\n__done__ ok\n")
        self.assertEqual(bad.verdict, "FAIL")
        self.assertIn("crashed", bad.detail)
        [silent] = runner.score(b.cases, "__done__ ok\n")
        self.assertEqual(silent.verdict, "FAIL", "a present Playwright the probe did not answer for is not a pass")

    def test_the_probe_knows_the_op(self):
        with open(runner.PROBE) as f:
            probe = f.read()
        self.assertIn("    playwright) ", probe)
        self.assertIn("p.chromium.launch()", probe)


class TheNotes(unittest.TestCase):
    def project(self):
        from types import SimpleNamespace
        return SimpleNamespace(name="p1", root="/w/p1", observe_logs=False, reviews=None, data_ro=[], data_rw=[],
                               dev_images=[], value=lambda k: "rw")

    def test_present_the_agent_is_told_how_to_use_it(self):
        notes = session.sandbox_notes(self.project(), "packages", playwright="1.65.2")
        self.assertIn("Playwright 1.65.2 (Python) is installed", notes)
        self.assertIn("p.chromium.launch()", notes)
        self.assertIn("do not run `playwright install`", notes)
        self.assertIn("/opt/ms-playwright", notes)

    def test_absent_it_is_not_mentioned(self):
        for v in (None, ""):
            with self.subTest(v=v):
                self.assertNotIn("Playwright", session.sandbox_notes(self.project(), "packages", playwright=v))


class TheSessionReadsTheImage(SettingsCase):
    """End to end through the fake podman: the label on the image the session runs decides the notes."""

    def setUp(self):
        super().setUp()
        self.token()
        self.project("p1", preset="script")

    def notes_for(self, labels):
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(
            {s.adapter.image: {"Id": "sha256:aaa", "Config": {"Labels": labels}}})
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        mounted = {dst: src for src, dst, _ro in s.setup.mounts}
        with open(mounted["/run/agentbox/claude/CLAUDE.md"]) as f:
            return f.read()

    def test_an_image_with_playwright(self):
        self.assertIn("Playwright 1.65.2 (Python) is installed", self.notes_for({"agentbox.playwright": "1.65.2"}))

    def test_an_opted_out_image(self):
        self.assertNotIn("Playwright", self.notes_for({"agentbox.playwright": "none"}))

    def test_an_image_from_before_the_option(self):
        self.assertNotIn("Playwright", self.notes_for({"agentbox.cli-version": "2.1.318"}))

    def test_the_selftest_asks_the_image_too(self):
        """build_cases reads the same label; agentbox.toml only words the SKIP."""
        from agentbox import selftest
        for labels, op in (({"agentbox.playwright": "1.65.2"}, "playwright"), ({"agentbox.playwright": "none"}, "skip")):
            with self.subTest(labels=labels):
                s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                         warn=lambda m: None)
                os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(
                    {s.adapter.image: {"Id": "sha256:aaa", "Config": {"Labels": labels}}})
                self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
                s.prepare(do_backup=False)
                self.addCleanup(s.teardown)
                b = selftest.build_cases(s, self.settings)
                [case] = [c for c in b.cases if c.label.startswith("Playwright")]
                self.assertEqual(case.op, op)


if __name__ == "__main__":
    unittest.main()
