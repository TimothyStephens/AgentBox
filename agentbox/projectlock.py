"""One live session per project, enforced by a lock file in the project's state directory.

Two sessions of one project shared its agent state with no locking: Claude's `.claude.json` and settings,
OMP's `agent/`, and the inbox, all last-writer-wins (docs/TODO.md carried this as the real fix). The lock
makes the overlap impossible instead of warning about it.

The file is created with `O_EXCL`, so taking it is atomic between two `ab session start`s racing, and it records who
holds it -- the launcher's pid (with its start time, so a recycled pid cannot impersonate it) and, once the
session has a name, the session id. A holder that is gone leaves the file behind; deciding whether a holder
is gone needs podman and the session table, so that judgement lives in `session.take_project_lock` and this
module stays pure file mechanics.
"""
import json
import os

from . import config
from .errors import AgentboxError


def path(settings, project):
    return os.path.join(settings.project_dir(project), config.LOCK_FILE)


def read(settings, project):
    """The lock's holder, or None when the lock is free. A file we cannot parse counts as held: it was
    written by something, and guessing it away is how two sessions would end up sharing state."""
    try:
        with open(path(settings, project)) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {"unreadable": True}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}


def acquire(settings, project, *, pid=None, pid_start=None, agent="", now=None):
    """Take the lock, or return the current holder without disturbing it.

    Returns (True, our record) or (False, the holder's record)."""
    import time
    p = path(settings, project)
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    doc = {"pid": pid if pid is not None else os.getpid(), "pid_start": pid_start,
           "agent": agent, "session": "", "taken": now if now is not None else time.time()}
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False, (read(settings, project) or {})
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
    return True, doc


def note_session(settings, project, session_id):
    """Record the session id once the session has one. The lock is taken before the session is named -- the
    race to lose is two launchers starting at once, which happens before either has a name."""
    doc = read(settings, project)
    if not doc or doc.get("unreadable"):
        return False
    doc["session"] = session_id
    p = path(settings, project)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
    os.replace(tmp, p)
    return True


def release(settings, project, *, pid=None, session_id=None):
    """Drop the lock if it is ours. Identified by pid or by session id, so both the launcher's own exit and
    a teardown that only knows the session can release it. Never removes someone else's lock."""
    doc = read(settings, project)
    if not doc:
        return False
    mine = (pid is not None and doc.get("pid") == pid) or \
           (session_id is not None and doc.get("session") and doc.get("session") == session_id)
    if not mine:
        return False
    try:
        os.unlink(path(settings, project))
    except FileNotFoundError:
        return False
    return True


def force_release(settings, project):
    """Remove the lock whoever holds it. Only for a holder already judged gone (and for its teardown)."""
    try:
        os.unlink(path(settings, project))
        return True
    except FileNotFoundError:
        return False


def projects_with_locks(settings):
    """Every project that has a lock file, from the data directory rather than the registry: a lock has to
    be releasable after `ab project rm`, and a session can outlive its registry entry."""
    return [name for name in settings.projects_on_disk() if os.path.isfile(path(settings, name))]


def release_for_session(settings, session_id):
    """Release whichever project lock names this session. Used by teardown, which knows the session id but
    not necessarily the project (a husk's metadata may be gone)."""
    if not session_id:
        return []
    freed = []
    for project in projects_with_locks(settings):
        if release(settings, project, session_id=session_id):
            freed.append(project)
    return freed


def holder_text(settings, project, doc):
    """How a held lock is described to the user: by PROJECT, never by session number."""
    doc = doc or {}
    if doc.get("unreadable"):
        return (f"{project} has a session lock that cannot be read ({path(settings, project)}); remove that "
                f"file once you are sure no session of {project} is running")
    sid = doc.get("session") or ""
    return f"{project} is running" + (f" (session {sid})" if sid else " (a session is starting up)")
