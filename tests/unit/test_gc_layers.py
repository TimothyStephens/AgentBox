"""`ab session gc` sweeps the two kinds of leftover that were outside it: personal image layers and
profile snapshots (docs/decisions.md, 5 Nov).

Both are produced ROUTINELY now rather than rarely -- a layer every time an image moves (`make omp-image`
then `ab image build omp` is two of them in a row), a snapshot every time a skill is edited -- and
`gc_images` could not see either: it filters on `agentbox.role=dev` plus the settings labels, and a
personal layer carries neither.

The two assertions that matter are not "it deletes things". They are:

  * **the current layer is never removed, however old it is.** Age is not staleness here: a layer built in
    September that still matches today's harness image and Containerfile is the one a session runs.
  * **nothing is swept while a session is alive.** A snapshot is a live session's read-only mount source,
    and session.json does not record which hash a session took (the metadata is written before the profile
    is loaded), so the question cannot be answered -- and the lesson of three liveness audits is to refuse
    an unanswerable question rather than guess.

The keep window (`image_keep_days`) is the third: a superseded layer stays for it, because the documented
rollback is `podman tag` onto the previous layer and a sweep that ran first would take the thing being
rolled back to.
"""
import json
import os
import time
import unittest

from agentbox import adapters, profile, session
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from .helpers import SettingsCase

DAY = 86400
RECIPE = b"ARG BASE\nFROM ${BASE}\nRUN echo mine\n"
HARNESS = {"claude": "localhost/agentbox-claude:latest", "omp": "localhost/agentbox-omp:latest"}


def layer(ref, agent, created, harness="sha256:harness1"):
    """One image as `podman images --format json` reports a personal layer."""
    return {"Id": "sha256:" + ref.rpartition(":")[2].ljust(12, "0"), "Names": [ref], "Created": created,
            "Labels": {"agentbox.user-layer": agent, "agentbox.harness-id": harness}}


class Layers(SettingsCase):
    def setUp(self):
        super().setUp()
        self.now = 1_000 * DAY
        # The harness images must inspect, or there is no current key to protect.
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps(
            {ref: {"Id": "sha256:harness1", "Config": {}} for ref in HARNESS.values()})
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGE_JSON", None)

    def recipe(self, agent="claude", text=RECIPE):
        src = os.path.join(self.settings.profile_src(agent))
        os.makedirs(src, mode=0o700, exist_ok=True)
        with open(os.path.join(src, profile.CONTAINERFILE), "wb") as f:
            f.write(text)
        return profile.user_image_ref(agent, "sha256:harness1", text)

    def images(self, *imgs):
        os.environ["FAKE_PODMAN_IMAGES"] = json.dumps(list(imgs))
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_IMAGES", None)

    def sweep(self, **kw):
        return session.gc_user_layers(self.settings, Podman(self.settings.podman), now=self.now, **kw)

    def rmi(self):
        return [c[1] for c in self.podman_calls() if c[:1] == ["rmi"]]

    def test_a_superseded_layer_past_the_keep_window_goes(self):
        current = self.recipe()
        stale = "localhost/agentbox-claude-user:0123456789abcdef"
        self.images(layer(current, "claude", self.now - 60 * DAY),
                    layer(stale, "claude", self.now - 60 * DAY))
        removed = self.sweep()
        self.assertEqual(self.rmi(), [layer(stale, "claude", 0)["Id"]])
        self.assertEqual(len(removed), 1)
        self.assertIn("0123456789abcdef", removed[0])

    def test_the_current_layer_is_never_removed_however_old_it_is(self):
        """Age is not staleness: a layer that still matches the harness image and the Containerfile is the
        one `ab session start` uses, whenever it was built."""
        current = self.recipe()
        self.images(layer(current, "claude", self.now - 900 * DAY))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.rmi(), [])

    def test_a_superseded_layer_inside_the_keep_window_stays(self):
        """The documented rollback is `podman tag` onto the previous layer. A sweep that took it the moment
        a rebuild superseded it would remove the only thing there is to roll back to."""
        current = self.recipe()
        self.images(layer(current, "claude", self.now),
                    layer("localhost/agentbox-claude-user:beefbeefbeefbeef", "claude", self.now - 2 * DAY))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.rmi(), [])

    def test_one_agents_key_never_decides_another_agents_layers(self):
        """The keys are per agent. Sweeping with claude's current key in hand must not make omp's current
        layer look superseded -- the shape of bug that a single flat list of images invites."""
        self.recipe("claude")
        omp_current = self.recipe("omp", RECIPE + b"RUN echo omp\n")
        self.images(layer(omp_current, "omp", self.now - 60 * DAY),
                    layer("localhost/agentbox-omp-user:aaaaaaaaaaaaaaaa", "omp", self.now - 60 * DAY))
        removed = self.sweep()
        self.assertEqual(len(removed), 1, removed)
        self.assertIn("aaaaaaaaaaaaaaaa", removed[0])

    def test_the_query_asks_podman_for_user_layers_only(self):
        """Filtered by the label `ab image build` writes, so the sweep cannot reach an image it did not
        build -- the harness images and Tim's own are in the same storage."""
        self.recipe()
        self.images()
        self.sweep()
        calls = [c for c in self.podman_calls() if c[:1] == ["images"]]
        self.assertTrue(calls)
        for c in calls:
            self.assertIn("label=agentbox.user-layer=claude", c + ["label=agentbox.user-layer=omp"])

    def test_rmi_is_not_forced_so_an_image_in_use_or_tagged_again_stays(self):
        """A rollback tag (`podman tag`) or a container still using the layer makes `rmi` fail. It is
        reported and kept, never forced -- same as gc_images."""
        current = self.recipe()
        stale = "localhost/agentbox-claude-user:0123456789abcdef"
        self.images(layer(current, "claude", self.now - 60 * DAY),
                    layer(stale, "claude", self.now - 60 * DAY))
        os.environ["FAKE_PODMAN_FAIL"] = layer(stale, "claude", 0)["Id"]
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        said = []
        self.assertEqual(self.sweep(warn=said.append), [])
        self.assertTrue([m for m in said if "kept" in m and "0123456789abcdef" in m], said)

    def test_a_layer_whose_harness_image_is_gone_is_left_alone(self):
        """`make base-image` can leave an agent image missing or unreadable. Then there is no current key,
        so every layer would look superseded -- the fail-open answer. Nothing is removed instead."""
        self.recipe()
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = "{}"          # no harness image inspects
        self.images(layer("localhost/agentbox-claude-user:0123456789abcdef", "claude", self.now - 60 * DAY))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.rmi(), [])

    def test_a_harness_image_that_inspects_without_an_id_is_unknown_too(self):
        """The test above covers the harness image being GONE (inspect fails). This covers podman
        answering with a doc that carries no Id -- still nothing to key on, still not a licence to sweep.
        It exists because a mutation showed no test reached that branch: the failing-inspect test went
        through the exception handler instead."""
        self.recipe()
        os.environ["FAKE_PODMAN_IMAGE_JSON"] = json.dumps({ref: {"Config": {}} for ref in HARNESS.values()})
        self.images(layer("localhost/agentbox-claude-user:0123456789abcdef", "claude", self.now - 60 * DAY))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.rmi(), [])

    def test_an_unreadable_image_list_removes_nothing(self):
        """`podman images` failing is not "there are no layers"."""
        self.recipe()
        os.environ["FAKE_PODMAN_FAIL"] = "images"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_FAIL", None)
        with self.assertRaises(AgentboxError):
            self.sweep()
        self.assertEqual(self.rmi(), [])

    def test_no_containerfile_means_no_layer_is_expected_and_none_is_swept(self):
        """Without a Containerfile in the profile there is no current key at all. The same fail-open trap
        as a missing harness image, and the same answer."""
        self.images(layer("localhost/agentbox-claude-user:0123456789abcdef", "claude", self.now - 60 * DAY))
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.rmi(), [])


class Snapshots(SettingsCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()

    def profile_src(self, agent="claude", text="skill\n"):
        src = self.settings.profile_src(agent)
        os.makedirs(os.path.join(src, "skills"), mode=0o700, exist_ok=True)
        with open(os.path.join(src, "skills", "s.md"), "w") as f:
            f.write(text)
        return src

    def snap(self, agent, name, age_days=0.0):
        d = os.path.join(self.settings.profiles_dir, agent, name)
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, "marker"), "w") as f:
            f.write("x")
        t = self.now - age_days * DAY
        os.utime(d, (t, t))
        return d

    def sweep(self, **kw):
        return session.gc_profile_snapshots(self.settings, Podman(self.settings.podman), now=self.now, **kw)

    def live_session(self, name="abt-unit-claude-p1-1", project="p1"):
        d = self.settings.session_run_dir(name)
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, "session.json"), "w") as f:
            json.dump({"session": name, "agent": "claude", "project": project, "pid": os.getpid(),
                       "pid_start": session._start_time(os.getpid())}, f)
        return name

    def test_an_unused_snapshot_past_the_keep_window_goes(self):
        src = self.profile_src()
        current = self.snap("claude", profile.tree_hash(src), age_days=100)
        old = self.snap("claude", "0123456789abcdef", age_days=100)
        removed = self.sweep()
        self.assertTrue(os.path.isdir(current), "the current profile's snapshot stays, whatever its age")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(len(removed), 1, removed)
        self.assertIn("0123456789abcdef", removed[0])

    def test_a_snapshot_used_recently_stays(self):
        self.profile_src()
        recent = self.snap("claude", "0123456789abcdef", age_days=1)
        self.assertEqual(self.sweep(), [])
        self.assertTrue(os.path.isdir(recent))

    def test_nothing_is_swept_while_a_session_is_alive(self):
        """A snapshot is a live session's read-only mount source and session.json does not say which hash
        it took, so "in use" cannot be answered. The sweep declines rather than guessing."""
        self.profile_src()
        old = self.snap("claude", "0123456789abcdef", age_days=100)
        self.live_session()
        self.assertEqual(self.sweep(), [])
        self.assertTrue(os.path.isdir(old))

    def test_a_dead_sessions_run_dir_does_not_hold_the_sweep_off(self):
        """The control for the test above: it must be LIVENESS stopping the sweep, not the mere existence
        of a session run dir -- otherwise the test would pass with the sweep broken."""
        self.profile_src()
        old = self.snap("claude", "0123456789abcdef", age_days=100)
        name = self.live_session()
        with open(os.path.join(self.settings.session_run_dir(name), "session.json"), "w") as f:
            json.dump({"session": name, "agent": "claude", "project": "p1", "pid": 2 ** 22,
                       "pid_start": "1"}, f)
        self.assertEqual(len(self.sweep()), 1)
        self.assertFalse(os.path.exists(old))

    def test_only_hash_shaped_directories_are_touched(self):
        """Anything else under <data>/profiles/<agent>/ was not put there by snapshot(), so it is not this
        sweep's to remove -- including a half-copied `.<hash>.<pid>.tmp` another process is writing."""
        self.profile_src()
        keep = [self.snap("claude", "scratch", age_days=100),
                self.snap("claude", ".0123456789abcdef.7.tmp", age_days=100),
                self.snap("claude", "0123456789ABCDEF", age_days=100),
                self.snap("claude", "0123456789abcde", age_days=100)]
        loose = os.path.join(self.settings.profiles_dir, "claude", "note.txt")
        with open(loose, "w") as f:
            f.write("x")
        os.utime(loose, (self.now - 100 * DAY,) * 2)
        self.assertEqual(self.sweep(), [])
        for d in keep:
            self.assertTrue(os.path.isdir(d), d)
        self.assertTrue(os.path.exists(loose))

    def test_a_snapshot_of_an_agent_with_no_profile_at_all_still_goes(self):
        """Deleting the profile leaves its snapshots behind with nothing to compare against. There is no
        current hash, so every snapshot is unused -- which is the answer, not a reason to keep them."""
        old = self.snap("omp", "0123456789abcdef", age_days=100)
        self.assertEqual(len(self.sweep()), 1)
        self.assertFalse(os.path.exists(old))

    def test_using_a_snapshot_again_counts_as_use(self):
        """mtime is the "last used" signal, so snapshot() has to refresh it on reuse. Without this, a
        snapshot in daily use for a week is swept the moment it crosses the window."""
        src = self.profile_src()
        d, digest = profile.snapshot(self.settings, "claude", src=src)
        os.utime(d, (self.now - 100 * DAY,) * 2)
        again, digest2 = profile.snapshot(self.settings, "claude", src=src)
        self.assertEqual((again, digest2), (d, digest))
        self.assertGreater(os.stat(d).st_mtime, self.now - DAY)
        self.assertEqual(self.sweep(), [])


class Wiring(SettingsCase):
    def test_gc_sweeps_both_kinds(self):
        """`ab session gc` must actually call them: a sweep nothing invokes is the dead-code class."""
        from unittest import mock
        with mock.patch.object(session, "gc_user_layers", return_value=["layer x"]) as layers, \
             mock.patch.object(session, "gc_profile_snapshots", return_value=["snapshot y"]) as snaps:
            removed = session.gc(self.settings, Podman(self.settings.podman), warn=lambda m: None)
        self.assertEqual(layers.call_count, 1)
        self.assertEqual(snaps.call_count, 1)
        self.assertEqual(removed[-2:], ["layer x", "snapshot y"])


class AgentNames(SettingsCase):
    def test_every_name_has_an_adapter(self):
        """`adapters.NAMES` is what "for each agent" iterates over. A name in it with no adapter, or an
        adapter missing from it, is a sweep (or a CLI choice) that silently skips an agent."""
        for n in adapters.NAMES:
            self.assertEqual(adapters.get(n).name, n)
        with self.assertRaises(AgentboxError):
            adapters.get("nosuch")

    def test_the_cli_offers_exactly_those_agents(self):
        from agentbox import cli
        parser = cli.parser(self.settings)
        found = set()
        for action in _walk_actions(parser):
            if action.dest == "agent" and action.choices:
                found |= set(action.choices)
        self.assertEqual(found, set(adapters.NAMES))


def _walk_actions(parser):
    for a in parser._actions:
        if hasattr(a, "choices") and isinstance(a.choices, dict):
            for sub in a.choices.values():
                yield from _walk_actions(sub)
        else:
            yield a
