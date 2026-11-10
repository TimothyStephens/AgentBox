"""Toulmin integration (gate B): the four `tm` calls around a session, and what each needs.

The contract is docs/agentbox_toulmin_integration.md. Toulmin (`tm`) records analysis runs and lets the
agent, inside the sandbox, draft claims through an MCP tool server that runs OUTSIDE the container as the
owner. agentbox's part: ask `tm session start` for a session, a token, a socket directory, a bridge binary
and two lists; mount the socket directory and the bridge (never the socket file, so a restarted server's
new socket is visible); hand the harness an MCP entry for the bridge with the token in a 0600 file; refuse
anything on the must-not-mount list; `confirm` what it planned (a hash over the audited mount list) before
the container exists; keep `tm session serve` running beside the session; and `end` from every teardown
path with the clone, base and head. Toulmin trusts the report and does not verify it.

Every path Toulmin sends is a host path; every path sent back under `container_paths` is a container path.
`handshake` is 1. Unknown keys are ignored; missing required keys are an error.
"""
import fnmatch
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from .errors import AgentboxError

HANDSHAKE = 1
ROLE = "writer"
SOCKET_DIR_DST = "/run/agentbox/toulmin"       # the socket directory, read-write
BRIDGE_DST = "/run/agentbox/bin/tm-bridge"     # the bridge binary, read-only
TOKEN_NAME = "token"                           # inside the socket directory, in the container only
TOKEN_FILE = "toulmin-token"                   # 0600, in the session run dir; never in --env or argv
CONFIRM_FILE = "toulmin-confirm.json"
END_FILE = "toulmin-end.json"
SERVE_PID = "toulmin-serve.pid"                # in the session run dir: the service dir is removed early in
SERVE_LOG = "toulmin-serve.log"                # teardown, and `end` must run before `serve` is stopped
MCP_NAME = "toulmin"
EXITS = ("agent_exit", "signal", "kill", "gc", "error")
START_KEYS = ("handshake", "session", "token", "bridge", "socket", "mount_socket_dir", "agent_denied",
              "must_not_mount", "project_root", "role")


def tm_path(settings):
    """The `tm` to run: the `toulmin_tm_path` setting, else `tm` on PATH. Absent means the session does not
    start (the contract, 1.5)."""
    given = os.path.expanduser(settings.values.get("toulmin_tm_path") or "")
    if given:
        if not os.access(given, os.X_OK):
            raise AgentboxError(f"toulmin_tm_path = {given!r} is not an executable; the session does not "
                                "start without Toulmin (`--no-toulmin` turns the integration off)")
        return given
    found = shutil.which("tm")
    if not found:
        raise AgentboxError("Toulmin's `tm` is not on PATH and `toulmin_tm_path` is not set; the session does "
                            "not start without it (`ab project set <p> --no-toulmin` turns the integration off)")
    return found


def launcher_version():
    """`agentbox <commit>` from the checkout the launcher runs from, or `agentbox unknown`."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        p = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=10)
        sha = p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"agentbox {sha or 'unknown'}"


def _run(argv, *, timeout=60):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except OSError as e:
        raise AgentboxError(f"cannot run {argv[0]}: {e}")
    except subprocess.TimeoutExpired:
        raise AgentboxError(f"`{' '.join(argv[:3])}` did not answer within {timeout}s")


# ---- 1. start --------------------------------------------------------------------------------------------

def start(settings):
    """`tm session start --role writer --json`: the start message (contract 2.1), checked, plus the `tm`
    used. Everything teardown needs is in here, so it goes into session.json."""
    tm = tm_path(settings)
    p = _run([tm, "session", "start", "--role", ROLE, "--json"])
    if p.returncode != 0:
        raise AgentboxError(f"`tm session start` failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-400:]}")
    try:
        doc = json.loads(p.stdout)
    except ValueError as e:
        raise AgentboxError(f"`tm session start` printed something that is not one JSON object: {e}")
    if not isinstance(doc, dict):
        raise AgentboxError("`tm session start` printed JSON that is not an object")
    missing = [k for k in START_KEYS if k not in doc]
    if missing:
        raise AgentboxError(f"`tm session start` message lacks required key(s): {', '.join(missing)}")
    if doc["handshake"] != HANDSHAKE:
        raise AgentboxError(f"`tm session start`: handshake {doc['handshake']!r}, this launcher speaks {HANDSHAKE}")
    for k in ("session", "token", "bridge", "socket", "mount_socket_dir", "project_root", "role"):
        if not isinstance(doc[k], str) or not doc[k]:
            raise AgentboxError(f"`tm session start`: {k} must be a non-empty string")
    if len(doc["token"]) < 32:
        raise AgentboxError("`tm session start`: the token is shorter than 32 characters")
    for k in ("bridge", "socket", "mount_socket_dir", "project_root"):
        if not doc[k].startswith("/"):
            raise AgentboxError(f"`tm session start`: {k} must be an absolute host path, got {doc[k]!r}")
        if "," in doc[k] or "\n" in doc[k]:
            raise AgentboxError(f"`tm session start`: {k} holds a character a mount option cannot carry")
    for k in ("agent_denied", "must_not_mount"):
        if not isinstance(doc[k], list) or not all(isinstance(x, str) for x in doc[k]):
            raise AgentboxError(f"`tm session start`: {k} must be a list of strings")
    sock_dir = os.path.realpath(doc["mount_socket_dir"])
    if not os.path.isdir(sock_dir):
        raise AgentboxError(f"`tm session start`: mount_socket_dir {doc['mount_socket_dir']} is not a directory")
    if not os.path.realpath(doc["socket"]).startswith(sock_dir.rstrip("/") + "/"):
        raise AgentboxError(f"`tm session start`: socket {doc['socket']} is not under mount_socket_dir")
    if not os.path.isfile(doc["bridge"]) or not os.access(doc["bridge"], os.X_OK):
        raise AgentboxError(f"`tm session start`: bridge {doc['bridge']} is not an executable file")
    return {k: doc[k] for k in START_KEYS} | {"tm": tm}


# ---- what the session mounts and tells the harness ------------------------------------------------------

def write_token(run_dir, token):
    """The token, 0600, in the session run dir. Mounted read-only into the socket directory's container path
    as `token`; never in --env, argv or a log (podman inspect shows env and argv)."""
    path = os.path.join(run_dir, TOKEN_FILE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return path


def socket_container_path(record):
    rel = os.path.relpath(os.path.realpath(record["socket"]), os.path.realpath(record["mount_socket_dir"]))
    return os.path.join(SOCKET_DIR_DST, rel)


def session_mounts(record, token_path):
    """[(host src, container dst, read_only)]: the socket DIRECTORY read-write, the bridge read-only, the
    token read-only inside the socket directory's container path."""
    return [(record["mount_socket_dir"], SOCKET_DIR_DST, False),
            (record["bridge"], BRIDGE_DST, True),
            (token_path, f"{SOCKET_DIR_DST}/{TOKEN_NAME}", True)]


def mcp_server(record):
    """The MCP entry the harness gets: the bridge, the socket's container path, the token from a file."""
    return {"type": "stdio", "command": BRIDGE_DST,
            "args": [socket_container_path(record), "--token-file", f"{SOCKET_DIR_DST}/{TOKEN_NAME}"]}


# ---- must_not_mount --------------------------------------------------------------------------------------

def _is_glob(entry):
    return any(ch in entry for ch in "*?[")


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def violations(must_not_mount, sources, allowed=()):
    """[(source, entry)] for every mount SOURCE that would expose a must-not-mount entry. An entry is an
    absolute host path or a glob. A literal entry is violated by a source that is it, is inside it, or
    contains it. A glob is violated by a source (or an ancestor of it) that matches, or by a source that
    contains the glob's literal prefix (`/tmp` would expose `/tmp/toulmin-1000/*`). `allowed` sources are
    the ones the start message itself asks for (the socket directory)."""
    allowed = {os.path.realpath(a) for a in allowed}
    out = []
    for src in sources:
        real = os.path.realpath(src)
        if real in allowed:
            continue
        for entry in must_not_mount:
            if _is_glob(entry):
                ancestors = [real]
                while ancestors[-1] not in ("/", ""):
                    ancestors.append(os.path.dirname(ancestors[-1]))
                hit = any(fnmatch.fnmatchcase(a, entry) for a in ancestors)
                prefix = entry[:min(entry.index(c) for c in "*?[" if c in entry)].rstrip("/") or "/"
                if hit or (prefix != "/" and _inside(prefix, real)):
                    out.append((src, entry))
            else:
                lit = os.path.realpath(entry)
                if _inside(real, lit) or _inside(lit, real):
                    out.append((src, entry))
    return out


# ---- 2. confirm -------------------------------------------------------------------------------------------

def mount_list(argv):
    """The planned mount list from the audited `podman run` argv: one entry per --mount and --tmpfs, as
    {"source", "target", "mode"}, sorted by target. This is what is hashed, so it is derived from the argv
    the container will be created with, not from a second computation."""
    out = []
    for o in argv:
        if o.startswith("--mount="):
            fields = dict(f.split("=", 1) if "=" in f else (f, "") for f in o[len("--mount="):].split(","))
            kind = fields.get("type")
            target = fields.get("dst") or fields.get("destination") or fields.get("target") or ""
            source = "tmpfs" if kind == "tmpfs" else (fields.get("src") or fields.get("source") or "")
            mode = "ro" if "ro" in fields else "rw"
            out.append({"source": source, "target": target, "mode": mode})
        elif o.startswith("--tmpfs="):
            target, _, opts = o[len("--tmpfs="):].partition(":")
            out.append({"source": "tmpfs", "target": target, "mode": "ro" if "ro" in opts.split(",") else "rw"})
    return sorted(out, key=lambda e: e["target"])


def canonical_json(obj):
    """RFC 8785 for what these messages hold: objects with sorted keys, no whitespace, strings escaped the
    way json.dumps does with ensure_ascii off (the same forms JCS prescribes), no floats."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def mounts_hash(entries):
    return "sha256:" + hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def confirm_message(record, *, preset, network, mounts_hash_value, project_root, agent, clone=None):
    doc = {"handshake": HANDSHAKE, "session": record["session"], "preset": preset, "isolation": "container",
           "network": network, "lists_applied": True, "mounts_hash": mounts_hash_value,
           "container_paths": {"socket_dir": SOCKET_DIR_DST, "bridge": BRIDGE_DST, "project_root": project_root},
           "agent": agent, "launcher": launcher_version()}
    if clone:
        doc.update({"clone": clone["clone"], "branch": clone["branch"], "base": clone["base"]})
    return doc


def _write_json(run_dir, name, doc):
    path = os.path.join(run_dir, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    return path


def confirm(record, doc, run_dir):
    """`tm session confirm <session> --json FILE`. A non-zero exit aborts the session before the container
    exists (the caller has not run `podman run` yet)."""
    path = _write_json(run_dir, CONFIRM_FILE, doc)
    p = _run([record["tm"], "session", "confirm", record["session"], "--json", path])
    if p.returncode != 0:
        raise AgentboxError(f"`tm session confirm {record['session']}` refused (exit {p.returncode}): "
                            f"{(p.stderr or p.stdout).strip()[-400:]}; the session does not start")
    return path


# ---- 3. serve ---------------------------------------------------------------------------------------------

def serve(record, run_dir):
    """Start `tm session serve <session>` beside the session: its own process group, a pid file and a log
    in the run dir, stopped by `stop_serve` at teardown (pid plus cmdline check, like the gate). Restarting
    it if it dies mid-session is not built (the contract records that as a gap)."""
    log = open(os.path.join(run_dir, SERVE_LOG), "ab")
    proc = subprocess.Popen([record["tm"], "session", "serve", record["session"]], stdin=subprocess.DEVNULL,
                            stdout=log, stderr=log, start_new_session=True)
    log.close()
    with open(os.path.join(run_dir, SERVE_PID), "w") as f:
        f.write(f"{proc.pid}\n")
    time.sleep(0.2)
    if proc.poll() is not None:
        raise AgentboxError(f"`tm session serve {record['session']}` exited at once (status {proc.returncode}); "
                            f"see {os.path.join(run_dir, SERVE_LOG)}")
    return proc


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return None


def stop_serve(run_dir, session_id):
    """Stop the serve child named by the pid file, if that PID is still a `tm session serve <session>`."""
    try:
        with open(os.path.join(run_dir, SERVE_PID)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    cmd = _cmdline(pid)
    if not cmd or "serve" not in cmd or session_id not in cmd:
        return False                                 # the PID was reused by something else
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(50):
        if _cmdline(pid) is None:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.waitpid(pid, os.WNOHANG)                  # ours to reap if we started it; harmless otherwise
    except ChildProcessError:
        pass
    return True


# ---- 4. end -----------------------------------------------------------------------------------------------

def head_of(repo, ref=None):
    """A commit id, or "" when the repo or ref cannot be read (a message with head == base is still valid)."""
    for candidate in ([ref] if ref else []) + ["HEAD"]:
        try:
            p = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", candidate],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    return ""


def end_message(record, meta, exit_reason):
    clone = meta.get("clone") or {}
    if clone:
        head = head_of(clone["clone"], f"refs/heads/{clone['branch']}") or clone["base"]
        doc = {"clone": clone["clone"], "base": clone["base"], "head": head}
    else:
        root = record.get("project_root") or meta.get("root") or ""
        head = head_of(root) if root else ""
        doc = {"base": meta.get("base") or head, "head": head}
    return {"handshake": HANDSHAKE, "session": record["session"], **doc, "exit": exit_reason,
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def end(meta, run_dir, exit_reason, warn):
    """`tm session end <session> --json FILE`, then stop the serve child. Runs from every teardown path and
    needs only session.json (`meta`): the launcher process may be gone (`ab session gc`). Non-zero from `tm`
    is logged, not fatal. Never raises."""
    record = (meta or {}).get("toulmin")
    if not record:
        return None
    if exit_reason not in EXITS:
        exit_reason = "error"
    try:
        doc = end_message(record, meta, exit_reason)
        path = _write_json(run_dir, END_FILE, doc)
        p = _run([record["tm"], "session", "end", record["session"], "--json", path])
        if p.returncode != 0:
            warn(f"agentbox: `tm session end {record['session']}` exited {p.returncode}: "
                 f"{(p.stderr or p.stdout).strip()[-300:]}")
    except Exception as e:                      # the end call informs Toulmin; it must not stop a teardown
        warn(f"agentbox: `tm session end` not delivered: {e!r}")
        doc = None
    try:
        stop_serve(run_dir, record["session"])
    except Exception as e:
        warn(f"agentbox: could not stop `tm session serve`: {e!r}")
    return doc
