"""The project registry: projects.toml, read with tomllib and written by a small emitter."""
import os
import re
import tomllib

from .errors import AgentboxError

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# Settings a project may carry. Preset settings (presets.SETTINGS) may also be written here as overrides.
PATH_KEYS = ("root", "data_ro", "data_rw")
PROJECT_KEYS = {"root", "preset", "data_ro", "data_rw", "mask_skip", "extra_domains", "swap", "build_memory",
                "history_secrets_accepted", "compose_project", "observe_logs", "dev_images"}


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, "rb") as f:
        doc = tomllib.load(f)
    return dict(doc.get("project", {}))


def _quote(s):
    if not isinstance(s, str):
        raise TypeError(f"registry values must be strings, got {s!r}")
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = "".join(c if c >= " " else f"\\u{ord(c):04x}" for c in out)
    return f'"{out}"'


def _value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_quote(k)} = {_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {v!r}")


def dump(projects):
    lines = ["# agentbox project registry. Edit with `ab project ...`, or by hand.", ""]
    for name in sorted(projects):
        if not NAME.match(name):
            raise AgentboxError(f"bad project name {name!r}")
        lines.append(f"[project.{name}]")
        for k, v in projects[name].items():
            lines.append(f"{k} = {_value(v)}")
        lines.append("")
    return "\n".join(lines)


def save(path, projects):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(dump(projects))
    os.replace(tmp, path)


def check_name(name):
    if not NAME.match(name or ""):
        raise AgentboxError(f"project name {name!r}: use lower-case letters, digits, '-' or '_' (max 40)")
