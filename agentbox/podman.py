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
        except OSError as e:
            # A missing or unexecutable binary is a condition to report, not a traceback: every safety
            # decision that asks podman a question has to be able to tell "no" from "cannot tell".
            raise AgentboxError(f"cannot run podman ({self.binary}): {e}")
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
        """IDs/names of containers or networks carrying all `labels`.

        Raises when the listing fails. It used to pass check=False and split whatever came back, so a failed
        or empty-on-error listing meant "nothing matches" -- which teardown reads as "nothing to remove",
        the session namer reads as "that name is free", and the selftest reads as "no leftovers" (audit
        F-2/F-10)."""
        filters = []
        for k, v in labels.items():
            filters += ["--filter", f"label={k}={v}"]
        if kind == "container":
            p = self.run(["ps", "-a", "--format", "{{.Names}}"] + filters)
        elif kind == "network":
            p = self.run(["network", "ls", "--format", "{{.Name}}"] + filters)
        elif kind == "pod":
            p = self.run(["pod", "ps", "--format", "{{.Name}}"] + filters)
        elif kind == "volume":
            p = self.run(["volume", "ls", "--format", "{{.Name}}"] + filters)
        else:
            raise ValueError(kind)
        return [x for x in (p.stdout or "").split() if x]

    def containers_json(self, labels):
        """Every container carrying all `labels`, as podman reports it.

        Raises when `ps` fails or its output cannot be parsed. This used to return [] in both cases, so
        "podman is broken" and "nothing is running" were the same answer -- the defect behind two of the
        third audit's findings, since liveness, the live-stack ownership check and gc all ask this."""
        filters = []
        for k, v in labels.items():
            filters += ["--filter", f"label={k}={v}"]
        p = self.run(["ps", "-a", "--format", "json"] + filters)
        try:
            return json.loads(p.stdout or "[]") or []
        except ValueError as e:
            raise AgentboxError(f"podman ps output could not be read as JSON ({e}); "
                                f"refusing to treat that as 'nothing is running'")
