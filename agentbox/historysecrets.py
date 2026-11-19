"""Masked file names in git history: the masks hide the working tree, never git's objects. Moved out of
session.py (item 54); session re-exports it."""
import os
import subprocess

from . import mounts, scan
from .errors import AgentboxError
from .fsutil import inside
from .project import _git_env


def _history_masked(git_dir, timeout):
    """Paths in one repo's history (all refs) whose names the masks cover. Raises if git cannot tell."""
    try:
        # -z: names NUL-separated and unquoted. Without it git quotes any name with a non-ASCII byte, a
        # newline, a tab or a `"` (`"caf\303\251.env"`), and the quotes hid it from the mask test (review
        # 16 Nov, R-1; the user's core.quotepath cannot help, since git's global config is not read here).
        p = subprocess.run(["git", "--no-pager", f"--git-dir={git_dir}", *scan.GIT_SAFE, "log", "-z",
                            "--all", "--format=", "--name-only", "--diff-filter=AM", "--no-renames"],
                           capture_output=True, text=True, errors="replace", timeout=timeout,
                           env=_git_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AgentboxError(f"cannot read the git history of {git_dir} ({e}); refusing rather than guessing")
    if p.returncode != 0 and "does not have any commits" not in p.stderr:
        raise AgentboxError(f"cannot read the git history of {git_dir}: {p.stderr.strip()[-200:]}")
    # With --format= git still separates commits by a newline, which lands at the front of the next name.
    names = sorted({n.lstrip("\n") for n in p.stdout.split("\0") if n.lstrip("\n")})
    return [n for n in names if mounts._masked(os.path.basename(n))
            or any(part in mounts.MASK_DIRS for part in n.split("/")[:-1])]


def history_secrets(root, grants, mask_skip=(), timeout=120):
    """Masked file names found in the history of every git repo the session can read: the masks hide the
    working tree, never git's objects. Names are shown relative to the project root (`<repo>/<path>` for a
    nested repo). Raises if a history cannot be read.

    It took the project's `swap` map until the swap targets stopped being part of "is this name masked?"
    (every target must now be a masked name anyway, audit F-12), so the parameter is gone rather than left
    to read as if it still narrowed the search."""
    found = set()
    for g in grants:
        dirs, _outside = mounts.find_git_dirs(g, mounts._rel_skips(root, g, mask_skip))
        for gd in dirs:
            if not any(inside(gd, x) for x in grants):
                continue                                    # not mounted, so not readable
            work = os.path.dirname(gd) if os.path.basename(gd) == ".git" else gd
            prefix = os.path.relpath(work, root)
            for n in _history_masked(gd, timeout):
                found.add(n if prefix == "." else os.path.join(prefix, n))
    return sorted(found)


def without_real_env(names, root, data_grants=()):
    """The history names that still count under `data_env = "on"`: an env file (`mounts.env_file`) of the
    project root is shown to the agent on purpose, so its name in git history exposes nothing more and is
    dropped. Names are relative to the root (history_secrets), so one in a data grant outside it starts with
    `..`; one in a data grant inside the root is kept too, as its working-tree file stays masked. Applied to
    the found set AND the stored acknowledgement, so turning the setting on does not re-prompt for names it
    makes moot."""
    data = [os.path.realpath(g) for g in data_grants]
    out = []
    for n in names:
        full = os.path.normpath(os.path.join(root, n))
        trusted = (mounts.env_file(os.path.basename(n)) and inside(full, root)
                   and not any(part in mounts.MASK_DIRS for part in n.split("/")[:-1])
                   and not any(inside(full, g) for g in data))
        if not trusted:
            out.append(n)
    return out


def history_secrets_changes(found, accepted):
    """(new, gone) between a stored history-secrets acknowledgement and what is in the history now."""
    return ([n for n in found if n not in accepted], [n for n in accepted if n not in found])


def history_secrets_message(project, names, accepted=()):
    shown = ", ".join(names[:15]) + (f" (and {len(names) - 15} more)" if len(names) > 15 else "")
    tail = ""
    if accepted:
        new, gone = history_secrets_changes(names, accepted)
        parts = ([f"new since the acknowledgement: {', '.join(new)}"] if new else []) + \
                ([f"no longer in the history: {', '.join(gone)}"] if gone else [])
        tail = " (" + "; ".join(parts) + ")" if parts else ""
    return (f"project {project}: {len(names)} masked file name(s) are in git history: {shown}{tail}. The agent can "
            "read them through git (`git log -p`, `git show`) even though the working-tree files are masked. "
            "Treat these secrets as exposed and rotate them (and consider rewriting the history). To go "
            "ahead once they are rotated, accept them with --accept-history-secrets "
            f"(`ab project accept-history-secrets {project}` for a registered project)")
