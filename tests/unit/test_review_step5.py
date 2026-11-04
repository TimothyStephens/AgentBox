"""Step 5 audit (docs/reviews/2026-10-01-step-5{A1,A4,B,C}.md): one test per gate finding, written to fail
before the fix. Upstream lookups are faked."""
import io
import json
import os
import threading
import unittest
import unittest.mock

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
        if path.endswith("/volumes/create") and body:
            self.objects[f"volumes/{body['Name']}"] = {"Name": body["Name"], "Driver": body.get("Driver", "local"),
                                                       "Labels": body.get("Label") or {},
                                                       "Options": body.get("Options") or {}}
        return 200

    def holder_ns(self):
        return "/proc/4242/ns/net", 4242, "net:[1]"

    def image_doc(self, ref):
        doc = self.objects.get(f"images/{ref}")
        first = ref.split("/", 1)[0]
        if doc is None and first in self.cfg["registries"]:
            return {"RepoTags": [ref], "Labels": {}}
        if doc is None and "/" in ref and ("." in first or ":" in first or first == "localhost"):
            return None                      # a registry-qualified name this fake store does not have
        return doc if doc is not None else {"RepoTags": [f"docker.io/library/{ref}"], "Labels": {}}

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
        import tempfile
        from agentbox import builtrec
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        mine = "9" * 64
        g = Fake({"images/localhost/live-app:latest": {"Labels": {}},
                  "images/localhost/labelled:1": {"Id": "8" * 64,
                                                  "Labels": {"agentbox.project": "proj", "agentbox.role": "dev"}},
                  "images/localhost/mine:1": {"Id": mine, "Labels": {}}})
        g.cfg["built_record"] = builtrec.path_for(d, "proj")
        builtrec.add(g.cfg["built_record"], mine, "abt-s-1")
        for t in ("localhost/agentbox-omp:latest", "agentbox-claude", "docker.io/library/busybox:1.36",
                  "lscr.io/linuxserver/sonarr:latest", "localhost/live-app:latest", "live-app",
                  "localhost/labelled:1"):
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

    def gate(self, source=None):
        doc = container("c1", Mounts=[{"Type": "bind", "Source": source or self.proj, "Destination": "/d"}],
                        State={"Pid": 4242})
        g = Fake({"containers/web": doc, "containers/c1": doc})
        g.cfg["bind_roots"] = [self.proj]
        g.verified.add("c1")
        return g

    def test_sub_path_binds_rewritten_and_a_stored_one_refused_at_start(self):
        """Tim's decision (1 Oct): a sub-path is never bound directly; it becomes a grant volume SubPath, which
        podman resolves inside the volume. A sub-path bind found in what podman stored is refused at start."""
        g = Fake()
        g.cfg["bind_roots"] = [self.proj]
        for src in (os.path.join(self.proj, "data"), os.path.join(self.proj, "data", "..", "data")):
            _t, body, _k = decide(g, "POST", "/v5/libpod/containers/create",
                                  {"image": "x", "mounts": [{"type": "bind", "source": src, "destination": "/d"}]})
            spec = json.loads(body)
            self.assertEqual(spec["mounts"], [], src)
            self.assertEqual([(v["Name"], v["SubPath"]) for v in spec["volumes"]], [("abt-s-1-grant0", "data")])
        os.symlink(os.path.join(self.tmp, "outside"), os.path.join(self.proj, "out"))
        with self.assertRaises(gate.Deny):                  # a symlink out is outside the grants
            decide(g, "POST", "/v5/libpod/containers/create",
                   {"image": "x", "mounts": [{"type": "bind", "source": os.path.join(self.proj, "out"),
                                              "destination": "/d"}]})
        g = self.gate(os.path.join(self.proj, "data"))     # stored somehow: still refused at start
        for path in ("/v5/libpod/containers/web/start", "/v5/containers/web/restart", "/v5/libpod/containers/web/init"):
            with self.subTest(path=path), self.assertRaises(gate.Deny):
                decide(g, "POST", path)

    def test_root_bind_works(self):
        g = self.gate()
        g.cfg["bind_roots"] = [self.proj]
        _t, body, _k = decide(g, "POST", "/v5/libpod/containers/create",
                              {"image": "x", "mounts": [{"type": "bind", "source": self.proj, "destination": "/d"}]})
        self.assertEqual(json.loads(body)["mounts"][0]["source"], self.proj)
        rel = self.proj[len(self.tmp):]
        g.host_mountinfo = lambda: f"38 1 0:40 / {self.tmp} rw - tmpfs tmpfs rw\n"
        g.read_mountinfo = lambda pid: f"1206 1 0:40 {rel} /d rw,nosuid - tmpfs tmpfs rw\n"
        decide(g, "POST", "/v5/libpod/containers/web/start")

    def test_start_initialises_and_checks_before_the_entrypoint(self):
        """Audit 5 Oct, H2: init first, read the mounts, only then start; fail closed."""
        g = self.gate()
        g.objects["containers/c1"]["State"] = {"Pid": 0, "Status": "created"}
        calls = []
        real_raw = g.raw_request

        def raw(method, path, body=None):
            calls.append((method, path))
            if path.endswith("/init"):
                g.objects["containers/c1"]["State"] = {"Pid": 4242, "Status": "initialized"}
            return real_raw(method, path, body)
        g.raw_request = raw
        rel = self.proj[len(self.tmp):]
        g.host_mountinfo = lambda: f"38 1 0:40 / {self.tmp} rw - tmpfs tmpfs rw\n"
        g.read_mountinfo = lambda pid: f"1206 1 0:40 {rel} /d rw - tmpfs tmpfs rw\n"
        decide(g, "POST", "/v5/libpod/containers/web/start")
        self.assertIn(("POST", "/libpod/containers/c1/init"), calls, "initialised by the gate before start")
        g.objects["containers/c1"]["State"] = {"Pid": 0, "Status": "created"}
        g.read_mountinfo = lambda pid: (_ for _ in ()).throw(OSError("gone"))
        g.verified.add("c1")
        with self.assertRaises(gate.Deny):                 # unreadable mount table: refused, not skipped
            decide(g, "POST", "/v5/libpod/containers/web/start")
        self.assertTrue(any("c1" in d for d in g.deleted))

    def test_restart_becomes_stop_init_check_start(self):
        g = self.gate()
        g.objects["containers/c1"]["State"] = {"Pid": 4242, "Status": "running"}
        calls = []

        def raw(method, path, body=None):
            calls.append(path)
            if path.endswith("/init"):
                g.objects["containers/c1"]["State"] = {"Pid": 5151, "Status": "initialized"}
            if "/stop" in path:
                g.objects["containers/c1"]["State"] = {"Pid": 0, "Status": "exited"}
            return 204
        g.raw_request = raw
        rel = self.proj[len(self.tmp):]
        g.host_mountinfo = lambda: f"38 1 0:40 / {self.tmp} rw - tmpfs tmpfs rw\n"
        g.read_mountinfo = lambda pid: f"1 1 0:40 {rel} /d rw - tmpfs tmpfs rw\n"
        target = decide(g, "POST", "/v5/libpod/containers/web/restart?t=3")[0]
        self.assertEqual(target, "/v5/libpod/containers/web/start?t=3")
        self.assertEqual([c.split("?")[0].rsplit("/", 1)[-1] for c in calls], ["stop", "init"])

    def test_mount_violations_strict(self):
        g = self.gate()
        doc = g.objects["containers/c1"]
        doc["State"] = {"Pid": 0}
        self.assertEqual(g.mount_violations(doc), [], "not strict: nothing to read")
        self.assertTrue(g.mount_violations(doc, strict=True), "strict: no process is a violation")

    def test_grant_volume_mount_checked_at_start(self):
        """E3/T2: drive the LIVE start path (prepare_start → mount_violations, strict), not the dead
        after_start. A grant volume must land inside its own root."""
        g = self.gate()
        g.objects["volumes/abt-s-1-grant0"] = {"Name": "abt-s-1-grant0", "Driver": "local",
                                               "Labels": {gate.DEV_OWNER: "abt-s-1", "agentbox.role": "grant"},
                                               "Options": {"type": "none", "o": "bind", "device": self.proj}}
        doc = g.objects["containers/c1"]
        doc["Mounts"] = [{"Type": "volume", "Name": "abt-s-1-grant0", "Destination": "/d"}]
        doc["State"] = {"Pid": 5151, "Status": "initialized"}
        g.host_mountinfo = lambda: f"38 1 0:40 / {self.tmp} rw - tmpfs tmpfs rw\n"
        rel = self.proj[len(self.tmp):]
        g.read_mountinfo = lambda pid: f"1206 1 0:40 {rel}/data /d ro - tmpfs tmpfs rw\n"
        g.prepare_start("c1")                                     # inside its root: fine
        g.release_held()
        g.read_mountinfo = lambda pid: "1206 1 0:40 /outside /d ro - tmpfs tmpfs rw\n"
        with self.assertRaises(gate.Deny):
            g.prepare_start("c1")                                 # outside the grant root: refused at start
        g.release_held()
        self.assertTrue(any("c1" in d for d in g.deleted), "the container was removed")

    def test_what_was_mounted_is_checked_at_start(self):
        """E3/T2: the swap-at-start check runs on the inited container before the entrypoint, via
        prepare_start. (Was tested only against the dead after_start, and in integration.)"""
        g = self.gate()                                           # bind source=self.proj (a grant root) at /d
        g.objects["containers/c1"]["State"] = {"Pid": 4242, "Status": "initialized"}
        g.host_mountinfo = lambda: f"38 1 0:40 / {self.tmp} rw - tmpfs tmpfs rw\n"
        rel = self.proj[len(self.tmp):]
        g.read_mountinfo = lambda pid: f"1206 1 0:40 {rel} /d ro - tmpfs tmpfs rw\n"
        g.prepare_start("c1")                                     # the grant root itself: fine
        g.release_held()
        for line in ("1206 1 0:40 /outside /d ro - tmpfs tmpfs rw\n",       # swapped to outside
                     f"1206 1 0:40 {rel}/data /d ro - tmpfs tmpfs rw\n",    # a sub-path, not the root
                     f"1206 1 0:41 {rel} /d ro - ext4 x rw\n"):             # another filesystem
            g.read_mountinfo = lambda pid, line=line: line
            with self.subTest(line=line), self.assertRaises(gate.Deny):
                g.prepare_start("c1")
            g.release_held()
        self.assertTrue(any("c1" in d for d in g.deleted))

    def test_after_start_checked_refuses_a_reinitialised_pid(self):
        """E4/T2: the live PID recheck after the start reply. A container re-initialised to a different PID
        between the gate's check and the start reply is removed and refused."""
        g = self.gate()
        g.begin_request()
        g._tls.checked = {"c1": 4242}
        g.objects["containers/c1"]["State"] = {"Pid": 9999, "Status": "running"}   # a different PID
        with self.assertRaises(gate.Deny) as e:
            g.after_start_checked()
        self.assertIn("re-initialised", str(e.exception))
        self.assertTrue(any("c1" in d for d in g.deleted))

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
    def test_build_body_spooled_exactly(self):
        out = io.BytesIO()
        n = gate.spool_body(FakeSock([b"\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"]), [("Transfer-Encoding", "chunked")], b"3", out)
        self.assertEqual((n, out.getvalue()), (5, b"abcde"))
        out = io.BytesIO()
        gate.spool_body(FakeSock([b"de"]), [("Content-Length", "5")], b"abc", out)
        self.assertEqual(out.getvalue(), b"abcde")

    def test_pipelined_bytes_after_a_build_body_refused(self):
        smuggled = b"POST /v5/libpod/containers/create HTTP/1.1\r\n\r\n"
        with self.assertRaises(gate.Deny):
            gate.spool_body(FakeSock([]), [("Transfer-Encoding", "chunked")], b"3\r\nabc\r\n0\r\n\r\n" + smuggled,
                            io.BytesIO())
        with self.assertRaises(gate.Deny):
            gate.spool_body(FakeSock([]), [("Content-Length", "3")], b"abc" + smuggled, io.BytesIO())

    def test_pipelined_request_refused_by_the_handler(self):
        import socket as so
        a, b = so.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
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


class Images(unittest.TestCase):
    """Tim, 1 Oct (A4 low, image reads): the dev tier runs and builds only from public registry images, this
    session's own builds, or images the project lists in dev_images."""

    MINE = "1" * 64
    OLD = "2" * 64
    FORGED = "3" * 64

    def gate(self, **cfg):
        import tempfile
        from agentbox import builtrec
        rec = os.path.join(tempfile.mkdtemp(prefix="abt-unit-"), "built-images", "proj.json")
        self.addCleanup(lambda: __import__("shutil").rmtree(os.path.dirname(os.path.dirname(rec))))
        builtrec.add(rec, self.MINE, "abt-s-1")
        builtrec.add(rec, self.OLD, "abt-s-0")          # an earlier session of this project
        g = Fake({"images/localhost/live-app:latest": {"RepoTags": ["localhost/live-app:latest"], "Labels": {}},
                  "images/live-app": {"RepoTags": ["localhost/live-app:latest"], "Labels": {}},
                  "images/localhost/other:1": {"RepoTags": ["localhost/other:1"],
                                               "Labels": {"agentbox.project": "other", "agentbox.role": "dev",
                                                          "agentbox.session": "abt-o-1"}},
                  "images/localhost/mine:1": {"Id": self.MINE, "RepoTags": ["localhost/mine:1"], "Labels": {}},
                  "images/localhost/mine:old": {"Id": self.OLD, "RepoTags": ["localhost/mine:old"], "Labels": {}},
                  # carries this project's labels but was built outside the gate (Tim, 2 Oct): refused
                  "images/localhost/forged:1": {"Id": self.FORGED, "RepoTags": ["localhost/forged:1"],
                                                "Labels": {"agentbox.project": "proj", "agentbox.role": "dev",
                                                           "agentbox.session": "abt-s-1"}},
                  "images/abc123": {"RepoTags": [], "Labels": {}}})
        g.cfg.update(cfg, built_record=rec)
        return g

    def test_run(self):
        g = self.gate()
        for ref in ("localhost/live-app:latest", "live-app", "localhost/other:1", "localhost/forged:1", "abc123"):
            with self.subTest(ref=ref), self.assertRaises(gate.Deny):
                decide(g, "POST", "/v5/libpod/containers/create", {"image": ref})
        for ref in ("docker.io/library/busybox:1.36", "localhost/mine:1", "localhost/mine:old"):
            decide(g, "POST", "/v5/libpod/containers/create", {"image": ref})
        with self.assertRaises(gate.Deny):      # a forged-label image's name cannot be taken by a build either
            g.rewrite_build("/v5/libpod/build?t=localhost/forged:1")
        g.rewrite_build("/v5/libpod/build?t=localhost/mine:old")
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/libpod/containers/create",
                   {"image": "busybox", "image_volumes": [{"Source": "localhost/live-app:latest", "Destination": "/i"}]})
        g = self.gate(images=["localhost/forged:*", "localhost/live-app:latest"])
        for ref in ("localhost/forged:1", "live-app"):
            decide(g, "POST", "/v5/libpod/containers/create", {"image": ref})

    def context(self, files):
        import tarfile
        import tempfile
        f = tempfile.NamedTemporaryFile(suffix=".tar")
        with tarfile.open(fileobj=f, mode="w") as t:
            for name, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        f.flush()
        self.addCleanup(f.close)
        return f.name

    def test_build(self):
        g = self.gate()
        q = "/v5/libpod/build?dockerfile=%5B%22Containerfile%22%5D"
        for cf in ("FROM localhost/live-app:latest\n", "FROM docker.io/library/busybox:1.36\nCOPY --from=live-app /a /b\n",
                   "FROM busybox\nRUN --mount=type=bind,from=localhost/other:1,target=/o true\n",
                   "ARG BASE=localhost/live-app:latest\nFROM $BASE\n", "FROM $UNSET\n"):
            with self.subTest(cf=cf), self.assertRaises(gate.Deny):
                g.check_build_images(self.context({"Containerfile": cf}), q)
        g.check_build_images(self.context({"Containerfile": (
            "FROM docker.io/library/python:3.12 AS build\nRUN true\nFROM localhost/mine:1\n"
            "COPY --from=build /a /b\nRUN <<EOF\nFROM localhost/live-app:latest\nEOF\n")}), q)
        g.check_build_images(self.context({"Containerfile": "ARG BASE\nFROM $BASE\n"}),
                             q + "&buildargs=%7B%22BASE%22%3A%22busybox%22%7D")
        with self.assertRaises(gate.Deny):          # the dockerfile must be in the context
            g.check_build_images(self.context({"Containerfile": "FROM busybox\n"}),
                                 "/v5/libpod/build?dockerfile=%5B%22%2Fetc%2Fhostname%22%5D")


class BuiltRecord(unittest.TestCase):
    def test_record_add_remove_and_watcher(self):
        import tempfile
        from agentbox import builtrec
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        rec = builtrec.path_for(d, "proj")
        builtrec.add(rec, "sha256:" + "a" * 64, "s1")
        builtrec.add(rec, "b" * 64, "s2")
        self.assertEqual(builtrec.ids(rec), {"a" * 64, "b" * 64})
        builtrec.remove_everywhere(d, ["a" * 64])
        self.assertEqual(builtrec.ids(rec), {"b" * 64})
        ok = b'{"stream":"STEP 1/1\\n"}\n{"stream":"' + b"c" * 64 + b'\\n"}\n'
        for raw, want in ((b"HTTP/1.1 200 OK\r\n\r\n" + ok, "c" * 64),
                          (b"HTTP/1.1 200 OK\r\n\r\n" + ok + b'{"error":"boom"}\n', None),
                          (b"HTTP/1.1 500 Internal\r\n\r\n" + ok, None)):
            w = builtrec.BuildWatcher()
            w.feed(raw)
            self.assertEqual(w.close(), want, raw[-40:])

    def test_only_the_id_the_reply_ends_with_counts(self):
        """Review of 785ee15, H1: a RUN step can print any image's ID; only the last stream line counts."""
        from agentbox import builtrec
        fake, real = "f" * 64, "e" * 64
        head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"

        def chunk(text):
            return b"%x\r\n%s\r\n" % (len(text), text)
        line = lambda t: chunk(b'{"stream":"' + t + b'\\n"}\n')
        replies = {
            "echoed ID, then the real one": (head + line(b"STEP 1/2") + line(fake.encode()) + line(b"STEP 2/2")
                                             + line(real.encode()) + b"0\r\n\r\n", real),
            "echoed ID, then an error": (head + line(fake.encode()) + chunk(b'{"error":"boom"}\n') + b"0\r\n\r\n",
                                         None),
            "echoed ID last, but status 500": (b"HTTP/1.1 500 x\r\nTransfer-Encoding: chunked\r\n\r\n"
                                               + line(fake.encode()) + b"0\r\n\r\n", None),
        }
        for name, (raw, want) in replies.items():
            pieces = [raw[i:i + 40] for i in range(0, len(raw), 40)]
            up, client, recorded = FakeSock(pieces), FakeSock([]), []

            def record(image_id, client=client, recorded=recorded):
                recorded.append((image_id, client.sent))
            gate.pipe_build_reply(up, client, record)
            with self.subTest(name=name):
                self.assertEqual([r[0] for r in recorded], [want])
                self.assertEqual(client.sent, raw, "everything is passed on")
                if want:
                    self.assertNotIn(want.encode(), recorded[0][1], "recorded before the client sees the ID")

    def test_record_requires_this_session_and_a_fresh_image(self):
        import tempfile
        import time as _time
        from agentbox import builtrec
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        now = _time.time()
        g = Fake({"images/" + "1" * 64: {"Id": "1" * 64, "Labels": {"agentbox.session": "abt-s-1"}, "Created": now},
                  "images/" + "2" * 64: {"Id": "2" * 64, "Labels": {"agentbox.session": "abt-s-1"},
                                         "Created": now - 3600},
                  "images/" + "3" * 64: {"Id": "3" * 64, "Labels": {}, "Created": now}})
        g.cfg["built_record"] = builtrec.path_for(d, "proj")
        self.assertTrue(g.record_build("1" * 64, since=now - 1))
        self.assertFalse(g.record_build("2" * 64, since=now - 1), "made before this build started")
        self.assertFalse(g.record_build("3" * 64, since=now - 1), "not labelled by this session")
        self.assertEqual(builtrec.ids(g.cfg["built_record"]), {"1" * 64})
        self.assertEqual(gate.image_created({"Created": "2026-10-02T15:38:11.322027189Z"}),
                         1790955491.322027)

    def test_gc_drops_removed_images_from_the_record(self):
        import tempfile
        from agentbox import builtrec, session
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        rec = builtrec.path_for(d, "proj")
        old, new = "e" * 64, "f" * 64
        builtrec.add(rec, old, "s1")
        builtrec.add(rec, new, "s2")

        class Settings:
            labels = {"agentbox": "1"}
            image_keep_days = 7
            data_dir = d

        class Podman:
            def run(self, argv, check=False):
                import subprocess
                if argv[:1] == ["images"]:
                    out = json.dumps([{"Id": old, "Created": 0, "Names": [], "Labels": {"agentbox.project": "proj"}},
                                      {"Id": new, "Created": 9e9, "Names": ["localhost/x:1"],
                                       "Labels": {"agentbox.project": "proj"}}])
                    return subprocess.CompletedProcess(argv, 0, out, "")
                return subprocess.CompletedProcess(argv, 0, "", "")
        session.gc_images(Settings(), Podman(), now=1e10)
        self.assertEqual(builtrec.ids(rec), {new})


class Covers(unittest.TestCase):
    """Critical (5 Oct, life-updates spec 6.2): the agent's in-container masks and read-only covers do not
    exist in a container it spawns through the gate, so the gate refuses a bind that reaches a covered path."""

    def gate(self):
        g = Fake()
        g.cfg["bind_roots"] = ["/proj"]
        g.cfg["covered"] = ["/proj/data", "/proj/.env", "/proj/.git/hooks", "/proj/.git/config"]
        return g

    def bind(self, g, src):
        return decide(g, "POST", "/v5/libpod/containers/create",
                      {"image": "x", "mounts": [{"type": "bind", "source": src, "destination": "/m"}]})

    def test_binding_the_whole_root_is_refused(self):
        with self.assertRaises(gate.Deny) as e:
            self.bind(self.gate(), "/proj")
        self.assertIn("masked or covered", str(e.exception))

    def test_binding_onto_or_under_a_cover_is_refused(self):
        g = self.gate()
        for src in ("/proj/data", "/proj/data/app.db", "/proj/.git", "/proj/.git/config", "/proj/.env"):
            with self.subTest(src=src), self.assertRaises(gate.Deny):
                self.bind(g, src)

    def test_a_sibling_subpath_is_allowed(self):
        g = self.gate()
        _t, body, _k = self.bind(g, "/proj/dev/data")   # not under or over any cover
        spec = json.loads(body)
        vols = spec.get("volumes") or []
        self.assertEqual([(v["Name"], v["SubPath"]) for v in vols], [("abt-s-1-grant0", "dev/data")])
        self.assertEqual(spec["mounts"], [])

    def test_compat_binds_checked_too(self):
        g = self.gate()
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/containers/create", {"Image": "x", "HostConfig": {"Binds": ["/proj/data:/m"]}})
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/containers/create",
                   {"Image": "x", "HostConfig": {"Mounts": [{"Type": "bind", "Source": "/proj", "Target": "/m"}]}})

    def test_mount_violations_rejects_a_grant_volume_swapped_onto_a_cover(self):
        # T3 (renamed from test_after_start_…): this exercises mount_violations directly, not after_start.
        g = self.gate()
        g.objects["volumes/abt-s-1-grant0"] = {"Name": "abt-s-1-grant0", "Driver": "local",
            "Labels": {gate.DEV_OWNER: "abt-s-1", "agentbox.role": "grant"},
            "Options": {"type": "none", "o": "bind", "device": "/proj"}}
        doc = container("c1", Mounts=[{"Type": "volume", "Name": "abt-s-1-grant0", "Destination": "/m"}],
                        State={"Pid": 4242})
        g.host_mountinfo = lambda: "38 1 0:40 / /proj rw - tmpfs tmpfs rw\n"
        g.read_mountinfo = lambda pid: "1 1 0:40 /app /m ro - tmpfs tmpfs rw\n"    # under the root, not a cover
        self.assertEqual(g.mount_violations(doc), [])
        g.read_mountinfo = lambda pid: "1 1 0:40 /data /m ro - tmpfs tmpfs rw\n"   # swapped onto the cover
        self.assertTrue(g.mount_violations(doc))


class CoversAudit(unittest.TestCase):
    """Independent audit of the covers fix (docs/reviews/2026-10-05-covers.md)."""

    def setUp(self):
        import tempfile
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="abt-unit-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.proj = os.path.join(self.tmp, "proj")
        self.ro = os.path.join(self.tmp, "ro")
        for d in ("proj/dev", "proj/app/sub", "proj/secrets", "ro/sub"):
            os.makedirs(os.path.join(self.tmp, d))
        open(os.path.join(self.proj, "dev", ".env"), "w").close()
        open(os.path.join(self.proj, "Containerfile"), "w").close()     # the context is on disk

    def gate(self):
        g = Fake()
        g.cfg["bind_roots"] = [self.proj, self.ro]
        g.cfg["ro_roots"] = [self.ro]
        cov = [os.path.join(self.proj, "dev", ".env"), os.path.join(self.proj, "secrets")]
        g.cfg["covered"] = cov
        g.cfg["masked"] = cov
        g.cfg["covered_ids"] = {c: [os.lstat(c).st_dev, os.lstat(c).st_ino] for c in cov}
        return g

    def bind(self, g, src):
        return json.loads(decide(g, "POST", "/v5/libpod/containers/create",
                                 {"image": "x", "mounts": [{"type": "bind", "source": src, "destination": "/m"}]})[1])

    def test_h1_a_moved_cover_stops_all_binds(self):
        g = self.gate()
        self.bind(g, os.path.join(self.proj, "app"))                       # fine while the covers are in place
        os.rename(os.path.join(self.proj, "dev"), os.path.join(self.proj, "dev2"))
        os.symlink("app", os.path.join(self.proj, "dev"))                  # a link left at the old name
        for src in (os.path.join(self.proj, "dev2"), os.path.join(self.proj, "app")):
            with self.subTest(src=src), self.assertRaises(gate.Deny) as e:
                self.bind(g, src)
            self.assertIn("moved or replaced", str(e.exception))

    def test_u_and_chown_mount_options_refused_on_grant_binds(self):
        """Audit 6 Oct, cheap half of H1: U/chown re-own the bound host files; refused on grant binds and
        grant-volume SubPaths (an accidental re-own of the project is a mistake-model harm)."""
        g = self.gate()
        for opt in ("U", "chown"):
            with self.subTest(opt=opt, kind="root"), self.assertRaises(gate.Deny):
                decide(g, "POST", "/v5/libpod/containers/create",
                       {"image": "x", "mounts": [{"type": "bind", "source": self.proj + "/app",
                                                   "destination": "/m", "options": [opt]}]})
        # a sub-path (grant-volume SubPath) with U is refused too
        with self.subTest(kind="subpath"), self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/libpod/containers/create",
                   {"image": "x", "mounts": [{"type": "bind", "source": self.proj + "/app/sub",
                                              "destination": "/m", "options": ["ro", "U"]}]})
        # a clean ro sub-path still works
        _t, body, _k = decide(g, "POST", "/v5/libpod/containers/create",
                              {"image": "x", "mounts": [{"type": "bind", "source": self.proj + "/app/sub",
                                                         "destination": "/m", "options": ["ro"]}]})
        self.assertTrue(json.loads(body).get("volumes"))
        # compat Binds with U is refused too
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/containers/create",
                   {"Image": "x", "HostConfig": {"Binds": [self.proj + "/app:/m:U"]}})

    def test_a_bind_of_a_foreign_uid_dir_is_refused(self):
        """Audit 6 Oct, H1 (the half the gate enforces): a gate-spawned container binding a sub-path owned by
        another uid — a container's data directory under keep-id — is refused."""
        import unittest.mock
        g = self.gate()
        foreign = os.path.join(self.proj, "app", "sub")
        real_lstat = os.lstat

        def fake(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if str(path) == foreign:          # no realpath here: realpath() calls lstat and would recurse
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1)
                                      + tuple(st)[5:])
            return st
        with unittest.mock.patch("agentbox.gate.os.lstat", side_effect=fake):
            with self.assertRaises(gate.Deny) as e:
                self.bind(g, foreign)
        self.assertIn("not you", str(e.exception))
        # a user-owned sibling still binds fine (the check only bites foreign ownership)
        self.assertTrue(self.bind(g, os.path.join(self.proj, "app")).get("volumes"))

    def test_covers_intact_detects_a_deleted_cover(self):
        """E8: covers_intact caught a renamed/replaced cover; a deleted one was untested."""
        g = self.gate()
        cover = os.path.join(self.proj, "dev", ".env")
        self.assertIsNone(g.covers_intact())
        os.remove(cover)
        self.assertEqual(g.covers_intact(), cover)

    def test_unmasked_secret_under_finds_late_secrets(self):
        """E7: a secret-named file that appeared after the masks were made (N6), a file under a secrets/ dir,
        and the scan-limit cutoff — none were unit-tested."""
        g = self.gate()
        base = os.path.join(self.proj, "app")               # not covered
        key = os.path.join(base, "late.key")
        with open(key, "w") as f:
            f.write("k")
        self.assertEqual(g.unmasked_secret_under(base), key)
        os.remove(key)
        os.makedirs(os.path.join(base, "secrets"))
        with open(os.path.join(base, "secrets", "pw"), "w") as f:
            f.write("x")
        self.assertTrue(g.unmasked_secret_under(base), "a file under a secrets/ dir counts")
        # the scan-limit cutoff refuses rather than pass an unchecked tree
        old = gate.BIND_SCAN_LIMIT
        gate.BIND_SCAN_LIMIT = 0
        try:
            self.assertIn("too large", g.unmasked_secret_under(base))
        finally:
            gate.BIND_SCAN_LIMIT = old

    def test_h3_read_only_grant_stays_read_only(self):
        g = self.gate()
        spec = self.bind(g, self.ro)
        self.assertIn("ro", spec["mounts"][0]["options"])
        spec = self.bind(g, os.path.join(self.ro, "sub"))
        self.assertIn("ro", spec["volumes"][0]["Options"])
        doc = container("c1", Mounts=[{"Type": "bind", "Source": self.ro, "Destination": "/m"}], State={"Pid": 7})
        g.host_mountinfo = lambda: f"38 1 0:40 / {self.tmp} rw - tmpfs tmpfs rw\n"
        g.read_mountinfo = lambda pid: "1 1 0:40 /ro /m rw,nosuid - tmpfs tmpfs rw\n"
        self.assertTrue(any("read-write" in v for v in g.mount_violations(doc)))
        g.read_mountinfo = lambda pid: "1 1 0:40 /ro /m ro,nosuid - tmpfs tmpfs rw\n"
        self.assertEqual(g.mount_violations(doc), [])

    def tar(self, files):
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for name, text in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(text)
                t.addfile(info, io.BytesIO(text))
        path = os.path.join(self.tmp, "ctx.tar")
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        return path

    def test_build_context_carrying_masked_content_refused(self):
        g = self.gate()
        q = "/v5/libpod/build?dockerfile=%5B%22Containerfile%22%5D"
        cf = b"FROM busybox\nCOPY . /app\n"
        # as packed in the session: the masks are empty -> allowed
        g.check_build_images(self.tar({"Containerfile": cf, "dev/.env": b"", "app/x": b"1"}), q)
        # content at a masked path: not packed through the masks -> refused (root context, and a sub context)
        for files in ({"Containerfile": cf, "dev/.env": b"REAL=1\n", "app/x": b"1"},
                      {"Containerfile": cf, "secrets/db": b"pw\n", "dev/.env": b""}):
            with self.subTest(files=sorted(files)), self.assertRaises(gate.Deny):
                g.check_build_images(self.tar(files), q)
        open(os.path.join(self.proj, "dev", "Containerfile"), "w").close()
        with self.assertRaises(gate.Deny):
            g.check_build_images(self.tar({"Containerfile": cf, ".env": b"REAL=1\n"}), q)

    def test_n5_identity_checked_at_start(self):
        g = self.gate()
        doc = container("c1", State={"Pid": 0, "Status": "created"})
        g.objects.update({"containers/web": doc, "containers/c1": doc})
        g.verified.add("c1")
        env = os.path.join(self.proj, "dev", ".env")
        open(env + ".new", "w").close()
        os.rename(env + ".new", env)                    # replaced by rename (new inode), as editors save
        with self.assertRaises(gate.Deny) as e:
            decide(g, "POST", "/v5/libpod/containers/web/start")
        self.assertIn("moved or replaced", str(e.exception))

    def test_n6_a_secret_named_file_that_appeared_later_blocks_the_bind(self):
        g = self.gate()
        self.bind(g, os.path.join(self.proj, "app"))
        with open(os.path.join(self.proj, "app", "new.pem"), "w") as f:
            f.write("written on the host after start\n")
        with self.assertRaises(gate.Deny) as e:
            self.bind(g, os.path.join(self.proj, "app"))
        self.assertIn("new.pem", str(e.exception))
        open(os.path.join(self.proj, "app", "new.pem"), "w").close()     # empty: nothing to protect
        self.bind(g, os.path.join(self.proj, "app"))

    def test_n7_sub_path_on_another_filesystem_refused(self):
        g = self.gate()
        real_stat = os.stat
        app = os.path.join(self.proj, "app")

        def fake_stat(path, *a, **k):
            st = real_stat(path, *a, **k)
            if path == app:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1) + tuple(st)[3:])
            return st
        with unittest.mock.patch("agentbox.gate.os.stat", side_effect=fake_stat), self.assertRaises(gate.Deny) as e:
            self.bind(g, app)
        self.assertIn("different filesystem", str(e.exception))

    def test_l2_grant_volume_cannot_be_named_directly(self):
        g = self.gate()
        g.objects["volumes/abt-s-1-grant0"] = {"Name": "abt-s-1-grant0", "Driver": "local",
            "Labels": {gate.DEV_OWNER: "abt-s-1", "agentbox.role": "grant"},
            "Options": {"type": "none", "o": "bind", "device": self.proj}}
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/libpod/containers/create",
                   {"image": "x", "volumes": [{"Name": "abt-s-1-grant0", "Dest": "/s", "SubPath": "secrets"}]})

    def test_l3_image_save_and_export_refused_in_the_gate(self):
        for path in ("/v5/libpod/images/x/get", "/v5/images/x/get", "/v5/images/get", "/v5/libpod/images/export"):
            with self.subTest(path=path), self.assertRaises(gate.Deny):
                decide(Fake(), "GET", path)


class StartRace(unittest.TestCase):
    """Audit 6 Oct, N1 (confirmed: podman 5.7 stops an initialized container, and the next start re-initialises
    it): a stop between the gate's check and the forwarded start must not re-resolve the sub-paths."""

    def gate(self):
        doc = container("c1", State={"Pid": 4242, "Status": "initialized"})
        return Fake({"containers/web": doc, "containers/c1": doc})

    def test_a_stop_waits_for_a_start_in_progress(self):
        g = self.gate()
        g.verified.add("c1")
        g.hold("c1")                                     # as check_start does, held until the start reply
        done = threading.Event()

        def stopper():
            g.begin_request()
            decide(g, "POST", "/v5/libpod/containers/web/stop")
            g.release_held()
            done.set()
        t = threading.Thread(target=stopper)
        t.start()
        self.assertFalse(done.wait(0.5), "the stop must wait for the start to finish")
        g.release_held()                                 # the start reply came back
        self.assertTrue(done.wait(5), "and then goes through")
        t.join(5)
        for path, method in (("/v5/libpod/containers/web/kill", "POST"), ("/v5/libpod/containers/web", "DELETE"),
                             ("/v5/libpod/containers/web/pause", "POST")):
            g.begin_request()
            decide(g, method, path)
            self.assertEqual([c for c, _ in g._tls.held], ["c1"], path)
            g.release_held()

    def test_reinitialised_after_the_check_is_refused(self):
        g = self.gate()
        g.begin_request()
        g._tls.checked = {"c1": 4242}
        g.objects["containers/c1"]["State"] = {"Pid": 5555, "Status": "running"}     # a new process
        with self.assertRaises(gate.Deny):
            g.after_start_checked()
        self.assertTrue(any("c1" in d for d in g.deleted))

    def test_same_process_rechecked_strictly_and_exited_accepted(self):
        g = self.gate()
        g.begin_request()
        g._tls.checked = {"c1": 4242}
        doc = g.objects["containers/c1"]
        doc["Mounts"] = [{"Type": "bind", "Source": "/proj", "Destination": "/d"}]
        doc["State"] = {"Pid": 4242, "Status": "running"}
        g.read_mountinfo = lambda pid: (_ for _ in ()).throw(OSError("gone"))
        with self.assertRaises(gate.Deny):               # strict: unreadable while running
            g.after_start_checked()
        doc["State"] = {"Pid": 0, "Status": "exited"}
        g.after_start_checked()                          # exited: the held lock ruled out a re-init


class RunMounts(unittest.TestCase):
    """Audit 6 Oct, N3."""

    def test_source_checked_by_the_gate(self):
        for line in ("RUN --mount=type=cache,target=/c,source=../../.. true",
                     "RUN --mount=type=bind,source=/etc,target=/e true",
                     "RUN --mount=type=bind,source=a/../../x,target=/e true",
                     "RUN --mount=type=cache,target=/c,src=/home true",
                     "RUN --mount=type=bind,source=x,target=/e,bind-propagation=rshared true",
                     "RUN --mount=type=bind,source=x,target=/e,relabel=shared true"):
            with self.subTest(line=line), self.assertRaises(gate.Deny):
                gate.dockerfile_images(f"FROM busybox\n{line}\n", {})
        for line in ("RUN --mount=type=cache,target=/c true", "RUN --mount=type=bind,source=src,target=/s true",
                     "RUN --mount=type=bind,from=busybox,source=/bin/sh,target=/x true",
                     "RUN --mount=type=secret,id=s true", "RUN --mount=type=tmpfs,target=/t true"):
            with self.subTest(line=line):
                gate.dockerfile_images(f"FROM busybox\n{line}\n", {})


class TrialFindings(unittest.TestCase):
    """The trial (2 Oct): what real podman-compose needed."""

    def test_stop_signal_annotations_from_podman_are_not_client_set(self):
        self.assertEqual([gate.signal_number(v) for v in ("SIGTERM", "TERM", "15", 15, "SIGINT", "", None, "BOGUS")],
                         [15, 15, 15, 15, 2, None, None, None])
        g = Fake()
        doc = container("c1")
        doc["Config"].update(StopSignal="SIGTERM", StopTimeout=10, Annotations={
            "io.container.manager": "libpod", "org.opencontainers.image.stopSignal": "15",
            "org.systemd.property.KillSignal": "15", "org.systemd.property.TimeoutStopUSec": "uint64 10000000"})
        self.assertEqual(g.container_violations(doc), [])
        doc["Config"]["Annotations"]["org.systemd.property.KillSignal"] = "9"          # not what podman derives
        self.assertTrue(g.container_violations(doc))
        doc["Config"]["Annotations"]["org.systemd.property.KillSignal"] = "15"
        doc["Config"]["Annotations"]["org.systemd.property.MemoryMax"] = "infinity"    # never from podman
        self.assertTrue(g.container_violations(doc))

    def test_image_annotations_compared_by_value(self):
        g = Fake({"images/img1": {"Id": "img1", "Annotations": {"org.opencontainers.image.version": "1.36",
                                                                 "run.oci.keep_original_groups": "1"}}})
        doc = container("c1", Image="img1")
        doc["Config"]["Annotations"] = {"org.opencontainers.image.version": "1.36"}
        self.assertEqual(g.container_violations(doc), [])
        doc["Config"]["Annotations"] = {"run.oci.keep_original_groups": "1"}   # refused even from the image
        self.assertTrue(g.container_violations(doc))

    def test_no_proxy_in_both_spellings_is_two_variables(self):
        _t, body, _k = decide(Fake(), "POST", "/v5/libpod/containers/create",
                              {"image": "x", "env": {"NO_PROXY": "db", "no_proxy": "db"}})
        spec = json.loads(body)
        merged = dict(spec.get("env") or {})
        merged.update(e.split("=", 1) for e in spec.get("envmerge") or [])
        self.assertEqual((merged.get("NO_PROXY"), merged.get("no_proxy")), ("db", "db"))
        lowered = [k.lower() for k in spec["env"]]
        self.assertEqual(len(lowered), len(set(lowered)), "one spelling per key in the env object (sockguard refuses two)")
        with self.assertRaises(gate.Deny):          # field names still may not differ only in case
            decide(Fake(), "POST", "/v5/libpod/containers/create", {"image": "x", "Image": "y"})

    def test_case_duplicate_field_names_inside_data_maps_still_refused(self):
        """Review of 785ee15, M2: the exemption is the data map's own keys, at its exact path."""
        for path, body in (
                ("/v5/libpod/containers/create", {"image": "x", "Networks": {"n": {"aliases": [], "Aliases": []}}}),
                ("/v5/libpod/containers/create", {"image": "x", "volumes": [{"Name": "a", "name": "b", "Dest": "/x"}]}),
                ("/v5/libpod/containers/create", {"image": "x", "mounts": [{"options": {"a": 1, "A": 2}}]}),
                ("/v5/containers/create", {"Image": "x", "HostConfig": {"PortBindings": {
                    "80/tcp": [{"HostPort": "1", "hostport": "2"}]}}}),
                ("/v5/networks/create", {"Name": "n", "IPAM": {"Config": [{"Subnet": "a", "subnet": "b"}]}}),
                ("/v5/libpod/containers/create", {"image": "x", "healthconfig": {"options": {"a": 1, "A": 2}}})):
            with self.subTest(body=body), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", path, body)
        # the data maps themselves may
        decide(Fake(), "POST", "/v5/libpod/containers/create",
               {"image": "x", "labels": {"A": "1", "a": "2"}, "annotations": {"note": "1", "NOTE": "2"}})

    def test_envmerge_edges(self):
        """Review of 785ee15, L4."""
        with self.assertRaises(gate.Deny):          # a moved value with a $ would be expanded
            decide(Fake(), "POST", "/v5/libpod/containers/create", {"image": "x", "env": {"A": "1", "a": "$HOME"}})
        _t, body, _k = decide(Fake(), "POST", "/v5/libpod/containers/create",
                              {"image": "x", "env": {"Ab": "1", "aB": "2", "AB": "3"}, "envmerge": ["Z=9"]})
        spec = json.loads(body)
        self.assertEqual(sorted(spec["envmerge"]), ["AB=3", "Z=9", "aB=2"])
        self.assertEqual([k for k in spec["env"] if k.lower() == "ab"], ["Ab"])

    def test_lows_of_the_785ee15_review(self):
        # L1: another project's (or the live stack's) container gets the same 404 as a missing one
        g = Fake({"containers/live": container("live1", labels={"io.podman.compose.project": "shop"}),
                  "containers/mine": container("m1")})
        answers = []
        for ref in ("live", "nothere"):
            with self.assertRaises(gate.NotFound) as e:
                decide(g, "POST", f"/v5/libpod/containers/{ref}/stop")
            answers.append(str(e.exception).replace(ref, "X"))
        self.assertEqual(answers[0], answers[1])
        with self.assertRaises(gate.Deny) as e:       # the session's own, not yet checked: 403
            decide(g, "POST", "/v5/libpod/containers/mine/stop")
        self.assertEqual(e.exception.status, 403)
        # L2: present-but-not-allowed and absent pulls get the same answer
        g = Fake({"images/localhost/live-app:latest": {"Id": "b" * 64, "RepoTags": ["localhost/live-app:latest"]}})
        msgs = []
        for ref in ("localhost/live-app:latest", "localhost/nothere:1"):
            with self.assertRaises(gate.Deny) as e:
                decide(g, "POST", f"/v5/libpod/images/pull?reference={ref}&policy=missing")
            msgs.append(str(e.exception))
        self.assertEqual(msgs[0], msgs[1])
        g = Fake({"images/localhost/listed:1": {"Id": "c" * 64, "RepoTags": ["localhost/listed:1"]}})
        g.cfg["images"] = ["localhost/listed:*"]                   # the dev_images branch
        with self.assertRaises(gate.LocalImage):
            decide(g, "POST", "/v5/libpod/images/pull?reference=localhost/listed:1&policy=missing")
        # L3: image annotations only org.opencontainers.*, by value; a removed image allows nothing
        g = Fake({"images/img1": {"Id": "img1", "Annotations": {"org.opencontainers.image.version": "1.36"}}})
        doc = container("c1", Image="img1")
        doc["Config"]["Annotations"] = {"org.opencontainers.image.version": "9.99"}     # value differs
        self.assertTrue(g.container_violations(doc))
        doc = container("c1", Image="gone")
        doc["Config"]["Annotations"] = {"org.opencontainers.image.version": "1.36"}
        self.assertTrue(g.container_violations(doc))
        doc = container("c1", Image="img1")
        doc["Config"].update(StopSignal="SIGTERM", Annotations={"org.opencontainers.image.stopSignal": "2"})
        self.assertTrue(g.container_violations(doc), "stopSignal must match the container's own config")
        # L5: a podman 5xx is a 502, not "no such object"
        g = Fake()
        g.raw_json = lambda path: (500, None)
        with self.assertRaises(gate.UpstreamError) as e:
            decide(g, "POST", "/v5/libpod/containers/x/stop")
        self.assertEqual(e.exception.status, 502)

    def test_depends_on_own_containers_only(self):
        g = Fake({"containers/db": container("db1")})
        with self.assertRaises(gate.Deny):          # not checked yet
            decide(g, "POST", "/v5/libpod/containers/create", {"image": "x", "dependencyContainers": ["db"]})
        g.verified.add("db1")
        _t, body, _k = decide(g, "POST", "/v5/libpod/containers/create", {"image": "x", "dependencyContainers": ["db"]})
        self.assertEqual(json.loads(body)["dependencyContainers"], ["db1"])
        with self.assertRaises(gate.Deny):
            decide(g, "POST", "/v5/libpod/containers/create", {"image": "x", "dependencyContainers": ["live_web"]})
        doc = container("c1", Dependencies=["db1"])
        self.assertEqual(g.container_violations(doc), [])
        self.assertTrue(g.container_violations(container("c1", Dependencies=["other"])))

    def test_pull_of_a_present_own_image_is_answered_locally(self):
        g = Fake({"images/localhost/web:latest": {"Id": "a" * 64, "RepoTags": ["localhost/web:latest"]}})
        with self.assertRaises(gate.Deny):          # present, but not this project's build
            decide(g, "POST", "/v5/libpod/images/pull?reference=localhost/web:latest&policy=missing")
        import tempfile
        from agentbox import builtrec
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        g.cfg["built_record"] = builtrec.path_for(d, "proj")
        builtrec.add(g.cfg["built_record"], "a" * 64, "abt-s-1")
        with self.assertRaises(gate.LocalImage) as e:
            decide(g, "POST", "/v5/libpod/images/pull?reference=localhost/web:latest&policy=missing")
        self.assertEqual(e.exception.image_id, "a" * 64)
        with self.assertRaises(gate.Deny):          # absent: never pulled from localhost
            decide(g, "POST", "/v5/libpod/images/pull?reference=localhost/nothere:1&policy=missing")

    def test_session_pod_operations_go_to_the_service(self):
        pod = {"Id": "pid", "Labels": POD_DEV, "InfraContainerID": "", "Containers": []}
        g = Fake({"pods/p": pod, "pods/pid": pod, "pods/other": {"Id": "oid", "Labels": {}}})
        g.verified.add("pid")
        for method, path in (("DELETE", "/v5/libpod/pods/p"), ("POST", "/v5/libpod/pods/p/stop"),
                             ("GET", "/v5/libpod/pods/p/exists"), ("GET", "/v5/libpod/pods/p/json")):
            self.assertEqual(decide(g, method, path)[2], "pod_raw", path)
        self.assertEqual(decide(g, "POST", "/v5/libpod/pods/p/start")[2], "pod_start")
        # review of 785ee15, M1: forwarded by the checked ID, never the client's name
        for method, path, want in (("DELETE", "/v5/libpod/pods/p?force=true", "/v5/libpod/pods/pid?force=true"),
                                   ("POST", "/v5/libpod/pods/p/stop?t=1", "/v5/libpod/pods/pid/stop?t=1"),
                                   ("POST", "/v5/libpod/pods/p/kill", "/v5/libpod/pods/pid/kill"),
                                   ("GET", "/v5/libpod/pods/p/top", "/v5/libpod/pods/pid/top"),
                                   ("POST", "/v5/libpod/pods/p/start", "/v5/libpod/pods/pid/start")):
            self.assertEqual(decide(g, method, path)[0], want, path)
        with self.assertRaises(gate.Deny):          # not the session's: never to the service
            decide(g, "DELETE", "/v5/libpod/pods/other")
        with self.assertRaises(gate.NotFound):
            decide(g, "DELETE", "/v5/libpod/pods/nothere")

    def test_raw_socket_only_for_session_pod_operations(self):
        """Review of 785ee15: nothing asserted which socket each request reaches."""
        import socket as so
        import tempfile
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        got = {}

        def server(name):
            path = os.path.join(d, name)
            srv = so.socket(so.AF_UNIX)
            srv.bind(path)
            srv.listen(8)

            def loop():
                while True:
                    try:
                        c, _ = srv.accept()
                    except OSError:
                        return
                    req = c.recv(65536).split(b"\r\n", 1)[0].decode()
                    got.setdefault(name, []).append(req)
                    c.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
                    c.close()
            threading.Thread(target=loop, daemon=True).start()
            self.addCleanup(srv.close)
            return path
        pod = {"Id": "pid", "Labels": POD_DEV, "InfraContainerID": "", "Containers": []}
        g = Fake({"pods/p": pod, "pods/pid": pod, "containers/web": container("c1")})
        g.verified.update({"pid", "c1"})
        g.cfg["raw"], g.cfg["upstream"] = server("raw"), server("upstream")
        h = gate.make_handler(g)
        for method, path in (("DELETE", "/v5/libpod/pods/p"), ("POST", "/v5/libpod/pods/p/stop"),
                             ("POST", "/v5/libpod/pods/p/start"), ("POST", "/v5/libpod/containers/web/stop"),
                             ("GET", "/v5/libpod/containers/json")):
            a, b = so.socketpair()
            a.sendall(f"{method} {path} HTTP/1.1\r\nHost: d\r\n\r\n".encode())
            a.shutdown(so.SHUT_WR)
            h(b, None, None)
            a.close()
            b.close()          # the handler may or may not; closing twice is a no-op, leaking is not
        raw = " ".join(got.get("raw", []))
        up = " ".join(got.get("upstream", []))
        for p in ("DELETE /v5/libpod/pods/pid", "POST /v5/libpod/pods/pid/stop", "POST /v5/libpod/pods/pid/start"):
            self.assertIn(p, raw)
        for p in ("containers/web/stop", "containers/json"):
            self.assertIn(p, up)
            self.assertNotIn(p, raw)

    def test_short_name_of_an_own_build(self):
        """Trial 2: compose built localhost/abt-trial_web:latest, then ran "abt-trial_web:latest"."""
        import tempfile
        from agentbox import builtrec
        d = tempfile.mkdtemp(prefix="abt-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        g = Fake({"images/localhost/abt-trial_web:latest": {"Id": "a" * 64, "RepoTags": ["localhost/abt-trial_web:latest"]},
                  "images/localhost/other_web:latest": {"Id": "b" * 64, "RepoTags": ["localhost/other_web:latest"]}})
        g.cfg["built_record"] = builtrec.path_for(d, "proj")
        builtrec.add(g.cfg["built_record"], "a" * 64, "abt-s-1")
        with self.assertRaises(gate.LocalImage) as e:
            decide(g, "POST", "/v5/libpod/images/pull?reference=abt-trial_web:latest&policy=missing")
        self.assertEqual(e.exception.image_id, "a" * 64)
        _t, body, _k = decide(g, "POST", "/v5/libpod/containers/create", {"image": "abt-trial_web"})
        self.assertEqual(json.loads(body)["image"], "localhost/abt-trial_web:latest")
        # not in the record: a short name stays docker.io's
        q = decide(g, "POST", "/v5/libpod/images/pull?reference=other_web&policy=missing")[0]
        self.assertIn("docker.io%2Flibrary%2Fother_web", q)

    def test_missing_objects_are_404(self):
        with self.assertRaises(gate.NotFound):
            decide(Fake(), "GET", "/v5/libpod/containers/nothere/json")

    def test_builds_use_the_proxy_ip_and_session_dns(self):
        from urllib.parse import parse_qsl, urlsplit
        g = Fake()
        g.cfg["build_proxy_env"] = {"http_proxy": "http://10.89.3.2:3128", "HTTPS_PROXY": "http://10.89.3.2:3128"}
        g.cfg["build_dns"] = ["10.89.3.1"]
        q = dict(parse_qsl(urlsplit(g.rewrite_build("/v5/libpod/build?t=x&dnsservers=%5B%228.8.8.8%22%5D")).query))
        self.assertEqual(json.loads(q["buildargs"])["http_proxy"], "http://10.89.3.2:3128")
        self.assertEqual(json.loads(q["dnsservers"]), ["10.89.3.1"])


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


class ProductionConfig(unittest.TestCase):
    """T4: both fakes set infra_pods and compat_creates True so the infra-pod and Docker-API code paths stay
    exercised, but production leaves them off. These two refusals were covered by a single test; here they
    are checked with production's own config."""

    def gate(self):
        g = Fake()
        g.cfg["infra_pods"] = g.cfg["compat_creates"] = False     # production defaults
        return g

    def test_docker_api_container_create_refused(self):
        with self.assertRaises(gate.Deny) as e:
            decide(self.gate(), "POST", "/v5/containers/create", {"Image": "x"})
        self.assertIn("Docker-API container creates are not supported", str(e.exception))

    def test_pod_with_an_infra_container_refused(self):
        with self.assertRaises(gate.Deny) as e:
            self.gate().rewrite_pod({"name": "p"})                # no_infra not set -> an infra pod
        self.assertIn("infra container", str(e.exception))


if __name__ == "__main__":
    unittest.main()
