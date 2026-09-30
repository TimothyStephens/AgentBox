"""Is the last passing `make claude-live` still current?

`make claude-live` (run by Tim, real token) is the only test of the real token end to end. When it passes it
records the commit it ran on in tests/live/claude-live-pass.txt; `make itest` then warns when any file that
handles tokens or keys has changed since that commit, so a stale pass is never mistaken for a current one.

  python3 -m tests.live.freshness check     print a warning if the pass is stale or missing (always exit 0)
"""
import datetime
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PASS_FILE = os.path.join(REPO, "tests", "live", "claude-live-pass.txt")

# Everything that reads, moves, exports, checks or could write a token or key.
TOKEN_FILES = [
    "agentbox/tokens.py",
    "agentbox/keys.py",
    "agentbox/session.py",
    "agentbox/hardening.py",
    "agentbox/backup.py",
    "agentbox/podman.py",
    "agentbox/adapters/",
    "agentbox/selftest/",
    "tests/live/claude_live.py",
]


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)


def changed_since(commit):
    """Token-handling files that differ from `commit`: committed since, uncommitted, or new and untracked."""
    diff = git("diff", "--name-only", commit, "--", *TOKEN_FILES)
    if diff.returncode != 0:
        return None
    new = git("ls-files", "--others", "--exclude-standard", "--", *TOKEN_FILES).stdout.split()
    return sorted(set(diff.stdout.split()) | set(new))


def record(mode):
    """Record HEAD as the last passing commit, unless token-handling files have uncommitted changes (then the
    pass does not describe any commit)."""
    dirty = changed_since("HEAD")
    if dirty:
        print(f"claude-live passed, but NOT recorded: uncommitted changes in {', '.join(dirty)}. "
              "Commit them and run make claude-live again.")
        return False
    head = git("rev-parse", "HEAD").stdout.strip()
    with open(PASS_FILE, "w") as f:
        f.write(f"{head} {datetime.date.today().isoformat()} egress={mode}\n")
    print(f"claude-live pass recorded for {head[:12]} in {os.path.relpath(PASS_FILE, REPO)} (commit it)")
    return True


def check(out=sys.stdout):
    """Returns the warning text ('' when current)."""
    try:
        with open(PASS_FILE) as f:
            commit, date = (f.read().split() + ["", ""])[:2]
    except OSError:
        commit = date = ""
    if not commit:
        msg = ("WARNING: no passing make claude-live is recorded; the real token has not been checked "
               "end to end for this code. Tim: run make claude-live.")
    else:
        changed = changed_since(commit)
        if changed is None:
            msg = (f"WARNING: the recorded claude-live pass ({commit[:12]}, {date}) is not a commit in this "
                   "repo. Tim: run make claude-live.")
        elif changed:
            msg = (f"WARNING: token-handling files changed since the last passing make claude-live "
                   f"({commit[:12]}, {date}): {', '.join(changed)}. Tim: run make claude-live.")
        else:
            msg = ""
    if msg:
        print("\n" + msg, file=out)
    return msg


if __name__ == "__main__":
    if sys.argv[1:] != ["check"]:
        sys.exit(__doc__)
    check()
