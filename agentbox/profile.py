"""The host-owned agent profile: the skills, sub-agents, commands and instructions every session gets.

`~/.config/agentbox/<agent>/profile/` is the source, and only the host writes it. At session start its tree
is hashed, snapshotted to `<data>/profiles/<agent>/<hash>/`, and mounted READ-ONLY at the agent's own load
paths. Four properties follow, and they are the reason for the shape:

  * **Host-owned is trusted.** It loads under every preset, `lockdown` included, where a repo's own
    `.claude`/`.omp` are covered empty. The threat model separates what Tim writes on the host from code a
    repository ships, and this is the one channel for the first kind.
  * **A session cannot change what later sessions load.** The load paths are read-only, so a write there
    fails; what a session wants to keep goes to the project's `inbox/`, which is never loaded.
  * **What a session loaded is pinned.** The snapshot is taken at start, so editing the host profile
    mid-session cannot reach a running session, and the hash (with the profile's git commit) goes in the
    session log: "which skills did that session have?" is answerable afterwards.
  * **No symlinks.** A link in the profile would be followed when the snapshot is copied, which is how a
    host secret would end up mounted in a sandbox. Any link refuses the session, by name.

The profile is a git repository (initialised on first use) so promoting an inbox item is a commit with the
item's provenance in the message, and `git log` is the history of what the agent has been taught.
"""
import hashlib
import json
import os
import shutil
import subprocess
import time

from .errors import AgentboxError

# What a profile may hold. Anything else in the directory is ignored (and reported by `ab profile harvest`),
# so a stray note or a README cannot become something an agent loads.
ITEMS = ("skills", "agents", "commands", "rules", "prompts", "CLAUDE.md", "AGENTS.md", "settings.json")
SIDECAR = ".provenance.json"
SKIP = {".git"}


def _walk(src):
    """(relpath, absolute path) for every regular file in the profile, sorted, `.git` excluded.

    Refuses a symlink or anything that is not a regular file: the snapshot copy would follow a link out of
    the profile (`~/.ssh/id_ed25519` is one bind away), and a device or socket has no business being
    mounted into a sandbox."""
    out = []
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                raise AgentboxError(f"profile {full} is a symlink; refusing to use it (a link here would be "
                                    "followed into the snapshot and mounted into every session)")
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                raise AgentboxError(f"profile {full} is a symlink; refusing to use it (a link here would be "
                                    "followed into the snapshot and mounted into every session)")
            if not os.path.isfile(full):
                raise AgentboxError(f"profile {full} is not a regular file; refusing to mount it")
            out.append((os.path.relpath(full, src), full))
    return sorted(out)


def tree_hash(src):
    """A stable id for the profile's CONTENT: every file's path, executable bit and bytes, and nothing else.

    No mtimes and no inodes, so re-saving a file with the same bytes keeps the hash and the snapshot is
    reused; `.git` is excluded, so committing does not change it."""
    h = hashlib.sha256()
    files = _walk(src) if os.path.isdir(src) else []
    if not files:
        return ""                            # no profile, or an empty directory: there is nothing to load
    for rel, full in files:
        h.update(rel.encode() + b"\0")
        h.update(b"x" if os.lstat(full).st_mode & 0o111 else b"-")
        with open(full, "rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()[:16]


def snapshot(settings, agent, src=None):
    """(snapshot dir, hash) for the agent's profile, copying it only the first time each hash is seen.

    Returns (None, "") when there is no profile. The copy is built beside its destination and renamed into
    place, so a half-copied snapshot is never mounted."""
    src = src or settings.profile_src(agent)
    if not os.path.isdir(src):
        return None, ""
    digest = tree_hash(src)
    if not digest:
        return None, ""                      # the directory exists but holds no files
    base = os.path.join(settings.profiles_dir, agent)
    dst = os.path.join(base, digest)
    if os.path.isdir(dst):
        return dst, digest
    os.makedirs(base, mode=0o700, exist_ok=True)
    tmp = os.path.join(base, f".{digest}.{os.getpid()}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, mode=0o700)
    for rel, full in _walk(src):
        out = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(out), mode=0o700, exist_ok=True)
        shutil.copyfile(full, out)
        shutil.copymode(full, out)
    try:
        os.rename(tmp, dst)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)   # another session won the race; its copy is the same bytes
        if not os.path.isdir(dst):
            raise
    return dst, digest


# --- the profile's git history -----------------------------------------------------------------------

def _git(src, *args, check=True):
    p = subprocess.run(["git", "-C", src, *args], capture_output=True, text=True, timeout=60)
    if check and p.returncode != 0:
        raise AgentboxError(f"git {' '.join(args[:2])} in {src} failed: {(p.stderr or '').strip()[-200:]}")
    return p


def ensure_repo(src, out=None):
    """The profile is version-controlled, so what the agent was taught has a history and a promotion is a
    commit. Initialised on first use; an existing repo is left exactly as it is."""
    if not os.path.isdir(src):
        return False
    if os.path.isdir(os.path.join(src, ".git")):
        return False
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    if _git(src, "status", "--porcelain").stdout.strip():
        _git(src, "-c", "user.name=agentbox", "-c", "user.email=agentbox@localhost",
             "commit", "-qm", "profile: initial contents")
    if out:
        out(f"ab: made {src} a git repository (the profile's history is what the agent has been taught)")
    return True


def commit_state(src):
    """(commit, dirty) for the profile's git repo: ("", False) without one.

    Recorded beside the content hash, and separately from it, because they answer different questions. The
    hash says what a session loaded. The commit says which reviewed state that came from. `dirty` says the
    two have drifted -- the session loaded something that is in nobody's history, which is exactly what you
    want to know when a skill turns out to have misbehaved."""
    if not os.path.isdir(os.path.join(src, ".git")):
        return "", False
    p = _git(src, "rev-parse", "--short", "HEAD", check=False)
    if p.returncode != 0:
        return "", bool(_walk(src))          # a repo with no commit yet: everything in it is uncommitted
    return p.stdout.strip(), bool(_git(src, "status", "--porcelain", check=False).stdout.strip())


def commit_id(src):
    """The commit with a `-dirty` suffix, for a one-line display."""
    commit, dirty = commit_state(src)
    return commit + ("-dirty" if commit and dirty else "")


# --- the inbox ---------------------------------------------------------------------------------------

def items(path):
    """The inbox's top-level entries (a skill directory, a note), sidecars excluded."""
    if not os.path.isdir(path):
        return []
    return sorted(n for n in os.listdir(path) if not n.endswith(SIDECAR))


def sidecar_path(inbox, name):
    return os.path.join(inbox, name + SIDECAR)


def read_sidecar(inbox, name):
    try:
        with open(sidecar_path(inbox, name)) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def write_sidecar(inbox, name, doc):
    path = sidecar_path(inbox, name)
    tmp = path + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def record_new_items(settings, project, agent, before, *, session, preset, egress, now=None):
    """Give every inbox item this session added a provenance sidecar, and return their names.

    Written at teardown from the before/after difference, which is the only moment the host knows which
    items are new: nothing hooks the agent's individual writes. Provenance matters because an item is
    reviewed later, out of context -- and `egress` in particular says whether the session that produced it
    could reach the whole internet."""
    inbox = settings.inbox_dir(project, agent)
    new = [n for n in items(inbox) if n not in set(before or ())]
    for name in new:
        doc = {"project": project, "agent": agent, "session": session, "preset": preset, "egress": egress,
               "added": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now or time.time())),
               "reviewed": False}
        existing = read_sidecar(inbox, name)
        if existing.get("added"):
            continue                        # a second teardown of the same session: keep the first record
        write_sidecar(inbox, name, doc)
    return new


def all_inboxes(settings, agent):
    """[(project, inbox dir)] for every project that has one, registered or not: an item outliving the
    project that made it is exactly what `ab profile harvest` must still show."""
    out = []
    data = settings.data_dir
    if not os.path.isdir(data):
        return out
    for project in sorted(os.listdir(data)):
        if project in ("logs", "backups", "profiles", "ephemeral"):
            continue
        inbox = settings.inbox_dir(project, agent)
        if os.path.isdir(inbox) and items(inbox):
            out.append((project, inbox))
    return out


def content_id(path):
    """A hash of an inbox item's content, so two sessions that produced the same skill group together."""
    h = hashlib.sha256()
    if os.path.isfile(path):
        with open(path, "rb") as f:
            h.update(f.read())
        return h.hexdigest()[:12]
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            h.update(os.path.relpath(full, path).encode() + b"\0")
            if os.path.isfile(full) and not os.path.islink(full):
                with open(full, "rb") as f:
                    h.update(f.read())
    return h.hexdigest()[:12]


# --- what a session mounts ---------------------------------------------------------------------------

class Loaded:
    """One session's profile: the snapshot it mounts, and the identity of what it loaded."""

    def __init__(self, agent, src, dir=None, hash="", commit="", dirty=False, mounts=(), notes=(),
                 off_reason=""):
        self.agent, self.src, self.dir, self.hash, self.commit = agent, src, dir, hash, commit
        self.dirty = dirty                          # the profile tree differs from its own last commit
        self.mounts = list(mounts)                  # (host src, container dst, read_only)
        self.notes = list(notes)
        self.off_reason = off_reason                # "flag" | "project" when the profile is not loaded

    @property
    def names(self):
        return [os.path.basename(dst) for _src, dst, _ro in self.mounts]

    @property
    def off(self):
        return bool(self.off_reason)

    def log_line(self):
        if self.off:
            # Nothing was read, so there is no hash, commit or snapshot to record -- only that it was off
            # and which of the two switches did it.
            return f"profile {self.agent}=off reason={self.off_reason}\n"
        return (f"profile agent={self.agent} hash={self.hash or '(none)'} commit={self.commit or '(no repo)'} "
                f"dirty={'yes' if self.dirty else 'no'} snapshot={self.dir or '(none)'} "
                f"loaded={','.join(self.names) or '(nothing)'}\n")

    @property
    def display(self):
        if self.off:
            return f"off ({self.off_reason})"
        return (f"{self.hash or '(none)'}"
                + (f" (commit {self.commit}{', uncommitted changes' if self.dirty else ''})"
                   if self.commit else " (not committed)" if self.hash else ""))


def migrate_legacy_seed(settings, agent, out=None):
    """Move a pre-profile seed (`~/.config/agentbox/<agent>/settings.json`, `CLAUDE.md`, `commands/`) down
    into `profile/`, once, and say so.

    Explicit rather than a fallback: if the loader also read the old location, there would be two places a
    skill or an instruction file could live and no way to tell which one a session used. Moved, not copied,
    so there is exactly one. Nothing is overwritten -- an item already in the profile wins and the old one
    is left for the user to resolve."""
    base = os.path.join(settings.config_dir, agent)
    profile_dir = settings.profile_src(agent)
    if not os.path.isdir(base):
        return []
    moved, kept = [], []
    for item in ITEMS:
        src = os.path.join(base, item)
        if not os.path.lexists(src):
            continue
        dst = os.path.join(profile_dir, item)
        if os.path.lexists(dst):
            kept.append(item)
            continue
        os.makedirs(profile_dir, mode=0o700, exist_ok=True)
        shutil.move(src, dst)
        moved.append(item)
    if moved and out:
        out(f"ab: moved {', '.join(moved)} from {base} into {profile_dir} -- the host-owned profile every "
            f"session now loads read-only. Nothing reads the old location any more.")
    if kept and out:
        out(f"ab: {', '.join(kept)} exist in BOTH {base} and {profile_dir}; the profile's copy is what "
            f"sessions load. Delete the ones in {base} once you have checked them.")
    return moved


def user_image(settings, agent):
    """The user's own image layer for this agent, or "" when there is none.

    The hook point for the planned `agentbox-base -> agentbox-<agent> -> user layer` chain: a user layer is
    built FROM the harness image and is profile content like a skill is, so `--no-profile` must not select
    it. Nothing builds one yet, so this is always "" -- but the launcher asks HERE (`session.Session.image`),
    which is what makes the `--no-profile` guarantee hold the moment something does."""
    return ""


def load_for_session(settings, adapter, *, out=None, off_reason=""):
    """Snapshot the agent's profile and work out what to mount read-only where.

    `adapter.profile_mounts` maps a profile item to the container path the agent loads it from; an item the
    profile does not have is simply not mounted. `adapter.profile_copies` are the exceptions that cannot be
    read-only (Claude Code writes its own settings.json), copied into the per-project state instead.

    With `off_reason` set (`ab run --no-profile`, or `profile = "off"` on the project) nothing is read at
    all: no snapshot is taken, no repo is created, no legacy seed is migrated. A session that does not load
    the profile must not reach into it either, or "off" would still be deciding what the host profile looks
    like for the next run that is on."""
    if off_reason:
        return Loaded(adapter.name, settings.profile_src(adapter.name), off_reason=off_reason)
    src = settings.profile_src(adapter.name)
    migrate_legacy_seed(settings, adapter.name, out=out)
    ensure_repo(src, out=out)
    dir, digest = snapshot(settings, adapter.name)
    commit, dirty = commit_state(src)
    loaded = Loaded(adapter.name, src, dir=dir, hash=digest, commit=commit, dirty=dirty)
    if not dir:
        return loaded
    for item, dst in sorted((adapter.profile_mounts or {}).items()):
        path = os.path.join(dir, item)
        if os.path.exists(path):
            loaded.mounts.append((path, dst, True))
    return loaded


def copy_into_state(settings, adapter, state_dir):
    """The profile items that cannot be mounted read-only, copied into the per-project state dir.

    Claude Code writes settings.json itself (it records `skipDangerousModePermissionPrompt` on first start
    with approvals off), so a read-only one would make it fail at every launch. Copied at every start, so
    the host profile still decides what it says -- the session's edits to it do not survive."""
    src = settings.profile_src(adapter.name)
    copied = []
    for item in adapter.profile_copies or ():
        s, d = os.path.join(src, item), os.path.join(state_dir, item)
        if os.path.islink(s) or not os.path.isfile(s):
            continue
        if os.path.islink(d) or (os.path.lexists(d) and not os.path.isfile(d)):
            os.unlink(d)
        shutil.copyfile(s, d)
        copied.append(item)
    return copied
