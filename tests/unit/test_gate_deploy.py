"""Deploy step 2: the gate's deploy mode (design.md, deploy mode). One allowed and one refused case per
row of the table, and a check that the dev tier decides the same with `mode` absent and `mode="dev"`.
Upstream lookups are faked (test_review_step5.Fake's object store)."""
import json
import os
import shutil
import socket
import tempfile
import unittest

from agentbox import gate
from tests.unit.test_gate import CFG, DEV
from tests.unit.test_review_step5 import Fake, container, decide

DEPLOY = {"agentbox.role": "deploy", "agentbox.deploy": "shop"}
COMPOSE = {"io.podman.compose.project": "shop", "com.docker.compose.project": "shop"}
LAN = "10.0.0.103"


def dcontainer(cid, labels=DEPLOY, **extra):
    doc = {"Id": cid, "Config": {"Labels": labels}, "Mounts": [],
           "NetworkSettings": {"Networks": {"shop_default": {}}},
           "HostConfig": {"PortBindings": {}, "CgroupParent": "user.slice", "OomScoreAdj": 0,
                          "RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "bridge",
                          "Memory": 1 << 30, "MemorySwap": 1 << 30, "PidsLimit": 1024}}
    doc.update(extra)
    return doc


def net(name, labels=DEPLOY, nid=None):
    return {"name": name, "id": nid or f"id-{name}", "labels": labels, "driver": "bridge"}


class DFake(Fake):
    """test_review_step5.Fake's podman, with a deploy-mode config."""

    def __init__(self, objects=None, **over):
        cfg = {k: v for k, v in CFG.items() if k not in ("network", "cgroup_parent", "slice", "proxy_env",
                                                       "subnets", "bind_roots")}
        cfg.update({"mode": "deploy", "log": "/dev/null", "slice": "", "compose_project": "shop",
                    "labels": {"agentbox": "1", "agentbox.deploy": "shop", "agentbox.deploy-run": "r1"},
                    "lan_addr": LAN, "ports": [8090, 8091], "stage": over.pop("stage"),
                    "deploy_binds": over.pop("deploy_binds", [])})
        cfg.update(over)
        gate.Gate.__init__(self, cfg)
        self.objects = {"networks/shop_default": net("shop_default"),
                        "networks/shop_back": net("shop_back"),
                        "networks/podman": net("podman", labels={}),
                        "networks/other_default": net("other_default",
                                                      labels={"agentbox.role": "deploy", "agentbox.deploy": "other"}),
                        **(objects or {})}
        self.deleted, self.created = [], []


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="abt-deploy-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stage = os.path.join(self.tmp, "stage")
        self.media = os.path.join(self.tmp, "media")
        self.outside = os.path.join(self.tmp, "outside")
        for d in (self.stage + "/config", self.media + "/films", self.outside):
            os.makedirs(d)
        os.symlink(self.outside, os.path.join(self.stage, "escape"))

    def g(self, objects=None, **over):
        return DFake(objects, stage=self.stage, deploy_binds=over.pop("deploy_binds", [self.media]), **over)

    def create(self, g, body):
        _t, out, _k = decide(g, "POST", "/v5/libpod/containers/create", body)
        return json.loads(out)

    def body(self, **kw):
        out = {"image": "x", "name": "shop_web_1", "labels": dict(COMPOSE),
               "Networks": {"shop_default": {"aliases": ["web"]}}}
        out.update(kw)
        return out

    def refused(self, g, body, path="/v5/libpod/containers/create"):
        with self.assertRaises(gate.Deny):
            decide(g, "POST", path, body)


class Config(Base):
    def test_mode_must_be_dev_or_deploy_and_deploy_needs_its_settings(self):
        with self.assertRaises(ValueError):
            gate.Gate(dict(CFG, mode="live"))
        for drop in ("lan_addr", "compose_project", "ports"):
            with self.subTest(drop=drop), self.assertRaises((ValueError, KeyError)):
                DFake(stage=self.stage, **{drop: None})
        with self.assertRaises(ValueError):
            DFake(stage="/")
        with self.assertRaises(ValueError):
            DFake(stage=self.stage, lan_addr="0.0.0.0")


class ComposeProjectAndLabels(Base):
    def test_allowed_stamped_with_the_deploy_labels_client_agentbox_labels_dropped(self):
        spec = self.create(self.g(), self.body(labels={**COMPOSE, "agentbox.role": "dev",
                                                      gate.DEV_OWNER: "abt-s-1", "app": "1"}))
        lab = spec["labels"]
        self.assertEqual((lab["agentbox.role"], lab["agentbox.deploy"], lab["app"]), ("deploy", "shop", "1"))
        self.assertNotIn(gate.DEV_OWNER, lab)
        self.assertEqual(lab["io.podman.compose.project"], "shop")

    def test_refused_another_compose_project_or_none(self):
        g = self.g()
        self.refused(g, self.body(labels={"io.podman.compose.project": "other"}))
        self.refused(g, self.body(labels={"io.podman.compose.project": "shop", "com.docker.compose.project": "x"}))
        self.refused(g, self.body(labels={}))
        self.refused(g, {"name": "n", "labels": {"io.podman.compose.project": "other"}},
                     "/v5/libpod/networks/create")
        self.refused(g, {"Name": "shop_v", "Label": {"io.podman.compose.project": "other"}},
                     "/v5/libpod/volumes/create")


class Networks(Base):
    def test_allowed_own_network_created_with_egress_and_joined(self):
        g = self.g()
        _t, out, _k = decide(g, "POST", "/v5/libpod/networks/create",
                             {"name": "shop_default", "labels": dict(COMPOSE)})
        spec = json.loads(out)
        self.assertIs(spec["internal"], False, "not forced internal: the live stack has normal egress")
        self.assertEqual((spec["driver"], spec["labels"]["agentbox.role"]), ("bridge", "deploy"))
        self.assertIs(json.loads(decide(g, "POST", "/v5/libpod/networks/create",
                                        {"name": "shop_int", "internal": True})[1])["internal"], True)
        spec = self.create(g, self.body(Networks={"shop_default": {"aliases": ["web"]}, "shop_back": {}}))
        self.assertEqual(sorted(spec["Networks"]), ["shop_back", "shop_default"])
        self.assertEqual(spec["netns"], {"nsmode": "bridge"})
        self.assertEqual(self.create(g, self.body(netns={"nsmode": "none"}, Networks={}))["netns"],
                         {"nsmode": "none"})
        self.assertEqual(g.network_violations(dcontainer("c")), [])

    def test_refused_host_namespaces_foreign_networks_and_drivers(self):
        g = self.g()
        for ns in ({"nsmode": "host"}, {"nsmode": "ns", "value": "/proc/1/ns/net"}, {"nsmode": "path", "value": "/x"},
                   {"nsmode": "container", "value": "abc"}, {"nsmode": "pasta"}, {"nsmode": "slirp4netns"},
                   {"nsmode": "private"}):
            with self.subTest(ns=ns):
                self.refused(g, self.body(netns=ns))
        for nets in ({}, {"podman": {}}, {"other_default": {}}, {"missing": {}},
                     {"shop_default": {"static_ips": ["10.1.1.1"]}}):
            with self.subTest(nets=nets):
                self.refused(g, self.body(Networks=nets))
        for spec in ({"name": "n", "driver": "macvlan"}, {"name": "n", "driver": "ipvlan"},
                     {"name": "n", "options": {"isolate": "true"}}, {"name": "n", "subnets": [{"subnet": "10.0.0.0/24"}]},
                     {"name": "n", "network_interface": "eth0"}):
            with self.subTest(spec=spec):
                self.refused(g, spec, "/v5/libpod/networks/create")
        self.refused(g, {"Name": "n", "Driver": "macvlan"}, "/v5/networks/create")
        with self.assertRaises(gate.Deny):          # connect stays refused
            decide(g, "POST", "/v5/libpod/networks/podman/connect", {})
        for mode, nets in (("host", {}), ("pasta", {"shop_default": {}}), ("bridge", {"podman": {}}),
                           ("container:abc", {"shop_default": {}}), ("bridge", {})):
            doc = dcontainer("c")
            doc["HostConfig"]["NetworkMode"] = mode
            doc["NetworkSettings"]["Networks"] = nets
            with self.subTest(mode=mode, nets=nets):
                self.assertTrue(g.network_violations(doc))


class Ports(Base):
    def test_allowed_on_the_lan_address_and_listed_ports(self):
        g = self.g()
        spec = self.create(g, self.body(portmappings=[{"host_ip": "", "host_port": 8090, "container_port": 80},
                                                      {"host_ip": LAN, "host_port": 8091, "container_port": 443,
                                                       "protocol": "tcp", "range": 1}]))
        self.assertEqual([m["host_ip"] for m in spec["portmappings"]], [LAN, LAN])
        spec = g.rewrite_compat_container({"Image": "x", "Labels": dict(COMPOSE), "HostConfig": {
            "NetworkMode": "shop_default", "PortBindings": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8090"}]}}})
        self.assertEqual(spec["HostConfig"]["PortBindings"], {"80/tcp": [{"HostIp": LAN, "HostPort": "8090"}]})
        doc = dcontainer("c")
        doc["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostIp": LAN, "HostPort": "8090"}]}
        self.assertEqual(g.container_violations(doc), [])

    def test_refused_other_address_unlisted_random_ranges_and_publish_all(self):
        g = self.g()
        for pm in ({"host_ip": "0.0.0.0", "host_port": 22, "container_port": 22},
                   {"host_ip": "127.0.0.1", "host_port": 8090, "container_port": 80},
                   {"host_ip": "10.0.0.5", "host_port": 8090, "container_port": 80},
                   {"host_ip": "::", "host_port": 8090, "container_port": 80},
                   {"host_port": 0, "container_port": 80}, {"container_port": 80},
                   {"host_port": 8090, "container_port": 80, "range": 2}):
            with self.subTest(pm=pm):
                self.refused(g, self.body(portmappings=[pm]))
        self.refused(g, self.body(publish_image_ports=True))
        for hc in ({"PublishAllPorts": True}, {"PortBindings": {"80/tcp": [{"HostPort": "8090-8091"}]}},
                   {"PortBindings": {"80/tcp": [{"HostPort": ""}]}},
                   {"PortBindings": {"80/tcp": [{"HostIp": "10.0.0.5", "HostPort": "8090"}]}}):
            with self.subTest(hc=hc), self.assertRaises(gate.Deny):
                g.rewrite_compat_container({"Image": "x", "Labels": dict(COMPOSE),
                                            "HostConfig": dict(hc, NetworkMode="shop_default")})
        self.refused(g, {"name": "p", "no_infra": True, "portmappings": [{"host_port": 8090}]},
                     "/v5/libpod/pods/create")
        for pb in ({"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8090"}]},
                   {"80/tcp": [{"HostIp": LAN, "HostPort": "9999"}]}):
            doc = dcontainer("c")
            doc["HostConfig"]["PortBindings"] = pb
            self.assertTrue(any("published port" in v for v in g.container_violations(doc)))
        doc = dcontainer("c")
        doc["HostConfig"]["PublishAllPorts"] = True
        self.assertTrue(g.container_violations(doc))


class Binds(Base):
    def mount(self, src):
        return self.body(mounts=[{"type": "bind", "source": src, "destination": "/d", "options": ["ro"]}])

    def test_allowed_inside_the_copy_or_a_deploy_bind(self):
        g = self.g()
        for src in (self.stage, self.stage + "/config", self.media + "/films"):
            with self.subTest(src=src):
                self.assertEqual(self.create(g, self.mount(src))["mounts"][0]["source"], src)
        spec = g.rewrite_compat_container({"Image": "x", "Labels": dict(COMPOSE), "HostConfig": {
            "NetworkMode": "shop_default", "Binds": [f"{self.stage}/config:/c:ro"]}})
        self.assertEqual(spec["HostConfig"]["Binds"], [f"{self.stage}/config:/c:ro"])
        doc = dcontainer("c", Mounts=[{"Type": "bind", "Source": self.media + "/films", "Destination": "/m"}])
        self.assertEqual(g.container_violations(doc), [])

    def test_refused_outside_symlink_escape_system_paths_and_sockets(self):
        sock_dir = os.path.join(self.stage, "run")
        os.makedirs(sock_dir)
        s = socket.socket(socket.AF_UNIX)
        s.bind(os.path.join(sock_dir, "x.sock"))
        self.addCleanup(s.close)
        g = self.g(raw=os.path.join(self.stage, "config", "podman.sock"))
        for src in (self.outside, self.stage + "/escape", self.stage + "/../outside", "/proc", "/sys/fs",
                    "/dev", "/run/user", "/", "/etc", os.path.join(sock_dir, "x.sock"), self.stage + "/config",
                    self.stage):
            with self.subTest(src=src):
                self.refused(g, self.mount(src))
        with self.assertRaises(gate.Deny):
            g.rewrite_compat_container({"Image": "x", "Labels": dict(COMPOSE), "HostConfig": {
                "NetworkMode": "shop_default", "Binds": [f"{self.outside}:/o"]}})
        with self.assertRaises(gate.Deny):          # a deploy bind that is a system path is still refused
            self.create(self.g(deploy_binds=["/dev"]), self.mount("/dev/null"))
        doc = dcontainer("c", Mounts=[{"Type": "bind", "Source": self.outside, "Destination": "/m"}])
        self.assertTrue(any("outside the deploy copy" in v for v in g.container_violations(doc)))


class NamedVolumes(Base):
    def test_allowed_the_stacks_own_created_or_reused(self):
        g = self.g({"volumes/shop_old": {"Name": "shop_old", "Labels": DEPLOY}})
        spec = self.create(g, self.body(volumes=[{"Name": "shop_data", "Dest": "/data"},
                                                 {"Name": "shop_old", "Dest": "/old"}]))
        self.assertEqual([v["Name"] for v in spec["volumes"]], ["shop_data", "shop_old"])
        made = g.objects["volumes/shop_data"]["Labels"]
        self.assertEqual((made["agentbox.role"], made["agentbox.deploy"]), ("deploy", "shop"))
        _t, out, _k = decide(g, "POST", "/v5/libpod/volumes/create", {"Name": "shop_db", "Label": dict(COMPOSE)})
        self.assertEqual(json.loads(out)["Label"]["agentbox.deploy"], "shop")
        doc = dcontainer("c", Mounts=[{"Type": "volume", "Name": "shop_old", "Destination": "/o"}])
        self.assertEqual(g.container_violations(doc), [])

    def test_refused_any_other_volume(self):
        g = self.g({"volumes/shop_hand": {"Name": "shop_hand", "Labels": {}},
                    "volumes/media": {"Name": "media", "Labels": DEPLOY},
                    "volumes/shop_dev": {"Name": "shop_dev", "Labels": DEV}})
        for name in ("other_data", "data", "media", "shop_hand", "shop_dev"):
            with self.subTest(name=name):
                self.refused(g, self.body(volumes=[{"Name": name, "Dest": "/d"}]))
        for body in ({"Name": "data"}, {}, {"Name": "other_x"}):
            with self.subTest(body=body):
                self.refused(g, body, "/v5/libpod/volumes/create")
        doc = dcontainer("c", Mounts=[{"Type": "volume", "Name": "media", "Destination": "/m"}])
        self.assertTrue(g.container_violations(doc))


class Restart(Base):
    def test_forced_to_unless_stopped(self):
        g = self.g()
        for policy in (None, "always", "no", "on-failure"):
            body = self.body(restart_policy=policy, restart_tries=3) if policy else self.body()
            with self.subTest(policy=policy):
                spec = self.create(g, body)
                self.assertEqual(spec["restart_policy"], "unless-stopped")
                self.assertNotIn("restart_tries", spec)
        spec = g.rewrite_compat_container({"Image": "x", "Labels": dict(COMPOSE), "HostConfig": {
            "NetworkMode": "shop_default", "RestartPolicy": {"Name": "always", "MaximumRetryCount": 0}}})
        self.assertEqual(spec["HostConfig"]["RestartPolicy"], {"Name": "unless-stopped"})

    def test_a_stored_container_without_it_is_refused(self):
        g = self.g()
        for name in ("no", "always", ""):
            doc = dcontainer("c")
            doc["HostConfig"]["RestartPolicy"] = {"Name": name}
            with self.subTest(name=name):
                self.assertTrue(any("restart policy" in v for v in g.container_violations(doc)))


class Privilege(Base):
    def test_allowed_no_new_privileges_and_dropped_caps(self):
        spec = self.create(self.g(), self.body(cap_drop=["ALL"], no_new_privileges=True))
        self.assertEqual(spec["cap_drop"], ["ALL"])

    def test_refused_exactly_as_in_the_dev_tier(self):
        g = self.g()
        for bad in ({"privileged": True}, {"cap_add": ["NET_ADMIN"]}, {"devices": [{"path": "/dev/kvm"}]},
                    {"pidns": {"nsmode": "host"}}, {"ipcns": {"nsmode": "host"}}, {"utsns": {"nsmode": "host"}},
                    {"userns": {"nsmode": "host"}}, {"cgroupns": {"nsmode": "host"}},
                    {"selinux_opts": ["disable"]}, {"apparmor_profile": "unconfined"},
                    {"seccomp_profile_path": "/x"}, {"cgroup_parent": "/"}, {"sysctl": {"net.ipv4.ip_forward": "1"}},
                    {"oci_runtime": "/x"}, {"rootfs": "/"}):
            with self.subTest(bad=bad):
                self.refused(g, self.body(**bad))
        self.refused(g, {"name": "p", "no_infra": True, "security_opt": ["label=disable"]}, "/v5/libpod/pods/create")
        for change in ({"Privileged": True}, {"CapAdd": ["CAP_SYS_ADMIN"]}, {"Devices": [{"PathOnHost": "/dev/x"}]},
                       {"PidMode": "host"}, {"SecurityOpt": ["seccomp=unconfined"]}):
            doc = dcontainer("c")
            doc["HostConfig"].update(change)
            with self.subTest(change=change):
                self.assertTrue(g.container_violations(doc))


class Images(Base):
    def test_allowed_public_images_and_builds_without_proxy_or_slice(self):
        g = self.g()
        g.rewrite_libpod_container(self.body(image="docker.io/library/nginx:1"))
        q = dict(__import__("urllib.parse", fromlist=["x"]).parse_qsl(
            g.rewrite_build("/v5/libpod/build?t=localhost/shop_web&buildargs=%7B%22A%22%3A%221%22%7D").split("?", 1)[1]))
        self.assertEqual(json.loads(q["buildargs"]), {"A": "1"}, "no proxy variables in a deploy build")
        self.assertNotIn("cgroupparent", q)
        self.assertEqual(json.loads(q["labels"])["agentbox.deploy"], "shop")

    def test_refused_foreign_localhost_images_and_reserved_tags(self):
        other = {"Id": "sha256:" + "a" * 64, "RepoTags": ["localhost/otherproj:latest"]}
        g = self.g({"images/localhost/otherproj": other, "images/localhost/otherproj:latest": other})
        self.refused(g, self.body(image="localhost/otherproj"))
        for t in ("localhost/agentbox-base", "docker.io/x/y", "localhost/otherproj"):
            with self.subTest(t=t), self.assertRaises(gate.Deny):
                g.rewrite_build(f"/v5/libpod/build?t={t}")

    def test_a_build_is_recorded_by_its_deploy_labels(self):
        rec = os.path.join(self.tmp, "built.json")
        now = __import__("time").time()
        aa, bb = "a" * 64, "b" * 64
        g = self.g({f"images/{aa}": {"Id": "sha256:" + aa, "Labels": {**DEPLOY, "agentbox": "1"}, "Created": now},
                    f"images/{bb}": {"Id": "sha256:" + bb, "Labels": {"agentbox.session": "abt-s-1"},
                                     "Created": now}}, built_record=rec)
        self.assertTrue(g.record_build(aa, now))
        self.assertFalse(g.record_build(bb, now), "a session's image is not this deploy's build")


class Removal(Base):
    def objects(self):
        return {"containers/old": dcontainer("old"), "containers/dev": container("dev"),
                "containers/hand": dcontainer("hand", labels={"io.podman.compose.project": "shop"}),
                "containers/other": dcontainer("other", labels={"agentbox.role": "deploy",
                                                                "agentbox.deploy": "other"}),
                "pods/pod_shop": {"Id": "pod_shop", "Labels": DEPLOY, "Containers": []},
                "volumes/shop_v": {"Name": "shop_v", "Labels": DEPLOY}}

    def test_allowed_the_stacks_own_objects_even_from_an_earlier_deploy(self):
        g = self.g(self.objects())
        for method, path in (("DELETE", "/v5/libpod/containers/old"), ("POST", "/v5/libpod/containers/old/stop"),
                             ("GET", "/v5/libpod/containers/old/json"), ("DELETE", "/v5/libpod/pods/pod_shop"),
                             ("DELETE", "/v5/libpod/volumes/shop_v"), ("DELETE", "/v5/libpod/networks/shop_default"),
                             ("GET", "/v5/libpod/networks/shop_default/exists")):
            with self.subTest(path=path):
                decide(g, method, path)
        # a start re-checks the earlier deploy's container against today's rules first
        _t, _b, kind = decide(g, "POST", "/v5/libpod/containers/old/start")
        self.assertEqual(kind, "start")
        g.release_held()

    def test_refused_anything_else_and_what_the_gate_removed(self):
        g = self.g(self.objects())
        for ref in ("dev", "hand", "other", "missing"):
            for method, path in (("DELETE", f"/v5/libpod/containers/{ref}"),
                                 ("POST", f"/v5/libpod/containers/{ref}/stop"),
                                 ("GET", f"/v5/libpod/containers/{ref}/json")):
                with self.subTest(ref=ref, path=path), self.assertRaises(gate.NotFound):
                    decide(g, method, path)
        for path in ("/v5/libpod/networks/podman", "/v5/libpod/networks/other_default"):
            with self.subTest(path=path), self.assertRaises(gate.NotFound):
                decide(g, "DELETE", path)
        g.remove("containers", "old", "refused")
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/libpod/containers/old/start")
        g2 = self.g({**self.objects(), "containers/old": dcontainer("old", HostConfig={
            **dcontainer("old")["HostConfig"], "Privileged": True})})
        with self.assertRaises(gate.Deny):           # an earlier deploy's container that fails today's rules
            decide(g2, "POST", "/v5/libpod/containers/old/start")
        g2.release_held()


class CgroupLimitsAndProxy(Base):
    def test_host_default_cgroup_and_the_gates_limits(self):
        g = self.g()
        spec = self.create(g, self.body())
        self.assertNotIn("cgroup_parent", spec)
        self.assertEqual((spec["resource_limits"]["memory"]["limit"], spec["resource_limits"]["pids"]["limit"],
                          spec["oom_score_adj"]), (1 << 30, 1024, 0), "a live service keeps the host default OOM score")
        self.assertNotIn("cgroup_parent", json.loads(decide(g, "POST", "/v5/libpod/pods/create",
                                                            {"name": "pod_shop", "no_infra": True})[1]))
        self.assertEqual(g.container_violations(dcontainer("c")), [], "any cgroup parent podman picks")

    def test_refused_a_client_cgroup_parent_raised_limits_and_infra_pods(self):
        g = self.g()
        self.refused(g, self.body(cgroup_parent="agentbox.slice"))
        self.refused(g, self.body(resource_limits={"memory": {"limit": 8 << 30}}))
        self.refused(g, {"name": "pod_shop"}, "/v5/libpod/pods/create")

    def test_no_proxy_variables(self):
        spec = self.create(self.g(), self.body(env={"A": "1"}))
        self.assertEqual(spec["env"], {"A": "1"})


class DevModeUnchanged(unittest.TestCase):
    """The dev tier decides exactly the same with `mode` absent and `mode="dev"`."""

    CASES = [
        ("POST", "/v5/libpod/containers/create", {"image": "x", "name": "w", "restart_policy": "always",
                                                  "Networks": {"proj_default": {"aliases": ["web"]}}}),
        ("POST", "/v5/libpod/containers/create", {"image": "x", "portmappings": [{"host_port": 8090}]}),
        ("POST", "/v5/libpod/containers/create", {"image": "x", "netns": {"nsmode": "host"}}),
        ("POST", "/v5/libpod/containers/create", {"image": "x", "mounts": [
            {"type": "bind", "source": "/etc", "destination": "/e"}]}),
        ("POST", "/v5/libpod/containers/create", {"image": "x", "volumes": [{"Name": "data", "Dest": "/d"}]}),
        ("POST", "/v5/libpod/containers/create", {"image": "x", "privileged": True}),
        ("POST", "/v5/containers/create", {"Image": "x", "HostConfig": {"RestartPolicy": {"Name": "always"}}}),
        ("POST", "/v5/containers/create", {"Image": "x", "HostConfig": {"PortBindings": {"80/tcp": [{}]}}}),
        ("POST", "/v5/libpod/pods/create", {"name": "p", "no_infra": True}),
        ("POST", "/v5/libpod/pods/create", {"name": "p"}),
        ("POST", "/v5/libpod/networks/create", {"name": "n"}),
        ("POST", "/v5/networks/create", {"Name": "n"}),
        ("POST", "/v5/libpod/networks/create", {"name": "n", "driver": "macvlan"}),
        ("POST", "/v5/libpod/volumes/create", {"Name": "v"}),
        ("POST", "/v5/volumes/create", {"Name": "v"}),
        ("DELETE", "/v5/libpod/containers/web", None),
        ("DELETE", "/v5/libpod/containers/live", None),
        ("POST", "/v5/libpod/containers/web/stop", None),
        ("GET", "/v5/libpod/networks/abt-s-1-int/json", None),
        ("DELETE", "/v5/libpod/networks/abt-s-1-int", None),
        ("POST", "/v5/libpod/images/pull?reference=busybox", None),
        ("POST", "/v5/libpod/images/pull?reference=registry.example.com/x", None),
        ("POST", "/v5/libpod/build?t=localhost/x&buildargs=%7B%7D", None),
        ("POST", "/v5/libpod/build?t=localhost/agentbox-x", None),
        ("GET", "/v5/libpod/pods/json", None),
    ]
    DOCS = [container("c"), container("c", HostConfig={**container("c")["HostConfig"],
                                                       "RestartPolicy": {"Name": "always"}}),
            container("c", HostConfig={**container("c")["HostConfig"],
                                       "PortBindings": {"80/tcp": [{"HostIp": LAN, "HostPort": "8090"}]}}),
            container("c", NetworkSettings={"Networks": {"podman": {}}})]

    def run_all(self, mode):
        class G(Fake):
            def __init__(self, objects=None):
                super().__init__(objects)
                if mode is not None:
                    self.cfg["mode"] = mode
                    gate.Gate.__init__(self, self.cfg)
                    self.objects = objects or {}
                    self.deleted, self.created = [], []
        out = []
        objects = {"containers/web": container("web"), "containers/live": container("live", labels={"a": "b"})}
        for method, path, body in self.CASES:
            g = G(dict(objects))
            g.verified.add("web")
            try:
                t, b, k = decide(g, method, path, body)
                out.append(("ok", t, b if isinstance(b, bytes) else b"", k))
            except gate.LocalReply as e:
                out.append(("local", e.body))
            except Exception as e:                  # noqa: BLE001 - the refusal itself is what is compared
                out.append((type(e).__name__, str(e)))
            out.append(sorted(g.created))
        g = G({"pods/p": {"Id": "p", "Labels": DEV}})
        out += [g.container_violations(d) for d in self.DOCS]
        out.append(g.pod_violations({"Labels": DEV, "CgroupParent": "agentbox.slice"}))
        out.append(g.net_violations({"labels": DEV, "driver": "bridge"}))
        return out

    def test_mode_absent_and_dev_decide_the_same(self):
        a, b = self.run_all(None), self.run_all("dev")
        self.assertEqual(a, b)
        self.assertFalse(Fake().deploy)
        self.assertTrue(any(r[0] == "ok" for r in a if isinstance(r, tuple)))
        self.assertTrue(any(r[0] == "Deny" for r in a if isinstance(r, tuple)))


if __name__ == "__main__":
    unittest.main()


class Decided17Nov(Base):
    """Tim's answers to the step-2 "needs a decision" list (decisions.md, 17 Nov)."""

    def test_a_live_service_keeps_the_host_default_oom_score_and_sessions_keep_1000(self):
        self.assertEqual(self.create(self.g(), self.body())["oom_score_adj"], 0)
        self.assertEqual(gate.Gate(dict(Fake().cfg)).oom_score, 1000)

    def test_the_timezone_may_be_bound_read_only_and_only_read_only(self):
        g = self.g()
        for src in ("/etc/localtime", "/etc/timezone", "/usr/share/zoneinfo"):
            if not os.path.exists(src):
                continue
            real = os.path.realpath(src)
            with self.subTest(src=src):
                self.assertIsNone(g.deploy_bind_refusal(real, rw=False))
                self.assertIn("read-only", g.deploy_bind_refusal(real, rw=True))
                doc = dcontainer("c", Mounts=[{"Type": "bind", "Source": src, "Destination": "/etc/localtime",
                                               "RW": True}])
                self.assertTrue(g.container_violations(doc))
        self.refused(g, self.body(mounts=[{"type": "bind", "source": "/etc/passwd", "destination": "/p",
                                            "options": ["ro"]}]))
