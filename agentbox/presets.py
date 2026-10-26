"""Presets: named bundles of per-project settings, merged with project overrides.

Rules beyond a plain merge:
  * egress is `open` only when the project's own entry says so; a preset reference alone resolves to at
    most `packages`. A preset that declares `egress = "open"` (research, build-open) has it written onto
    the entry at registration/edit time -- see materialize_open_egress for why, and for what a hand-written
    entry resolves to;
  * `lockdown` is a ceiling: an override may tighten it, never loosen it (including podman.live and
    observe_logs);
  * a user preset file under a new name inherits lockdown's restrictive values for every key it omits.
"""
import os
import tomllib

from . import projectkeys
from .errors import AgentboxError

# Every one of these is derived from projectkeys.KEYS, the single settings table: which keys a preset may
# set, their allowed values, and the looseness order a ceiling compares on (audit: five parallel lists).
SETTINGS = projectkeys.PRESET_SETTINGS
ALLOWED = projectkeys.ALLOWED
LOOSENESS = projectkeys.LOOSENESS
EGRESS = projectkeys.BY_NAME["egress"].values
PODMAN_TIERS = projectkeys.PODMAN_TIERS

# "build" and "script" are the canonical names; "pods" and "python" are kept as true aliases (separate
# entries holding the identical dict, not a redirect), so a project already using "pods"/"python" resolves
# exactly as it did before this preset set grew (test_policy.py's parity tests pin this).
BUILTIN = {
    "lockdown": {"compose": "ro", "egress": "locked", "podman": {"tier": "none"}, "caps": "refused",
                 "secrets": "locked", "approvals": "on", "data": "ro-only"},
    "build": {"compose": "rw", "egress": "packages", "podman": {"tier": "dev", "live": "observe"},
              "caps": "opt-in", "secrets": "masked", "approvals": "off", "data": "declared"},
    "pods": {"compose": "rw", "egress": "packages", "podman": {"tier": "dev", "live": "observe"},
             "caps": "opt-in", "secrets": "masked", "approvals": "off", "data": "declared"},
    "build-open": {"compose": "rw", "egress": "open", "podman": {"tier": "dev", "live": "observe"},
                   "caps": "opt-in", "secrets": "masked", "approvals": "off", "data": "declared"},
    "script": {"compose": "ro", "egress": "packages", "podman": {"tier": "none"}, "caps": "refused",
               "secrets": "masked", "approvals": "off", "data": "declared"},
    "python": {"compose": "ro", "egress": "packages", "podman": {"tier": "none"}, "caps": "refused",
               "secrets": "masked", "approvals": "off", "data": "declared"},
    "research": {"compose": "ro", "egress": "open", "podman": {"tier": "none"}, "caps": "refused",
                 "secrets": "masked", "approvals": "off", "data": "declared"},
    "operate": {"compose": "ro", "egress": "packages", "podman": {"tier": "none", "live": "observe"},
                "caps": "refused", "secrets": "masked", "approvals": "off", "data": "ro-only"},
    "analysis-offline": {"compose": "ro", "egress": "locked", "podman": {"tier": "none"}, "caps": "refused",
                         "secrets": "masked", "approvals": "off", "data": "ro-only"},
}

# One line each, for `--preset`'s --help and the unknown-preset error. Aliases say so.
DESCRIPTIONS = {
    "lockdown": "review-only: no network beyond the model API, no podman access, approvals on",
    "build": "build, run and test the project's own pod/dev stack (packages egress)",
    "pods": "alias of build",
    "build-open": "like build, with open web egress for fetching arbitrary sites",
    "script": "scripts and data processing, no podman access (packages egress)",
    "python": "alias of script",
    "research": "review, planning and open web research; no podman access; the code grant is read-write, "
                "as under every preset",
    "operate": "inspect a live stack (observe tier); data grants are read-only",
    "analysis-offline": "sensitive data, no egress beyond the model API, no podman access, data read-only",
}


# The old names, kept as aliases of the canonical ones. Both names exist in BUILTIN with identical values
# (so provenance still reads "preset pods"); this map is what tells the preset-change check that moving a
# project between the two is a rename, not a change of posture.
ALIASES = {"pods": "build", "python": "script"}


def canonical(name):
    """The canonical name of a preset: `pods` and `python` are second names for `build` and `script`."""
    return ALIASES.get(name, name)


def same_preset(old, new, presets_map):
    """Do these two preset names mean the same thing? True for a name against itself, and for an alias
    against its target — as long as both still resolve to the same values. A user's own `pods.toml` that
    has drifted from `build` is therefore treated as a different preset, fail-closed: switching between
    them clears the overrides rather than letting them carry into a posture they were not set against."""
    if old == new:
        return True
    if canonical(old) != canonical(new):
        return False
    values = presets_map.get(old)
    return values is not None and values == presets_map.get(new)


def names_in_order(presets_map):
    """Built-ins first, in the order they are defined above (lockdown, build, ... analysis-offline), then
    any user presets alphabetically."""
    return [n for n in BUILTIN if n in presets_map] + sorted(set(presets_map) - set(BUILTIN))


def describe(presets_map, sep="; "):
    """Every preset that actually exists, with its one-line description: `--preset`'s help, and the list an
    unknown preset name is answered with."""
    return sep.join(f"{n} — {DESCRIPTIONS.get(n, 'custom preset')}" for n in names_in_order(presets_map))


def materialize_open_egress(entry, presets):
    """A preset whose definition declares `egress = "open"` gets that written onto the project's own
    registry entry when the project is registered or edited (`ab init` / `ab project add` / `ab project
    set`), unless an explicit --egress was given.

    The invariant: open egress is never granted by live reference to a preset. effective() demotes a
    preset-sourced "open" back to "packages", so the only thing that actually opens a project is an egress
    key on its own entry -- visible in projects.toml and printed by `ab project show` as project-sourced.
    Three consequences, all deliberate:

      * editing a preset file to say "open" later does not open any already-registered project;
      * a hand-written registry entry with `preset = "research"` and no egress key resolves to `packages`
        (fail-closed), not open;
      * the open default is an attribute of the preset definition, so a user's own `research.toml` that
        says `egress = "locked"` stays locked -- there is no separate name list to fall out of step.
    """
    name = entry.get("preset")
    if name in presets and presets[name].get("egress") == "open" and "egress" not in entry:
        entry["egress"] = "open"


def load_presets(presets_dir):
    presets = {k: dict(v) for k, v in BUILTIN.items()}
    if os.path.isdir(presets_dir):
        for fn in sorted(os.listdir(presets_dir)):
            if not fn.endswith(".toml"):
                continue
            name = fn[:-5]
            with open(os.path.join(presets_dir, fn), "rb") as f:
                custom = tomllib.load(f)
            if name == "lockdown":
                raise AgentboxError("the lockdown preset cannot be redefined")
            # A brand-new preset name inherits lockdown's restrictive values for every key it omits, so a
            # minimal user file is fail-closed (it used to inherit `python`, which quietly granted packages
            # egress and turned approvals off). A file named after a built-in customises that built-in.
            base = dict(presets.get(name, BUILTIN["lockdown"]))
            base.update(custom)
            _validate(base, f"preset {name}")
            presets[name] = base
    return presets


def _validate(values, where):
    """Every value a preset (or a project override of one) may hold, checked by the table's own validator
    for that key. `exec` stays a known podman value so its message is "not built" rather than "bad value",
    and it is refused wherever a project is defined or loaded, not only when a session is prepared."""
    for k, v in values.items():
        if k not in SETTINGS:
            raise AgentboxError(f"{where}: unknown setting {k!r}")
        projectkeys.validate_value(k, where, v)


def effective(project_name, project, presets):
    """Return {setting: (value, source)} for a project."""
    preset_name = project.get("preset")
    if preset_name not in presets:
        raise AgentboxError(f"project {project_name}: unknown preset {preset_name!r}; choose one of:\n  "
                            + describe(presets, sep="\n  "))
    preset = presets[preset_name]
    overrides = {k: v for k, v in project.items() if k in SETTINGS}
    _validate(overrides, f"project {project_name}")

    out = {k: (v, f"preset {preset_name}") for k, v in preset.items()}
    for k, v in overrides.items():
        out[k] = (v, "project")

    if out["egress"][0] == "open" and out["egress"][1] != "project":
        out["egress"] = ("packages", f"preset {preset_name} (open only when the project says so)")

    if preset_name == "lockdown":
        for k, v in overrides.items():
            if k == "podman":
                # tier AND live: `live` alone would hand a review-only project read access to the live
                # stack, which the ceiling is there to refuse.
                if v.get("tier") != "none" or v.get("live"):
                    raise AgentboxError(f"project {project_name}: lockdown allows no podman access (tier or "
                                        "live); switch to another preset to loosen")
                continue
            if LOOSENESS[k][v] > LOOSENESS[k][preset[k]]:
                raise AgentboxError(f"project {project_name}: lockdown is a ceiling; {k} = {v!r} would loosen "
                                    f"it (preset: {preset[k]!r}); switch to another preset to loosen")
        # observe_logs is a registry setting, not one of SETTINGS, so the loop above never sees it.
        if project.get("observe_logs"):
            raise AgentboxError(f"project {project_name}: lockdown allows no live-stack logs "
                                "(observe_logs); switch to another preset to loosen")
    if out["data"][0] == "ro-only" and project.get("data_rw"):
        raise AgentboxError(f"project {project_name}: preset {preset_name} allows read-only data grants only")
    return out
