"""Step 5: the gate's rewrites and refusals, without podman (upstream lookups are faked)."""
import json
import unittest
from urllib.parse import parse_qsl, urlsplit

from agentbox import gate
from agentbox.errors import AgentboxError

CFG = {"session": "abt-s-1", "network": "abt-s-1-int", "labels": {"agentbox": "1", "agentbox.session": "abt-s-1"},
       "proxy_env": {"http_proxy": "http://p:3128", "https_proxy": "http://p:3128", "no_proxy": "localhost"},
       "bind_roots": ["/proj"], "cgroup_parent": "agentbox.slice", "slice": "agentbox.slice", "memory": 1 << 30,
       "pids": 1024, "build_memory": 2 << 30, "holder": "abt-s-1-holder",
       "registries": ["docker.io", "ghcr.io", "lscr.io", "quay.io"], "raw": "/x", "upstream": "/y", "listen": "/z"}


class FakeGate(gate.Gate):
    def __init__(self, labels=None, infra=False):
        super().__init__(dict(CFG))
        self._labels = labels or {}
        self._infra = infra

    def labels_of(self, kind, ref):
        return self._labels.get((kind, ref))

    def pod_has_infra(self, ref):
        return self._infra

    def holder_ns(self):
        return "/proc/4242/ns/net", 4242, "net:[1]"

    def slice_state(self, name):
        return True, "active"


DEV = {gate.DEV_OWNER: "abt-s-1", "agentbox.role": "dev"}


class Containers(unittest.TestCase):
    def test_libpod_create_is_pinned_and_stamped(self):
        g = FakeGate()
        spec = g.rewrite_libpod_container({"image": "x", "Networks": {"proj_default": {"aliases": ["web"]}},
                                           "restart_policy": "always", "env": {"A": "1"}})
        self.assertEqual(spec["Networks"], {"abt-s-1-int": {"aliases": ["web"]}})
        self.assertEqual(spec["netns"], {"nsmode": "bridge"})
        self.assertNotIn("restart_policy", spec)
        self.assertEqual(spec["labels"][gate.DEV_OWNER], "abt-s-1")
        self.assertEqual(spec["labels"]["agentbox.role"], "dev")
        self.assertEqual(spec["systemd"], "false")
        self.assertEqual(spec["cgroup_parent"], "agentbox.slice")
        self.assertEqual(spec["oom_score_adj"], 1000)
        self.assertEqual(spec["resource_limits"]["memory"]["limit"], 1 << 30)
        self.assertEqual(spec["resource_limits"]["pids"]["limit"], 1024)
        self.assertEqual(spec["env"]["A"], "1")
        self.assertEqual(spec["env"]["http_proxy"], "http://p:3128")

    def test_own_limits_kept(self):
        spec = FakeGate().rewrite_libpod_container({"resource_limits": {"memory": {"limit": 5}, "pids": {"limit": 7}}})
        self.assertEqual((spec["resource_limits"]["memory"]["limit"], spec["resource_limits"]["pids"]["limit"]), (5, 7))

    def test_libpod_refusals(self):
        for bad in ({"portmappings": [{"host_port": 1}]}, {"privileged": True}, {"cap_add": ["NET_ADMIN"]},
                    {"devices": [{"path": "/dev/kvm"}]}, {"pidns": {"nsmode": "host"}},
                    {"netns": {"nsmode": "host"}}, {"netns": {"nsmode": "container", "value": "x"}},
                    {"userns": {"nsmode": "host"}}, {"cgroup_parent": "/"},
                    {"mounts": [{"type": "bind", "source": "/etc", "destination": "/e"}]},
                    {"mounts": [{"type": "bind", "source": "/proj/../etc", "destination": "/e"}]}):
            with self.subTest(bad=bad), self.assertRaises(gate.Deny):
                FakeGate().rewrite_libpod_container(dict(bad))

    def test_binds_inside_the_project_allowed(self):
        FakeGate().rewrite_libpod_container({"mounts": [{"type": "bind", "source": "/proj/data", "destination": "/d"}]})

    def test_compat_create(self):
        spec = FakeGate().rewrite_compat_container({"Image": "x", "Env": ["http_proxy=mine"],
            "HostConfig": {"RestartPolicy": {"Name": "always"}, "NetworkMode": "bridge"},
            "NetworkingConfig": {"EndpointsConfig": {"other": {"Aliases": ["db"]}}}})
        self.assertEqual(spec["HostConfig"]["NetworkMode"], "abt-s-1-int")
        self.assertEqual(spec["NetworkingConfig"], {"EndpointsConfig": {"abt-s-1-int": {"Aliases": ["db"]}}})
        self.assertNotIn("RestartPolicy", spec["HostConfig"])
        self.assertEqual(spec["HostConfig"]["OomScoreAdj"], 1000)
        self.assertIn("http_proxy=mine", spec["Env"])
        self.assertEqual(sum(e.startswith("http_proxy=") for e in spec["Env"]), 1, "the container's own value wins")
        for bad in ({"PortBindings": {"80/tcp": [{}]}}, {"Privileged": True}, {"NetworkMode": "host"},
                    {"NetworkMode": "container:abc"}, {"PidMode": "host"}, {"CapAdd": ["SYS_ADMIN"]},
                    {"Binds": ["/:/h"]}, {"CgroupParent": "x"}):
            with self.subTest(bad=bad), self.assertRaises(gate.Deny):
                FakeGate().rewrite_compat_container({"HostConfig": dict(bad)})

    def test_pods(self):
        spec = FakeGate().rewrite_pod({"name": "p"})
        self.assertEqual(spec["Networks"], {"abt-s-1-int": {}})
        spec = FakeGate().rewrite_pod({"name": "p", "no_infra": True})
        self.assertNotIn("Networks", spec, "a pod without infra has no network of its own")
        self.assertEqual(spec["labels"]["agentbox.role"], "dev")
        with self.assertRaises(gate.Deny):
            FakeGate().rewrite_pod({"portmappings": [{"host_port": 1}]})

    def test_container_in_a_pod(self):
        g = FakeGate(labels={("pods", "p"): DEV}, infra=False)
        spec = g.rewrite_libpod_container({"pod": "p"})
        self.assertNotIn("pod", spec, "membership of an infra-less pod is dropped")
        self.assertEqual(spec["Networks"], {"abt-s-1-int": {}})
        g = FakeGate(labels={("pods", "p"): DEV}, infra=True)
        spec = g.rewrite_libpod_container({"pod": "p"})
        self.assertEqual(spec["pod"], "p")
        with self.assertRaises(gate.Deny):
            FakeGate(labels={("pods", "p"): {"agentbox.role": "dev"}}).rewrite_libpod_container({"pod": "p"})

    def test_networks_and_volumes(self):
        self.assertTrue(FakeGate().rewrite_network({"name": "n"})["internal"])
        self.assertTrue(FakeGate().rewrite_network({"Name": "n"})["Internal"])
        self.assertEqual(FakeGate().rewrite_volume({"Name": "v"})["Labels"][gate.DEV_OWNER], "abt-s-1")
        for bad in ({"Name": "v", "Options": {"o": "bind", "device": "/"}}, {"Name": "v", "Driver": "nfs"},
                    {"Name": "v", "DriverOpts": {"device": "/"}}):
            with self.subTest(bad=bad), self.assertRaises(gate.Deny):
                FakeGate().rewrite_volume(dict(bad))


class PullsAndBuilds(unittest.TestCase):
    def query(self, target):
        return dict(parse_qsl(urlsplit(target).query, keep_blank_values=True))

    def test_pull_normalised_and_allowlisted(self):
        g = FakeGate()
        q = self.query(g.rewrite_pull("/v5/libpod/images/pull?reference=busybox:1.36", "/v5/libpod/images/pull"))
        self.assertEqual(q["reference"], "docker.io/library/busybox:1.36")
        q = self.query(g.rewrite_pull("/v5/images/create?fromImage=acme/app&tag=2", "/v5/images/create"))
        self.assertEqual(q["fromImage"], "docker.io/acme/app:2")
        q = self.query(g.rewrite_pull("/v5/libpod/images/pull?reference=docker://ghcr.io/a/b:1",
                                      "/v5/libpod/images/pull"))
        self.assertEqual(q["reference"], "ghcr.io/a/b:1")
        for bad in ("/v5/libpod/images/pull?reference=registry.example.com/x:1",
                    "/v5/libpod/images/pull?reference=localhost:5000/x",
                    "/v5/libpod/images/pull?reference=a&reference=b"):
            with self.subTest(bad=bad), self.assertRaises(gate.Deny):
                g.rewrite_pull(bad, "/v5/libpod/images/pull")
        with self.assertRaises(gate.Deny):
            g.rewrite_pull("/v5/images/create?fromSrc=http://x/a.tar", "/v5/images/create")

    def test_build_query(self):
        g = FakeGate()
        target = ("/v5/libpod/build?t=x&networkmode=1&nsoptions=" +
                  '[{"Name":"network","Path":"/proc/1/ns/net"},{"Name":"user","Host":true},{"Name":"ipc","Path":"/x"}]'
                  + "&seccomp=/etc/x.json&isolation=1&pull=true&buildargs=%7B%22A%22%3A%221%22%7D&memory=99")
        q = self.query(g.rewrite_build(target))
        self.assertEqual(q["networkmode"], "2")
        ns = json.loads(q["nsoptions"])
        self.assertEqual(ns[0], {"Name": "network", "Host": False, "Path": "/proc/4242/ns/net"})
        self.assertEqual([o["Name"] for o in ns], ["network", "user"], "only the client's user entry is kept")
        for k in ("seccomp", "isolation"):
            self.assertNotIn(k, q)
        self.assertEqual((q["pull"], q["pullpolicy"]), ("false", "never"))
        self.assertEqual(json.loads(q["buildargs"])["A"], "1")
        self.assertEqual(json.loads(q["buildargs"])["http_proxy"], "http://p:3128")
        self.assertEqual(q["memory"], str(2 << 30))
        self.assertTrue(q["cgroupparent"].endswith("/agentbox.slice/abt-s-1-build"))
        self.assertEqual(json.loads(q["labels"])[gate.DEV_OWNER], "abt-s-1")
        for bad in ("/v5/libpod/build?cgroupparent=/", "/v5/libpod/build?remote=http://x/ctx.tar",
                    "/v5/libpod/build?volume=/:/h"):
            with self.subTest(bad=bad), self.assertRaises((gate.Deny, AgentboxError)):
                g.rewrite_build(bad)

    def test_build_refused_when_the_holder_changes(self):
        g = FakeGate()
        seen = iter([("/proc/1/ns/net", 1, "net:[1]"), ("/proc/2/ns/net", 2, "net:[2]")])
        g.holder_ns = lambda: next(seen)
        with self.assertRaises(gate.Deny):
            g.rewrite_build("/v5/libpod/build?t=x")


class Scoping(unittest.TestCase):
    def decide(self, g, method, path):
        return g.decide(method, path, [], b"")

    def test_named_resources_must_be_the_sessions_dev_stack(self):
        g = FakeGate(labels={("containers", "web"): DEV,
                             ("containers", "proxy"): {"agentbox.session": "abt-s-1", "agentbox.role": "proxy"},
                             ("containers", "live"): {"io.podman.compose.project": "shop"},
                             ("containers", "other"): {gate.DEV_OWNER: "abt-t-1", "agentbox.role": "dev"}})
        self.decide(g, "POST", "/v5/libpod/containers/web/stop")
        self.decide(g, "GET", "/v5/containers/web/json")
        for ref in ("proxy", "live", "other", "missing"):
            for method, path in (("POST", f"/v5/libpod/containers/{ref}/stop"), ("DELETE", f"/v5/containers/{ref}"),
                                 ("GET", f"/v5/libpod/containers/{ref}/logs"),
                                 ("POST", f"/v5/libpod/containers/{ref}/exec")):
                with self.subTest(ref=ref, path=path), self.assertRaises(gate.Deny):
                    self.decide(g, method, path)

    def test_collections_pass_and_the_session_network_may_be_read(self):
        g = FakeGate()
        for path in ("/v5/libpod/containers/json", "/v5/libpod/containers/stats", "/v5/libpod/pods/json"):
            self.decide(g, "GET", path)
        self.decide(g, "GET", "/v5/libpod/networks/abt-s-1-int/json")
        with self.assertRaises(gate.Deny):
            self.decide(g, "DELETE", "/v5/libpod/networks/abt-s-1-int")

    def test_exec_sessions_only_if_created_here(self):
        g = FakeGate()
        with self.assertRaises(gate.Deny):
            self.decide(g, "POST", "/v5/libpod/exec/abc123/start")
        g.exec_ids.add("abc123")
        self.decide(g, "POST", "/v5/libpod/exec/abc123/start")

    def test_short_names(self):
        self.assertEqual(gate.normalize_ref("python:3.12"), "docker.io/library/python:3.12")
        self.assertEqual(gate.normalize_ref("org/app"), "docker.io/org/app")
        self.assertEqual(gate.normalize_ref("quay.io/a/b"), "quay.io/a/b")
        self.assertEqual(gate.normalize_ref("localhost/x"), "localhost/x")


if __name__ == "__main__":
    unittest.main()
