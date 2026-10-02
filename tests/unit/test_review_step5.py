"""Step 5 audit (docs/reviews/2026-10-01-step-5{A1,A4,B,C}.md): one test per gate finding, written to fail
before the fix. Upstream lookups are faked."""
import json
import os
import threading
import unittest

from agentbox import gate
from tests.unit.test_gate import CFG, DEV

POD_DEV = {gate.DEV_OWNER: "abt-s-1", "agentbox.role": "dev"}


class Fake(gate.Gate):
    """A gate whose podman lookups come from dicts: containers/pods/volumes/images by name or id."""

    def __init__(self, objects=None):
        cfg = dict(CFG)
        cfg["labels"] = {**CFG["labels"], "agentbox.project": "proj"}
        cfg["log"] = "/dev/null"
        cfg["infra_pods"] = cfg["compat_creates"] = True     # the checks behind the two refusals stay tested
        super().__init__(cfg)
        self.objects = objects or {}
        self.deleted, self.created = [], []

    def raw_json(self, path):
        for key, doc in self.objects.items():
            if path.split("?")[0].endswith("/" + key + "/json") or path.split("?")[0] == key:
                return 200, doc
        if "/containers/json" in path:
            return 200, []
        return 404, None

    def raw_request(self, method, path, body=None):
        (self.deleted if method == "DELETE" else self.created).append(path)
        return 200

    def holder_ns(self):
        return "/proc/4242/ns/net", 4242, "net:[1]"

    def slice_state(self, name):
        return True, "active"

    def network_names(self):
        return set()


def container(cid, labels=DEV, **extra):
    doc = {"Id": cid, "Config": {"Labels": labels}, "Mounts": [],
           "NetworkSettings": {"Networks": {"abt-s-1-int": {}}},
           "HostConfig": {"PortBindings": {}, "CgroupParent": "agentbox.slice", "OomScoreAdj": 1000,
                          "RestartPolicy": {"Name": "no"}, "NetworkMode": "bridge", "Memory": 1 << 30,
                          "MemorySwap": 1 << 30, "PidsLimit": 1024}}
    doc.update(extra)
    return doc


def decide(g, method, path, body=None):
    return g.decide(method, path, [], json.dumps(body).encode() if body is not None else b"")


class A1PodNetworks(unittest.TestCase):
    def test_h1_pod_without_net_sharing_is_forced_to_share_net(self):
        _t, body, _k = decide(Fake(), "POST", "/v5/libpod/pods/create",
                              {"name": "p", "shared_namespaces": ["ipc", "uts"]})
        self.assertIn("net", json.loads(body)["shared_namespaces"])

    def test_h1_container_joining_an_infra_pod_loses_its_own_netns(self):
        pod = {"Id": "pid", "Labels": POD_DEV, "InfraContainerID": "infra1", "SharedNamespaces": ["net", "ipc"]}
        g = Fake({"pods/p": pod, "pods/pid": pod})
        g.verified.add("pid")
        _t, body, _k = decide(g, "POST", "/v5/libpod/containers/create",
                              {"image": "x", "pod": "p", "netns": {"nsmode": "pasta"}, "network_options": {"pasta": ["-t", "80"]}})
        spec = json.loads(body)
        self.assertNotIn("netns", spec)
        self.assertNotIn("network_options", spec)

    def test_h1_infra_pod_not_sharing_net_is_treated_as_infra_less(self):
        g = Fake({"pods/p": {"Id": "pid", "Labels": POD_DEV, "InfraContainerID": "infra1",
                             "SharedNamespaces": ["ipc"]}})
        g.verified.add("pid")
        _t, body, _k = decide(g, "POST", "/v5/libpod/containers/create", {"image": "x", "pod": "p"})
        spec = json.loads(body)
        self.assertNotIn("pod", spec)
        self.assertEqual(spec["Networks"], {"abt-s-1-int": {}})

    def test_h1_post_check_in_a_pod_requires_the_infra_network(self):
        g = Fake({"pods/p": {"Id": "pid", "Labels": POD_DEV, "InfraContainerID": "infra1",
                             "SharedNamespaces": ["net"]},
                  "containers/infra1": container("infra1", labels={},
                                                 NetworkSettings={"Networks": {"abt-s-1-int": {}}})})
        doc = container("c1", Pod="p")
        for mode in ("pasta", "slirp4netns", "bridge", "private"):
            doc["HostConfig"]["NetworkMode"] = mode
            self.assertTrue(g.container_violations(doc), mode)
        doc["HostConfig"]["NetworkMode"] = "container:infra1"
        self.assertEqual(g.container_violations(doc), [])

    def test_m1_pod_post_check(self):
        g = Fake()
        ok = {"Labels": POD_DEV, "InfraContainerID": "i", "SharedNamespaces": ["net"],
              "InfraConfig": {"Networks": ["abt-s-1-int"], "PortBindings": {}}, "CgroupParent": "agentbox.slice"}
        self.assertEqual(g.pod_violations(ok), [])
        for change in ({"InfraConfig": {"Networks": []}}, {"InfraConfig": {"Networks": ["abt-s-1-int", "podman"]}},
                       {"SharedNamespaces": ["ipc"]}, {"Labels": {gate.DEV_OWNER: "abt-s-1"}},
                       {"InfraConfig": {"Networks": ["abt-s-1-int"], "HostNetwork": True}}):
            with self.subTest(change=change):
                self.assertTrue(g.pod_violations({**ok, **change}))

    def test_l3_network_options_dropped(self):
        _t, body, _k = decide(Fake(), "POST", "/v5/libpod/containers/create",
                              {"image": "x", "network_options": {"pasta": ["-t", "8080"]}})
        self.assertNotIn("network_options", json.loads(body))

    def test_l1_network_create_rules(self):
        g = Fake()
        _t, body, _k = decide(g, "POST", "/v5/libpod/networks/create", {"name": "n", "INTERNAL": False})
        spec = json.loads(body)
        self.assertEqual([k for k in spec if k.lower() == "internal"], ["internal"])
        self.assertIs(spec["internal"], True)
        for bad in ({"name": "n", "driver": "macvlan"}, {"name": "n", "network_interface": "eth0"},
                    {"name": "n", "subnets": [{"subnet": "10.0.0.0/24"}]}, {"name": "n", "options": {"isolate": "false"}}):
            with self.subTest(bad=bad), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", "/v5/libpod/networks/create", bad)
        for bad in ({"Name": "n", "Driver": "macvlan"}, {"Name": "n", "IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}]}}):
            with self.subTest(bad=bad), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", "/v5/networks/create", bad)

    def test_l5_network_modes_and_publishing(self):
        for body in ({"netns": {"nsmode": "pasta"}}, {"netns": {"nsmode": "slirp4netns"}},
                     {"netns": {"nsmode": "path", "value": "/proc/1/ns/net"}}, {"publish_image_ports": True}):
            with self.subTest(body=body), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", "/v5/libpod/containers/create", {"image": "x", **body})
        for hc in ({"NetworkMode": "ns:/proc/1/ns/net"}, {"NetworkMode": "pasta"}, {"PublishAllPorts": True}):
            with self.subTest(hc=hc), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", "/v5/containers/create", {"Image": "x", "HostConfig": hc})
        for ns in ({"netns": {"nsmode": "host"}}, {"pidns": {"nsmode": "host"}}, {"netns": {"nsmode": "path"}}):
            with self.subTest(pod=ns), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", "/v5/libpod/pods/create", {"name": "p", **ns})

    def test_l2_network_connect_refused_in_the_gate(self):
        for path in ("/v5/libpod/networks/abt-s-1-int/connect", "/v5/networks/abt-s-1-int/connect",
                     "/v5/libpod/networks/abt-s-1-int/disconnect"):
            with self.subTest(path=path), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", path, {"container": "x"})


class A4Ownership(unittest.TestCase):
    def test_g1_a_container_not_yet_verified_cannot_be_used(self):
        g = Fake({"containers/web": container("c1"), "containers/c1": container("c1")})   # labelled, never checked
        for method, path in (("POST", "/v5/libpod/containers/web/start"), ("POST", "/v5/libpod/containers/web/exec"),
                             ("POST", "/v5/libpod/containers/web/attach"), ("POST", "/v5/containers/web/init")):
            with self.subTest(path=path), self.assertRaises(gate.Deny):
                decide(g, method, path)
        g.verified.add("c1")
        decide(g, "POST", "/v5/libpod/containers/web/start")

    def test_g1_start_during_the_check_is_refused(self):
        """A start arriving while the create's post-check is still running is refused, and only the create's
        own verification makes the container usable."""
        started, release = threading.Event(), threading.Event()
        g = Fake({"containers/c1": container("c1"), "containers/web": container("c1")})
        real = g.container_violations

        def slow(doc):
            started.set()
            release.wait(5)
            return real(doc)
        g.container_violations = slow
        raw = b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n\r\n" + json.dumps({"Id": "c1"}).encode()
        t = threading.Thread(target=g.verify_created, args=("libpod_create", raw, "web"))
        t.start()
        started.wait(5)
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/libpod/containers/web/start")
        release.set()
        t.join(5)
        decide(g, "POST", "/v5/libpod/containers/web/start")

    def test_g2_unparseable_2xx_reply_fails_closed(self):
        g = Fake({"containers/web": container("c9")})
        for raw in (b"HTTP/1.1 201 Created\r\n\r\nnot json",
                    b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 201 Created\r\n\r\n{\"Id\": \"\"}"):
            with self.subTest(raw=raw), self.assertRaises(gate.Deny):
                g.verify_created("libpod_create", raw, "web")
        self.assertTrue(any("c9" in d for d in g.deleted), g.deleted)

    def test_g2_interim_1xx_then_201_is_checked(self):
        bad = container("c2")
        bad["HostConfig"]["Privileged"] = True
        g = Fake({"containers/c2": bad})
        raw = b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 201 Created\r\n\r\n" + json.dumps({"Id": "c2"}).encode()
        with self.assertRaises(gate.Deny):
            g.verify_created("libpod_create", raw, "c2")
        self.assertTrue(any("c2" in d for d in g.deleted))
        self.assertNotIn("c2", g.verified)

    def test_g3_inspect_failure_removes_and_never_verifies(self):
        g = Fake()

        def boom(path):
            raise OSError("timed out")
        g.raw_json = boom
        raw = b"HTTP/1.1 201 Created\r\n\r\n" + json.dumps({"Id": "c3"}).encode()
        with self.assertRaises(gate.Deny):
            g.verify_created("libpod_create", raw, "web")
        self.assertTrue(any("c3" in d for d in g.deleted))
        self.assertNotIn("c3", g.verified)

    def test_g4_named_volumes_must_be_the_sessions(self):
        foreign = {"Name": "media_config", "Labels": {"io.podman.compose.project": "media"}}
        g = Fake({"volumes/media_config": foreign})
        for body in ({"image": "x", "volumes": [{"Name": "media_config", "Dest": "/c"}]},
                     {"image": "x", "mounts": [{"type": "volume", "source": "media_config", "destination": "/c"}]},
                     {"image": "x", "volumes_from": ["web"]}):
            with self.subTest(body=body), self.assertRaises(gate.Deny):
                decide(g, "POST", "/v5/libpod/containers/create", body)
        for hc in ({"Binds": ["media_config:/c"]}, {"Mounts": [{"Type": "volume", "Source": "media_config", "Target": "/c"}]},
                   {"VolumesFrom": ["web"]}):
            with self.subTest(hc=hc), self.assertRaises(gate.Deny):
                decide(g, "POST", "/v5/containers/create", {"Image": "x", "HostConfig": hc})

    def test_g4_a_new_named_volume_is_created_labelled_first(self):
        g = Fake()
        decide(g, "POST", "/v5/libpod/containers/create", {"image": "x", "volumes": [{"Name": "data", "Dest": "/d"}]})
        self.assertTrue(any("volumes/create" in c for c in g.created), g.created)

    def test_g4_post_check_covers_volume_mounts(self):
        g = Fake({"volumes/media_config": {"Name": "media_config", "Labels": {}}})
        doc = container("c4", Mounts=[{"Type": "volume", "Name": "media_config", "Source": "/x"}])
        self.assertTrue(g.container_violations(doc))
        doc = container("c4", Mounts=[{"Type": "devpts", "Source": "/dev/pts"}])
        self.assertTrue(g.container_violations(doc))

    def test_g8_build_tags_cannot_take_other_images(self):
        g = Fake({"images/localhost/live-app:latest": {"Labels": {}},
                  "images/localhost/mine:1": {"Labels": {"agentbox.project": "proj", "agentbox.role": "dev"}}})
        for t in ("localhost/agentbox-omp:latest", "agentbox-claude", "docker.io/library/busybox:1.36",
                  "lscr.io/linuxserver/sonarr:latest", "localhost/live-app:latest", "live-app"):
            with self.subTest(t=t), self.assertRaises(gate.Deny):
                g.rewrite_build(f"/v5/libpod/build?t={t}")
        g.rewrite_build("/v5/libpod/build?t=localhost/mine:1")      # this project's own image
        g.rewrite_build("/v5/libpod/build?t=brand-new")             # nothing by that name yet
        with self.assertRaises(gate.Deny):
            g.rewrite_build("/v5/libpod/build?output=localhost/live-app:latest")

    def test_g5_endpoints_refused_in_the_gate_itself(self):
        for method, path in (("POST", "/v5/images/x/push"), ("POST", "/v5/libpod/images/x/push"),
                             ("POST", "/v5/libpod/manifests/m/push"), ("POST", "/v5/auth"), ("POST", "/v5/libpod/auth"),
                             ("POST", "/v5/commit?container=x"), ("POST", "/v5/libpod/commit?container=x"),
                             ("POST", "/v5/libpod/play/kube"), ("POST", "/v5/libpod/kube/play"),
                             ("POST", "/v5/images/x/tag"), ("POST", "/v5/libpod/images/x/tag"),
                             ("DELETE", "/v5/images/x"), ("POST", "/v5/images/load"), ("POST", "/v5/libpod/images/import"),
                             ("POST", "/v5/libpod/containers/x/checkpoint"), ("POST", "/v5/libpod/containers/x/restore"),
                             ("GET", "/v5/containers/x/export"), ("PUT", "/v5/containers/x/archive"),
                             ("POST", "/v5/libpod/containers/x/update"), ("POST", "/v5/libpod/local/build"),
                             ("POST", "/v5/libpod/secrets/create"), ("POST", "/v5/libpod/system/prune")):
            with self.subTest(path=path), self.assertRaises(gate.Deny):
                decide(Fake({"containers/x": container("x")}), method, path)

    def test_g7_exec_sessions_tied_to_a_verified_container(self):
        g = Fake()
        g.exec_ids["e1"] = "c1"
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/libpod/exec/e1/start")       # its container is not (or no longer) verified
        g.verified.add("c1")
        decide(g, "POST", "/v5/libpod/exec/e1/start")


class BParsing(unittest.TestCase):
    def test_g1_non_canonical_paths_refused(self):
        body = {"image": "x", "portmappings": [{"host_port": 18081, "container_port": 80}]}
        for path in ("/v5/libpod/../containers/create", "//v5/libpod/containers/create", "/v5/libpod//containers/create",
                     "/v5/libpod/containers/create/", "/v5/libpod/./containers/create", "/v5/libpod%2Fcontainers/create",
                     "/v5/libpod/containers/web%2F..%2Fx/start"):
            with self.subTest(path=path), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", path, body)


class CEnvAndLabels(unittest.TestCase):
    def test_l2_env_and_label_keys_are_not_renamed(self):
        _t, body, _k = decide(Fake(), "POST", "/v5/libpod/containers/create",
                              {"image": "x", "env": {"HOSTNAME": "0.0.0.0", "NAME": "n"}, "labels": {"TYPE": "t"}})
        spec = json.loads(body)
        self.assertEqual(spec["env"]["HOSTNAME"], "0.0.0.0")
        self.assertIn("NAME", spec["env"])
        self.assertIn("TYPE", spec["labels"])

    def test_l2_no_case_variant_proxy_variable_added(self):
        _t, body, _k = decide(Fake(), "POST", "/v5/libpod/containers/create",
                              {"image": "x", "env": {"HTTP_PROXY": "mine"}})
        env = json.loads(body)["env"]
        self.assertEqual([k for k in env if k.lower() == "http_proxy"], ["HTTP_PROXY"])


class Starts(unittest.TestCase):
    """Found 1 Oct (A2 replacement): podman resolves a bind source when the container starts, so a project
    directory swapped for a symlink after create mounted the symlink's target. No race needed."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="abt-unit-")
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.proj, "data"))
        os.makedirs(os.path.join(self.tmp, "outside"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def gate(self):
        doc = container("c1", Mounts=[{"Type": "bind", "Source": os.path.join(self.proj, "data"),
                                       "Destination": "/d"}], State={"Pid": 4242})
        g = Fake({"containers/web": doc, "containers/c1": doc})
        g.cfg["bind_roots"] = [self.proj]
        g.verified.add("c1")
        return g

    def test_swapped_bind_source_refused_at_start(self):
        g = self.gate()
        decide(g, "POST", "/v5/libpod/containers/web/start")
        os.rename(os.path.join(self.proj, "data"), os.path.join(self.proj, "data.real"))
        os.symlink(os.path.join(self.tmp, "outside"), os.path.join(self.proj, "data"))
        for path in ("/v5/libpod/containers/web/start", "/v5/containers/web/restart", "/v5/libpod/containers/web/init"):
            with self.subTest(path=path), self.assertRaises(gate.Deny):
                decide(g, "POST", path)

    def test_what_was_mounted_is_checked_after_start(self):
        g = self.gate()
        g.host_mountinfo = lambda: f"38 1 0:40 / {self.tmp} rw - tmpfs tmpfs rw\n"
        rel = self.proj[len(self.tmp):]
        g.read_mountinfo = lambda pid: f"1206 1 0:40 {rel}/data /d ro - tmpfs tmpfs rw\n"
        g.after_start(["c1"])
        self.assertIn("c1", g.verified)
        g.read_mountinfo = lambda pid: "1206 1 0:40 /outside /d ro - tmpfs tmpfs rw\n"
        with self.assertRaises(gate.Deny):
            g.after_start(["c1"])
        self.assertTrue(any("c1" in d for d in g.deleted))
        self.assertNotIn("c1", g.verified)
        g.verified.add("c1")
        g.read_mountinfo = lambda pid: "1206 1 0:41 " + rel + "/data /d ro - ext4 x rw\n"   # another filesystem
        with self.assertRaises(gate.Deny):
            g.after_start(["c1"])

    def test_pod_start_needs_every_member_checked(self):
        pod = {"Id": "pid", "Labels": POD_DEV, "InfraContainerID": "", "Containers": [{"Id": "c1"}, {"Id": "c2"}]}
        g = Fake({"pods/p": pod, "pods/pid": pod, "containers/c1": container("c1"), "containers/c2": container("c2")})
        g.verified.update({"pid", "c1"})
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/libpod/pods/p/start")
        g.verified.add("c2")
        decide(g, "POST", "/v5/libpod/pods/p/start")


class FakeSock:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent += data


class Framing(unittest.TestCase):
    def test_streamed_build_body_forwarded_exactly(self):
        up = FakeSock([])
        gate.stream_body(FakeSock([b"\r\nabc\r\n0\r\n\r\n"]), up, [("Transfer-Encoding", "chunked")], b"3")
        self.assertEqual(up.sent, b"3\r\nabc\r\n0\r\n\r\n")
        up = FakeSock([])
        gate.stream_body(FakeSock([b"de"]), up, [("Content-Length", "5")], b"abc")
        self.assertEqual(up.sent, b"abcde")

    def test_pipelined_bytes_after_a_build_body_refused(self):
        smuggled = b"POST /v5/libpod/containers/create HTTP/1.1\r\n\r\n"
        up = FakeSock([])
        with self.assertRaises(gate.Deny):
            gate.stream_body(FakeSock([]), up, [("Transfer-Encoding", "chunked")], b"3\r\nabc\r\n0\r\n\r\n" + smuggled)
        self.assertNotIn(b"0\r\n\r\n", up.sent, "the body's end is never sent, so the build never runs")
        with self.assertRaises(gate.Deny):
            gate.stream_body(FakeSock([]), FakeSock([]), [("Content-Length", "3")], b"abc" + smuggled)

    def test_pipelined_request_refused_by_the_handler(self):
        import socket as so
        a, b = so.socketpair()
        g = Fake()
        h = gate.make_handler(g)
        smuggled = b"POST /v5/libpod/containers/create HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}"
        a.sendall(b"GET /v5/libpod/_ping HTTP/1.1\r\nHost: d\r\n\r\n" + smuggled)
        a.shutdown(so.SHUT_WR)
        h(b, None, None)
        reply = a.recv(65536)
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply[:80])
        self.assertIn(b"pipelined", reply)

    def test_response_parsing(self):
        st, reason, headers, body = gate.parse_response(
            b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 201 Created\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\n{\"a\":\r\n2\r\n1}\r\n0\r\n\r\n")
        self.assertEqual((st, body), (201, b'{"a":1}'))


class BuildOptions(unittest.TestCase):
    def test_unknown_and_host_reaching_build_options_refused(self):
        g = Fake()
        for q in ("addcaps=%5B%22NET_ADMIN%22%5D", "cachefrom=x", "cacheto=x", "rusagelogfile=/tmp/x",
                  "outputs=type%3Dlocal%2Cdest%3D%2Ftmp", "registry=x", "groupadd=keep-groups", "ssh=default",
                  "secrets=%5B%22id%3Ds%2Csrc%3D%2Fhome%2Fx%2Fnotes.txt%22%5D",
                  "secrets=%5B%22id%3Ds%2Csrc%3D..%2Fx%22%5D", "secrets=%5B%22id%3Ds%2Cenv%3DHOME%22%5D",
                  "volume=%2F%3A%2Fh", "whatever=1"):
            with self.subTest(q=q), self.assertRaises(gate.Deny):
                g.rewrite_build("/v5/libpod/build?t=x&" + q)
        g.rewrite_build("/v5/libpod/build?t=x&secrets=%5B%22id%3Ds%2Csrc%3Dpodman-build-secret-123%22%5D"
                        "&dockerfile=%5B%22Containerfile%22%5D&rm=1&layers=1&output=x&outputformat=oci")


class Unsupported(unittest.TestCase):
    """Found by test_step5_fields (1 Oct): the dev sockguard refuses the forced cgroup parent on Docker-API
    creates, cannot resolve pods for a member container, and refuses every sysctl. The gate refuses each
    up front, with a reason."""

    def test_refused_by_default(self):
        g = gate.Gate(dict(CFG, log="/dev/null"))
        for path, body in (("/v5/containers/create", {"Image": "x"}), ("/v5/libpod/pods/create", {"name": "p"}),
                           ("/v5/libpod/containers/create", {"image": "x", "sysctl": {"net.core.somaxconn": "1"}})):
            with self.subTest(path=path), self.assertRaises(gate.Deny):
                g.decide("POST", path, [], json.dumps(body).encode())
        _t, body, _k = gate.Gate(dict(CFG, log="/dev/null")).decide(
            "POST", "/v5/libpod/pods/create", [], json.dumps({"name": "p", "no_infra": True}).encode())
        self.assertTrue(json.loads(body)["no_infra"])


class CreateFields(unittest.TestCase):
    """Fields found by capturing podman-remote's own requests (1 Oct): each reaches the host or adds privilege."""

    def test_refused(self):
        for body in ({"oci_runtime": "/proj/run.sh"}, {"conmon_pid_file": "/home/x/profile"},
                     {"init_path": "/home/x/notes.txt"}, {"rootfs": "/"}, {"env_host": True},
                     {"overlay_volumes": [{"source": "/proj", "destination": "/o"}]},
                     {"artifact_volumes": [{"source": "x", "destination": "/a"}]},
                     {"secrets": [{"Source": "live_db"}]}, {"secret_env": {"A": "live_db"}},
                     {"dependencyContainers": ["live_web"]}, {"log_configuration": {"path": "/home/x/profile"}},
                     {"healthLogDestination": "/home/x"}, {"base_hosts_file": "/home/x/hosts.txt"},
                     {"health_check_on_failure_action": "restart"}, {"seccomp_policy": "image"},
                     {"seccomp_profile_path": "unconfined"}, {"apparmor_profile": "unconfined"},
                     {"unmask": ["ALL"]}, {"cgroups_mode": "disabled"},
                     {"annotations": {"run.oci.keep_original_groups": "1"}}, {"groups": ["keep-groups"]},
                     {"sysctl": {"kernel.domainname": "x"}}, {"oom_score_adj": -1000},
                     {"resource_limits": {"memory": {"limit": -1}}}, {"resource_limits": {"pids": {"limit": -1}}},
                     {"resource_limits": {"memory": {"limit": 1 << 40}}},
                     {"resource_limits": {"unified": {"memory.max": "max"}}},
                     {"resource_limits": {"memory": {"disableOOMKiller": True}}},
                     {"mounts": [{"type": "devpts", "destination": "/e"}]},
                     {"mounts": [{"type": "bind", "source": "proj/rel", "destination": "/r"}]},
                     {"mounts": [{"type": "bind", "source": "/proj", "destination": "/r", "options": ["upperdir=/x"]}]},
                     {"volumes": [{"Name": "rel/path", "Dest": "/r"}]},
                     {"volumes": [{"Name": "v", "Dest": "/r", "Options": ["volume-opt=o=bind"]}]},
                     {"no_such_field": 1}):
            with self.subTest(body=body), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", "/v5/libpod/containers/create", {"image": "x", **body})

    def test_compat_empty_masked_paths_refused(self):
        """Found by test_step5_fields (1 Oct): MaskedPaths [] (unmask everything) is falsy and passed the gate."""
        for hc in ({"MaskedPaths": []}, {"ReadonlyPaths": []}):
            with self.subTest(hc=hc), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", "/v5/containers/create", {"Image": "x", "HostConfig": hc})

    def test_allowed_values_still_work(self):
        g = Fake()
        _t, body, _k = decide(g, "POST", "/v5/libpod/containers/create", {
            "image": "x", "cap_drop": ["ALL"], "oom_score_adj": 1000,
            "annotations": {"org.example.note": "x", "io.podman.annotations.pids-limit": "5"},
            "resource_limits": {"memory": {"limit": 512 << 20}, "pids": {"limit": 100}},
            "mounts": [{"type": "tmpfs", "destination": "/t", "options": ["size=64m"]}],
            "volumes": [{"Name": "", "Dest": "/anon"}], "seccomp_policy": "default", "groups": ["10"]})
        spec = json.loads(body)
        self.assertEqual(spec["resource_limits"]["memory"], {"limit": 512 << 20, "swap": 512 << 20})
        self.assertEqual(spec["annotations"], {"org.example.note": "x"})


if __name__ == "__main__":
    unittest.main()
