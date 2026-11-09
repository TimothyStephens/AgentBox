"""Pre-session snapshots of read-write grants, as rsync --link-dest hard-link trees."""
import datetime
import os
import re
import shutil
import subprocess
import sys

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


def _snapshots(backups):
    if not os.path.isdir(backups):
        return []
    return sorted(d for d in os.listdir(backups) if SNAP.match(d) and not d.endswith(".incomplete"))


def _total_size(stats):
    m = re.search(r"Total file size: ([\d,]+) bytes", stats)
    return int(m.group(1).replace(",", "")) if m else 0


def snapshot(backups, session, grants, *, skip_rel_for=lambda g: (),
             exclude_rel_for=lambda g: (), warn_mb=0, now=None,
             warn=lambda msg: print(msg, file=sys.stderr)):
    """Back up every grant into one snapshot directory under `backups` (the project's own backups directory,
    `settings.project_backups_dir`). Returns the snapshot path. `exclude_rel_for(g)` gives paths (files or
    dirs, relative to the grant) to leave out: the masks and covers, so the backup never holds a plaintext
    copy of a secret the agent cannot damage anyway."""
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
        try:
            p = subprocess.run(rsync_argv(g, dst, link_dest=link, skip_rel=skip_rel_for(g),
                                          exclude_rel=exclude_rel_for(g)),
                               capture_output=True, text=True)
        except OSError as e:
            # The pre-session snapshot is the session's only undo, so a missing rsync has to stop the
            # session -- but as a condition with a fix in it, not a traceback (the same shape as a missing
            # podman binary, audit F-2).
            shutil.rmtree(work, ignore_errors=True)
            raise AgentboxError(f"cannot run rsync ({e}); agentbox needs it for the pre-session snapshot of "
                                f"{g}. Install it (`apt-get install rsync`) -- see docs/INSTALL.md.")
        # 24 = some files vanished during the copy; the snapshot is still usable.
        if p.returncode not in (0, 24):
            shutil.rmtree(work, ignore_errors=True)
            raise AgentboxError(f"backup of {g} failed (rsync {p.returncode}): {p.stderr.strip()[-300:]}")
        total += _total_size(p.stdout)
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
