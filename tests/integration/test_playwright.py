"""Opt-in Playwright, proved in a real session (decisions.md, 19 Nov).

Skips unless an agent image carries Playwright (`agentbox.playwright` label not `none`): it is opt-in, so its
absence is a choice, not a failure. To run it on the host:

    make base-image PLAYWRIGHT=1 && make claude-image && make itest   # or just this module:
    python3 -m unittest tests.integration.test_playwright -v

In a session with the session's full hardening (read-only root, every capability dropped, no-new-privileges,
the session's small /dev/shm, $HOME an empty tmpfs) and the proxy variables set:
  * `python` is a command (python-is-python3, in the base since 19 Nov);
  * PLAYWRIGHT_BROWSERS_PATH points at the image's read-only browser directory;
  * Python Playwright launches its headless Chromium with its defaults, loads a page from a server the session
    itself started on 127.0.0.1 (so loopback is not sent to the proxy), and reads the page's title;
  * `ab project selftest` reports its own launch check as PASS.
"""
import json
import secrets
import shutil
import unittest

from . import harness

ENV = None
AGENT = None
VERSION = None
RUN = harness.RUN
TITLE = f"abt-playwright-{secrets.token_hex(4)}"


def image_playwright(image):
    """The image's Playwright version, or None (absent, opted out, or built before the label)."""
    p = harness.podman("image", "inspect", image, "--format", "json")
    if p.returncode != 0:
        return None
    try:
        labels = (json.loads(p.stdout or "[]")[0].get("Config") or {}).get("Labels") or {}
    except (ValueError, IndexError):
        return None
    from agentbox import imageopts
    return imageopts.playwright_version(labels)


def setUpModule():
    global ENV, AGENT, VERSION
    if shutil.which("podman") is None:
        raise unittest.SkipTest("no podman here (host-only test)")
    # Claude: the agent that starts on the fake token every itest uses. The other agent images are built FROM the
    # same base, so they carry the same Playwright (the label is inherited).
    AGENT, VERSION = "claude", image_playwright("localhost/agentbox-claude:latest")
    if not VERSION:
        raise unittest.SkipTest("localhost/agentbox-claude:latest carries no Playwright (agentbox.playwright is "
                                "none or missing): make base-image PLAYWRIGHT=1, then make claude-image")
    ENV = harness.Env()
    ENV.make_project("pw", preset="python")
    print(f"\n[itest run {RUN}: playwright {VERSION} in agentbox-{AGENT}]")


def tearDownModule():
    harness.cleanup()


SCRIPT = r"""
set +e
echo "PYTHON=$(python --version 2>&1)"
echo "BROWSERS=$PLAYWRIGHT_BROWSERS_PATH"
mkdir -p /tmp/abt-site && cd /tmp/abt-site
printf '<html><head><title>%s</title></head><body><h1 id="h">hello</h1></body></html>' "$TITLE" > index.html
python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/abt-site.log 2>&1 &
SRV=$!
for i in $(seq 1 50); do (exec 3<>/dev/tcp/127.0.0.1/8765) 2>/dev/null && break; sleep 0.2; done
cat > /tmp/abt-pw.py <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page()
    pg.goto("http://127.0.0.1:8765/")
    pg.wait_for_load_state("networkidle")
    print("TITLE=" + pg.title())
    print("H1=" + pg.inner_text("#h"))
    print("UA=" + pg.evaluate("navigator.userAgent"))
    b.close()
PY
python /tmp/abt-pw.py 2>&1 | tail -20
echo "PWRC=${PIPESTATUS[0]}"
kill $SRV 2>/dev/null
"""


class PlaywrightInASession(unittest.TestCase):
    maxDiff = None

    def test_headless_chromium_loads_a_local_page(self):
        script = f"TITLE={TITLE}\n" + SCRIPT
        p = ENV.ab("session", "start", AGENT, "pw", "--shell", "--no-tty", "--", "-c", script, timeout=900)
        print(f"\n{p.stdout[-2500:]}\n--stderr--\n{p.stderr[-1200:]}")
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        out = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in p.stdout.splitlines() if "=" in l}
        self.assertTrue(out.get("PYTHON", "").startswith("Python 3"), out.get("PYTHON"))
        self.assertEqual(out.get("BROWSERS"), "/opt/ms-playwright")
        self.assertEqual(out.get("PWRC"), "0", p.stdout[-1500:])
        self.assertEqual(out.get("TITLE"), TITLE)
        self.assertEqual(out.get("H1"), "hello")
        self.assertIn("HeadlessChrome", out.get("UA", ""))

    def test_selftest_reports_the_launch_as_pass(self):
        results, p = ENV.selftest(AGENT, "pw")
        lines = [r for r in results if r.get("label", "").startswith("Playwright launches headless Chromium")]
        self.assertEqual(len(lines), 1, json.dumps(results)[-1500:])
        self.assertEqual(lines[0].get("verdict"), "PASS", lines[0])
        self.assertIn(VERSION, lines[0].get("label", ""))


if __name__ == "__main__":
    unittest.main()
