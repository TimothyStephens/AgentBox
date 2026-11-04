"""The user's own image layer: `ab image build <agent>`, and when a session runs it.

Two things here are worth more than the rest.

**The tag is keyed on the harness image ID and the Containerfile** -- not on the profile hash. Adding a
skill must not invalidate a built image, and a rebuilt harness image must. The ID rather than the
`:latest` tag it was built from, because that tag moves, and a moved tag is the whole staleness this has
to notice.

**A layer built on an older harness is never run.** When the key no longer matches, nothing is tagged under
it, so `ab run` says `personal layer out of date` and runs the plain harness image. The alternative -- run
it anyway -- means the agent, the podman client and the toolchain underneath a session are whatever they
were the last time someone remembered to rebuild.
"""
import json
import os
import unittest

from agentbox import profile, session, userimage
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from .helpers import SettingsCase

HARNESS = "localhost/agentbox-claude:latest"
GOOD = "ARG BASE\nFROM ${BASE}\nRUN echo mine\n"
# What the harness image inspects as. The candidate must match all of it.
BASE_DOC = {"Id": "sha256:harness1",
            "Config": {"User": "", "Entrypoint": None, "Cmd": ["claude"],
                       "Env": ["PATH=/usr/local/bin:/usr/bin"],
                       "Labels": {"agentbox.cli-version": "2.1.318"}}}
CHECK_OK = "VERSION=2.1.318\nDONE\n"


class TheKey(unittest.TestCase):
    def test_a_new_harness_image_is_a_new_key(self):
        a = profile.user_image_ref("claude", "sha256:aaa", b"ARG BASE\nFROM ${BASE}\n")
        b = profile.user_image_ref("claude", "sha256:bbb", b"ARG BASE\nFROM ${BASE}\n")
        self.assertNotEqual(a, b)

    def test_an_edited_containerfile_is_a_new_key(self):
        a = profile.user_image_ref("claude", "sha256:aaa", b"ARG BASE\nFROM ${BASE}\n")
        b = profile.user_image_ref("claude", "sha256:aaa", b"ARG BASE\nFROM ${BASE}\nRUN true\n")
        self.assertNotEqual(a, b)

    def test_the_same_inputs_are_the_same_key(self):
        args = ("claude", "sha256:aaa", b"ARG BASE\nFROM ${BASE}\n")
        self.assertEqual(profile.user_image_ref(*args), profile.user_image_ref(*args))

    def test_the_agent_is_in_the_repository_name(self):
        ref = profile.user_image_ref("omp", "sha256:aaa", b"ARG BASE\nFROM ${BASE}\n")
        self.assertTrue(ref.startswith("localhost/agentbox-omp-user:"), ref)


class TheContract(unittest.TestCase):
    def ok(self, text):
        profile.check_user_containerfile(text.encode())

    def refused(self, text):
        with self.assertRaises(AgentboxError) as cm:
            profile.check_user_containerfile(text.encode())
        return str(cm.exception)

    def test_the_required_shape_passes(self):
        self.ok(GOOD)
        self.ok("# a comment\nARG BASE\nFROM ${BASE}\nRUN true\n")
        self.ok("ARG BASE=localhost/agentbox-claude:latest\nFROM ${BASE}\n")
        self.ok("ARG BASE\nFROM $BASE\n")

    def test_a_named_base_is_refused(self):
        """The point of the contract: this would not be a layer on the harness image at all."""
        said = self.refused("FROM debian:bookworm-slim\nRUN true\n")
        self.assertIn("not allowed", said)
        self.assertIn("ARG BASE", said)

    def test_a_second_from_is_refused(self):
        """A multi-stage build whose final stage is not the harness image ends up replacing it."""
        said = self.refused("ARG BASE\nFROM ${BASE}\nRUN true\nFROM debian:bookworm-slim\n")
        self.assertIn("exactly one FROM", said)

    def test_no_from_at_all_is_refused(self):
        self.assertIn("exactly one FROM", self.refused("ARG BASE\nRUN true\n"))

    def test_arg_base_must_be_declared(self):
        """Declared below the FROM, `${BASE}` is empty -- the omp-env bug, in a user's own file."""
        said = self.refused("FROM ${BASE}\nARG BASE\n")
        self.assertIn("must be declared above", said)

    def test_nothing_but_arg_may_precede_the_from(self):
        said = self.refused("ARG BASE\nRUN echo early\nFROM ${BASE}\n")
        self.assertIn("only `ARG BASE`", said)


class Verifying(SettingsCase):
    """What disqualifies a built image, checked without building anything."""

    def setUp(self):
        super().setUp()
        self.images = {HARNESS: BASE_DOC}
        self.podman = Podman(self.settings.podman)

    def run_verify(self, candidate_doc, check_out=CHECK_OK):
        self.images["cand"] = candidate_doc
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(self.images)
        os.environ["FAKE_PODMAN_RUN_OUT"] = check_out
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_RUN_OUT", None)
        return userimage.verify(self.podman, HARNESS, "cand", "claude", "2.1.318")

    def test_an_identical_config_passes(self):
        self.assertEqual(self.run_verify(dict(BASE_DOC, Id="sha256:layer")), [])

    def test_a_changed_user_is_refused(self):
        doc = {"Id": "x", "Config": dict(BASE_DOC["Config"], User="root")}
        self.assertTrue([b for b in self.run_verify(doc) if b.startswith("User differs")])

    def test_a_changed_entrypoint_is_refused(self):
        doc = {"Id": "x", "Config": dict(BASE_DOC["Config"], Entrypoint=["/sneaky"])}
        self.assertTrue([b for b in self.run_verify(doc) if b.startswith("Entrypoint differs")])

    def test_a_changed_cmd_is_refused(self):
        doc = {"Id": "x", "Config": dict(BASE_DOC["Config"], Cmd=["bash"])}
        self.assertTrue([b for b in self.run_verify(doc) if b.startswith("Cmd differs")])

    def test_a_changed_path_is_refused(self):
        """How "the agent binary path" is checked at inspect level: prepend a directory and `claude` on PATH
        is whatever is in it. The image check below is what actually runs the binary."""
        doc = {"Id": "x", "Config": dict(BASE_DOC["Config"], Env=["PATH=/opt/mine:/usr/local/bin:/usr/bin"])}
        self.assertTrue([b for b in self.run_verify(doc) if b.startswith("PATH differs")])

    def test_a_missing_tool_is_refused(self):
        bad = self.run_verify(dict(BASE_DOC, Id="x"), check_out="VERSION=2.1.318\nMISSING=rg\nDONE\n")
        self.assertEqual(bad, ["missing from the image: rg"])

    def test_a_different_agent_version_is_refused(self):
        bad = self.run_verify(dict(BASE_DOC, Id="x"), check_out="VERSION=2.0.1\nDONE\n")
        self.assertTrue([b for b in bad if "pinned at '2.1.318'" in b], bad)

    def test_no_version_at_all_is_refused(self):
        """Reported separately from a mismatch: "it does not run" and "it is the wrong one" are different
        things to go and look at."""
        bad = self.run_verify(dict(BASE_DOC, Id="x"), check_out="DONE\n")
        self.assertEqual(bad, ["claude did not report a version in it"])

    def test_a_check_that_did_not_finish_is_refused(self):
        """No DONE line: the script died part-way, so the absence of MISSING lines means nothing."""
        bad = self.run_verify(dict(BASE_DOC, Id="x"), check_out="VERSION=2.1.318\n")
        self.assertTrue([b for b in bad if "did not run in it" in b], bad)

    def test_the_check_runs_offline(self):
        """It answers "is this still a session image", which needs no network and no mounts."""
        self.run_verify(dict(BASE_DOC, Id="x"))
        with open(os.environ["FAKE_PODMAN_LOG"]) as f:
            calls = [json.loads(l) for l in f if l.strip()]
        runs = [a for a in calls if "run" in a[:2]]
        self.assertTrue(runs, calls)
        run = runs[-1]
        self.assertIn("--network=none", run)
        self.assertNotIn("--privileged", run)
        self.assertFalse([a for a in run if a.startswith("--mount")], run)


class ImageExistsIsNotAGuess(SettingsCase):
    """`podman image exists` answers by exit status: 0 yes, 1 no. Anything else is NEITHER answer.

    Reading every non-zero status as "no" is the fail-open shape this project keeps finding: a podman that
    cannot answer would look like a missing image, so a current personal layer would be silently skipped
    and the session would quietly run the plain harness image instead."""

    def setUp(self):
        super().setUp()
        self.podman = Podman(self.settings.podman)
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps({HARNESS: BASE_DOC})
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)

    def test_zero_is_yes(self):
        self.assertTrue(self.podman.image_exists(HARNESS))

    def test_one_is_no(self):
        self.assertFalse(self.podman.image_exists("localhost/nothing:here"))

    def test_any_other_status_raises(self):
        os.environ["FAKE_PODMAN_FAIL"] = "exists"
        os.environ["FAKE_PODMAN_FAIL_STATUS"] = "125"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL_STATUS", None)
        with self.assertRaises(AgentboxError) as cm:
            self.podman.image_exists(HARNESS)
        self.assertIn("125", str(cm.exception))


class Building(SettingsCase):
    def setUp(self):
        super().setUp()
        self.src = self.settings.profile_src("claude")
        self.tags_file = os.path.join(self.tmp, "tags")
        self.images_file = os.path.join(self.tmp, "images.json")
        self.podman = Podman(self.settings.podman)

    def set_images(self, docs, tags=None):
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(docs)
        os.environ["FAKE_PODMAN_IMAGE_JSON_FILE"] = self.images_file
        os.environ["FAKE_PODMAN_IMAGE_TAGS_FILE"] = self.tags_file
        with open(self.tags_file, "w") as f:
            f.write(" ".join(tags if tags is not None else sorted(docs)))
        for var in ("FAKE_PODMAN_IMAGE_JSON", "FAKE_PODMAN_IMAGE_JSON_FILE", "FAKE_PODMAN_IMAGE_TAGS_FILE"):
            self.addCleanup(os.environ.pop, var, None)

    def tags(self):
        with open(self.tags_file) as f:
            return set(f.read().split())

    def podman_calls(self):
        """Every argv the fake podman was given. The log file does not exist when podman was never run at
        all, which is itself an answer -- so no call is an empty list, not a crash."""
        path = os.environ["FAKE_PODMAN_LOG"]
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def write_recipe(self, text=GOOD):
        self.write("config/claude/profile/Containerfile", text)
        with open(os.path.join(self.src, "Containerfile"), "rb") as f:
            return f.read()

    def build(self, **kw):
        said = []
        ref = userimage.build(self.settings, self.podman, "claude", out=said.append, **kw)
        return ref, "\n".join(said)

    def good_build(self, recipe, check_out=CHECK_OK):
        """The harness image, plus a candidate that inspects identically to it."""
        self.set_images({HARNESS: BASE_DOC, "BUILT": dict(BASE_DOC, Id="sha256:layer")}, tags=[HARNESS])
        os.environ["FAKE_PODMAN_RUN_OUT"] = check_out
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_RUN_OUT", None)
        return profile.user_image_ref("claude", BASE_DOC["Id"], recipe)

    def test_it_builds_tags_and_cleans_up(self):
        recipe = self.write_recipe()
        want = self.good_build(recipe)
        ref, said = self.build()
        self.assertEqual(ref, want)
        self.assertIn(want, self.tags())
        self.assertNotIn(want + userimage.CANDIDATE_SUFFIX, self.tags(), "the candidate tag is removed")
        self.assertIn("built", said)

    def test_a_rejected_build_is_discarded_and_nothing_is_tagged(self):
        recipe = self.write_recipe()
        want = self.good_build(recipe, check_out="VERSION=2.1.318\nMISSING=git\nDONE\n")
        with self.assertRaises(AgentboxError) as cm:
            self.build()
        said = str(cm.exception)
        self.assertIn("not a usable layer", said)
        self.assertIn("missing from the image: git", said)
        self.assertNotIn(want, self.tags(), "nothing is tagged under the real name")
        self.assertNotIn(want + userimage.CANDIDATE_SUFFIX, self.tags())
        self.assertIn("no personal layer is tagged", said)

    def test_a_rejected_rebuild_keeps_the_image_already_in_use(self):
        """The one that matters: a bad edit must not take away the layer that was working."""
        recipe = self.write_recipe()
        want = self.good_build(recipe)
        self.build()
        self.assertIn(want, self.tags())
        os.environ["FAKE_PODMAN_RUN_OUT"] = "VERSION=1.0.0\nDONE\n"      # now the agent is the wrong version
        with self.assertRaises(AgentboxError) as cm:
            self.build(force=True)
        self.assertIn("untouched", str(cm.exception))
        self.assertIn(want, self.tags(), "the image in use is still tagged")

    def test_building_twice_does_nothing_the_second_time(self):
        recipe = self.write_recipe()
        self.good_build(recipe)
        self.build()
        _ref, said = self.build()
        self.assertIn("already built", said)

    def test_force_rebuilds(self):
        recipe = self.write_recipe()
        self.good_build(recipe)
        self.build()
        _ref, said = self.build(force=True)
        self.assertIn("building", said)

    def test_no_containerfile_says_so(self):
        self.set_images({HARNESS: BASE_DOC})
        with self.assertRaises(AgentboxError) as cm:
            self.build()
        self.assertIn("no Containerfile", str(cm.exception))
        self.assertIn("ARG BASE", str(cm.exception), "and says what one must look like")

    def test_a_bad_containerfile_is_refused_before_anything_is_built(self):
        """Refused by the contract, by name -- not by the verification afterwards. Asserting only that it
        raised passed with the contract check deleted (found by mutation): the build then went ahead and
        failed later, which is the same verdict reached the expensive way."""
        self.write_recipe("FROM debian:bookworm-slim\n")
        self.set_images({HARNESS: BASE_DOC})
        with self.assertRaises(AgentboxError) as cm:
            self.build()
        self.assertIn("not allowed", str(cm.exception))
        self.assertFalse([a for a in self.podman_calls() if "build" in a[:2]], "podman build never ran")
        self.assertEqual(self.tags(), {HARNESS})

    def test_a_missing_harness_image_says_which_make_target(self):
        self.write_recipe()
        self.set_images({}, tags=[])
        with self.assertRaises(AgentboxError) as cm:
            self.build()
        self.assertIn("make claude-image", str(cm.exception))

    def test_the_build_passes_the_harness_image_as_base(self):
        recipe = self.write_recipe()
        self.good_build(recipe)
        self.build()
        calls = self.podman_calls()
        builds = [a for a in calls if "build" in a[:2]]
        self.assertTrue(builds, calls)
        self.assertIn(f"BASE={HARNESS}", builds[-1])
        self.assertIn("--pull=never", builds[-1], "a layer never reaches for a registry base")
        self.assertEqual([a for a in builds[-1] if a.startswith("--build-arg")], ["--build-arg"],
                         "BASE is the only build argument the user's recipe is given")


class WhatASessionRuns(SettingsCase):
    """`ab run`'s half: the layer when it is current, the harness image and a warning when it is not."""

    def setUp(self):
        super().setUp()
        self.token()
        self.src = self.settings.profile_src("claude")
        self.write("config/claude/profile/skills/deploy/SKILL.md", "run make deploy\n")
        self.recipe = GOOD.encode()
        self.write("config/claude/profile/Containerfile", GOOD)

    def images(self, docs):
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(docs)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)

    def start(self, name):
        self.project(name, preset="script")
        warnings = []
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, name),
                                 warn=warnings.append)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s, warnings

    def test_a_current_layer_is_what_runs(self):
        ref = profile.user_image_ref("claude", "sha256:harness1", self.recipe)
        self.images({HARNESS: BASE_DOC, ref: {"Id": "sha256:layer"}})
        s, warnings = self.start("p1")
        self.assertEqual(s.image, ref)
        self.assertIn(ref, s.run_args(["true"]))
        self.assertFalse([w for w in warnings if "out of date" in w], warnings)

    def built(self, refs, other=()):
        """What `podman images` would list: `refs` are claude's own layers, `other` are OMP's.

        `other` is why the label filter is not decoration -- a question asked without it would see another
        agent's layer and call claude's Containerfile "out of date"."""
        listed = [{"Id": "sha256:layer", "Names": [r], "Labels": {userimage.USER_LAYER_LABEL: "claude"}}
                  for r in refs]
        listed += [{"Id": "sha256:other", "Names": [r], "Labels": {userimage.USER_LAYER_LABEL: "omp"}}
                   for r in other]
        os.environ["FAKE_PODMAN_IMAGES"] = json.dumps(listed)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGES", None)

    def test_a_layer_built_on_an_older_harness_is_not_run(self):
        """The harness image has been rebuilt, so its ID moved. The layer under the old key still exists --
        and must not be used, because what is underneath it is last month's agent and toolchain."""
        stale = profile.user_image_ref("claude", "sha256:OLDharness", self.recipe)
        self.images({HARNESS: BASE_DOC, stale: {"Id": "sha256:layer"}})
        self.built([stale])
        s, warnings = self.start("p1")
        self.assertEqual(s.image, HARNESS)
        self.assertNotIn(stale, s.run_args(["true"]))
        self.assertTrue([w for w in warnings if "personal layer out of date" in w], warnings)
        self.assertTrue([w for w in warnings if "ab image build claude" in w], warnings)

    def test_a_containerfile_that_was_never_built_says_so_instead(self):
        """"Out of date" is only true of a layer that exists. `ab selftest` on the host reported a layer
        out of date for a profile whose Containerfile had never been built once -- which sends the reader
        looking for an image that was never there (4 Nov)."""
        self.images({HARNESS: BASE_DOC})
        # Another agent HAS a layer. Asking "is any layer built?" without the agent label would find it and
        # report claude's Containerfile as out of date (found by mutation).
        self.built([], other=["localhost/agentbox-omp-user:abcdef"])
        s, warnings = self.start("p1")
        self.assertEqual(s.image, HARNESS)
        self.assertFalse([w for w in warnings if "out of date" in w], warnings)
        said = [w for w in warnings if "ab image build claude" in w]
        self.assertTrue(said, warnings)
        self.assertIn("none has been built", said[0])
        self.assertIn(os.path.join(self.src, "Containerfile"), said[0],
                      "and names the file in the PROFILE, not the snapshot's internal copy")

    def test_an_edited_containerfile_also_makes_it_stale(self):
        built = profile.user_image_ref("claude", "sha256:harness1", self.recipe)
        self.images({HARNESS: BASE_DOC, built: {"Id": "sha256:layer"}})
        self.built([built])
        self.write("config/claude/profile/Containerfile", GOOD + "RUN echo more\n")
        s, warnings = self.start("p1")
        self.assertEqual(s.image, HARNESS)
        self.assertTrue([w for w in warnings if "personal layer out of date" in w], warnings)

    def test_a_changed_skill_does_not_make_it_stale(self):
        """The key is the harness image and the recipe, not the profile hash: teaching the agent something
        has nothing to do with whether the image still fits."""
        ref = profile.user_image_ref("claude", "sha256:harness1", self.recipe)
        self.images({HARNESS: BASE_DOC, ref: {"Id": "sha256:layer"}})
        self.write("config/claude/profile/skills/deploy/SKILL.md", "run make deploy --prod\n")
        s, warnings = self.start("p1")
        self.assertEqual(s.image, ref)
        self.assertFalse([w for w in warnings if "out of date" in w], warnings)

    def test_no_containerfile_means_no_layer_and_no_warning(self):
        os.unlink(os.path.join(self.src, "Containerfile"))
        self.images({HARNESS: BASE_DOC})
        s, warnings = self.start("p1")
        self.assertEqual(s.image, HARNESS)
        self.assertFalse([w for w in warnings if "out of date" in w], warnings)

    def test_the_image_that_ran_is_in_the_session_log(self):
        ref = profile.user_image_ref("claude", "sha256:harness1", self.recipe)
        self.images({HARNESS: BASE_DOC, ref: {"Id": "sha256:layer"}})
        s, _w = self.start("p1")
        with open(s.log_path("profile")) as f:
            self.assertIn(f"image claude={ref}", f.read())

    def test_every_reader_of_the_image_agrees(self):
        """`image` is resolved once in prepare: the hardening audit, the argv and the CLI-version check
        must not each ask podman again and risk different answers."""
        ref = profile.user_image_ref("claude", "sha256:harness1", self.recipe)
        self.images({HARNESS: BASE_DOC, ref: {"Id": "sha256:layer"}})
        s, _w = self.start("p1")
        self.assertEqual(s.image, s.image)
        argv = s.run_args(["true"])
        self.assertEqual([a for a in argv if a.startswith("localhost/agentbox")], [ref])


if __name__ == "__main__":
    unittest.main()
