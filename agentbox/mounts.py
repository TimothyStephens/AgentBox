"""The filesystem plan for a session: grants, read-only overlays, secret masks and the dev swap.

Everything mounts at its real host path, so paths inside and outside the sandbox agree.
Masks are computed once, at session start.
"""
import fnmatch
import os
from dataclasses import dataclass

MASK_FILES = (".env", ".env.*", "*.pem", "*.key")
MASK_EXEMPT = (".env.example", ".env.sample", ".env.template", ".env.dev")
MASK_DIRS = ("secrets",)
WALK_SKIP = (".git", "node_modules", ".venv", "venv", "__pycache__")
COMPOSE_FILES = ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml",
                 "podman-compose.yml", "podman-compose.yaml")
DEFAULT_SWAP = {".env": ".env.dev"}


@dataclass(frozen=True)
class Mount:
    kind: str          # bind | devnull | tmpfs
    dst: str
    src: str = ""
    ro: bool = False

    def podman_arg(self):
        if self.kind == "bind":
            return f"--mount=type=bind,src={self.src},dst={self.dst}" + (",ro" if self.ro else "")
        if self.kind == "devnull":
            return f"--mount=type=bind,src=/dev/null,dst={self.dst},ro"
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
                mounts.append(Mount("devnull", full))
    return mounts, create


def git_overlays(grant):
    """Read-only .git/hooks and .git/config for a writable repo, and for each submodule's git dir."""
    mounts, create_dirs, notes = [], [], []
    gitdir = os.path.join(grant, ".git")
    if os.path.isfile(gitdir):
        notes.append(f"{grant}: .git is a file (worktree or submodule checkout); its git dir is not mounted")
        return mounts, create_dirs, notes
    if not os.path.isdir(gitdir):
        return mounts, create_dirs, notes
    gitdirs = [gitdir]
    modules = os.path.join(gitdir, "modules")
    if os.path.isdir(modules):
        for dirpath, dirnames, filenames in os.walk(modules, followlinks=False):
            if "HEAD" in filenames and "config" in filenames:
                gitdirs.append(dirpath)
    for g in gitdirs:
        hooks = os.path.join(g, "hooks")
        if not os.path.isdir(hooks):
            create_dirs.append(hooks)
        mounts.append(Mount("bind", hooks, hooks, ro=True))
        cfg = os.path.join(g, "config")
        if os.path.isfile(cfg):
            mounts.append(Mount("bind", cfg, cfg, ro=True))
    return mounts, create_dirs, notes


def compose_overlays(grant, skip_rel=()):
    out = []
    for dirpath, _dirnames, filenames in _walk(grant, skip_rel):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if fn in COMPOSE_FILES and os.path.isfile(full) and not os.path.islink(full):
                out.append(Mount("bind", full, full, ro=True))
    return out


def plan(*, root, data_ro=(), data_rw=(), compose="rw", secrets="masked", mask_skip=(), swap=None):
    """Build the mount plan. All paths must already be validated real paths."""
    mounts, dirs, files, notes = [], [], [], []
    rw_grants = [root] + list(data_rw)
    for g in rw_grants:
        mounts.append(Mount("bind", g, g))
    for g in data_ro:
        mounts.append(Mount("bind", g, g, ro=True))
    for g in rw_grants:
        m, d, n = git_overlays(g)
        mounts += m
        dirs += d
        notes += n
    if compose == "ro":
        mounts += compose_overlays(root, _rel_skips(root, root, mask_skip))
    if secrets != "exposed":
        for g in rw_grants + list(data_ro):
            m, c = secret_mounts(g, ro=g in data_ro, skip_rel=_rel_skips(root, g, mask_skip), swap=swap)
            mounts += m
            files += c
    # One mount per destination; later (more specific) entries win.
    by_dst = {}
    for m in mounts:
        by_dst[m.dst] = m
    return Plan(list(by_dst.values()), dirs, files, notes)


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
