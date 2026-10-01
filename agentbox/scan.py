"""The end-of-session scan: what a session left behind that you will run, or that should not be there.

At session start `snapshot` records, for every git repo in the writable grants, its HEAD, the files git
already sees as changed, its executable files and its config files. At the end `report` lists:

  * secrets in the lines the session's commits added, found with the host's own pre-commit secret regex (read
    from the global hook at run time, so they never drift apart), in grep -E -i, its engine. The hook's
    `scanner:allow` marker is ignored: an agent must not be able to exempt its own lines;
  * changed files that run later on the host: Makefiles, package.json, pyproject.toml/setup.py/setup.cfg,
    .pre-commit-config.yaml, .vscode/tasks.json, .envrc, git config (read-only in the session, so a change
    here means something went wrong), and files that became executable.

It informs and never blocks. Matched secrets are shown only by their first characters.
"""
import os
import re
import stat
import subprocess

RUN_LATER_NAMES = {"Makefile", "makefile", "GNUmakefile", "package.json", "pyproject.toml", "setup.py",
                   "setup.cfg", ".pre-commit-config.yaml", ".envrc"}
RUN_LATER_PATHS = (".vscode/tasks.json",)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}
HOOK_VAR = "SECRETS_REGEX"          # the variable in the host hook that holds its pattern


def git(work, *args, timeout=60):
    return subprocess.run(["git", "--no-pager", "-C", work, "-c", "core.fsmonitor=false", *args],
                          capture_output=True, text=True, timeout=timeout)


def host_secret_regex():
    """(regex, source) from the global pre-commit hook's pattern variable, or (None, why) without one: the
    scan never carries a copy of its own, so it cannot drift from the hook."""
    try:
        hooks = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"], capture_output=True,
                               text=True, timeout=10).stdout.strip()
        path = os.path.join(os.path.expanduser(hooks), "pre-commit") if hooks else ""
        if path:
            with open(path) as f:
                for line in f:
                    name, sep, value = line.strip().partition("=")
                    if name == HOOK_VAR and sep and len(value) >= 2 and value[0] == value[-1] == '"':
                        return value[1:-1].replace('\\"', '"'), path
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None, "no host pre-commit hook with a secret pattern was found"


def worktrees(grants):
    """Work trees of the git repos in the writable grants (repos with a .git directory or file)."""
    out = []
    for g in grants:
        for dirpath, dirnames, filenames in os.walk(g, followlinks=False):
            if ".git" in dirnames or ".git" in filenames:
                out.append(dirpath)
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
    return sorted(out)


def _executables(work):
    found = set()
    for dirpath, dirnames, filenames in os.walk(work, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_mode & 0o111:
                found.add(os.path.relpath(p, work))
    return found


def _git_configs(work):
    gd = git(work, "rev-parse", "--absolute-git-dir").stdout.strip()
    out = {}
    for name in ("config", "config.worktree"):
        p = os.path.join(gd, name) if gd else ""
        if p and os.path.isfile(p):
            with open(p, "rb") as f:
                out[p] = f.read()
    return out


def _dirty(work):
    return set(line[3:] for line in git(work, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
               if len(line) > 3)


def snapshot(grants):
    """The state the report compares against: {work tree: {...}}."""
    state = {}
    for work in worktrees(grants):
        head = git(work, "rev-parse", "--verify", "-q", "HEAD").stdout.strip() or None
        state[work] = {"head": head, "dirty": sorted(_dirty(work)), "exec": sorted(_executables(work)),
                       "configs": {k: v.hex() for k, v in _git_configs(work).items()}}
    return state


def _is_run_later(rel):
    return os.path.basename(rel) in RUN_LATER_NAMES or any(rel == p or rel.endswith("/" + p) for p in RUN_LATER_PATHS)


def _redact(text):
    text = text.strip()
    return (text[:12] + "…") if len(text) > 12 else text


def report(state, grants, regex=None):
    """Returns (summary lines, full report text). Empty summary when nothing was found."""
    lines, summary = [], []
    regex, source = (regex, "given") if regex else host_secret_regex()
    if not regex:
        summary.append(f"secret scan skipped: {source}")
    for work in worktrees(grants):
        before = state.get(work) or {"head": None, "dirty": [], "exec": [], "configs": {}}
        head = git(work, "rev-parse", "--verify", "-q", "HEAD").stdout.strip() or None
        rng = f"{before['head']}..HEAD" if before["head"] else "HEAD"
        commits = git(work, "rev-list", rng).stdout.split() if head else []
        section = [f"## {work}", f"commits added this session: {len(commits)}"]
        # secrets in the added lines of those commits (the marker is ignored on purpose)
        hits = []
        if commits and regex:
            diff_args = ["diff", "--no-color", "-U0", before["head"], "HEAD"] if before["head"] else \
                ["show", "--no-color", "-U0", "--format=", "--root", *commits]
            current = None
            added = []
            for line in git(work, *diff_args, timeout=300).stdout.splitlines():
                if line.startswith("+++ "):
                    current = line[6:] if line.startswith("+++ b/") else line[4:]
                elif line.startswith("+") and current:
                    added.append((current, line[1:]))
            if added:
                p = subprocess.run(["grep", "-n", "-o", "-E", "-i", regex], input="\n".join(t for _, t in added) + "\n",
                                   capture_output=True, text=True)
                for m in p.stdout.splitlines():
                    n, _, match = m.partition(":")
                    if n.isdigit() and 0 < int(n) <= len(added):
                        hits.append((added[int(n) - 1][0], _redact(match)))
        if hits:
            section.append(f"POSSIBLE SECRETS in added lines ({len(hits)}), by the host hook's pattern ({source}):")
            section += [f"  {f}: {m}" for f, m in hits]
            summary.append(f"{work}: {len(hits)} possible secret(s) in the session's commits")
        # files that run later: changed in those commits, or changed in the work tree since the start
        changed = set(git(work, "diff", "--name-only", before["head"], "HEAD").stdout.split()) if before["head"] and head \
            else set(git(work, "ls-files").stdout.split()) if commits else set()
        changed |= _dirty(work) - set(before["dirty"])
        run_later = sorted(f for f in changed if _is_run_later(f))
        new_exec = sorted(_executables(work) - set(before["exec"]))
        configs = {k: v.hex() for k, v in _git_configs(work).items()}
        changed_cfg = sorted(k for k in set(configs) | set(before["configs"])
                             if configs.get(k) != before["configs"].get(k))
        if run_later:
            section.append("changed files that run later on the host (review before running them):")
            section += [f"  {f}" for f in run_later]
            summary.append(f"{work}: changed files that run later: {', '.join(run_later)}")
        if new_exec:
            section.append("files that became executable:")
            section += [f"  {f}" for f in new_exec]
            summary.append(f"{work}: {len(new_exec)} file(s) became executable: {', '.join(new_exec[:5])}")
        if changed_cfg:
            section.append("git config changed (it is read-only in the session):")
            section += [f"  {f}" for f in changed_cfg]
            summary.append(f"{work}: git config changed: {', '.join(changed_cfg)}")
        lines += section + [""]
    return summary, "\n".join(lines)
