"""`ab deploy`: start a project's live stack from a reviewed commit (docs/design.md "Preview and deploy"; decisions.md,
17 Nov 2026).

This module is the part that needs no podman: what would be deployed and whether it may be. A deploy is a
COMMIT, never the working tree (D2): the tracked tree must be clean, the commit is copied out with
`git archive` into a staging directory together with the real `.env`, and everything is built and started
from that copy, so nothing changed after the review can reach the live stack. The review is the diff since
the last recorded deploy, with the files that decide what runs (compose files, Containerfiles, lockfiles)
first and in full. The `.env` check compares variable NAMES only; no value is ever read into output.

Every git call goes through `scan.git`: the repo's own config only, and nothing in it that runs a command
(no hooks, fsmonitor, external diff, textconv), since the tree is agent-written.
"""
import datetime
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import tarfile

from . import mounts, scan
from .errors import AgentboxError
from .fsutil import inside, write_private

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
# The files that decide what runs, shown first and in full in the review.
KEY_FILES = (*mounts.COMPOSE_FILES, "compose.*.yml", "compose.*.yaml", "Containerfile*", "Dockerfile*",
             "*.Containerfile", "*.Dockerfile", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
             "pnpm-lock.yaml", "bun.lock", "requirements*.txt", "poetry.lock", "uv.lock", "Pipfile.lock",
             "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", ".env.dev")
RECORDS = "deploys.jsonl"
REVIEW_FULL_LINES = 4000            # a key file's diff past this is cut, and the review says so


def env_names(path):
    """Variable names a dotenv file defines (`KEY=…`, `export KEY=…`). Values are not kept."""
    names = set()
    with open(path, errors="replace") as f:
        for line in f:
            m = ENV_LINE.match(line)
            if m:
                names.add(m.group(1))
    return names


def env_check(root):
    """(missing, extra) variable names: in `.env.dev` but not in the real `.env`, and the other way. No
    `.env.dev` means nothing is required. A missing real `.env` when `.env.dev` names variables is all of
    them missing."""
    dev, real = os.path.join(root, ".env.dev"), os.path.join(root, ".env")
    want = env_names(dev) if os.path.isfile(dev) else set()
    have = env_names(real) if os.path.isfile(real) else set()
    return sorted(want - have), sorted(have - want)


def _git(root, *args):
    p = scan.git(root, *args)
    if p.returncode != 0:
        raise AgentboxError(f"git {' '.join(args[:2])} in {root} failed: {(p.stderr or '').strip()[-300:]}")
    return p.stdout


def resolve_commit(root, ref="HEAD"):
    if not os.path.isdir(os.path.join(root, ".git")) and not os.path.isfile(os.path.join(root, ".git")):
        raise AgentboxError(f"{root} is not a git repository; `ab deploy` deploys a commit")
    if ref.startswith("-"):
        raise AgentboxError(f"--commit {ref!r} is not a commit")
    p = scan.git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = (p.stdout or "").strip()
    if p.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise AgentboxError(f"{ref!r} is not a commit in {root}")
    return sha


def dirty(root):
    """Tracked files with uncommitted changes (staged or not). Untracked files do not matter: they are not in
    the commit, and `.env` is untracked by design."""
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no", "--ignore-submodules=none")
    return sorted({e[3:] for e in out.split("\0") if len(e) > 3})


def records_path(settings, project):
    return os.path.join(settings.project_dir(project), RECORDS)


def records(settings, project):
    path = records_path(settings, project)
    out = []
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def last_deployed(settings, project):
    """The commit of the last deploy that succeeded, or None."""
    ok = [r for r in records(settings, project) if r.get("result") == "ok" and r.get("commit")]
    return ok[-1]["commit"] if ok else None


def record(settings, project, doc):
    os.makedirs(settings.project_dir(project), mode=0o700, exist_ok=True)
    line = json.dumps(dict(doc, time=datetime.datetime.now().isoformat(timespec="seconds")), sort_keys=True)
    fd = os.open(records_path(settings, project), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def _is_key_file(path):
    base = os.path.basename(path)
    return any(fnmatch.fnmatchcase(base, p) for p in KEY_FILES)


def _cut(text, limit=REVIEW_FULL_LINES):
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.rstrip("\n")
    return "\n".join(lines[:limit]) + f"\n… ({len(lines) - limit} more lines; see `git diff` for the rest)"


def review(root, last, commit):
    """The text to review before a deploy: what changed since `last` (None: the first deploy), the files that
    decide what runs in full, then the rest as a list."""
    nodiff = ["--no-ext-diff", "--no-textconv", "--no-color"]
    if last and scan.git(root, "cat-file", "-e", f"{last}^{{commit}}").returncode != 0:
        head = [f"The last deploy ({last[:12]}) is not in this repository any more (history rewritten?); "
                "showing the commit as a first deploy."]
        last = None
    else:
        head = []
    if last == commit:
        return "\n".join(head + [f"No change: {commit[:12]} is what was deployed last."]), []
    if last:
        names = [line.split("\t", 1) for line in _git(root, "diff", "--name-status", "--no-renames",
                                                       f"{last}..{commit}").splitlines() if "\t" in line]
        head.append(f"Changes since the last deploy ({last[:12]} -> {commit[:12]}):")
        key = [p for _s, p in names if _is_key_file(p)]
        parts = head + [f"  {s} {p}" for s, p in names]
        for p in key:
            parts += ["", f"--- {p} (full diff) ---",
                      _cut(_git(root, "diff", *nodiff, f"{last}..{commit}", "--", p))]
        return "\n".join(parts), [p for _s, p in names]
    files = [f for f in _git(root, "ls-tree", "-r", "-z", "--name-only", commit).split("\0") if f]
    key = [p for p in files if _is_key_file(p)]
    parts = head + [f"First deploy of {commit[:12]}: {len(files)} file(s)."]
    for p in key:
        parts += ["", f"--- {p} ---", _cut(_git(root, "show", f"{commit}:{p}"))]
    others = [p for p in files if p not in key]
    parts += ["", "Other files:"] + [f"  {p}" for p in others[:300]]
    if len(others) > 300:
        parts.append(f"  … and {len(others) - 300} more")
    return "\n".join(parts), files


def stage(root, commit, dst, extra_files=(".env",)):
    """The deploy copy: `git archive <commit>` unpacked into `dst` (which must not exist), plus the untracked
    files named in `extra_files` (relative to the root, regular files only, never through a symlink), 0600.
    Returns `dst`."""
    if os.path.lexists(dst):
        raise AgentboxError(f"{dst} already exists")
    p = subprocess.run(["git", "--no-pager", "-C", root, *scan.GIT_SAFE, "archive", "--format=tar", commit],
                       capture_output=True, env=dict(os.environ, **scan.GIT_ENV), timeout=600)
    if p.returncode != 0:
        raise AgentboxError(f"git archive {commit[:12]} failed: {p.stderr.decode(errors='replace')[-300:]}")
    os.makedirs(dst, mode=0o700)
    try:
        with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tar:
            _extract(tar, dst)
        for rel in extra_files:
            if rel.startswith("/") or ".." in rel.split("/"):
                raise AgentboxError(f"deploy file {rel!r} must be a path inside the project")
            src = os.path.join(root, rel)
            if not os.path.lexists(src):
                continue
            real = os.path.realpath(src)
            if os.path.islink(src) or not os.path.isfile(real) or not inside(real, os.path.realpath(root)):
                raise AgentboxError(f"deploy file {src} is not a regular file inside the project")
            with open(real, "rb") as f:
                data = f.read()
            target = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            write_private(target, data, atomic=False)
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise
    return dst


def _extract(tar, dst):
    """Unpack git's archive by hand (tarfile's `filter="data"` arrived in 3.11.4; agentbox runs on 3.11):
    regular files, directories and symlinks only, no absolute name, no `..`, nothing written through a
    symlink. Modes are git's own (0644, or 0755 for an executable; directories 0755) and never setuid: a
    container whose app runs as a non-root user (`USER node` after `COPY .`) must be able to read what it
    was built from. The copy's top directory is 0700, so no other host user gets in. A symlink is kept as a
    link; what it points at is the gate's concern when something binds or builds from it."""
    base = os.path.realpath(dst)
    for m in tar.getmembers():
        name = m.name
        if name.startswith("/") or ".." in name.split("/") or "\0" in name:
            raise AgentboxError(f"the archive holds an unsafe path {name!r}")
        target = os.path.join(base, name)
        parent = os.path.realpath(os.path.dirname(target))
        if not inside(parent, base):
            raise AgentboxError(f"the archive writes {name!r} through a symlink")
        if m.isdir():
            os.makedirs(target, exist_ok=True)
            os.chmod(target, 0o755)
        elif m.issym():
            os.symlink(m.linkname, target)
        elif m.isfile():
            with tar.extractfile(m) as src:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if m.mode & 0o100 else 0o644)
        elif m.type == tarfile.XGLTYPE or m.type == tarfile.XHDTYPE:
            continue
        else:
            raise AgentboxError(f"the archive holds {name!r}, which is not a file, directory or symlink")


def compose_file(tree):
    """The compose file at the root of the deploy copy (the first of mounts.COMPOSE_FILES that exists)."""
    for name in mounts.COMPOSE_FILES:
        path = os.path.join(tree, name)
        if os.path.isfile(path) and not os.path.islink(path):
            return path
    raise AgentboxError(f"no compose file ({', '.join(mounts.COMPOSE_FILES)}) at the root of the commit")


def parse_lan(value, host_addrs):
    """`ADDR:PORT` for --lan: an IPv4 address of this host and a port in 1-65535."""
    m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})", value or "")
    if not m or not all(0 <= int(o) <= 255 for o in m.group(1).split(".")) or not 0 < int(m.group(2)) < 65536:
        raise AgentboxError(f"--lan {value!r}: give ADDR:PORT, an IPv4 address of this host and a port")
    if host_addrs and m.group(1) not in host_addrs:
        raise AgentboxError(f"--lan {value!r}: {m.group(1)} is not an address of this host "
                            f"({', '.join(sorted(host_addrs))})")
    return m.group(1), int(m.group(2))


def check(settings, project, root, *, ref="HEAD"):
    """Everything `ab deploy` decides before it touches podman. Returns a dict: commit, last, dirty, missing,
    extra, review, files. Refusals (no repo, no such commit) raise; the rest the caller reports."""
    commit = resolve_commit(root, ref)
    last = last_deployed(settings, project)
    text, files = review(root, last, commit)
    missing, extra = env_check(root)
    has_compose = any(scan.git(root, "cat-file", "-e", f"{commit}:{n}").returncode == 0
                      for n in mounts.COMPOSE_FILES)
    return {"commit": commit, "last": last, "dirty": dirty(root) if ref == "HEAD" else [],
            "missing": missing, "extra": extra, "review": text, "files": files, "has_compose": has_compose}
