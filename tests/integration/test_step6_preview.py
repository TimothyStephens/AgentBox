"""ab session preview against real podman (Tim, 2 Oct): a dev service of a running session, opened on 127.0.0.1 only.

One dev-tier session (gate, sockguard, podman service; no agent needed) with a busybox web server created
through the gate. Checked:
  * `preview.start` serves it on 127.0.0.1:<port>, also to several connections at once;
  * the port listens on 127.0.0.1 only: not 0.0.0.0, not [::], not any LAN address of this host;
  * only the session's own dev containers can be previewed (not its proxy, not a missing one);
  * the session's teardown (what `ab session kill` runs) removes the forwarder and closes the port;
  * a session that has ended cannot get a preview.
"""
import json
import os
import socket
import subprocess
import threading
import time
import unittest
import urllib.request

from agentbox import config, gate, preview, session
from agentbox.errors import AgentboxError
from agentbox.podman import Podman

from . import harness

ENV = SESS = SETTINGS = None
IMG = "docker.io/library/busybox:1.36"
WEB = f"abt-{harness.RUN}-pvweb"
OLD_ENV = {}


def api(method, path, body=None):
    conn = gate.UnixHTTP(SESS.gate_socket)
    try:
        conn.request(method, "/v5.0.0" + path, body=json.dumps(body).encode() if body is not None else None,
                     headers={"Content-Type": "application/json"} if body is not None else {})
        r = conn.getresponse()
        return r.status, r.read().decode(errors="replace")
    finally:
        conn.close()


def setUpModule():
    global ENV, SESS, SETTINGS
    if harness.podman("image", "exists", IMG).returncode != 0:
        raise unittest.SkipTest(f"{IMG} not present")
    ENV = harness.Env()
    from tests import ompfake
    # The installed plugin tree, where `ab profile install omp` puts it: there is no
    # setting to point anywhere else, and nothing reads the host's ~/.omp (5 Nov).
    tree = ompfake.build(ENV.cfg)
    ENV.make_project("pv", preset="python", extra=["--podman", "dev"], secrets_files=False)
    keys = ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR")
    OLD_ENV.update({k: os.environ.get(k) for k in keys})
    os.environ.update({k: ENV.env[k] for k in keys})
    SETTINGS = config.load()
    SESS = session.make_session(SETTINGS, "omp", session.load_project(SETTINGS, "pv"), warn=lambda m: None)
    SESS.prepare()
    SESS.start_infra()
    st, body = api("POST", "/libpod/containers/create", {
        "image": IMG, "name": WEB,
        "command": ["sh", "-c", "mkdir -p /w && echo preview-ok > /w/index.html && exec httpd -f -p 8000 -h /w"]})
    assert st == 201, (st, body)
    st, body = api("POST", f"/libpod/containers/{WEB}/start")
    assert st in (204, 304), (st, body)
    print(f"\n[itest run {harness.RUN}: step 6 preview, session {SESS.name}]")


def tearDownModule():
    try:
        if SESS is not None:
            session.teardown(SETTINGS, SESS.podman, SESS.name, warn=lambda m: None)
    finally:
        for k, v in OLD_ENV.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        harness.cleanup()


def get(port, timeout=5):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            f"http://127.0.0.1:{port}/index.html", timeout=timeout) as r:
        return r.read().decode().strip()


def lan_addresses():
    out = subprocess.run(["ip", "-o", "addr", "show"], capture_output=True, text=True).stdout
    addrs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[2] in ("inet", "inet6"):
            ip = parts[3].split("/")[0]
            if not ip.startswith(("127.", "::1", "fe80")):
                addrs.append(ip)
    return addrs


def connects(host, port):
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam)
    s.settimeout(3)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Preview(unittest.TestCase):
    port = None

    def test_a_serves_the_service_on_loopback(self):
        port, desk = preview.start(SETTINGS, Podman(SETTINGS.podman), SESS.name, f"{WEB}:8000")
        Preview.port = port
        self.assertIn(f"-L {port}:127.0.0.1:{port}", desk)
        body = harness.wait_for(lambda: (lambda: get(port))() if connects("127.0.0.1", port) else None, timeout=30)
        self.assertEqual(body, "preview-ok")
        results = []
        threads = [threading.Thread(target=lambda: results.append(get(port))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(results, ["preview-ok"] * 6, "several connections at once")

    def test_b_listens_on_127_0_0_1_only(self):
        port = Preview.port
        listening = subprocess.run(["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True).stdout
        local = [l.split()[3] for l in listening.splitlines() if l.strip()]
        self.assertEqual(local, [f"127.0.0.1:{port}"], listening)
        for ip in lan_addresses():
            with self.subTest(ip=ip):
                self.assertFalse(connects(ip, port), f"reachable on {ip}:{port}")

    def test_c_only_the_sessions_own_services(self):
        pm = Podman(SETTINGS.podman)
        for target in (f"{SESS.proxy_name}:3128", "nothere:80", f"{SESS.holder_name}:80"):
            with self.subTest(target=target), self.assertRaises(AgentboxError):
                preview.start(SETTINGS, pm, SESS.name, target)

    def test_d_kill_removes_it(self):
        port = Preview.port
        name = f"{SESS.name}-preview-{port}"
        self.assertEqual(harness.podman("container", "exists", name).returncode, 0)
        session.teardown(SETTINGS, SESS.podman, SESS.name, warn=lambda m: None)       # what ab session kill runs
        self.assertNotEqual(harness.podman("container", "exists", name).returncode, 0, "forwarder removed")
        self.assertTrue(harness.wait_for(lambda: not connects("127.0.0.1", port), timeout=20), "port closed")
        with self.assertRaises(AgentboxError):                                         # the session is gone
            preview.start(SETTINGS, Podman(SETTINGS.podman), SESS.name, f"{WEB}:8000")

    def test_z_nothing_left(self):
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
