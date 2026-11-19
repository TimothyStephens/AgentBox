"""The project's one session slot for an `ab session start`: taken under projectlock.guard, a dead holder's
session reaped first. Moved out of session.py (item 54); session re-exports it."""
import os

from . import projectlock
from .errors import AgentboxError
from .lifecycle import teardown
from .liveness import _start_time, released, session_is_alive, sessions, stopping_text


class ProjectLock:
    """The project's one session slot, held for the life of an `ab session start`.

    Released by `teardown()` as well, keyed by session id, so every path that ends a session frees it: a
    normal exit, an exception in prepare(), SIGINT/SIGTERM/SIGHUP (`_guarded` tears down on all three), and
    `ab session kill`. SIGKILL cannot release it -- that is what the stale check is for."""

    def __init__(self, settings, project, pid, pid_start):
        self.settings, self.project, self.pid, self.pid_start = settings, project, pid, pid_start
        self.session = ""

    def note(self, session_id):
        self.session = session_id
        projectlock.note_session(self.settings, self.project, session_id)

    def release(self):
        return projectlock.release(self.settings, self.project, pid=self.pid)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def lock_holder_state(settings, podman, holder):
    """(gone, why) for whoever holds a project's session lock.

    Gone means: its launcher process is not there AND nothing of its session is running or starting. The
    pid is checked with its start time, so a recycled pid is not mistaken for the original.

    A launcher that is still alive is never treated as gone, even with no containers left -- a session in
    prepare() looks exactly like that, and three audits ran into the version of this question that guessed
    (F3, F2, F-1). A hung launcher therefore holds its project until `ab session kill` ends it, which is
    the one action that can actually stop it; tearing down containers it does not have would free nothing.

    Raises if podman cannot be read: a lock is never stolen on a guess."""
    pid, start = holder.get("pid"), holder.get("pid_start")
    if isinstance(pid, int) and pid > 0 and _start_time(pid) is not None and _start_time(pid) == start:
        return False, f"its launcher (pid {pid}) is still running"
    sid = holder.get("session") or ""
    if not sid:
        # Taken, but the session was never named: either a launcher that died in its first moments, or one
        # that is naming its session right now. The pid answers it, and the pid is gone.
        return True, "the session it was taking was never named, and its launcher is gone"
    info = sessions(settings, podman).get(sid)
    if info:
        alive, why = session_is_alive(settings, sid, info)
        if alive:
            return False, f"session {sid} is still alive ({why})"
    return True, f"its launcher is gone and nothing of session {sid} is running"


def take_project_lock(settings, podman, project, warn=lambda m: None):
    """Hold the project's one session slot, reaping a dead holder's session first.

    Returns a ProjectLock. Raises AgentboxError naming `ab session attach` when a live session already holds it.
    The whole judgement runs under projectlock.guard, so two launchers cannot both find the same stale lock,
    clear it, and each take a fresh one (item 16)."""
    with projectlock.guard(settings, project):
        return _take_project_lock(settings, podman, project, warn)


def _take_project_lock(settings, podman, project, warn):
    pid = os.getpid()
    ok, holder = projectlock.acquire(settings, project, pid=pid, pid_start=_start_time(pid))
    if ok:
        lock = ProjectLock(settings, project, pid, _start_time(pid))
        # Backstop: a live session of this project with no lock file at all. That happens to a session
        # started before the lock existed, and to one whose lock was removed by hand -- and the lock would
        # otherwise be handed out while that session is still writing the project's state.
        other = live_session_of(settings, podman, project)
        if other:
            lock.release()
            raise AgentboxError(
                f"{project} is running (session {other}), although it holds no session lock -- it was "
                f"started before agentbox enforced one, or its lock file was removed. "
                f"`ab session attach {project}` joins it, `ab session kill {project}` ends it. One session per "
                f"project: two would share {project}'s agent state and inbox, last writer wins.")
        return lock
    if holder.get("unreadable"):
        raise AgentboxError(projectlock.holder_text(settings, project, holder))
    gone, why = lock_holder_state(settings, podman, holder)
    if not gone:
        sid = holder.get("session") or ""
        mark = ((sessions(settings, podman).get(sid) or {}).get("stopping")) if sid else None
        if mark:
            # its teardown holds the project only until its containers and networks are gone (decisions.md,
            # 19 Nov 2026), so this is short unless a removal hangs
            raise AgentboxError(
                f"{project} is shutting down ({stopping_text(mark)}; session {sid}); start it again once that "
                f"is done, or `ab session kill {project}` ends it now (its logs and end-of-session scan are "
                f"kept). [{why}]")
        raise AgentboxError(
            f"{projectlock.holder_text(settings, project, holder)}; `ab session attach {project}` joins it"
            + (f", `ab session kill {project}` ends it (its logs and end-of-session scan are kept)"
               if sid else "")
            + ". One session per project: two would share "
            + f"{project}'s agent state and inbox, last writer wins. [{why}]")
    sid = holder.get("session") or ""
    if sid:
        info = sessions(settings, podman).get(sid)
        left = ((info or {}).get("containers") or []), ((info or {}).get("networks") or [])
        teardown(settings, podman, sid, warn=warn, exit_reason="gc")
        warn(f"agentbox: reaped session {sid} of {project} before starting: {why}. Removed "
             f"{len(left[0])} container(s) and {len(left[1])} network(s)"
             + (f" ({', '.join([c['name'] for c in left[0]] + list(left[1]))})" if any(left) else "")
             + ". Its logs and end-of-session scan were kept.")
    else:
        warn(f"agentbox: cleared a stale session lock on {project}: {why}.")
    projectlock.force_release(settings, project, holder=holder)
    ok, holder = projectlock.acquire(settings, project, pid=pid, pid_start=_start_time(pid))
    if not ok:
        raise AgentboxError(f"{projectlock.holder_text(settings, project, holder)} (it was taken while the "
                            f"stale one was being cleared); try again")
    return ProjectLock(settings, project, pid, _start_time(pid))


def _of_project(info, project):
    return (info.get("meta") or {}).get("project") == project or any(
        c.get("project") == project for c in info.get("containers") or [])


def live_session_of(settings, podman, project):
    """The id of the project's live session, or None. One per project, so there is no ambiguity.

    A session whose teardown has already removed everything and freed the project (`released`) is not the
    project's live session: a new one may start while the old one's end-of-session scan still runs."""
    for sid, info in sorted(sessions(settings, podman).items()):
        if _of_project(info, project) and not released(info) and session_is_alive(settings, sid, info)[0]:
            return sid
    return None


def sessions_in_use(settings, podman, project):
    """Every session of the project that is alive, a released one still being torn down included: what a
    caller must not remove from under it (its clone, which its end-of-session scan reads)."""
    return {sid for sid, info in sessions(settings, podman).items()
            if _of_project(info, project) and session_is_alive(settings, sid, info)[0]}
