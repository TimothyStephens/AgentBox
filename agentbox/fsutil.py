"""Small path and file helpers shared by the launcher modules."""
import os


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
