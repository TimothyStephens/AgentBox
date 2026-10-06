"""Track 1 (Tim, 6 Oct): the grant is for code; secrets and live data live outside it. This is the primary
rule — the masks and the gate's cover checks are defence in depth behind it.

Before a session starts, each grant root is scanned (skipping masked/excluded sub-dirs) for things that should
not be in a grant the agent gets read-write:
  * a non-empty `.env` / `.env.*` / `*.pem` / `*.key`, or any file under a `secrets/` directory, that the masks
    do not cover (an un-masked secret the agent could read);
  * a `.git` whose remote URLs carry credentials (`https://user:pass@host`);
  * files that look like live data: `*.db` / `*.sqlite` / `*.sqlite3` / `*.duckdb` over LIVE_DATA_MB, and any
    host path a compose file in the grant binds as a volume (a live data directory).

If any are found, the session refuses to start and names them. `ab project add --i-understand-grant-exposure`,
or `ab project accept-grant-exposure <name>`, records an acknowledgement per project (like history secrets);
the ack is tied to what was found, so a new exposure re-prompts.
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


def _rel_skips(root, grant, mask_skip):
    out = set()
    for s in mask_skip:
        full = os.path.normpath(s if os.path.isabs(s) else os.path.join(root, s))
        if full == grant or full.startswith(grant.rstrip("/") + "/"):
            out.add(os.path.relpath(full, grant))
    return out


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


def scan(root, grants, mask_skip=(), covered=()):
    """Return a sorted list of findings: each 'kind: path (detail)'. `covered` are paths the masks already
    hide (an empty cover makes a secret-named file safe)."""
    covered = {os.path.realpath(c) for c in covered}
    limit = LIVE_DATA_MB << 20

    def is_covered(p):
        return any(p == c or p.startswith(c.rstrip("/") + "/") for c in covered)

    found = set()
    for grant in grants:
        skip = _rel_skips(root, grant, mask_skip)
        for dirpath, dirnames, filenames in os.walk(grant, followlinks=False):
            rel = os.path.relpath(dirpath, grant)
            # a covered (masked, empty) directory hides everything under it; don't descend into it
            dirnames[:] = [d for d in dirnames if d not in WALK_SKIP and not os.path.islink(os.path.join(dirpath, d))
                           and os.path.normpath(os.path.join(rel, d)) not in skip
                           and not is_covered(os.path.realpath(os.path.join(dirpath, d)))]
            gd = os.path.join(dirpath, ".git")
            if os.path.isdir(gd):
                for c in _git_credential_urls(gd):
                    found.add(f"git credentials in a remote URL: {c}")
            in_secrets = any(part in mounts.MASK_DIRS for part in rel.split(os.sep))
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if _is_secret_name(fn, in_secrets) and st.st_size and not is_covered(p):
                    found.add(f"unmasked secret file: {p}")
                if fn.lower().endswith(DATA_SUFFIXES) and st.st_size >= limit and not is_covered(p):
                    found.add(f"live data ({st.st_size >> 20} MB): {p}")
            for fn in filenames:
                if fn in mounts.COMPOSE_FILES and not os.path.islink(os.path.join(dirpath, fn)):
                    for b in _compose_data_binds(os.path.join(dirpath, fn), grant):
                        if not is_covered(b):
                            found.add(f"compose data bind: {b} (bound by {os.path.join(dirpath, fn)})")
    return sorted(found)


def fingerprint(findings):
    """A stable id for a set of findings, so an ack covers exactly what was seen."""
    return hashlib.sha256("\n".join(sorted(findings)).encode()).hexdigest()[:16]


def message(name, findings):
    lines = [f"project {name}: the grant holds secrets or live data the agent would get read-write. A grant is "
             "for code; move these outside it, or add a mask (mask_skip excludes a path; a secret under a "
             "masked name is covered). Found:"]
    lines += [f"  - {f}" for f in findings]
    lines.append(f"To run anyway, recording that you accept this exposure: "
                 f"`ab project accept-grant-exposure {name}` (or `--i-understand-grant-exposure` at add time).")
    return "\n".join(lines)
