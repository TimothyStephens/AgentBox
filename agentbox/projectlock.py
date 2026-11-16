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
import contextlib
import fcntl
import json
import os

from . import config
from .fsutil import write_private


def path(settings, project):
    return os.path.join(settings.project_dir(project), config.LOCK_FILE)


@contextlib.contextmanager
def guard(settings, project):
    """An exclusive flock on `<lock>.guard` for as long as the block runs. The O_EXCL file stays the record
    of who holds the project; this only serialises the launchers that are deciding about it, so "the holder
    is gone, clear it, take it" happens as one step (review 16 Nov, item 16). flock is released by the kernel
    when the process dies, so a crashed launcher cannot leave it held."""
    p = path(settings, project) + ".guard"
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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
        write_private(p, json.dumps(doc), atomic=False)
    except FileExistsError:
        return False, (read(settings, project) or {})
    return True, doc


def note_session(settings, project, session_id):
    """Record the session id once the session has one. The lock is taken before the session is named -- the
    race to lose is two launchers starting at once, which happens before either has a name."""
    doc = read(settings, project)
    if not doc or doc.get("unreadable"):
        return False
    doc["session"] = session_id
    p = path(settings, project)
    write_private(p, json.dumps(doc))
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


def _identity(doc):
    return (doc.get("pid"), doc.get("pid_start"), doc.get("session") or "")


def force_release(settings, project, holder=None):
    """Remove the lock of a holder already judged gone. With `holder` (the record that judgement was made
    about), the file is removed only while it still records THAT holder: between the judgement and here
    another launcher may have cleared the stale lock and taken its own, and removing that one would hand the
    project to two sessions (review 16 Nov, item 16; `session.take_project_lock` also serialises the whole
    judge-and-clear under a flock, this is the check that makes the window harmless if it is ever reached)."""
    if holder is not None:
        now = read(settings, project)
        if not now or now.get("unreadable") or _identity(now) != _identity(holder):
            return False
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
