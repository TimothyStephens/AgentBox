import datetime
import io
import os

from agentbox import backup, tokens
from agentbox.errors import AgentboxError

from .helpers import TempCase


class BackupTest(TempCase):
    def setUp(self):
        super().setUp()
        self.src = self.mkdir("proj")
        self.write("proj/a.txt", "hello")
        self.write("proj/.git/HEAD", "ref: x")
        self.write("proj/node_modules/m/i.js", "x")
        self.write("proj/.venv/bin/py", "x")
        self.write("proj/big/huge.bin", "x")
        self.write("proj/src/__pycache__/c.pyc", "x")
        self.backups = self.mkdir("backups")

    def snap(self, t, **kw):
        return backup.snapshot(self.backups, "p", f"s{t:%H%M%S}", [self.src], now=t,
                               skip_rel_for=lambda g: ["big"], **kw)

    def test_snapshot_contents(self):
        s = self.snap(datetime.datetime(2026, 9, 29, 10, 0, 0))
        dst = os.path.join(s, self.src.lstrip("/"))
        self.assertTrue(os.path.isfile(os.path.join(dst, "a.txt")))
        self.assertTrue(os.path.isfile(os.path.join(dst, ".git/HEAD")), ".git is kept")
        for gone in ("node_modules", ".venv", "big", "src/__pycache__"):
            self.assertFalse(os.path.exists(os.path.join(dst, gone)), gone)

    def test_link_dest_shares_unchanged_files(self):
        s1 = self.snap(datetime.datetime(2026, 9, 29, 10, 0, 0))
        s2 = self.snap(datetime.datetime(2026, 9, 29, 11, 0, 0))
        a1 = os.stat(os.path.join(s1, self.src.lstrip("/"), "a.txt"))
        a2 = os.stat(os.path.join(s2, self.src.lstrip("/"), "a.txt"))
        self.assertEqual(a1.st_ino, a2.st_ino)

    def test_same_second_same_session(self):
        t = datetime.datetime(2026, 9, 29, 10, 0, 0)
        a = backup.snapshot(self.backups, "p", "s1", [self.src], now=t)
        b = backup.snapshot(self.backups, "p", "s1", [self.src], now=t)
        self.assertNotEqual(a, b)
        self.assertTrue(os.path.isdir(a) and os.path.isdir(b))

    def test_size_warning(self):
        self.write("proj/blob", "x" * (2 * 1024 * 1024))
        msgs = []
        self.snap(datetime.datetime(2026, 9, 29, 10, 0, 0), warn_mb=1, warn=msgs.append)
        self.assertTrue(msgs and "backup_warn_mb" in msgs[0])

    def test_retention(self):
        pdir = self.mkdir("backups", "p")
        names = []
        for day in range(1, 11):          # 10 days, two snapshots per day
            for hour in (9, 17):
                n = f"202609{day:02d}-{hour:02d}0000-s"
                os.mkdir(os.path.join(pdir, n))
                names.append(n)
        backup.retain(self.backups, "p", keep=5, daily_days=7, today=datetime.date(2026, 9, 10))
        left = sorted(os.listdir(pdir))
        want = set(names[-5:]) | {f"202609{d:02d}-170000-s" for d in range(4, 11)}
        self.assertEqual(set(left), want)


class TokenTest(TempCase):
    def test_accepts_0600(self):
        p = self.write("tok", "fake-token", 0o600)
        self.assertEqual(tokens.check_token_file(p), p)

    def test_refusals(self):
        loose = self.write("loose", "x", 0o644)
        empty = self.write("empty", "", 0o600)
        target = self.write("target", "x", 0o600)
        link = os.path.join(self.tmp, "link")
        os.symlink(target, link)
        for bad in (loose, empty, link, os.path.join(self.tmp, "missing"), self.tmp):
            with self.assertRaises(AgentboxError, msg=bad):
                tokens.check_token_file(bad)

    def test_rotate_writes_0600(self):
        p = os.path.join(self.tmp, "cfg", "claude-oauth-token")
        tokens.rotate(p, "claude", read=lambda prompt: "fake-rotated-token", out=io.StringIO())
        with open(p) as f:
            self.assertEqual(f.read(), "fake-rotated-token\n")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_rotate_refuses_empty(self):
        p = os.path.join(self.tmp, "t")
        with self.assertRaises(AgentboxError):
            tokens.rotate(p, "claude", read=lambda prompt: "  ", out=io.StringIO())
        self.assertFalse(os.path.exists(p))
