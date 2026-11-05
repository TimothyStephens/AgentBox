"""A fake ~/.omp built from tests/fixtures/omp/tree.txt (Tim's real layout, names only).

Every file holds a canary line naming its path, so a test can prove which files a session can read.

The provisioned links mirror Tim's real ~/.omp (30 Sep): plugins -> versions/19760485e9c2/plugins while
repos and pylibs -> versions/c1b51fef8911/. A plugin in node_modules links to an absolute
/root/.omp/versions/19760485e9c2/repos/... path, the way `omp-env install` (run as root) writes links, and
versions/legacy-20260905172136 is referenced by nothing.
"""
import os

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "omp")
ACTIVE = "c1b51fef8911"            # repos and pylibs
PLUGINS_VERSION = "19760485e9c2"   # plugins (and the repos its plugin links point into)
LINKS = {"plugins": PLUGINS_VERSION, "repos": ACTIVE, "pylibs": ACTIVE}
CANARY = "OMPCANARY"


def canary(rel):
    return f"{CANARY}:{rel}"


def build(dest):
    """Create the fake tree at `dest`; returns dest."""
    with open(os.path.join(FIXTURE, "tree.txt")) as f:
        entries = [line.rstrip("\n").split(" ", 1) for line in f if line.strip()]
    for kind, path in entries:
        rel = path[len("~/.omp"):].lstrip("/")
        full = os.path.join(dest, rel)
        if kind == "d":
            os.makedirs(full, exist_ok=True)
        elif kind == "f":
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(canary(rel) + "\n")
        elif kind == "l":
            os.makedirs(os.path.dirname(full), exist_ok=True)
            name = os.path.basename(rel)
            os.symlink(f"versions/{LINKS[name]}/{name}", full)
    # Plausible contents where OMP reads them.
    with open(os.path.join(dest, "agent", "config.yml"), "w") as f:
        f.write(f"# {canary('agent/config.yml')}\n{{}}\n")
    with open(os.path.join(dest, "bin", "mags-codedev"), "w") as f:
        f.write(f"#!/bin/sh\n# {canary('bin/mags-codedev')}\necho mags-codedev-stub\n")
    os.chmod(os.path.join(dest, "bin", "mags-codedev"), 0o755)
    for version, name in ((ACTIVE, "plugins"), (ACTIVE, "repos"), (ACTIVE, "pylibs"),
                          (PLUGINS_VERSION, "plugins"), (PLUGINS_VERSION, "repos")):
        d = os.path.join(dest, "versions", version, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "MARKER"), "w") as f:
            f.write(canary(f"versions/{version}/{name}/MARKER") + "\n")
    # a linked plugin, as `omp plugin link` leaves it: an absolute /root/.omp path into its own version
    ext = os.path.join(dest, "versions", PLUGINS_VERSION, "repos", "pi-fake-ext")
    os.makedirs(ext, exist_ok=True)
    with open(os.path.join(ext, "index.js"), "w") as f:
        f.write(f"// {canary(f'versions/{PLUGINS_VERSION}/repos/pi-fake-ext/index.js')}\n")
    nm = os.path.join(dest, "versions", PLUGINS_VERSION, "plugins", "node_modules")
    os.makedirs(nm, exist_ok=True)
    os.symlink(f"/root/.omp/versions/{PLUGINS_VERSION}/repos/pi-fake-ext", os.path.join(nm, "pi-fake-ext"))
    with open(os.path.join(dest, "versions", PLUGINS_VERSION, "plugins", "package.json"), "w") as f:
        f.write('{"dependencies": {"pi-fake-ext": "link:/root/.omp/versions/%s/repos/pi-fake-ext"}}\n'
                % PLUGINS_VERSION)
    legacy = os.path.join(dest, "versions", "legacy-20260905172136", "plugins")
    os.makedirs(legacy, exist_ok=True)
    with open(os.path.join(legacy, "MARKER"), "w") as f:
        f.write(canary("versions/legacy-20260905172136/plugins/MARKER") + "\n")
    with open(os.path.join(dest, "versions", ACTIVE, "pylibs", "abt_pylib_probe.py"), "w") as f:
        f.write("VALUE = 'pylibs-importable'\n")
    return dest


# What a session may read out of ~/.omp; everything else in it must be unreachable. `agent/config.yml` and
# `.cache/ms-playwright/` left this list on 4 Nov -- config.yml is agentbox's own seed now, and the image's
# chromium replaced the host browser cache -- so the fake home still HAS both, and a session must not see
# either (test_omp.test_config_yml_comes_from_agentbox_not_from_the_host_omp).
MOUNTED_PREFIXES = (f"versions/{ACTIVE}/", f"versions/{PLUGINS_VERSION}/", "plugins/", "repos/", "pylibs/",
                    "bin/mags-codedev")


def is_mounted(rel):
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in MOUNTED_PREFIXES)
