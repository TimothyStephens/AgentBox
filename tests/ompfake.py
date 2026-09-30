"""A fake ~/.omp built from tests/fixtures/omp/tree.txt (Tim's real layout, names only).

Every file holds a canary line naming its path, so a test can prove which files a session can read. The
provisioned links (plugins, repos, pylibs) point into the active version, as `omp-env install` leaves them.
"""
import os

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "omp")
ACTIVE = "c1b51fef8911"
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
            os.symlink(f"versions/{ACTIVE}/{os.path.basename(rel)}", full)
    # Plausible contents where OMP reads them.
    with open(os.path.join(dest, "agent", "config.yml"), "w") as f:
        f.write(f"# {canary('agent/config.yml')}\n{{}}\n")
    with open(os.path.join(dest, "bin", "mags-codedev"), "w") as f:
        f.write(f"#!/bin/sh\n# {canary('bin/mags-codedev')}\necho mags-codedev-stub\n")
    os.chmod(os.path.join(dest, "bin", "mags-codedev"), 0o755)
    for name in ("plugins", "repos", "pylibs"):
        d = os.path.join(dest, "versions", ACTIVE, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "MARKER"), "w") as f:
            f.write(canary(f"versions/{ACTIVE}/{name}/MARKER") + "\n")
    with open(os.path.join(dest, "versions", ACTIVE, "pylibs", "abt_pylib_probe.py"), "w") as f:
        f.write("VALUE = 'pylibs-importable'\n")
    return dest


# Files that must never be readable in a session (all of ~/.omp except the mounted pieces).
MOUNTED_PREFIXES = (f"versions/{ACTIVE}/", "plugins/", "repos/", "pylibs/", "agent/config.yml",
                    "bin/mags-codedev", ".cache/ms-playwright/")


def is_mounted(rel):
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in MOUNTED_PREFIXES)
