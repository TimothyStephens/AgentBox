"""The one-walk change (decisions.md, 19 Nov 2026) is a speed change of security code: it must not change a
single result. The mount plan's walks and grantcheck's scan now read one shared `mounts.Tree` (one scandir
per directory per start, no per-directory islink/realpath) instead of walking every grant four times.

The gate is equivalence: the functions as they were before the change are kept below, verbatim apart from an
`old_` prefix on the walking functions (their helpers did not change and are the live ones), and every test
asserts that the new code returns exactly what they return on a tree built to hit each skip rule: nested,
bare, separate-dir, worktree and submodule repos, a .git file pointing outside the grant, WALK_SKIP dirs
holding repos and secrets, symlinked dirs and files (dangling ones too), secrets at depth, a dev swap and its
edge cases, a project mask (name and path), hard links to covered files, a sparse live-data file, compose
data binds, a directory that cannot be listed, one that can be listed but not entered, a mask_skip folder,
read-write and read-only data grants (one nested in the root), and a git remote carrying credentials.
"""
import errno
import os
import stat
import subprocess
import unittest
from unittest import mock

from agentbox import grantcheck, mounts
from agentbox.errors import AgentboxError
from agentbox.fsutil import inside
from agentbox.grantcheck import (DATA_SUFFIXES, LIVE_DATA_MB, READABLE_REFUSED, WALK_SKIP, _compose_data_binds,
                                 _git_config_escapes, _git_credential_urls, _is_secret_name, _sig,
                                 _under_mask_skip, fingerprint, sig_path)
from agentbox.mounts import (COMPOSE_FILES, MASK_DIRS, Mount, Plan, _gitfile_target, _is_git_dir, _masked, _rel_skips,
                             effective_swap, env_file, git_overlays, mask_is_path)

from .helpers import SettingsCase, TempCase

mounts_WALK_SKIP = mounts.WALK_SKIP


# ---- the code before the change (agentbox/mounts.py and agentbox/grantcheck.py at a684a94) ----------------
# Verbatim but for: the `old_` names, `WALK_SKIP` inside the mounts functions spelled `mounts_WALK_SKIP`
# (grantcheck's own WALK_SKIP, without .git, is the module-level name here), and preflight's relative import.

def old_walk(grant, skip_rel):
    skip_abs = {os.path.normpath(os.path.join(grant, s)) for s in skip_rel}
    for dirpath, dirnames, filenames in os.walk(grant, topdown=True, followlinks=False):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d in mounts_WALK_SKIP or full in skip_abs or os.path.islink(full):
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        yield dirpath, dirnames, sorted(filenames)


def old_secret_mounts(grant, *, ro, skip_rel=(), swap=None, notes=None, real_env=False):
    """Masks and dev-swap mounts for one grant. Returns (mounts, files_to_create).

    `real_env` (the project root under `data_env = "on"`): the env files (`env_file`) are neither masked nor
    swapped, so the agent sees the real ones; every other masked name stays masked.

    A missing swap target is created empty (0600) as the mount point -- documented since 29 Sep -- but only in
    a read-write grant: a read-only grant is not ours to write into, so there the swap is skipped and noted
    (nothing is hidden by skipping it: there is no real file). A target that exists as something other than
    a regular file or symlink (a `.env/` directory, a FIFO) cannot take a file bind; it is skipped and noted
    instead of failing `podman run` (item 37)."""
    swap = effective_swap(swap)
    if real_env:
        swap = {t: d for t, d in swap.items() if not env_file(t)}
    notes = notes if notes is not None else []
    mounts, create = [], []
    for dirpath, dirnames, filenames in old_walk(grant, skip_rel):
        for d in list(dirnames):
            if d in MASK_DIRS:
                mounts.append(Mount("tmpfs", os.path.join(dirpath, d)))
                dirnames.remove(d)
        present = set(filenames)
        handled = set()
        for target, dev in swap.items():
            if dev in present and not os.path.islink(os.path.join(dirpath, dev)):
                dst = os.path.join(dirpath, target)
                if os.path.lexists(dst) and not (os.path.islink(dst) or os.path.isfile(dst)):
                    notes.append(f"{dst} is not a regular file, so {dev} is not shown in its place")
                    continue
                if not os.path.lexists(dst):
                    if ro:
                        notes.append(f"{dst} does not exist and {grant} is read-only, so {dev} is not shown "
                                     f"at {target} there (nothing is created in a read-only grant)")
                        continue
                    create.append(dst)
                mounts.append(Mount("bind", dst, os.path.join(dirpath, dev), ro=ro))
                handled.add(target)
        for fn in filenames:
            # The name test first: it is a regex, the two file-type tests are a stat each, and nearly every
            # file in a tree is not a mask candidate (two stats per file were 45% of a session's mask plan on
            # a 100k-file Rust tree, 17 Nov).
            if fn in handled or not _masked(fn) or (real_env and env_file(fn)):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            mounts.append(Mount("mask", full))
    return mounts, create


def old_user_masks(grant, *, root, mask, skip_rel=(), notes=None):
    """The project's own masks (the `mask` setting) in one grant. Returns (mounts, dirs_to_create).

    A NAME covers every file or directory of that name the walk finds -- the secret masks' own walk, so
    `mask_skip` folders are not searched and symlinks are not followed. A PATH covers exactly that path under
    the ROOT, when it lies in this grant. A directory becomes an empty read-only tmpfs, anything else the
    empty mask file. A path that does not exist yet is created as a directory and covered, so a mask added
    ahead of the data does not silently do nothing (docs/agentbox-masked-paths.md §4). A grant root itself
    is never masked: that would hide the whole grant."""
    notes = notes if notes is not None else []
    names = {m for m in mask if not mask_is_path(m)}
    out, create = [], []
    for rel in (m for m in mask if mask_is_path(m)):
        full = os.path.normpath(os.path.join(root, rel))
        if not inside(full, grant) or full == grant:
            continue
        if os.path.islink(full):
            notes.append(f"{full} is a symlink; the mask covers this path, not what it points to")
            out.append(Mount("mask", full))
        elif os.path.isdir(full):
            out.append(Mount("tmpfs", full, ro=True))
        elif os.path.lexists(full):
            out.append(Mount("mask", full))
        else:
            create.append(full)
            out.append(Mount("tmpfs", full, ro=True))
            notes.append(f"{full} does not exist yet; created empty and masked")
    if names:
        for dirpath, dirnames, filenames in old_walk(grant, skip_rel):
            for d in list(dirnames):
                if d in names:
                    out.append(Mount("tmpfs", os.path.join(dirpath, d), ro=True))
                    dirnames.remove(d)
            for fn in filenames:
                if fn in names:
                    out.append(Mount("mask", os.path.join(dirpath, fn)))
    return out, create


def old_find_git_dirs(grant, skip_rel=()):
    """Every git directory reachable from the grant: .git dirs of nested repos, the targets of .git files
    (worktrees, submodule checkouts, --separate-git-dir), bare repos, and submodule git dirs under modules/,
    plus each one's commondir. Returns (set of real paths, [(checkout, target outside this grant)])."""
    found, outside = set(), []
    skip_abs = {os.path.normpath(os.path.join(grant, s)) for s in skip_rel}

    def add(g):
        g = os.path.realpath(g)
        if g in found or not _is_git_dir(g):
            return
        found.add(g)
        common = os.path.join(g, "commondir")
        if os.path.isfile(common):
            with open(common) as f:
                add(os.path.join(g, f.read().strip()))
        modules = os.path.join(g, "modules")
        if os.path.isdir(modules):
            for dirpath, _dirnames, _filenames in os.walk(modules, followlinks=False):
                if dirpath != modules and _is_git_dir(dirpath):
                    add(dirpath)

    for dirpath, dirnames, filenames in os.walk(grant, topdown=True, followlinks=False):
        dot = os.path.join(dirpath, ".git")
        if ".git" in dirnames and not os.path.islink(dot):
            add(dot)
        elif ".git" in filenames and not os.path.islink(dot):
            target = _gitfile_target(dot)
            if target:
                add(target)
                if not inside(target, grant):
                    outside.append((dirpath, target))
        if dirpath != grant and _is_git_dir(dirpath):
            add(dirpath)                                    # a bare repo or separate git dir
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d in mounts_WALK_SKIP or full in skip_abs or os.path.islink(full):
                continue
            keep.append(d)
        dirnames[:] = keep
    return found, outside


def old_compose_overlays(grant, skip_rel=()):
    out = []
    for dirpath, _dirnames, filenames in old_walk(grant, skip_rel):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if fn in COMPOSE_FILES and os.path.isfile(full) and not os.path.islink(full):
                out.append(Mount("bind", full, full, ro=True))
    return out


def old_plan(*, root, data_ro=(), data_rw=(), compose="rw", mask_skip=(), swap=None, mask=(), code="rw", data_env=False):
    """Build the mount plan. All paths must already be validated real paths; `mask` is the project's own
    mask list, as projectkeys stores it. `code = "ro"` mounts the root read-only: it is then handled exactly
    like a data_ro grant (no git or compose covers in it, the swap read-only, masks over it). `data_env`
    (the project setting is "on") leaves the env files in the ROOT grant unmasked and unswapped: the agent
    sees the real ones. Nothing in the plan changes for `data_bind`: `<root>/data` is part of the root grant,
    and a mask over it is refused when the setting is stored (project.check_trust)."""
    mounts, dirs, files, notes = [], [], [], []
    ro_root = code == "ro"
    rw_grants = ([] if ro_root else [root]) + list(data_rw)
    data_ro = ([root] if ro_root else []) + list(data_ro)
    if ro_root:
        mounts.append(Mount("bind", root, root, ro=True))      # first: the root, then what lies elsewhere
    for g in rw_grants:
        mounts.append(Mount("bind", g, g))
    for g in data_ro[1 if ro_root else 0:]:
        mounts.append(Mount("bind", g, g, ro=True))
    git_dirs = set()
    for g in rw_grants:
        found, outside = old_find_git_dirs(g, _rel_skips(root, g, mask_skip))
        git_dirs |= found
        # a .git file in one grant may point into another grant: protected there; otherwise not mounted
        for checkout, target in outside:
            if not any(inside(target, gr) for gr in rw_grants):
                notes.append(f"{checkout}: .git points to {target}, which is not mounted (git will not work "
                             "there, and its hooks are out of the agent's reach)")
    ro_git_dirs = set()
    for g in data_ro:                            # their configs may name a path in a writable grant
        try:
            ro_git_dirs |= old_find_git_dirs(g, _rel_skips(root, g, mask_skip))[0]
        except AgentboxError as e:               # read-only: it did not refuse the plan before either
            notes.append(f"{e}; a hooks path its repository names in a writable grant is not covered")
    m, d, f = git_overlays(git_dirs, rw_grants, ro_git_dirs)
    mounts += m
    dirs += d
    files += f
    if compose == "ro" and not ro_root:          # a read-only cover in a read-only root adds nothing
        mounts += old_compose_overlays(root, _rel_skips(root, root, mask_skip))
    for g in rw_grants + list(data_ro):          # secret masks apply; only data_env lifts the root's env files
        m, c = old_secret_mounts(g, ro=g in data_ro, skip_rel=_rel_skips(root, g, mask_skip), swap=swap, notes=notes,
                             real_env=data_env and g == root)
        mounts += m
        files += c
    # The project's own masks come AFTER the secret masks and the swap: one mount per destination and the
    # later entry wins, so a masked path that is also a swap target ends up masked, never substituted.
    for g in rw_grants + list(data_ro):
        m, d = old_user_masks(g, root=root, mask=mask, skip_rel=_rel_skips(root, g, mask_skip), notes=notes)
        mounts += m
        dirs += d

    mounts += old_hardlink_masks(mounts, rw_grants + list(data_ro), root, mask_skip, notes)
    # One mount per destination; later (more specific) entries win.
    by_dst = {}
    for m in mounts:
        by_dst[m.dst] = m
    # Nothing is mounted inside a directory mask (audit 6 Oct, N2): a git dir or compose file under
    # secrets/ would otherwise get a real bind on top of the empty cover, showing the real file.
    covers = [d for d, m in by_dst.items() if m.kind == "tmpfs"]
    kept = [m for d, m in by_dst.items() if not any(inside(d, c) and d != c for c in covers)]
    dirs = [d for d in dirs if not any(inside(d, c) and d != c for c in covers)]
    files = [f for f in files if not any(inside(f, c) for c in covers)]
    return Plan(kept, dirs, files, notes)


def old_hardlink_masks(mounts, grants, root, mask_skip, notes):
    """A cover hides a path, not an inode (audit 6 Oct, N4; spec 4.4): every other name of a covered regular
    file (a mask, a file under a mask dir, the real file behind the .env swap, a read-only cover such as
    .git/config) is masked too."""
    covered_files, cover_dirs = set(), []
    for m in mounts:
        if m.kind == "tmpfs":
            cover_dirs.append(m.dst)
        elif m.kind == "mask" or (m.kind == "bind" and (m.ro or m.src != m.dst) and m.dst not in grants):
            covered_files.add(m.dst)
    for d in cover_dirs:
        for dirpath, _dirnames, filenames in os.walk(d, followlinks=False):
            covered_files.update(os.path.join(dirpath, f) for f in filenames)
    inodes = {}
    for f in covered_files:
        try:
            st = os.lstat(f)
        except OSError as e:
            if e.errno == errno.ENOENT:
                continue                      # gone: it cannot be the target of a live hard link
            raise AgentboxError(f"{f}: cannot stat this covered file ({e}), so another hard link to it "
                                "could not be found and masked")
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            inodes[(st.st_dev, st.st_ino)] = f
    if not inodes:
        return []
    out = []
    for g in grants:
        for dirpath, _dirnames, filenames in old_walk(g, _rel_skips(root, g, mask_skip)):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if p in covered_files or any(inside(p, d) for d in cover_dirs):
                    continue
                try:
                    st = os.lstat(p)
                except OSError as e:
                    if e.errno == errno.ENOENT:
                        continue              # vanished mid-walk: not a live hard link
                    raise AgentboxError(f"{p}: cannot stat this file ({e}), so whether it is another hard "
                                        "link to a covered file cannot be told")
                key = (st.st_dev, st.st_ino)
                if st.st_nlink > 1 and key in inodes:
                    out.append(Mount("mask", p))
                    notes.append(f"{p} is another hard link to the covered {inodes[key]}; masked too")
    return out


def old_scan(root, grants, mask_skip=(), covered=(), warn_grants=(), readable_grants=(), env_ok=(), data_ok=()):
    """Return (findings, warnings). Each finding is {"text": <shown>, "sig": <for the fingerprint>}; a grant
    with any finding refuses to start until acknowledged. Warnings are shown but do not block (audit 6 Oct,
    the cheap half of H1: entries the scanner can't read or that another user owns). `mask_skip` is NOT
    honoured here (H3): it is a speed knob for the mask walk, not an exemption; a secret under it is unmasked,
    so it must still be reported. `covered` are paths the masks hide (an empty cover makes a secret safe).

    `warn_grants` are the read-only data grants. They are scanned exactly the same way, but their findings
    come back as warnings (F4): a read-only grant holding live data is the declared purpose of the
    `analysis-offline` and `operate` presets, so it is reported, never refused. Before this they were not
    scanned at all, so what was in them was invisible. One exception: a repo in a read-only grant whose config
    names a hooks path or include in a WRITABLE grant that the covers cannot pin (a symlink, a grant root) is a
    finding, since the owner's git in that repo would run what the session wrote.

    The two trusted opt-outs (decisions.md, 19 Nov 2026): an env file (`mounts.env_file`) under a path in
    `env_ok` (the root, when `data_env` is on) is shown to the agent on purpose, so it is not an "unmasked
    secret"; live data and compose data binds under a path in `data_ok` (`<root>/data`, when `data_bind` is
    on) are the agent's on purpose, so they are not findings. Data grants are never in either list."""
    covered = {os.path.realpath(c) for c in covered}
    env_ok = [os.path.realpath(e) for e in env_ok]
    data_ok = [os.path.realpath(d) for d in data_ok]
    # The data grants: their env files stay masked under data_env (mounts.plan), so they stay findings, even
    # in a data grant that lies inside the root.
    excluded = {os.path.realpath(g) for g in list(grants) + list(warn_grants) + list(readable_grants)
                if os.path.realpath(g) != os.path.realpath(root)}

    def env_trusted(p, fn, in_secrets):
        return (not in_secrets and mounts.env_file(fn) and any(inside(p, e) for e in env_ok)
                and not any(inside(p, x) for x in excluded))

    def data_trusted(p):
        return any(inside(os.path.realpath(p), d) for d in data_ok)

    uid = os.getuid()
    limit = LIVE_DATA_MB << 20

    def is_covered(p):
        return any(inside(p, c) for c in covered)

    found, ro_found, warn = {}, {}, set()
    all_grants = [os.path.realpath(g) for g in list(grants) + list(warn_grants) + list(readable_grants)]
    rw_grants = [os.path.realpath(g) for g in grants]
    blocking = found
    readable = {}           # findings in a read-only project root (code = ro), sorted out after the walk

    def onerror(e):
        warn.add(f"{getattr(e, 'filename', '?')}: unreadable ({getattr(e, 'strerror', e)}); a container's data "
                 "dir? move it out of the grant")

    def walk(grant, found):
        for dirpath, dirnames, filenames in os.walk(grant, followlinks=False, onerror=onerror):
            rel = os.path.relpath(dirpath, grant)
            kept = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if d in WALK_SKIP or os.path.islink(full) or is_covered(os.path.realpath(full)):
                    continue
                try:
                    if os.lstat(full).st_uid != uid:
                        warn.add(f"{full}: owned by uid {os.lstat(full).st_uid}, not you; a container's data "
                                 "dir? move it out of the grant")
                        continue                         # not descended: the scanner can't see inside it anyway
                except OSError as e:
                    onerror(e)
                    continue
                kept.append(d)
            dirnames[:] = kept
            gd = os.path.join(dirpath, ".git")
            if os.path.isdir(gd):
                for c in _git_credential_urls(gd):
                    found[f"git credentials in a remote URL: {c}"] = f"gitcred:{c}"
                for text, sig in _git_config_escapes(gd, all_grants):
                    if (found is ro_found or found is readable) and rw_grants and (
                            sig == f"gitcfg:{gd}" or any(inside(sig_path(sig), g) for g in rw_grants)):
                        # a read-only repo naming an uncoverable path in a WRITABLE grant: the owner's git
                        # in that repo runs what the session writes there, so this refuses like any escape
                        # (read-only root proposal, section 3). A config it cannot read may name one, so it
                        # refuses too (decision 19 Nov 2026; mounts.plan refuses it first).
                        blocking[f"{text} (a repository in a read-only grant: {gd})"] = sig
                    else:
                        found[text] = sig
            in_secrets = any(part in mounts.MASK_DIRS for part in rel.split(os.sep))
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(p)
                except OSError as e:
                    onerror(e)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_uid != uid and not os.access(p, os.R_OK):
                    warn.add(f"{p}: owned by uid {st.st_uid}, not you (unreadable); a container's data file?")
                    continue
                if (_is_secret_name(fn, in_secrets) and st.st_size and not is_covered(p)
                        and not env_trusted(p, fn, in_secrets)):
                    tag = " (under mask_skip: unmasked)" if _under_mask_skip(root, p, mask_skip) else ""
                    found[f"unmasked secret file{tag}: {p}"] = _sig(p, st)
                if (fn.lower().endswith(DATA_SUFFIXES) and st.st_size >= limit and not is_covered(p)
                        and not data_trusted(p)):
                    found[f"live data ({st.st_size >> 20} MB): {p}"] = _sig(p, st)
            for fn in filenames:
                if fn in mounts.COMPOSE_FILES and not os.path.islink(os.path.join(dirpath, fn)):
                    cf = os.path.join(dirpath, fn)
                    for b in _compose_data_binds(cf, grant):
                        if not is_covered(b) and not data_trusted(b):
                            try:
                                sig = _sig(b, os.lstat(b))
                            except OSError:
                                sig = f"compose:{b}"
                            found[f"compose data bind: {b} (bound by {cf})"] = sig

    for grant in grants:
        walk(grant, found)
    for grant in warn_grants:
        walk(grant, ro_found)
    for text in ro_found:
        warn.add(f"in a read-only data grant (allowed, since read-only, but worth knowing): {text}")
    for grant in readable_grants:
        walk(grant, readable)
    for text, sig in readable.items():
        if text.startswith(READABLE_REFUSED):
            found[text] = sig            # readable is enough to leak it
        else:
            warn.add(f"in the read-only project root (it cannot be changed from the session): {text}")
    return [{"text": t, "sig": g} for t, g in sorted(found.items())], sorted(warn)


def old_preflight(settings, name, entry, *, plan=None, code=None):
    """The one grant scan (audit F-6). `ab session start`, `ab project check` and add/set validation all come here, so
    the verdict `check` prints is the verdict `run` reaches. Returns (findings, warnings, fingerprint); the
    fingerprint is "" when there is nothing to accept.

    `plan` is the session's already-built mount plan, passed by `ab session start` so there is no second plan and no
    chance of divergence. Without one, the plan is built from the registry entry exactly as a session would
    build it: `compose` is an effective setting (a preset can set it), while `mask_skip` and `swap` are the
    project's own (see projectkeys.KEYS).
    """
    from agentbox import presets
    if plan is None or code is None:
        eff = presets.effective(name, dict(entry, name=name), presets.load_presets(settings.presets_dir))
        code = eff["code"][0] if code is None else code
    data_env, data_bind = entry.get("data_env") == "on", entry.get("data_bind") == "on"
    if plan is None:
        plan = old_plan(root=entry["root"], data_ro=entry.get("data_ro", []),
                           data_rw=entry.get("data_rw", []), compose=eff["compose"][0],
                           mask_skip=entry.get("mask_skip", []),
                           swap=mounts.effective_swap(entry.get("swap")), mask=entry.get("mask", []), code=code,
                           data_env=data_env)
    # A read-only root (code = ro) is scanned as a READABLE grant: what can leave through the model API (an
    # unmasked secret, a credential in a git remote) still refuses; what could only be damaged by a write
    # (live data, compose data binds, git config escapes) is reported as a warning.
    ro_root = code == "ro"
    findings, warnings = old_scan(entry["root"], ([] if ro_root else [entry["root"]]) + list(entry.get("data_rw", [])),
                              entry.get("mask_skip", []), covered=mounts.hidden(plan),
                              warn_grants=entry.get("data_ro", []),
                              readable_grants=[entry["root"]] if ro_root else [],
                              env_ok=[entry["root"]] if data_env else [],
                              data_ok=[mounts.data_dir(entry["root"])] if data_bind else [])
    return findings, warnings, (fingerprint(findings) if findings else "")



# ---- the tree ------------------------------------------------------------------------------------------------

def git(*args):
    subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "protocol.file.allow=always", *args], check=True, capture_output=True)


class VariedTree(TempCase):
    def setUp(self):
        super().setUp()
        t = self.tmp
        self.root = root = self.mkdir("proj")
        self.outside = self.mkdir("outside")
        self.rw = self.mkdir("rwgrant")
        self.ro = self.mkdir("rogrant")
        w = self.write
        # the root repo, a commit, a worktree inside the grant (a .git file + commondir), a credential remote
        git("init", "-q", root)
        w("proj/app/main.py", "code\n")
        git("-C", root, "add", "-A")
        git("-C", root, "commit", "-qm", "init")
        git("-C", root, "remote", "add", "origin", "https://user:pw@example.invalid/r.git")
        git("-C", root, "worktree", "add", "-q", os.path.join(root, "wt"))
        # a submodule-style git dir under .git/modules, nested one level more
        git("init", "-q", "--bare", os.path.join(root, ".git", "modules", "libx"))
        git("init", "-q", "--bare", os.path.join(root, ".git", "modules", "libx", "modules", "liby"))
        # nested repo, bare repo, separate git dirs inside and outside the grant
        git("init", "-q", os.path.join(root, "sub", "inner"))
        git("init", "-q", "--bare", os.path.join(root, "bare.git"))
        os.makedirs(os.path.join(root, "gitdirs"))                         # git wants the parent to exist
        git("init", "-q", f"--separate-git-dir={os.path.join(root, 'gitdirs', 'sep')}", os.path.join(root, "sepwork"))
        git("init", "-q", f"--separate-git-dir={os.path.join(self.outside, 'gd')}", os.path.join(root, "outwork"))
        w("proj/notagit/.git", "not a gitdir line\n")                      # a .git file that is no pointer
        # a git dir with a commondir (outside the grant), reached through a .git file
        git("init", "-q", "--bare", os.path.join(root, "gitdirs", "withcommon"))
        git("init", "-q", "--bare", os.path.join(self.outside, "common"))
        w("proj/gitdirs/withcommon/commondir", "../../../outside/common\n")
        w("proj/cwork/.git", "gitdir: ../gitdirs/withcommon\n")                      # a .git file that is no pointer
        # WALK_SKIP dirs holding repos and secrets
        git("init", "-q", os.path.join(root, "node_modules", "pkg"))
        for rel in ("node_modules/pkg/.env", ".venv/.env", "venv/x.pem", "__pycache__/a.key", "sub/__pycache__/.netrc"):
            w(f"proj/{rel}", "S=1\n")
        # secrets at depth, empty ones, exempt names, a dev swap and its edge cases
        for rel in (".env", ".env.local", "deep/a/b/c/.env.production", "tls/x.key", "conf/prod.env", ".netrc",
                    "a/b/store.p12", "keys/id.jks", "keys/k.pfx", "conf/.env.example", "conf/.env.sample",
                    "conf/.env.template", "example.env"):
            w(f"proj/{rel}", "SECRET=1\n")
        w("proj/.pgpass", "")                                               # empty: masked, not a finding
        w("proj/.env.dev", "DEV=1\n")
        w("proj/sub/.env.dev", "DEV=1\n")                                   # no .env: created
        w("proj/weird/.env.dev", "DEV=1\n")
        os.makedirs(os.path.join(root, "weird", ".env"))                   # .env a directory: noted
        w("proj/lsrc/real.dev", "DEV=1\n")
        os.symlink("real.dev", os.path.join(root, "lsrc", ".env.dev"))     # a symlinked swap source: not used
        os.mkfifo(os.path.join(root, "fifo.pem"))                          # a masked name that is no file
        # secrets/ dirs (whole-directory masks), with a repo and a hard link inside
        w("proj/secrets/db.txt", "pw\n")
        w("proj/secrets/inner/.env", "S=1\n")
        git("init", "-q", os.path.join(root, "secrets", "repo"))
        w("proj/sub/secrets/token", "t\n")
        # symlinks: to dirs inside and outside, to files, dangling, a .git symlink, a symlinked compose file
        w("outside/.env", "S=1\n")
        w("outside/big.db", "x")
        os.symlink(os.path.join(root, "sub"), os.path.join(root, "linkdir"))
        os.symlink(self.outside, os.path.join(root, "linkout"))
        os.symlink("/etc/hostname", os.path.join(root, "link.pem"))
        os.symlink("/nonexistent/x", os.path.join(root, "dangling.key"))
        os.symlink("/nonexistent/d", os.path.join(root, "data"))            # a dangling link NAMED like a mask
        # links to real directories named like a mask dir and a project mask: never a tmpfs cover
        os.symlink(os.path.join(root, "sub", "inner"), os.path.join(root, "app", "secrets"))
        os.symlink(os.path.join(root, "private"), os.path.join(root, "app", "data"))
        os.symlink(os.path.join(root, "sub", "inner", ".git"), os.path.join(root, "app", ".git"))
        w("proj/svc/compose.yaml", "services:\n  db:\n    volumes:\n      - ./pgdata:/var/lib/pg\n"
          "      - type: bind\n        source: ../dump\n")
        w("proj/svc/pgdata/x.sqlite", "x")
        os.symlink(os.path.join(root, "svc", "compose.yaml"), os.path.join(root, "compose.yml"))
        w("proj/compose.yaml", "services: {}\n")
        # a project mask by name and by path; live data in and out of it; a big sparse db
        w("proj/sub/data/.env", "S=1\n")
        w("proj/private/notes.txt", "n\n")
        for rel in ("dump/big.db", "sub/data/big.sqlite", "private/huge.duckdb"):
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.truncate(2 << 20)
        w("proj/dump/small.db", "x")
        # hard links: to the swapped .env, to a file under secrets/, to a plain mask
        os.link(os.path.join(root, ".env"), os.path.join(root, "notes.txt"))
        os.link(os.path.join(root, "secrets", "db.txt"), os.path.join(root, "app", "db-copy.txt"))
        os.link(os.path.join(root, "tls", "x.key"), os.path.join(root, "deep", "k.bak"))
        # a mask_skip folder with a repo and a secret
        git("init", "-q", os.path.join(root, "big", "repo"))
        w("proj/big/.env", "S=1\n")
        # a nested read-write data grant inside the root
        self.nested = os.path.join(root, "nested")
        w("proj/nested/.env", "S=1\n")
        git("init", "-q", self.nested)
        # the other grants
        git("init", "-q", self.rw)
        w("rwgrant/.env", "S=1\n")
        w("rwgrant/.env.dev", "D=1\n")
        w("rwgrant/secrets/x", "x\n")
        git("init", "-q", self.ro)
        w("rogrant/.env", "S=1\n")
        w("rogrant/sub/.env.dev", "D=1\n")
        with open(os.path.join(self.ro, "live.db"), "wb") as f:
            f.truncate(3 << 20)
        git("config", "--file", os.path.join(self.ro, ".git", "config"), "core.hooksPath", os.path.join(root, "hooks"))
        # a directory the walk cannot list (the scan warns, the plan's walks pass it by)
        self.locked = os.path.join(root, "locked")
        w("proj/locked/.env", "S=1\n")
        git("init", "-q", os.path.join(self.locked, "repo"))
        os.chmod(self.locked, 0)
        self.addCleanup(os.chmod, self.locked, 0o700)
        self.unreadable = not os.access(self.locked, os.R_OK)        # root reads it anyway
        self.noexec = os.path.join(root, "noexec")

    def make_noexec(self):
        """A directory that can be listed but not entered: every lstat in it fails, so hardlink_masks
        refuses the plan (fail closed) and the scan warns. Only for the tests about that."""
        self.write("proj/noexec/.env", "S=1\n")
        self.write("proj/noexec/inner/x.pem", "k\n")
        os.symlink("/etc/hostname", os.path.join(self.noexec, "l.key"))
        os.chmod(self.noexec, stat.S_IRUSR)
        self.addCleanup(os.chmod, self.noexec, 0o700)
        if os.access(os.path.join(self.noexec, ".env"), os.R_OK):
            self.skipTest("running as root: the directory stays enterable")

    # Every configuration a session can plan, from the plain one to each grant and setting
    CONFIGS = [
        {},
        {"mask_skip": ["big"]},
        {"mask_skip": ["big", "sub/inner"], "compose": "ro"},
        {"mask": ["data", "./private", "notes.txt", "./sub/data/big.sqlite", "./future/x"]},
        {"swap": {".env": ".env.dev", ".env.local": ".env.dev"}},
        {"data_rw": ["RW", "NESTED"], "data_ro": ["RO"]},
        {"data_rw": ["RW"], "data_ro": ["RO"], "code": "ro", "compose": "ro"},
        {"data_env": True, "data_rw": ["NESTED"], "mask": ["data"]},
    ]

    def kwargs(self, cfg):
        sub = {"RW": self.rw, "RO": self.ro, "NESTED": self.nested}
        kw = dict(cfg, root=self.root)
        for k in ("data_rw", "data_ro"):
            if k in kw:
                kw[k] = [sub[x] for x in kw[k]]
        kw["swap"] = effective_swap(kw.get("swap"))
        return kw

    def assertPlansEqual(self, new, old, msg=None):
        self.assertEqual(new.mounts, old.mounts, msg)
        self.assertEqual(new.create_dirs, old.create_dirs, msg)
        self.assertEqual(new.create_files, old.create_files, msg)
        self.assertEqual(new.notes, old.notes, msg)


class TheTreeIsVaried(VariedTree):
    """The fixture really hits the cases it is for, so an equal answer is not an empty one."""

    def test_the_fixture_exercises_each_case(self):
        found, outside = old_find_git_dirs(self.root)
        names = {os.path.relpath(g, self.root) for g in found}
        for want in (".git", "sub/inner/.git", "bare.git", "gitdirs/sep", "gitdirs/withcommon", "../outside/common",
                     "../outside/gd", ".git/modules/libx",
                     ".git/modules/libx/modules/liby", "big/repo/.git", "secrets/repo/.git", "nested/.git"):
            self.assertIn(want, names)
        self.assertEqual([c for c, _ in outside], [os.path.join(self.root, "outwork")])
        p = old_plan(**self.kwargs({"mask": ["data", "./private"], "mask_skip": ["big"]}))
        kinds = {os.path.relpath(m.dst, self.root): m.kind for m in p.mounts}
        self.assertEqual(kinds["notes.txt"], "mask", "hard link to the swapped .env")
        self.assertEqual(kinds["app/db-copy.txt"], "mask", "hard link to a file under secrets/")
        self.assertEqual(kinds["sub/data"], "tmpfs")
        self.assertTrue(any("not a regular file" in n for n in p.notes))
        findings, warnings = old_scan(self.root, [self.root], ["big"], covered=mounts.hidden(p))
        text = "\n".join(f["text"] for f in findings) + "\n".join(warnings)
        for want in ("git credentials", "live data", "compose data bind", "unmasked secret file (under mask_skip"):
            self.assertIn(want, text)
        if self.unreadable:
            self.assertIn("unreadable", text)
            self.assertIn(self.locked, text)


class SameAnswers(VariedTree):
    def test_find_git_dirs(self):
        for grant in (self.root, self.rw, self.ro, self.nested, os.path.join(self.root, "secrets")):
            for skips in ([], ["big"], ["big", "sub", "nope"]):
                rel = _rel_skips(self.root, grant, skips)
                self.assertEqual(mounts.find_git_dirs(grant, rel), old_find_git_dirs(grant, rel), (grant, skips))

    def test_find_git_dirs_refuses_the_same_way(self):
        gitfile = os.path.join(self.root, "sub", "badgit", ".git")
        os.makedirs(os.path.dirname(gitfile))
        with open(gitfile, "w") as f:
            f.write("gitdir: x\n")
        os.chmod(gitfile, 0)
        self.addCleanup(os.chmod, gitfile, 0o600)
        if os.access(gitfile, os.R_OK):
            self.skipTest("running as root: the file stays readable")
        errors = []
        for fn in (mounts.find_git_dirs, old_find_git_dirs):
            with self.assertRaises(AgentboxError) as cm:
                fn(self.root)
            errors.append(str(cm.exception))
        self.assertEqual(errors[0], errors[1])

    def test_secret_mounts(self):
        for grant in (self.root, self.rw, self.ro):
            for ro in (False, True):
                for real_env in (False, True):
                    for swap in (None, {".env.local": ".env.dev"}):
                        for skips in ([], ["big"]):
                            rel = _rel_skips(self.root, grant, skips)
                            kw = dict(ro=ro, skip_rel=rel, swap=swap, real_env=real_env)
                            nn, on = [], []
                            new = mounts.secret_mounts(grant, notes=nn, **kw)
                            old = old_secret_mounts(grant, notes=on, **kw)
                            self.assertEqual((new, nn), (old, on), (grant, kw))

    def test_user_masks_and_compose_overlays(self):
        for mask in ([], ["data", "notes.txt", "secrets"], ["./private", "./sub/data", "./future", "./."]):
            for skips in ([], ["big"]):
                rel = _rel_skips(self.root, self.root, skips)
                nn, on = [], []
                self.assertEqual(
                    (mounts.user_masks(self.root, root=self.root, mask=mask, skip_rel=rel, notes=nn), nn),
                    (old_user_masks(self.root, root=self.root, mask=mask, skip_rel=rel, notes=on), on), mask)
                self.assertEqual(mounts.compose_overlays(self.root, rel), old_compose_overlays(self.root, rel))

    def test_hardlink_masks(self):
        grants = [self.root, self.rw, self.ro]
        for skips in ([], ["big", "app"]):
            ms = []
            for g in grants:
                ms += old_secret_mounts(g, ro=g == self.ro, skip_rel=_rel_skips(self.root, g, skips))[0]
                ms += old_user_masks(g, root=self.root, mask=["data", "./private"],
                                     skip_rel=_rel_skips(self.root, g, skips))[0]
            nn, on = [], []
            new = mounts.hardlink_masks(ms, grants, self.root, skips, nn)
            old = old_hardlink_masks(ms, grants, self.root, skips, on)
            self.assertTrue(old, "the fixture has hard links to covered files")
            self.assertEqual((new, nn), (old, on), skips)

    def test_the_plan(self):
        for cfg in self.CONFIGS:
            kw = self.kwargs(cfg)
            self.assertPlansEqual(mounts.plan(**kw), old_plan(**kw), cfg)

    def test_the_scan(self):
        for cfg in self.CONFIGS:
            kw = self.kwargs(cfg)
            new_plan, old_p = mounts.plan(**kw), old_plan(**kw)
            ro_root = kw.get("code") == "ro"
            rw = ([] if ro_root else [self.root]) + kw.get("data_rw", [])
            args = (self.root, rw, kw.get("mask_skip", []))
            for extra in ({}, {"env_ok": [self.root]}, {"data_ok": [os.path.join(self.root, "dump")]}):
                skw = dict(warn_grants=kw.get("data_ro", []), readable_grants=[self.root] if ro_root else [],
                           **extra)
                old = old_scan(*args, covered=mounts.hidden(old_p), **skw)
                self.assertEqual(grantcheck.scan(*args, covered=mounts.hidden(new_plan), tree=new_plan.tree, **skw),
                                 old, (cfg, extra))
                self.assertEqual(grantcheck.scan(*args, covered=mounts.hidden(new_plan), **skw), old,
                                 f"{cfg} {extra}: a fresh tree")

    def test_the_scan_of_foreign_owned_directories(self):
        """Every directory then looks foreign: warned and not descended, the same ones both ways."""
        p = old_plan(**self.kwargs({}))
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            new = grantcheck.scan(self.root, [self.root], covered=mounts.hidden(p))
            old = old_scan(self.root, [self.root], covered=mounts.hidden(p))
        self.assertTrue(any("owned by uid" in w for w in old[1]))
        self.assertEqual(new, old)

    def test_the_scan_from_a_grant_named_through_a_symlink(self):
        """The cover test skips the per-directory realpath only when the grant is already a real path."""
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.root, alias)
        p = old_plan(**self.kwargs({"mask": ["data", "./private"]}))
        for grant in (alias, self.root + "/"):
            self.assertEqual(grantcheck.scan(alias, [grant], covered=mounts.hidden(p)),
                             old_scan(alias, [grant], covered=mounts.hidden(p)), grant)

    def test_is_covered_matches_inside_on_odd_cover_lists(self):
        """Covers at the top, nested in each other, sharing a prefix that is not a path prefix, and `/`."""
        tops = [os.path.join(self.root, x) for x in ("sub", "sub/inner", "su", "deep/a", "app/main.py")]
        for covered in (tops, ["/"], [self.root], [os.path.join(self.root, "dump", "big.db")]):
            self.assertEqual(grantcheck.scan(self.root, [self.root], covered=covered),
                             old_scan(self.root, [self.root], covered=covered), covered)

    def test_preflight_with_the_sessions_plan(self):
        for cfg in self.CONFIGS:
            kw = self.kwargs(cfg)
            entry = {"root": self.root, "data_ro": kw.get("data_ro", []), "data_rw": kw.get("data_rw", []),
                     "mask_skip": kw.get("mask_skip", []), "swap": cfg.get("swap"), "mask": kw.get("mask", []),
                     "data_env": "on" if kw.get("data_env") else "off", "data_bind": "on"}
            code = kw.get("code", "rw")
            new_plan = mounts.plan(**kw)
            self.assertIsNotNone(new_plan.tree)
            new = grantcheck.preflight(None, "p", entry, plan=new_plan, code=code)
            self.assertIsNone(new_plan.tree, "the listings are dropped once the scan has used them")
            self.assertEqual(new, old_preflight(None, "p", entry, plan=old_plan(**kw), code=code), cfg)


class ListableButNotEnterable(VariedTree):
    def setUp(self):
        super().setUp()
        self.make_noexec()

    def test_the_plan_refuses_the_same_way(self):
        for cfg in self.CONFIGS:
            errors = []
            for fn in (mounts.plan, old_plan):
                with self.assertRaises(AgentboxError) as cm:
                    fn(**self.kwargs(cfg))
                errors.append(str(cm.exception))
            self.assertEqual(errors[0], errors[1], cfg)
            self.assertIn(self.noexec, errors[0])

    def test_but_not_where_mask_skip_leaves_it_out(self):
        kw = self.kwargs({"mask_skip": ["noexec"]})
        self.assertPlansEqual(mounts.plan(**kw), old_plan(**kw))

    def test_the_scan_warns_the_same_way(self):
        p = old_plan(**self.kwargs({"mask_skip": ["noexec"]}))
        new = grantcheck.scan(self.root, [self.root], covered=mounts.hidden(p))
        old = old_scan(self.root, [self.root], covered=mounts.hidden(p))
        self.assertIn(os.path.join(self.noexec, ".env"), "\n".join(old[1]))
        self.assertEqual(new, old)

    def test_the_walks_see_the_same_listing(self):
        for fn in (mounts.find_git_dirs, old_find_git_dirs):
            fn(self.root)                        # no refusal: nothing in it is a .git file to read
        self.assertEqual(mounts.find_git_dirs(self.root), old_find_git_dirs(self.root))
        self.assertEqual(mounts.secret_mounts(self.root, ro=False), old_secret_mounts(self.root, ro=False))


class PreflightBuildsItsOwnPlan(SettingsCase, VariedTree):
    def test_preflight_without_a_plan(self):
        for extra in ({}, {"mask_skip": ["big"], "mask": ["data"]}, {"data_rw": [self.rw], "data_ro": [self.ro]},
                      {"data_env": "on", "data_bind": "on"}):
            entry = {"root": self.root, "preset": "script", **extra}
            self.assertEqual(grantcheck.preflight(self.settings, "p", entry),
                             old_preflight(self.settings, "p", entry), extra)


class OneReadPerDirectory(VariedTree):
    """The point of the change: the plan and the scan read each directory once between them."""

    def test_plan_then_scan_lists_each_directory_once(self):
        reads = []
        real = mounts.Tree._read

        def counting(top):
            reads.append(top)
            return real(top)

        # (grantcheck's _looks_like_data still walks a compose-bound path on its own: not a grant walk)
        with mock.patch.object(mounts.Tree, "_read", side_effect=counting):
            p = mounts.plan(root=self.root, mask=["data"])
            grantcheck.preflight(None, "p", {"root": self.root, "mask": ["data"]}, plan=p, code="rw")
        repeated = sorted({r for r in reads if reads.count(r) > 1})
        self.assertEqual(repeated, [], "a directory read twice")
        self.assertIn(os.path.join(self.root, "src") if os.path.isdir(os.path.join(self.root, "src"))
                      else os.path.join(self.root, "app"), reads)

    def test_a_listing_is_os_walks_split(self):
        tree = mounts.Tree()
        for top in (self.root, self.locked, self.noexec, os.path.join(self.root, "nope")):
            got = [(d, sorted(ds), sorted(fs)) for d, ds, fs in tree.walk(top, onerror=lambda e: None)]
            want = [(d, sorted(ds), sorted(fs)) for d, ds, fs in os.walk(top, followlinks=False)]
            self.assertEqual(got, want, top)
            errs_new, errs_old = [], []
            list(tree.walk(top, onerror=errs_new.append))
            list(os.walk(top, onerror=errs_old.append))
            self.assertEqual([(e.errno, e.filename) for e in errs_new], [(e.errno, e.filename) for e in errs_old])


if __name__ == "__main__":
    unittest.main()
