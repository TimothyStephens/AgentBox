"""Shared fixtures for the unit tests: temp dirs, settings, a fake podman on PATH."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from agentbox import config

FAKE_PODMAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakepodman.py")


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="abt-unit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def mkdir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def write(self, rel, text="x", mode=None):
        p = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        if mode is not None:
            os.chmod(p, mode)
        return p


class AbTimeout(AssertionError):
    pass


class SettingsCase(TempCase):
    """Settings rooted in a temp dir, with a fake podman that records every call."""

    # Every in-process `ab` run gets a closed stdin and a deadline, because the two ways a CLI test can
    # fail to produce a verdict are both real: `ab run` READS stdin when it has no TTY, so a test whose
    # refusal stops working blocks on the terminal instead of failing; and anything that waits turns a red
    # test into a suite that never finishes. Both now end as a FAIL naming the command and what it printed.
    AB_TIMEOUT = 30

    def ab(self, *argv, stdin="", timeout=None):
        """Run `ab <argv>` in this process. Returns (exit code, stdout+stderr).

        stdin is an empty, already-at-EOF stream unless a test passes something (or `stdin=None` to leave
        the real one alone, which only a test that means to read it should do)."""
        import io
        import signal
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from unittest import mock

        from agentbox import cli

        out, err = io.StringIO(), io.StringIO()
        shown = lambda: out.getvalue() + err.getvalue()
        limit = self.AB_TIMEOUT if timeout is None else timeout

        def too_slow(_signum, _frame):
            raise AbTimeout(f"`ab {' '.join(argv)}` did not finish in {limit}s. Output so far:\n"
                            f"{shown() or '(nothing)'}")

        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            if stdin is not None:
                stack.enter_context(mock.patch("sys.stdin", io.StringIO(stdin)))
            if limit and hasattr(signal, "SIGALRM"):
                old_handler = signal.signal(signal.SIGALRM, too_slow)
                stack.callback(signal.signal, signal.SIGALRM, old_handler)
                stack.callback(signal.alarm, 0)
                signal.alarm(int(limit))
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, shown()

    def setUp(self):
        super().setUp()
        self.cfg = self.mkdir("config")
        self.data = self.mkdir("data")
        self.run = self.mkdir("run")
        self.podman_log = os.path.join(self.tmp, "podman.log")
        env = {"AB_CONFIG_DIR": self.cfg, "AB_DATA_DIR": self.data, "AB_RUN_DIR": self.run,
               "AB_PODMAN": FAKE_PODMAN, "FAKE_PODMAN_LOG": self.podman_log}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(_restore, old)
        with open(os.path.join(self.cfg, "agentbox.toml"), "w") as f:
            f.write('name_prefix = "abt-unit-"\ncgroup_parent = ""\n'
                    'labels = { "agentbox-test" = "1" }\n')
        self.settings = config.load()

    def token(self, value="fake-token-UNIT-0123456789", mode=0o600):
        p = self.settings.token_file("claude")
        with open(p, "w") as f:
            f.write(value + "\n")
        os.chmod(p, mode)
        return value

    def podman_calls(self):
        if not os.path.exists(self.podman_log):
            return []
        with open(self.podman_log) as f:
            return [json.loads(line) for line in f]

    def project(self, name="p1", preset="python", git=True, **extra):
        from agentbox import registry
        root = self.mkdir("projects", name)
        if git:
            subprocess.run(["git", "init", "-q", root], check=True)
        reg = registry.load(self.settings.registry_path)
        reg[name] = {"root": root, "preset": preset, **extra}
        registry.save(self.settings.registry_path, reg)
        return root


def _restore(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


os.chmod(FAKE_PODMAN, os.stat(FAKE_PODMAN).st_mode | stat.S_IXUSR)
assert sys.version_info >= (3, 11)


def read_file(path, *args, **kw):
    """A file's contents, with the handle closed. Arguments are forwarded to `open`, so a mode works.

    `open(p).read()` leaves the handle to the garbage collector, which is why `make test` printed dozens of
    `ResourceWarning: unclosed file` lines. Harmless in itself -- but a test log nobody can skim is how a
    real finding gets missed, and two have been found by reading these logs.
    """
    with open(path, *args, **kw) as f:
        return f.read()


def read_json(path):
    import json as _json
    return _json.loads(read_file(path))
