"""The data directory's project layout: the one-time move into `projects/`, and a project's identity.

**Why `projects/`** (decisions.md, 9 Nov 2026). Project state used to sit directly in the data dir, beside
agentbox's own `logs/`, `backups/`, `built-images/`, `profiles/` and `tmp/`. Nothing reserved those names, so
`ab project add logs ...` would have put that project's transcripts inside the kept logs; and one project's
things were spread over four top-level directories, which is what made `ab project purge` leave the gate's
built-image record and the backups behind. Now `projects/<name>/` holds everything one project owns
(config.py lists it), a project name cannot collide with anything, and purge removes one directory.

**Why an identity file.** `ab project rm` keeps the state so that re-adding the name reattaches it, which is
right when it is the same project and a quiet merge when it is not: a new project given an old name
inherited the old one's memory, inbox, backups and -- the part that matters under the threat model -- the
gate's record of the images the old project built, which the dev tier may run. Nothing on disk said which
root a state directory belonged to; the registry entry was the only link, and `rm` deletes it. So
`projects/<name>/project.json` records the root the state was made for. `ab project add` compares and
refuses a different root unless told `--reattach` (carry the state over) or `--fresh` (archive it first);
a session start compares too, against a registry edited by hand.
"""
import json
import os
import re
import time

from . import config, registry
from .errors import AgentboxError

STAMP = "%Y-%m-%dT%H:%M:%S"


def _is_project_name(name):
    return bool(registry.NAME.match(name))


def _move(src, dst, moved):
    os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
    os.rename(src, dst)
    moved.append(f"{src} -> {dst}")


def migrate(settings, warn=lambda m: None):
    """Move a flat data dir into the `projects/` layout, once. Returns what was moved, as `old -> new` lines.

    Three things lived at the top level keyed by project name: `<name>/` (state), `backups/<name>/` and
    `built-images/<name>.json`. Each moves under `projects/<name>/` by rename -- same filesystem, atomic, and a
    live session's bind mounts follow the directory (a mount is attached to the inode, not the path).
    Anything that is not a project name, or whose destination already exists, is left where it is and named
    in a warning: guessing would be how two projects' state gets merged."""
    data = settings.data_dir
    if not os.path.isdir(data):
        return []
    moved, left = [], []
    for name in sorted(os.listdir(data)):
        src = os.path.join(data, name)
        if name in config.OWN_DIRS or name.startswith(".") or not os.path.isdir(src) or os.path.islink(src):
            continue
        if not _is_project_name(name):
            left.append(f"{src}: not a project name")
            continue
        dst = settings.project_dir(name)
        if os.path.lexists(dst):
            left.append(f"{src}: {dst} already exists")
            continue
        _move(src, dst, moved)

    old_backups = os.path.join(data, "backups")
    if os.path.isdir(old_backups) and not os.path.islink(old_backups):
        for name in sorted(os.listdir(old_backups)):
            src = os.path.join(old_backups, name)
            if not os.path.isdir(src) or os.path.islink(src) or not _is_project_name(name):
                left.append(f"{src}: not a project's backups")
                continue
            dst = settings.project_backups_dir(name)
            if os.path.lexists(dst):
                left.append(f"{src}: {dst} already exists")
                continue
            _move(src, dst, moved)
        _rmdir_if_empty(old_backups)

    old_records = os.path.join(data, "built-images")
    if os.path.isdir(old_records) and not os.path.islink(old_records):
        for fn in sorted(os.listdir(old_records)):
            src = os.path.join(old_records, fn)
            if fn.endswith(".json.lock") or re.search(r"\.json\.\d+\.tmp$", fn):
                os.unlink(src)                      # builtrec's flock file and a dead writer's temp
                continue
            name = fn[:-5] if fn.endswith(".json") else None
            if not name or not _is_project_name(name) or not os.path.isfile(src) or os.path.islink(src):
                left.append(f"{src}: not a project's built-image record")
                continue
            dst = settings.built_record(name)
            if os.path.lexists(dst):
                left.append(f"{src}: {dst} already exists")
                continue
            _move(src, dst, moved)
        _rmdir_if_empty(old_records)

    if moved:
        warn(f"ab: moved {len(moved)} item(s) into {settings.projects_dir}/ -- one project is one directory "
             f"now (docs/decisions.md, 9 Nov 2026):\n  " + "\n  ".join(moved))
    if left:
        warn(f"ab: left in the old place, not a project's: \n  " + "\n  ".join(left))
    return moved


def _rmdir_if_empty(path):
    try:
        os.rmdir(path)
    except OSError:
        pass


# ---- identity --------------------------------------------------------------------------------------------

def read_identity(settings, project):
    """The project's `project.json`, or None when it has none (a project whose state predates the file)."""
    try:
        with open(settings.project_file(project)) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise AgentboxError(f"{settings.project_file(project)}: unreadable ({e}); fix or remove it")
    if not isinstance(doc, dict) or not isinstance(doc.get("root"), str):
        raise AgentboxError(f"{settings.project_file(project)}: not a project identity; fix or remove it")
    return doc


def write_identity(settings, project, root, *, created=None, previous=None, now=None):
    """Record which root the project's state belongs to. `previous` keeps the roots a `--reattach` carried
    the state over from, so the file says how it got here."""
    doc = {"name": project, "root": os.path.realpath(root),
           "created": created or time.strftime(STAMP, time.localtime(now))}
    if previous:
        doc["previous_roots"] = list(previous)
    path = settings.project_file(project)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return doc


def has_state(settings, project):
    """Is there anything of the project's on disk besides the identity file and the lock?"""
    base = settings.project_dir(project)
    if not os.path.isdir(base):
        return False
    if settings.agent_state_dirs(project):
        return True
    return os.path.isfile(settings.built_record(project)) or os.path.isdir(settings.project_backups_dir(project))


def mismatch_message(settings, project, ident, root):
    base = settings.project_dir(project)
    return (f"project {project}: the state at {base} belongs to {ident['root']} (registered "
            f"{ident.get('created', '?')}), and this root is {root}. Reusing the name would hand the new "
            f"project the old one's memory, inbox, backups and built-image record (which the dev tier may "
            f"run). Add --fresh to archive that state first (it goes to "
            f"{settings.project_backups_dir(project)}/agent-state-<stamp>/), --reattach to carry it over "
            f"on purpose, or `ab project purge {project} --yes` to delete it.")


def check_registration(settings, project, root, *, reattach=False, fresh=False):
    """`ab project add`'s identity check, before anything is saved. Returns what `add` should do after
    saving: "fresh" (archive, then write a new identity), "reattach" (rewrite the identity to this root,
    keeping the old root in it), "keep" (same root, or a legacy state dir with no identity yet: write one),
    or "new" (nothing on disk)."""
    if reattach and fresh:
        raise AgentboxError("--reattach and --fresh contradict each other: pick one")
    ident = read_identity(settings, project)
    root = os.path.realpath(root)
    if fresh:
        return "fresh" if has_state(settings, project) else "new"
    if not has_state(settings, project):
        return "new"                 # an identity with nothing behind it: nothing to merge, rewrite it
    if ident is None or ident["root"] == root:
        return "keep"
    if reattach:
        return "reattach"
    raise AgentboxError(mismatch_message(settings, project, ident, root))


def check_session(settings, project, root):
    """A session start's identity check: the registry and the state must agree on the root. A missing
    identity (state from before the file existed, or a project added by an older ab) is written now from the
    registry, which is the only authority there is for it."""
    ident = read_identity(settings, project)
    root = os.path.realpath(root)
    if ident is None:
        return write_identity(settings, project, root)
    if ident["root"] != root:
        raise AgentboxError(
            f"project {project}: the registry says its root is {root}, but the state at "
            f"{settings.project_dir(project)} belongs to {ident['root']} (registered {ident.get('created', '?')}). "
            f"Re-register it to say which you mean: `ab project rm {project}`, then `ab project add {project} "
            f"{root} --reattach` (keep the state) or `--fresh` (archive it).")
    return ident
