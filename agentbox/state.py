"""A project's agent state on the host: the one-time move into the inbox, `--fresh`'s reset, and the
per-session ephemeral and build-cache directories. Moved out of session.py (item 54); session re-exports it."""
import os
import shutil
import time

from . import config


def _free_name(directory, base):
    """`directory/base`, or `base.2`, `base.3`, ... : the first that nothing (not even a dangling link) holds."""
    dst, n = os.path.join(directory, base), 2
    while os.path.lexists(dst):
        dst, n = os.path.join(directory, f"{base}.{n}"), n + 1
    return dst


def migrate_state_to_inbox(settings, project, adapter, warn):
    """Move what earlier sessions accumulated in the per-project state into the project's inbox, once.

    Those paths (`skills/`, `agents/`, and Claude's state `CLAUDE.md`) are read-only mounts now, so
    whatever is under them would be shadowed and invisible rather than loaded. Nothing is deleted: it is
    moved to the inbox, where `ab profile harvest` can promote it into the shared profile or dismiss it.
    Returns the names moved."""
    src_dir = settings.state_dir(project, adapter.name)
    inbox = settings.inbox_dir(project, adapter.name)
    moved, removed, unreadable, stuck = [], [], [], []
    for name in adapter.migrate_to_inbox:
        # Always the project's own WRITABLE state (`<data>/projects/<project>/<agent>/<name>`) -- never the profile's
        # read-only snapshot, which is a different path and is what gets mounted ON TOP of this one.
        src = os.path.join(src_dir, name)
        if not os.path.exists(src) or os.path.islink(src):
            continue
        # A bind whose destination does not exist gets its mount point created by the container runtime,
        # so the state dir collects an empty file or directory that the launcher did not write and may not
        # be able to read (the owner and mode are the runtime's to choose; `_own_mount_point` now creates
        # these itself, so this handles the ones earlier sessions left). Neither holds anything, so neither
        # is content to rescue: both are simply removed, as an empty directory always was.
        try:
            empty = (not os.listdir(src)) if os.path.isdir(src) else (os.lstat(src).st_size == 0)
        except OSError:
            empty = False                     # cannot even look: treat it as content and keep it below
        if empty:
            (os.rmdir if os.path.isdir(src) else os.unlink)(src)
            continue
        readable = os.access(src, os.R_OK | (os.X_OK if os.path.isdir(src) else 0))
        try:
            redundant = readable and os.path.isfile(src) and adapter.redundant_state_copy(settings, name, src)
        except OSError:
            redundant = False                 # unreadable mid-check: keep it rather than judge it
        if redundant:
            os.unlink(src)
            removed.append(name)
            continue
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        dst = _free_name(inbox, f"from-state-{name}")
        try:
            shutil.move(src, dst)
        except OSError as e:
            # Renaming a DIRECTORY needs write access to the directory itself (its `..` entry changes), so
            # one a container created cannot be moved by us at all. Leave it exactly where it is and say
            # so: the read-only profile mount covers it, so nothing loads it and the session is unaffected.
            stuck.append(f"{src} ({e.strerror or e})")
            continue
        if not readable:
            # Preserved, not judged: we cannot tell whether it holds anything worth keeping.
            unreadable.append(name)
        moved.append(os.path.basename(dst))
    if moved:
        warn(f"agentbox: moved {', '.join(moved)} out of {project}'s {adapter.name} state into {inbox} "
             f"(those paths are read-only profile mounts now; nothing was deleted). "
             f"`ab profile harvest` reviews them.")
    if removed:
        warn(f"agentbox: removed {', '.join(removed)} from {project}'s {adapter.name} state: it was a copy "
             f"of what the profile already holds, with nothing added, so there was nothing to review. The "
             f"profile's own copy is what sessions load.")
    if unreadable:
        warn(f"agentbox: {', '.join(unreadable)} in {project}'s {adapter.name} state could not be read, so "
             f"it was moved to {inbox} unexamined rather than judged (a file a container wrote can carry an "
             f"owner or mode of its own).")
    if stuck:
        warn(f"agentbox: left in place, unreadable and unmovable: {'; '.join(stuck)}. Renaming a directory "
             f"needs write access to the directory itself, which this one does not give us. The profile's "
             f"read-only mount covers it, so nothing loads it and the session is unaffected; it was most "
             f"likely written from inside a container, and clearing it needs the same privileges that "
             f"created it.")
    return moved


def ephemeral_state(settings, session):
    """A lockdown session's agent state: empty at start, removed at teardown."""
    return os.path.join(settings.data_dir, "ephemeral", session)


def reset_state(settings, project, now=None, *, built_record=False):
    """Move a project's agent state (every agent's directory) into `<project>/backups/agent-state-<stamp>/`.
    With `built_record`, the gate's record of the project's built images goes with it (`ab project add
    --fresh`: a new project under an old name must not inherit images it may run). The identity file, the
    lock and the grant backups stay. Returns (the project dir, the archive) or None when there is nothing."""
    src = settings.project_dir(project)
    names = [agent for agent, _ in settings.agent_state_dirs(project)]
    record = settings.built_record(project)
    if built_record and os.path.isfile(record):
        names.append(config.BUILT_RECORD)
    if not names:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    dst = os.path.join(settings.project_backups_dir(project), f"agent-state-{stamp}")
    os.makedirs(dst, mode=0o700)
    for name in names:
        os.rename(os.path.join(src, name), os.path.join(dst, name))
    for leftover in (record + ".lock",):
        try:
            os.unlink(leftover)
        except FileNotFoundError:
            pass
    return src, dst


def service_tmp(settings, session):
    """The session's podman service's TMPDIR: buildah's RUN --mount=type=cache directories live under it, so a
    session never shares a build cache with the host's or another session's builds."""
    return os.path.join(settings.data_dir, "tmp", session)
