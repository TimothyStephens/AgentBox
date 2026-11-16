"""Pre-session snapshots of read-write grants, as rsync --link-dest hard-link trees."""
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile

from .errors import AgentboxError

EXCLUDE_NAMES = (".venv", "venv", "node_modules", "__pycache__", ".cache", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", ".tox", ".nox", ".gradle", ".parcel-cache", ".turbo", ".next")
SNAP = re.compile(r"^(\d{8})-(\d{6})-(.+)$")


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
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err)
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
            proc.kill()
            proc.wait()
            raise
        finally:
            proc.stdout.close()
        err.seek(0)
        return (proc.returncode, b"\n".join(kept).decode(errors="replace"),
                err.read().decode(errors="replace"))


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
        argv = rsync_argv(g, dst, link_dest=link, skip_rel=skip_rel_for(g), exclude_rel=exclude_rel_for(g))
        try:
            if step is None:
                rc, out, err = _run_rsync(argv)
            else:
                with step(g) as st:
                    rc, out, err = _run_rsync(argv, st.progress)
        except OSError as e:
            # The pre-session snapshot is the session's only undo, so a missing rsync has to stop the
            # session -- but as a condition with a fix in it, not a traceback (the same shape as a missing
            # podman binary, audit F-2).
            shutil.rmtree(work, ignore_errors=True)
            raise AgentboxError(f"cannot run rsync ({e}); agentbox needs it for the pre-session snapshot of "
                                f"{g}. Install it (`apt-get install rsync`) -- see docs/INSTALL.md.")
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        # 24 = some files vanished during the copy; the snapshot is still usable.
        if rc not in (0, 24):
            shutil.rmtree(work, ignore_errors=True)
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
            shutil.rmtree(os.path.join(backups, s), ignore_errors=True)
            removed.append(s)
    # Leftovers of interrupted backups; a day's grace so a concurrent session's copy is never removed.
    for d in os.listdir(backups) if os.path.isdir(backups) else []:
        full = os.path.join(backups, d)
        if d.endswith(".incomplete") and os.path.getmtime(full) < datetime.datetime.now().timestamp() - 86400:
            shutil.rmtree(full, ignore_errors=True)
    return removed
