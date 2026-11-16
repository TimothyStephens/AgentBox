#!/usr/bin/env python3
"""PreToolUse guard for building agentbox on ubuntu-dev.

Claude Code runs as vmuser on the host while it builds the sandbox, so it can reach the
live stacks. This hook refuses the commands that could damage them or leak credentials.
It sees only the command text Claude writes: a script Claude writes and then runs is not
inspected. It guards against mistakes, not against a hostile agent.

Exit 0 = no objection (normal permissions apply). Exit 2 = blocked; stderr goes to Claude.
"""
import json
import os
import re
import shlex
import sys

TEST_PREFIXES = ("abt-", "abt_", "spike")          # names the build may create and destroy
TEST_LABELS = ("agentbox-test=1", "agentbox-spike=1")
LIVE_NAMES = re.compile(r"\b(media[_-]?server|life[_-]?updates|ollama)\b", re.I)
LIVE_PROJECT = re.compile(r"(?:-p|--project-name)[= ]+deploy\b|io\.podman\.compose\.project=deploy\b")
SECRETS = re.compile(r"claude-oauth-token|\.credentials\.json|(?:^|[\s/~])\.ssh/|sk-ant-[A-Za-z0-9]")
WRAPPERS = {"timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin", "env", "exec"}
DESTRUCTIVE = {"rm", "rmi", "stop", "kill", "restart", "pause", "unpause", "exec", "cp",
               "commit", "export", "checkpoint", "restore", "update", "rename", "attach"}
GROUP_DESTRUCTIVE = {("volume", "rm"), ("volume", "export"), ("volume", "import"),
                     ("network", "rm"), ("network", "connect"), ("network", "disconnect"),
                     ("pod", "rm"), ("pod", "stop"), ("pod", "kill"), ("pod", "restart"),
                     ("pod", "pause"), ("image", "rm"), ("secret", "rm")}
GROUPS = {"container", "image", "volume", "network", "pod", "secret", "system", "machine"}
PODMAN_GLOBAL_ARG = {"--url", "--connection", "-c", "--log-level", "--root", "--runroot",
                     "--storage-driver", "--identity", "--module", "--config"}
OPT_WITH_ARG = {"-t", "--time", "--signal", "-s", "--filter", "--depend", "-u", "--user",
                "-w", "--workdir", "-e", "--env", "--env-file", "--detach-keys"}


def block(reason):
    sys.stderr.write("agentbox guard: blocked: %s. Do not work around this; if it is really "
                     "needed, stop and ask Tim to run it himself.\n" % reason)
    sys.exit(2)


SEPARATORS = ";&|()<>\n"          # shell punctuation that ends a simple command (shlex returns runs of it as tokens)
XARGS_WITH_ARG = {"-I", "-i", "-n", "-L", "-l", "-P", "-s", "-d", "-E", "-e", "-a", "--max-args", "--max-procs",
                  "--max-lines", "--max-chars", "--replace", "--delimiter", "--arg-file", "--eof",
                  "--process-slot-var"}
HEREDOC = re.compile(r"<<-?\s*([\"']?)(\w+)\1")


def _lex(text):
    """Shell words, with every run of SEPARATORS as its own token. Quotes are honoured, so a `;` or a
    newline inside `-m '...'` stays inside the message instead of starting a new command (16 Nov)."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=SEPARATORS)
    lex.whitespace = " \t\r"           # a newline separates commands: it must not vanish as whitespace
    lex.whitespace_split = True
    return list(lex)


def _simple_commands(tokens):
    """[(toks, src)] for each simple command in a token stream; src is the command piped into it, else None."""
    out, cur, piped = [], [], False
    for t in tokens + ["\n"]:
        if t and all(c in SEPARATORS for c in t):
            if cur:
                out.append((cur, out[-1][0] if piped and out else None))
            piped, cur = t.strip() in ("|", "|&"), []
        else:
            cur.append(t)
    return out


def _crude(text):
    """The pre-16-Nov splitter, for text shlex cannot read (an apostrophe in a heredoc body): split on
    separators first, then shlex each piece or fall back to whitespace."""
    for p in re.split(r"\|\||&&|[;|&\n]", text):
        p = p.strip().lstrip("(").rstrip(")")
        if not p:
            continue
        try:
            yield shlex.split(p, comments=True), None
        except ValueError:
            yield p.split(), None


def _without_heredoc_bodies(text):
    """(text with heredoc bodies cut out, the body lines). The bodies are data to the command that reads
    them, but `bash <<EOF` runs them, so the caller still scans every body line as a command."""
    kept, bodies, term = [], [], None
    for line in text.split("\n"):
        if term is not None:
            if line.strip() == term:
                term = None
            else:
                bodies.append(line)
            continue
        kept.append(line)
        m = HEREDOC.search(line)
        if m:
            term = m.group(2)
    return "\n".join(kept), bodies


def _commands(text):
    try:
        return _simple_commands(_lex(text))
    except ValueError:
        pass
    head, bodies = _without_heredoc_bodies(text)
    try:
        cmds = _simple_commands(_lex(head))
    except ValueError:
        return list(_crude(text))
    for line in bodies:
        cmds += list(_crude(line))
    return cmds


def _strip_prefix(toks):
    """Drop leading VAR=value assignments and wrappers (timeout 30, env, nohup ...)."""
    while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in WRAPPERS):
        if toks[0] == "timeout" and len(toks) > 1 and re.match(r"^\d", toks[1]):
            toks = toks[2:]
        else:
            toks = toks[1:]
    return toks


def segments(cmd):
    """Yield (toks, src) for every simple command in `cmd`, including those inside $(...) (quoted or not:
    `-m "$(podman rm ...)"` runs podman all the same). src is the command piped into this one, or None."""
    cmds = _commands(cmd)
    for a in re.findall(r"\$\(([^()]*)\)", cmd) + re.findall(r"`([^`]*)`", cmd):
        cmds += _commands(a)       # a substitution is a command of its own, quoted or not
    for toks, src in cmds:
        toks = _strip_prefix(toks)
        if toks:
            yield toks, _strip_prefix(src or [])


def xargs_command(args):
    """The command xargs runs, with xargs's own options removed."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in XARGS_WITH_ARG else 1
    return args[i:]


def shell_c_string(toks):
    """The string `sh -c`/`bash -lc` runs, or None when the shell reads a script or stdin."""
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] == "-o":
            i += 2
            continue
        if not toks[i].startswith("--") and "c" in toks[i][1:]:
            return toks[i + 1] if i + 1 < len(toks) else None
        i += 1
    return None


def is_test_name(n):
    return n.startswith(TEST_PREFIXES)


def expand(path, cwd):
    path = path.replace("${HOME}", "~").replace("$HOME", "~")
    path = os.path.expanduser(path)
    return os.path.normpath(os.path.join(cwd, path))


def allowed_path(p, root):
    tmp = ("/tmp", "/var/tmp", os.environ.get("XDG_RUNTIME_DIR") or "/run/user")
    return (p != root and p.startswith(root + os.sep)) or any(
        p != t and p.startswith(t.rstrip("/") + os.sep) for t in tmp)


def unexpanded(path):
    """A path whose value only the shell knows: $(...), ${...}, a backtick, or a leading $VAR (which would
    otherwise read as a name relative to the cwd, i.e. inside the repo). $HOME is expanded before this."""
    path = path.replace("${HOME}", "~").replace("$HOME", "~")
    return bool(re.search(r"\$[({]|`", path) or path.startswith("$"))


def check_rm(toks, cwd, root):
    flags = [t for t in toks[1:] if t.startswith("-")]
    recursive = any(t in ("--recursive",) or (not t.startswith("--") and re.search(r"[rR]", t)) for t in flags)
    if not recursive:
        return
    for t in toks[1:]:
        if t.startswith("-"):
            continue
        p = expand(t, cwd)
        if unexpanded(t):
            block("recursive rm of %s: the guard cannot see what $(...), ${...} or a backtick expands to; "
                  "expand it yourself and pass the path" % t)
        if not allowed_path(p.rstrip("*").rstrip("/") or "/", root):
            block("recursive rm outside the repo and /tmp (%s)" % t)


def podman_verb(toks):
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if toks[i] in PODMAN_GLOBAL_ARG and "=" not in toks[i] else 1
    rest = toks[i:]
    if not rest:
        return None, None, []
    if rest[0] in GROUPS and len(rest) > 1:
        return rest[0], rest[1], rest[2:]
    return None, rest[0], rest[1:]


def names_in(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            if a in OPT_WITH_ARG:
                skip = True
            continue
        out.append(a)
    return out


def check_mounts(args, root, cwd):
    vals = []
    for i, a in enumerate(args):
        if a in ("-v", "--volume", "--mount") and i + 1 < len(args):
            vals.append(args[i + 1])
        elif a.startswith(("--volume=", "--mount=", "-v=")):
            vals.append(a.split("=", 1)[1])
        elif a.startswith("-v") and len(a) > 2:       # -v/src:/dst, the attached form
            vals.append(a[2:])
    for v in vals:
        if "type=" in v or "src=" in v or "source=" in v:
            m = re.search(r"(?:src|source)=([^,]+)", v)
            src = m.group(1) if m else ""
        else:
            src = v.split(":", 1)[0]
        if not src:
            continue
        if unexpanded(src):
            block("mounting %s: the guard cannot see what $(...), ${...} or a backtick expands to; expand it "
                  "yourself and pass the path" % src)
        if src.startswith(("/", "~", "$HOME", "${HOME}", ".")):
            if not allowed_path(expand(src, cwd), root):
                block("mounting %s: test containers may only mount paths inside the repo or /tmp" % src)
        elif not is_test_name(src):
            block("named volume %s: test volumes must be named abt-* or spike*" % src)


NAMESPACE_OPT = re.compile(r"^--(pid|ipc|uts|userns|cgroupns|net|network)(?:=(.*))?$")


def check_namespaces(args):
    """No host namespace of any kind, and no joining another container's: --pid=host and --ipc=host reach
    the live stacks' processes as surely as --network host reaches their ports (16 Nov)."""
    for i, a in enumerate(args):
        m = NAMESPACE_OPT.match(a)
        if not m:
            continue
        val = m.group(2) if m.group(2) is not None else (args[i + 1] if i + 1 < len(args) else "")
        if val == "host":
            block("--%s=host shares the host's %s namespace" % (m.group(1), m.group(1)))
        if re.match(r"^(container|ns):", val):
            block("joining another container's or process's %s namespace" % m.group(1))


def check_podman(toks, root, cwd, src=None, via_xargs=False):
    group, verb, args = podman_verb(toks)
    if verb is None:
        return
    if group == "system" and verb in ("reset", "prune", "migrate", "renumber"):
        block("podman system %s affects every container you own" % verb)
    if verb == "prune":
        block("podman %s prune affects resources beyond this build" % (group or ""))
    if verb == "unshare":
        block("podman unshare gives root over your files inside the user namespace")
    if verb in ("run", "create") or (group == "pod" and verb == "create") or (group == "container" and verb in ("run", "create")):
        if "--privileged" in args:
            block("--privileged containers")
        check_namespaces(args)
        check_mounts(args, root, cwd)
        return
    if verb == "build" and group in (None, "image"):
        # a build's -v/--mount binds host paths into every RUN step, like a run's
        check_namespaces(args)
        check_mounts(args, root, cwd)
        return
    if (group, verb) in GROUP_DESTRUCTIVE or (group in (None, "container", "image") and verb in DESTRUCTIVE):
        if any(a == "--all" or re.match(r"^-[a-zA-Z]*a[a-zA-Z]*$", a) for a in args):
            block("podman %s --all" % verb)
        if any(a in ("-l", "--latest") for a in args):
            block("podman %s --latest acts on whatever was created last, which may not be yours; name the "
                  "abt-* resource" % verb)
        if via_xargs:
            fed_by_label = any(lab in t for t in (src or []) for lab in TEST_LABELS)
            if not fed_by_label:
                block("xargs podman %s takes its targets from stdin: the command feeding it must select them "
                      "with --filter label=agentbox-test=1" % verb)
        filters = [args[i + 1] for i, a in enumerate(args) if a == "--filter" and i + 1 < len(args)]
        filters += [a.split("=", 1)[1] for a in args if a.startswith("--filter=")]
        for f in filters:
            if not any(lab in f for lab in TEST_LABELS):
                block("--filter %s: destructive filters must select label agentbox-test=1" % f)
        targets = names_in(args)
        if verb == "exec" or verb == "attach":
            targets = targets[:1]
        if verb == "cp":
            targets = [t.split(":", 1)[0] for t in targets if ":" in t]
        if (group, verb) in (("network", "connect"), ("network", "disconnect")):
            targets = targets[:2]
        bad = [t for t in targets if not is_test_name(t)]
        if bad and not filters:
            block("podman %s%s on %s: only abt-* or spike* resources may be changed "
                  "(IDs are not accepted; use names or --filter label=agentbox-test=1)"
                  % ((group + " ") if group else "", verb, ", ".join(bad)))


GIT_GLOBAL_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
COMMIT_VALUE_SHORT = set("mFcCt")   # short options of `git commit` that take a value
COMMIT_VALUE_LONG = {"--message", "--file", "--reuse-message", "--reedit-message", "--template", "--author",
                     "--date", "--cleanup", "--fixup", "--squash", "--trailer"}


def check_git(toks):
    """Refuse commits that skip hooks: --no-verify, -n (alone or in a cluster), or a hooksPath override."""
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] == "-c" and i + 1 < len(toks) and toks[i + 1].lower().startswith("core.hookspath"):
            block("git -c core.hooksPath overrides the commit hooks")
        i += 2 if toks[i] in GIT_GLOBAL_ARG else 1
    if i >= len(toks) or toks[i] not in ("commit", "merge", "push"):
        return
    sub, args, j = toks[i], toks[i + 1:], 0
    while j < len(args):
        a = args[j]
        if a == "--":
            break
        if a.startswith("--no-v"):
            # git takes any unambiguous prefix of a long option, so --no-veri is --no-verify too
            block("git %s --no-verify skips the hooks (the pre-commit secret scan)" % sub)
        if sub != "commit":
            j += 1
            continue
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                if ch == "n":
                    block("git commit -n skips the pre-commit secret scan")
                if ch in COMMIT_VALUE_SHORT:
                    if k == len(a) - 2:
                        j += 1          # the value is the next token
                    break
        elif a in COMMIT_VALUE_LONG:
            j += 1
        j += 1


def check_compose(toks):
    args = toks[1:]
    proj = None
    for i, a in enumerate(args):
        if a in ("-p", "--project-name") and i + 1 < len(args):
            proj = args[i + 1]
        elif a.startswith(("--project-name=", "-p=")):
            proj = a.split("=", 1)[1]
    readonly = {"version", "config", "--version", "-v", "help", "--help", "-h"}
    if any(a in readonly for a in args) and not any(a in ("up", "down", "rm", "stop", "kill", "start", "restart", "run", "exec", "build", "pull", "create") for a in args):
        return
    if not proj or not is_test_name(proj):
        block("podman-compose must name a test project with -p abt-<name> (got %s)" % (proj or "none"))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cwd = data.get("cwd") or os.getcwd()
    root = os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)

    if SECRETS.search(cmd):
        block("the command touches a credential (token file, .credentials.json, ~/.ssh or an API key)")
    if LIVE_NAMES.search(cmd) or LIVE_PROJECT.search(cmd):
        if re.search(r"\b(podman|podman-compose|docker|kill|rm|systemctl)\b", cmd):
            block("the command names a live stack (media_server, life-updates/deploy, ollama)")
    if re.search(r"(^|[\s;&|(])sudo\b", cmd):
        block("sudo")
    if re.search(r"\bsystemctl\s+--user\s+(stop|disable|mask|restart|kill|edit|enable|daemon-reload|reset-failed)\b", cmd):
        block("changing systemd user units")
    if re.search(r"\bloginctl\b", cmd):
        block("loginctl")
    if re.search(r"\bpodman\b.*\bsystem\s+(reset|prune|migrate|renumber)\b", cmd):
        block("podman system reset/prune/migrate/renumber")

    if re.search(r"\bGIT_PRECOMMIT_CHECK_\w+=", cmd):
        block("turning off the pre-commit checks")
    if re.search(r"\bgit\b", cmd) and re.search(r"core\.hookspath", cmd, re.I):
        block("core.hooksPath (as -c, --config-env, GIT_CONFIG_* or git config) redirects the commit hooks")

    for toks, src in segments(cmd):
        check_command(toks, root, cwd, src)
    return 0


def check_command(toks, root, cwd, src=None, via_xargs=False):
    prog = os.path.basename(toks[0])
    if prog == "rm":
        check_rm(toks, cwd, root)
    elif prog == "xargs":
        inner = _strip_prefix(xargs_command(toks[1:]))
        if inner:
            check_command(inner, root, cwd, src, via_xargs=True)
    elif prog in ("sh", "bash", "dash", "zsh", "ksh"):
        script = shell_c_string(toks)
        if script:
            for inner, inner_src in segments(script):
                check_command(inner, root, cwd, inner_src)
    elif prog == "podman" and "compose" in toks[1:2]:
        check_compose(toks[1:])
    elif prog == "podman":
        check_podman(toks, root, cwd, src, via_xargs)
    elif prog in ("podman-compose", "docker-compose"):
        check_compose(toks)
    elif prog in ("docker",):
        block("use podman, not docker")
    elif prog == "git":
        check_git(toks)


if __name__ == "__main__":
    sys.exit(main())
