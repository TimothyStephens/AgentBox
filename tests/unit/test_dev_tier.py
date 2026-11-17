"""The dev tier's policy and post-create checks, without podman (review 16 Nov, item 19).

What the dev tier hands sockguard (sockguard.dev_config), what the session hands the gate (Session.gate_config,
built against the fake podman), the gate's post-create tables for volumes and networks, the compat `Binds`
read-only rewrite, and the agentbox.slice check -- none of which had a unit test, so a change to any of them
reached `make itest` on the host before anything could tell.
"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from agentbox import cgroup, session, sockguard
from agentbox.errors import AgentboxError

from .helpers import SettingsCase, TempCase
from .test_gate import CFG, FakeGate

DEV = {sockguard.DEV_OWNER: CFG["session"], "agentbox.role": "dev"}


class SockguardDevPolicy(unittest.TestCase):
    def test_exact_keys(self):
        """The dev policy's shape, key by key: sockguard ignores a key it does not know, so a typo here
        silently drops a rule."""
        doc = json.loads(sockguard.dev_config("abt-s-1", bind_roots=["/b", "/a"]))
        self.assertEqual(sorted(doc), ["clients", "insecure_allow_body_blind_writes",
                                       "insecure_allow_read_exfiltration", "listen", "log", "ownership", "upstream"])
        self.assertEqual(doc["listen"], {"socket": sockguard.LISTEN, "socket_mode": "0600"})
        self.assertEqual(doc["upstream"], {"socket": sockguard.UPSTREAM, "flavor": "podman", "request_timeout": "off"})
        self.assertEqual(sorted(doc["log"]), ["access_log", "audit", "format", "level"])
        self.assertEqual(doc["ownership"],
                         {"owner": "abt-s-1", "label_key": sockguard.DEV_OWNER, "allow_unowned_images": True})
        self.assertEqual(sorted(doc["clients"]), ["default_profile", "profiles"])
        self.assertEqual(doc["clients"]["default_profile"], "session")
        (prof,) = doc["clients"]["profiles"]
        self.assertEqual(sorted(prof), ["mode", "name", "request_body", "rules"])
        self.assertEqual((prof["name"], prof["mode"]), ("session", "enforce"))
        body = prof["request_body"]
        self.assertEqual(sorted(body), ["build", "container_create", "container_remove", "exec", "image_pull",
                                        "libpod_container_create"])
        self.assertEqual(body["container_create"], {"allowed_bind_mounts": ["/a", "/b"]}, "sorted")
        self.assertEqual(body["libpod_container_create"], body["container_create"])
        self.assertEqual(body["container_remove"], {"allow_force": True, "allow_remove_volumes": True})
        self.assertEqual(body["exec"], {"allow_root_user": True, "allow_privileged": False})
        self.assertEqual(body["image_pull"], {"allowed_registries": sockguard.REGISTRIES, "allow_official": True})
        self.assertEqual(body["build"], {"allow_run_instructions": True, "allow_remote_context": False,
                                         "allow_host_network": False})
        self.assertEqual(prof["rules"], list(sockguard.DEV_RULES))
        self.assertEqual(prof["rules"][-1]["action"], "deny", "deny-all stays last")

    def test_mode_is_enforce_or_audit(self):
        for mode in ("audit", "enforce"):
            doc = json.loads(sockguard.dev_config("s", bind_roots=[], mode=mode))
            self.assertEqual(doc["clients"]["profiles"][0]["mode"], mode)
        with self.assertRaises(AgentboxError):
            sockguard.dev_config("s", bind_roots=[], mode="warn")
        prof = json.loads(sockguard.dev_discovery_config("s", bind_roots=[]))["clients"]["profiles"][0]
        self.assertEqual((prof["mode"], [r["action"] for r in prof["rules"]]), ("audit", ["deny"]))


class GateConfig(SettingsCase):
    """Session.gate_config against the fake podman: the gate's whole policy comes from this one dict."""

    def setUp(self):
        super().setUp()
        self.token()

    def make(self, **extra):
        self.root = self.project("p1", preset="pods", **extra)
        s = session.make_session(self.settings, "claude", session.load_project(self.settings, "p1"),
                                 warn=lambda m: None)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s

    def config(self, s, proxy_env=None):
        s.dev_proxy_env()                            # reads the session subnet from `network inspect`
        return s.gate_config("/svc/podman.sock", proxy_env or {"http_proxy": "http://p:3128"})

    def test_exact_keys_and_values(self):
        s = self.make()
        os.environ["FAKE_PODMAN_NETWORKS"] = json.dumps({s.int_net: {"IPAddress": "10.89.9.7"}})
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_NETWORKS", None)
        cfg = self.config(s)
        self.assertEqual(sorted(cfg), sorted([
            "listen", "upstream", "raw", "session", "network", "labels", "proxy_env", "bind_roots", "covered",
            "covered_ids", "ro_roots", "masked", "mask_names", "subnets", "cgroup_parent", "slice", "memory", "pids",
            "memory_max", "pids_max", "build_memory", "holder", "registries", "images", "spool", "built_record",
            "build_proxy_env", "build_dns", "log"]))
        self.assertEqual((cfg["listen"], cfg["upstream"], cfg["raw"]),
                         (s.gate_socket, s.sockguard_dev_socket, "/svc/podman.sock"))
        self.assertEqual((cfg["session"], cfg["network"], cfg["holder"]), (s.name, s.int_net, f"{s.name}-holder"))
        self.assertEqual(cfg["labels"]["agentbox.role"], "dev")
        self.assertEqual(cfg["labels"]["agentbox.session"], s.name)
        self.assertEqual(cfg["labels"]["agentbox-test"], "1", "the settings' labels are on every dev object")
        self.assertEqual(cfg["proxy_env"], {"http_proxy": "http://p:3128"})
        self.assertEqual(cfg["bind_roots"], [self.root])
        self.assertEqual(cfg["ro_roots"], [])
        self.assertEqual(cfg["subnets"], ["10.89.9.0/24"], "from `network inspect ... Subnet`, not the gateway")
        self.assertEqual(cfg["build_dns"], ["10.89.9.1"], "from `network inspect ... Gateway`")
        self.assertEqual(cfg["build_proxy_env"]["http_proxy"], "http://10.89.9.7:3128",
                         "builds reach the proxy by IP: names do not resolve in a build's RUN steps")
        self.assertEqual(cfg["build_proxy_env"]["no_proxy"], "localhost,127.0.0.1,10.89.9.0/24")
        self.assertEqual((cfg["cgroup_parent"], cfg["slice"]), ("", ""), "the unit settings run without a slice")
        self.assertEqual((cfg["memory"], cfg["pids"], cfg["memory_max"], cfg["pids_max"]),
                         (1 << 30, 1024, 4 << 30, 4096))
        self.assertEqual(cfg["build_memory"], 2 << 30)
        self.assertEqual(cfg["registries"], sockguard.REGISTRIES)
        self.assertEqual(cfg["images"], [])
        self.assertTrue(cfg["log"].endswith("/gate.log"))
        self.assertEqual(json.loads(json.dumps(cfg)), cfg, "the dict is what gate.json holds")

    def test_no_proxy_address_means_no_build_proxy(self):
        """The proxy's address comes from `inspect {{json .NetworkSettings.Networks}}`; when the proxy is not on
        the session network the gate gets None, not an env with an empty host."""
        cfg = self.config(self.make())
        self.assertIsNone(cfg["build_proxy_env"])
        self.assertEqual(cfg["build_dns"], ["10.89.9.1"])

    def test_read_only_grants_and_images_are_passed_through(self):
        ro = self.mkdir("ro")
        cfg = self.config(self.make(data_ro=[ro], dev_images=["localhost/myapp:*"]), {})
        self.assertEqual(cfg["ro_roots"], [os.path.realpath(ro)])
        self.assertEqual(cfg["bind_roots"], [self.root, ro])
        self.assertEqual(cfg["images"], ["localhost/myapp:*"])


class PostCreateTables(unittest.TestCase):
    """What the gate reads back after a volume or network create, case by case."""

    def test_volume_violations(self):
        g = FakeGate()
        table = [
            ({"Labels": DEV}, []),
            ({"Labels": DEV, "Driver": "local"}, []),
            ({"Labels": {}}, ["missing the dev labels"]),
            ({"Labels": {sockguard.DEV_OWNER: "other", "agentbox.role": "dev"}}, ["missing the dev labels"]),
            ({"Labels": {sockguard.DEV_OWNER: CFG["session"]}}, ["missing the dev labels"]),
            ({"Labels": DEV, "Driver": "image"}, ["driver image"]),
            ({"Labels": DEV, "Options": {"device": "/dev/sda1"}}, ["options {'device': '/dev/sda1'}"]),
            ({"Driver": "nfs", "Options": {"o": "addr=10.0.0.1"}},
             ["missing the dev labels", "driver nfs", "options {'o': 'addr=10.0.0.1'}"]),
            ({}, ["missing the dev labels"]),
        ]
        for doc, want in table:
            with self.subTest(doc=doc):
                self.assertEqual(g.volume_violations(doc), want)

    def test_net_violations(self):
        g = FakeGate()
        ok = {"labels": DEV, "driver": "bridge", "internal": True}
        table = [
            (ok, []),
            (dict(ok, options={"isolate": "true"}), []),
            (dict(ok, options={"isolate": "strict"}), []),
            ({"Labels": DEV, "Driver": "bridge", "Internal": True}, []),           # compat capitalisation
            (dict(ok, labels={}), ["missing the dev labels"]),
            (dict(ok, driver="macvlan"), ["driver macvlan"]),
            (dict(ok, internal=False), ["not internal"]),
            (dict(ok, ipv6_enabled=True), ["IPv6"]),
            (dict(ok, options={"isolate": "false"}), ["option isolate=false"]),
            (dict(ok, options={"mtu": "9000"}), ["option mtu=9000"]),
            ({}, ["missing the dev labels", "driver None", "not internal"]),
        ]
        for doc, want in table:
            with self.subTest(doc=doc):
                self.assertEqual(g.net_violations(doc), want)


class CompatBindsReadOnly(TempCase):
    """A compat-API `Binds` entry on a read-only grant is forwarded read-only, whatever the client asked
    (audit H3); a writable grant keeps what the client wrote."""

    def setUp(self):
        super().setUp()
        self.proj, self.ro = self.mkdir("proj"), self.mkdir("ro")
        self.g = FakeGate()
        self.g.cfg["bind_roots"] = [self.proj, self.ro]
        self.g.cfg["ro_roots"] = [self.ro]

    def binds(self, *binds):
        spec = self.g.rewrite_compat_container({"Image": "x", "HostConfig": {"Binds": list(binds)}})
        return spec["HostConfig"]["Binds"]

    def test_ro_grant_binds_are_rewritten_ro(self):
        self.assertEqual(self.binds(f"{self.ro}:/m"), [f"{self.ro}:/m:ro"])
        self.assertEqual(self.binds(f"{self.ro}:/m:rw"), [f"{self.ro}:/m:ro"])
        self.assertEqual(self.binds(f"{self.ro}:/m:rw,z"), [f"{self.ro}:/m:z,ro"])

    def test_rw_grant_binds_are_left_alone(self):
        self.assertEqual(self.binds(f"{self.proj}:/p", f"{self.proj}:/q:rw", f"{self.proj}:/r:ro"),
                         [f"{self.proj}:/p", f"{self.proj}:/q:rw", f"{self.proj}:/r:ro"])

    def test_the_source_is_forwarded_by_its_real_path(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.ro, link)
        self.assertEqual(self.binds(f"{link}:/m"), [f"{self.ro}:/m:ro"])

    def test_an_explicit_ro_on_a_ro_grant_is_not_doubled(self):
        """`ro` once. Today the rewrite appends `ro` without dropping the client's own, so `:ro` on a read-only
        grant goes upstream as `ro,ro`, which podman refuses as a duplicate mount option: the bind fails closed,
        but a correct request is refused. Found by this test, 16 Nov; the fix is in gate.py (not this
        package's file, see the review handoff). Drop the decorator with the fix."""
        self.assertEqual(self.binds(f"{self.ro}:/m:ro"), [f"{self.ro}:/m:ro"])


class SliceCheck(unittest.TestCase):
    """cgroup.check: refuse without an installed, capped slice; warn when it is nearly full."""

    def settings(self, parent="agentbox.slice", fraction=0.75):
        return SimpleNamespace(cgroup_parent=parent, slice_warn_fraction=fraction)

    def check(self, props, use, settings=None):
        warned = []
        with mock.patch.object(cgroup, "unit_props", return_value=props) as up, \
                mock.patch.object(cgroup, "usage", return_value=use) as us:
            cgroup.check(settings or self.settings(), warned.append)
        return warned, up, us

    def test_no_slice_configured_asks_nothing(self):
        warned, up, us = self.check({}, (0, 0, 0, 0), settings=self.settings(parent=""))
        self.assertEqual(warned, [])
        self.assertFalse(up.called)
        self.assertFalse(us.called)

    def test_refuses_an_uninstalled_or_uncapped_slice(self):
        for props in ({}, {"FragmentPath": "", "MemoryMax": "4294967296"},
                      {"FragmentPath": "/u/agentbox.slice", "MemoryMax": "infinity"},
                      {"FragmentPath": "/u/agentbox.slice", "MemoryMax": ""},
                      {"FragmentPath": "/u/agentbox.slice"}):
            with self.subTest(props=props), self.assertRaises(AgentboxError) as cm:
                self.check(props, (0, 0, 0, 0))
            self.assertIn("make install-slice", str(cm.exception))
            self.assertIn("agentbox.slice", str(cm.exception))

    def test_an_installed_capped_slice_with_room_is_quiet(self):
        props = {"FragmentPath": "/u/agentbox.slice", "MemoryMax": str(4 << 30)}
        warned, _up, us = self.check(props, (1 << 30, 4 << 30, 100, 1000))
        self.assertEqual(warned, [])
        us.assert_called_once_with("agentbox.slice")

    def test_warns_when_memory_or_tasks_pass_the_fraction(self):
        props = {"FragmentPath": "/u/agentbox.slice", "MemoryMax": str(4 << 30)}
        warned, _up, _us = self.check(props, (3200 << 20, 4 << 30, 100, 1000))
        self.assertEqual(warned, ["agentbox: agentbox.slice is using 3200 MB of its 4096 MB cap; "
                                  "a new session may be OOM-killed"])
        warned, _up, _us = self.check(props, (1 << 30, 4 << 30, 800, 1000))
        self.assertEqual(warned, ["agentbox: agentbox.slice has 800 of 1000 tasks"])
        warned, _up, _us = self.check(props, (3200 << 20, 4 << 30, 800, 1000))
        self.assertEqual(len(warned), 2)
        # exactly at the fraction is not over it; a lower fraction moves the line
        warned, _up, _us = self.check(props, (3 << 30, 4 << 30, 750, 1000))
        self.assertEqual(warned, [])
        warned, _up, _us = self.check(props, (3 << 30, 4 << 30, 750, 1000), settings=self.settings(fraction=0.5))
        self.assertEqual(len(warned), 2)

    def test_unreadable_usage_does_not_warn_or_crash(self):
        props = {"FragmentPath": "/u/agentbox.slice", "MemoryMax": str(4 << 30)}
        warned, _up, _us = self.check(props, (None, None, None, None))
        self.assertEqual(warned, [])
