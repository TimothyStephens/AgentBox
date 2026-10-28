"""The filesystem plan for a session: grants, read-only overlays, secret masks and the dev swap.

Everything mounts at its real host path, so paths inside and outside the sandbox agree.
Masks are computed once, at session start.
"""
import errno
import fnmatch
import os
import stat
from dataclasses import dataclass

from .errors import AgentboxError

MASK_FILES = (".env", ".env.*", "*.pem", "*.key")
MASK_EXEMPT = (".env.example", ".env.sample", ".env.template", ".env.dev")
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


def _masked(name, swap_targets):
    if name in MASK_EXEMPT:
        return False
    return name in swap_targets or any(fnmatch.fnmatchcase(name, p) for p in MASK_FILES)


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


def secret_mounts(grant, *, ro, skip_rel=(), swap=None):
    """Masks and dev-swap mounts for one grant. Returns (mounts, files_to_create)."""
    swap = DEFAULT_SWAP if swap is None else swap
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
                if target not in present:
                    create.append(dst)
                mounts.append(Mount("bind", dst, os.path.join(dirpath, dev), ro=ro))
                handled.add(target)
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if fn in handled or os.path.islink(full) or not os.path.isfile(full):
                continue
            if _masked(fn, swap):
                mounts.append(Mount("mask", full))
    return mounts, create


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


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
                if not _inside(target, grant):
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


def git_overlays(git_dirs, grants):
    """Read-only hooks/, config and config.worktree for every git dir that sits inside a writable grant.
    A git dir outside every grant is not mounted, so the agent cannot reach it at all."""
    mounts, create_dirs = [], []
    for g in sorted(git_dirs):
        if not any(_inside(g, gr) for gr in grants):
            continue
        hooks = os.path.join(g, "hooks")
        if not os.path.isdir(hooks):
            create_dirs.append(hooks)
        mounts.append(Mount("bind", hooks, hooks, ro=True))
        for name in ("config", "config.worktree"):
            cfg = os.path.join(g, name)
            if os.path.isfile(cfg) and not os.path.islink(cfg):
                mounts.append(Mount("bind", cfg, cfg, ro=True))
    return mounts, create_dirs


def compose_overlays(grant, skip_rel=()):
    out = []
    for dirpath, _dirnames, filenames in _walk(grant, skip_rel):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if fn in COMPOSE_FILES and os.path.isfile(full) and not os.path.islink(full):
                out.append(Mount("bind", full, full, ro=True))
    return out


def plan(*, root, data_ro=(), data_rw=(), compose="rw", mask_skip=(), swap=None):
    """Build the mount plan. All paths must already be validated real paths."""
    mounts, dirs, files, notes = [], [], [], []
    rw_grants = [root] + list(data_rw)
    for g in rw_grants:
        mounts.append(Mount("bind", g, g))
    for g in data_ro:
        mounts.append(Mount("bind", g, g, ro=True))
    git_dirs = set()
    for g in rw_grants:
        found, outside = find_git_dirs(g, _rel_skips(root, g, mask_skip))
        git_dirs |= found
        # a .git file in one grant may point into another grant: protected there; otherwise not mounted
        for checkout, target in outside:
            if not any(_inside(target, gr) for gr in rw_grants):
                notes.append(f"{checkout}: .git points to {target}, which is not mounted (git will not work "
                             "there, and its hooks are out of the agent's reach)")
    m, d = git_overlays(git_dirs, rw_grants)
    mounts += m
    dirs += d
    if compose == "ro":
        mounts += compose_overlays(root, _rel_skips(root, root, mask_skip))
    for g in rw_grants + list(data_ro):          # secret masks always apply; there is no way to turn them off
        m, c = secret_mounts(g, ro=g in data_ro, skip_rel=_rel_skips(root, g, mask_skip), swap=swap)
        mounts += m
        files += c

    mounts += hardlink_masks(mounts, rw_grants + list(data_ro), root, mask_skip, notes)
    # One mount per destination; later (more specific) entries win.
    by_dst = {}
    for m in mounts:
        by_dst[m.dst] = m
    # Nothing is mounted inside a directory mask (audit 6 Oct, N2): a git dir or compose file under
    # secrets/ would otherwise get a real bind on top of the empty cover, showing the real file.
    covers = [d for d, m in by_dst.items() if m.kind == "tmpfs"]
    kept = [m for d, m in by_dst.items() if not any(_inside(d, c) and d != c for c in covers)]
    dirs = [d for d in dirs if not any(_inside(d, c) and d != c for c in covers)]
    files = [f for f in files if not any(_inside(f, c) for c in covers)]
    return Plan(kept, dirs, files, notes)


def hidden(plan):
    """Paths a session makes UNREADABLE: the secret masks, the directory masks, and the dev-swap targets
    (where the agent sees the substitute, not the real file). A secret at one of these paths cannot be read
    from inside, so grantcheck does not flag it -- this is the only "covered" the scan may use.

    Not the same set as `covered()`. A read-only cover is fully readable; it only stops writes."""
    out = set()
    for m in plan.mounts:
        if m.kind in ("mask", "tmpfs"):
            out.add(os.path.realpath(m.dst))
        elif m.kind == "bind" and not m.ro and os.path.realpath(m.src) != os.path.realpath(m.dst):
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
                if p in covered_files or any(_inside(p, d) for d in cover_dirs):
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


def _rel_skips(root, grant, mask_skip):
    """mask_skip entries are relative to the project root, or absolute. Return those under `grant`."""
    out = []
    for s in mask_skip:
        full = os.path.normpath(s if os.path.isabs(s) else os.path.join(root, s))
        if full == grant or full.startswith(grant.rstrip("/") + "/"):
            out.append(os.path.relpath(full, grant))
    return out


def prepare(p):
    """Create mount points the plan needs on the host (empty hooks dirs, empty .env for the dev swap)."""
    for d in p.create_dirs:
        os.makedirs(d, exist_ok=True)
    for f in p.create_files:
        try:
            os.close(os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass
