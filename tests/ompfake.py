"""A fake installed plugin tree, as `ab profile install omp` leaves one.

Built at `<config_dir>/omp/plugins`, because that is where the installer puts it and where a session
mounts it from. **There is no setting to point this anywhere else**: the host's `~/.omp` is not read at
all any more (5 Nov), so a test that wants a tree puts one in the config dir the test already controls.

What this used to be: a fake `~/.omp`, with provisioned symlinks into `versions/<hash>/`, a legacy version
nothing referenced, and a plugin linked to an absolute `/root/.omp/...` path -- all of it modelling Tim's
real `omp-env install` output, because agentbox had to read a directory it did not control. The whitelist
that made that safe is gone with it.

Every file holds a canary line naming its path, so a test can prove which files a session can read.
"""
import json
import os
import time

# The fixtures directory is still used for the redacted models.yml seed the tests read.
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "omp")
CANARY = "OMPCANARY"
FAKE_PLUGIN = "pi-fake-ext"
MARKETPLACE = "egm"
PLUGIN_ID = f"{MARKETPLACE}@{MARKETPLACE}"
PLUGIN_VERSION = "0.1.0"
SHA = "464db031e7dd1b9a79803ac8ea45f55c7c20c5bc"
CLI_VERSION = "18.13.3"
NODE_MAJOR = "26"
# What `ab profile install` writes under the tree (agentbox/installer.py). Named here so a test can assert
# on them without reaching into the installer's constants.
OURS = ".agentbox"
LOCKFILE = f"{OURS}/install.json"
MARKETPLACES = f"{OURS}/marketplaces.json"


def canary(rel):
    return f"{CANARY}:{rel}"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def tree_path(config_dir, agent="omp"):
    return os.path.join(config_dir, agent, "plugins")


def build(config_dir, *, agent="omp", cli_version=CLI_VERSION, node_major=NODE_MAJOR, declared=None):
    """Create a fake installed tree under `config_dir`; returns its path.

    The shape is the one the probe found on the host (decisions.md, 5 Nov): `node_modules/` for npm and
    linked plugins, `cache/marketplaces/<name>/` for a catalog, `cache/plugins/<id>___<version>/` for an
    installed marketplace plugin, `package.json`/`bun.lock`/`installed_plugins.json` as OMP's own state,
    and `.agentbox/` for what agentbox puts there."""
    tree = tree_path(config_dir, agent)
    # A plugin, as `omp plugin link` leaves one: a directory in node_modules with a manifest.
    ext = os.path.join(tree, "node_modules", FAKE_PLUGIN)
    _write(os.path.join(ext, "index.js"), f"// {canary(f'node_modules/{FAKE_PLUGIN}/index.js')}\n")
    _write(os.path.join(ext, "package.json"),
           json.dumps({"name": FAKE_PLUGIN, "version": "0.1.0",
                       "omp": {"extensions": ["./index.js"]}}, indent=1) + "\n")
    _write(os.path.join(tree, "package.json"),
           json.dumps({"name": "omp-plugins", "private": True,
                       "dependencies": {FAKE_PLUGIN: "0.1.0"}}, indent=1) + "\n")
    _write(os.path.join(tree, "bun.lock"), f"# {canary('bun.lock')}\n")
    _write(os.path.join(tree, "installed_plugins.json"), json.dumps({"plugins": [FAKE_PLUGIN]}) + "\n")
    # A marketplace catalog and the plugin installed from it.
    cat = os.path.join(tree, "cache", "marketplaces", MARKETPLACE)
    _write(os.path.join(cat, "marketplace.json"),
           json.dumps({"name": MARKETPLACE, "plugins": [{"name": MARKETPLACE}]}) + "\n")
    _write(os.path.join(cat, "MARKER"), canary(f"cache/marketplaces/{MARKETPLACE}/MARKER") + "\n")
    inst = os.path.join(tree, "cache", "plugins", f"{MARKETPLACE}___{MARKETPLACE}___{PLUGIN_VERSION}")
    _write(os.path.join(inst, "MARKER"), canary(f"cache/plugins/{MARKETPLACE}.../MARKER") + "\n")
    # agentbox's own corner: the catalog clone it pinned, the marketplace registry it copied out, the
    # state OMP reported, and the lockfile.
    src = os.path.join(tree, OURS, "sources", MARKETPLACE)
    _write(os.path.join(src, "marketplace.json"), json.dumps({"name": MARKETPLACE}) + "\n")
    _write(os.path.join(tree, MARKETPLACES),
           json.dumps({"version": 1, "marketplaces": [
               {"name": MARKETPLACE, "sourceType": "local", "sourceUri": src,
                "catalogPath": os.path.join(cat, "marketplace.json")}]}, indent=1) + "\n")
    state = {"npm": [{"name": FAKE_PLUGIN, "version": "0.1.0",
                      "path": os.path.join(tree, "node_modules", FAKE_PLUGIN)}],
             "marketplace": [{"id": PLUGIN_ID, "scope": "user",
                              "entries": [{"version": PLUGIN_VERSION, "installPath": inst}]}]}
    _write(os.path.join(tree, OURS, "state.json"), json.dumps(state) + "\n")
    _write(os.path.join(tree, LOCKFILE), json.dumps(
        {"version": 1, "agent": agent, "installed_at": int(time.time()),
         "profile_hash": "",
         "image": {"ref": f"localhost/agentbox-{agent}:latest", "id": "sha256:fake",
                   "cli_version": cli_version, "node_major": node_major},
         "declared": declared if declared is not None else [
             {"kind": "plugin", "id": PLUGIN_ID, "marketplace": MARKETPLACE,
              "source": "google/elephant-goldfish-model", "ref": SHA}],
         "state": state}, indent=1, sort_keys=True) + "\n")
    return tree


# Everything under ~/.omp that comes from the tree, relative to the tree. A session may read all of it and
# write none of it; anything else it can see under ~/.omp is its own state (agent/, logs/) or generated
# for the session, never a host file.
VISIBLE = (f"node_modules/{FAKE_PLUGIN}/index.js", "bun.lock", f"cache/marketplaces/{MARKETPLACE}/MARKER",
           f"cache/plugins/{MARKETPLACE}.../MARKER")
