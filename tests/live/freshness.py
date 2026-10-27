"""Is the last passing `make claude-live` still current?

`make claude-live` (run by Tim, real token) is the only test of the real token end to end. When it passes it
records the commit it ran on in tests/live/claude-live-pass.txt; `make itest` then warns when any file that
handles tokens or keys has changed since that commit, so a stale pass is never mistaken for a current one.

  python3 -m tests.live.freshness check     print a warning if the pass is stale or missing (always exit 0)
"""
import datetime
import os
import socket
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PASS_FILE = os.path.join(REPO, "tests", "live", "claude-live-pass.txt")
# Append-only provenance beside the one-line record. The record says WHICH commit last passed and is
# overwritten by the next run; the log keeps every run (commit, timestamp, egress mode, host), so an
# overwrite is visible after the fact and a line nobody remembers producing can be traced -- or shown to
# have no run behind it. Both files are committed together.
PASS_LOG = os.path.join(REPO, "tests", "live", "claude-live-pass.log")

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


def record(mode, *, from_live_run=False):
    """Record HEAD as the last passing commit, unless token-handling files have uncommitted changes (then the
    pass does not describe any commit).

    `from_live_run=True` must be passed explicitly, and only `tests/live/claude_live.py` passes it. This
    file is the sole evidence that the real token was exercised end to end, and `make itest` trusts it to
    decide whether that evidence still describes the code -- so a line written by anything else is a FALSE
    pass that silences the staleness warning. A line appeared here once that no live run had produced; the
    writer was never identified, so the write is now opt-in rather than available to any caller."""
    if not from_live_run:
        raise RuntimeError(
            "freshness.record: refusing to write the claude-live pass record. Only the live runner "
            "(tests/live/claude_live.py) may, with from_live_run=True -- a line written by anything else "
            "is a false pass that silences make itest's staleness warning.")
    dirty = changed_since("HEAD")
    if dirty:
        print(f"claude-live passed, but NOT recorded: uncommitted changes in {', '.join(dirty)}. "
              "Commit them and run make claude-live again.")
        return False
    head = git("rev-parse", "HEAD").stdout.strip()
    with open(PASS_FILE, "w") as f:
        f.write(f"{head} {datetime.date.today().isoformat()} egress={mode}\n")
    when = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with open(PASS_LOG, "a") as f:
        f.write(f"{head} {when} egress={mode} host={socket.gethostname()}\n")
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
