"""Every `--format` agentbox issues, against real podman, inside `make itest`.

`tests/manual/podman_formats.sh` is the only thing that can check this class for real: a `--format`
template is a per-subcommand contract with podman's internals (`index .Labels` is fine on `image inspect`
and refused on `network ls`), and the fake podman can only refuse the templates someone already knew to
teach it. That script used to run when somebody remembered -- which is how
`network ls --format '{{.Name}} {{index .Labels "agentbox.session"}}'` lived in the code for a whole build:
podman 5.7 rejected it, the call was swallowed, and network-based session detection had never worked.

Run by hand the script inspects whatever the host happens to have, so on an idle machine it skipped the
inspect checks and asserted almost nothing. Here it is given **this run's own objects** -- an abt-<run>
network, a container attached to it, and an image reference -- so every check has a target, and a SKIP is
a failure rather than a shrug.

Read-only by construction: the script only lists and inspects. The objects are this test's, labelled, and
cleaned up by the harness.
"""
import os
import subprocess
import unittest

from . import harness

IMG = "docker.io/library/busybox:1.36"
SCRIPT = os.path.join(harness.REPO, "tests", "manual", "podman_formats.sh")
NET = f"abt-{harness.RUN}-fmt"
CTR = f"abt-{harness.RUN}-fmt"


def setUpModule():
    if harness.podman("image", "exists", IMG).returncode != 0:
        raise unittest.SkipTest(f"{IMG} not present")
    label = f"agentbox-test-run={harness.RUN}"
    p = harness.podman("network", "create", "--label", "agentbox-test=1", "--label", label, NET)
    if p.returncode != 0:
        raise RuntimeError(f"could not create {NET}: {p.stderr.strip()[:200]}")
    # Started, not just created: `{{json .NetworkSettings.Networks}}` is checked against a populated value,
    # which is what agentbox reads it for (the proxy's address on the session network).
    p = harness.podman("run", "-d", "--name", CTR, "--label", "agentbox-test=1", "--label", label,
                       "--network", NET, IMG, "sleep", "600")
    if p.returncode != 0:
        raise RuntimeError(f"could not start {CTR}: {p.stderr.strip()[:200]}")
    print(f"\n[itest run {harness.RUN}: podman --format contracts, against {NET} and {CTR}]")


def tearDownModule():
    harness.cleanup()


class Formats(unittest.TestCase):
    def test_every_format_agentbox_issues_is_accepted_by_this_podman(self):
        p = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, timeout=300,
                           env={**os.environ, "AB_FMT_NET": NET, "AB_FMT_CTR": CTR, "AB_FMT_IMG": IMG})
        print(p.stdout)
        self.assertEqual(p.returncode, 0, f"podman rejected a format agentbox issues:\n{p.stdout}\n{p.stderr}")
        summary = [l for l in p.stdout.splitlines() if l.startswith("pass=")]
        self.assertTrue(summary, f"the script printed no summary line:\n{p.stdout}")
        self.assertIn("fail=0", summary[-1])
        # The point of running it here rather than by hand: every check has a target of ours, so a skip
        # means the script could not reach an object it was handed -- not that the host was idle.
        self.assertIn("skip=0", summary[-1], f"a check had no target to inspect:\n{p.stdout}")
        self.assertGreater(int(summary[-1].split("pass=")[1].split()[0]), 10,
                           f"too few checks ran to mean anything:\n{p.stdout}")

    def test_the_script_fails_when_podman_rejects_a_format(self):
        """The control. Without it, `returncode == 0` would also be satisfied by a script that checks
        nothing -- and this whole test would pass with the script gutted."""
        p = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, timeout=300,
                           env={**os.environ, "AB_FMT_NET": NET, "AB_FMT_CTR": CTR, "AB_FMT_IMG": IMG,
                                "PODMAN": os.path.join(harness.REPO, "tests", "integration",
                                                       "fmtrefuser.sh")})
        self.assertNotEqual(p.returncode, 0, f"a podman that refuses every format was reported as fine:\n{p.stdout}")
        self.assertIn("podman rejected:", p.stdout)

    def test_z_only_this_tests_own_objects_are_there(self):
        """The script lists and inspects and nothing else, so what this run labelled must be exactly the
        two objects the module created -- and they go in tearDownModule."""
        self.assertEqual(harness.leftovers(), [CTR, NET])
