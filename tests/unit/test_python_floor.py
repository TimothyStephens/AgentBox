"""The 3.11 floor is declared (pyproject.toml) and checked before anything that needs it is imported."""
import os
import subprocess
import sys
import tomllib
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Run an entry point as if the interpreter were 3.10: sys.version_info is replaced before the entry point's
# own code runs, so the check must fire before `agentbox.cli` (which imports tomllib) is imported.
AS_310 = """
import runpy, sys
class V(tuple):
    major, minor = 3, 10
sys.version_info = V((3, 10, 0, "final", 0))
sys.argv = [{path!r}, "--help"]
runpy.{how}
"""


class PythonFloor(unittest.TestCase):
    def run_as_310(self, how, path):
        return subprocess.run([sys.executable, "-c", AS_310.format(how=how, path=path)], cwd=REPO,
                              capture_output=True, text=True, timeout=60)

    def test_python_m_agentbox_refuses_before_importing_cli(self):
        p = self.run_as_310('run_module("agentbox", run_name="__main__")', "agentbox")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("agentbox needs Python 3.11+ (tomllib)", p.stderr)
        self.assertNotIn("usage:", p.stdout)

    def test_bin_ab_refuses_before_importing_cli(self):
        ab = os.path.join(REPO, "bin", "ab")
        p = self.run_as_310(f'run_path({ab!r}, run_name="__main__")', ab)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("agentbox needs Python 3.11+ (tomllib)", p.stderr)

    def test_the_real_interpreter_still_runs(self):
        p = subprocess.run([sys.executable, "-m", "agentbox", "--help"], cwd=REPO, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_pyproject_declares_the_floor(self):
        with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
            doc = tomllib.load(f)
        self.assertEqual(doc["project"]["requires-python"], ">=3.11")
        self.assertEqual(doc["project"]["dependencies"], [], "standard library only")
        self.assertEqual(doc["tool"]["ruff"]["target-version"], "py311")
