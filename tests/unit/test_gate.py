"""Step 5: the gate's rewrites and refusals, without podman (upstream lookups are faked)."""
import json
import unittest
from urllib.parse import parse_qsl, urlsplit

from agentbox import gate
from agentbox.errors import AgentboxError

CFG = {"subnets": ["10.89.3.0/24"], "session": "abt-s-1", "network": "abt-s-1-int", "labels": {"agentbox": "1", "agentbox.session": "abt-s-1"},
       "proxy_env": {"http_proxy": "http://p:3128", "https_proxy": "http://p:3128", "no_proxy": "localhost"},
       "bind_roots": ["/proj"], "cgroup_parent": "agentbox.slice", "slice": "agentbox.slice", "memory": 1 << 30,
       "pids": 1024, "build_memory": 2 << 30, "holder": "abt-s-1-holder",
       "registries": ["docker.io", "ghcr.io", "lscr.io", "quay.io"], "raw": "/x", "upstream": "/y", "listen": "/z"}


class FakeGate(gate.Gate):
    """Objects named in `labels` exist with those labels and count as having passed the post-create check."""

    def __init__(self, labels=None, infra=False):
        super().__init__(dict(CFG, infra_pods=True, compat_creates=True))
        self._labels = labels or {}
        self._infra = infra
        self.verified.update(ref for _kind, ref in self._labels)
        self.volumes = {}

    def inspect(self, kind, ref):
        if kind == "volumes" and ref in self.volumes:
            return self.volumes[ref]
        labels = self._labels.get((kind, ref))
        if labels is None:
            return None
        return {"Id": ref, "Name": ref, "Config": {"Labels": labels}, "Labels": labels}

    images = {}                      # ref -> image doc; any other ref is a public docker.io image

    def image_doc(self, ref):
        first = ref.split("/", 1)[0]
        if ref not in self.images and "/" in ref and ("." in first or ":" in first or first == "localhost"):
            return None                      # a registry-qualified name the fake store does not have
        return self.images.get(ref, {"RepoTags": [f"docker.io/library/{ref}"], "Labels": {}})

    def raw_request(self, method, path, body=None):
        if path.endswith("/volumes/create"):
            self.volumes[body["Name"]] = {"Name": body["Name"], "Driver": body.get("Driver", "local"),
                                          "Labels": body.get("Label") or {}, "Options": body.get("Options") or {}}
        return 201

    def pod_has_infra(self, ref):
        return self._infra

    def holder_ns(self):
        return "/proc/4242/ns/net", 4242, "net:[1]"

    def network_names(self):
        return {"proj_web_1", "web", "db"}

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
        spec = FakeGate().rewrite_libpod_container({"image": "x", "resource_limits": {"memory": {"limit": 5}, "pids": {"limit": 7}}})
        self.assertEqual((spec["resource_limits"]["memory"]["limit"], spec["resource_limits"]["pids"]["limit"]), (5, 7))

    def test_libpod_refusals(self):
        for bad in ({"portmappings": [{"host_port": 1}]}, {"privileged": True}, {"cap_add": ["NET_ADMIN"]},
                    {"devices": [{"path": "/dev/kvm"}]}, {"pidns": {"nsmode": "host"}},
                    {"netns": {"nsmode": "host"}}, {"netns": {"nsmode": "container", "value": "x"}},
                    {"userns": {"nsmode": "host"}}, {"cgroup_parent": "/"},
                    {"mounts": [{"type": "bind", "source": "/etc", "destination": "/e"}]},
                    {"mounts": [{"type": "bind", "source": "/proj/../etc", "destination": "/e"}]}):
            with self.subTest(bad=bad), self.assertRaises(gate.Deny):
                FakeGate().rewrite_libpod_container(dict(bad, image="x"))

    def test_sub_path_binds_become_grant_volume_sub_paths(self):
        """Tim, 1 Oct: a path below a grant root could be swapped for a symlink before start. A grant root is
        bound as is; a sub-path becomes a SubPath of the gate's bind-backed volume over that root."""
        spec = FakeGate().rewrite_libpod_container({"image": "x", "mounts": [{"type": "bind", "source": "/proj", "destination": "/d"}]})
        self.assertEqual(spec["mounts"][0]["source"], "/proj")
        g = FakeGate()
        spec = g.rewrite_libpod_container({"image": "x", "mounts": [{"type": "bind", "source": "/proj/data/x", "destination": "/d",
                                                       "options": ["ro", "rbind", "rshared"]}]})
        self.assertEqual(spec["mounts"], [])
        self.assertEqual(spec["volumes"], [{"Name": "abt-s-1-grant0", "Dest": "/d", "SubPath": "data/x",
                                            "Options": ["ro"]}])
        vol = g.volumes["abt-s-1-grant0"]
        self.assertEqual(vol["Options"], {"type": "none", "o": "bind", "device": "/proj"})
        self.assertEqual((vol["Labels"][gate.DEV_OWNER], vol["Labels"]["agentbox.role"]), ("abt-s-1", "grant"))
        with self.assertRaises(gate.Deny):          # the agent cannot name the grant volume itself
            g.decide("DELETE", "/v5/libpod/volumes/abt-s-1-grant0", [], b"")
        with self.assertRaises(gate.Deny) as e:     # pods keep option 2
            FakeGate().rewrite_pod({"name": "p", "no_infra": True,
                                    "mounts": [{"type": "bind", "source": "/proj/data", "destination": "/d"}]})
        self.assertIn("only a grant root itself", str(e.exception))

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
        spec = g.rewrite_libpod_container({"image": "x", "pod": "p"})
        self.assertNotIn("pod", spec, "membership of an infra-less pod is dropped")
        self.assertEqual(spec["Networks"], {"abt-s-1-int": {}})
        g = FakeGate(labels={("pods", "p"): DEV}, infra=True)
        spec = g.rewrite_libpod_container({"image": "x", "pod": "p"})
        self.assertEqual(spec["pod"], "p")
        with self.assertRaises(gate.Deny):
            FakeGate(labels={("pods", "p"): {"agentbox.role": "dev"}}).rewrite_libpod_container({"image": "x", "pod": "p"})

    def test_networks_and_volumes(self):
        self.assertTrue(FakeGate().rewrite_network({"name": "n"})["internal"])
        self.assertTrue(FakeGate().rewrite_network({"Name": "n"})["Internal"])
        # podman's libpod volume API reads "Label", the Docker API "Labels" (found 1 Oct: the old stamp under
        # "Labels" was ignored by the libpod API)
        self.assertEqual(FakeGate().rewrite_volume({"Name": "v"})["Label"][gate.DEV_OWNER], "abt-s-1")
        self.assertEqual(FakeGate().rewrite_volume({"Name": "v"}, compat=True)["Labels"][gate.DEV_OWNER], "abt-s-1")
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
        g.exec_ids["abc123"] = "web"
        g.verified.add("web")
        self.decide(g, "POST", "/v5/libpod/exec/abc123/start")

    def test_short_names(self):
        self.assertEqual(gate.normalize_ref("python:3.12"), "docker.io/library/python:3.12")
        self.assertEqual(gate.normalize_ref("org/app"), "docker.io/org/app")
        self.assertEqual(gate.normalize_ref("quay.io/a/b"), "quay.io/a/b")
        self.assertEqual(gate.normalize_ref("localhost/x"), "localhost/x")


if __name__ == "__main__":
    unittest.main()


class NoProxy(unittest.TestCase):
    def test_names_on_the_network_and_own_aliases_go_direct(self):
        spec = FakeGate().rewrite_libpod_container({"image": "x", "name": "proj_api_1",
                                                    "Networks": {"proj_default": {"aliases": ["api"]}}})
        parts = spec["env"]["no_proxy"].split(",")
        self.assertEqual(parts[:3], ["localhost", "127.0.0.1", "10.89.3.0/24"])
        for name in ("web", "db", "proj_web_1", "api", "proj_api_1"):
            self.assertIn(name, parts)
        self.assertNotIn("NO_PROXY", spec["env"], "one spelling only (sockguard refuses case variants)")

    def test_the_compose_files_own_value_wins(self):
        spec = FakeGate().rewrite_libpod_container({"image": "x", "env": {"no_proxy": "mine"}})
        self.assertEqual(spec["env"]["no_proxy"], "mine")
        spec = FakeGate().rewrite_compat_container({"Env": ["no_proxy=mine"], "HostConfig": {}})
        self.assertEqual([e for e in spec["Env"] if e.startswith("no_proxy=")], ["no_proxy=mine"])


class SpellingAndVerification(unittest.TestCase):
    """Found 1 Oct (from the step 5 review brief, item 7): "PortMappings" got a published host port past the
    gate and sockguard, because podman matches JSON field names case-insensitively."""

    def decide(self, path, body):
        return FakeGate().decide("POST", path, [], json.dumps(body).encode())

    def test_case_variants_are_checked_like_the_exact_spelling(self):
        for body in ({"image": "x", "PortMappings": [{"host_port": 18081, "container_port": 80}]},
                     {"image": "x", "PRIVILEGED": True}, {"image": "x", "Cap_Add": ["NET_ADMIN"]},
                     {"image": "x", "NetNS": {"NsMode": "host"}},
                     {"image": "x", "Mounts": [{"Type": "bind", "Source": "/etc", "Destination": "/e"}]}):
            with self.subTest(body=body), self.assertRaises(gate.Deny):
                self.decide("/v5/libpod/containers/create", body)
        for hc in ({"portbindings": {"80/tcp": [{}]}}, {"privileged": True}, {"NETWORKMODE": "host"}):
            with self.subTest(hc=hc), self.assertRaises(gate.Deny):
                self.decide("/v5/containers/create", {"Image": "x", "hostconfig": hc})

    def test_keys_differing_only_in_case_are_refused(self):
        with self.assertRaises(gate.Deny):
            FakeGate().decide("POST", "/v5/libpod/containers/create", [],
                              b'{"image": "x", "privileged": false, "Privileged": true}')
        with self.assertRaises(gate.Deny):
            FakeGate().decide("POST", "/v5/libpod/volumes/create", [], b'{"Name": "v", "Options": {}, "options": {"o": "bind"}}')

    def test_volume_options_in_any_spelling_refused(self):
        with self.assertRaises(gate.Deny):
            self.decide("/v5/libpod/volumes/create", {"name": "v", "options": {"o": "bind", "device": "/"}})

    def good_doc(self):
        return {"Config": {"Labels": DEV}, "Mounts": [], "NetworkSettings": {"Networks": {"abt-s-1-int": {}}},
                "HostConfig": {"PortBindings": {}, "CgroupParent": "/user.slice/x/agentbox.slice", "OomScoreAdj": 1000,
                               "RestartPolicy": {"Name": "no"}, "NetworkMode": "bridge", "Memory": 1 << 30,
                               "MemorySwap": 1 << 30, "PidsLimit": 1024}}

    def test_post_create_verification(self):
        g = FakeGate()
        self.assertEqual(g.container_violations(self.good_doc()), [])
        for change, what in (({"PortBindings": {"80/tcp": [{"HostPort": "18081"}]}}, "published ports"),
                             ({"Privileged": True}, "privileged"), ({"CapAdd": ["CAP_NET_ADMIN"]}, "capabilities"),
                             ({"PidMode": "host"}, "PidMode"), ({"NetworkMode": "ns:/proc/1/ns/net"}, "NetworkMode"),
                             ({"CgroupParent": "/"}, "cgroup parent"), ({"OomScoreAdj": 0}, "OOM"),
                             ({"RestartPolicy": {"Name": "always"}}, "restart")):
            doc = self.good_doc()
            doc["HostConfig"].update(change)
            with self.subTest(change=change):
                self.assertTrue(any(what in v for v in g.container_violations(doc)), g.container_violations(doc))
        doc = self.good_doc()
        doc["NetworkSettings"]["Networks"] = {"abt-s-1-int": {}, "podman": {}}
        self.assertTrue(g.container_violations(doc))
        doc = self.good_doc()
        doc["Mounts"] = [{"Type": "bind", "Source": "/home/vmuser"}]
        self.assertTrue(g.container_violations(doc))
