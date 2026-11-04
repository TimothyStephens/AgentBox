"""The end-of-session scan: what a session left behind that you will run, or that should not be there.

At session start `snapshot` records (as JSON, kept in the session's run dir so `ab session kill` and `ab session gc` can report
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
    .git/config and hooks before you use git there;
  * where HEAD and every ref went: a branch created, deleted, moved on or rewound, and HEAD on a different
    branch or detached. A session can end with a clean work tree, no new file and no new object and still
    have moved a branch (`git switch`, `git reset --hard`, `git branch -f`), which no file comparison sees.
    A branch moved forward is what work looks like, so it is in the full report only; a ref deleted or
    rewound, and HEAD somewhere else, are in the summary. When the project names a base branch, that
    branch's new commits are listed one by one whatever it did.

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
                   "compose.yml", "compose.yaml", "Containerfile", "Dockerfile", ".gitattributes",
                   ".gitmodules",
                   # What an AGENT reads next session, which is the same kind of "runs later" as a Makefile:
                   # instructions it will follow, and (for Claude) the file its hooks and permissions live in.
                   "CLAUDE.md", "AGENTS.md", ".mcp.json", "mcp.json"}
RUN_LATER_PATHS = (".vscode/tasks.json", ".vscode/launch.json", ".vscode/settings.json")
# A repo's agent directories, whole: a skill, hook, tool, sub-agent or settings file added under one of them
# is read by the next session and by the user's own agent outside agentbox. `lockdown` covers `.claude` and
# `.omp` read-only (omp.REPO_CODE); every other preset leaves them writable, and the others here are not
# covered at all, which is when the report matters most.
RUN_LATER_DIRS = (".github/workflows/", ".github/skills/", ".claude/", ".omp/", ".agents/", ".codex/",
                  ".opencode/")
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
    """Every ref by NAME with the object it points at, and where HEAD is.

    The objects alone are enough to find the commits a session added, and that is all version 2 of the
    snapshot kept. They are not enough to see a ref MOVE: `git reset --hard HEAD~3`, `git branch -f` and a
    deleted branch all land refs on objects that already existed, so nothing is new, the work tree can be
    clean, and a report that compares objects and files has nothing to say. The names are what makes it
    visible."""
    refs = {}
    p = git(work, "for-each-ref", "--format=%(refname)%09%(objectname)")
    for line in p.stdout.splitlines():
        name, tab, oid = line.partition("\t")
        if tab and name and oid:
            refs[name] = oid
    head = git(work, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()
    on = git(work, "symbolic-ref", "-q", "HEAD").stdout.strip()
    return {"refs": refs, "head": head, "head_ref": on}


def snapshot(grants, skip=None):
    """The state the report compares against (JSON-serialisable)."""
    repos = {}
    for work in worktrees(grants, skip):
        meta, _gd = git_meta(work)
        try:
            r = _refs(work)
        except (OSError, subprocess.SubprocessError):
            r = {"refs": {}, "head": "", "head_ref": ""}
        # "refs" keeps the flat object list version 2 wrote: it is what the new-commit search negates with,
        # and a session started on the older code is still reported by this code at its teardown.
        objects = set(r["refs"].values()) | ({r["head"]} if r["head"] else set())
        repos[work] = {"meta": meta, "refs": sorted(objects), "byname": r["refs"],
                       "head": r["head"], "head_ref": r["head_ref"]}
    files = {g: files_state(g, (skip or {}).get(g, ())) for g in grants}
    return {"version": 3, "repos": repos, "files": files}


def _redact(text):
    text = text.strip()
    return (text[:12] + "…") if len(text) > 12 else text


def _grep(texts, regex):
    """[(index into texts, redacted match)] for the lines the pattern matches, found with `grep -E -i` --
    the host hook's own engine, so the scan cannot differ from the hook by a regex dialect."""
    if not texts or not regex:
        return []
    p = subprocess.run(["grep", "-n", "-o", "-E", "-i", regex], input="\n".join(texts) + "\n",
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = []
    for m in p.stdout.splitlines():
        n, _, match = m.partition(":")
        if n.isdigit() and 0 < int(n) <= len(texts):
            out.append((int(n) - 1, _redact(match)))
    return out


def _short(oid):
    return oid[:12] if oid else "(none)"


def _label(full):
    """A ref's full name as it is worth reading: `refs/heads/x` is the branch `x`, a tag says it is one."""
    for prefix, fmt in (("refs/heads/", "{}"), ("refs/tags/", "tag {}"), ("refs/remotes/", "remote {}"),
                        ("refs/stash", "the stash"), ("refs/notes/", "notes {}")):
        if full.startswith(prefix):
            return fmt.format(full[len(prefix):])
    return full


def _full_refname(name, refs):
    """The full ref a configured branch name means, resolved the way git would."""
    if name in refs:
        return name
    for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
        if prefix + name in refs:
            return prefix + name
    return ""


def _distance(work, old, new):
    """(commits `new` has that `old` has not, commits `old` has that `new` has not), or (None, None) when one
    of the objects is no longer in the repo. The second number is the one that matters: it counts what a move
    took OFF the ref."""
    p = git(work, "rev-list", "--count", "--left-right", f"{old}...{new}")
    parts = p.stdout.split()
    if p.returncode != 0 or len(parts) != 2 or not all(x.isdigit() for x in parts):
        return None, None
    return int(parts[1]), int(parts[0])


def _ref_changes(work, before_map, now_map):
    out = []
    for name in sorted(set(before_map) | set(now_map)):
        old, new = before_map.get(name), now_map.get(name)
        if old == new:
            continue
        kind = "created" if old is None else "deleted" if new is None else "moved"
        c = {"kind": kind, "name": name, "old": old or "", "new": new or ""}
        if kind == "moved":
            c["ahead"], c["dropped"] = _distance(work, old, new)
        out.append(c)
    return out


BASE_COMMITS_MAX = 50


def _base_branch_lines(work, base_branch, old_map, now_map, regex):
    """What the session put on the branch the project says the work lands on.

    Elsewhere the report gives a count; for this one branch it names the commits, because they are the
    session's product and the count alone says nothing about what it is."""
    full = _full_refname(base_branch, now_map)
    if not full:
        was = _full_refname(base_branch, old_map)
        if was:
            return ([f"base branch {base_branch}: GONE (was {_short(old_map[was])})"],
                    [f"{work}: base branch {base_branch} no longer exists"])
        return [f"base branch {base_branch}: not in this repo"], []
    new, old = now_map[full], old_map.get(full)
    # A branch that did not exist at the start is new in full: everything it has that no start ref had.
    neg = [f"^{old}"] if old else [f"^{r}" for r in sorted(set(old_map.values()))]
    p = git(work, "log", "--no-color", "--no-show-signature", "--no-textconv",
            f"--max-count={BASE_COMMITS_MAX + 1}", "--format=%h %s", new, *neg, timeout=300)
    if p.returncode != 0:
        return ([f"base branch {base_branch}: its commits could not be listed ({p.stderr.strip()})"],
                [f"{work}: commits on {base_branch} not listed"])
    got = [l for l in p.stdout.splitlines() if l.strip()]
    if not got:
        return [f"base branch {base_branch} ({_short(new)}): no new commits this session"], []
    # The subjects are the agent's own text: held to the same rule as an added line, never printed whole
    # when the host hook's pattern matches them.
    withheld = {i for i, _m in _grep(got, regex)}
    shown = [(l.split(" ", 1)[0] + " <subject withheld: it matches the secret pattern>") if i in withheld else l
             for i, l in enumerate(got[:BASE_COMMITS_MAX])]
    more = len(got) > BASE_COMMITS_MAX
    lines = [f"new commits on {base_branch} ({len(got)}{'+' if more else ''}):"] + [f"  {l}" for l in shown]
    if more:
        lines.append(f"  ... more than {BASE_COMMITS_MAX}")
    return lines, [f"{work}: {len(got)}{'+' if more else ''} new commit(s) on {base_branch}"]


def _movement(work, before, base_branch, regex):
    """(section lines, summary lines): where HEAD and the refs went during the session.

    A session can end with a clean work tree, no new file and no new object, and still have changed what the
    next git command in the repo will do. None of that is a file, so none of it is in the file lists."""
    try:
        now = _refs(work)
    except (OSError, subprocess.SubprocessError) as e:
        return [f"the refs could not be read: {e}"], [f"{work}: refs not read ({e})"]
    old_map = before.get("byname")
    if old_map is None:                 # a version 2 start state: say so rather than call every ref new
        return ["refs not compared: this session's start state was recorded before refs were kept by name"], []
    lines, summary = [], []
    old_head, old_on = before.get("head") or "", before.get("head_ref") or ""
    if now["head_ref"] != old_on:
        was = _label(old_on) if old_on else f"detached at {_short(old_head)}"
        isnow = _label(now["head_ref"]) if now["head_ref"] else f"detached at {_short(now['head'])}"
        lines.append(f"HEAD moved from {was} to {isnow}")
        summary.append(f"{work}: HEAD is now {isnow} (was {was})")
    elif not now["head_ref"] and now["head"] != old_head:
        lines.append(f"HEAD (detached) moved from {_short(old_head)} to {_short(now['head'])}")
        summary.append(f"{work}: detached HEAD moved to {_short(now['head'])}")

    changes = _ref_changes(work, old_map, now["refs"])
    if changes:
        lines.append(f"refs changed ({len(changes)}):")
    for c in changes:
        name = _label(c["name"])
        if c["kind"] == "created":
            lines.append(f"  {name}: new, at {_short(c['new'])}")
        elif c["kind"] == "deleted":
            lines.append(f"  {name}: DELETED (was {_short(c['old'])})")
            summary.append(f"{work}: {name} was deleted (was {_short(c['old'])})")
        elif c["ahead"] is None:
            lines.append(f"  {name}: moved {_short(c['old'])} -> {_short(c['new'])} "
                         "(not compared: an object is no longer in the repo)")
            summary.append(f"{work}: {name} moved to {_short(c['new'])} and its old tip is gone")
        else:
            how = f"+{c['ahead']}" + (f", -{c['dropped']} DROPPED" if c["dropped"] else "")
            lines.append(f"  {name}: moved {_short(c['old'])} -> {_short(c['new'])} ({how})")
            if c["dropped"]:
                summary.append(f"{work}: {name} dropped {c['dropped']} commit(s) "
                               f"(a reset or a forced move), now at {_short(c['new'])}")
    # A branch moved forward is what work looks like, and the commit count above already says it happened,
    # so it is not in the summary. What is: a ref deleted or rewound, HEAD somewhere else, and the base
    # branch, which is named in full whatever it did.
    if base_branch:
        bl, bs = _base_branch_lines(work, base_branch, old_map, now["refs"], regex)
        lines += bl
        summary += bs
    return lines, summary


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
    hits = [(added[i][0], m) for i, m in _grep([t for _, t in added], regex)]
    return len(commits), hits, touched


def report(state, grants, regex=None, skip=None, base_branch=None):
    """Returns (summary lines, full report text). Empty summary when nothing was found.

    `base_branch` is the branch the project says its work lands on: its new commits are named one by one."""
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
        mv, mv_summary = _movement(work, before, base_branch, regex)
        section += mv
        summary += mv_summary
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
