"""`ab deploy` against real podman (step 4; docs/design.md "Preview and deploy", decisions.md 17 Nov).

A two-service stack (a web server built from a Containerfile, a worker from a public image, one named volume)
is deployed from a test repo through the whole path: the review, the confirmation on a pty, the per-deploy
podman service, sockguard and gate, and podman-compose in the runner. Checked:
  * the first deploy serves the commit on the LAN address and port, and nothing else is published;
  * the live containers carry the deploy labels and `unless-stopped`, run outside agentbox.slice, and the
    host's default OOM score;
  * the deploy's own helpers (service, sockguard, gate, holder, runner) are gone afterwards;
  * a redeploy of a new commit serves it and keeps the data volume's contents;
  * `--yes` redeploys the same commit from a script;
  * each refusal bites: privileged, a bind outside the copy, a port that is not listed, host networking,
    an `external:` volume that is not listed, and a stack started by hand;
  * `--adopt-volumes` takes a hand-run stack's volume over by name.
Everything is named abt-<run>-* / dep-<run>-* and labelled with the run, and removed at the end.
"""
import json
import os
import socket
import subprocess
import time
import unittest
import urllib.request

from agentbox import egress

from . import harness

ENV = None
BUSYBOX = "docker.io/library/busybox:1.36"
BASE_IMAGE = "localhost/agentbox-base:latest"
LAN = None
PORT = None
MAIN = f"dep-{harness.RUN}"
BAD = f"depbad-{harness.RUN}"
HAND = f"dephand-{harness.RUN}"

COMPOSE = """services:
  web:
    build: .
    command: httpd -f -p 8080 -h /www
    ports: ["{port}:8080"]
    volumes: ["data:/data"]
  worker:
    image: {busybox}
    command: sh -c 'sleep 3600'
    volumes: ["data:/data"]
volumes:
  data: {{}}
"""
CONTAINERFILE = f"FROM {BUSYBOX}\nCOPY www /www\n"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def git(root, *args):
    return subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def make_repo(name, compose, page="v1"):
    root = os.path.join(ENV.projects, name)
    os.makedirs(os.path.join(root, "www"), exist_ok=True)
    files = {"compose.yaml": compose, "Containerfile": CONTAINERFILE, "www/index.html": page + "\n",
             ".gitignore": ".env\n", ".env.dev": "GREETING=dev\n"}
    for rel, text in files.items():
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)
    with open(os.path.join(root, ".env"), "w") as f:
        f.write("GREETING=real\n")
    os.chmod(os.path.join(root, ".env"), 0o600)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "v1")
    ENV.ab("project", "add", name, root, "--preset", "build", check=True)
    return root


def commit(root, rel, text, msg):
    with open(os.path.join(root, rel), "w") as f:
        f.write(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg)


def deploy_tty(*args, answer="y\n", timeout=900):
    """`ab deploy <args>` on a pty, answering the confirmation. Returns (exit code, everything printed)."""
    p, master = ENV.ab_pty("deploy", *args)
    out = ENV.drain(master, until="[y/N]", timeout=300)
    if "[y/N]" in out:
        os.write(master, answer.encode())
    end = time.time() + timeout
    while p.poll() is None and time.time() < end:
        out += ENV.drain(master, timeout=5)
    if p.poll() is None:
        p.kill()
        raise AssertionError(f"`ab deploy {' '.join(args)}` did not finish in {timeout}s:\n{out}")
    out += ENV.drain(master, timeout=2)
    os.close(master)
    return p.returncode, out


def get(path="/index.html", timeout=5):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{LAN}:{PORT}{path}", timeout=timeout) as r:
        return r.read().decode().strip()


def stack(project):
    out = harness.podman("ps", "-a", "--format", "json", "--filter", f"label=agentbox.deploy={project}").stdout
    return json.loads(out or "[]")


def inspect(name):
    return json.loads(harness.podman("inspect", name).stdout or "[{}]")[0]


def records(project):
    path = os.path.join(ENV.data, "projects", project, "deploys.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def setUpModule():
    global ENV, LAN, PORT
    for img in (BUSYBOX, BASE_IMAGE):
        if harness.podman("image", "exists", img).returncode != 0:
            raise unittest.SkipTest(f"{img} not present (make base-image; podman pull {BUSYBOX})")
    addrs = sorted(a for a in egress.host_addresses() if ":" not in a and not a.startswith("127."))
    if not addrs:
        raise unittest.SkipTest("no IPv4 address of this host to deploy on")
    LAN, PORT = addrs[0], _free_port()
    ENV = harness.Env()


def tearDownModule():
    try:
        for name in (MAIN, BAD, HAND):
            for c in stack(name):
                harness.podman("rm", "-f", "-t", "0", c["Id"])
            for img in harness.listing("images", "--format", "{{.Repository}}:{{.Tag}}",
                                       "--filter", f"reference=*{name}*").split():
                harness.podman("rmi", "-f", img)
            for v in harness.listing("volume", "ls", "--format", "{{.Name}}").split():
                if v.startswith(f"{name}_"):
                    harness.podman("volume", "rm", "-f", v)
    finally:
        harness.cleanup()


class A_FirstDeployAndRedeploy(unittest.TestCase):
    """In order: first deploy, then a redeploy of a new commit, then `--yes` of the same commit."""
    root = None

    @classmethod
    def setUpClass(cls):
        cls.root = make_repo(MAIN, COMPOSE.format(port=PORT, busybox=BUSYBOX))

    def test_1_the_first_deploy_serves_the_commit_on_the_lan_address(self):
        code, out = deploy_tty(MAIN, "--lan", f"{LAN}:{PORT}")
        self.assertEqual(code, 0, out)
        self.assertIn("First deploy", out)
        self.assertNotIn("GREETING=real", out, "no .env value is printed")
        self.assertEqual(harness.wait_for(lambda: self._get_or_none(), timeout=60), "v1")
        self.assertEqual(records(MAIN)[-1]["result"], "ok")

    def test_2_the_live_containers_are_what_the_gate_made_them(self):
        live = stack(MAIN)
        self.assertEqual(len(live), 2, live)
        for c in live:
            doc = inspect(c["Id"])
            hc = doc["HostConfig"]
            with self.subTest(container=c["Names"]):
                self.assertEqual(doc["Config"]["Labels"].get("agentbox.role"), "deploy")
                self.assertEqual(hc["RestartPolicy"]["Name"], "unless-stopped")
                self.assertNotIn("agentbox.slice", hc.get("CgroupParent") or "")
                self.assertEqual(hc.get("OomScoreAdj"), 0)
                self.assertFalse(hc.get("Privileged"))
                for port, binds in (hc.get("PortBindings") or {}).items():
                    for b in binds or []:
                        self.assertEqual((b["HostIp"], b["HostPort"]), (LAN, str(PORT)))
        vol = json.loads(harness.podman("volume", "inspect", f"{MAIN}_data").stdout)[0]
        self.assertEqual(vol["Labels"].get("agentbox.deploy"), MAIN)

    def test_3_the_deploys_own_helpers_are_gone(self):
        left = [n for n in harness.listing("ps", "-a", "--format", "{{.Names}}").split()
                if n.startswith(f"abt-{harness.RUN}-deploy-")]
        self.assertEqual(left, [])

    def test_4_a_redeploy_serves_the_new_commit_and_keeps_the_volume(self):
        worker = [c for c in stack(MAIN) if "worker" in c["Names"][0]][0]["Id"]
        harness.podman("exec", worker, "sh", "-c", "echo kept > /data/marker")
        commit(self.root, "www/index.html", "v2\n", "v2")
        code, out = deploy_tty(MAIN)
        self.assertEqual(code, 0, out)
        self.assertIn("Changes since the last deploy", out)
        self.assertEqual(harness.wait_for(lambda: self._get_or_none() == "v2" and "v2", timeout=60), "v2")
        worker = [c for c in stack(MAIN) if "worker" in c["Names"][0]][0]["Id"]
        self.assertEqual(harness.podman("exec", worker, "cat", "/data/marker").stdout.strip(), "kept")

    def test_5_yes_redeploys_the_same_commit_from_a_script(self):
        p = ENV.ab("deploy", MAIN, "--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self._get_or_none(), "v2")

    @staticmethod
    def _get_or_none():
        try:
            return get()
        except OSError:
            return None


class B_Refusals(unittest.TestCase):
    """Each variant is committed in its own project and must be refused by the deploy gate."""
    VARIANTS = {
        "privileged": "    privileged: true\n",
        "bind_outside": "    volumes: [\"/etc:/host-etc:ro\"]\n",
        "host_network": "    network_mode: host\n",
    }

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.root = make_repo(BAD, COMPOSE.format(port=cls.port, busybox=BUSYBOX))

    def deploy_variant(self, compose_text):
        commit(self.root, "compose.yaml", compose_text, "variant")
        return deploy_tty(BAD, "--lan", f"{LAN}:{self.port}")

    def assert_refused(self, code, out):
        self.assertNotEqual(code, 0, out)
        self.assertEqual(records(BAD)[-1]["result"], "failed", out)

    def test_service_level_refusals(self):
        simple = (f"services:\n  web:\n    image: {BUSYBOX}\n    command: sleep 3600\n")
        for name, extra in self.VARIANTS.items():
            with self.subTest(variant=name):
                code, out = self.deploy_variant(simple + extra)
                self.assert_refused(code, out)
                for c in stack(BAD):
                    hc = inspect(c["Id"])["HostConfig"]
                    self.assertFalse(hc.get("Privileged"))
                    self.assertNotEqual(hc.get("NetworkMode"), "host")

    def test_a_port_that_is_not_listed_is_refused(self):
        other = _free_port()
        compose = (f"services:\n  web:\n    image: {BUSYBOX}\n    command: sleep 3600\n"
                   f"    ports: [\"{self.port}:8080\", \"{other}:8081\"]\n")
        code, out = self.deploy_variant(compose)
        self.assert_refused(code, out)
        published = harness.listing("ps", "--format", "{{.Ports}}", "--filter", f"label=agentbox.deploy={BAD}")
        self.assertNotIn(str(other), published)

    def test_an_external_volume_that_is_not_listed_is_refused(self):
        vol = f"abt-{harness.RUN}-foreign"
        harness.podman("volume", "create", "--label", harness.LABEL, "--label", "agentbox-test=1", vol)
        compose = (f"services:\n  web:\n    image: {BUSYBOX}\n    command: sleep 3600\n"
                   f"    volumes: [\"x:/x\"]\nvolumes:\n  x:\n    external: true\n    name: {vol}\n")
        code, out = self.deploy_variant(compose)
        self.assert_refused(code, out)


class C_HandRunStacks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.root = make_repo(HAND, COMPOSE.format(port=cls.port, busybox=BUSYBOX))

    def test_1_a_stack_started_by_hand_is_refused_with_the_down_command(self):
        name = f"{HAND}_web_1"
        harness.podman("run", "-d", "--name", name, "--label", harness.LABEL, "--label", "agentbox-test=1",
                       "--label", f"io.podman.compose.project={HAND}", BUSYBOX, "sleep", "3600")
        try:
            code, out = deploy_tty(HAND, "--lan", f"{LAN}:{self.port}")
            self.assertNotEqual(code, 0, out)
            self.assertIn(f"podman-compose -p {HAND} down", out)
            self.assertTrue(inspect(name).get("Id"), "agentbox never removes what it did not create")
        finally:
            harness.podman("rm", "-f", "-t", "0", name)

    def test_2_its_volume_is_adopted_by_name(self):
        vol = f"{HAND}_data"
        harness.podman("volume", "create", "--label", harness.LABEL, "--label", "agentbox-test=1", vol)
        code, out = deploy_tty(HAND, "--lan", f"{LAN}:{self.port}")
        self.assertNotEqual(code, 0, out)
        self.assertIn("--adopt-volumes", out)
        code, out = deploy_tty(HAND, "--adopt-volumes")
        self.assertEqual(code, 0, out)
        show = ENV.ab("project", "show", HAND).stdout
        self.assertIn(vol, show)
        code, out = deploy_tty(HAND, "--lan", f"{LAN}:{self.port}")
        self.assertEqual(code, 0, out)
        users = [c for c in stack(HAND)
                 if any(m.get("Name") == vol for m in inspect(c["Id"]).get("Mounts") or [])]
        self.assertTrue(users, "the adopted volume is the live stack's")
