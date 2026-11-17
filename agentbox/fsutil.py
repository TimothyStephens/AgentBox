"""Small path and file helpers shared by the launcher modules."""
import os
import shutil
import stat
import sys


def inside(path, root):
    """True when `path` is `root` or lies below it, compared as strings (callers realpath first where a link
    could matter). A trailing slash on `root` does not change the answer; `/a/bc` is not inside `/a/b`."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def write_private(path, data, mode=0o600, atomic=True, tmp_suffix=".tmp"):
    """Write `data` (str as UTF-8, or bytes) to a file only its owner can read, never through a symlink.

    atomic: write `path + tmp_suffix` and rename it over `path`. A leftover or planted temporary is
    unlinked first and the temporary made fresh (O_EXCL|O_NOFOLLOW): O_TRUNC on an existing file kept its
    mode and wrote through a link there. It is fsynced before the rename, so a crash leaves the old file or
    the new one, never an empty one. A symlink at `path` is replaced, not followed.
    Not atomic: create `path` itself with O_EXCL|O_NOFOLLOW; FileExistsError if anything is there.
    The mode is set on the open file too, so the umask cannot widen or narrow it."""
    if isinstance(data, str):
        data = data.encode()
    target = path + tmp_suffix if atomic else path
    if atomic:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), mode)
        f.write(data)
        if atomic:
            f.flush()
            os.fsync(f.fileno())
    if atomic:
        os.replace(target, path)
    return path


def rmtree(path, ignore_errors=False):
    """shutil.rmtree that also removes a tree holding read-only folders. rsync -a and a clone keep a folder's
    mode, so a toolchain's 0555 `bin/` in a project lands read-only in its snapshot, and a plain rmtree
    cannot unlink anything inside it: snapshot pruning then silently kept every snapshot of such a project
    (toulmin-build, 17 Nov). On a permission error the folder that holds the entry is made writable by its
    owner (and the entry itself, when it is a folder rmtree could not list) and the step is retried once.
    Symlinks are never followed: rmtree does not enter them, and only folders inside the tree are chmod'ed."""
    def retry(func, p, exc):
        if isinstance(exc, FileNotFoundError):
            return                                 # already gone (a folder removed by the retry below)
        if not isinstance(exc, PermissionError):
            raise exc
        for d in (os.path.dirname(p), p):
            try:
                st = os.lstat(d)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode) and inside(d, path):
                os.chmod(d, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
        if func in (os.unlink, os.remove, os.rmdir):
            func(p)
        elif os.path.isdir(p) and not os.path.islink(p):
            rmtree(p)                              # it could not be opened or listed: remove it whole now

    def onexc(func, p, exc):
        try:
            retry(func, p, exc)
        except Exception:
            if not ignore_errors:
                raise

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onexc)
    else:
        shutil.rmtree(path, onerror=lambda func, p, ei: onexc(func, p, ei[1]))
