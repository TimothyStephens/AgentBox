"""The `ab deploy` contract as a check (deploycheck.py; design.md "What `ab deploy` deploys")."""
import unittest

from agentbox import deploycheck

LAN = "192.0.2.10"                 # a documentation address (RFC 5737)
# A typical hand-run stack before it was made deployable: compose in deploy/, live data and a dump folder
# bound from inside the repo with `:U`, an external network shared with a reverse proxy, a loopback port.
HANDRUN = {
    "x-podman": {"in_pod": False},
    "services": {
        "web": {
            "build": {"context": "..", "dockerfile": "deploy/Containerfile"}, "container_name": "webapp-web",
            "restart": "always", "env_file": "../.env",
            "volumes": ["../data:/data:U", "../config:/config:ro"],
            "ports": [f"{LAN}:8090:8000", "127.0.0.1:8090:8000"], "networks": ["edge"]},
        "worker": {
            "build": {"context": "..", "dockerfile": "deploy/Containerfile"}, "restart": "always",
            "env_file": "../.env",
            "volumes": ["../data:/data:U", "../config:/config:ro", "../dumps:/dump:U"],
            "networks": ["edge"]}},
    "networks": {"edge": {"name": "edge", "external": True}},
}
TRACKED = ["deploy/compose.yaml", "deploy/Containerfile", "config/app.yaml", "app/main.py", ".env.dev"]
CTX = {"compose": "deploy/compose.yaml", "lan_addr": LAN, "ports": [8090], "binds": [], "external": [],
       "adopted": [], "compose_project": "webapp"}


def fails(report):
    return [(rule, msg) for level, rule, msg in report.lines if level == "FAIL"]


class AHandRunStack(unittest.TestCase):
    def test_as_it_is_the_data_binds_and_the_unlisted_network_fail_and_nothing_else(self):
        r = deploycheck.check(HANDRUN, CTX, TRACKED)
        self.assertEqual(sorted({rule for rule, _m in fails(r)}), ["external", "state"], r.text())
        text = r.text()
        self.assertIn("data is not committed", text)
        self.assertIn("dumps is not committed", text)
        self.assertIn("podman unshare chown", text, "and the `:U` the gate refuses is named, with the fix")
        self.assertIn("--deploy-external edge", text)
        self.assertNotIn("config is not committed", text, "a committed config dir read from the copy is fine")
        self.assertNotIn("WARN  set-by-deploy", text, "restart: always is already what the deploy sets")
        r2 = deploycheck.check({"services": {"w": {"image": "busybox", "restart": "unless-stopped"}}}, {}, [])
        self.assertIn("WARN  set-by-deploy", r2.text())

    def test_after_the_migration_it_passes(self):
        doc = {**HANDRUN, "services": {k: dict(v) for k, v in HANDRUN["services"].items()}}
        doc["services"]["web"]["volumes"] = ["/srv/webapp/data:/data", "../config:/config:ro"]
        doc["services"]["worker"]["volumes"] = ["appdata:/data", "../config:/config:ro"]
        doc["volumes"] = {"appdata": {}}
        r = deploycheck.check(doc, dict(CTX, binds=["/srv/webapp"], external=["edge"]), TRACKED)
        self.assertFalse(r.failed, r.text())


class Rules(unittest.TestCase):
    def one(self, service, ctx=None, tracked=("compose.yaml",), top=None):
        doc = {"services": {"web": service}, **(top or {})}
        return deploycheck.check(doc, dict({"lan_addr": LAN, "ports": [8090]}, **(ctx or {})), tracked)

    def test_privilege_and_host_namespaces(self):
        for svc in ({"privileged": True}, {"cap_add": ["NET_ADMIN"]}, {"devices": ["/dev/dri"]},
                    {"security_opt": ["seccomp=unconfined"]}, {"pid": "host"}, {"network_mode": "host"},
                    {"network_mode": "container:x"}, {"userns_mode": "host"}, {"sysctls": {"a": 1}}):
            with self.subTest(svc=svc):
                self.assertTrue(self.one(dict(svc, image="docker.io/library/busybox")).failed)

    def test_ports(self):
        cases = {"8090:80": False, f"{LAN}:8090:80": False, "127.0.0.1:8090:80": False,
                 "80": True, "8090-8091:80-81": True, "10.0.0.5:8090:80": True, "8091:80": True,
                 "0.0.0.0:8090:80": False}
        for spec, bad in cases.items():
            with self.subTest(spec=spec):
                self.assertEqual(self.one({"image": "busybox", "ports": [spec]}).failed, bad)
        self.assertFalse(self.one({"image": "busybox",
                                   "ports": [{"target": 80, "published": 8090, "host_ip": "127.0.0.1"}]}).failed)
        self.assertTrue(self.one({"image": "busybox", "ports": [{"target": 80}]}).failed)
        first = self.one({"image": "busybox", "ports": ["8090:80", f"{LAN}:8090:80"]},
                         ctx={"ports": [], "lan_addr": ""})
        self.assertFalse(first.failed, "a first --check, before --lan stored anything, only warns")
        self.assertIn("no deploy ports set yet", first.text())
        self.assertIn("none stored yet", first.text())

    def test_binds(self):
        self.assertFalse(self.one({"image": "busybox", "volumes": ["/srv/x/data:/d"]},
                                  ctx={"binds": ["/srv/x"]}).failed)
        self.assertTrue(self.one({"image": "busybox", "volumes": ["/srv/xy:/d"]}, ctx={"binds": ["/srv/x"]}).failed)
        self.assertTrue(self.one({"image": "busybox", "volumes": ["/home/u:/d"]}).failed)
        self.assertTrue(self.one({"image": "busybox", "volumes": ["~/data:/d"]}).failed)
        self.assertTrue(self.one({"image": "busybox", "volumes": ["../outside:/d"]}).failed)
        self.assertFalse(self.one({"image": "busybox", "volumes": ["/etc/localtime:/etc/localtime:ro"]}).failed)
        self.assertTrue(self.one({"image": "busybox", "volumes": ["/etc/localtime:/etc/localtime"]}).failed)
        self.assertFalse(self.one({"image": "busybox", "volumes": ["data:/d", "/cache"]}).failed)
        self.assertFalse(self.one({"image": "busybox", "volumes": ["./www:/www:ro"]},
                                  tracked=("compose.yaml", "www/index.html")).failed)
        self.assertTrue(self.one({"image": "busybox", "volumes": ["./www:/www:ro"]}).failed)
        self.assertTrue(self.one({"image": "busybox", "volumes": ["/srv/x:/d:U"]}, ctx={"binds": ["/srv/x"]}).failed)
        self.assertTrue(self.one({"image": "busybox", "volumes": ["/srv/x:/d:rw,chown"]},
                                 ctx={"binds": ["/srv/x"]}).failed)
        self.assertFalse(self.one({"image": "busybox", "volumes": ["/srv/x:/d:Z"]}, ctx={"binds": ["/srv/x"]}).failed)
        long = {"type": "bind", "source": "/srv/x", "target": "/d"}
        self.assertFalse(self.one({"image": "busybox", "volumes": [long]}, ctx={"binds": ["/srv/x"]}).failed)

    def test_external_volumes_and_networks_need_listing(self):
        top = {"volumes": {"v": {"external": True, "name": "media"}}}
        svc = {"image": "busybox", "volumes": ["v:/m"]}
        self.assertTrue(self.one(svc, top=top).failed)
        self.assertFalse(self.one(svc, top=top, ctx={"external": ["media"]}).failed)
        self.assertFalse(self.one(svc, top=top, ctx={"adopted": ["media"]}).failed)
        self.assertTrue(self.one({"image": "busybox"}, top={"networks": {"n": {"driver": "macvlan"}}}).failed)

    def test_images_and_build(self):
        self.assertFalse(self.one({"image": "python:3.12"}).failed, "a short name is docker.io")
        self.assertFalse(self.one({"image": "ghcr.io/o/x:1"}).failed)
        self.assertTrue(self.one({"image": "registry.example.com/x"}).failed)
        self.assertTrue(self.one({"build": {"context": "../.."}}).failed)
        self.assertTrue(self.one({"build": {"context": ".", "dockerfile": "Containerfile"}}).failed,
                        "an uncommitted Containerfile is not in the copy")
        self.assertFalse(self.one({"build": {"context": ".", "dockerfile": "Containerfile"}},
                                  tracked=("compose.yaml", "Containerfile")).failed)

    def test_env_file_must_be_the_root_env(self):
        self.assertFalse(self.one({"image": "busybox", "env_file": ".env"}).failed)
        self.assertTrue(self.one({"image": "busybox", "env_file": "sub/.env"}).failed)
        self.assertTrue(self.one({"image": "busybox", "env_file": "../.env"}).failed)
        self.assertFalse(self.one({"image": "busybox", "env_file": ["config/app.env"]}).failed)

    def test_include_and_extends_are_refused(self):
        self.assertTrue(deploycheck.check({"include": ["x.yaml"], "services": {"a": {"image": "busybox"}}},
                                          {}, []).failed)
        self.assertTrue(self.one({"extends": {"file": "x.yaml", "service": "a"}}).failed)

    def test_no_services_fails(self):
        self.assertTrue(deploycheck.check({}, {}, []).failed)
        self.assertTrue(deploycheck.check(None, {}, []).failed)


class InASession(unittest.TestCase):
    """`deploy-check` as the agent runs it: YAML from the working tree, values from the contract file."""

    def setUp(self):
        try:
            import yaml  # noqa: F401  (the agent images carry it for podman-compose; the host may not)
        except ImportError:
            self.skipTest("PyYAML is not installed here")
        import json
        import os
        import subprocess
        import tempfile
        from unittest import mock
        self.tmp = tempfile.mkdtemp(prefix="abt-unit-dc-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.root, "deploy"))
        with open(os.path.join(self.root, "deploy", "compose.yaml"), "w") as f:
            f.write("services:\n  web:\n    image: busybox\n    ports: ['8090:80']\n"
                    "    volumes: ['../data:/data']\n")
        subprocess.run(["git", "init", "-q", self.root], check=True)
        subprocess.run(["git", "-C", self.root, "add", "-A"], check=True)
        contract = os.path.join(self.tmp, "contract.json")
        with open(contract, "w") as f:
            json.dump({"root": self.root, "compose": "deploy/compose.yaml", "lan_addr": LAN, "ports": [8090]}, f)
        p = mock.patch.object(deploycheck, "CONTRACT_PATH", contract)
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, *argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = deploycheck.main(list(argv))
        return code, out.getvalue()

    def test_it_reports_the_uncommitted_data_bind_and_exits_1(self):
        code, out = self.run_main()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL  state", out)
        self.assertIn("PASS  ports", out)

    def test_a_broken_file_is_one_fail_line(self):
        with open(f"{self.root}/deploy/compose.yaml", "w") as f:
            f.write("services: [\n")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("not valid YAML", out)
