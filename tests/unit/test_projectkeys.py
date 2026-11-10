"""The project settings table (`agentbox/projectkeys.py`): one row per registry key, every property of that
key decided in one place.

Five audit findings (F4, F6, F8, F10, F11) were all the same shape: a key missing from one of five
hand-maintained lists (cli.SECURITY_OVERRIDES, cli.UNSETTABLE, registry.PROJECT_KEYS, the display gating in
_print_effective, the ceiling's looseness orders). These tests pin that the lists are now *derived*, and that
a new key cannot be added without answering every column.
"""
import unittest

from agentbox import cli, presets, projectkeys, registry
from agentbox.errors import AgentboxError


# Frozen on purpose: adding a key means updating this list too, which is the moment to check that every
# property below was decided rather than defaulted.
EXPECTED_KEYS = {
    "root", "preset",
    "compose", "egress", "podman", "caps", "secrets", "approvals", "data",
    "data_ro", "data_rw", "extra_domains", "compose_project", "observe_logs", "dev_images",
    "mask_skip", "swap", "mask", "toulmin", "mount", "build_memory", "profile", "base_branch",
    "history_secrets_accepted", "grant_exposure_accepted",
}


class TableIsComplete(unittest.TestCase):
    def test_the_table_covers_exactly_the_registry_keys(self):
        self.assertEqual({k.name for k in projectkeys.KEYS}, EXPECTED_KEYS)
        self.assertEqual(set(registry.PROJECT_KEYS), EXPECTED_KEYS, "the registry accepts exactly these")

    def test_every_row_answers_every_column(self):
        for key in projectkeys.KEYS:
            with self.subTest(key=key.name):
                self.assertIn(key.kind, ("preset", "project", "structural", "ack"))
                self.assertIn(key.arity, ("scalar", "list", "bool", "map"))
                self.assertIn(key.display, ("always", "stored"))
                self.assertIn(key.validate, projectkeys.VALIDATORS, "validator must exist")
                self.assertIsInstance(key.security, bool)
                self.assertIsInstance(key.clears, bool)
                self.assertIsInstance(key.unsettable, bool)
                self.assertIsInstance(key.values, tuple)
                if key.inert:
                    self.assertIn(key.inert, projectkeys.INERT_WHEN)
                    self.assertTrue(key.inert_reason, "an inert key must say why")
                if key.flags:
                    self.assertTrue(key.dest, f"{key.name}: a flag needs a dest")

    def test_a_new_row_must_decide_every_property(self):
        """No defaults on the decision fields: a half-filled row is a TypeError at import, not a key that
        quietly inherits someone else's answer."""
        with self.assertRaises(TypeError):
            projectkeys.Key(name="newkey")

    def test_an_ordered_key_has_a_tightest_value(self):
        for key in projectkeys.KEYS:
            if key.values or key.name == "podman":
                self.assertIsNotNone(key.tightest, key.name)


class DerivedViews(unittest.TestCase):
    def test_presets_settings_and_allowed_derive_from_the_table(self):
        self.assertEqual(set(presets.SETTINGS), {k.name for k in projectkeys.KEYS if k.kind == "preset"})
        for name, values in presets.ALLOWED.items():
            self.assertEqual(tuple(values), projectkeys.BY_NAME[name].values, name)

    def test_looseness_derives_from_the_table(self):
        for name, order in presets.LOOSENESS.items():
            self.assertEqual(list(order), list(projectkeys.BY_NAME[name].values), name)

    def test_cli_lists_derive_from_the_table(self):
        self.assertEqual(set(cli.UNSETTABLE), {k.name for k in projectkeys.KEYS if k.unsettable})
        self.assertEqual(set(cli.SECURITY_OVERRIDES), {k.name for k in projectkeys.KEYS if k.clears})
        self.assertEqual(set(cli.CLEAR_FLAGS.values()),
                         {k.name for k in projectkeys.KEYS if k.arity == "list" and k.unsettable})
        self.assertEqual(set(cli.SETTING_FLAGS), {k.name for k in projectkeys.KEYS if k.flags})

    def test_compose_project_and_dev_images_are_security_relevant_and_cleared(self):
        """F4: both were missing from SECURITY_OVERRIDES, so a preset change left a waived live-stack
        ownership check and an extra image allowlist in place."""
        for name in ("compose_project", "dev_images"):
            key = projectkeys.BY_NAME[name]
            self.assertTrue(key.security, name)
            self.assertTrue(key.clears, name)
            self.assertIn(name, cli.SECURITY_OVERRIDES)

    def test_the_acks_and_tree_properties_are_not_cleared_by_a_preset_change(self):
        for name in ("mask_skip", "swap", "mask", "build_memory", "root", "preset",
                     "history_secrets_accepted", "grant_exposure_accepted"):
            self.assertFalse(projectkeys.BY_NAME[name].clears, name)


class Looseness(unittest.TestCase):
    def test_scalar_orders(self):
        self.assertTrue(projectkeys.is_looser("egress", "open", "locked"))
        self.assertTrue(projectkeys.is_looser("egress", "packages", "locked"))
        self.assertFalse(projectkeys.is_looser("egress", "locked", "open"))
        self.assertFalse(projectkeys.is_looser("egress", "open", "open"))
        self.assertTrue(projectkeys.is_looser("approvals", "off", "on"))
        self.assertTrue(projectkeys.is_looser("observe_logs", True, False))
        self.assertFalse(projectkeys.is_looser("observe_logs", False, True))

    def test_podman_is_a_capability_set_not_a_ladder(self):
        """F-3: `none < observe < dev < exec` made a ceiling of `{tier = "dev"}` ALLOW an override of
        `{tier = "observe"}` -- but observe reads the LIVE host stack while dev runs the project's own
        sandboxed one. Different capabilities, not degrees, so looser is "not a subset"."""
        caps = projectkeys.podman_capabilities
        self.assertEqual(caps({"tier": "none"}), frozenset())
        self.assertEqual(caps(None), frozenset())
        self.assertEqual(caps({"tier": "observe"}), frozenset({"live-observe"}))
        self.assertEqual(caps({"tier": "dev"}), frozenset({"dev-stack"}))
        self.assertEqual(caps({"tier": "none", "live": "observe"}), frozenset({"live-observe"}))
        self.assertEqual(caps({"tier": "dev", "live": "observe"}),
                         frozenset({"dev-stack", "live-observe"}))

        looser = lambda v, than: projectkeys.is_looser("podman", v, than)
        self.assertTrue(looser({"tier": "observe"}, {"tier": "dev"}),
                        "a dev ceiling must refuse observe: it reads the live stack, which dev cannot")
        self.assertTrue(looser({"tier": "dev"}, {"tier": "observe"}))
        self.assertTrue(looser({"tier": "dev", "live": "observe"}, {"tier": "dev"}))
        self.assertTrue(looser({"tier": "observe"}, {"tier": "none"}))
        self.assertFalse(looser({"tier": "none"}, {"tier": "dev"}), "none is never looser")
        self.assertFalse(looser(None, {"tier": "observe"}))
        self.assertFalse(looser({"tier": "dev"}, {"tier": "dev"}))
        self.assertFalse(looser({"tier": "dev"}, {"tier": "dev", "live": "observe"}), "a subset")
        self.assertFalse(looser({"tier": "none", "live": "observe"}, {"tier": "observe"}),
                         "the same capability by another spelling")

    def test_unordered_keys_are_never_looser(self):
        self.assertFalse(projectkeys.is_looser("data_ro", ["/a"], []))
        self.assertFalse(projectkeys.is_looser("mask_skip", ["x"], []))

    def test_label_for_a_change(self):
        self.assertEqual(projectkeys.change_label("egress", "locked", "open"), "loosened")
        self.assertEqual(projectkeys.change_label("egress", "open", "locked"), "tightened")
        self.assertEqual(projectkeys.change_label("egress", "open", "open"), "")
        self.assertEqual(projectkeys.change_label("data_ro", ["/a"], []), "")


class Inertness(unittest.TestCase):
    def ctx(self, tier="none", live=None, egress="packages"):
        return {"tier": tier, "live": live, "egress": egress, "live_access": "observe" in (tier, live)}

    def test_always_inert_keys(self):
        for name in ("caps", "secrets"):
            self.assertEqual(projectkeys.BY_NAME[name].inert, "always", name)
            self.assertTrue(projectkeys.inert_reason(name, self.ctx()), name)

    def test_live_stack_keys_are_inert_without_live_access(self):
        for name in ("compose_project", "observe_logs"):
            self.assertTrue(projectkeys.inert_reason(name, self.ctx()), name)
            self.assertFalse(projectkeys.inert_reason(name, self.ctx(live="observe")), name)

    def test_dev_tier_keys_are_inert_off_the_dev_tier(self):
        for name in ("dev_images", "build_memory"):
            self.assertTrue(projectkeys.inert_reason(name, self.ctx()), name)
            self.assertFalse(projectkeys.inert_reason(name, self.ctx(tier="dev")), name)

    def test_extra_domains_is_inert_under_locked_egress(self):
        self.assertTrue(projectkeys.inert_reason("extra_domains", self.ctx(egress="locked")))
        self.assertFalse(projectkeys.inert_reason("extra_domains", self.ctx(egress="packages")))

    def test_a_plain_key_is_never_inert(self):
        self.assertEqual(projectkeys.inert_reason("egress", self.ctx()), "")
        self.assertEqual(projectkeys.inert_reason("root", self.ctx()), "")


if __name__ == "__main__":
    unittest.main()


class SwapNeverExposesASecret(unittest.TestCase):
    """F6: `swap` maps a masked destination to the dev stand-in mounted in its place. Two rules keep it from
    becoming a way to READ a secret: the stand-in may not itself be a masked name, and the destination must
    be one."""

    def check(self, swap):
        return projectkeys.validate_value("swap", "project p", swap)

    def test_the_default_swap_is_legal(self):
        from agentbox import mounts
        self.check(mounts.DEFAULT_SWAP)
        self.check({".env": ".env.dev"})

    def test_a_masked_name_as_the_stand_in_is_refused(self):
        """`swap = {"notes.txt" = ".env"}` mounted the REAL .env at an unmasked path, in plain sight, while
        the mask dutifully covered the original."""
        for bad in ({"notes.txt": ".env"}, {".env": "other.pem"}, {".env": "id.key"},
                    {".env": ".env.prod"}, {".env.local": ".env"}):
            with self.assertRaises(AgentboxError, msg=bad) as cm:
                self.check(bad)
            self.assertIn("masked name", str(cm.exception))

    def test_an_unmasked_destination_is_refused(self):
        for bad in ({"notes.txt": ".env.dev"}, {"secrets.env": "secrets.dev.env"}, {"app.py": "app.dev.py"}):
            with self.assertRaises(AgentboxError, msg=bad):
                self.check(bad)

    def test_a_masked_destination_with_a_plain_stand_in_is_allowed(self):
        self.check({".env.local": ".env.dev"})
        self.check({"server.pem": "dev-cert.txt"})
        self.check({"id.key": "dev-key.txt"})
        self.check({".env": ".env.example"})          # exempt, so not a secret the masks hide

    def test_plain_file_names_only(self):
        for bad in ({"../escape": ".env.dev"}, {".env": "../outside.env"}, {".env": "sub/dir.env"}):
            with self.assertRaises(AgentboxError, msg=bad):
                self.check(bad)


class DevImagesCannotNameAgentboxsOwn(unittest.TestCase):
    """F-9: `dev_images` patterns are matched against an image's RepoTags in the gate, so a broad pattern --
    `--dev-image '*'`, `--dev-image 'localhost/*'` -- let the dev tier run agentbox's OWN images, which are
    exactly what the gate's build-tag rule reserves. A pattern that can match a reserved name is refused
    where it is written, not left to the gate."""

    def check(self, patterns):
        return projectkeys.validate_value("dev_images", "project p", patterns)

    def test_a_pattern_that_matches_a_reserved_name_is_refused(self):
        for bad in ("localhost/*", "localhost/agentbox-claude:latest", "localhost/agentbox*",
                    "localhost/agentbox-omp:latest", "localhost/agentbox-lockdown:latest", "agentbox*"):
            with self.assertRaises(AgentboxError, msg=bad) as cm:
                self.check([bad])
            self.assertIn("agentbox", str(cm.exception))
            self.assertIn("reserved", str(cm.exception))

    def test_a_bare_wildcard_was_already_refused_by_the_shape_rule(self):
        """`*` and `*:*` never passed: a pattern must start with a letter or digit. `localhost/*` did."""
        for bad in ("*", "*:*", "*agentbox*"):
            with self.assertRaises(AgentboxError, msg=bad):
                self.check([bad])

    def test_a_normal_pattern_is_allowed(self):
        self.check(["localhost/myapp:*", "localhost/shop_web", "localhost/x:1.2", "docker.io/library/*"])

    def test_every_adapter_image_is_among_the_probes(self):
        """The probes must stay the names agentbox actually runs, or the check tests nothing: each agent's
        own image has to be one a pattern is probed against."""
        from agentbox import adapters
        for agent in ("claude", "omp"):
            self.assertIn(adapters.get(agent).image, projectkeys.RESERVED_IMAGES, agent)


class TheSecurityColumnDrivesSomething(unittest.TestCase):
    """F-8: `security` was a column nothing read -- documentation pretending to be code. It now decides
    three things, each tested here or alongside: which settings `projectkeys.loosenings` compares (so which
    edits `ab project set` will not let past an unacknowledged grant exposure, audit F-5), which overrides
    `ab project show` marks as loosening their preset, and the invariant below."""

    def test_every_cleared_key_is_security_relevant(self):
        """A preset change drops an override so it cannot outlive the posture it was set against. That is a
        security measure, so `clears` without `security` would be a contradiction -- and the two columns
        cannot drift apart without this failing."""
        for key in projectkeys.KEYS:
            if key.clears:
                self.assertTrue(key.security, f"{key.name}: cleared on a preset change but not security")

    def loosenings(self, before, after):
        ps = presets.BUILTIN
        return projectkeys.loosenings(presets.effective("p", before, ps), presets.effective("p", after, ps),
                                      before, after)

    def test_a_non_security_setting_is_never_a_loosening(self):
        base = {"root": "/r", "preset": "script"}
        self.assertEqual(self.loosenings(base, {**base, "build_memory": "8g"}), [])

    def test_an_ordered_security_setting_is(self):
        base = {"root": "/r", "preset": "script"}
        got = self.loosenings(base, {**base, "egress": "open"})
        self.assertTrue(any("egress" in g for g in got), got)
        self.assertEqual(self.loosenings({**base, "egress": "open"}, base), [], "tightening is not")

    def test_a_security_key_that_is_not_a_preset_setting_is_compared_too(self):
        """observe_logs, compose_project, dev_images and mask_skip are not resolvable from a preset, so they
        are absent from the effective dict; comparing only that dict skipped them."""
        base = {"root": "/r", "preset": "build"}
        for key, value in (("observe_logs", True), ("compose_project", "other"),
                           ("dev_images", ["localhost/x:*"]), ("mask_skip", ["vendor"])):
            got = self.loosenings(base, {**base, key: value})
            self.assertTrue(any(key in g for g in got), f"{key}: {got}")

    def test_shortening_a_list_setting_is_not_a_loosening(self):
        base = {"root": "/r", "preset": "build", "dev_images": ["localhost/x:*", "localhost/y:*"]}
        self.assertEqual(self.loosenings(base, {**base, "dev_images": ["localhost/x:*"]}), [])
        self.assertEqual(self.loosenings(base, {k: v for k, v in base.items() if k != "dev_images"}), [])


class SwapTargetsAreAlwaysMaskedNames(unittest.TestCase):
    """F-12, the deadness proof for `mounts._masked`'s `name in swap_targets` branch.

    That branch masked a swap TARGET whatever it was called. Since F6 every target must itself be a masked
    name, so the pattern test that follows it already returns True -- the branch cannot change an answer.
    Removing it needed this first (CLAUDE.md: dead code goes only with a test proving deadness)."""

    def test_every_valid_swap_target_is_a_masked_name(self):
        from agentbox import mounts
        valid = [mounts.DEFAULT_SWAP, {".env.local": ".env.dev"}, {"server.pem": "dev-cert.txt"},
                 {"id.key": "dev-id.txt"}, {".env.production": ".env.dev"}]
        for swap in valid:
            checked = projectkeys.validate_value("swap", "project p", swap)
            for target in checked:
                with self.subTest(target=target):
                    self.assertTrue(projectkeys.masked_name(target),
                                    "the validator let through a target the masks would not cover")

    def test_the_branch_is_gone_and_the_masking_is_unchanged(self):
        import inspect
        from agentbox import mounts
        self.assertEqual(list(inspect.signature(mounts._masked).parameters), ["name"])
        self.assertTrue(mounts._masked(".env.local"))
        self.assertTrue(mounts._masked("server.pem"))
        self.assertFalse(mounts._masked(".env.dev"), "the stand-ins stay visible")
        self.assertFalse(mounts._masked("notes.txt"))


class CeilingMessageNamesTheRightLimit(unittest.TestCase):
    """F-12: the refusal said "the preset's own value is False" for `observe_logs`, which no preset sets --
    the limit there is the settings table's tightest value. It now says which it is."""

    def refusal(self, project):
        with self.assertRaises(AgentboxError) as cm:
            presets.effective("p", project, presets.BUILTIN)
        return str(cm.exception)

    def test_a_key_the_preset_sets_is_called_the_presets_value(self):
        msg = self.refusal({"preset": "lockdown", "egress": "open"})
        self.assertIn("the preset's own value is 'locked'", msg)

    def test_a_key_the_preset_does_not_set_is_called_the_tightest_value(self):
        msg = self.refusal({"preset": "lockdown", "observe_logs": True})
        self.assertNotIn("the preset's own value", msg)
        self.assertIn("tightest", msg)
        self.assertIn("False", msg)


class NoDeadParameters(unittest.TestCase):
    def test_grant_kw_takes_only_the_settings(self):
        """F-12: `_grant_kw(s, _reg=None, exclude=None)` kept two parameters from before
        paths.check_overlaps took over cross-project overlap; both were ignored, and passing them read as
        if they still scoped the check."""
        import inspect
        self.assertEqual(list(inspect.signature(cli._grant_kw).parameters), ["s"])

    def test_history_secrets_no_longer_takes_a_swap_map(self):
        """It stopped using it when swap targets left the "is this masked?" test; a parameter that is read
        nowhere still reads as if it narrowed the search."""
        import inspect
        from agentbox import session
        self.assertEqual(list(inspect.signature(session.history_secrets).parameters),
                         ["root", "grants", "mask_skip", "timeout"])
        self.assertEqual(list(inspect.signature(session._history_masked).parameters),
                         ["git_dir", "timeout"])
