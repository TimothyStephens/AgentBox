"""Track 1 (Tim, 6 Oct): the grant is for code; secrets and live data live outside it. This is the primary
rule — the masks and the gate's cover checks are defence in depth behind it.

Before a session starts, each grant root is scanned for things that should not be in a grant the agent gets
read-write:
  * a non-empty `.env` / `.env.*` / `*.pem` / `*.key`, or any file under a `secrets/` directory, that the masks
    do not cover (an un-masked secret the agent could read);
  * a `.git` whose remote URLs carry credentials (`https://user:pass@host`);
  * files that look like live data: `*.db` / `*.sqlite` / `*.sqlite3` / `*.duckdb` over LIVE_DATA_MB, and any
    host path a compose file in the grant binds as a volume holding such data.

If any are found, the session refuses to start and names them. `ab project add --i-understand-grant-exposure`,
or `ab project accept-grant-exposure <name>`, records an acknowledgement per project (like history secrets);
the ack is over each finding's content, so a change to the file's content (not only its path) re-prompts.

This is a safety net under the design's threat model (mistake containment, not a wall against a malicious
agent); it catches the common shapes, not every one. Scanning covers what it can read: a directory owned by
another user (a rootless container's data dir) can't be read and is reported as a warning, not descended into.
Known limitations (secrets under other names, more data shapes) are in `docs/decisions.md`.
"""
import fnmatch
import hashlib
import os
import re
import stat

from . import mounts

LIVE_DATA_MB = 1
SECRET_FILES = (".env", ".env.*", "*.pem", "*.key")
DATA_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".duckdb")
CRED_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")     # scheme://user:password@host
WALK_SKIP = set(mounts.WALK_SKIP) - {".git"}                            # .git is scanned for credential URLs


def _is_secret_name(fn, in_secrets):
    return in_secrets or (fn not in mounts.MASK_EXEMPT
                          and any(fnmatch.fnmatchcase(fn, p) for p in SECRET_FILES))


def _git_credential_urls(git_dir):
    for name in ("config", "config.worktree"):
        p = os.path.join(git_dir, name)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if CRED_URL.search(text):
            yield p


def _compose_data_binds(path, grant):
    """Host paths a compose file binds as volumes (short `./x:/y` or long `source:`), resolved under the
    compose file's directory, that exist and are inside the grant. A best-effort text scan: no YAML parse,
    since a bind here is a finding regardless of the rest of the file."""
    base = os.path.dirname(path)
    out = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip().lstrip("-").strip().strip('"').strip("'")
        src = None
        if s.startswith(("./", "../", "/", "~")) and ":" in s:
            src = s.split(":", 1)[0]
        elif s.startswith("source:"):
            src = s[len("source:"):].strip().strip('"').strip("'")
        if not src or src.startswith("$"):
            continue
        real = os.path.realpath(os.path.join(base, os.path.expanduser(src)))
        inside = real == grant or real.startswith(grant.rstrip("/") + "/")
        if os.path.exists(real) and inside and _looks_like_data(real):
            out.add(real)
    return out


def _looks_like_data(path):
    """A compose-bound path that is, or contains, a data file (so a code bind like ./app is not flagged)."""
    if os.path.isfile(path):
        return path.lower().endswith(DATA_SUFFIXES)
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in WALK_SKIP]
        if any(f.lower().endswith(DATA_SUFFIXES) for f in filenames):
            return True
    return False


def _sig(path, st):
    """A content signature for a file finding: path plus size, mtime and inode, so the ack re-prompts when the
    file's content changes, not only when its path does (audit 6 Oct, M4)."""
    return f"{path}|{st.st_size}|{st.st_mtime_ns}|{st.st_ino}"


def _under_mask_skip(root, path, mask_skip):
    for s in mask_skip:
        full = os.path.normpath(s if os.path.isabs(s) else os.path.join(root, s))
        if path == full or path.startswith(full.rstrip("/") + "/"):
            return True
    return False


def scan(root, grants, mask_skip=(), covered=(), warn_grants=()):
    """Return (findings, warnings). Each finding is {"text": <shown>, "sig": <for the fingerprint>}; a grant
    with any finding refuses to start until acknowledged. Warnings are shown but do not block (audit 6 Oct,
    the cheap half of H1: entries the scanner can't read or that another user owns). `mask_skip` is NOT
    honoured here (H3): it is a speed knob for the mask walk, not an exemption; a secret under it is unmasked,
    so it must still be reported. `covered` are paths the masks hide (an empty cover makes a secret safe).

    `warn_grants` are the read-only data grants. They are scanned exactly the same way, but their findings
    come back as warnings (F4): a read-only grant holding live data is the declared purpose of the
    `analysis-offline` and `operate` presets, so it is reported, never refused. Before this they were not
    scanned at all, so what was in them was invisible."""
    covered = {os.path.realpath(c) for c in covered}
    uid = os.getuid()
    limit = LIVE_DATA_MB << 20

    def is_covered(p):
        return any(p == c or p.startswith(c.rstrip("/") + "/") for c in covered)

    found, ro_found, warn = {}, {}, set()

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
                if _is_secret_name(fn, in_secrets) and st.st_size and not is_covered(p):
                    tag = " (under mask_skip: unmasked)" if _under_mask_skip(root, p, mask_skip) else ""
                    found[f"unmasked secret file{tag}: {p}"] = _sig(p, st)
                if fn.lower().endswith(DATA_SUFFIXES) and st.st_size >= limit and not is_covered(p):
                    found[f"live data ({st.st_size >> 20} MB): {p}"] = _sig(p, st)
            for fn in filenames:
                if fn in mounts.COMPOSE_FILES and not os.path.islink(os.path.join(dirpath, fn)):
                    cf = os.path.join(dirpath, fn)
                    for b in _compose_data_binds(cf, grant):
                        if not is_covered(b):
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
    return [{"text": t, "sig": g} for t, g in sorted(found.items())], sorted(warn)


def preflight(settings, name, entry, *, plan=None):
    """The one grant scan (audit F-6). `ab run`, `ab project check` and add/set validation all come here, so
    the verdict `check` prints is the verdict `run` reaches. Returns (findings, warnings, fingerprint); the
    fingerprint is "" when there is nothing to accept.

    `plan` is the session's already-built mount plan, passed by `ab run` so there is no second plan and no
    chance of divergence. Without one, the plan is built from the registry entry exactly as a session would
    build it: `compose` is an effective setting (a preset can set it), while `mask_skip` and `swap` are the
    project's own (see projectkeys.KEYS).
    """
    from . import presets
    if plan is None:
        eff = presets.effective(name, dict(entry, name=name), presets.load_presets(settings.presets_dir))
        plan = mounts.plan(root=entry["root"], data_ro=entry.get("data_ro", []),
                           data_rw=entry.get("data_rw", []), compose=eff["compose"][0],
                           mask_skip=entry.get("mask_skip", []),
                           swap=mounts.effective_swap(entry.get("swap")), mask=entry.get("mask", []))
    findings, warnings = scan(entry["root"], [entry["root"]] + list(entry.get("data_rw", [])),
                              entry.get("mask_skip", []), covered=mounts.hidden(plan),
                              warn_grants=entry.get("data_ro", []))
    return findings, warnings, (fingerprint(findings) if findings else "")


def fingerprint(findings):
    """A stable id for a set of findings, over each finding's content signature, so the ack re-prompts when a
    finding's content changes, not just its path (audit 6 Oct, M4). Accepts finding dicts or plain strings."""
    sigs = [(f["sig"] if isinstance(f, dict) else f) for f in findings]
    return hashlib.sha256("\n".join(sorted(sigs)).encode()).hexdigest()[:16]


def sig_path(sig):
    """The path a finding's signature is about. Signatures are `<path>|size|mtime|inode`, or `gitcred:<path>`
    / `compose:<path>` where there is no file to stat."""
    sig = str(sig)
    for prefix in ("gitcred:", "compose:"):
        if sig.startswith(prefix):
            return sig[len(prefix):]
    return sig.split("|", 1)[0]


def accepted_sigs(value):
    """The finding signatures a stored acknowledgement covers. An acknowledgement written before they were
    recorded holds only the fingerprint (a bare string), so there is nothing to compare finding by finding:
    () means "not known", not "nothing was accepted" — check `acceptance_fingerprint` for that."""
    return tuple(str(s) for s in value) if isinstance(value, list) else ()


def acceptance_fingerprint(value):
    """The short id for a stored acknowledgement, whichever shape it is in. This is what `ab project show`
    prints and what `ab project check` compares, so the display is unchanged by the stored shape."""
    if isinstance(value, list):
        return fingerprint(value) if value else ""
    return str(value or "")


def matches(value, findings):
    """Does a stored acknowledgement describe exactly these findings?"""
    return acceptance_fingerprint(value) == (fingerprint(findings) if findings else "")


def compare(value, findings):
    """(new, gone, changed) between what was accepted and what the scan finds now -- `new` and `changed` as
    finding texts, `gone` as paths (the text of a finding that no longer exists is not recorded). None when
    the acknowledgement holds only a fingerprint, so no comparison is possible."""
    sigs = set(accepted_sigs(value))
    if not sigs:
        return None
    by_path = {}
    for s in sigs:
        by_path.setdefault(sig_path(s), set()).add(s)
    new, changed, seen = [], [], set()
    for f in findings:
        sig = f["sig"] if isinstance(f, dict) else f
        text = f["text"] if isinstance(f, dict) else f
        seen.add(sig_path(sig))
        if sig in sigs:
            continue
        (changed if sig_path(sig) in by_path else new).append(text)
    gone = sorted(p for p in by_path if p not in seen)
    return new, gone, changed


def _diff_lines(value, findings):
    if not value:
        return [f"  not accepted: {f['text'] if isinstance(f, dict) else f}" for f in findings]
    diff = compare(value, findings)
    if diff is None:
        return ["  (the stored acknowledgement records only a fingerprint, so which findings changed cannot "
                "be shown; the next acceptance records them)"]
    new, gone, changed = diff
    lines = []
    for label, items in (("new since the acceptance", new), ("changed since the acceptance", changed)):
        for t in items:
            lines.append(f"  {label}: {t}")
    for p in gone:
        lines.append(f"  no longer in the grant: {p}")
    return lines or ["  (the same findings, in a different order)"]


def change_message(name, findings, accepted, loosenings):
    lines = [f"project {name}: the grant exposure has changed since it was accepted, and this edit grants "
             f"something new, so it is refused."]
    lines += _diff_lines(accepted, findings)
    lines.append("  this edit would also grant: " + "; ".join(loosenings))
    lines.append(f"Accept the new state with `ab project accept-grant-exposure {name}` (or pass "
                 f"--i-understand-grant-exposure here), or move the findings out of the grant. "
                 f"`ab project check {name}` lists everything in it.")
    return "\n".join(lines)


def unblocked_message(name, findings, accepted):
    """The edit grants nothing new, so it is not blocked -- but the exposure is still there and a session
    will still refuse (audit F-5)."""
    lines = [f"ab: project {name}: the grant exposure does not match what was accepted:"]
    lines += _diff_lines(accepted, findings)
    lines.append(f"  this edit grants nothing new, so it proceeds. A SESSION WILL REFUSE to start until the "
                 f"findings are moved out of the grant or accepted with `ab project accept-grant-exposure "
                 f"{name}`.")
    return "\n".join(lines)


def acceptance_message(name, findings, accepted):
    lines = [f"ab: project {name}: accepted {len(findings)} grant finding(s):"]
    lines += [f"  - {f['text'] if isinstance(f, dict) else f}" for f in findings]
    if accepted:
        lines.append("  newly accepted by this command:")
        lines += _diff_lines(accepted, findings)
    lines.append("  move secrets and live data out of the grant when you can.")
    return "\n".join(lines)


def message(name, findings):
    lines = [f"project {name}: the grant holds secrets or live data the agent would get read-write. A grant is "
             "for code; move these out of the grant (for a secret file, a masked name — .env, *.pem, *.key, or "
             "a secrets/ directory — also covers it). Found:"]
    lines += [f"  - {f['text'] if isinstance(f, dict) else f}" for f in findings]
    lines.append(f"To run anyway, recording that you accept this exposure: "
                 f"`ab project accept-grant-exposure {name}` (or `--i-understand-grant-exposure` at add time).")
    return "\n".join(lines)
