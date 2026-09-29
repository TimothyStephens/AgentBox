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


class SettingsCase(TempCase):
    """Settings rooted in a temp dir, with a fake podman that records every call."""

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
