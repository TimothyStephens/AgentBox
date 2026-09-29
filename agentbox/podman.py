"""Thin wrapper around the podman CLI. Every call is logged (argv only; no secret is ever in argv)."""
import json
import os
import subprocess
import time

from .errors import AgentboxError

_LOG = None


def set_log(path):
    global _LOG
    _LOG = path


def _log(argv):
    if not _LOG:
        return
    os.makedirs(os.path.dirname(_LOG), exist_ok=True)
    with open(_LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + json.dumps(argv) + "\n")


class Podman:
    def __init__(self, binary="podman", timeout=120):
        self.binary = binary
        self.timeout = timeout

    def run(self, args, *, check=True, capture=True, input=None, timeout=None):
        argv = [self.binary] + list(args)
        _log(argv)
        try:
            p = subprocess.run(argv, input=input, text=True, timeout=timeout or self.timeout,
                               stdout=subprocess.PIPE if capture else None,
                               stderr=subprocess.PIPE if capture else None)
        except subprocess.TimeoutExpired:
            raise AgentboxError(f"podman {' '.join(args[:2])} timed out")
        if check and p.returncode != 0:
            err = (p.stderr or "").strip().splitlines()[-3:] if capture else []
            raise AgentboxError(f"podman {' '.join(args[:2])} failed ({p.returncode}): {' '.join(err)}")
        return p

    def interactive(self, args):
        """Run with the terminal attached; return the exit code."""
        argv = [self.binary] + list(args)
        _log(argv)
        return subprocess.call(argv)

    def ids(self, kind, labels):
        """IDs/names of containers or networks carrying all `labels`."""
        filters = []
        for k, v in labels.items():
            filters += ["--filter", f"label={k}={v}"]
        if kind == "container":
            p = self.run(["ps", "-a", "--format", "{{.Names}}"] + filters, check=False)
        elif kind == "network":
            p = self.run(["network", "ls", "--format", "{{.Name}}"] + filters, check=False)
        else:
            raise ValueError(kind)
        return [x for x in (p.stdout or "").split() if x]

    def containers_json(self, labels):
        filters = []
        for k, v in labels.items():
            filters += ["--filter", f"label={k}={v}"]
        p = self.run(["ps", "-a", "--format", "json"] + filters, check=False)
        try:
            return json.loads(p.stdout or "[]") or []
        except ValueError:
            return []
