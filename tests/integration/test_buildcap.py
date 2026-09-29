"""An uncapped build is impossible: the gate's build rewrite (agentbox.buildcap) against a real podman service.

  * slice idle   -> the rewrite refuses the build (an idle slice would let the runtime create an uncapped cgroup);
  * slice active -> the build's RUN step runs in <slice>/<session>-build with the gate's own memory limit.
The gate (step 5) calls this rewrite on every build; this proves the rewrite, not the gate's routing.
"""
import http.client
import io
import json
import os
import socket
import subprocess
import tarfile
import threading
import time
import unittest
from urllib.parse import urlencode

from agentbox import buildcap
from agentbox.errors import AgentboxError

from . import harness
from .harness import LABEL, podman

SLICE = "agentbox.slice"
SESSION = f"abt-{harness.RUN}-bc"
GIB = 1 << 30
API = None
API_PROC = None


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("d", timeout=300)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(300)
        self.sock.connect(self._path)


def setUpModule():
    global API, API_PROC
    if not harness.slice_installed():
        raise unittest.SkipTest("agentbox.slice not installed")
    os.makedirs(harness.BASE, exist_ok=True)
    API = os.path.join(harness.BASE, "api.sock")
    API_PROC = subprocess.Popen(["podman", "system", "service", "--time=0", f"unix://{API}"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not harness.wait_for(lambda: os.path.exists(API), timeout=60):
        raise RuntimeError("podman API service did not start")


def tearDownModule():
    if API_PROC:
        API_PROC.terminate()
        API_PROC.wait(timeout=30)
    podman("rmi", "-f", f"{SESSION}-img:1")
    harness.cleanup()


def context_tar():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = (b"FROM docker.io/library/alpine:3.20\n"
                b'RUN echo "MEMMAX=$(cat /sys/fs/cgroup/memory.max)"; sleep 5\n')
        info = tarfile.TarInfo("Containerfile")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def api_build(pairs):
    conn = UnixHTTP(API)
    conn.request("POST", "/v5.0.0/libpod/build?" + urlencode(pairs), body=context_tar(),
                 headers={"Content-Type": "application/x-tar"})
    resp = conn.getresponse()
    raw = resp.read().decode(errors="replace")
    conn.close()
    lines = []
    for chunk in raw.replace("}{", "}\n{").splitlines():
        try:
            doc = json.loads(chunk)
        except ValueError:
            continue
        lines += (doc.get("stream") or doc.get("error") or "").splitlines()
    return resp.status, lines


def base_query():
    return [("t", f"{SESSION}-img:1"), ("dockerfile", "Containerfile"), ("nocache", "true"),
            ("networkmode", "1"), ("nsoptions", json.dumps([{"Name": "network", "Host": False, "Path": ""}]))]


def slice_active():
    return buildcap.slice_state(SLICE)[0]


class BuildCap(unittest.TestCase):
    def test_1_idle_slice_refuses_the_build(self):
        # systemd keeps a slice active once it has been used, even when empty, so agentbox.slice is only
        # idle after boot until the first session, and stopping it would be a systemd change. A slice name
        # that was never started is idle in exactly the same way, with the real systemctl state behind it.
        idle = f"abt-idle-{harness.RUN}.slice"
        with self.assertRaises(AgentboxError) as e:
            buildcap.rewrite_build_query(base_query(), slice_name=idle, session=SESSION, memory_bytes=GIB)
        self.assertIn("uncapped build is not allowed", str(e.exception))
        print(f"\nidle slice: {e.exception}")
        if not slice_active():
            with self.assertRaises(AgentboxError):
                buildcap.rewrite_build_query(base_query(), slice_name=SLICE, session=SESSION, memory_bytes=GIB)

    def test_2_active_slice_caps_the_build(self):
        holder = f"{SESSION}-holder"
        r = podman("run", "-d", "--name", holder, "--label", "agentbox-test=1", "--label", LABEL,
                   "--network", "none", f"--cgroup-parent={SLICE}", "docker.io/library/alpine:3.20", "sleep", "300")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(harness.wait_for(slice_active, timeout=30), "slice did not become active")
        pairs = buildcap.rewrite_build_query(base_query(), slice_name=SLICE, session=SESSION, memory_bytes=GIB)
        build_dir = "/sys/fs/cgroup" + dict(pairs)["cgroupparent"]
        seen = []

        def watch():
            end = time.time() + 240
            while time.time() < end and not seen:
                if os.path.isdir(build_dir):
                    seen.append(build_dir)
                time.sleep(0.2)
        t = threading.Thread(target=watch, daemon=True)
        t.start()
        status, lines = api_build(pairs)
        t.join(timeout=5)
        print(f"\nbuild status {status}; RUN step saw: {[l for l in lines if l.startswith('MEMMAX=')]}; "
              f"build cgroup seen: {bool(seen)}")
        self.assertEqual(status, 200, "\n".join(lines[-10:]))
        self.assertIn(f"MEMMAX={GIB}", lines, "the RUN step has the gate's memory limit")
        self.assertTrue(seen, f"the RUN step's cgroup {build_dir} appeared under the slice")

    def test_3_client_cgroup_parent_is_refused(self):
        with self.assertRaises(AgentboxError):
            buildcap.rewrite_build_query(base_query() + [("cgroupparent", "/")], slice_name=SLICE,
                                         session=SESSION, memory_bytes=GIB)


if __name__ == "__main__":
    unittest.main()
