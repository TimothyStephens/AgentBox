"""Presets: named bundles of per-project settings, merged with project overrides.

Rules beyond a plain merge:
  * egress is `open` only when the project itself says so; a preset alone gives at most `packages`;
  * `lockdown` is a ceiling: an override may tighten it, never loosen it.
"""
import os
import tomllib

from .errors import AgentboxError

EGRESS = ("locked", "packages", "open")
ALLOWED = {
    "compose": ("ro", "rw"),
    "egress": EGRESS,
    "caps": ("refused", "opt-in"),
    "secrets": ("locked", "masked"),
    "approvals": ("on", "off"),
    "data": ("ro-only", "declared"),
}
SETTINGS = tuple(ALLOWED) + ("podman",)
PODMAN_TIERS = ("none", "observe", "exec", "dev")

BUILTIN = {
    "lockdown": {"compose": "ro", "egress": "locked", "podman": {"tier": "none"}, "caps": "refused",
                 "secrets": "locked", "approvals": "on", "data": "ro-only"},
    "pods": {"compose": "rw", "egress": "packages", "podman": {"tier": "dev", "live": "observe"},
             "caps": "opt-in", "secrets": "masked", "approvals": "off", "data": "declared"},
    "python": {"compose": "ro", "egress": "packages", "podman": {"tier": "none"}, "caps": "refused",
               "secrets": "masked", "approvals": "off", "data": "declared"},
}

# How loose each value is; lockdown's ceiling refuses anything looser than its own value.
LOOSENESS = {
    "compose": {"ro": 0, "rw": 1},
    "egress": {"locked": 0, "packages": 1, "open": 2},
    "caps": {"refused": 0, "opt-in": 1},
    "secrets": {"locked": 0, "masked": 1},
    "approvals": {"on": 0, "off": 1},
    "data": {"ro-only": 0, "declared": 1},
}


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
            base = dict(presets.get(name, BUILTIN["python"]))
            base.update(custom)
            _validate(base, f"preset {name}")
            presets[name] = base
    return presets


def _validate(values, where):
    for k, v in values.items():
        if k not in SETTINGS:
            raise AgentboxError(f"{where}: unknown setting {k!r}")
        if k == "podman":
            tier = v.get("tier") if isinstance(v, dict) else None
            if tier not in PODMAN_TIERS:
                raise AgentboxError(f"{where}: podman tier must be one of {', '.join(PODMAN_TIERS)}")
        elif v not in ALLOWED[k]:
            raise AgentboxError(f"{where}: {k} = {v!r}, expected one of {', '.join(ALLOWED[k])}")


def effective(project_name, project, presets, with_secrets=False):
    """Return {setting: (value, source)} for a project."""
    preset_name = project.get("preset")
    if preset_name not in presets:
        raise AgentboxError(f"project {project_name}: unknown preset {preset_name!r}")
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
                if v.get("tier") != "none":
                    raise AgentboxError(f"project {project_name}: lockdown allows no podman access; "
                                        "switch to another preset to loosen")
                continue
            if LOOSENESS[k][v] > LOOSENESS[k][preset[k]]:
                raise AgentboxError(f"project {project_name}: lockdown is a ceiling; {k} = {v!r} would loosen "
                                    f"it (preset: {preset[k]!r}); switch to another preset to loosen")
    if out["data"][0] == "ro-only" and project.get("data_rw"):
        raise AgentboxError(f"project {project_name}: preset {preset_name} allows read-only data grants only")

    if with_secrets:
        if out["secrets"][0] == "locked":
            raise AgentboxError(f"project {project_name}: --with-secrets is refused by {out['secrets'][1]}")
        out["secrets"] = ("exposed", "flag --with-secrets")
    return out
