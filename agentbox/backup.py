"""Pre-session snapshots of read-write grants, as rsync --link-dest hard-link trees."""
import datetime
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

from .errors import AgentboxError
from .fsutil import rmtree

EXCLUDE_NAMES = (".venv", "venv", "node_modules", "__pycache__", ".cache", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", ".tox", ".nox", ".gradle", ".parcel-cache", ".turbo", ".next")
SNAP = re.compile(r"^(\d{8})-(\d{6})-(.+)$")
# The Cache Directory Tagging spec (bford.info/cachedir): a folder holding a CACHEDIR.TAG that starts with
# this line is a cache its tool can rebuild, and backups may leave it out. Cargo tags every `target/`, which
# in a Rust project is most of the tree (a 56 GB target/ in a 65 GB project took the backup to 53 GB).
CACHEDIR_TAG = "CACHEDIR.TAG"
CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"


def _tagged_cache(path):
    try:
        with open(os.path.join(path, CACHEDIR_TAG), "rb") as f:
            return f.read(len(CACHEDIR_SIGNATURE)) == CACHEDIR_SIGNATURE
    except OSError:
        return False


def cache_dirs(grant, skip_rel=(), exclude_rel=()):
    """Folders under `grant` (relative paths) tagged as caches with a valid CACHEDIR.TAG. The walk does not
    enter what the backup leaves out anyway (EXCLUDE_NAMES, `skip_rel`, `exclude_rel`) or a tagged folder,
    and follows no symlink. The tag is looked for in each folder's own listing, which the walk reads anyway,
    so an untagged tree costs one scandir per folder and no failed opens. The grant itself is never
    reported: a project root is not a cache."""
    pruned = {os.path.normpath(s.strip("/")) for s in (*skip_rel, *exclude_rel)}
    found, stack = [], [""]
    while stack:
        rel = stack.pop()
        path = os.path.join(grant, rel)
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            continue
        if rel and any(e.name == CACHEDIR_TAG for e in entries) and _tagged_cache(path):
            found.append(rel)
            continue
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False) or e.name in EXCLUDE_NAMES:
                    continue
            except OSError:
                continue
            r = os.path.join(rel, e.name)
            if r not in pruned:
                stack.append(r)
    return sorted(found)


def _rsync_literal(s):
    """Escape rsync's exclude-pattern wildcards (\\ * ? [ ]) so a real file name is matched literally. Without
    this a covered name like `prod[1].key` is read as a character class: the real file slips into the backup
    and `prod1.key` is excluded instead (audit 2026-10-15 #5)."""
    return re.sub(r"([\\*?\[\]])", r"\\\1", s)


def rsync_argv(src, dst, *, link_dest=None, skip_rel=(), exclude_rel=()):
    argv = ["rsync", "-a", "--delete", "--numeric-ids", "--stats"]
    for n in EXCLUDE_NAMES:
        argv.append(f"--exclude={n}/")
    for s in skip_rel:
        argv.append(f"--exclude=/{_rsync_literal(s.strip('/'))}/")  # a folder (mask_skip), anchored at the root
    for s in exclude_rel:
        argv.append(f"--exclude=/{_rsync_literal(s.strip('/'))}")   # a covered file or dir, anchored at the root
    if link_dest:
        argv.append(f"--link-dest={link_dest}")
    argv += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    return argv


_TO_CHK = re.compile(rb"to-chk=(\d+)/(\d+)")
_PROGRESS_LINE = re.compile(rb"^\s*[\d,]+\s+\d+%")


def _run_rsync(argv, progress=None):
    """(returncode, stdout, stderr) of one rsync. With `progress`, rsync also reports as it goes and
    `progress(fraction, detail)` gets the share of the file list done.

    The share is of files and folders, not bytes: with --link-dest an unchanged file is hard-linked rather
    than copied, and rsync's own byte percentage leaves those out, so a mostly-unchanged tree would finish at
    "33%". --no-inc-recursive makes rsync list the whole tree first, so the total is known from the start
    (one file list in memory, about 100 bytes a file). The progress lines are dropped from the returned
    output; `--stats` still parses."""
    if progress is None:
        p = subprocess.run(argv, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    argv = argv[:1] + ["--info=progress2", "--no-inc-recursive"] + argv[1:]
    with tempfile.TemporaryFile() as err:
        # Its own process group: a local rsync is two processes (sender and receiver), and killing only the
        # one we started left the receiver writing into the snapshot while it was being removed, so an
        # interrupted backup could leave `.incomplete` behind (seen under load, 17 Nov).
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err, start_new_session=True)
        kept, buf = [], b""
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                *parts, buf = re.split(rb"[\r\n]", buf + chunk)
                for part in parts:
                    m = _TO_CHK.search(part)
                    if m:
                        left, total = int(m[1]), int(m[2])
                        if total:
                            progress(1 - left / total, f"{total - left:,} of {total:,} files and folders")
                    elif not _PROGRESS_LINE.match(part):
                        kept.append(part)
            kept.append(buf)
            proc.wait()
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise
        finally:
            proc.stdout.close()
        err.seek(0)
        return (proc.returncode, b"\n".join(kept).decode(errors="replace"),
                err.read().decode(errors="replace"))


def _remove(path):
    """Remove a cut-short snapshot. Retried briefly: a killed rsync's last write can land just after the
    first pass."""
    for _ in range(10):
        rmtree(path, ignore_errors=True)
        if not os.path.lexists(path):
            return
        time.sleep(0.05)


def _snapshots(backups):
    if not os.path.isdir(backups):
        return []
    return sorted(d for d in os.listdir(backups) if SNAP.match(d) and not d.endswith(".incomplete"))


def _total_size(stats):
    m = re.search(r"Total file size: ([\d,]+) bytes", stats)
    return int(m.group(1).replace(",", "")) if m else 0


def snapshot(backups, session, grants, *, skip_rel_for=lambda g: (),
             exclude_rel_for=lambda g: (), warn_mb=0, now=None,
             warn=lambda msg: print(msg, file=sys.stderr), step=None):
    """Back up every grant into one snapshot directory under `backups` (the project's own backups directory,
    `settings.project_backups_dir`). Returns the snapshot path. `exclude_rel_for(g)` gives paths (files or
    dirs, relative to the grant) to leave out: the masks and covers, so the backup never holds a plaintext
    copy of a secret the agent cannot damage anyway.

    `step(grant)`, when given, returns the context manager that reports on the copy of that grant (a
    progress.Step); its `progress` gets rsync's share done. An interrupted copy (Ctrl-C) removes its
    half-written snapshot as a failed one does, so no `.incomplete` directory is left behind."""
    now = now or datetime.datetime.now()
    os.makedirs(backups, mode=0o700, exist_ok=True)
    prev = _snapshots(backups)
    # Session names are reused once a session ends, so two snapshots can share a second and a name.
    for n in range(1, 1000):
        name = f"{now:%Y%m%d-%H%M%S}-{session}" + (f".{n}" if n > 1 else "")
        final = os.path.join(backups, name)
        work = final + ".incomplete"
        if os.path.exists(final):
            continue
        try:
            os.mkdir(work, 0o700)
            break
        except FileExistsError:
            continue
    if os.path.exists(final):
        raise AgentboxError(f"backup snapshot {final} already exists")
    total = 0
    for g in grants:
        dst = os.path.join(work, g.lstrip("/"))
        os.makedirs(dst, exist_ok=True)
        link = None
        if prev:
            cand = os.path.join(backups, prev[-1], g.lstrip("/"))
            if os.path.isdir(cand):
                link = cand
        skip, excl = skip_rel_for(g), exclude_rel_for(g)

        def copy(progress=None):
            caches = cache_dirs(g, skip, excl)
            argv = rsync_argv(g, dst, link_dest=link, skip_rel=[*skip, *caches], exclude_rel=excl)
            return argv, _run_rsync(argv, progress)
        argv = ["rsync"]
        try:
            if step is None:
                argv, (rc, out, err) = copy()
            else:
                with step(g) as st:
                    argv, (rc, out, err) = copy(st.progress)
        except OSError as e:
            # The pre-session snapshot is the session's only undo, so a missing rsync has to stop the
            # session -- but as a condition with a fix in it, not a traceback (the same shape as a missing
            # podman binary, audit F-2). Only a missing binary gets the install advice: a full /tmp or a
            # closed stderr is an OSError too (review 16 Nov, R-7).
            _remove(work)
            if not (isinstance(e, FileNotFoundError) and e.filename in (None, argv[0])):
                raise AgentboxError(f"backup of {g} failed: {e}")
            raise AgentboxError(f"cannot run rsync ({e}); agentbox needs it for the pre-session snapshot of "
                                f"{g}. Install it (`apt-get install rsync`) -- see docs/INSTALL.md.")
        except BaseException:
            _remove(work)
            raise
        # 24 = some files vanished during the copy; the snapshot is still usable.
        if rc not in (0, 24):
            _remove(work)
            raise AgentboxError(f"backup of {g} failed (rsync {rc}): {err.strip()[-300:]}")
        total += _total_size(out)
    os.rename(work, final)
    if warn_mb and total > warn_mb * 1024 * 1024:
        warn(f"agentbox: backup {final} is {total // (1024 * 1024)} MB (over backup_warn_mb={warn_mb}); "
             "consider adding large folders to the project's mask_skip")
    return final


def retain(backups, *, keep=5, daily_days=7, today=None):
    """Keep the newest `keep` snapshots in the project's backups directory, plus the newest snapshot of each
    of the last `daily_days` days. `agent-state-<stamp>/` directories (reset-state) are not snapshots and stay."""
    snaps = _snapshots(backups)
    keep_set = set(snaps[-keep:]) if keep else set()
    today = today or datetime.date.today()
    newest_per_day = {}
    for s in snaps:
        newest_per_day[SNAP.match(s).group(1)] = s
    for day, s in newest_per_day.items():
        d = datetime.datetime.strptime(day, "%Y%m%d").date()
        if 0 <= (today - d).days < daily_days:
            keep_set.add(s)
    removed = []
    for s in snaps:
        if s not in keep_set:
            rmtree(os.path.join(backups, s), ignore_errors=True)
            removed.append(s)
    # Leftovers of interrupted backups; a day's grace so a concurrent session's copy is never removed.
    for d in os.listdir(backups) if os.path.isdir(backups) else []:
        full = os.path.join(backups, d)
        if d.endswith(".incomplete") and os.path.getmtime(full) < datetime.datetime.now().timestamp() - 86400:
            rmtree(full, ignore_errors=True)
    return removed
