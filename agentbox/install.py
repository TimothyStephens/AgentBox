"""`make install`: put the launcher on PATH as `agentbox`, with `ab` as an alias.

Writes <bindir>/agentbox (a two-line shell launcher for this checkout, marked as ours) and <bindir>/ab, a
symlink to it. It never overwrites an `ab` or `agentbox` that is not ours (ApacheBench installs `ab`), and
it says when another `ab` earlier on PATH would shadow ours, or ours would shadow one later on PATH.
"""
import argparse
import os
import shutil
import sys

MARKER = "# agentbox launcher, installed by `make install`"


def launcher(repo):
    return (f"#!/bin/sh\n{MARKER} from {repo}\n"
            f'exec env PYTHONPATH="{repo}${{PYTHONPATH:+:$PYTHONPATH}}" python3 -m agentbox "$@"\n')


def ours(path, bindir):
    """True if `path` is our launcher, or a symlink to our launcher in bindir."""
    if os.path.islink(path):
        return os.path.realpath(path) == os.path.realpath(os.path.join(bindir, "agentbox")) and \
            ours(os.path.join(bindir, "agentbox"), bindir)
    try:
        with open(path, errors="replace") as f:
            return MARKER in f.read(4096)
    except OSError:
        return False


def install(bindir, repo, out=sys.stdout, path_env=None):
    bindir, repo = os.path.abspath(os.path.expanduser(bindir)), os.path.abspath(repo)
    target, alias = os.path.join(bindir, "agentbox"), os.path.join(bindir, "ab")
    for p in (target, alias):
        if os.path.lexists(p) and not ours(p, bindir):
            what = os.readlink(p) if os.path.islink(p) else "a file"
            raise SystemExit(f"refusing to overwrite {p} ({what}): it is not agentbox's launcher. Remove or "
                             "rename it yourself if it should go (ApacheBench installs `ab`, for one).")
    os.makedirs(bindir, exist_ok=True)
    tmp = target + ".new"
    with open(tmp, "w") as f:
        f.write(launcher(repo))
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)
    if not os.path.islink(alias):
        if os.path.lexists(alias):
            os.unlink(alias)                     # our own old copy
        os.symlink("agentbox", alias)
    print(f"installed {target} and {alias} -> agentbox (running {repo})", file=out)
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    dirs = [os.path.abspath(os.path.expanduser(d)) for d in path_env.split(os.pathsep) if d]
    if bindir not in dirs:
        print(f"note: {bindir} is not on PATH; add it to use `agentbox` and `ab`", file=out)
    found = shutil.which("ab", path=path_env)
    others = [d for d in dirs if d != bindir and os.path.exists(os.path.join(d, "ab"))]
    if found and os.path.realpath(found) != os.path.realpath(target):
        print(f"note: `ab` on PATH is {found}, not agentbox; use `agentbox` or put {bindir} first", file=out)
    elif others:
        print(f"note: agentbox's `ab` shadows {os.path.join(others[0], 'ab')} on PATH", file=out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bindir", default="~/.local/bin")
    p.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = p.parse_args(argv)
    install(a.bindir, a.repo)


if __name__ == "__main__":
    main()
