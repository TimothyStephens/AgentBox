#!/usr/bin/env python3
"""Checks the guard hook: python3 .claude/hooks/test_guard.py"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
GUARD = os.path.join(HERE, "guard.py")

BLOCK = [
    "podman system reset -f",
    "podman system prune -af",
    "podman volume prune -f",
    "podman image prune -a",
    "podman rm -af",
    "podman stop --all",
    "podman rm -f media_server_web_1",
    "podman rm -f 3f2a9c1d",
    "podman exec -it jellyfin sh",
    "podman logs media_server-web",
    "podman-compose -p deploy down",
    "cd ~/life-updates && podman-compose down",
    "podman-compose up -d",
    "podman compose up -d",
    "podman-compose -p myapp up -d",
    "podman volume rm uploads",
    "podman network rm podman",
    "podman pod rm -f media",
    "podman rm -f --filter status=exited",
    "podman run --rm -v $HOME:/h alpine ls",
    "podman run --rm -v /home/vmuser/repos/other:/w alpine true",
    "podman run --rm --privileged alpine true",
    "podman run --rm --network host alpine true",
    "podman run --rm --network=container:media alpine true",
    "podman run -d -v uploads:/data alpine",
    "podman unshare rm -rf x",
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME/repos",
    "rm -rf ..",
    "rm -rf ~/.config",
    "cd /tmp && rm -rf /home/vmuser/*",
    "echo $(podman rm -f media_server)",
    "cat ~/.config/agentbox/claude-oauth-token",
    "cat ~/.claude/.credentials.json",
    "ls ~/.ssh/",
    "sudo apt install jq",
    "systemctl --user stop podman.socket",
    "loginctl enable-linger",
    "timeout 30 podman rm -f web",
    "FOO=1 podman rmi alpine",
    "docker ps",
]
ALLOW = [
    "podman ps -a",
    "podman ps --filter label=agentbox-test=1",
    "podman inspect abt-web",
    "podman logs abt-web",
    "podman rm -f abt-web abt-db",
    "podman rm -f --filter label=agentbox-test=1",
    "podman network rm abt-net",
    "podman volume rm abt-data",
    "podman pod rm -f abt-pod",
    "podman exec -it abt-web sh",
    "podman exec -e X=1 abt-web env",
    "podman-compose -p abt-dummy up -d --build",
    "podman compose -p abt-dummy down -v",
    "podman-compose version",
    "podman run --rm --network abt-net -v ./tests/fixtures:/f:ro alpine ls /f",
    "podman run --rm -v /tmp/abt-x:/x alpine true",
    "podman run -d --name abt-v -v abt-data:/data alpine sleep 1",
    "podman build -t abt-img tests/fixtures/dummy",
    "rm -rf build/ .venv",
    "rm -rf /tmp/abt-work",
    "rm -f notes.txt",
    "make test",
    "./spikes/run_all.sh 1",
    "git commit -m 'gate: pin builds'",
    "curl -s http://10.0.0.103:8088/ -o /dev/null",
    "podman network ls",
    "podman images",
    "python3 -m pytest -q",
]

def run(cmd):
    p = subprocess.run([sys.executable, GUARD], input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": ROOT}),
        capture_output=True, text=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=ROOT))
    return p.returncode, p.stderr.strip()

bad = 0
for c in BLOCK:
    rc, err = run(c)
    if rc != 2:
        bad += 1; print("SHOULD BLOCK:", c)
for c in ALLOW:
    rc, err = run(c)
    if rc != 0:
        bad += 1; print("SHOULD ALLOW:", c, "->", err)
rc, _ = run.__wrapped__ if False else (0, "")
p = subprocess.run([sys.executable, GUARD], input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}}), capture_output=True, text=True)
if p.returncode != 0:
    bad += 1; print("non-Bash tool should pass")
total = len(BLOCK) + len(ALLOW) + 1
print("%d/%d guard checks passed" % (total - bad, total))
sys.exit(1 if bad else 0)
