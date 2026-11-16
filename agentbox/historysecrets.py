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
        p = subprocess.run(["git", "--no-pager", f"--git-dir={git_dir}", *scan.GIT_SAFE, "log",
                            "--all", "--format=", "--name-only", "--diff-filter=AM", "--no-renames"],
                           capture_output=True, text=True, timeout=timeout, env=_git_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AgentboxError(f"cannot read the git history of {git_dir} ({e}); refusing rather than guessing")
    if p.returncode != 0 and "does not have any commits" not in p.stderr:
        raise AgentboxError(f"cannot read the git history of {git_dir}: {p.stderr.strip()[-200:]}")
    names = sorted({line for line in p.stdout.splitlines() if line})
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
