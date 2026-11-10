"""Toulmin (gate B) against the stub `tm`, with REAL podman: the parts of docs/agentbox_toulmin_integration.md
§3 the unit tests cannot produce -- the agent's view from inside the container (3.3) and the
`podman inspect` check that the token is nowhere in the container's config (3.2). Prints both so the run's
output is the artifact.

The unit tests (tests/unit/test_toulmin.py) cover the rest with the fake podman: the mount list and hash,
the three files, the five exit traces.
"""
import json
import os
import subprocess
import time
import unittest

from . import harness
from .harness import LABEL, podman

ENV = None
STUB = os.path.join(harness.REPO, "tests", "toulmin", "tm-stub.sh")
TOKEN = "t0ken-itest-" + harness.RUN + "-" + "x" * 32
PROJECT = "tlm"
paths = {}


def setUpModule():
    global ENV
    ENV = harness.Env()
    root = ENV.make_project(PROJECT, preset="script", secrets_files=False)
    stub = os.path.join(harness.BASE, "tm-stub")
    sock_dir = os.path.join(harness.BASE, "toulmin-1000", "S4")
    bridge = os.path.join(harness.BASE, "bin", "tm-bridge")
    denied = os.path.join(harness.BASE, "shared", "records")
    for d in (stub, sock_dir, os.path.dirname(bridge), os.path.join(denied, "proj")):
        os.makedirs(d, exist_ok=True)
    with open(bridge, "w") as f:
        f.write("#!/bin/sh\necho tm-bridge stub; exit 0\n")
    os.chmod(bridge, 0o755)
    with open(os.path.join(stub, "start.json"), "w") as f:
        json.dump({"handshake": 1, "session": "S4", "token": TOKEN, "bridge": bridge,
                   "socket": os.path.join(sock_dir, "sock"), "mount_socket_dir": sock_dir,
                   "agent_denied": ["data/patients.tsv"],
                   "must_not_mount": [denied, os.path.join(harness.BASE, "toulmin-1000", "*")],
                   "project_root": root, "role": "writer"}, f)
    with open(os.path.join(ENV.cfg, "agentbox.toml"), "a") as f:
        f.write(f'toulmin_tm_path = "{STUB}"\n')
    ENV.env["TM_STUB_DIR"] = stub
    ENV.ab("project", "set", PROJECT, "--toulmin", check=True)
    paths.update(root=root, stub=stub, sock_dir=sock_dir, bridge=bridge, denied=denied)
    print(f"\n[itest run {harness.RUN}: toulmin in {harness.BASE}]")


def tearDownModule():
    harness.cleanup()


def agent_container():
    out = podman("ps", "--format", "{{.Names}}", "--filter", f"label={LABEL}",
                 "--filter", f"label=agentbox.project={PROJECT}", "--filter", "label=agentbox.role=agent").stdout
    names = out.split()
    return names[0] if names else None


SCRIPT = r'''
set +e
echo "== ls -la /run/agentbox/toulmin/"; ls -la /run/agentbox/toulmin/
echo "== ls -la /run/agentbox/bin/"; ls -la /run/agentbox/bin/
echo "== MCP config the harness loads (.claude.json mcpServers)"
python3 -c 'import json; print(json.dumps(json.load(open("/run/agentbox/claude/.claude.json")).get("mcpServers"), indent=1))'
echo "== env | grep AGENTBOX"; env | grep AGENTBOX | sort
echo "== git status --short --branch"; git -C "$ROOT" status --short --branch
echo "== ls of a must_not_mount path"; if ls "$DENIED"; then echo "UNEXPECTED: visible"; else echo "ls failed as expected"; fi
echo "== the bridge runs"; /run/agentbox/bin/tm-bridge
echo "== token file"; wc -c /run/agentbox/toulmin/token
sleep 20
echo "== done"
'''


class Toulmin(unittest.TestCase):
    def test_the_agents_view_and_the_token_is_not_in_podman_inspect(self):
        env_prefix = f'ROOT={paths["root"]} DENIED={paths["denied"]}/proj; '
        p = ENV.ab_bg("session", "start", "claude", PROJECT, "--no-tty", "--shell", "--", "-c", env_prefix + SCRIPT)
        name = harness.wait_for(agent_container, timeout=180)
        self.assertTrue(name, "the agent container never appeared")
        inspect = podman("inspect", name).stdout
        self.assertNotIn(TOKEN, inspect, "the token is in the container's config")
        self.assertIn("/run/agentbox/toulmin", inspect)
        out, err = p.communicate(timeout=600)
        print("\n--- artifact 3: the agent's view ---\n" + out + "\n--- launcher stderr (tail) ---\n" + err[-1500:])
        self.assertEqual(p.returncode, 0, err[-2000:])
        self.assertIn("AGENTBOX_AGENT=claude", out)
        self.assertIn("AGENTBOX_SESSION=", out)
        self.assertRegex(out, r"## agentbox/abt-\S+")
        status = out.split("== git status --short --branch\n", 1)[1].split("\n== ", 1)[0].strip().splitlines()
        self.assertEqual(len(status), 1, f"a clean tree on the session branch: {status}")
        self.assertIn("ls failed as expected", out)
        self.assertIn("tm-bridge stub", out)
        self.assertIn(f"{len(TOKEN)} /run/agentbox/toulmin/token", out)
        self.assertIn('"command": "/run/agentbox/bin/tm-bridge"', out)
        confirm = harness.read_json(os.path.join(paths["stub"], "confirm.json"))
        end = harness.read_json(os.path.join(paths["stub"], "end.json"))
        print("--- confirm ---\n" + json.dumps(confirm, indent=1, sort_keys=True))
        print("--- end ---\n" + json.dumps(end, indent=1, sort_keys=True))
        self.assertEqual(confirm["clone"], end["clone"])
        self.assertEqual((end["exit"], end["head"]), ("agent_exit", confirm["base"]))
        self.assertTrue(harness.wait_for(lambda: agent_container() is None or None, timeout=120), "torn down")
        self.assertFalse(os.path.exists(os.path.join(paths["stub"], "serving")), "serve stopped at teardown")
        self.assertTrue(os.path.isdir(os.path.join(end["clone"], ".git")), "the clone is kept")
        self.assertFalse(os.path.exists(os.path.join(paths["root"], ".git", "refs", "heads", "agentbox")),
                         "no branch was made in the owner's repository")


if __name__ == "__main__":
    unittest.main()
