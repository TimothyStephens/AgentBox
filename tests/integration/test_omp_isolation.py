"""OMP is a first-class agent, so prove its home isolation from inside a live OMP session (a fake ~/.omp, so
the test never touches the real one):

  * a write to ~/.omp/logs/<f> lands in the per-project state dir, NOT the host ~/.omp/logs;
  * the read-only mounts reject writes: `plugins/` and `bin/mags-codedev` from the host's ~/.omp, and
    `agent/config.yml` from agentbox's own config (it moved there on 4 Nov -- see decisions.md);
  * no key material is readable: the session's ~/.omp/agent/models.yml references AB_KEY_<NAME> variables, not
    a literal key, and no key string (the live key value, or a sentinel planted in the host's never-mounted
    models.yml) appears in any file under ~/.omp.

What this does NOT prove, because the adapter does not enforce it (it is an assumption about where OMP keeps
keys): a key string sitting literally in agent/config.yml or under plugins/ WOULD be mounted read-only and
readable. check_no_credentials only refuses a hard-linked credential file or a git-URL credential; it does
not scan config.yml or plugins for secrets. Since 4 Nov config.yml is a file the user writes under
~/.config/agentbox/omp/ rather than one copied from a host OMP install, so the warning is in the session
notes ("never put secrets there") and the gap is unchanged. See docs for it.
"""
import os
import secrets
import subprocess
import unittest

from tests import ompfake

from . import harness

ENV = None
IMAGE = "localhost/agentbox-omp:latest"
RUN = harness.RUN
LOGMARK = f"OMP-LOGMARK-{RUN}"                       # written to ~/.omp/logs inside the session
KEYVAL = f"sk-OMP-KEYVAL-{secrets.token_hex(10)}"   # the live provider key value (exported as AB_KEY_STUBAI)
# A fake key in a decoy OMP home nothing points at. Since 5 Nov no code path reads a host OMP install, so
# this is a canary rather than a proof -- the proof is tests/unit/test_omp (every mount source is under a
# directory agentbox owns, and the words are gone from the adapter). Kept because a canary costs nothing
# and the day someone reaches for ~/.omp again, this says so.
HOSTMODELS_SENTINEL = f"OMP-HOSTMODELS-KEYLEAK-{RUN}"
SEED = """providers:
  stubai:
    api: openai-completions
    baseUrl: http://127.0.0.1:8766/v1
    models:
      - id: stub-model
        contextWindow: 32000
        maxOut: 2000
"""


def setUpModule():
    global ENV
    if harness.podman("image", "exists", IMAGE).returncode != 0:
        raise unittest.SkipTest(f"{IMAGE} missing: run make omp-image")
    ENV = harness.Env()
    # The installed plugin tree, where `ab profile install omp` puts it: there is no
    # setting to point anywhere else, and nothing reads the host's ~/.omp (5 Nov).
    tree = ompfake.build(ENV.cfg)
    # A keyed models.yml in a decoy OMP home: nothing points at it, and nothing must read it.
    decoy = os.path.join(harness.BASE, "omp-decoy", "agent")
    os.makedirs(decoy, exist_ok=True)
    with open(os.path.join(decoy, "models.yml"), "w") as f:
        f.write(f"providers:\n  stubai:\n    apiKey: {HOSTMODELS_SENTINEL}\n")
    # The keyless seed the launcher regenerates models.yml from, plus a provider key file.
    os.makedirs(os.path.join(ENV.cfg, "omp"), exist_ok=True)
    with open(os.path.join(ENV.cfg, "omp", "models.yml"), "w") as f:
        f.write(SEED)
    # config.yml is agentbox's own since 4 Nov. Without this seed nothing mounts over agent/config.yml and
    # the read-only check below would be probing a plain writable file in the state dir.
    with open(os.path.join(ENV.cfg, "omp", "config.yml"), "w") as f:
        f.write("# agentbox's own config.yml\n{}\n")
    kdir = os.path.join(ENV.cfg, "keys")
    os.makedirs(kdir, mode=0o700, exist_ok=True)
    kf = os.path.join(kdir, "stubai")
    with open(kf, "w") as f:
        f.write(KEYVAL + "\n")
    os.chmod(kf, 0o600)
    ENV.make_project("ompiso", preset="python")
    print(f"\n[itest run {RUN}: omp isolation, plugin tree {tree}]")


def tearDownModule():
    harness.cleanup()


SCRIPT = f"""
set +e
echo "{LOGMARK}" > ~/.omp/logs/iso-probe.txt; echo "LOGWRITE_RC=$?"
( : > ~/.omp/plugins/SHOULD_NOT_WRITE ) 2>/dev/null; echo "PLUGINS_RC=$?"
( echo tampered > ~/.omp/agent/config.yml ) 2>/dev/null; echo "CONFIG_RC=$?"
( echo tampered >> ~/.omp/marketplaces.json ) 2>/dev/null; echo "MARKETPLACES_RC=$?"
if [ -n "$AB_KEY_STUBAI" ]; then echo KEYVAR_SET=yes; else echo KEYVAR_SET=no; fi
HITS=""
if [ -n "$AB_KEY_STUBAI" ]; then HITS="$(grep -rlF -e "$AB_KEY_STUBAI" ~/.omp 2>/dev/null | tr '\\n' ',')"; fi
echo "KEYVAL_HITS=$HITS"
echo "HOSTMODELS_HITS=$(grep -rlF -e '{HOSTMODELS_SENTINEL}' ~/.omp 2>/dev/null | tr '\\n' ',')"
echo "MODELS_HAS_VAR=$(grep -c 'AB_KEY_STUBAI' ~/.omp/agent/models.yml 2>/dev/null || echo 0)"
# the keyed provider's apiKey references the variable, never the literal value
echo "STUBAI_APIKEY=$(grep -iA3 'stubai' ~/.omp/agent/models.yml 2>/dev/null | grep -i apikey | tr -d ' ')"
"""


class OmpIsolation(unittest.TestCase):
    maxDiff = None

    def run_in_session(self):
        p = ENV.ab("run", "--agent", "omp", "--project", "ompiso", "--shell", "--no-tty", "--", "-c", SCRIPT,
                   timeout=900)
        print(f"\n{p.stdout[-1800:]}\n--stderr--\n{p.stderr[-500:]}")
        return {l.split("=", 1)[0]: l.split("=", 1)[1] for l in p.stdout.splitlines() if "=" in l}, p

    def test_omp_home_is_isolated(self):
        out, p = self.run_in_session()
        self.assertEqual(p.returncode, 0, p.stderr[-500:])

        # 1. the log write lands in the per-project state dir, never the host ~/.omp/logs
        self.assertEqual(out.get("LOGWRITE_RC"), "0", "~/.omp/logs is writable (per-project)")
        tree = ompfake.tree_path(ENV.cfg)
        host_hits = subprocess.run(["grep", "-rlF", LOGMARK, tree], capture_output=True, text=True).stdout.strip()
        self.assertEqual(host_hits, "", f"the log write reached the installed tree:\n{host_hits}")
        proj_log = os.path.join(ENV.data, "ompiso", "omp", "logs", "iso-probe.txt")
        self.assertTrue(os.path.isfile(proj_log), "the write landed in the per-project state dir")
        with open(proj_log) as f:
            self.assertIn(LOGMARK, f.read())

        # 2. the host-shared read-only mounts reject writes
        self.assertNotEqual(out.get("PLUGINS_RC"), "0",
                            "the installed plugin tree must be read-only -- THE question this feature "
                            "left open: a session must not be able to change what the next one loads")
        self.assertNotEqual(out.get("CONFIG_RC"), "0",
                            "agent/config.yml must be read-only -- agentbox's own copy now, not the host's")
        self.assertNotEqual(out.get("MARKETPLACES_RC"), "0", "marketplaces.json must be read-only")

        # 3. no key material is readable under ~/.omp
        self.assertEqual(out.get("KEYVAR_SET"), "yes", "the provider key is exported as AB_KEY_STUBAI (env only)")
        self.assertEqual(out.get("KEYVAL_HITS", "x"), "", "the live key value appears in a file under ~/.omp")
        self.assertEqual(out.get("HOSTMODELS_HITS", "x"), "",
                         "the decoy OMP home's keyed models.yml reached the session")
        self.assertEqual(out.get("MODELS_HAS_VAR"), "1", "models.yml references the AB_KEY_STUBAI variable")
        self.assertIn("AB_KEY_STUBAI", out.get("STUBAI_APIKEY", ""),
                      "the keyed provider's apiKey is the variable reference, not a literal key")
        self.assertNotIn(KEYVAL, out.get("STUBAI_APIKEY", ""))


if __name__ == "__main__":
    unittest.main()
