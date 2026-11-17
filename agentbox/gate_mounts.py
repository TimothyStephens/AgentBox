"""Mount verification for the gate: the grant roots and their covers (masks, .git/hooks, compose and lockdown
covers), the bind and mount-option checks on a create, the grant volumes behind sub-path binds, and, at start,
reading /proc/<pid>/mountinfo for what the runtime actually mounted under the per-container locks. The
mountinfo parsers are plain functions; the rest is `MountsMixin`, a mixin of `gate.Gate` (every method keeps
its name and runs with the Gate as `self`). Split out of gate.py, which re-exports every module-level name
here. `Gate.unmasked_secret_under` stays in gate.py: tests lower gate.BIND_SCAN_LIMIT to reach its cutoff."""
import os
import re
import stat
import threading
from urllib.parse import quote

from .fsutil import inside
from .gate_http import UPSTREAM_TIMEOUT
from .gate_schema import DEV_OWNER, Deny, NotFound

ROLE_GRANT = "grant"                 # the gate's bind-backed volumes over the grant roots (sub-path binds)
MOUNT_OPTION_OK = re.compile(r"^(r?(shared|slave|private|unbindable)|ro|rw|nosuid|suid|nodev|dev|noexec|exec|"
                             r"r?bind|z|Z|idmap(=.*)?|nocopy|copy|notmpcopyup|tmpcopyup|size=\d+[kmgKMG]?|"
                             r"mode=[0-7]{3,4}|uid=\d+|gid=\d+|relatime|norelatime|noatime|strictatime|"
                             r"src-nofollow|dest-nofollow)$")
# Deploy mode: never a bind source, whatever the stage or deploy_binds hold (the host's kernel and device
# interfaces, and /run, where the user's podman and systemd sockets live).
DEPLOY_SYSTEM = ("/proc", "/sys", "/dev", "/run")
# The host's timezone, which compose files commonly bind read-only. Allowed in deploy mode without a
# deploy_binds entry, read-only only (Tim, 17 Nov). /etc/localtime is a link into zoneinfo, so its real path
# is what the gate sees.
DEPLOY_TIMEZONE = ("/etc/localtime", "/etc/timezone", "/usr/share/zoneinfo")


def parse_mountinfo_opts(text):
    """{mount point: (major:minor, root within that filesystem, [per-mount options])}."""
    def unescape(s):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 6:
            out[unescape(f[4])] = (f[2], unescape(f[3]), f[5].split(","))
    return out


def parse_mountinfo(text):
    """[(mount point, 'major:minor', root within that filesystem)] from a mountinfo file."""
    def unescape(s):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 5:
            out.append((unescape(f[4]), f[2], unescape(f[3])))
    return out


class MountsMixin:
    # ---- grant roots and covers -------------------------------------------------------------------
    def _roots(self):
        """Where binds may come from: the grant roots; in deploy mode the deploy copy and the deploy_binds."""
        if self.deploy:
            return [os.path.realpath(r) for r in [self.cfg["stage"]] + list(self.cfg.get("deploy_binds") or [])]
        return [os.path.realpath(r) for r in self.cfg["bind_roots"]]

    def _inside_roots(self, src):
        if not isinstance(src, str) or not src.startswith("/"):
            return False
        real = os.path.realpath(src)
        return any(inside(real, r) for r in self._roots())

    def _is_root(self, src):
        """Only a grant root itself may be bind-mounted (Tim, 1 Oct): podman resolves a bind source again when
        the container starts, and a path below a root is one the agent can swap for a symlink in between. A
        grant root is a mount point in the agent's container, so the agent cannot replace it."""
        return isinstance(src, str) and src.startswith("/") and os.path.realpath(src) in self._roots()

    def _covered(self):
        """The covered paths as recorded at session start. Never resolved again: a symlink left at a cover's old
        name must not move the entry somewhere harmless (audit 5 Oct, H1)."""
        return list(self.cfg.get("covered", []))

    def covers_intact(self):
        """Every cover still has the identity it had at session start (st_dev, st_ino; not following links).
        A cover renamed away, replaced or deleted means the path-based check can no longer be trusted."""
        for c, ident in (self.cfg.get("covered_ids") or {}).items():
            try:
                st = os.lstat(c)
            except OSError:
                return c
            if [st.st_dev, st.st_ino] != list(ident):
                return c
        return None

    def is_ro_root(self, real):
        """`real` is a read-only grant, or inside one."""
        return any(inside(real, r) for r in self.cfg.get("ro_roots") or [])

    def covers_overlap(self, real):
        """A covered path (a mask or a read-only cover in the agent's own container) that `real` overlaps: the
        same path, one under `real`, or `real` under it. The agent's in-container masks and read-only covers do
        not exist in a container it spawns through the gate, so a bind that reaches a covered path would expose
        or let it write the real data (crit 5 Oct). Returns the covered path, or None."""
        for c in self._covered():
            if inside(real, c) or inside(c, real):
                return c
        return None

    def _refuse_if_foreign(self, real, src):
        """A bind source owned by another user is almost certainly a container's data directory: under keep-id
        the agent's own files are owned by the user on the host, so a foreign uid means bytes a rootless
        container wrote (a live stack's database, say). Binding it into a spawned container is a
        mistake-model harm — refuse it (audit 6 Oct, H1, the half the gate must enforce; grantcheck also
        warns about it at scan time). The grant root itself is always user-owned, so this only ever bites a
        sub-path."""
        try:
            uid = os.lstat(real).st_uid
        except OSError:
            return          # a missing source is podman's to reject; the cover checks below still run
        if uid != os.getuid():
            raise Deny(f"bind mount {src}: owned by uid {uid}, not you — a container's data directory? agentbox "
                       "binds only paths you own; move it out of the project")

    def _refuse_if_covered(self, real, src):
        self._refuse_if_foreign(real, src)
        moved = self.covers_intact()
        if moved is not None:
            raise Deny(f"bind mount {src}: the covered path {moved} was moved or replaced during the session (by "
                       "the agent, or on the host: `git config` or an editor that saves by renaming does this), so no "
                       "bind into the project is accepted any more (end the session and start a new one)")
        c = self.covers_overlap(real)
        if c is not None:
            raise Deny(f"bind mount {src} reaches {c}, which is masked or covered read-only in the session; a "
                       "spawned container would see the real data. Bind a path that does not contain or sit "
                       "under a masked or covered path")
        new = self.unmasked_secret_under(real)
        if new is not None:
            raise Deny(f"bind mount {src} contains {new}, named like a secret or like one of the project's own "
                       "masks, which the session does not mask (it appeared after the session started). Move it "
                       "out of the project, or start a new session so it is masked")

    @staticmethod
    def own_storage(m, cid):
        """After start podman shows an image volume as a bind from the container's own storage dir; a client
        cannot name that as a bind source (sources must be inside the grants)."""
        return bool(cid) and f"/overlay-containers/{cid}/userdata/" in (m.get("Source") or "")

    def mount_rule_violations(self, mounts, cid=""):
        bad = []
        for m in mounts:
            t = m.get("Type")
            if t == "bind" and self.own_storage(m, cid):
                continue
            if t == "bind" and self.deploy:
                why = self.deploy_bind_refusal(os.path.realpath(m.get("Source") or "/"), rw=m.get("RW", True))
                if why:
                    bad.append(f"bind {m.get('Source')} ({why})")
            elif t == "bind":
                if not self._is_root(m.get("Source", "")):
                    bad.append(f"bind {m.get('Source')} (only a grant root itself)")
            elif t == "volume":
                name = m.get("Name") or ""
                vdoc = self.inspect("volumes", name) if name else None
                labels = self.labels_in("volumes", vdoc) if vdoc else None
                if self.deploy and labels is not None and not self.anonymous_volume(name, labels) \
                        and not name.startswith(f"{self.cfg['compose_project']}_"):
                    labels = None
                if labels is None or not (self.is_dev(labels) or self.anonymous_volume(name, labels)
                                          or (not self.deploy and self.grant_volume_root(vdoc))):
                    bad.append(f"volume {name or m.get('Source')} is not this session's")
            elif t == "image":
                # the image the mount serves must be one the dev tier may use (item 8): the create's
                # image_volumes are checked, but what podman stored is what counts
                try:
                    self.check_image(m.get("Source") or "", "image mount")
                except Deny as e:
                    bad.append(str(e))
            elif t != "tmpfs":
                bad.append(f"{t} mount")
        return bad

    # ---- grant volumes: bind-backed volumes over the grant roots, for sub-path binds ----------------------
    def grant_volume_root(self, doc):
        """The grant root a gate-made grant volume covers, or None if `doc` is not exactly one."""
        labels = self.labels_in("volumes", doc or {})
        opts = (doc or {}).get("Options") or {}
        if labels.get(DEV_OWNER) != self.session or labels.get("agentbox.role") != ROLE_GRANT:
            return None
        if (doc.get("Driver") or "local") != "local" or set(opts) != {"type", "o", "device"} \
                or opts.get("type") != "none" or opts.get("o") != "bind":
            return None
        dev = os.path.realpath(opts.get("device") or "")
        return dev if dev in self._roots() else None

    def grant_volume(self, root):
        """The session's grant volume for one grant root, created on the raw service on first use. Its role is
        `grant`, so the agent can name it in no request; only the gate puts it into a create."""
        name = f"{self.session}-grant{self._roots().index(root)}"
        doc = self.inspect("volumes", name)
        if doc is None:
            labels = self.stamp({})
            labels["agentbox.role"] = ROLE_GRANT
            status = self.raw_request("POST", "/libpod/volumes/create", {
                "Name": name, "Driver": "local", "Label": labels,
                "Options": {"type": "none", "o": "bind", "device": root}})
            if not (isinstance(status, int) and 200 <= status < 300):
                raise Deny(f"could not create the grant volume for {root} (status {status})")
            doc = self.inspect("volumes", name)
        if self.grant_volume_root(doc) != root:
            raise Deny(f"volume {name} exists but is not this session's grant volume for {root}")
        return name

    def grant_subpath(self, src):
        """(grant volume, sub-path) for a bind source strictly below a grant root (by real path), else None."""
        if not isinstance(src, str) or not src.startswith("/"):
            return None
        real = os.path.realpath(src)
        holders = [r for r in self._roots() if real.startswith(r.rstrip("/") + "/")]
        if not holders or self._is_root(real):
            return None
        self._refuse_if_covered(real, src)           # the sub-path (via a grant-volume SubPath) must miss covers
        root = max(holders, key=len)
        try:
            crosses = os.stat(real).st_dev != os.stat(root).st_dev
        except OSError:
            crosses = False
        if crosses:                                  # N7: grant volumes are non-recursive binds
            raise Deny(f"bind mount {src} is on a different filesystem from its grant root {root} (a mount inside "
                       "the project); bind a path that does not cross a mount")
        return self.grant_volume(root), os.path.relpath(real, root)

    @staticmethod
    def anonymous_volume(name, labels):
        """podman's anonymous volumes have a 64-hex name and no labels."""
        return bool(re.fullmatch(r"[0-9a-f]{64}", name or "")) and not labels

    def read_mountinfo(self, pid):
        with open(f"/proc/{pid}/mountinfo") as f:
            return f.read()

    def host_mountinfo(self):
        with open("/proc/self/mountinfo") as f:
            return f.read()

    def allowed_fs(self, with_root=False):
        """[(dev, path within that filesystem)] of every grant root, as the host sees them."""
        mounts = parse_mountinfo(self.host_mountinfo())
        out = []
        for real in self._roots():
            best = None
            for mp, dev, root in mounts:
                if inside(real, mp) or mp == "/":
                    if best is None or len(mp) >= len(best[0]):
                        best = (mp, dev, root)
            if best:
                mp, dev, root = best
                rel = real[len(mp):].lstrip("/") if mp != "/" else real.lstrip("/")
                loc = (dev, os.path.join(root, rel) if rel else root)
                out.append(loc + (real,) if with_root else loc)
        return out

    def mount_violations(self, doc, strict=False):
        """What the runtime actually mounted, read from /proc/<pid>/mountinfo: a plain bind must be a grant root,
        a grant volume must land inside its root, neither may reach a covered path, and both must be read-only
        when the grant is. `strict` (before the entrypoint runs, after init): a missing PID or an unreadable
        mount table is itself a violation (audit 5 Oct, H2)."""
        pid = (doc.get("State") or {}).get("Pid")
        binds = [m for m in doc.get("Mounts") or [] if m.get("Type") == "bind"
                 and not self.own_storage(m, doc.get("Id") or "")]
        grants = []
        for m in doc.get("Mounts") or []:
            if m.get("Type") == "volume" and (m.get("Name") or "").startswith(f"{self.session}-grant"):
                grants.append((m, self.grant_volume_root(self.inspect("volumes", m["Name"]))))
        if not (binds or grants):
            return []
        if not pid:
            return ["cannot read the container's mounts (no process)"] if strict else []
        try:
            mounted = parse_mountinfo_opts(self.read_mountinfo(pid))
        except OSError:
            return ["cannot read the container's mounts"] if strict else []
        located = self.allowed_fs(with_root=True)
        bad = []

        def check_host(dest, host, opts):
            c = self.covers_overlap(os.path.normpath(host))
            if c is not None:
                bad.append(f"{dest} resolved onto {c}, which is masked or covered")
            if self.is_ro_root(host) and "ro" not in opts:
                bad.append(f"{dest} is a read-only grant mounted read-write")

        for m in binds:
            got = mounted.get(m.get("Destination"))
            if got is None:
                bad.append(f"cannot confirm what is mounted at {m.get('Destination')}")
                continue
            dev, root, opts = got
            # dev: exactly a grant root (sub-paths are grant volumes); deploy: anywhere inside an allowed root
            hosts = [h for d, p, h in located if dev == d and (inside(root, p) if self.deploy else root == p)]
            if not hosts:
                bad.append(f"{m.get('Destination')} is {root} (device {dev}; stored as {m.get('Type')} "
                           f"{m.get('Source')}), not a grant root")
                continue
            check_host(m.get("Destination"), hosts[0], opts)
        by_root = {h: (d, p) for d, p, h in located}
        for m, groot in grants:
            got = mounted.get(m.get("Destination"))
            want = by_root.get(groot)
            if got is None or want is None:
                bad.append(f"cannot confirm what is mounted at {m.get('Destination')}")
                continue
            dev, root, opts = got
            if not (dev == want[0] and inside(root, want[1])):
                bad.append(f"{m.get('Destination')} is {root} (device {dev}), outside its grant root {groot}")
                continue
            host = groot if root == want[1] else os.path.join(groot, os.path.relpath(root, want[1]))
            check_host(m.get("Destination"), host, opts)
        return bad

    # ---- per-container locks (N1) ---------------------------------------------------------------------
    def begin_request(self):
        self._tls.held, self._tls.checked = [], {}

    def hold(self, cid):
        """Take container `cid`'s lock for the rest of this request (released by release_held)."""
        held = getattr(self._tls, "held", None)
        if held is None:
            self.begin_request()
            held = self._tls.held
        if any(c == cid for c, _ in held):
            return
        with self.lock:
            lock = self.ctr_locks.setdefault(cid, threading.Lock())
        if not lock.acquire(timeout=UPSTREAM_TIMEOUT):
            raise Deny(f"container {cid[:12]} is busy (a start is in progress); try again")
        held.append((cid, lock))

    def release_held(self):
        for _cid, lock in reversed(getattr(self._tls, "held", None) or []):
            lock.release()
        self._tls.held, self._tls.checked = [], {}

    def after_start_checked(self):
        """After podman's start reply, with the locks still held: a container the gate initialised must still
        be the process it checked. A different PID means it was re-initialised after the check: removed and
        refused. Still running from the checked PID: its mounts are read again, strictly. Already exited: the
        held lock means nothing re-initialised it in between, so the checked mounts were the ones it ran with."""
        for cid, pid in (getattr(self._tls, "checked", None) or {}).items():
            doc = self.inspect("containers", cid) or {}
            state = doc.get("State") or {}
            now = state.get("Pid") or 0
            if now and now != pid:
                self.remove("containers", cid, f"process {now} is not the checked {pid}")
                raise Deny(f"container {cid[:12]} was re-initialised after the gate checked it; removed it")
            if state.get("Status") == "running" and now == pid:
                bad = self.mount_violations(doc, strict=True)
                if bad:
                    self.remove("containers", cid, "; ".join(bad))
                    raise Deny(f"container {cid[:12]} mounted something it may not ({'; '.join(bad)}); removed it")

    def prepare_start(self, cid, restart=False, seen=None):
        """Before a container runs: stop it first if this is a restart, have podman set up its mounts without
        running the entrypoint (init), and check what was mounted. Its dependencies (which podman would start
        with it) get the same treatment first. On any violation the container is removed and the start
        refused. A concurrent stop would re-initialise the container and undo this check, so the handler holds
        each container's lock from here through the start reply, and rechecks the PID afterwards (audit 6 Oct,
        N1); that is what makes a swapped sub-path never reach a running process (audit 5 Oct, H2)."""
        seen = set() if seen is None else seen
        if cid in seen:
            return
        seen.add(cid)
        self.hold(cid)
        moved = self.covers_intact()                 # N5: at start too, not only at create
        if moved is not None:
            raise Deny(f"the covered path {moved} was moved or replaced during the session (by the agent, or on the "
                       "host: `git config` or an editor that saves by renaming does this); no container starts with "
                       "binds into the project any more. End the session and start a new one")
        doc = self.inspect("containers", cid)
        if doc is None:
            raise NotFound(f"no such container: {cid[:12]!r}")
        for dep in doc.get("Dependencies") or []:
            if not self.dep_ok(dep):
                raise Deny(f"dependency {str(dep)[:12]} has not passed the gate's post-create check")
            self.prepare_start(dep, False, seen)
        status = (doc.get("State") or {}).get("Status")
        if status == "running" and not restart:
            return                                       # its mounts were checked when it started
        if status in ("running", "paused") and restart:
            self.raw_request("POST", f"/libpod/containers/{quote(cid, safe='')}/stop?timeout=10")
            doc = self.inspect("containers", cid) or {}
            status = (doc.get("State") or {}).get("Status")
        if status != "initialized":
            st = self.raw_request("POST", f"/libpod/containers/{quote(cid, safe='')}/init")
            if not (isinstance(st, int) and (200 <= st < 300 or st == 304)):
                raise Deny(f"container {cid[:12]} could not be prepared for start (init: {st})")
            doc = self.inspect("containers", cid) or {}
        bad = self.mount_violations(doc, strict=True)
        if bad:
            self.remove("containers", cid, "; ".join(bad))
            raise Deny(f"container {cid[:12]} mounted something it may not ({'; '.join(bad)}); removed it "
                       "before it ran")
        if not hasattr(self._tls, "checked"):
            self.begin_request()
        self._tls.checked[cid] = (doc.get("State") or {}).get("Pid") or 0

    def check_bind(self, src):
        """A bind source: absolute, and by its real path exactly a grant root (see _is_root). Returns the real
        path, which is what gets forwarded."""
        if not isinstance(src, str) or not src.startswith("/"):
            raise Deny(f"bind mount source {src!r} must be an absolute path")
        real = os.path.realpath(src)
        if self.deploy:
            why = self.deploy_bind_refusal(real)
            if why:
                raise Deny(f"bind mount {src}: {why}")
            return real
        if self._is_root(real):
            self._refuse_if_covered(real, src)       # the whole root contains the covers; refuse binding it
            return real
        if self._inside_roots(real):
            roots = ", ".join(self._roots())
            raise Deny(f"bind mount {src}: only a grant root itself can be bind-mounted ({roots}), not a path "
                       "inside one (podman resolves the path again at start, and it could be swapped for a symlink "
                       "by then). Bind the root and use the sub-path inside the container, or use a named volume "
                       "created in this session")
        raise Deny(f"bind mount {src} is outside the project's grants")

    def deploy_bind_refusal(self, real, rw=None):
        """Deploy mode: why the real path `real` may not be bound, or None. Inside the deploy copy or a
        deploy_binds path (by real path, so a symlink in the copy cannot lead out), and never a system
        interface, a socket, or a directory holding the gate's or podman's sockets. The dev tier's covers,
        masks and foreign-owner check do not apply: the copy holds the real .env by design."""
        if any(real == z or inside(real, z) for z in
               {os.path.realpath(t) for t in DEPLOY_TIMEZONE} | set(DEPLOY_TIMEZONE)) and real != "/":
            return "the host's timezone, which may only be bound read-only" if rw else None
        if real == "/" or any(inside(real, s) for s in DEPLOY_SYSTEM):
            return "a system path"
        for sock in (self.cfg.get("raw"), self.cfg.get("upstream"), self.cfg.get("listen")):
            if sock and inside(os.path.realpath(sock), real):
                return "holds the podman or gate socket"
        try:
            if stat.S_ISSOCK(os.stat(real).st_mode):
                return "a socket"
        except OSError:
            pass                     # missing: podman refuses it itself; the root check below still applies
        if not any(inside(real, r) for r in self._roots()):
            return "outside the deploy copy and the project's deploy_binds"
        return None

    def check_mount_options(self, opts, where):
        for o in opts or []:
            if not isinstance(o, str) or not MOUNT_OPTION_OK.match(o):
                raise Deny(f"mount option {o!r} on {where} is not allowed")
