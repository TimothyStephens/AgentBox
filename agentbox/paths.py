"""Grant validation: every directory a session may mount passes through here."""
import os

from .errors import AgentboxError

SYSTEM = ("/etc", "/var", "/usr", "/boot", "/dev", "/run", "/proc", "/sys", "/bin", "/sbin",
          "/lib", "/lib32", "/lib64", "/libx32", "/opt", "/root", "/srv", "/snap", "/mnt", "/media")
# Every user's scratch space. Refused as a grant in itself (the whole of it is other people's files too);
# a private directory below one is an ordinary grant, which is why these are not in SYSTEM (item 5).
SHARED_SCRATCH = ("/tmp", "/var/tmp")
HOME_PROTECTED = (".ssh", ".gnupg", ".local/share/containers", ".config/containers", ".config/systemd",
                  ".config/autostart", ".local/bin", ".claude", ".claude.json", ".omp",
                  ".config/claude-container", ".bashrc", ".bash_profile", ".bash_login", ".bash_logout",
                  ".bash_aliases", ".profile", ".zshrc", ".zprofile", ".config/fish")


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def protected_paths(home, extra=()):
    """Paths no grant may equal, sit inside, or contain: each home entry as written and, where it exists
    and is a link, where it really is. The grant is realpath'd before the comparison, so a `~/.gnupg` that
    links to `~/vault/gpg` protected nothing until the target was listed too (item 5)."""
    out = []
    for p in HOME_PROTECTED:
        literal = os.path.join(home, p)
        out.append(literal)
        if os.path.lexists(literal):
            real = os.path.realpath(literal)
            if real != literal:
                out.append(real)
    return out + [os.path.realpath(p) for p in extra]


def grants_of(entry):
    """(path, writable) for every grant a registry entry declares."""
    out = [(entry["root"], True)] if entry.get("root") else []
    out += [(d, True) for d in entry.get("data_rw", [])]
    out += [(d, False) for d in entry.get("data_ro", [])]
    return out


def _check_within_one_project(name, mine):
    """One project's own grants: a read-write grant may not sit inside its own read-only one (audit F-7).

    The deeper mount wins, so that subtree would be writable while `ab project show` lists it under data_ro:
    the display would say read-only and the session would not be. The other direction is allowed -- a
    read-only grant inside a read-write one makes that subtree read-only, which is tighter than the display,
    not looser -- and so is a read-write grant inside the (read-write) root, where both mounts agree."""
    for mine_real, mine_rw in mine:
        if not mine_rw:
            continue
        for ro_real, ro_rw in mine:
            if ro_rw or not _inside(mine_real, ro_real):
                continue
            same = mine_real == ro_real
            raise AgentboxError(
                f"project {name}: {mine_real} is declared read-write and "
                + ("read-only at the same time" if same else f"sits inside its own read-only grant "
                   f"{ro_real}") +
                ". The deeper mount wins, so that path would be writable while `ab project show` lists it "
                "as read-only. Move it out of the read-only grant, or declare it read-only too "
                "(a read-only grant inside a read-write one is fine: it makes that subtree read-only).")


def check_overlaps(name, entry, reg):
    """No read-write grant may overlap another project's grants, in either direction; read-only may overlap
    read-only, which is what shared reference data is for.

    This is the only cross-project overlap check. check_grant used to take an `other_roots` list, but it
    only ever held other projects' ROOTS, so a data_rw grant could sit inside another project's data_rw or
    data_ro: two projects writing one tree, or one writing what another reads, with neither session able to
    tell (audit F3). Run at `ab project add`, `ab project set` and session start."""
    mine = [(os.path.realpath(os.path.expanduser(p)), rw) for p, rw in grants_of(entry)]
    _check_within_one_project(name, mine)
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
    if real in SHARED_SCRATCH:
        raise AgentboxError(f"{kind} {path}: {real} is the shared scratch directory of every user; grant a "
                            "directory of your own below it instead")
    for p in protected_paths(home, extra_protected):
        if _inside(real, p) or _inside(p, real):
            raise AgentboxError(f"{kind} {path}: {real} overlaps protected path {p}")
    return real
