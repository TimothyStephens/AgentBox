"""`ab screen`: a tmux workspace for agentbox.

A workspace is one tmux session per project, named `ab-<project>`, holding your builder agent, an optional
reviewer agent and host shells, laid out by a layout you pick or one chosen from your terminal width. Both
projects must be registered: every pane opens in the project's registered root, and an agent pane enters
the sandbox only through `ab session start` (or `ab session attach`, when the project already has a live
session), so all sandboxing stays in ab. The verbs mirror `ab session`: start, attach, ls, kill, plus
layouts and config.

Layouts come from <config dir>/screen.toml when that file exists (AB_CONFIG_DIR moves it with the rest;
the older name ab-ws.toml is read when screen.toml is absent), otherwise from the built-in defaults, which
`--print-config` prints. This was contrib/ab-ws until 10 Nov 2026 (docs/decisions.md).
"""
import os
import shlex
import shutil
import subprocess
import sys
import tomllib

from . import adapters, registry
from .errors import AgentboxError

CONFIG_NAME = "screen.toml"
PREFIX = "ab-"                 # tmux session name = PREFIX + project
LEGACY_CONFIG_NAME = "ab-ws.toml"

DEFAULT_CONFIG = r'''# ab screen layouts.  Save as ~/.config/agentbox/screen.toml
#   ab screen --print-config > ~/.config/agentbox/screen.toml
#
# When this file exists, ONLY the layouts in it are used (the built-ins are ignored),
# so keep any of the ones below that you still want.
#
# HOW A LAYOUT IS DESCRIBED
#   A layout is one or more tmux windows. Each window is a row of COLUMNS, and a
#   column can be split into stacked panes. You list the panes; panes with the same
#   column number stack top to bottom in the order you list them.
#
#   Pane keys:
#     column  1, 2, 3 ...   which column, counted from the left (required)
#     width   percent       column width; give it on one pane of the column
#                           (omitted widths share what is left equally)
#     height  percent       pane height within its column (omitted: share equally)
#     run     what runs in the pane (required):
#               "builder"   the builder agent:   ab session start <agent> <builder-project>
#               "reviewer"  the reviewer agent:  ab session start <agent> <reviewer-project>
#               "shell"     a host shell
#     start   true/false    start the command, or only type it for you to press Enter
#                           (default: true for builder, false for reviewer)
#     cmd     "..."         for a shell pane, a command to run in it (e.g. "btop")
#     dir     "builder" | "reviewer" | "/some/path"
#                           working directory: the builder or reviewer project's registered
#                           root, or a path (default: builder for builder and shell panes,
#                           reviewer for reviewer panes)
#
#   Reviewer panes are left out when you start ab screen without a reviewer project.
#     agent   "claude" | "omp"  which agent an agent pane starts (default: --agent, then "claude")
#
# CHOOSING A LAYOUT
#   default = "auto" picks the layout with the largest min_cols that fits your
#   terminal width (on a tie, the one listed first). Put a layout name instead to always use that one, or override
#   per run with:  ab screen --layout NAME ...

default = "auto"

# Ultrawide: builder | reviewer | shell, side by side.
[[layout]]
name     = "wide"
min_cols = 240

[[layout.window]]
name  = "work"
panes = [
  { column = 1, width = 40, run = "builder" },
  { column = 2, width = 35, run = "reviewer" },
  { column = 3, width = 25, run = "shell" },
]

# Normal screen: builder | shell, reviewer in a second window (Ctrl-b n to switch).
[[layout]]
name     = "narrow"
min_cols = 0

[[layout.window]]
name  = "build"
panes = [
  { column = 1, width = 65, run = "builder" },
  { column = 2, width = 35, run = "shell" },
]

[[layout.window]]
name  = "review"
panes = [
  { column = 1, run = "reviewer" },
]

# More examples (uncomment to use):
#
# Ultrawide with the shell column split: builder repo on top, review clone below
# (run the long `make itest` in the bottom one, on committed code).
#
# [[layout]]
# name     = "wide-2shell"
# min_cols = 240
#
# [[layout.window]]
# name  = "work"
# panes = [
#   { column = 1, width = 40, run = "builder" },
#   { column = 2, width = 35, run = "reviewer" },
#   { column = 3, width = 25, height = 50, run = "shell" },
#   { column = 3,             height = 50, run = "shell", dir = "reviewer" },
# ]
#
# Vertical (portrait) monitor: everything stacked in one column.
#
# [[layout]]
# name     = "portrait"
# min_cols = 0
#
# [[layout.window]]
# name  = "work"
# panes = [
#   { column = 1, height = 60, run = "builder" },
#   { column = 1, height = 40, run = "shell" },
# ]
'''

PANE_KEYS = {"column", "width", "height", "run", "start", "cmd", "dir", "agent"}
RUN_KINDS = {"builder", "reviewer", "shell"}


class ConfigError(Exception):
    pass


def load_config(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            try:
                return tomllib.load(f), path
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"{path}: {e}")
    return tomllib.loads(DEFAULT_CONFIG), "built-in defaults"


def validate(cfg, source):
    layouts = cfg.get("layout")
    if not isinstance(layouts, list) or not layouts:
        raise ConfigError(f"{source}: no [[layout]] defined")
    unknown_top = set(cfg) - {"default", "reviewer_dir", "layout"}     # reviewer_dir: old key, ignored
    if unknown_top:
        raise ConfigError(f"{source}: unknown top-level key(s): {', '.join(sorted(unknown_top))}")
    names = set()
    for li, lay in enumerate(layouts, 1):
        where = f"{source}: layout #{li}"
        name = lay.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where}: needs name = \"...\"")
        where = f"{source}: layout '{name}'"
        if name in names:
            raise ConfigError(f"{where}: defined twice")
        names.add(name)
        bad = set(lay) - {"name", "min_cols", "window"}
        if bad:
            raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(bad))}")
        if not isinstance(lay.get("min_cols", 0), int):
            raise ConfigError(f"{where}: min_cols must be a whole number")
        windows = lay.get("window")
        if not isinstance(windows, list) or not windows:
            raise ConfigError(f"{where}: needs at least one [[layout.window]]")
        for wi, win in enumerate(windows, 1):
            wwhere = f"{where}, window #{wi}"
            bad = set(win) - {"name", "panes"}
            if bad:
                raise ConfigError(f"{wwhere}: unknown key(s): {', '.join(sorted(bad))}")
            panes = win.get("panes")
            if not isinstance(panes, list) or not panes:
                raise ConfigError(f"{wwhere}: needs panes = [ ... ]")
            for pi, p in enumerate(panes, 1):
                pwhere = f"{wwhere}, pane #{pi}"
                if not isinstance(p, dict):
                    raise ConfigError(f"{pwhere}: each pane is {{ column = .., run = .. }}")
                bad = set(p) - PANE_KEYS
                if bad:
                    raise ConfigError(f"{pwhere}: unknown key(s): {', '.join(sorted(bad))}")
                if not isinstance(p.get("column"), int) or p["column"] < 1:
                    raise ConfigError(f"{pwhere}: column must be 1, 2, 3 ...")
                if p.get("run") not in RUN_KINDS:
                    raise ConfigError(f"{pwhere}: run must be one of {', '.join(sorted(RUN_KINDS))}")
                for k in ("width", "height"):
                    if k in p and not (isinstance(p[k], (int, float)) and 0 < p[k] < 100):
                        raise ConfigError(f"{pwhere}: {k} is a percent between 1 and 99")
                if "start" in p and not isinstance(p["start"], bool):
                    raise ConfigError(f"{pwhere}: start is true or false")
                if "cmd" in p and (p["run"] != "shell" or not isinstance(p["cmd"], str)):
                    raise ConfigError(f"{pwhere}: cmd is a string, and only for run = \"shell\"")
                if "dir" in p and not isinstance(p["dir"], str):
                    raise ConfigError(f"{pwhere}: dir is \"builder\", \"reviewer\" or a path")
    default = cfg.get("default", "auto")
    if default != "auto" and default not in names:
        raise ConfigError(f"{source}: default = \"{default}\" is not a layout name")


def pick_layout(cfg, requested, cols):
    layouts = cfg["layout"]
    name = requested or cfg.get("default", "auto")
    if name != "auto":
        for lay in layouts:
            if lay["name"] == name:
                return lay
        raise ConfigError(f"no layout named '{name}' (ab screen --list)")
    fitting = [l for l in layouts if l.get("min_cols", 0) <= cols]
    if fitting:
        return max(fitting, key=lambda l: l.get("min_cols", 0))
    return min(layouts, key=lambda l: l.get("min_cols", 0))


def shares(values, what="sizes"):
    """Percent list with None for unspecified -> normalized shares summing to 100."""
    given = sum(v for v in values if v is not None)
    missing = [v for v in values if v is None]
    if given > 100 or (given == 100 and missing):
        raise ConfigError(f"{what} {values} add up to more than 100%")
    fill = (100 - given) / len(missing) if missing else 0
    out = [fill if v is None else v for v in values]
    total = sum(out)
    return [v * 100 / total for v in out]


def plan_window(win, have_reviewer, where=""):
    """-> list of columns, each a list of (pane, height_share); widths alongside."""
    panes = [p for p in win["panes"] if have_reviewer or p["run"] != "reviewer"]
    if not panes:
        return None
    cols = {}
    widths = {}
    for p in panes:
        cols.setdefault(p["column"], []).append(p)
        if "width" in p:
            if p["column"] in widths and widths[p["column"]] != p["width"]:
                raise ConfigError(f"{where}: column {p['column']} has two different widths")
            widths[p["column"]] = p["width"]
    order = sorted(cols)
    w = shares([widths.get(c) for c in order], f"{where}: column widths")
    plan = []
    for c, cw in zip(order, w):
        hs = shares([p.get("height") for p in cols[c]], f"{where}: heights in column {c}")
        plan.append((cw, list(zip(cols[c], hs))))
    return plan


def split_pct(this_share, remaining_share):
    """Size (in % of the pane being split) to give to the NEW pane holding the rest."""
    return max(1, min(99, round((remaining_share - this_share) * 100 / remaining_share)))


class Tmux:
    def __init__(self, dry):
        self.dry = dry
        self.n = 0

    def __call__(self, *args, capture=False):
        if self.dry:
            print("tmux " + " ".join(shlex.quote(a) for a in args))
            if capture:
                self.n += 1
                return f"%{self.n}"
            return ""
        r = subprocess.run(["tmux", *args], check=True, text=True,
                           stdout=subprocess.PIPE if capture else None)
        return r.stdout.strip() if capture else ""


def pane_dir(p, builder_dir, reviewer_dir):
    d = p.get("dir") or ("reviewer" if p["run"] == "reviewer" else "builder")
    if d == "builder":
        return builder_dir
    if d == "reviewer":
        return reviewer_dir
    return os.path.expanduser(d)


def agent_command(project, live_session, agent):
    """What an agent pane types: `ab session start` normally, `ab session attach` when the project already
    has a live session (one per project, so start would only be refused with that pointer)."""
    if live_session:
        return f"ab session attach {shlex.quote(project)}"
    return f"ab session start {shlex.quote(agent)} {shlex.quote(project)}"


def pane_command(p, builder, reviewer, agent="claude", live=None):
    """-> (keys to type, press_enter). Agent panes enter the sandbox through ab, and only that way, so the
    sandboxing stays in ab. `live` maps a project to its live session id, or None."""
    live = live or {}
    a = p.get("agent") or agent
    if p["run"] == "builder":
        return agent_command(builder, live.get(builder), a), p.get("start", True)
    if p["run"] == "reviewer":
        return agent_command(reviewer, live.get(reviewer), a), p.get("start", False)
    if "cmd" in p:
        return p["cmd"], p.get("start", True)
    return None, False


def build(t, session, layout, builder, reviewer, builder_dir, reviewer_dir, cols, rows, agent="claude", live=None):
    first = True
    for wi, win in enumerate(layout["window"]):
        plan = plan_window(win, bool(reviewer),
                           f"layout '{layout['name']}', window '{win.get('name') or wi + 1}'")
        if plan is None:
            continue
        wname = win.get("name") or f"w{wi + 1}"
        top = plan[0][1][0][0]
        start_dir = pane_dir(top, builder_dir, reviewer_dir)
        if first:
            pid = t("new-session", "-d", "-s", session, "-n", wname, "-x", str(cols), "-y", str(rows),
                    "-c", start_dir, "-P", "-F", "#{pane_id}", capture=True)
            first = False
        else:
            pid = t("new-window", "-d", "-t", session + ":", "-n", wname, "-c", start_dir,
                    "-P", "-F", "#{pane_id}", capture=True)
        # Columns, left to right: each split hands the rest of the width to a new pane.
        col_tops = [pid]
        remaining = 100.0
        for i in range(1, len(plan)):
            this = plan[i - 1][0]
            d = pane_dir(plan[i][1][0][0], builder_dir, reviewer_dir)
            pid = t("split-window", "-h", "-t", col_tops[-1], "-l", f"{split_pct(this, remaining)}%",
                    "-c", d, "-P", "-F", "#{pane_id}", capture=True)
            remaining -= this
            col_tops.append(pid)
        # Rows inside each column, top to bottom.
        for (cw, cpanes), ctop in zip(plan, col_tops):
            ids = [ctop]
            remaining = 100.0
            for j in range(1, len(cpanes)):
                this = cpanes[j - 1][1]
                d = pane_dir(cpanes[j][0], builder_dir, reviewer_dir)
                pid = t("split-window", "-v", "-t", ids[-1], "-l", f"{split_pct(this, remaining)}%",
                        "-c", d, "-P", "-F", "#{pane_id}", capture=True)
                remaining -= this
                ids.append(pid)
            for (p, _), pane_id in zip(cpanes, ids):
                keys, enter = pane_command(p, builder, reviewer, agent, live)
                if keys:
                    t("send-keys", "-t", pane_id, keys, *(["Enter"] if enter else []))
    if first:
        raise ConfigError(f"layout '{layout['name']}' has no panes left (all were reviewer panes?)")
    t("select-window", "-t", session + ":^")
    t("select-pane", "-t", session + ":^.0")


# ---- tmux, as a queryable thing -------------------------------------------------------------------------

def tmux_available():
    return shutil.which("tmux") is not None


def _query(args):
    """stdout of a tmux query, or None when tmux is absent or the query fails (no server running counts)."""
    if not tmux_available():
        return None
    p = subprocess.run(["tmux", *args], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def workspaces():
    """[{name, project, windows, attached}] for every tmux session agentbox named (PREFIX + project)."""
    out = _query(["list-sessions", "-F", "#{session_name}\t#{session_windows}\t#{session_attached}"])
    rows = []
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].startswith(PREFIX):
            continue
        rows.append({"name": parts[0], "project": parts[0][len(PREFIX):],
                     "windows": int(parts[1] or 0), "attached": int(parts[2] or 0)})
    return rows


def workspace_name(project):
    return PREFIX + project


def _attach_argv(name):
    return ["switch-client", "-t", name] if os.environ.get("TMUX") else ["attach", "-t", name]


# ---- the verbs --------------------------------------------------------------------------------------------

def config_path(settings, given=None):
    if given:
        return os.path.expanduser(given)
    new = os.path.join(settings.config_dir, CONFIG_NAME)
    legacy = os.path.join(settings.config_dir, LEGACY_CONFIG_NAME)
    return legacy if not os.path.exists(new) and os.path.exists(legacy) else new


def _layout_config(settings, a):
    path = config_path(settings, getattr(a, "config", None))
    cfg, source = load_config(path)
    validate(cfg, source)
    return cfg, source


def _registered(settings, names):
    """{project: registered root} for every name, or the `ab project add` message for the first that is not.
    Both the builder and the reviewer must be registered: the panes open in the roots, and an agent pane's
    `ab session start` would refuse an unregistered project anyway, one step later and inside tmux."""
    from . import cli
    reg = registry.load(settings.registry_path)
    roots = {}
    for n in names:
        if n not in reg:
            cli._require_registered(settings, n)
        roots[n] = reg[n]["root"]
    return roots


def _live_sessions(settings, projects):
    """{project: live session id} for the projects that have one (through the launcher's own liveness)."""
    from . import session
    from .podman import Podman
    podman = Podman(settings.podman)
    out = {}
    for p in projects:
        sid = session.live_session_of(settings, podman, p)
        if sid:
            out[p] = sid
    return out


def start(settings, a, *, out=print, err=lambda m: print(m, file=sys.stderr)):
    """`ab screen start <builder> [reviewer]`: open the workspace and attach to it."""
    try:
        cfg, source = _layout_config(settings, a)
        names = [a.builder] + ([a.reviewer] if a.reviewer else [])
        roots = _registered(settings, names)
        size = shutil.get_terminal_size((200, 50))
        cols, rows = size.columns, size.lines
        layout = pick_layout(cfg, a.layout, cols)
        name = workspace_name(a.builder)
        if not a.dry_run and not tmux_available():
            raise AgentboxError("tmux is not on PATH; `ab screen` is a tmux layout (install tmux, or use "
                                "`ab session start` directly)")
        if not a.dry_run and any(w["name"] == name for w in workspaces()):
            raise AgentboxError(f"workspace {name} is already open: `ab screen attach {a.builder}` joins it, "
                                f"`ab screen kill {a.builder}` ends it")
        live = _live_sessions(settings, names)
        for p, sid in live.items():
            err(f"ab screen: {p} already has a live session ({sid}); its pane will attach to it")
        if not a.dry_run and not shutil.which("ab"):
            err("ab screen: `ab` is not on PATH; agent panes will fail to start")
        err(f"ab screen: layout '{layout['name']}' ({source})")
        t = Tmux(a.dry_run)
        build(t, name, layout, a.builder, a.reviewer, roots[a.builder],
              roots.get(a.reviewer, roots[a.builder]), cols, rows, a.agent, live)
        if a.dry_run:
            t(*_attach_argv(name))
            return 0
        os.execvp("tmux", ["tmux", *_attach_argv(name)])
    except ConfigError as e:
        err(f"ab screen: {e}")
        return 2
    except subprocess.CalledProcessError as e:
        err(f"ab screen: tmux failed: {e}")
        return 1


def _open_or_refuse(project):
    name = workspace_name(project)
    have = workspaces()
    if any(w["name"] == name for w in have):
        return name
    others = ", ".join(w["project"] for w in have) or "none"
    raise AgentboxError(f"no workspace for {project} is open (open: {others}); "
                        f"`ab screen start {project}` opens one, `ab screen ls` lists them")


def attach(settings, a, *, out=print, err=lambda m: print(m, file=sys.stderr)):
    """`ab screen attach <project>`: join the project's workspace (switch to it from inside tmux)."""
    if not tmux_available():
        raise AgentboxError("tmux is not on PATH, so there is no workspace to attach to")
    name = _open_or_refuse(a.project)
    if a.dry_run:
        out("tmux " + " ".join(shlex.quote(x) for x in _attach_argv(name)))
        return 0
    os.execvp("tmux", ["tmux", *_attach_argv(name)])


def ls(settings, a, *, out=print, err=lambda m: print(m, file=sys.stderr)):
    """`ab screen ls`: every open workspace, its project, and the project's live agentbox session."""
    if not tmux_available():
        out("no workspaces (tmux is not on PATH)")
        return 0
    rows = workspaces()
    if not rows:
        out("no workspaces open; `ab screen start <project>` opens one")
        return 0
    reg = registry.load(settings.registry_path)
    live = _live_sessions(settings, [w["project"] for w in rows])
    out(f"{'WORKSPACE':<28} {'PROJECT':<20} {'WINDOWS':>7} {'ATTACHED':>8}  SESSION")
    for w in rows:
        proj = w["project"] + ("" if w["project"] in reg else " (not registered)")
        out(f"{w['name']:<28} {proj:<20} {w['windows']:>7} {w['attached']:>8}  {live.get(w['project'], '-')}")
    return 0


def kill(settings, a, *, out=print, err=lambda m: print(m, file=sys.stderr)):
    """`ab screen kill <project>`: end the workspace. Every pane's process ends with it; an agent session in
    it gets SIGHUP, which the launcher tears down cleanly (scan and logs kept)."""
    if not tmux_available():
        raise AgentboxError("tmux is not on PATH, so there is no workspace to kill")
    name = _open_or_refuse(a.project)
    live = _live_sessions(settings, [a.project])
    if a.dry_run:
        out(f"tmux kill-session -t {shlex.quote(name)}")
        return 0
    subprocess.run(["tmux", "kill-session", "-t", name], check=True)
    out(f"killed workspace {name}" + (f"; its session {live[a.project]} is being torn down (SIGHUP)"
                                      if a.project in live else ""))
    return 0


def layouts(settings, a, *, out=print, err=lambda m: print(m, file=sys.stderr)):
    """`ab screen layouts`: the layouts available, and which one auto would pick at this terminal width."""
    try:
        cfg, source = _layout_config(settings, a)
    except ConfigError as e:
        err(f"ab screen: {e}")
        return 2
    cols = shutil.get_terminal_size((200, 50)).columns
    auto = pick_layout(cfg, "auto", cols)["name"]
    out(f"layouts from {source} (terminal is {cols} columns):")
    for lay in cfg["layout"]:
        mark = "  <- auto" if lay["name"] == auto else ""
        out(f"  {lay['name']:<16} min_cols {lay.get('min_cols', 0):<5}{mark}")
    out(f"default: {cfg.get('default', 'auto')}")
    return 0


def config(settings, a, *, out=print, err=lambda m: print(m, file=sys.stderr)):
    """`ab screen config`: a commented starter config, to redirect into <config dir>/screen.toml."""
    sys.stdout.write(DEFAULT_CONFIG)
    return 0


VERBS = {"start": start, "attach": attach, "ls": ls, "kill": kill, "layouts": layouts, "config": config}


def add_verbs(scr, add, ex):
    """The `ab screen` subparsers, built with the CLI's own `add(subparsers, name, summary, epilog)` and
    `ex(*lines)` helpers so the help reads like every other noun's."""
    x = add(scr, "start", "open a project's workspace: agent panes (each an `ab session start`) and host shells "
                          "in a layout, then attach to it",
            ex("ab screen start web", "ab screen start web web-review --layout wide",
               "ab screen start web --agent omp", "ab screen start web --dry-run   print the tmux commands"))
    x.add_argument("builder", help="the builder project (registered)")
    x.add_argument("reviewer", nargs="?", help="a reviewer project (registered; a separate clone: "
                                                "`ab project add <name> --reviews <builder>`)")
    x.add_argument("--agent", choices=adapters.NAMES, default="claude",
                   help="the agent the agent panes start (default: claude; a pane's own `agent` key wins)")
    x.add_argument("--layout", help="layout name (default: the config's `default`, usually auto)")
    x.add_argument("--config", metavar="FILE", help=f"layout config (default: <config dir>/{CONFIG_NAME})")
    x.add_argument("--dry-run", action="store_true", help="print the tmux commands, run nothing")
    x = add(scr, "attach", "join a project's open workspace (from inside tmux: switch to it)",
            ex("ab screen attach web"))
    x.add_argument("project", help="the project whose workspace to join")
    x.add_argument("--dry-run", action="store_true", help="print the tmux command, run nothing")
    add(scr, "ls", "list open workspaces with their project and its live session", ex("ab screen ls"))
    x = add(scr, "kill", "end a project's workspace; a session running in it is torn down (SIGHUP)",
            ex("ab screen kill web"))
    x.add_argument("project", help="the project whose workspace to end")
    x.add_argument("--dry-run", action="store_true", help="print the tmux command, run nothing")
    x = add(scr, "layouts", "list the layouts available, and which one auto would pick here",
            ex("ab screen layouts"))
    x.add_argument("--config", metavar="FILE", help=f"layout config (default: <config dir>/{CONFIG_NAME})")
    add(scr, "config", "print a commented starter config", ex(f"ab screen config > ~/.config/agentbox/{CONFIG_NAME}"))
