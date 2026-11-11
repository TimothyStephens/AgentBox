"""Reviewer projects: a separate clone of a builder's repository, synced on the host (decisions.md, 11 Nov 2026).

  ab project add <name> --reviews <builder> --preset P    clone the builder next to its root and register it
  ab project sync <reviewer | builder>                     bring the clone to the builder's last commit and
                                                           hand the reviewer's reviews to the builder

A reviewer is an ordinary project with its own root (the clone), so no grant rule changes: it overlaps
nothing, has its own memory, lock and posture, and sees only what the builder has committed. Neither agent can
reach the other's tree, so the two directions both happen here, on the host, when the user asks:

  * the clone fetches from its `origin` (the builder's root, which `git clone` set and `check` re-confirms)
    and checks out the builder's branch DETACHED -- the reviewer reviews, it does not commit;
  * the reviewer's `reviews/*.md` (excluded from its git) are copied to the builder's `.review/`.

Both trees are written by agents, so the host treats them as untrusted: git runs with hooks and fsmonitor off;
review files are opened through directory fds with O_NOFOLLOW (a symlink planted at `reviews/x.md` or at
`.review` cannot make the host read a secret or write outside the tree); only regular `*.md` files up to
MAX_REVIEW_BYTES are copied, and nothing is ever deleted on either side.
"""
import os
import re
import stat
import subprocess

from .errors import AgentboxError

KEY = "reviews"
REVIEWS_DIR = "reviews"          # in the reviewer's clone; listed in its .git/info/exclude
HANDOVER_DIR = ".review"         # in the builder's root
MAX_REVIEW_BYTES = 1 << 20
BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}$")
# Host-side git in a repository an agent can write: no hooks, no fsmonitor command, no submodule recursion.
# (.git/hooks and .git/config are read-only inside sessions too; this does not rely on that.)
SAFE_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "submodule.recurse=false"]


def _git(repo, *args, check=True):
    p = subprocess.run(SAFE_GIT + ["-C", repo, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AgentboxError(f"git {' '.join(args)} in {repo} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def _real(path):
    return os.path.realpath(os.path.expanduser(path))


def origin_of(repo):
    """The clone's `origin` as a real path, or "" when it has none or it is not a local path."""
    p = _git(repo, "config", "--get", "remote.origin.url", check=False)
    url = p.stdout.strip()
    if p.returncode != 0 or not url:
        return ""
    if url.startswith("file://"):
        url = url[len("file://"):]
    return _real(url) if url.startswith(("/", "~")) else ""


def default_root(builder_root, name):
    """Next to the builder: ~/repos/web + reviewer `web-review` -> ~/repos/web-review."""
    return os.path.join(os.path.dirname(_real(builder_root)), name)


def reviewers_of(reg, builder):
    return sorted(n for n, e in reg.items() if e.get(KEY) == builder)


def check(name, entry, reg):
    """At add, set and sync: the builder is another registered project, not itself a reviewer; this project is
    nobody's builder; and its root is a clone whose origin is the builder's root (what sync fetches from)."""
    builder = entry.get(KEY)
    theirs = reviewers_of({n: e for n, e in reg.items() if n != name}, name)
    if builder and theirs:
        raise AgentboxError(f"project {name}: {', '.join(theirs)} review{'s' if len(theirs) == 1 else ''} it, so it "
                            f"cannot itself review {builder!r}")
    if not builder:
        return
    if builder == name:
        raise AgentboxError(f"project {name}: --reviews names the project itself")
    if builder not in reg:
        raise AgentboxError(f"project {name}: --reviews {builder!r}, which is not a registered project")
    if reg[builder].get(KEY):
        raise AgentboxError(f"project {name}: {builder} itself reviews {reg[builder][KEY]!r}; name the project that "
                            "writes the code")
    mine, broot = _real(entry["root"]), _real(reg[builder]["root"])
    if origin_of(mine) != broot:
        raise AgentboxError(f"project {name}: {mine} is not a clone of {builder}'s root {broot} (its origin is "
                            f"{origin_of(mine) or 'unset'}). `ab project add {name} --reviews {builder}` without a "
                            "root makes one")


def make_clone(builder_root, dest):
    """Clone the builder's repository at `dest` for a new reviewer. Returns True when it made one (the caller
    removes it if registration then fails), False when `dest` already is a clone of the builder (reused)."""
    broot, dest = _real(builder_root), os.path.abspath(os.path.expanduser(dest))
    if not os.path.isdir(os.path.join(broot, ".git")):
        raise AgentboxError(f"{broot} is not a git repository, so there is nothing to clone for a reviewer")
    if os.path.lexists(dest):
        if os.path.isdir(os.path.join(dest, ".git")) and origin_of(dest) == broot:
            return False
        raise AgentboxError(f"{dest} already exists and is not a clone of {broot}; pass another root, or remove it")
    p = subprocess.run(SAFE_GIT + ["clone", "-q", "--no-hardlinks", broot, dest], capture_output=True, text=True)
    if p.returncode != 0:
        raise AgentboxError(f"git clone {broot} {dest} failed: {p.stderr.strip()[-400:]}")
    with open(os.path.join(dest, ".git", "info", "exclude"), "a") as f:
        f.write(f"\n# agentbox reviewer: reviews are handed to the builder by `ab project sync`\n/{REVIEWS_DIR}/\n")
    os.mkdir(os.path.join(dest, REVIEWS_DIR))
    return True


def _open_dir(path, *, create=False):
    """An fd for a directory that is not a symlink (O_NOFOLLOW on the last component), or None if absent."""
    if create:
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            pass
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise AgentboxError(f"{path} is not a plain directory ({e.strerror}); a symlink here is refused")


def hand_over(reviewer_root, builder_root):
    """Copy the reviewer's reviews/*.md into the builder's .review/. Returns (copied, skipped) name lists."""
    src = os.path.join(reviewer_root, REVIEWS_DIR)
    sfd = _open_dir(src)
    if sfd is None:
        return [], []
    copied, skipped = [], []
    try:
        names = sorted(n for n in os.listdir(sfd) if n.endswith(".md") and not n.startswith("."))
        if not names:
            return [], []
        dfd = _open_dir(os.path.join(builder_root, HANDOVER_DIR), create=True)
        try:
            for n in names:
                try:
                    fd = os.open(n, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=sfd)
                except OSError:
                    skipped.append(f"{n} (a symlink or unreadable)")
                    continue
                with os.fdopen(fd, "rb") as f:
                    st = os.fstat(f.fileno())
                    if not stat.S_ISREG(st.st_mode):
                        skipped.append(f"{n} (not a regular file)")
                        continue
                    if st.st_size > MAX_REVIEW_BYTES:
                        skipped.append(f"{n} (over {MAX_REVIEW_BYTES >> 20} MB)")
                        continue
                    data = f.read(MAX_REVIEW_BYTES + 1)
                try:
                    out = os.open(n, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
                except OSError as e:
                    skipped.append(f"{n} (cannot write it in {HANDOVER_DIR}/: {e.strerror})")
                    continue
                with os.fdopen(out, "wb") as f:
                    f.write(data)
                copied.append(n)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)
    return copied, skipped


def sync(reg, reviewer, *, branch=None, out=print):
    """Fetch the builder's commits into the reviewer's clone, check out `branch` (default: the builder's current
    branch) detached, and hand the reviews over. Refuses rather than overwrite tracked changes in the clone."""
    entry = reg[reviewer]
    check(reviewer, entry, reg)
    builder = entry[KEY]
    broot, rroot = _real(reg[builder]["root"]), _real(entry["root"])
    if branch is None:
        branch = _git(broot, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        if not branch:
            raise AgentboxError(f"{builder} ({broot}) is on a detached HEAD; name the branch to review with --branch")
    if not BRANCH.match(branch) or ".." in branch or branch.endswith((".lock", "/")):
        raise AgentboxError(f"--branch {branch!r} is not a branch name")
    dirty = _git(rroot, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise AgentboxError(f"{reviewer}'s clone {rroot} has changes to tracked files, which a checkout would "
                            f"overwrite:\n  " + "\n  ".join(dirty.splitlines()[:10]) +
                            f"\nA reviewer writes only under {REVIEWS_DIR}/. Look at them, then "
                            f"`git -C {rroot} checkout -- .` to drop them")
    _git(rroot, "fetch", "-q", "--prune", "--no-recurse-submodules", "origin")
    ref = f"origin/{branch}"
    if _git(rroot, "rev-parse", "-q", "--verify", f"refs/remotes/{ref}^{{commit}}", check=False).returncode != 0:
        raise AgentboxError(f"{builder} has no branch {branch!r} to review")
    _git(rroot, "checkout", "-q", "--detach", ref)
    head = _git(rroot, "log", "-1", "--format=%h %s").stdout.strip()
    out(f"{reviewer}: {rroot} is at {ref} ({head}), detached")
    copied, skipped = hand_over(rroot, broot)
    if copied:
        out(f"  handed to {builder}: {', '.join(f'{HANDOVER_DIR}/{n}' for n in copied)} (in {broot})")
        if _git(broot, "check-ignore", "-q", f"{HANDOVER_DIR}/{copied[0]}", check=False).returncode != 0:
            out(f"  note: {HANDOVER_DIR}/ is not ignored in {builder}'s repository; add `/{HANDOVER_DIR}/` to its "
                f".gitignore or .git/info/exclude so the reviews are not committed")
    else:
        out(f"  no reviews to hand over (the reviewer writes them as {rroot}/{REVIEWS_DIR}/*.md)")
    for s in skipped:
        out(f"  skipped {s}")
    return copied
