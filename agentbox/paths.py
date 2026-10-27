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


def grants_of(entry):
    """(path, writable) for every grant a registry entry declares."""
    out = [(entry["root"], True)] if entry.get("root") else []
    out += [(d, True) for d in entry.get("data_rw", [])]
    out += [(d, False) for d in entry.get("data_ro", [])]
    return out


def check_overlaps(name, entry, reg):
    """No read-write grant may overlap another project's grants, in either direction; read-only may overlap
    read-only, which is what shared reference data is for.

    This is the only cross-project overlap check. check_grant used to take an `other_roots` list, but it
    only ever held other projects' ROOTS, so a data_rw grant could sit inside another project's data_rw or
    data_ro: two projects writing one tree, or one writing what another reads, with neither session able to
    tell (audit F3). Run at `ab project add`, `ab project set` and session start."""
    mine = [(os.path.realpath(os.path.expanduser(p)), rw) for p, rw in grants_of(entry)]
    for other_name, other in sorted(reg.items()):
        if other_name == name:
            continue
        for their_path, their_rw in grants_of(other):
            their_real = os.path.realpath(os.path.expanduser(their_path))
            for my_real, my_rw in mine:
                if not (_inside(my_real, their_real) or _inside(their_real, my_real)):
                    continue
                if not my_rw and not their_rw:
                    continue                      # read-only vs read-only: shared reference data
                raise AgentboxError(
                    f"project {name}: its {'read-write' if my_rw else 'read-only'} grant {my_real} "
                    f"overlaps project {other_name}'s {'read-write' if their_rw else 'read-only'} grant "
                    f"{their_real}. A read-write grant may not overlap another project's grants at all "
                    "(read-only may overlap read-only); narrow one of them with `ab project set`")


def check_grant(path, *, home, extra_protected=(), kind="grant"):
    """Resolve a grant and refuse it if it is unsafe in itself: `/`, $HOME or an ancestor, a system
    directory, or a protected path. Cross-project overlap is check_overlaps' job. Returns the real path."""
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
    return real
