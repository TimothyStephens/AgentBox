"""make install: agentbox launcher plus an `ab` alias, never over someone else's `ab`."""
import io
import os
import subprocess

from agentbox import install

from .helpers import TempCase

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Install(TempCase):
    def run_install(self, bindir, path_env=None):
        out = io.StringIO()
        install.install(bindir, REPO, out=out, path_env=path_env if path_env is not None else bindir)
        return out.getvalue()

    def test_installs_a_working_launcher_and_alias(self):
        b = self.mkdir("bin")
        self.run_install(b)
        self.assertEqual(os.readlink(os.path.join(b, "ab")), "agentbox")
        p = subprocess.run([os.path.join(b, "ab"), "--help"], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=""))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("agentbox", p.stdout)

    def test_refuses_someone_elses_ab(self):
        b = self.mkdir("bin")
        with open(os.path.join(b, "ab"), "w") as f:
            f.write('#!/bin/sh\nexec env PYTHONPATH="$HOME/repos/agentbox" python3 -m agentbox "$@"\n')
        with self.assertRaises(SystemExit) as cm:
            self.run_install(b)
        self.assertIn("not agentbox's launcher", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(b, "agentbox")), "nothing written")

    def test_refuses_an_ab_symlink_elsewhere(self):
        b = self.mkdir("bin")
        os.symlink("/usr/bin/ab", os.path.join(b, "ab"))
        with self.assertRaises(SystemExit):
            self.run_install(b)

    def test_reinstall_is_fine(self):
        b = self.mkdir("bin")
        self.run_install(b)
        self.run_install(b)
        self.assertTrue(install.ours(os.path.join(b, "ab"), b))

    def test_notes_another_ab_on_path(self):
        b, other = self.mkdir("bin"), self.mkdir("usrbin")
        with open(os.path.join(other, "ab"), "w") as f:
            f.write("#!/bin/sh\necho apachebench\n")
        os.chmod(os.path.join(other, "ab"), 0o755)
        out = self.run_install(b, path_env=f"{other}:{b}")
        self.assertIn("`ab` on PATH is", out)
        out = self.run_install(b, path_env=f"{b}:{other}")
        self.assertIn("shadows", out)
