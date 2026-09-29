"""Grant validation: every directory a session may mount passes through here."""
import os

from .errors import AgentboxError

SYSTEM = ("/etc", "/var", "/usr", "/boot", "/dev", "/run", "/proc", "/sys", "/bin", "/sbin",
          "/lib", "/lib32", "/lib64", "/libx32", "/opt", "/root", "/srv", "/snap", "/mnt", "/media")
HOME_PROTECTED = (".ssh", ".gnupg", ".local/share/containers", ".config/containers", ".config/systemd",
                  ".config/autostart", ".local/bin", ".claude", ".claude.json", ".omp",
                  ".config/claude-container", ".bashrc", ".bash_profile", ".bash_login", ".bash_logout",
                  ".bash_aliases", ".profile", ".zshrc", ".zprofile", ".config/fish")


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def protected_paths(home, extra=()):
    """Paths no grant may equal, sit inside, or contain."""
    return [os.path.join(home, p) for p in HOME_PROTECTED] + [os.path.realpath(p) for p in extra]


def check_grant(path, *, home, extra_protected=(), other_roots=(), kind="grant"):
    """Resolve a grant and refuse it if it is unsafe. Returns the real path."""
    if not path:
        raise AgentboxError(f"{kind}: empty path")
    real = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(real):
        raise AgentboxError(f"{kind} {path}: not an existing directory")
    home = os.path.realpath(home)

    # `/`, $HOME and every ancestor of $HOME
    if real == "/" or _inside(home, real):
        raise AgentboxError(f"{kind} {path}: {real} is / or $HOME or one of its ancestors")
    for s in SYSTEM:
        if _inside(real, s):
            raise AgentboxError(f"{kind} {path}: {real} is a system directory")
    for p in protected_paths(home, extra_protected):
        if _inside(real, p) or _inside(p, real):
            raise AgentboxError(f"{kind} {path}: {real} overlaps protected path {p}")
    for other_name, other in other_roots:
        other = os.path.realpath(other)
        if _inside(real, other) or _inside(other, real):
            raise AgentboxError(f"{kind} {path}: {real} overlaps project {other_name} ({other})")
    return real
