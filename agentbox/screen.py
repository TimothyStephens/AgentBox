"""`ab screen`: a tmux workspace for agentbox.

Opens a tmux session with your builder agent, an optional reviewer agent and host shells, laid out by a
layout you pick or one chosen from your terminal width. Agents are only ever started with
`ab session start`, so all sandboxing stays in ab. Re-running attaches to the existing workspace instead
of starting duplicates.

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

from . import adapters

CONFIG_NAME = "screen.toml"
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
#                           working directory (default: builder for builder and shell
#                           panes, reviewer for reviewer panes)
#
#   Reviewer panes are left out when you start ab screen without a reviewer project.
#     agent   "claude" | "omp"  which agent an agent pane starts (default: --agent, then "claude")
#
# CHOOSING A LAYOUT
#   default = "auto" picks the layout with the largest min_cols that fits your
#   terminal width (on a tie, the one listed first). Put a layout name instead to always use that one, or override
#   per run with:  ab screen --layout NAME ...

default = "auto"

# Where the reviewer's clone lives. {reviewer} is the reviewer project name.
reviewer_dir = "~/repos/{reviewer}"

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
    unknown_top = set(cfg) - {"default", "reviewer_dir", "layout"}
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


def pane_command(p, builder, reviewer, agent="claude"):
    """-> (keys to type, press_enter)"""
    a = shlex.quote(p.get("agent") or agent)
    if p["run"] == "builder":
        return f"ab session start {a} {shlex.quote(builder)}", p.get("start", True)
    if p["run"] == "reviewer":
        return f"ab session start {a} {shlex.quote(reviewer)}", p.get("start", False)
    if "cmd" in p:
        return p["cmd"], p.get("start", True)
    return None, False


def build(t, session, layout, builder, reviewer, builder_dir, reviewer_dir, cols, rows, agent="claude"):
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
                keys, enter = pane_command(p, builder, reviewer, agent)
                if keys:
                    t("send-keys", "-t", pane_id, keys, *(["Enter"] if enter else []))
    if first:
        raise ConfigError(f"layout '{layout['name']}' has no panes left (all were reviewer panes?)")
    t("select-window", "-t", session + ":^")
    t("select-pane", "-t", session + ":^.0")


def add_args(x):
    """`ab screen`'s arguments, on the subparser the CLI builds."""
    x.add_argument("builder", nargs="?", help="builder project (a registered project name)")
    x.add_argument("reviewer", nargs="?", help="reviewer project (a separate clone, registered too)")
    x.add_argument("--agent", choices=adapters.NAMES, default="claude",
                   help="the agent the agent panes start (default: claude; a pane's own `agent` key wins)")
    x.add_argument("--layout", help="layout name (default: from the config, usually auto)")
    x.add_argument("--config", metavar="FILE", help=f"config file (default: <config dir>/{CONFIG_NAME})")
    x.add_argument("--list", action="store_true", help="list layouts, and which one auto would pick")
    x.add_argument("--print-config", action="store_true", help="print a commented starter config")
    x.add_argument("--dry-run", action="store_true", help="print the tmux commands, run nothing")


def config_path(settings, given=None):
    if given:
        return os.path.expanduser(given)
    new = os.path.join(settings.config_dir, CONFIG_NAME)
    legacy = os.path.join(settings.config_dir, LEGACY_CONFIG_NAME)
    return legacy if not os.path.exists(new) and os.path.exists(legacy) else new


def run(settings, a, *, out=print, err=lambda m: print(m, file=sys.stderr)):
    """The command. Returns an exit status (0 ok, 1 tmux failed, 2 refused)."""
    if a.print_config:
        sys.stdout.write(DEFAULT_CONFIG)
        return 0
    path = config_path(settings, a.config)
    size = shutil.get_terminal_size((200, 50))
    cols, rows = size.columns, size.lines
    try:
        cfg, source = load_config(path)
        validate(cfg, source)
        if a.list:
            auto = pick_layout(cfg, "auto", cols)["name"]
            out(f"layouts from {source} (terminal is {cols} columns):")
            for lay in cfg["layout"]:
                mark = "  <- auto" if lay["name"] == auto else ""
                out(f"  {lay['name']:<16} min_cols {lay.get('min_cols', 0):<5}{mark}")
            out(f"default: {cfg.get('default', 'auto')}")
            return 0
        if not a.builder:
            raise ConfigError("give the builder project name (`ab screen <project>`), or --list / --print-config")
        layout = pick_layout(cfg, a.layout, cols)
        builder_dir = os.getcwd()
        reviewer_dir = builder_dir
        if a.reviewer:
            tmpl = cfg.get("reviewer_dir", "~/repos/{reviewer}")
            reviewer_dir = os.path.expanduser(tmpl.replace("{reviewer}", a.reviewer))
            if not os.path.isdir(reviewer_dir):
                err(f"ab screen: reviewer dir {reviewer_dir} not found; using {builder_dir}"
                    f" (set reviewer_dir in {path})")
                reviewer_dir = builder_dir
        session = "ab-" + a.builder
        t = Tmux(a.dry_run)
        attach = ["switch-client", "-t", session] if os.environ.get("TMUX") else ["attach", "-t", session]
        if not a.dry_run and not shutil.which("tmux"):
            raise ConfigError("tmux is not on PATH; `ab screen` is a tmux layout (install tmux, or use "
                              "`ab session start` directly)")
        exists = (not a.dry_run and
                  subprocess.run(["tmux", "has-session", "-t", session],
                                 stderr=subprocess.DEVNULL).returncode == 0)
        if not exists:
            if not a.dry_run and not shutil.which("ab"):
                err("ab screen: `ab` is not on PATH; agent panes will fail to start")
            err(f"ab screen: layout '{layout['name']}' ({source})")
            build(t, session, layout, a.builder, a.reviewer, builder_dir, reviewer_dir, cols, rows, a.agent)
        if a.dry_run:
            t(*attach)
            return 0
        os.execvp("tmux", ["tmux", *attach])
    except ConfigError as e:
        err(f"ab screen: {e}")
        return 2
    except subprocess.CalledProcessError as e:
        err(f"ab screen: tmux failed: {e}")
        return 1
