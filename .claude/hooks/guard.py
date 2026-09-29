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


def segments(cmd):
    """Split on shell separators; also descend into $(...) and backticks crudely."""
    inner = re.findall(r"\$\(([^()]*)\)|`([^`]*)`", cmd)
    parts = re.split(r"\|\||&&|[;|&\n]", cmd)
    for a, b in inner:
        parts += re.split(r"\|\||&&|[;|&\n]", a or b)
    for p in parts:
        p = p.strip().lstrip("(").rstrip(")")
        if not p:
            continue
        try:
            toks = shlex.split(p, comments=True)
        except ValueError:
            toks = p.split()
        while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in WRAPPERS):
            if toks[0] == "timeout" and len(toks) > 1 and re.match(r"^\d", toks[1]):
                toks = toks[2:]
            else:
                toks = toks[1:]
        if toks:
            yield toks


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


def check_rm(toks, cwd, root):
    flags = [t for t in toks[1:] if t.startswith("-")]
    recursive = any(t in ("--recursive",) or (not t.startswith("--") and re.search(r"[rR]", t)) for t in flags)
    if not recursive:
        return
    for t in toks[1:]:
        if t.startswith("-"):
            continue
        p = expand(t, cwd)
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
    for v in vals:
        if "type=" in v or "src=" in v or "source=" in v:
            m = re.search(r"(?:src|source)=([^,]+)", v)
            src = m.group(1) if m else ""
        else:
            src = v.split(":", 1)[0]
        if not src:
            continue
        if src.startswith(("/", "~", "$HOME", "${HOME}", ".")):
            if not allowed_path(expand(src, cwd), root):
                block("mounting %s: test containers may only mount paths inside the repo or /tmp" % src)
        elif not is_test_name(src):
            block("named volume %s: test volumes must be named abt-* or spike*" % src)


def check_podman(toks, root, cwd):
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
        if any(re.match(r"^--(net|network)(=|$)", a) for a in args):
            joined = " ".join(args)
            if re.search(r"--(?:net|network)[= ]+host\b", joined):
                block("--network host")
            if re.search(r"--(?:net|network)[= ]+(?:container|ns):", joined):
                block("joining another container's or process's network namespace")
        check_mounts(args, root, cwd)
        return
    if (group, verb) in GROUP_DESTRUCTIVE or (group in (None, "container", "image") and verb in DESTRUCTIVE):
        if any(a == "--all" or re.match(r"^-[a-zA-Z]*a[a-zA-Z]*$", a) for a in args):
            block("podman %s --all" % verb)
        filters = [args[i + 1] for i, a in enumerate(args) if a == "--filter" and i + 1 < len(args)]
        filters += [a.split("=", 1)[1] for a in args if a.startswith("--filter=")]
        for f in filters:
            if not any(l in f for l in TEST_LABELS):
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

    for toks in segments(cmd):
        prog = os.path.basename(toks[0])
        if prog == "rm":
            check_rm(toks, cwd, root)
        elif prog == "podman" and "compose" in toks[1:2]:
            check_compose(toks[1:])
        elif prog == "podman":
            check_podman(toks, root, cwd)
        elif prog in ("podman-compose", "docker-compose"):
            check_compose(toks)
        elif prog in ("docker",):
            block("use podman, not docker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
