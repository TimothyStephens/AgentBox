"""`ab session`: the live runs -- start, attach, ls, preview, kill, gc (split out of cli.py; the parser scaffolding
stays there)."""
import os
import sys

from . import adapters, registry, session
from .cli_common import warn
from .errors import AgentboxError
from .podman import Podman


def _dur(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m"
    return f"{seconds}s"


def cmd_run(s, a):
    """`ab session start <agent> <project>`: flags like --shell are parsed normally, and anything after a
    `--` is passed through to the agent or shell (split off in main() as a.passthrough)."""
    agent, project_name = a.agent, a.project
    _require_registered(s, project_name)
    podman = Podman(s.podman)
    # One live session per project: the lock is taken before any work, so two `ab session start`s racing cannot both
    # get past here, and it is released by teardown however this session ends.
    with session.take_project_lock(s, podman, project_name, warn=warn) as lock:
        project = session.load_project(s, project_name)
        sess = session.make_session(s, agent, project, warn=warn, no_profile=a.no_profile)
        sess.prepare()
        lock.note(sess.name)
        command = sess.agent_command(shell=a.shell, extra=list(getattr(a, "passthrough", []) or []))
        if sys.stdin.isatty() and not a.no_tty:
            return sess.run_interactive(command, tty=True)
        p = sess.run_captured(command, stdin=sys.stdin.read() if not sys.stdin.isatty() else "",
                              timeout=a.timeout)
        sys.stdout.write(p.stdout or "")
        sys.stderr.write(p.stderr or "")
        return p.returncode


def _require_registered(s, project_name):
    """A project is a grant, so registering one is a decision -- never a side effect of `ab session start`.

    The message is the command to run, filled in: the root guessed from the repos dir when something is
    actually there, and left as a placeholder when it is not."""
    reg = registry.load(s.registry_path)
    if project_name in reg:
        return
    registry.check_name(project_name)
    guess = os.path.join(os.path.expanduser(s.repos_dir), project_name)
    root = guess if os.path.isdir(guess) else "<path to its code>"
    known = ", ".join(sorted(reg)) or "none yet"
    raise AgentboxError(
        f"no project named {project_name!r}. agentbox does not register one for you -- a project names a "
        f"grant the agent gets read-write, which is a decision, not a default. Register it, then run:"
        f"\n  ab project add {project_name} {root} --preset <preset>"
        f"\n  ab session start {project_name}"
        f"\n`ab project add --help` lists the presets. Registered now: {known}.")


def cmd_attach(s, a):
    """Re-attach a terminal to the project's running session.

    The agent is a container the launcher started, so this is `podman attach` on it: the same stdio the
    launcher has. If the launcher's own terminal is still open, both are attached to the one agent -- which
    is the honest shape of re-attaching to a process rather than to a multiplexer. Detach with ctrl-p ctrl-q
    (`ab screen`, the tmux workspace, is the planned version of this; design.md)."""
    podman = Podman(s.podman)
    name = a.project
    known = session.sessions(s, podman)
    # A project name (the usual) or a session id (what `ab session ls` prints): every session verb takes
    # either, through the same resolution as `kill` and `preview`.
    sid = session.live_session_of(s, podman, name)
    if not sid and name in known and session.session_is_alive(s, name, known[name])[0]:
        sid = name
    if not sid:
        if name in known:
            raise AgentboxError(f"session {name} is not running any more; `ab session ls` shows what is")
        _require_registered(s, name)
        raise AgentboxError(f"no session of {name} is running; start one with "
                            f"`ab session start <agent> {name}`")
    info = known.get(sid) or {}
    project = (info.get("meta") or {}).get("project") or next(
        (c.get("project") for c in info.get("containers") or [] if c.get("project")), name)
    agents = [c["name"] for c in info.get("containers") or []
              if c.get("role") == "agent" and c.get("state") == "running"]
    if not agents:
        raise AgentboxError(f"session {sid} of {project} is alive but its agent container is not running "
                            f"(it may be starting, or it may have exited). `ab session ls` shows it; "
                            f"`ab session kill {project}` ends it.")
    print(f"attaching to {project} (session {sid}); detach with ctrl-p ctrl-q", file=sys.stderr)
    return podman.interactive(["attach", agents[0]])


def cmd_ls(s, a):
    import time
    podman = Podman(s.podman)
    rows = session.sessions(s, podman)
    now = time.time()
    idle_warn = float(s.session_idle_warn_hours) * 3600
    flagged = []
    for name, info in sorted(rows.items()):
        meta = info["meta"] or {}
        state = "running" if info["alive"] else "dead"
        started = meta.get("started")
        start_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if started else "?"
        age = _dur(now - started) if started else "?"
        idle = session.idle_seconds(s, meta, now=now) if info["alive"] else None
        mark = ""
        if idle is not None and idle > idle_warn:
            mark = "  <-- idle"
            flagged.append((name, idle))
        # The PROJECT is what a user names (`ab session attach web`, `ab session kill web`); the session id is kept
        # because it is what the logs, the container labels and the inbox provenance carry.
        print(f"{meta.get('project', '?'):<16} {state:<7} {meta.get('agent', '?'):<7} "
              f"start {start_str}  age {age:<6} idle {_dur(idle):<6} "
              f"{len(info['containers'])}c/{len(info['networks'])}n  id {name}{mark}")
    for name, idle in flagged:
        project = (rows[name]["meta"] or {}).get("project") or name
        warn(f"ab: {project} has been idle {_dur(idle)} — if it is forgotten, end it with "
             f"`ab session kill {project}` (its logs and end-of-session scan are kept).")


def cmd_preview(s, a):
    from . import preview
    podman = Podman(s.podman)
    sid = _resolve_session(s, podman, a.session)       # a project name or a session id, as kill and attach
    if a.stop:
        gone = preview.stop(s, podman, sid)
        print("removed: " + (" ".join(gone) if gone else "no previews"))
        return 0
    if not a.target:
        raise AgentboxError("give <service>:<port> (or --stop)")
    port, desk = preview.start(s, podman, sid, a.target, a.port)
    print(f"preview of {a.target} in {sid}: http://127.0.0.1:{port}/ (on this host only)\n"
          f"from your desktop: {desk}\n  then open http://127.0.0.1:{port}/\n"
          "it ends with the session (or ab session preview --stop)")
    return 0


def _resolve_session(s, podman, name):
    """A session id or a project name. One session per project, so a project name is unambiguous -- and it
    is what the user sees everywhere else."""
    known = session.sessions(s, podman)
    if name in known:
        return name
    sid = session.live_session_of(s, podman, name)
    if sid:
        return sid
    of_project = sorted(k for k, info in known.items()
                        if (info.get("meta") or {}).get("project") == name
                        or any(c.get("project") == name for c in info.get("containers") or []))
    if len(of_project) == 1:
        return of_project[0]
    if of_project:
        raise AgentboxError(f"{name} has more than one session on disk ({', '.join(of_project)}); name the "
                            f"one you mean")
    raise AgentboxError(f"no session of {name} (and no session named {name}); `ab session ls` lists them")


def cmd_kill(s, a):
    podman = Podman(s.podman)
    sid = _resolve_session(s, podman, a.session)
    session.teardown(s, podman, sid, warn=warn, exit_reason="kill")
    print(f"killed {sid}")


def cmd_gc(s, a):
    removed = session.gc(s, Podman(s.podman), warn=warn)
    print("removed: " + (" ".join(removed) if removed else "nothing"))


def add_verbs(sess, add, ex):
    """The `ab session` subparsers, built with the CLI's own `add` and `ex` helpers (as screen.add_verbs is)."""
    x = add(sess, "start", "start an agent session in the foreground (one per project)",
            ex("ab session start claude web",
               "ab session start claude web --shell         a bash shell in the sandbox instead of the agent",
               "ab session start claude web -- --resume     pass everything after -- to the agent",
               "ab session attach web                       re-attach to the session already running"))
    x.add_argument("agent", choices=adapters.NAMES, help="the agent to run")
    x.add_argument("project", help="the registered project to run against; anything after a `--` is passed "
                                   "through to the agent or shell")
    x.add_argument("--shell", action="store_true", help="run a bash shell in the sandbox instead of the agent")
    x.add_argument("--no-tty", action="store_true",
                   help="do not allocate a TTY (for non-interactive or piped runs)")
    x.add_argument("--no-profile", action="store_true",
                   help="for this run only: load no host-owned profile (skills, sub-agents, commands, "
                        "instructions, settings) and no plugins, and use the harness image. agentbox's own "
                        "session notes still apply. `ab project set <p> --profile off` makes it permanent")
    x.add_argument("--timeout", type=int, default=3600, metavar="SECONDS",
                   help="time limit for a non-interactive run (default 3600; ignored for interactive sessions)")
    x.set_defaults(fn=cmd_run)

    x = add(sess, "attach", "re-attach a terminal to the session already running for a project",
            ex("ab session attach web", "ab session attach ab-claude-web-1   by session id"))
    x.add_argument("project", help="the project whose running session to attach to (one per project), or the "
                                   "session id itself")
    x.set_defaults(fn=cmd_attach)
    add(sess, "ls", "list sessions with their project, start time, age and idle time",
        ex("ab session ls")).set_defaults(fn=cmd_ls)
    x = add(sess, "kill", "tear down a session and everything it created (keeps its logs and end-of-session scan)",
            ex("ab session kill web", "ab session kill ab-claude-web-1   by session id"))
    x.add_argument("session", help="the project whose session to tear down, or the session id itself "
                                   "(`ab session ls` shows both)")
    x.set_defaults(fn=cmd_kill)
    x = add(sess, "preview",
            "open a session's dev service on 127.0.0.1 (reach it from your desktop with ssh -L)",
            ex("ab session preview web web:8000",
               "ab session preview web web:8000 --port 18080",
               "ab session preview web --stop"))
    x.add_argument("session", help="the project whose session to preview, or the session id itself "
                                   "(`ab session ls` shows both)")
    x.add_argument("target", nargs="?", metavar="SERVICE:PORT",
                   help="the dev-stack service (or container) and the port inside it, e.g. web:8000; omit with "
                        "--stop")
    x.add_argument("--port", type=int, metavar="PORT",
                   help="host port to publish on 127.0.0.1 (default: a free one, printed on start)")
    x.add_argument("--stop", action="store_true", help="remove this session's previews instead of opening one")
    x.set_defaults(fn=cmd_preview)
    add(sess, "gc", "remove what dead sessions left (containers, networks, sockets) and storage nothing "
                    "can use: session-built images, superseded personal image layers, unused profile "
                    "snapshots (runs automatically after boot)",
        ex("ab session gc")).set_defaults(fn=cmd_gc)
