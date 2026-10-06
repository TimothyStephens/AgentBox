"""Track 1 (Tim, 6 Oct): the grant holds code; a grant with secrets or live data refuses to start unless the
exposure is accepted. The masks are defence in depth behind this."""
import os

from agentbox import grantcheck

from .helpers import TempCase


class GrantScan(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.mkdir("proj")

    def test_live_data_and_secrets_are_found_covered_ones_are_not(self):
        self.write("proj/app/main.py", "code\n")                     # code: fine
        self.write("proj/data/app.db", "x" * (2 << 20))              # 2 MB sqlite: live data
        self.write("proj/small.db", "x" * 1024)                      # under 1 MB: fine
        self.write("proj/.env", "SECRET=real\n")                     # a secret, but masked below
        self.write("proj/config/service.pem", "KEY\n")              # an uncovered secret
        found = grantcheck.scan(self.root, [self.root], covered=[os.path.join(self.root, ".env")])
        joined = "\n".join(found)
        self.assertIn(f"live data", joined)
        self.assertIn("data/app.db", joined)
        self.assertIn("config/service.pem", joined)
        self.assertNotIn("small.db", joined)
        self.assertNotIn(f"{self.root}/.env", joined, "a masked/covered secret is not flagged")

    def test_empty_secret_and_masked_secrets_dir_pass(self):
        self.write("proj/.env", "")                                  # empty: nothing to protect
        self.write("proj/secrets/db.txt", "pw\n")
        found = grantcheck.scan(self.root, [self.root],
                                covered=[os.path.join(self.root, "secrets")])   # secrets/ masked
        self.assertEqual(found, [])

    def test_git_credentials_in_a_remote_url(self):
        import subprocess
        subprocess.run(["git", "init", "-q", self.root], check=True)
        subprocess.run(["git", "-C", self.root, "remote", "add", "origin",
                        "https://user:ghp_secrettoken@github.com/x/y.git"], check=True)
        found = grantcheck.scan(self.root, [self.root])
        self.assertTrue(any("git credentials" in f for f in found), found)

    def test_compose_data_bind_flags_data_not_code(self):
        self.write("proj/data/app.db", "x")                          # a data dir
        self.write("proj/app/main.py", "code\n")                     # a code dir
        self.write("proj/compose.yaml",
                   "services:\n  web:\n    volumes:\n      - ./data:/data\n      - ./app:/app\n")
        found = grantcheck.scan(self.root, [self.root])
        self.assertTrue(any("compose data bind" in f and "proj/data (" in f for f in found), found)
        self.assertFalse(any("/app" in f for f in found), "a code bind is not flagged")

    def test_mask_skip_excludes(self):
        self.write("proj/vendor/big.db", "x" * (2 << 20))
        self.assertTrue(grantcheck.scan(self.root, [self.root]))
        self.assertEqual(grantcheck.scan(self.root, [self.root], mask_skip=["vendor"]), [])

    def test_fingerprint_changes_with_findings(self):
        a = grantcheck.fingerprint(["live data: /x"])
        self.assertEqual(a, grantcheck.fingerprint(["live data: /x"]))
        self.assertNotEqual(a, grantcheck.fingerprint(["live data: /x", "unmasked secret file: /y"]))
