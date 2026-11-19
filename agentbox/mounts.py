"""The filesystem plan for a session: grants, read-only overlays, secret masks and the dev swap.

Everything mounts at its real host path, so paths inside and outside the sandbox agree.
Masks are computed once, at session start.
"""
import errno
import fnmatch
import os
import re
import stat
import subprocess
from dataclasses import dataclass

from .errors import AgentboxError
from .fsutil import inside, write_private

# The one list of secret file NAMES: the masks hide them, grantcheck (SECRET_FILES) and the history check look
# for them, and a swap target must be one. Widened 16 Nov (review item 28) from `.env`/`.env.*`/`*.pem`/`*.key`:
# `*.env` (prod.env), `.netrc`/`.pgpass` (plaintext passwords by convention), `*.p12`/`*.pfx`/`*.jks` (key stores).
# Exact names only in MASK_EXEMPT: a committed example under another name (`example.env`) is masked.
MASK_FILES = (".env", ".env.*", "*.env", ".netrc", ".pgpass", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks")
MASK_EXEMPT = (".env.example", ".env.sample", ".env.template", ".env.dev")
# MASK_FILES as one compiled pattern: `_masked` runs once per file of every grant, several times per session
# start, and a fnmatch call per pattern was about 1-1.4 s of a 20 000-file grant (tests/perf, 16 Nov).
_MASK_RE = re.compile("|".join(f"(?:{fnmatch.translate(p)})" for p in MASK_FILES))
MASK_DIRS = ("secrets",)
WALK_SKIP = (".git", "node_modules", ".venv", "venv", "__pycache__")
COMPOSE_FILES = ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml",
                 "podman-compose.yml", "podman-compose.yaml")
DEFAULT_SWAP = {".env": ".env.dev"}


@dataclass(frozen=True)
class Mount:
    kind: str          # bind | mask | tmpfs
    dst: str
    src: str = ""
    ro: bool = False

    def podman_arg(self, mask_file=None):
        if self.kind == "bind":
            return f"--mount=type=bind,src={self.src},dst={self.dst}" + (",ro" if self.ro else "")
        if self.kind == "mask":
            # An empty regular file, read-only: it reads empty and cannot be written. Not /dev/null: a
            # character device in the tree breaks a rootless `podman build` of that directory (found in step 5).
            if not mask_file:
                raise ValueError("a mask needs the session's empty mask file")
            return f"--mount=type=bind,src={mask_file},dst={self.dst},ro"
        if self.kind == "tmpfs":
            return f"--mount=type=tmpfs,dst={self.dst},tmpfs-mode=0700,U=true,notmpcopyup" + (",ro" if self.ro else "")
        raise ValueError(self.kind)


@dataclass
class Plan:
    mounts: list
    create_dirs: list      # host dirs to create before start (mount points)
    create_files: list     # host files to create (mode 0600) before start
    notes: list


def effective_swap(stored=None):
    """The dev-swap map a session uses: the project's own entries ON TOP OF the default `.env` -> `.env.dev`.

    A stored swap used to REPLACE the default (audit F-12), so adding one `.env.local` mapping silently
    stopped `.env.dev` standing in for `.env` -- the mapping every `ab project init` project is built around. A
    stored entry for the same target still wins. Idempotent, so it is safe at every boundary that reads a
    swap (load_project, grantcheck.preflight, the history scan, and the plan itself)."""
    return {**DEFAULT_SWAP, **(stored or {})}


def _masked(name):
    """Would the secret masks hide a file with this name?

    There used to be a `name in swap_targets` arm here, so a swap target was masked whatever it was called.
    Since F6 every target must itself be a masked name, so the pattern test below already answers True --
    the arm could not change an answer (deadness pinned by test_projectkeys.SwapTargetsAreAlwaysMaskedNames)."""
    if name in MASK_EXEMPT:
        return False
    return _MASK_RE.match(name) is not None


def _walk(grant, skip_rel):
    skip_abs = {os.path.normpath(os.path.join(grant, s)) for s in skip_rel}
    for dirpath, dirnames, filenames in os.walk(grant, topdown=True, followlinks=False):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d in WALK_SKIP or full in skip_abs or os.path.islink(full):
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        yield dirpath, dirnames, sorted(filenames)


def secret_mounts(grant, *, ro, skip_rel=(), swap=None, notes=None):
    """Masks and dev-swap mounts for one grant. Returns (mounts, files_to_create).

    A missing swap target is created empty (0600) as the mount point -- documented since 29 Sep -- but only in
    a read-write grant: a read-only grant is not ours to write into, so there the swap is skipped and noted
    (nothing is hidden by skipping it: there is no real file). A target that exists as something other than
    a regular file or symlink (a `.env/` directory, a FIFO) cannot take a file bind; it is skipped and noted
    instead of failing `podman run` (item 37)."""
    swap = effective_swap(swap)
    notes = notes if notes is not None else []
    mounts, create = [], []
    for dirpath, dirnames, filenames in _walk(grant, skip_rel):
        for d in list(dirnames):
            if d in MASK_DIRS:
                mounts.append(Mount("tmpfs", os.path.join(dirpath, d)))
                dirnames.remove(d)
        present = set(filenames)
        handled = set()
        for target, dev in swap.items():
            if dev in present and not os.path.islink(os.path.join(dirpath, dev)):
                dst = os.path.join(dirpath, target)
                if os.path.lexists(dst) and not (os.path.islink(dst) or os.path.isfile(dst)):
                    notes.append(f"{dst} is not a regular file, so {dev} is not shown in its place")
                    continue
                if not os.path.lexists(dst):
                    if ro:
                        notes.append(f"{dst} does not exist and {grant} is read-only, so {dev} is not shown "
                                     f"at {target} there (nothing is created in a read-only grant)")
                        continue
                    create.append(dst)
                mounts.append(Mount("bind", dst, os.path.join(dirpath, dev), ro=ro))
                handled.add(target)
        for fn in filenames:
            # The name test first: it is a regex, the two file-type tests are a stat each, and nearly every
            # file in a tree is not a mask candidate (two stats per file were 45% of a session's mask plan on
            # a 100k-file Rust tree, 17 Nov).
            if fn in handled or not _masked(fn):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            mounts.append(Mount("mask", full))
    return mounts, create


def mask_is_path(entry):
    """A `mask` entry is a root-relative PATH when it starts with `./` or contains a '/'; otherwise a NAME."""
    return entry.startswith("./") or "/" in entry


def user_masks(grant, *, root, mask, skip_rel=(), notes=None):
    """The project's own masks (the `mask` setting) in one grant. Returns (mounts, dirs_to_create).

    A NAME covers every file or directory of that name the walk finds -- the secret masks' own walk, so
    `mask_skip` folders are not searched and symlinks are not followed. A PATH covers exactly that path under
    the ROOT, when it lies in this grant. A directory becomes an empty read-only tmpfs, anything else the
    empty mask file. A path that does not exist yet is created as a directory and covered, so a mask added
    ahead of the data does not silently do nothing (docs/agentbox-masked-paths.md §4). A grant root itself
    is never masked: that would hide the whole grant."""
    notes = notes if notes is not None else []
    names = {m for m in mask if not mask_is_path(m)}
    out, create = [], []
    for rel in (m for m in mask if mask_is_path(m)):
        full = os.path.normpath(os.path.join(root, rel))
        if not inside(full, grant) or full == grant:
            continue
        if os.path.islink(full):
            notes.append(f"{full} is a symlink; the mask covers this path, not what it points to")
            out.append(Mount("mask", full))
        elif os.path.isdir(full):
            out.append(Mount("tmpfs", full, ro=True))
        elif os.path.lexists(full):
            out.append(Mount("mask", full))
        else:
            create.append(full)
            out.append(Mount("tmpfs", full, ro=True))
            notes.append(f"{full} does not exist yet; created empty and masked")
    if names:
        for dirpath, dirnames, filenames in _walk(grant, skip_rel):
            for d in list(dirnames):
                if d in names:
                    out.append(Mount("tmpfs", os.path.join(dirpath, d), ro=True))
                    dirnames.remove(d)
            for fn in filenames:
                if fn in names:
                    out.append(Mount("mask", os.path.join(dirpath, fn)))
    return out, create


def _is_git_dir(path):
    """A git directory (a repo's .git, a separate git dir, or a bare repo): HEAD, config, objects/, refs/."""
    return all(os.path.isfile(os.path.join(path, f)) for f in ("HEAD", "config")) and \
        all(os.path.isdir(os.path.join(path, d)) for d in ("objects", "refs"))


def _gitfile_target(path):
    """The git dir a `.git` file points to (`gitdir: <path>`), resolved, or None when it is not a gitdir
    pointer. Refuses when the file cannot be READ: that would leave the repository it names uncovered, so
    its hooks and config would be writable in the session and nothing would say so (audit F-10's class)."""
    try:
        with open(path) as f:
            line = f.readline().strip()
    except OSError as e:
        raise AgentboxError(f"{path}: cannot read this .git file ({e}), so the git directory it names "
                            "cannot be covered read-only; fix its permissions or move it out of the grant")
    if not line.startswith("gitdir:"):
        return None
    target = line[len("gitdir:"):].strip()
    return os.path.realpath(os.path.join(os.path.dirname(path), target))


def find_git_dirs(grant, skip_rel=()):
    """Every git directory reachable from the grant: .git dirs of nested repos, the targets of .git files
    (worktrees, submodule checkouts, --separate-git-dir), bare repos, and submodule git dirs under modules/,
    plus each one's commondir. Returns (set of real paths, [(checkout, target outside this grant)])."""
    found, outside = set(), []
    skip_abs = {os.path.normpath(os.path.join(grant, s)) for s in skip_rel}

    def add(g):
        g = os.path.realpath(g)
        if g in found or not _is_git_dir(g):
            return
        found.add(g)
        common = os.path.join(g, "commondir")
        if os.path.isfile(common):
            with open(common) as f:
                add(os.path.join(g, f.read().strip()))
        modules = os.path.join(g, "modules")
        if os.path.isdir(modules):
            for dirpath, _dirnames, _filenames in os.walk(modules, followlinks=False):
                if dirpath != modules and _is_git_dir(dirpath):
                    add(dirpath)

    for dirpath, dirnames, filenames in os.walk(grant, topdown=True, followlinks=False):
        dot = os.path.join(dirpath, ".git")
        if ".git" in dirnames and not os.path.islink(dot):
            add(dot)
        elif ".git" in filenames and not os.path.islink(dot):
            target = _gitfile_target(dot)
            if target:
                add(target)
                if not inside(target, grant):
                    outside.append((dirpath, target))
        if dirpath != grant and _is_git_dir(dirpath):
            add(dirpath)                                    # a bare repo or separate git dir
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d in WALK_SKIP or full in skip_abs or os.path.islink(full):
                continue
            keep.append(d)
        dirnames[:] = keep
    return found, outside


CONFIG_PATH_KEYS = r"^(core\.hookspath|include\.path|includeif\..*\.path)$"
CONFIG_DEPTH = 8                 # include chains deeper than this are not something a repo does by accident


def git_config_paths(cfg, work):
    """[(key, path)] for every `core.hooksPath`, `include.path` and `includeIf.<cond>.path` a git config file
    sets, each resolved the way git resolves it: `~/` expanded; a relative hooksPath against the WORK tree
    (where hooks run), a relative include against the config file's own directory. Nothing is followed:
    `--file` turns includes off, so this is one file's own entries.

    git parses its own format (quoting, continuation lines, subsections); a hand parser would disagree with
    it exactly where an agent would want it to. Raises when the file cannot be read or parsed (git exits 128):
    a config the launcher cannot read is one whose hooks path it cannot cover (audit F-10's class)."""
    fix = "make it readable or move the repo out of the grant"
    try:
        with open(cfg, "rb"):
            pass                         # git exits 1 on a file it cannot open, the same as "no such key"
    except OSError as e:
        raise AgentboxError(f"{cfg}: cannot read this git config ({e.strerror}), so a hooks path or include it "
                            f"names cannot be checked or covered read-only; {fix}")
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(["git", "config", "--file", cfg, "-z", "--get-regexp", CONFIG_PATH_KEYS],
                           capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AgentboxError(f"{cfg}: cannot read this git config ({e}), so a hooks path or include it names "
                            f"cannot be checked or covered read-only; {fix}")
    if p.returncode == 1:
        return []
    if p.returncode != 0:
        raise AgentboxError(f"{cfg}: cannot read this git config ({p.stderr.strip()[-200:]}), so a hooks path or "
                            "include it names cannot be checked or covered read-only; fix it or move the repo out of "
                            "the grant")
    out = []
    for entry in p.stdout.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        value = os.path.expanduser(value)
        base = work if key == "core.hookspath" else os.path.dirname(cfg)
        out.append((key, os.path.normpath(os.path.join(base, value))))
    return out


def _git_work_tree(g):
    """Where a git dir's hooks run: the checkout for a `.git` directory, the git dir itself for a bare repo."""
    return os.path.dirname(g) if os.path.basename(g) == ".git" else g


def git_config_targets(g):
    """Every path the covered configs of one git dir point at, following includes that lie inside the git
    dir's own tree of config files: [(config file, key, target)]. Raises when a config cannot be read."""
    work = _git_work_tree(g)
    todo = [os.path.join(g, n) for n in ("config", "config.worktree")]
    seen, out = set(), []
    while todo and len(seen) < CONFIG_DEPTH * 2:
        cfg = todo.pop(0)
        if cfg in seen or not os.path.isfile(cfg) or os.path.islink(cfg):
            continue
        seen.add(cfg)
        for key, target in git_config_paths(cfg, work):
            out.append((cfg, key, target))
            if key != "core.hookspath" and os.path.isfile(target) and not os.path.islink(target):
                todo.append(target)          # an included file may itself set hooksPath or include more
    return out


def git_overlays(git_dirs, grants, ro_git_dirs=()):
    """Read-only hooks/, config and config.worktree for every git dir that sits inside a writable grant,
    plus whatever those configs point at inside a grant: a `core.hooksPath` (husky's `.husky`, a repo's
    `.githooks`) is where the OWNER's git runs hooks from, and an `include.path` is config the owner's git
    reads, so a writable one would let a session plant a hook the read-only `.git/hooks` was meant to stop
    (review 16 Nov, item 15). A missing target is created empty and covered: it would otherwise be the
    agent's to create. A target outside every grant, or a symlink, is not covered here; `grantcheck` reports
    it, so the session refuses until it is accepted or the config is changed. A git dir outside every grant
    is not mounted, so the agent cannot reach it at all.

    `ro_git_dirs` are git dirs in READ-ONLY grants: they get no covers of their own (nothing there is
    writable), but their configs are read the same way, and a target they name inside a writable grant is
    covered (read-only root proposal, section 3). A config there that cannot be read refuses the plan, as it
    does in a writable grant: the launcher cannot tell whether it names a path in a writable grant (decision
    19 Nov 2026). With no writable grant at all there is nothing it could name, so it is not read."""
    mounts, create_dirs, create_files = [], [], []
    todo = [(g, False) for g in sorted(git_dirs)] + [(g, True) for g in sorted(ro_git_dirs)]
    for g, in_ro in todo:
        if in_ro:
            if not grants or any(inside(g, gr) for gr in grants):
                continue                     # nothing writable to name, or covered as one of the writable ones
            targets = git_config_targets(g)  # raises on an unreadable config: refused, as in a writable grant
        else:
            if not any(inside(g, gr) for gr in grants):
                continue
            hooks = os.path.join(g, "hooks")
            if not os.path.isdir(hooks):
                create_dirs.append(hooks)
            mounts.append(Mount("bind", hooks, hooks, ro=True))
            for name in ("config", "config.worktree"):
                cfg = os.path.join(g, name)
                if os.path.isfile(cfg) and not os.path.islink(cfg):
                    mounts.append(Mount("bind", cfg, cfg, ro=True))
            targets = git_config_targets(g)
        for _cfg, key, target in targets:
            if not any(inside(target, gr) for gr in grants) or target in grants or os.path.islink(target):
                continue                     # grantcheck's to report, not ours to cover
            if not os.path.lexists(target):
                (create_dirs if key == "core.hookspath" else create_files).append(target)
            mounts.append(Mount("bind", target, target, ro=True))
    return mounts, create_dirs, create_files


def compose_overlays(grant, skip_rel=()):
    out = []
    for dirpath, _dirnames, filenames in _walk(grant, skip_rel):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if fn in COMPOSE_FILES and os.path.isfile(full) and not os.path.islink(full):
                out.append(Mount("bind", full, full, ro=True))
    return out


def plan(*, root, data_ro=(), data_rw=(), compose="rw", mask_skip=(), swap=None, mask=(), code="rw"):
    """Build the mount plan. All paths must already be validated real paths; `mask` is the project's own
    mask list, as projectkeys stores it. `code = "ro"` mounts the root read-only: it is then handled exactly
    like a data_ro grant (no git or compose covers in it, the swap read-only, masks over it)."""
    mounts, dirs, files, notes = [], [], [], []
    ro_root = code == "ro"
    rw_grants = ([] if ro_root else [root]) + list(data_rw)
    data_ro = ([root] if ro_root else []) + list(data_ro)
    if ro_root:
        mounts.append(Mount("bind", root, root, ro=True))      # first: the root, then what lies elsewhere
    for g in rw_grants:
        mounts.append(Mount("bind", g, g))
    for g in data_ro[1 if ro_root else 0:]:
        mounts.append(Mount("bind", g, g, ro=True))
    git_dirs = set()
    for g in rw_grants:
        found, outside = find_git_dirs(g, _rel_skips(root, g, mask_skip))
        git_dirs |= found
        # a .git file in one grant may point into another grant: protected there; otherwise not mounted
        for checkout, target in outside:
            if not any(inside(target, gr) for gr in rw_grants):
                notes.append(f"{checkout}: .git points to {target}, which is not mounted (git will not work "
                             "there, and its hooks are out of the agent's reach)")
    ro_git_dirs = set()
    for g in data_ro:                            # their configs may name a path in a writable grant
        try:
            ro_git_dirs |= find_git_dirs(g, _rel_skips(root, g, mask_skip))[0]
        except AgentboxError as e:               # read-only: it did not refuse the plan before either
            notes.append(f"{e}; a hooks path its repository names in a writable grant is not covered")
    m, d, f = git_overlays(git_dirs, rw_grants, ro_git_dirs)
    mounts += m
    dirs += d
    files += f
    if compose == "ro" and not ro_root:          # a read-only cover in a read-only root adds nothing
        mounts += compose_overlays(root, _rel_skips(root, root, mask_skip))
    for g in rw_grants + list(data_ro):          # secret masks always apply; there is no way to turn them off
        m, c = secret_mounts(g, ro=g in data_ro, skip_rel=_rel_skips(root, g, mask_skip), swap=swap, notes=notes)
        mounts += m
        files += c
    # The project's own masks come AFTER the secret masks and the swap: one mount per destination and the
    # later entry wins, so a masked path that is also a swap target ends up masked, never substituted.
    for g in rw_grants + list(data_ro):
        m, d = user_masks(g, root=root, mask=mask, skip_rel=_rel_skips(root, g, mask_skip), notes=notes)
        mounts += m
        dirs += d

    mounts += hardlink_masks(mounts, rw_grants + list(data_ro), root, mask_skip, notes)
    # One mount per destination; later (more specific) entries win.
    by_dst = {}
    for m in mounts:
        by_dst[m.dst] = m
    # Nothing is mounted inside a directory mask (audit 6 Oct, N2): a git dir or compose file under
    # secrets/ would otherwise get a real bind on top of the empty cover, showing the real file.
    covers = [d for d, m in by_dst.items() if m.kind == "tmpfs"]
    kept = [m for d, m in by_dst.items() if not any(inside(d, c) and d != c for c in covers)]
    dirs = [d for d in dirs if not any(inside(d, c) and d != c for c in covers)]
    files = [f for f in files if not any(inside(f, c) for c in covers)]
    return Plan(kept, dirs, files, notes)


def remap(p, src_root, dst_root):
    """The same plan with every destination under `src_root` moved under `dst_root`: clone mode walks the
    clone (sources stay host paths in the clone) but the agent sees the owner's path (destinations). Host
    paths to create (`create_dirs`, `create_files`) stay where they are: they are made in the clone."""
    def move(dst):
        return dst_root + dst[len(src_root):] if inside(dst, src_root) else dst
    mounts = [Mount(m.kind, move(m.dst), m.src, m.ro) for m in p.mounts]
    return Plan(mounts, list(p.create_dirs), list(p.create_files), list(p.notes))


def hidden(plan):
    """Paths a session makes UNREADABLE: the secret masks, the directory masks, and the dev-swap targets
    (where the agent sees the substitute, not the real file). A secret at one of these paths cannot be read
    from inside, so grantcheck does not flag it -- this is the only "covered" the scan may use.

    Not the same set as `covered()`. A read-only cover is fully readable; it only stops writes."""
    out = set()
    for m in plan.mounts:
        if m.kind in ("mask", "tmpfs"):
            out.add(os.path.realpath(m.dst))
        elif m.kind == "bind" and os.path.realpath(m.src) != os.path.realpath(m.dst):
            # A dev-swap: the agent sees the stand-in, read-only or not. (This said `not m.ro` from 28 Oct
            # to 11 Nov, meaning to exclude the read-only covers -- but those all bind a path onto itself, so
            # the src/dst test already does, and a swap in a data_ro grant was reported as the secret.)
            out.add(os.path.realpath(m.dst))
    return sorted(out)


def covered(plan, roots):
    """Paths the agent's own container mounts over and a container it SPAWNS would not: `hidden()` plus the
    read-only covers (git hooks/config, compose files). The gate refuses a spawned container's bind that
    reaches any of these, since none of these in-container mounts exist there (crit 5 Oct).

    A read-only bind whose destination is a grant root is not a cover: it is a data_ro grant the agent may
    read, and the gate keeps it read-only in spawned containers (`ro_roots`, audit 5 Oct H3)."""
    real_roots = {os.path.realpath(r) for r in roots}
    out = set(hidden(plan))
    for m in plan.mounts:
        dst = os.path.realpath(m.dst)
        if m.kind == "bind" and m.ro and dst not in real_roots:
            out.add(dst)
    return sorted(out)


def hardlink_masks(mounts, grants, root, mask_skip, notes):
    """A cover hides a path, not an inode (audit 6 Oct, N4; spec 4.4): every other name of a covered regular
    file (a mask, a file under a mask dir, the real file behind the .env swap, a read-only cover such as
    .git/config) is masked too."""
    covered_files, cover_dirs = set(), []
    for m in mounts:
        if m.kind == "tmpfs":
            cover_dirs.append(m.dst)
        elif m.kind == "mask" or (m.kind == "bind" and (m.ro or m.src != m.dst) and m.dst not in grants):
            covered_files.add(m.dst)
    for d in cover_dirs:
        for dirpath, _dirnames, filenames in os.walk(d, followlinks=False):
            covered_files.update(os.path.join(dirpath, f) for f in filenames)
    inodes = {}
    for f in covered_files:
        try:
            st = os.lstat(f)
        except OSError as e:
            if e.errno == errno.ENOENT:
                continue                      # gone: it cannot be the target of a live hard link
            raise AgentboxError(f"{f}: cannot stat this covered file ({e}), so another hard link to it "
                                "could not be found and masked")
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            inodes[(st.st_dev, st.st_ino)] = f
    if not inodes:
        return []
    out = []
    for g in grants:
        for dirpath, _dirnames, filenames in _walk(g, _rel_skips(root, g, mask_skip)):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if p in covered_files or any(inside(p, d) for d in cover_dirs):
                    continue
                try:
                    st = os.lstat(p)
                except OSError as e:
                    if e.errno == errno.ENOENT:
                        continue              # vanished mid-walk: not a live hard link
                    raise AgentboxError(f"{p}: cannot stat this file ({e}), so whether it is another hard "
                                        "link to a covered file cannot be told")
                key = (st.st_dev, st.st_ino)
                if st.st_nlink > 1 and key in inodes:
                    out.append(Mount("mask", p))
                    notes.append(f"{p} is another hard link to the covered {inodes[key]}; masked too")
    return out


def mask_skip_paths(root, mask_skip):
    """mask_skip entries as absolute, normalised paths: each is relative to the project root, or absolute.
    The one resolver, for the mask walk here and grantcheck's "(under mask_skip: unmasked)" tag."""
    return [os.path.normpath(s if os.path.isabs(s) else os.path.join(root, s)) for s in mask_skip]


def _rel_skips(root, grant, mask_skip):
    """The mask_skip entries under `grant`, relative to it."""
    return [os.path.relpath(full, grant) for full in mask_skip_paths(root, mask_skip) if inside(full, grant)]


def prepare(p):
    """Create mount points the plan needs on the host (empty hooks dirs, empty .env for the dev swap)."""
    for d in p.create_dirs:
        os.makedirs(d, exist_ok=True)
    for f in p.create_files:
        try:
            write_private(f, "", atomic=False)
        except FileExistsError:
            pass
