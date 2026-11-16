#!/usr/bin/env python3
"""Checks the guard hook: python3 .claude/hooks/test_guard.py"""
import json
import os
import subprocess
import sys

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
    "git commit --no-verify -m x",
    "git commit -n -m x",
    "git commit -qn -m x",
    "git commit -anm x",
    "git -C /home/vmuser/repos/agentbox commit --no-verify -F msg.txt",
    "git add -A && git commit -m 'step' -n",
    "git -c core.hooksPath=/dev/null commit -m x",
    "GIT_PRECOMMIT_CHECK_SECRETS=0 git commit -m x",
    # review 16 Nov, item 7: each of these got past the guard before
    "git add x && git commit --no-verify -m y",
    "git status; git commit --no-verify -m y",
    "git commit -m 'a;b' --no-verify",
    "git commit -m 'line one\nline two' --no-verify",
    "git commit -m \"$(printf 'a\nb')\" --no-veri",
    "git commit --no-v -m x",
    "git push --no-verify",
    "git merge --no-verify topic",
    "git -c core.hooksPath=/tmp/h commit -m x",
    "GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null GIT_CONFIG_COUNT=1 git commit -m x",
    "printf 'web\\n' | xargs podman rm -f",
    "podman ps -q | xargs -r podman rm -f",
    "echo media | xargs -I{} podman stop {}",
    "sh -c 'podman rm -f media_server'",
    "bash -lc \"podman exec -it jellyfin sh\"",
    "bash -c 'git commit --no-verify -m x'",
    "podman build -v /home/vmuser:/h -t abt-x .",
    "podman build --volume=$HOME/.config:/c -t abt-x .",
    "podman image build -v /:/r -t abt-x .",
    "podman rm -l",
    "podman stop --latest",
    "podman run --rm -v/home/vmuser:/h alpine true",
    "podman run --rm -v$HOME:/h alpine true",
    "podman run --rm -v $(pwd)/..:/h alpine true",
    "podman run --rm -v ${SRC}:/h alpine true",
    "rm -rf $(git rev-parse --show-toplevel)/..",
    "rm -rf ${TARGET}",
    "rm -rf $TARGET",
    "rm -rf `pwd`/..",
    "podman run --rm --pid=host alpine true",
    "podman run --rm --ipc host alpine true",
    "podman run --rm --userns=host alpine true",
    "podman run --rm --uts=host alpine true",
    "podman run --rm --cgroupns=host alpine true",
    "podman run --rm --pid=container:media alpine true",
    "podman build --network=host -t abt-x .",
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
    "git commit -m 'gate: pin builds'",
    "git commit -m 'no -n here'",
    "git commit -q -F - <<'EOF'",
    "git commit -m -n",
    "git commit -am 'fix'",
    "git log -n 3",
    "git -C /tmp/x commit -m x --author 'n <n@n>'",
    "curl -s http://10.0.0.103:8088/ -o /dev/null",
    "podman network ls",
    "podman images",
    "python3 -m pytest -q",
    "git commit -m 'drop --no-verify from the docs; podman rm -f x is refused'",
    "git commit -m 'one\ntwo' && git log -1",
    "podman ps -aq --filter label=agentbox-test=1 | xargs -r podman rm -f",
    "podman network ls -q --filter label=agentbox-test=1 | xargs -r podman network rm",
    "sh -c 'podman rm -f abt-web'",
    "bash -c 'make test'",
    "podman build -v ./tests/fixtures:/f:ro -t abt-img tests/fixtures/dummy",
    "podman run --rm -v/tmp/abt-x:/x alpine true",
    "podman run --rm --userns=keep-id alpine true",
    "rm -rf /tmp/abt-$RUN",
    "rm -rf build-$NAME",
    "git push origin main",
    "echo $(date)",
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
        bad += 1
        print("SHOULD BLOCK:", c)
for c in ALLOW:
    rc, err = run(c)
    if rc != 0:
        bad += 1
        print("SHOULD ALLOW:", c, "->", err)
p = subprocess.run([sys.executable, GUARD], input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}}), capture_output=True, text=True)
if p.returncode != 0:
    bad += 1
    print("non-Bash tool should pass")
total = len(BLOCK) + len(ALLOW) + 1
print("%d/%d guard checks passed" % (total - bad, total))
sys.exit(1 if bad else 0)
