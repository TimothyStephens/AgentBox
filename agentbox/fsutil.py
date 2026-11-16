"""Small path and file helpers shared by the launcher modules."""


def inside(path, root):
    """True when `path` is `root` or lies below it, compared as strings (callers realpath first where a link
    could matter). A trailing slash on `root` does not change the answer; `/a/bc` is not inside `/a/b`."""
    return path == root or path.startswith(root.rstrip("/") + "/")
