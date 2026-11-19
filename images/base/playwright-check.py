"""Build-time proof for the base image's `playwright` stage (images/base/Containerfile). Run by the build, then
deleted; not in the image.

    python3 playwright-check.py <playwright version> <chromium-headless-shell revision> <its browser version>

Fails the build unless all three hold:
  * the installed Python package is the pinned version (a pin that is only written down is not a pin);
  * that package's own browsers.json names the browser build the Makefile pinned -- the zip came in by
    ADD --checksum from the Makefile's pins, so if the package moved and the pins did not, Playwright would
    look for a directory that is not there;
  * a headless launch renders a page: proves the browser, its shared libraries and Playwright's driver work
    together, which "the executable exists" does not.
"""
import json
import os
import sys
from importlib.metadata import version as dist_version

import playwright
from playwright.sync_api import sync_playwright

TITLE = "agentbox-playwright-check"


def main(version, revision, browser_version):
    # The distribution's metadata: the package has no `__version__` (checked on 1.65.2).
    installed = dist_version("playwright")
    if installed != version:
        sys.exit(f"playwright is {installed}, not the pinned {version}")
    path = os.path.join(os.path.dirname(playwright.__file__), "driver", "package", "browsers.json")
    with open(path) as f:
        browsers = {b["name"]: b for b in json.load(f)["browsers"]}
    shell = browsers.get("chromium-headless-shell") or {}
    got = (shell.get("revision"), shell.get("browserVersion"))
    if got != (revision, browser_version):
        sys.exit(f"playwright {version} wants chromium-headless-shell {got[0]} ({got[1]}), but the Makefile "
                 f"pins {revision} ({browser_version}): set PLAYWRIGHT_CHROMIUM_REVISION/_VERSION (and its sha256) "
                 "to what the package wants")
    with sync_playwright() as p:
        browser = p.chromium.launch()            # headless, Playwright's defaults: what a session will do
        try:
            page = browser.new_page()
            page.set_content(f"<html><head><title>{TITLE}</title></head><body>ok</body></html>")
            title = page.title()
        finally:
            browser.close()
    if title != TITLE:
        sys.exit(f"headless Chromium rendered the title {title!r}, not {TITLE!r}")
    print(f"playwright {version}: chromium-headless-shell {revision} ({browser_version}) renders a page headless")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
