"""Model token files: checked strictly, never read into argv, logs or the session's state."""
import getpass
import os
import stat
import sys

from .errors import AgentboxError


def check_token_file(path):
    """Refuse a token file unless it is a regular, non-empty file owned by us with mode 0600 or tighter."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise AgentboxError(f"token file {path} is missing; create it with `ab keys rotate <agent>`")
    if stat.S_ISLNK(st.st_mode):
        raise AgentboxError(f"token file {path} is a symlink; it must be a regular file")
    if not stat.S_ISREG(st.st_mode):
        raise AgentboxError(f"token file {path} is not a regular file")
    if st.st_uid != os.getuid():
        raise AgentboxError(f"token file {path} is not owned by you")
    if st.st_mode & 0o077:
        raise AgentboxError(f"token file {path} has mode {stat.S_IMODE(st.st_mode):04o}; run: chmod 600 {path}")
    if st.st_size == 0:
        raise AgentboxError(f"token file {path} is empty")
    return path


def rotate(path, agent, read=getpass.getpass, out=sys.stderr):
    print(f"Rotate the {agent} token:\n"
          f"  1. In another terminal on this host, run:  claude setup-token\n"
          f"  2. Paste the token it prints below (input is hidden).\n"
          f"  3. Afterwards, revoke the old token in your Claude account settings.", file=out)
    token = read("token: ").strip()
    if not token or any(c.isspace() for c in token):
        raise AgentboxError("no token given (or it contains whitespace); nothing changed")
    if not token.startswith("sk-ant-"):
        print("warning: this does not look like a Claude token (expected sk-ant-...); saving anyway", file=out)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print(f"saved {path}; new sessions use it, running sessions keep the old one until restarted", file=out)
