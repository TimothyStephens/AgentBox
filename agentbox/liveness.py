"""Which sessions exist and which are still doing work: the predicates `ab session ls`, `gc`, the project
lock and the registry guards rest on. Moved out of session.py (item 54); session re-exports it."""
import json
import os
import time

from .errors import AgentboxError
from .state import ephemeral_state


def _start_time(pid):
    """Process start time in clock ticks since boot (field 22 of /proc/<pid>/stat); None if gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _alive(meta):
    """The launcher that started the session is still running (same PID and the same process)."""
    pid = int(meta.get("pid", 0))
    return pid > 0 and _start_time(pid) is not None and _start_time(pid) == meta.get("pid_start")


STOPPING = "stopping.json"      # in the session run dir while a teardown runs (lifecycle.teardown writes it)


def stopping_marker(settings, session):
    """The teardown marker of a session: {pid, pid_start, began, step, step_since, released}, or None.

    `released` is set once the session's containers, pods, volumes and networks are gone and its project lock
    is about to be freed: from then on the session does no work in its project (a new one may start), but
    its teardown is still running (the end-of-session scan, the Toulmin end) and must not be reaped."""
    try:
        with open(os.path.join(settings.session_run_dir(session), STOPPING)) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def released(info):
    """The session's teardown has removed everything that ran and freed its project (marker `released`)."""
    return bool((info.get("marker") or {}).get("released"))


def session_metas(settings):
    """[(session, meta or None)] for every session run dir; None means session.json is missing or
    unparseable. Kept separate from sessions() so a podman failure cannot hide this signal, and vice
    versa."""
    out = []
    base = os.path.join(settings.run_dir, "sessions")
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return out
    except OSError as e:
        raise AgentboxError(f"cannot read the session directory {base}: {e}")
    for s in names:
        try:
            with open(os.path.join(settings.session_run_dir(s), "session.json")) as f:
                out.append((s, json.load(f)))
        except (OSError, ValueError):
            out.append((s, None))
    return out


def project_is_live(settings, podman, name):
    """Is anything of this project running? Returns (live, why).

    EVERY signal is consulted, ANY of them says live, and a signal that cannot be READ also says live.

    Three audits found this question answered wrong, each time the same way -- one signal taken for the
    whole truth, and an unreadable signal read as "nothing is running":
      * the launcher PID alone, so a dead launcher hid running containers (F2);
      * then container labels alone, so a session still in prepare() -- session.json written, containers not
        yet created -- was invisible (F-1);
      * and `podman ps` failing, a missing binary or unparseable output all returning "no containers",
        which every caller read as "nothing is running" (F-2, F-10).

    So the signals are: a running container labelled with the project; a session.json naming it whose
    launcher is alive; and any session run dir whose session.json cannot be read -- that one cannot be
    attributed to a project, so it counts for every project rather than being guessed at by parsing the
    session name."""
    reasons = []
    try:
        for c in podman.containers_json(settings.labels):
            labels = c.get("Labels") or {}
            if labels.get("agentbox.project") != name or (c.get("State") or "").lower() != "running":
                continue
            shown = (c.get("Names") or [(c.get("Id") or "")[:12]])[0]
            sess = labels.get("agentbox.session") or ""
            reasons.append(f"container {shown} (session {sess or '?'}) is running"
                           + (f" -- `ab session kill {sess}`" if sess else ""))
    except AgentboxError as e:
        return True, f"cannot tell whether its containers are running: {e}"
    try:
        metas = session_metas(settings)
    except AgentboxError as e:
        return True, f"cannot tell whether a session is starting: {e}"
    for sess, meta in metas:
        if meta is None:
            reasons.append(f"session {sess} has no readable session.json, so it cannot be told apart from a "
                           "session starting right now (`ab session gc` clears one left by a crash)")
        elif meta.get("project") == name and _alive(meta):
            reasons.append(f"session {sess} is running (launcher pid {meta.get('pid')}) -- "
                           f"`ab session kill {sess}`")
    return (True, "; ".join(reasons)) if reasons else (False, "")


def sessions(settings, podman):
    """{session: {'meta': dict|None, 'containers': [..], 'networks': [..], 'alive': bool}}"""
    out = {}
    for c in podman.containers_json(settings.labels):
        s = (c.get("Labels") or {}).get("agentbox.session")
        if s:
            names = c.get("Names") or []
            out.setdefault(s, {"containers": [], "networks": []})["containers"].append(
                {"name": names[0] if names else c.get("Id", "")[:12], "role": c["Labels"].get("agentbox.role"),
                 "state": c.get("State"), "project": c["Labels"].get("agentbox.project")})
    for n in podman.networks_json(settings.labels):
        s = (n.get("labels") or {}).get("agentbox.session")
        if s and n.get("name"):
            out.setdefault(s, {"containers": [], "networks": []})["networks"].append(n["name"])
    sess_dir = os.path.join(settings.run_dir, "sessions")
    if os.path.isdir(sess_dir):
        for s in os.listdir(sess_dir):
            out.setdefault(s, {"containers": [], "networks": []})
    for s, info in out.items():
        meta = None
        try:
            with open(os.path.join(settings.session_run_dir(s), "session.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
        info["meta"] = meta
        mark = stopping_marker(settings, s)
        info["marker"] = mark
        # a teardown that is still running (its process, which may not be the launcher: `ab session kill`)
        info["stopping"] = mark if mark and _alive(mark) else None
        # The launcher keeps running through its own teardown, so its pid alone would call a session that has
        # removed everything and freed its project "running" for as long as the end-of-session scan takes.
        info["alive"] = bool(meta) and _alive(meta) and not released(info)
    return out


def _newest_mtime(path):
    newest = None
    try:
        newest = os.stat(path).st_mtime
    except OSError:
        return None
    for dirpath, dirnames, filenames in os.walk(path):
        for n in dirnames + filenames:
            try:
                m = os.lstat(os.path.join(dirpath, n)).st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return newest


def idle_seconds(settings, meta, now=None):
    """Seconds since the agent last wrote to its state (transcripts, memory, settings) — a cheap proxy for
    how long a session has sat without the agent doing anything. None when it can't be told (no meta, or
    the state dir is missing). A forgotten session shows up here and in `ab session ls`."""
    if not meta or not meta.get("project") or not meta.get("agent"):
        return None
    dirs = [settings.state_dir(meta["project"], meta["agent"])]
    if meta.get("session"):          # a lockdown session writes an ephemeral per-session dir, not the persistent one
        dirs.append(os.path.join(ephemeral_state(settings, meta["session"]), meta["agent"]))
    mtimes = [m for m in (_newest_mtime(d) for d in dirs) if m is not None]
    if not mtimes:
        return None
    return max(0.0, (time.time() if now is None else now) - max(mtimes))


def _young(settings, session, seconds=60):
    """Was this session's run dir touched just now? Used to tell a session starting right now (no metadata
    yet) from a dead leftover.

    A run dir that does NOT EXIST is not a session starting up -- `prepare()` creates it and writes
    session.json before anything else of the session exists -- so absence is an answer: not young. Any other
    error means "cannot tell", which must still read as young, so gc never reaps a session mid-start.

    (The fail-open sweep made every OSError read as young, including ENOENT, which is how a crashed
    session's husk became unreapable: itest test_f_gc_stops_what_a_crashed_session_left, 29 Oct.)"""
    try:
        return time.time() - os.stat(settings.session_run_dir(session)).st_mtime < seconds
    except FileNotFoundError:
        return False
    except OSError:
        return True


# Containers a session runs for itself. None of them is the agent doing work: a crashed session leaves them
# running, and they are exactly what `ab session gc` exists to remove.
INFRA_ROLES = ("proxy", "sockguard", "gate", "holder", "dev")


def session_is_alive(settings, name, info):
    """Is this SESSION still doing work? Returns (alive, reason).

    Deliberately NOT `project_is_live`, and the difference is the point:

      * `project_is_live` guards a registry EDIT, so it is pessimistic -- any running container labelled
        with the project counts, and a signal it cannot read counts too. That is right there: editing a
        project under a running session is what it must prevent.
      * this guards REAPING, where the pessimistic answer is the wrong one. A crashed session leaves its
        proxy, sockguard and networks running; treating those as "alive" would make the husk immortal, and
        because project_is_live *does* count them, the husk's project would stay unmanageable for good
        (no `ab project set`/`rm` while it is live). `ab session kill <name>` stays the manual way out.

    Alive: a teardown of it is running ("stopping": nothing may reap a session whose end is still being
    written; `released(info)` says whether it still holds its project), the launcher process is alive
    ("launcher"), an AGENT container is still running ("agent"), or the session is being started this
    second -- its run dir exists and was touched seconds ago, before any container of it exists
    ("starting"). Infrastructure containers on their own are a husk.
    """
    if info.get("stopping"):
        return True, "stopping"
    if info["alive"]:
        return True, "launcher"
    if [c for c in info["containers"] if c.get("state") == "running" and c.get("role") == "agent"]:
        return True, "agent"
    if info["meta"] is None and _young(settings, name):
        return True, "starting"
    return False, ""
