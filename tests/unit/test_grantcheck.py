"""Track 1 (Tim, 6 Oct): the grant holds code; a grant with secrets or live data refuses to start unless the
exposure is accepted. grantcheck.scan returns (findings, warnings); findings block, warnings only inform."""
import os

from agentbox import grantcheck

from .helpers import TempCase


def texts(findings):
    return [f["text"] for f in findings]


class GrantScan(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")

    def scan(self, **kw):
        return grantcheck.scan(self.root, [self.root], **kw)

    def test_live_data_and_secrets_are_found_covered_ones_are_not(self):
        self.write("proj/app/main.py", "code\n")
        self.write("proj/data/app.db", "x" * (2 << 20))
        self.write("proj/small.db", "x" * 1024)
        self.write("proj/.env", "SECRET=real\n")                     # masked below
        self.write("proj/config/service.pem", "KEY\n")              # uncovered secret
        findings, warnings = self.scan(covered=[os.path.join(self.root, ".env")])
        joined = "\n".join(texts(findings))
        self.assertIn("live data", joined)
        self.assertIn("data/app.db", joined)
        self.assertIn("config/service.pem", joined)
        self.assertNotIn("small.db", joined)
        self.assertNotIn(f"{self.root}/.env", joined, "a masked/covered secret is not flagged")

    def test_empty_secret_and_masked_secrets_dir_pass(self):
        self.write("proj/.env", "")
        self.write("proj/secrets/db.txt", "pw\n")
        findings, _ = self.scan(covered=[os.path.join(self.root, "secrets")])
        self.assertEqual(findings, [])

    def test_git_credentials_in_a_remote_url(self):
        import subprocess
        subprocess.run(["git", "init", "-q", self.root], check=True)
        subprocess.run(["git", "-C", self.root, "remote", "add", "origin",
                        "https://user:ghp_secrettoken@github.com/x/y.git"], check=True)
        findings, _ = self.scan()
        self.assertTrue(any("git credentials" in t for t in texts(findings)), findings)

    def test_compose_data_bind_flags_data_not_code(self):
        self.write("proj/data/app.db", "x")
        self.write("proj/app/main.py", "code\n")
        self.write("proj/compose.yaml",
                   "services:\n  web:\n    volumes:\n      - ./data:/data\n      - ./app:/app\n")
        findings, _ = self.scan()
        joined = texts(findings)
        self.assertTrue(any("compose data bind" in t and "proj/data (" in t for t in joined), joined)
        self.assertFalse(any("/app" in t for t in joined), "a code bind is not flagged")

    def test_mask_skip_is_not_an_exemption(self):
        """H3: mask_skip does not hide a secret from the scan, and the message doesn't offer it."""
        self.write("proj/vendor/big.db", "x" * (2 << 20))
        self.write("proj/vendor/.env", "SECRET=1\n")
        findings, _ = self.scan(mask_skip=["vendor"])
        joined = "\n".join(texts(findings))
        self.assertIn("vendor/big.db", joined)
        self.assertIn("vendor/.env", joined)
        self.assertIn("under mask_skip", joined)
        self.assertNotIn("mask_skip excludes", grantcheck.message("p", findings))

    def test_fingerprint_changes_with_content_not_just_path(self):
        self.write("proj/data/app.db", "x" * (2 << 20))
        f1, _ = self.scan()
        a = grantcheck.fingerprint(f1)
        self.assertEqual(a, grantcheck.fingerprint(self.scan()[0]))
        self.write("proj/data/app.db", "y" * (3 << 20))              # same path, new content
        self.assertNotEqual(a, grantcheck.fingerprint(self.scan()[0]), "content change re-prompts")

    def test_warns_on_foreign_owned_or_unreadable_entries(self):
        import unittest.mock
        self.write("proj/pgdata/PG_VERSION", "16\n")
        real_lstat = os.lstat
        pg = os.path.join(self.root, "pgdata")

        def fake(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if str(path) == pg:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1) + tuple(st)[5:])
            return st
        with unittest.mock.patch("agentbox.grantcheck.os.lstat", side_effect=fake):
            findings, warnings = self.scan()
        self.assertTrue(any("pgdata" in w and "uid" in w for w in warnings), warnings)
        self.assertEqual(findings, [], "a warning does not block on its own")
