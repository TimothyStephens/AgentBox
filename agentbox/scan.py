"""The end-of-session scan: what a session left behind that you will run, or that should not be there.

At session start `snapshot` records (as JSON, kept in the session's run dir so `ab kill` and `ab gc` can report
too):
  * for every git repo in the writable grants: its git metadata (the .git file, commondir, config and
    config.worktree, by hash) and every ref;
  * for every writable grant: a hash of each file that runs later on the host and of each executable file,
    ignored files included (a gitignored .envrc and .venv/**/*.pth count).

At the end `report` lists:
  * secrets in the lines added by every commit the session made on any ref (branches, tags, notes, stash),
    found with the host's own pre-commit secret regex (read from the global hook at run time, so they never
    drift apart), in grep -E -i, its engine. The hook's `scanner:allow` marker is ignored: an agent must
    not be able to exempt its own lines;
  * files that run later on the host which are new, changed or removed (Makefiles, package.json,
    pyproject.toml/setup.py/setup.cfg, conftest.py, tox/nox, .pre-commit-config.yaml, compose files and
    Containerfiles, .envrc, .vscode tasks/launch/settings, .github/workflows, .venv .pth files), also when
    only touched in a commit on another branch;
  * files that became executable, and executables whose content changed;
  * git repos that are new, or whose metadata changed. git is never run in those (their config is the
    agent's: textconv, filters, a pager or a signature program would run on the host). Check their
    .git/config and hooks before you use git there.

git runs only in repos whose metadata is unchanged since the start, with the system and global config off,
no textconv, external diff, pager, hooks, fsmonitor or signature checks, and no `git status` (clean filters).
It informs and never blocks: every failure becomes a line in the report. Matched secrets are shown only by
their first characters.
"""
import hashlib
import os
import re
import stat
import subprocess

RUN_LATER_NAMES = {"Makefile", "makefile", "GNUmakefile", "package.json", "pyproject.toml", "setup.py",
                   "setup.cfg", ".pre-commit-config.yaml", ".envrc", "conftest.py", "tox.ini", "noxfile.py",
                   "justfile", "Justfile", "Taskfile.yml", "docker-compose.yml", "docker-compose.yaml",
                   "compose.yml", "compose.yaml", "Containerfile", "Dockerfile", ".gitattributes", ".gitmodules"}
RUN_LATER_PATHS = (".vscode/tasks.json", ".vscode/launch.json", ".vscode/settings.json")
RUN_LATER_DIRS = (".github/workflows/",)
SKIP_DIRS = {".git", "node_modules", "__pycache__"}
VENV_DIRS = {".venv", "venv"}           # only their .pth files are looked at
HOOK_VAR = "SECRETS_REGEX"          # the variable in the host hook that holds its pattern
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat"}
GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat",
            "-c", "diff.external=", "-c", "log.showSignature=false", "-c", "core.untrackedCache=false"]


def git(work, *args, timeout=60, input=None):
    """git with the repo's own config as the only one, and nothing in it that runs a command."""
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(["git", "--no-pager", "-C", work, *GIT_SAFE, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env, input=input)


def host_secret_regex():
    """(regex, source) from the global pre-commit hook's pattern variable, or (None, why) without one: the
    scan never carries a copy of its own, so it cannot drift from the hook."""
    try:
        hooks = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"], capture_output=True,
                               text=True, timeout=10).stdout.strip()
        path = os.path.join(os.path.expanduser(hooks), "pre-commit") if hooks else ""
        if path:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    name, sep, value = line.strip().partition("=")
                    if name == HOOK_VAR and sep and len(value) >= 2 and value[0] == value[-1] == '"':
                        return value[1:-1].replace('\\"', '"'), path
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None, "no host pre-commit hook with a secret pattern was found"


def _skipped(rel, skip):
    return any(rel == s or rel.startswith(s.rstrip("/") + "/") for s in skip)


def worktrees(grants, skip=None):
    """Work trees of the git repos in the writable grants (repos with a .git directory, file or link)."""
    out = []
    for g in grants:
        sk = (skip or {}).get(g, ())
        for dirpath, dirnames, filenames in os.walk(g, followlinks=False):
            if ".git" in dirnames or ".git" in filenames:
                out.append(dirpath)
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and d not in VENV_DIRS
                           and not os.path.islink(os.path.join(dirpath, d))
                           and not _skipped(os.path.relpath(os.path.join(dirpath, d), g), sk)]
    return sorted(out)


def _hash_file(path):
    """'link:<target>' for a symlink, else sha256 of the content plus the mode bits."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return "link:" + os.readlink(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return f"{st.st_mode & 0o777:o}:{h.hexdigest()}"


def is_run_later(rel):
    base = os.path.basename(rel)
    return (base in RUN_LATER_NAMES or any(rel == p or rel.endswith("/" + p) for p in RUN_LATER_PATHS)
            or any(rel.startswith(d) or ("/" + d) in rel for d in RUN_LATER_DIRS)
            or (rel.endswith(".pth") and any(f"{v}/" in rel for v in VENV_DIRS)))


def files_state(grant, skip=()):
    """{rel: [run_later, executable, hash]} for the files that matter, ignored files included."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(grant, followlinks=False):
        rel_dir = os.path.relpath(dirpath, grant)
        in_venv = any(part in VENV_DIRS for part in rel_dir.split(os.sep))
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))
                       and not _skipped(os.path.normpath(os.path.join(rel_dir, d)), skip)]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            if _skipped(rel, skip):
                continue
            try:
                st = os.lstat(p)
            except OSError:
                continue
            run_later = is_run_later(rel)
            executable = stat.S_ISREG(st.st_mode) and bool(st.st_mode & 0o111) and not in_venv
            if run_later or executable:
                try:
                    out[rel] = [run_later, executable, _hash_file(p)]
                except OSError as e:
                    out[rel] = [run_later, executable, f"unreadable: {e.strerror}"]
    return out


def git_meta(work):
    """Hashes of what decides how git behaves in this repo, read without running git: the .git file or link,
    commondir, config and config.worktree. Returns (meta, git dir or None)."""
    dotgit = os.path.join(work, ".git")
    meta = {}

    def h(path):
        try:
            return _hash_file(path)
        except FileNotFoundError:
            return None
        except OSError as e:
            return f"unreadable: {e.strerror}"
    if os.path.islink(dotgit):
        meta[".git"] = "link:" + os.readlink(dotgit)
        return meta, None
    gd = dotgit
    if os.path.isfile(dotgit):
        meta[".git"] = h(dotgit)
        try:
            with open(dotgit, encoding="utf-8", errors="replace") as f:
                line = f.read().strip()
        except OSError:
            return meta, None
        if not line.startswith("gitdir:"):
            return meta, None
        gd = os.path.normpath(os.path.join(work, line[len("gitdir:"):].strip()))
    common = gd
    cd = os.path.join(gd, "commondir")
    meta["commondir"] = h(cd)
    if meta["commondir"]:
        try:
            with open(cd, encoding="utf-8", errors="replace") as f:
                common = os.path.normpath(os.path.join(gd, f.read().strip()))
        except OSError:
            pass
    meta["config"] = h(os.path.join(common, "config"))
    meta["config.worktree"] = h(os.path.join(gd, "config.worktree"))
    return meta, gd


def _refs(work):
    p = git(work, "for-each-ref", "--format=%(objectname)")
    head = git(work, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()
    return sorted(set(p.stdout.split()) | ({head} if head else set()))


def snapshot(grants, skip=None):
    """The state the report compares against (JSON-serialisable)."""
    repos = {}
    for work in worktrees(grants, skip):
        meta, _gd = git_meta(work)
        try:
            refs = _refs(work)
        except (OSError, subprocess.SubprocessError):
            refs = []
        repos[work] = {"meta": meta, "refs": refs}
    files = {g: files_state(g, (skip or {}).get(g, ())) for g in grants}
    return {"version": 2, "repos": repos, "files": files}


def _redact(text):
    text = text.strip()
    return (text[:12] + "…") if len(text) > 12 else text


def _new_commits(work, start_refs, regex):
    """(commit count, [(path, redacted match)], {paths touched}) for commits reachable from any ref now and
    from none at the start."""
    neg = "".join(f"^{r}\n" for r in start_refs)
    commits = git(work, "rev-list", "--all", "--stdin", input=neg, timeout=300).stdout.split()
    if not commits:
        return 0, [], set()
    log = git(work, "log", "-p", "-U0", "--no-color", "--no-textconv", "--no-ext-diff", "--no-show-signature",
              "--diff-merges=first-parent", "--format=%x00%H", "--all", "--stdin", input=neg, timeout=600).stdout
    current, added, touched = None, [], set()
    for line in log.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            name = line[4:]
            if name.startswith(("a/", "b/")):
                name = name[2:]
            if name != "/dev/null":
                touched.add(name)
            if line.startswith("+++ "):
                current = name if name != "/dev/null" else None
        elif line.startswith("+") and current:
            added.append((current, line[1:]))
    hits = []
    if added and regex:
        p = subprocess.run(["grep", "-n", "-o", "-E", "-i", regex], input="\n".join(t for _, t in added) + "\n",
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        for m in p.stdout.splitlines():
            n, _, match = m.partition(":")
            if n.isdigit() and 0 < int(n) <= len(added):
                hits.append((added[int(n) - 1][0], _redact(match)))
    return len(commits), hits, touched


def report(state, grants, regex=None, skip=None):
    """Returns (summary lines, full report text). Empty summary when nothing was found."""
    lines, summary = [], []
    regex, source = (regex, "given") if regex else host_secret_regex()
    if not regex:
        summary.append(f"secret scan skipped: {source}")
    start_repos = (state or {}).get("repos") or {}
    for work in worktrees(grants, skip):
        section = [f"## {work}"]
        meta, _gd = git_meta(work)
        before = start_repos.get(work)
        if before is None:
            section.append("NEW GIT REPO made during the session: git was not run here. Check .git/config and "
                           ".git/hooks before you run git in it.")
            summary.append(f"{work}: NEW GIT REPO (check its .git/config and hooks before using git there)")
            lines += section + [""]
            continue
        changed = sorted(k for k in set(meta) | set(before["meta"]) if meta.get(k) != before["meta"].get(k))
        if changed:
            section.append(f"git config changed ({', '.join(changed)}); it is read-only in the session, so "
                           "something went wrong. git was not run here.")
            summary.append(f"{work}: git config changed: {', '.join(changed)} (git not run there)")
            lines += section + [""]
            continue
        try:
            n, hits, touched = _new_commits(work, before["refs"], regex)
        except (OSError, subprocess.SubprocessError) as e:
            n, hits, touched = 0, [], set()
            section.append(f"could not read the commits: {e}")
            summary.append(f"{work}: commits not scanned ({e})")
        section.append(f"commits added this session: {n}")
        if hits:
            section.append(f"POSSIBLE SECRETS in added lines ({len(hits)}), by the host hook's pattern ({source}):")
            section += [f"  {f}: {m}" for f, m in hits]
            summary.append(f"{work}: {len(hits)} possible secret(s) in the session's commits")
        committed = sorted(f for f in touched if is_run_later(f))
        if committed:
            section.append("files that run later, changed in the session's commits (any branch):")
            section += [f"  {f}" for f in committed]
            summary.append(f"{work}: files that run later changed in commits: {', '.join(committed)}")
        lines += section + [""]
    start_files = (state or {}).get("files") or {}
    for g in grants:
        before = start_files.get(g) or {}
        now = files_state(g, (skip or {}).get(g, ()))
        section = [f"## files in {g}"]
        run_later = sorted(r for r in set(now) | set(before)
                           if (now.get(r) or before.get(r))[0] and (now.get(r) or [0, 0, None])[2]
                           != (before.get(r) or [0, 0, None])[2])
        new_exec = sorted(r for r in now if now[r][1] and not (before.get(r) or [0, False])[1])
        changed_exec = sorted(r for r in now if now[r][1] and (before.get(r) or [0, False])[1]
                              and now[r][2] != before[r][2] and r not in run_later)
        if run_later:
            section.append("files that run later on the host, new, changed or removed (review before running them):")
            section += [f"  {f}" + ("" if f in now else " (removed)") for f in run_later]
            summary.append(f"{g}: changed files that run later: {', '.join(run_later[:8])}")
        if new_exec:
            section.append("files that became executable:")
            section += [f"  {f}" for f in new_exec]
            summary.append(f"{g}: {len(new_exec)} file(s) became executable: {', '.join(new_exec[:5])}")
        if changed_exec:
            section.append("executable files whose content changed:")
            section += [f"  {f}" for f in changed_exec]
            summary.append(f"{g}: {len(changed_exec)} executable(s) changed: {', '.join(changed_exec[:5])}")
        lines += section + [""]
    return summary, "\n".join(lines)


# ---- the project's agent state (memory, instructions, settings), kept across sessions (Tim, 2 Oct) ----------
STATE_NAMES = {"CLAUDE.md", "AGENTS.md", "MEMORY.md", "settings.json", "settings.local.json", "config.yml",
               "config.yaml", "models.yml", "agentbox-lockdown.yml"}
STATE_SKIP_DIRS = {"shell-snapshots", "file-history", "sessions", "session-env", "logs", "backups", "plugins",
                   "cache", "statsig", "todos", "debug", "ide"}
CREDENTIAL_HINTS = ("credential", "token", "secret", "auth", "apikey", "api_key", ".key", ".pem", "oauth")
STATE_TEXT_MAX = 64 << 10
DIFF_LINES = 300


def _state_watched(rel):
    base = os.path.basename(rel)
    return (base in STATE_NAMES or "/memory/" in "/" + rel or (base.startswith("settings") and base.endswith(".json"))
            or (rel.startswith("agent/") and base.endswith((".md", ".yml", ".yaml"))))


def _credential_like(rel):
    return any(h in rel.lower() for h in CREDENTIAL_HINTS)


def state_snapshot(state_dir):
    """{rel: {"hash", "text"?}} for the agent state's memory, instruction and settings files. Credential-like
    files are recorded by name and hash only; transcripts and caches are not looked at."""
    out = {}
    if not state_dir or not os.path.isdir(state_dir):
        return out
    for dirpath, dirnames, filenames in os.walk(state_dir, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in STATE_SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, state_dir)
            if not _state_watched(rel):
                continue
            try:
                entry = {"hash": _hash_file(p)}
                if not _credential_like(rel) and not os.path.islink(p) and os.path.getsize(p) <= STATE_TEXT_MAX:
                    with open(p, "rb") as f:
                        entry["text"] = f.read().decode("utf-8", errors="replace")
            except OSError as e:
                entry = {"hash": f"unreadable: {e.strerror}"}
            out[rel] = entry
    return out


def state_report(before, state_dir):
    """(summary lines, report lines): memory/instruction/settings files of the agent state that are new,
    changed or removed since the start, with a unified diff of each (credential-like files: names only)."""
    import difflib
    now = state_snapshot(state_dir)
    changed = sorted(r for r in set(before) | set(now) if (before.get(r) or {}).get("hash") != (now.get(r) or {}).get("hash"))
    if not changed:
        return [], []
    lines = [f"## agent state in {state_dir} (kept for this project across sessions; `ab project reset-state` "
             "wipes it)", "memory, instruction and settings files changed this session:"]
    for r in changed:
        old, new = before.get(r), now.get(r)
        what = "new" if old is None else "removed" if new is None else "changed"
        lines.append(f"  {r} ({what})")
        if _credential_like(r):
            continue
        a = ((old or {}).get("text") or "").splitlines()
        b = ((new or {}).get("text") or "").splitlines()
        diff = list(difflib.unified_diff(a, b, f"a/{r}", f"b/{r}", lineterm="", n=2))
        lines += [f"    {l}" for l in diff[:DIFF_LINES]]
        if len(diff) > DIFF_LINES:
            lines.append(f"    ... {len(diff) - DIFF_LINES} more diff lines")
    names = ", ".join(changed[:6]) + (f" and {len(changed) - 6} more" if len(changed) > 6 else "")
    return [f"agent memory/instructions/settings changed: {names} (diff in the full report)"], lines + [""]
