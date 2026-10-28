"""Integration-test harness: one test run = one run id, one /tmp tree, one label to clean up by.

Everything the run creates is named abt-<run>-* and labelled agentbox-test=1 and
agentbox-test-run=<run>. `cleanup()` removes exactly that, and runs even when a test fails.
"""
import atexit
import json
import os
import secrets
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(REPO, "tests", "fixtures", "dummy")
RUN = secrets.token_hex(3)
BASE = f"/tmp/abt-{RUN}"
FAKE_TOKEN = f"fake-token-{RUN}-{secrets.token_hex(12)}"
LABEL = f"agentbox-test-run={RUN}"


def slice_installed():
    p = subprocess.run(["systemctl", "--user", "show", "agentbox.slice", "-p", "FragmentPath", "-p", "MemoryMax"],
                       capture_output=True, text=True)
    props = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
    return bool(props.get("FragmentPath")) and props.get("MemoryMax") not in ("", "infinity", None)


class Env:
    def __init__(self):
        self.cfg = os.path.join(BASE, "config")
        self.data = os.path.join(BASE, "data")
        self.run_dir = os.path.join(BASE, "run")
        self.projects = os.path.join(BASE, "projects")
        self.slice = slice_installed()
        for d in (self.cfg, self.data, self.run_dir, self.projects):
            os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(self.cfg, "agentbox.toml"), "w") as f:
            f.write(f'name_prefix = "abt-{RUN}-"\n'
                    f'cgroup_parent = "{"agentbox.slice" if self.slice else ""}"\n'
                    f'labels = {{ "agentbox-test" = "1", "agentbox-test-run" = "{RUN}" }}\n')
        tok = os.path.join(self.cfg, "claude-oauth-token")
        with open(tok, "w") as f:
            f.write(FAKE_TOKEN + "\n")
        os.chmod(tok, 0o600)
        self.env = dict(os.environ, AB_CONFIG_DIR=self.cfg, AB_DATA_DIR=self.data, AB_RUN_DIR=self.run_dir)

    def ab(self, *args, timeout=900, check=False, input=""):
        p = subprocess.run([sys.executable, "-m", "agentbox", *args], cwd=REPO, env=self.env, input=input,
                           capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise AssertionError(f"ab {' '.join(args)} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
        return p

    def ab_bg(self, *args):
        return subprocess.Popen([sys.executable, "-m", "agentbox", *args], cwd=REPO, env=self.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def make_project(self, name, *, preset="python", extra=(), git=True, secrets_files=True):
        root = os.path.join(self.projects, name)
        shutil.copytree(FIXTURE, root)
        if git:
            subprocess.run(["git", "init", "-q", root], check=True)
            subprocess.run(["git", "-C", root, "add", "-A"], check=True)
            subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t",
                            "commit", "-qm", "init"], check=True)
        if secrets_files:
            _write(os.path.join(root, ".env"), "GREETING=real-secret\n")
            _write(os.path.join(root, ".env.dev"), "GREETING=dev-value\n")
            _write(os.path.join(root, "sub", ".env"), "SUB=real\n")
            _write(os.path.join(root, "tls", "server.key"), "KEY\n")
            _write(os.path.join(root, "secrets", "db-password.txt"), "hunter2\n")
        self.ab("project", "add", name, root, "--preset", preset, *extra, check=True)
        return root

    def selftest(self, agent, project):
        p = self.ab("selftest", "--agent", agent, "--project", project, "--json")
        try:
            results = json.loads(p.stdout)
        except ValueError:
            raise AssertionError(f"selftest gave no JSON ({p.returncode}):\n{p.stdout}\n{p.stderr}")
        return results, p


def podman(*args, timeout=120):
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout)


def listing(*args):
    """A podman listing whose EMPTY result the tests read as a verdict ("nothing left over"), so a failed
    one must not look like an empty one. `podman network ls --format '{{index .Labels "x"}}'` was rejected
    by podman 5.7 for a whole build while the code swallowed it (28 Oct); this harness swallowed the same
    way, so a rejected template here would have made every leftover check pass vacuously."""
    p = podman(*args)
    if p.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args[:3])} failed ({p.returncode}): "
                           f"{(p.stderr or '').strip()[:300]} -- an unreadable listing is not an empty one")
    return p.stdout


def leftovers():
    """Containers, pods, volumes and networks this run labelled (images: leftover_images)."""
    c = listing("ps", "-a", "--format", "{{.Names}}", "--filter", f"label={LABEL}").split()
    p = listing("pod", "ps", "--format", "{{.Name}}", "--filter", f"label={LABEL}").split()
    v = listing("volume", "ls", "--format", "{{.Name}}", "--filter", f"label={LABEL}").split()
    n = listing("network", "ls", "--format", "{{.Name}}", "--filter", f"label={LABEL}").split()
    return c + p + v + n


def leftover_images():
    """Images this run labelled, as 'repo:tag' (or the short ID when untagged)."""
    out = listing("images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}", "--filter", f"label={LABEL}")
    return sorted(line.split()[0] if not line.startswith("<none>") else line.split()[-1]
                  for line in out.splitlines() if line.strip())


def all_volumes():
    return set(listing("volume", "ls", "--format", "{{.Name}}").split())


def wait_for(pred, timeout=90, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def cleanup():
    for c in podman("ps", "-aq", "--filter", f"label={LABEL}").stdout.split():
        podman("rm", "-f", "-t", "2", c)
    for p in podman("pod", "ps", "-q", "--filter", f"label={LABEL}").stdout.split():
        podman("pod", "rm", "-f", p)
    for v in podman("volume", "ls", "-q", "--filter", f"label={LABEL}").stdout.split():
        podman("volume", "rm", "-f", v)
    for n in podman("network", "ls", "-q", "--filter", f"label={LABEL}").stdout.split():
        podman("network", "rm", "-f", n)
    shutil.rmtree(BASE, ignore_errors=True)


def report(results):
    return "\n".join(f"  {r['verdict']:<4} {r['label']}" + (f"  [{r['detail']}]" if r["verdict"] != "PASS" else "")
                     for r in results)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


atexit.register(cleanup)
