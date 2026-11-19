"""The host-side gate: the only podman socket a dev-tier agent gets.

    agent --/run/agentbox/podman.sock--> gate --> sockguard (dev profile, owner isolation) --> podman service
                                          \\--> podman service (the gate's own raw requests: inspects, pod
                                               start and pod operations, stop/init, grant and named-volume
                                               creates, and removals)

Run per session as `python3 -m agentbox.gate <config.json>` (started by the launcher in the session's slice
scope). Standard library only. One request per connection; the request is read whole (builds: streamed and
counted) and then:

  * the path must be canonical: no empty, `.` or `..` segments and no trailing slash; an encoded `/` only
    inside an image name; anything else is refused before it is matched;
  * endpoints the dev tier never needs are refused here as well as by sockguard: push, manifests, auth,
    commit, export, archive, checkpoint/restore, update, mount, kube/play/generate, image tag/untag/rm/
    load/import, network connect/disconnect, prune, secrets, quadlets, artifacts, system;
  * create (container, libpod or Docker API): every top-level field must be one the gate knows (an unknown
    field is refused). Pinned to the session network (aliases kept), no published ports, restart policy
    dropped, owner labels stamped, cgroup parent forced to the slice, memory/pids limits defaulted and
    capped, swap off, OOM score 1000, proxy variables added, binds of a grant root itself (by real
    path); a path below one becomes a SubPath of the gate's bind-backed volume over that root, which podman
    resolves inside the volume (a symlink swapped in before start cannot lead out). A bind that reaches a
    masked or covered path (a secret mask, .git/hooks, .git/config, a compose or lockdown cover) is refused:
    those covers live in the agent's own mount namespace and are absent in a container it spawns (crit 5 Oct).
    Named volumes only the session's own (a new name is created labelled
    first), no host paths through any other field (rootfs, overlay volumes, init path, conmon pid file,
    log path, health log directory, hosts file, runtime), no privilege (privileged, capabilities, devices,
    security options, host namespaces, sysctls outside net.*, OCI annotations, env from the service,
    podman secrets, dependency containers, cgroups off, OOM killer off), systemd mode off;
  * pod create: the same rules; pods with an infra container are refused (the dev sockguard cannot resolve
    pods, so no container could join one). The infra rules below stay for `infra_pods` (off);
  * Docker-API container create: refused (the dev sockguard refuses the forced cgroup parent); its checks stay
    for `compat_creates` (off);
  * network create: bridge only, internal, no options, subnets, interfaces, routes or IPAM (nothing can attach
    to such a network: containers are pinned and connect is refused);
    volume create: local driver, no options, labelled;
  * after every create the gate inspects what podman stored and judges that, not the request; on any
    violation, or if the reply cannot be read, it removes the object and refuses. Only an object whose
    check passed is recorded as the session's (by ID), and every request naming a container, pod, volume
    or network must name one recorded here *and* labelled agentbox.dev-owner=<session>, agentbox.role=dev.
    So nothing can be started, exec'd or attached during the check, the agent's own container, the proxy,
    sockguard and the holder are out of reach, and a gate restart fails closed;
  * start/restart/init (and pod start/restart): the container is `init`ed first so podman sets up the
    mounts without running the entrypoint, then what the runtime actually mounted is read from
    /proc/<pid>/mountinfo and a bind from outside the grants (a directory swapped for a symlink after
    create) is refused before the start is forwarded; a per-container lock is held from that check through
    the start reply, and the PID is rechecked afterwards;
  * build: the query must use known keys only; RUN steps join the session holder's network namespace (the
    holder's PID re-checked every build), the client's namespace options dropped except `user`, never
    pulls, capped by buildcap, proxy passed as build args, labelled; tags only localhost/ names, never
    localhost/agentbox-*, and never a name an image outside this project already has; build secrets only
    from inside the context;
  * image pull: allowlisted registries only; short names become docker.io names;
  * exec sessions can be started, resized, inspected or removed only if they were created through the gate
    on a container that passed its check.

Everything else is forwarded to sockguard, whose rules and owner isolation apply on top.

Layout: this module holds the request policy and dispatch (`Gate.decide`, the create rewrites, the post-create
and start re-checks, the upstream helpers) and the connection handler. `Gate` inherits mount verification
(grant roots and covers, bind checks, grant volumes, the mountinfo checks at start and their per-container
locks) from `gate_mounts.MountsMixin`, and image handling (image allowlist, build and pull rewrites, the
built-image record) from `gate_images.ImagesMixin`; schema tables, Containerfile reading and HTTP framing are
in `gate_schema`, `gate_dockerfile` and `gate_http`. Every module-level name of those is re-exported here.

Deploy mode (`cfg["mode"] == "deploy"`; absent or "dev" is everything above, unchanged): the same gate in front
of a per-deploy podman service, for `podman-compose up` of the LIVE stack (design.md, deploy mode).
The decision points branch on `self.deploy` and nowhere else: what is owned is what carries
agentbox.role=deploy and agentbox.deploy=<project> (labels alone: a fresh gate must reach the previous deploy's
objects for `--remove-orphans` and recreation; a dev gate always forces role=dev, so it can never make one);
containers join only the stack's own networks; ports only on cfg["lan_addr"] (or 127.0.0.1) and cfg["ports"]; binds only
inside cfg["stage"] or cfg["deploy_binds"] (never /proc, /sys, /dev, /run, a socket, or a directory holding
the gate's sockets); named volumes only `<compose_project>_*`; restart forced to always; no cgroup
parent, no proxy variables. Privilege, image and build checks are the dev tier's. `external:` volumes and networks
are usable only when named in cfg["external_ok"] (the project's deploy_external) and existing: never created,
labelled or removed, and read (inspect, exists) on the raw service, since the deploy sockguard's owner isolation
does not see them.
"""
import http.client
import json
import os
import re
import socket
import socketserver
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from .errors import AgentboxError
from .fsutil import inside
from .gate_schema import (COMPAT_KEYS, COMPAT_NETWORK_KEYS, DATA_PATHS, HOSTCONFIG_KEYS,  # noqa: F401
                          HOSTCONFIG_REFUSED, LIBPOD_KEYS, LIBPOD_REFUSED, NETWORK_KEYS, POD_KEYS, POD_REFUSED,
                          VOLUME_KEYS, DEV_OWNER, Deny, NotFound, UpstreamError, _Obj, canonical, cget, cset,
                          strict_loads)
from .gate_dockerfile import (_HEREDOC, _WS, _subst, check_run_mount, dockerfile_images,  # noqa: F401
                              heredoc_markers, image_name, instruction_words, normalize_ref)
from .gate_mounts import MOUNT_OPTION_OK, ROLE_GRANT, MountsMixin, parse_mountinfo, parse_mountinfo_opts  # noqa: F401
from .gate_images import (BUILD_KEYS, DROPPED_BUILD_KEYS, NET_ENABLED, NS_KEYS, REFUSED_BUILD_KEYS,  # noqa: F401
                          SECRET_SRC, ImagesMixin, LocalImage, _member_key, image_created)
from .gate_http import (MAX_BODY, MAX_BUILD_BODY, MAX_HEAD, TOKEN, UPSTREAM_TIMEOUT, UnixHTTP,  # noqa: F401
                        build_cut_off_message, early_reply,
                        _BytesSocket, build_head, build_response, deny, hget, hset, parse_head, parse_response,
                        pipe, pipe_back, pipe_build_reply, pipe_reply, read_body, read_chunked, read_head,
                        recv_all, send_json, spool_body)

V = r"(?:/v[0-9][0-9.]*)?"
LIB = r"(?:/libpod)?"
RE = {
    "libpod_create": re.compile(rf"^{V}/libpod/containers/create$"),
    "compat_create": re.compile(rf"^{V}/containers/create$"),
    "pod_create": re.compile(rf"^{V}/libpod/pods/create$"),
    "net_create": re.compile(rf"^{V}{LIB}/networks/create$"),
    "vol_create": re.compile(rf"^{V}{LIB}/volumes/create$"),
    "build": re.compile(rf"^{V}{LIB}/build$"),
    "pull": re.compile(rf"^{V}(?:/libpod/images/pull|/images/create)$"),
    "exec_create": re.compile(rf"^{V}{LIB}/containers/([^/]+)/exec$"),
    "exec_op": re.compile(rf"^{V}{LIB}/exec/([^/]+)/(start|resize|json|remove)$"),
    "start": re.compile(rf"^{V}{LIB}/containers/([^/]+)/(start|restart|init)$"),
    "pod_start": re.compile(rf"^{V}/libpod/pods/([^/]+)/(start|restart)$"),
}
CREATES = ("libpod_create", "compat_create", "pod_create", "net_create", "vol_create")
# Operations on a pod the session owns (labels and the verified record, checked first) that the gate sends to the
# podman service itself: the dev sockguard's owner policy cannot resolve pods (trial, 2 Oct: compose's `pod rm`
# failed and left the pod behind).
POD_RAW = re.compile(rf"^{V}/libpod/pods/[^/]+(?:/(json|exists|top|start|stop|kill|restart))?$")
POD_RAW_OPS = {("GET", "json"), ("GET", "exists"), ("GET", "top"), ("DELETE", ""), ("POST", "start"),
               ("POST", "stop"), ("POST", "kill"), ("POST", "restart")}
LIFECYCLE = re.compile(rf"^{V}{LIB}/containers/[^/]+/(stop|kill|pause|unpause|restart|init|start)$")
POD_LIFECYCLE = re.compile(rf"^{V}/libpod/pods/[^/]+/(stop|kill|pause|unpause|restart|start)$")
UPGRADE = re.compile(rf"^{V}{LIB}/(?:exec/[^/]+/start|containers/[^/]+/attach)$")
TAIL = re.compile(rf"^{V}{LIB}(/.*)$")
# Collection paths whose second segment is an action word, not a resource name.
COLLECTION = {"json", "create", "prune", "stats", "load", "import", "pull", "search", "export", "exists",
              "showmounts", "kube"}
RESOURCE = re.compile(rf"^{V}{LIB}/(containers|pods|volumes|networks)/([^/]+)(?:/.*)?$")
# Reads of one image (its config and ENV, layers, existence): only for images the dev tier may use (item 13). An
# image name may hold `/` (canonical_path admits an encoded one there), so the name runs to the last segment.
IMAGE_READ = re.compile(rf"^{V}{LIB}/images/(.+)/(json|history|exists|tree|changes)$")
PODS_LIST = re.compile(rf"^{V}/libpod/pods/json$")

# Refused by the gate itself (sockguard refuses them too; this keeps them refused if its rules ever loosen).
# Matched on the path without its version and /libpod prefix.
DENIED = [(m, re.compile(p), why) for m, p, why in (
    (None, r"^/images/.+/(push|tag|untag)$", "pushing or retagging images"),
    (None, r"^/images/(.+/get|get|export)$", "saving or exporting images"),
    ("DELETE", r"^/images/.+$", "removing images"),
    (None, r"^/images/(load|import|remove|prune|scp(/.*)?)$", "loading, importing or removing images"),
    (None, r"^/manifests(/.*)?$", "manifest lists"),
    (None, r"^/(auth|commit|session)$", "registry login, commit and build sessions"),
    (None, r"^/containers/[^/]+/(export|archive|checkpoint|restore|update|mount|unmount)$",
     "copying, exporting, checkpointing, updating or mounting containers"),
    (None, r"^/containers/(prune|restore|export|showmounts|checkpoint)$", "container bulk operations"),
    (None, r"^/(play|kube|generate)(/.*)?$", "kube play and generate"),
    (None, r"^/networks/[^/]+/(connect|disconnect)$", "connecting containers to networks"),
    (None, r"^/(networks|volumes|pods|system|images|containers|builds?)/prune$", "prune"),
    (None, r"^/volumes/[^/]+/(import|export)$", "volume import and export"),
    (None, r"^/(secrets|quadlets|artifacts|local|swarm|services|tasks|nodes|configs|plugins|distribution)(/.*)?$",
     "secrets, quadlets, artifacts and swarm endpoints"),
    (None, r"^/system/(?!df$).*$", "system operations"),
)]

# The client's side (item 43): a client that stops sending its request (or reading the gate's own reply) is
# dropped after this long, per read or write. Lifted for what is meant to be long-lived: upgraded exec/attach
# streams, a build's reply, and a relayed reply (wait, logs -f, events).
CLIENT_TIMEOUT = 60
# A podman 5xx on a lookup is retried: `podman run --rm` removes the container in the service while the client sends
# its own DELETE, and podman answers 500 mid-removal (19 Nov itest). A lookup that stays 5xx still fails closed.
INSPECT_TRIES = 3
INSPECT_RETRY_DELAY = 0.2
MAX_CONNECTIONS = 128                # concurrent client connections; one more is answered 503
MAX_EXEC_IDS = 4096                  # exec sessions remembered; the oldest are forgotten (refused) past this
BIND_SCAN_LIMIT = 200000             # entries walked under a bind source looking for new secret files (N6)

class LocalReply(Exception):
    """A GET the gate answers itself (200, JSON), from the raw service and filtered to the session's objects."""
    def __init__(self, body):
        super().__init__(len(body))
        self.body = body


# Annotations with an effect on the runtime or OCI hooks (crun reads run.oci.*, e.g. keep_original_groups).
# podman's own informational io.podman.annotations.* are dropped from requests; podman re-adds some.
ANNOTATION_REFUSED = ("run.oci.", "io.kubernetes.", "io.containers.", "org.systemd.", "io.crun.", "module.wasm.",
                      "org.opencontainers.")
ANNOTATION_DROPPED = "io.podman.annotations."
# The only refused annotations an image may bring, compared by value (review of 785ee15, L3: named, not implied).
# Everything else in ANNOTATION_REFUSED (run.oci., org.systemd., ...) changes how crun or systemd run the
# container and stays refused even from an image.
ANNOTATION_FROM_IMAGE = ("org.opencontainers.",)
SYSCTL_ALLOWED = ()                  # none: the dev sockguard refuses every sysctl (1 Oct), so the gate does too
INFRA_ROOTFS = "/libpod/tmp/infra-container"
# No `U`/`chown` (audit 6 Oct, cheap half of H1): they recursively re-own the bound host files to the
# container user, and an accidental re-own of a grant's files is a mistake-model harm. They are refused up
# front by MOUNT_OPTION_OK in check_mount_options; this VOLUME_OPTS allowlist is a second layer that, by
# then, never sees them.
VOLUME_OPTS = ("ro", "rw", "z", "Z", "idmap", "noexec", "exec", "nosuid", "suid", "nodev", "dev")
NS_REFUSED = ("host", "path", "container", "ns")

# ---- deploy mode (design.md, deploy mode) ----
DEPLOY_LABEL = "agentbox.deploy"     # =<project>: what a deploy-mode gate may reach (with agentbox.role=deploy)
ROLE_DEPLOY = "deploy"
COMPOSE_LABELS = ("io.podman.compose.project", "com.docker.compose.project")
# `always`, not `unless-stopped`: rootless podman-restart.service runs `podman start --all --filter
# restart-policy=always`, so anything else stays down after a reboot (decisions.md, 18 Nov 2026).
DEPLOY_RESTART = "always"
# Besides the LAN address, a listed port may be published on loopback (a local reverse proxy or tunnel).
DEPLOY_LOOPBACK = "127.0.0.1"
DEPLOY_LIBPOD_REFUSED = {k: v for k, v in LIBPOD_REFUSED.items() if k != "portmappings"}
DEPLOY_HOSTCONFIG_REFUSED = {k: v for k, v in HOSTCONFIG_REFUSED.items() if k != "PortBindings"}
DEPLOY_NET_ENTRY_KEYS = ("aliases",)   # per-network options a container may set on one of the stack's networks


def canonical_path(target):
    """The request path, decoded segment by segment. Refused: empty, `.` and `..` segments (also encoded), a
    trailing slash, backslashes, control characters, and an encoded `/` anywhere but inside an image name.
    podman's router matches on the encoded path, so what the gate matches is never looser than what runs."""
    raw = urlsplit(target).path
    if not raw.startswith("/"):
        raise Deny("the request path must be absolute")
    if raw == "/":
        return raw
    out = []
    for seg in raw.split("/")[1:]:
        if seg in ("", ".", ".."):
            raise Deny("non-canonical request path (empty, '.' or '..' segment, or a trailing slash)")
        d = unquote(seg)
        if d in (".", "..") or "\\" in d or any(ord(c) < 32 for c in d):
            raise Deny("non-canonical request path")
        if "/" in d:
            if not out or out[-1] not in ("images", "manifests") or any(p in ("", ".", "..") for p in d.split("/")):
                raise Deny("an encoded '/' is only accepted inside an image name")
        out.append(d)
    return "/" + "/".join(out)


class Gate(MountsMixin, ImagesMixin):
    def __init__(self, cfg):
        self.cfg = cfg
        mode = cfg.get("mode") or "dev"
        if mode not in ("dev", "deploy"):
            raise ValueError(f"gate mode {mode!r} is neither dev nor deploy")
        self.deploy = mode == "deploy"
        self.session = cfg["session"]
        self.network = None if self.deploy else cfg["network"]
        # Sessions' containers die first under memory pressure (1000); a deployed live service keeps the
        # host's default (0), like a service started by hand (Tim, 17 Nov).
        self.oom_score = 0 if self.deploy else 1000
        self.labels = dict(cfg["labels"])
        if self.deploy:
            self._check_deploy_cfg()
            self.refused = set()         # deploy mode: IDs the gate removed (labels alone decide ownership)
        # deploy mode: `external:` volumes and networks the project lists in deploy_external. Used as they are
        # (they must exist), never created, labelled or removed; read through the raw service.
        self.external_ok = frozenset(cfg.get("external_ok") or ()) if self.deploy else frozenset()
        self.exec_ids = {}               # exec session id -> container id
        self.slots = threading.BoundedSemaphore(int(cfg.get("max_connections", MAX_CONNECTIONS)))
        self.verified = set()            # IDs (volume names) whose post-create check passed
        self.lock = threading.Lock()
        # Per-container locks (audit 6 Oct, N1): a start holds its containers' locks from the gate's
        # init-and-check until podman's start reply, and every request that can stop or re-initialise a
        # container takes the same lock, so nothing can re-initialise it (and re-resolve its sub-paths)
        # between the check and the start.
        self.ctr_locks = {}
        self._tls = threading.local()
        self.log_path = cfg.get("log")

    def _check_deploy_cfg(self):
        cfg = self.cfg
        if not self.labels.get(DEPLOY_LABEL):
            raise ValueError(f"deploy mode needs the {DEPLOY_LABEL} label in cfg['labels']")
        cp = cfg.get("compose_project")
        if not isinstance(cp, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", cp):
            raise ValueError(f"deploy mode: compose_project {cp!r} is not a compose project name")
        if not isinstance(cfg.get("lan_addr"), str) or not cfg["lan_addr"] or cfg["lan_addr"] in ("0.0.0.0", "::"):
            raise ValueError("deploy mode needs lan_addr (one address, not 0.0.0.0)")
        ports = cfg.get("ports")
        if not isinstance(ports, list) or not all(isinstance(p, int) and 0 < p < 65536 for p in ports):
            raise ValueError("deploy mode: ports must be a list of host port numbers")
        for p in [cfg.get("stage")] + list(cfg.get("deploy_binds") or []):
            if not isinstance(p, str) or not p.startswith("/") or os.path.realpath(p) == "/":
                raise ValueError(f"deploy mode: bind root {p!r} must be an absolute path other than /")

    def passed(self, oid):
        """An object already known to be this gate's (by labels, checked by the caller) may be used. Dev: it
        passed this gate's post-create check (`verified`). Deploy: labels decide, so a redeploy reaches the last
        deploy's objects; only one this gate removed is refused (its delete may not have taken effect)."""
        with self.lock:
            return oid not in self.refused if self.deploy else oid in self.verified

    def dep_ok(self, dep):
        """A dependency container (podman starts it with the dependent one)."""
        if not self.deploy:
            with self.lock:
                return dep in self.verified
        doc = self.inspect("containers", dep)
        return doc is not None and self.is_dev(self.labels_in("containers", doc)) and self.passed(self.ident(
            "containers", doc))

    def remember_exec(self, exec_id, cid):
        """Record an exec session made through the gate; bounded, the oldest forgotten first (item 43: the map
        only ever grew). A forgotten session is refused, which fails closed."""
        with self.lock:
            self.exec_ids.pop(exec_id, None)
            self.exec_ids[exec_id] = cid
            while len(self.exec_ids) > MAX_EXEC_IDS:
                self.exec_ids.pop(next(iter(self.exec_ids)))

    # ---- logging --------------------------------------------------------------------------------
    def log(self, verdict, method, path, why=""):
        line = json.dumps({"t": round(time.time(), 3), "verdict": verdict, "method": method, "path": path,
                           "why": why})
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(line + "\n")
        else:
            print(line, file=sys.stderr, flush=True)

    # ---- upstream helpers -------------------------------------------------------------------------
    def raw_json(self, path):
        """GET from the podman service directly (the gate's own lookups). (status, doc or None)."""
        conn = UnixHTTP(self.cfg["raw"])
        try:
            conn.request("GET", "/v5.0.0" + path)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, None
        finally:
            conn.close()

    def raw_request(self, method, path, body=None):
        conn = UnixHTTP(self.cfg["raw"])
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, "/v5.0.0" + path, body=data,
                         headers={"Content-Type": "application/json"} if data else {})
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def inspect(self, kind, ref):
        """What podman stored for one object, or None."""
        q = quote(ref, safe="/:@") if kind == "images" else quote(ref, safe="")
        for attempt in range(INSPECT_TRIES):
            status, doc = self.raw_json(f"/libpod/{kind}/{q}/json")
            if status < 500:
                break
            if attempt + 1 < INSPECT_TRIES:
                time.sleep(INSPECT_RETRY_DELAY * (attempt + 1))
        if status >= 500:
            # podman failed: not the same as "no such object" (review of 785ee15, L5)
            raise UpstreamError(f"podman could not look up {kind[:-1]} {ref!r} (status {status})")
        if status != 200 or doc is None:
            return None
        if isinstance(doc, list):
            doc = doc[0] if doc else None
        return doc if isinstance(doc, dict) else None

    @staticmethod
    def ident(kind, doc):
        if kind == "volumes":
            return doc.get("Name") or doc.get("name") or ""
        return doc.get("Id") or doc.get("id") or doc.get("ID") or ""

    @staticmethod
    def labels_in(kind, doc):
        if kind == "containers":
            return (doc.get("Config") or {}).get("Labels") or {}
        return doc.get("Labels") or doc.get("labels") or (doc.get("Config") or {}).get("Labels") or {}

    def is_dev(self, labels):
        """The object is this gate's: the session's dev stack, or in deploy mode the project's live stack."""
        if self.deploy:
            return labels.get("agentbox.role") == ROLE_DEPLOY and labels.get(DEPLOY_LABEL) == self.labels[DEPLOY_LABEL]
        return labels.get(DEV_OWNER) == self.session and labels.get("agentbox.role") == "dev"

    def require_dev(self, kind, ref):
        """The object must carry the dev labels and must have passed this gate's post-create check.
        Returns its ID (a volume's name)."""
        doc = self.inspect(kind, ref)
        if doc is None:
            raise NotFound(f"no such {kind[:-1]}: {ref!r}")
        if not self.is_dev(self.labels_in(kind, doc)):
            # the same answer as for a missing object: the status must not tell whether another project's or a
            # live stack's object exists (review of 785ee15, L1)
            raise NotFound(f"no such {kind[:-1]}: {ref!r}")
        oid = self.ident(kind, doc)
        if not self.passed(oid):
            raise Deny(f"{kind[:-1]} {ref!r} has not passed the gate's post-create check")
        return oid

    def remove(self, kind, oid, why):
        """Remove an object the gate refused; check the outcome. An object that may still exist is dropped
        from `verified` first, so nothing can use it even if the delete does not take effect."""
        path = {"containers": f"/libpod/containers/{quote(oid, safe='')}?force=true&v=true",
                "pods": f"/libpod/pods/{quote(oid, safe='')}?force=true",
                "volumes": f"/libpod/volumes/{quote(oid, safe='')}?force=true",
                "networks": f"/libpod/networks/{quote(oid, safe='')}?force=true"}[kind]
        with self.lock:
            # THE protection: nothing can use an object that is not in `verified`, whether or not the
            # delete below takes effect. (A second `refused` set was written here and never read.) Deploy mode
            # trusts labels instead of `verified`, so there it is `refused`, read by `passed`.
            self.verified.discard(oid)
            if self.deploy:
                self.refused.add(oid)
        for _attempt in range(2):
            try:
                status = self.raw_request("DELETE", path)
            except OSError as e:
                status = repr(e)
            if isinstance(status, int) and (200 <= status < 300 or status == 404):
                self.log("removed", "DELETE", path, why)
                return True
        self.log("remove-failed", "DELETE", path, f"{why}; last status {status}")
        return False

    def unmasked_secret_under(self, real):
        """A non-empty file named like a secret (the names mounts.py masks: MASK_FILES less MASK_EXEMPT, or
        anything under a MASK_DIRS directory), under `real`, that is not covered: it appeared after the
        session's masks were made (audit 6 Oct, N6). Gives up (refusing) past BIND_SCAN_LIMIT entries rather
        than allow an unchecked tree."""
        from . import mounts as _m
        if not os.path.isdir(real):
            return None
        covered = self._covered()
        # The project's own `mask` NAMES (a bare `data`, `state.db`) mask whatever has that name at the start;
        # one that appears later is as unmasked as a new `.env` (TODO 4.2, 17 Nov). Its PATH entries are made
        # at the start if missing, so they are always covered.
        names = set(self.cfg.get("mask_names") or [])
        # data_env (decisions.md, 19 Nov 2026): the session shows the root's env files on purpose, so one there is
        # not a secret the session failed to mask -- unless it is in a data grant inside the root, which stays
        # masked (mounts.plan).
        visible = [os.path.realpath(v) for v in self.cfg.get("env_visible") or []]
        others = [os.path.realpath(r) for r in self.cfg.get("bind_roots") or []
                  if os.path.realpath(r) not in visible]

        def env_shown(p, fn, in_secrets):
            return (not in_secrets and _m.env_file(fn) and any(inside(p, v) for v in visible)
                    and not any(inside(p, o) for o in others))
        n = 0
        for dirpath, dirnames, filenames in os.walk(real, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            n += len(dirnames) + len(filenames)
            if n > BIND_SCAN_LIMIT:
                return f"{real} (over {BIND_SCAN_LIMIT} entries; too large to check)"
            for d in dirnames:
                if d in names and not any(inside(os.path.join(dirpath, d), c) for c in covered):
                    return os.path.join(dirpath, d)
            in_secrets = any(p in _m.MASK_DIRS for p in os.path.relpath(dirpath, real).split(os.sep))
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if not (in_secrets or _m._masked(fn) or fn in names) or (fn not in names
                                                                         and env_shown(p, fn, in_secrets)):
                    continue
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if st.st_size and not any(inside(p, c) for c in covered):
                    return p
        return None

    # ---- post-create checks (what podman stored) ---------------------------------------------------
    def privilege_violations(self, doc, infra=False):
        """Shared by containers and pod infra containers."""
        hc = doc.get("HostConfig") or {}
        bad = []
        if self.deploy:
            bad += self.port_binding_violations(hc)
        elif any(v for v in (hc.get("PortBindings") or {}).values()):
            bad.append("published ports")
        if hc.get("Privileged"):
            bad.append("privileged")
        if hc.get("CapAdd"):
            bad.append(f"added capabilities {hc['CapAdd']}")
        if hc.get("Devices"):
            bad.append("devices")
        for opt in hc.get("SecurityOpt") or []:
            if not str(opt).startswith("no-new-privileges"):
                bad.append(f"security option {opt}")
        for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode"):
            mode = hc.get(key) or ""
            if mode == "host" or mode.startswith(("ns:", "/proc", "container:")):
                bad.append(f"{key} {mode}")
        if hc.get("Cgroups") == "disabled":
            bad.append("cgroups disabled")
        if hc.get("OomKillDisable"):
            bad.append("OOM killer disabled")
        for k in (hc.get("Sysctls") or {}):
            if not SYSCTL_ALLOWED or not k.startswith(SYSCTL_ALLOWED):
                bad.append(f"sysctl {k}")
        annotations = dict((doc.get("Config") or {}).get("Annotations") or {})
        annotations.update(hc.get("Annotations") or {})
        derived = self.derived_annotations(doc)
        for k, v in annotations.items():
            if k.startswith(ANNOTATION_REFUSED) and derived.get(k) != v:
                bad.append(f"annotation {k}")
        runtime = doc.get("OCIRuntime") or ""
        if "/" in runtime:
            bad.append(f"OCI runtime {runtime}")
        rootfs = doc.get("Rootfs") or ""
        # rootless podman runs a pod's infra container from its own rootfs (infra_image and infra_command
        # are refused, so the client cannot choose it); nothing else may have one
        if rootfs and not (infra and rootfs.endswith(INFRA_ROOTFS)):
            bad.append("a host directory as root filesystem")
        cid = doc.get("Id") or ""
        pidfile = doc.get("ConmonPidFile") or ""
        if pidfile and f"/{cid}/userdata/" not in pidfile:
            bad.append(f"conmon PID file {pidfile}")
        logpath = (hc.get("LogConfig") or {}).get("Path") or ""
        if logpath and f"/{cid}/userdata/" not in logpath:
            bad.append(f"log path {logpath}")
        hld = (doc.get("Config") or {}).get("HealthLogDestination") or ""
        if hld not in ("", "local", "events"):
            bad.append(f"health log destination {hld}")
        if (doc.get("Config") or {}).get("HealthcheckOnFailureAction") == "restart":
            bad.append("health check restart action")
        if (doc.get("Config") or {}).get("Secrets"):
            bad.append("podman secrets")
        for dep in doc.get("Dependencies") or []:
            if not self.dep_ok(dep):
                bad.append(f"dependency container {str(dep)[:12]} is not this session's")
        if hc.get("VolumesFrom"):
            bad.append("volumes from other containers")
        bad += self.mount_rule_violations(doc.get("Mounts") or [], cid)
        return bad

    def derived_annotations(self, doc):
        """Annotations podman itself puts on a container, which are not client-set (trial, 2 Oct): the stop
        signal and stop timeout (from the image's STOPSIGNAL or the create), and the image's own annotations,
        compared by value. Prefixes that change the runtime's behaviour stay refused even from an image."""
        cfg = doc.get("Config") or {}
        out = {}
        sig = signal_number(cfg.get("StopSignal"))
        if sig is not None:
            out["org.opencontainers.image.stopSignal"] = str(sig)
            out["org.systemd.property.KillSignal"] = str(sig)
        if isinstance(cfg.get("StopTimeout"), int):
            out["org.systemd.property.TimeoutStopUSec"] = f"uint64 {cfg['StopTimeout'] * 1000000}"
        image = self.image_doc(doc.get("Image") or "") if doc.get("Image") else None
        for k, v in ((image or {}).get("Annotations") or {}).items():
            if k.startswith(ANNOTATION_FROM_IMAGE):
                out.setdefault(k, v)
        return out

    def container_violations(self, doc):
        """What is wrong with a created container, judged by what podman stored (not by the request)."""
        hc = doc.get("HostConfig") or {}
        bad = self.privilege_violations(doc)
        labels = (doc.get("Config") or {}).get("Labels") or {}
        if not self.is_dev(labels):
            bad.append("missing the dev labels")
        if doc.get("Pod"):
            pod = self.pod_doc(doc["Pod"])
            if not pod or not self.is_dev(pod.get("Labels") or {}):
                bad.append("in a pod that is not this session's")
            infra = (pod or {}).get("InfraContainerID") or ""
            shares_net = "net" in ((pod or {}).get("SharedNamespaces") or [])
            if infra and shares_net:
                if hc.get("NetworkMode") != f"container:{infra}":
                    bad.append(f"NetworkMode {hc.get('NetworkMode')} in a pod (must be the infra container's)")
                infra_doc = self.inspect("containers", infra)
                nets = set(((infra_doc or {}).get("NetworkSettings") or {}).get("Networks") or {})
                if nets != {self.network}:
                    bad.append(f"the pod's networks {sorted(nets)}")
            else:
                bad += self.network_violations(doc)
        else:
            bad += self.network_violations(doc)
        if not self.deploy and self.cfg.get("cgroup_parent") and \
                self.cfg["cgroup_parent"] not in (hc.get("CgroupParent") or ""):
            bad.append(f"cgroup parent {hc.get('CgroupParent')}")
        if self.deploy:
            if (hc.get("RestartPolicy") or {}).get("Name") != DEPLOY_RESTART:
                bad.append(f"restart policy {(hc.get('RestartPolicy') or {}).get('Name')} (not {DEPLOY_RESTART})")
        elif (hc.get("RestartPolicy") or {}).get("Name") not in (None, "", "no"):
            bad.append("a restart policy")
        if hc.get("OomScoreAdj") != self.oom_score:
            bad.append(f"OOM score {hc.get('OomScoreAdj')}")
        mem, swap, pids = hc.get("Memory") or 0, hc.get("MemorySwap"), hc.get("PidsLimit") or 0
        if not 0 < mem <= self.memory_max:
            bad.append(f"memory limit {mem}")
        elif swap not in (mem, 0, None):
            bad.append(f"swap {swap}")
        if not 0 < pids <= self.pids_max:
            bad.append(f"pids limit {pids}")
        return bad

    def network_violations(self, doc):
        if self.deploy:
            return self.deploy_network_violations(doc)
        hc = doc.get("HostConfig") or {}
        bad = []
        mode = hc.get("NetworkMode") or ""
        if mode not in ("bridge", self.network):
            bad.append(f"NetworkMode {mode}")
        nets = set(((doc.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        if nets != {self.network}:
            bad.append(f"networks {sorted(nets)}")
        return bad

    # ---- deploy mode: networks and ports -------------------------------------------------------------
    def network_owned(self, name):
        """A network of the stack's own (deploy labels), by name or ID."""
        if not isinstance(name, str) or not name:
            return False
        doc = self.inspect("networks", name)
        if doc is not None and name in self.external_ok:
            return True                  # an external network the project lists (deploy_external)
        return doc is not None and self.is_dev(self.labels_in("networks", doc)) and self.passed(
            doc.get("id") or doc.get("Id") or name)

    def require_own_network(self, name):
        if not self.network_owned(name):
            raise Deny(f"network {name!r} is not one of this stack's networks (only networks the stack created, "
                       "labelled for this deploy, can be joined; an `external:` network must exist and be listed "
                       "in the project's deploy_external)")

    def deploy_network_violations(self, doc):
        hc = doc.get("HostConfig") or {}
        mode = hc.get("NetworkMode") or ""
        nets = set(((doc.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        if mode == "none":
            return [f"networks {sorted(nets)} with network mode none"] if nets else []
        bad = []
        if mode == "host" or mode.startswith(("ns:", "/", "container:", "slirp4netns", "pasta", "private")):
            bad.append(f"NetworkMode {mode}")
        elif mode not in ("bridge", "default", "") and mode not in nets:
            bad.append(f"NetworkMode {mode}")
        if not nets:
            bad.append("no network of the stack's own")
        for n in sorted(nets):
            if not self.network_owned(n):
                bad.append(f"network {n} is not this stack's")
        return bad

    def deploy_port(self, host_ip, host_port, where):
        """One published port: on the LAN address (an empty or 0.0.0.0 address becomes it) or on 127.0.0.1, and
        a host port the project lists. Returns the host IP to store."""
        lan, ports = self.cfg["lan_addr"], self.cfg["ports"]
        ip = host_ip or ""
        if ip in ("", "0.0.0.0"):
            ip = lan
        if ip not in (lan, DEPLOY_LOOPBACK):
            raise Deny(f"{where}: published on {host_ip}, not the deploy's LAN address {lan} or {DEPLOY_LOOPBACK}")
        if isinstance(host_port, bool) or not isinstance(host_port, int) or host_port not in ports:
            raise Deny(f"{where}: host port {host_port!r} is not one of the project's deploy ports {ports} "
                       "(a random or unlisted host port is refused)")
        return ip

    def deploy_portmappings(self, maps):
        if not isinstance(maps, list):
            raise Deny("portmappings must be a list")
        for m in maps:
            if not isinstance(m, dict):
                raise Deny("a port mapping must be an object")
            for k in m:
                if k.lower() not in ("host_ip", "container_port", "host_port", "range", "protocol"):
                    raise Deny(f"port mapping field {k!r} is not one the gate knows")
            if cget(m, "range") not in (None, 0, 1):
                raise Deny("port ranges are not allowed in a deploy (list each port)")
            cp = cget(m, "container_port")
            if isinstance(cp, bool) or not isinstance(cp, int) or not 0 < cp < 65536:
                raise Deny(f"container port {cp!r} is not allowed")
            for proto in str(cget(m, "protocol") or "tcp").split(","):
                if proto not in ("tcp", "udp", "sctp"):
                    raise Deny(f"port protocol {proto!r} is not allowed")
            cset(m, "host_ip", self.deploy_port(cget(m, "host_ip"), cget(m, "host_port"), "published port"))
        return maps

    def deploy_port_bindings(self, pb):
        """Docker API PortBindings: {"80/tcp": [{"HostIp": "", "HostPort": "8090"}]}."""
        if not isinstance(pb, dict):
            raise Deny("PortBindings must be an object")
        for key, binds in pb.items():
            for b in binds or []:
                hp = str(cget(b, "HostPort") or "")
                if not hp.isdigit():
                    raise Deny(f"published port {key}: host port {hp!r} (a range or a random port) is not allowed")
                cset(b, "HostIp", self.deploy_port(cget(b, "HostIp"), int(hp), f"published port {key}"))
                cset(b, "HostPort", hp)
        return pb

    def port_binding_violations(self, hc):
        """What podman stored: every binding on the LAN address or loopback and a listed port; no publish-all."""
        bad = ["publish all ports"] if hc.get("PublishAllPorts") else []
        for key, binds in (hc.get("PortBindings") or {}).items():
            for b in binds or []:
                hp = str((b or {}).get("HostPort") or "")
                if (b or {}).get("HostIp") not in (self.cfg["lan_addr"], DEPLOY_LOOPBACK) or not hp.isdigit() \
                        or int(hp) not in self.cfg["ports"]:
                    bad.append(f"published port {key} on {(b or {}).get('HostIp')}:{hp}")
        return bad

    def check_compose_project(self, labels, required):
        """The compose project the client names must be the deploy's (podman-compose labels everything it
        creates with it)."""
        cp = self.cfg["compose_project"]
        got = [str(labels.get(k)) for k in COMPOSE_LABELS if k in (labels or {})]
        if any(g != cp for g in got):
            raise Deny(f"compose project {', '.join(got)} is not this deploy's ({cp})")
        if required and not got:
            raise Deny(f"a deploy container must carry the compose project label ({cp})")

    def deploy_volume_name(self, name, where):
        prefix = f"{self.cfg['compose_project']}_"
        if not isinstance(name, str) or not name.startswith(prefix):
            raise Deny(f"{where}: volume {name!r} is not this stack's (its named volumes start with {prefix!r}; an "
                       "`external:` volume must exist and be listed in the project's deploy_external)")

    @property
    def memory_max(self):
        return int(self.cfg.get("memory_max", 4 * self.cfg["memory"]))

    @property
    def pids_max(self):
        return int(self.cfg.get("pids_max", 4 * self.cfg["pids"]))

    def pod_doc(self, ref):
        return self.inspect("pods", ref)

    def pod_violations(self, doc):
        bad = []
        if not self.is_dev(doc.get("Labels") or {}):
            bad.append("missing the dev labels")
        infra = doc.get("InfraConfig") or {}
        if any(v for v in (infra.get("PortBindings") or {}).values()):
            bad.append("published ports")
        if infra.get("HostNetwork"):
            bad.append("host network")
        if self.deploy:
            if doc.get("InfraContainerID"):
                bad.append("an infra container (deploy pods have none)")
        elif doc.get("InfraContainerID"):
            if "net" not in (doc.get("SharedNamespaces") or []):
                bad.append("an infra container that does not share its network")
            if set(infra.get("Networks") or []) != {self.network}:
                bad.append(f"networks {infra.get('Networks')}")
        if not self.deploy and self.cfg.get("cgroup_parent") and \
                self.cfg["cgroup_parent"] not in (doc.get("CgroupParent") or ""):
            bad.append(f"cgroup parent {doc.get('CgroupParent')}")
        return bad

    def infra_violations(self, doc):
        return self.privilege_violations(doc, infra=True) + self.network_violations(doc)

    def volume_violations(self, doc):
        bad = []
        if not self.is_dev(doc.get("Labels") or {}):
            bad.append("missing the dev labels")
        if (doc.get("Driver") or "local") != "local":
            bad.append(f"driver {doc.get('Driver')}")
        if doc.get("Options"):
            bad.append(f"options {doc.get('Options')}")
        return bad

    def net_violations(self, doc):
        bad = []
        if not self.is_dev(doc.get("labels") or doc.get("Labels") or {}):
            bad.append("missing the dev labels")
        if (doc.get("driver") or doc.get("Driver")) != "bridge":
            bad.append(f"driver {doc.get('driver') or doc.get('Driver')}")
        if not self.deploy and not (doc.get("internal") or doc.get("Internal")):
            bad.append("not internal")
        if doc.get("ipv6_enabled"):
            bad.append("IPv6")
        for k, v in (doc.get("options") or doc.get("Options") or {}).items():
            if not (k == "isolate" and str(v) in ("true", "strict")):
                bad.append(f"option {k}={v}")
        return bad

    def verify_created(self, kind, raw, name=None):
        """After a create: read podman's reply (interim 1xx skipped), inspect what it stored, and on any
        violation, or a 2xx whose object cannot be identified or inspected, remove it and refuse. Only a
        passing object is recorded in `verified`. Returns the parsed reply to send on."""
        what = {"libpod_create": "containers", "compat_create": "containers", "pod_create": "pods",
                "vol_create": "volumes", "net_create": "networks"}[kind]
        try:
            status, reason, headers, body = parse_response(raw)
        except (ValueError, http.client.HTTPException) as e:
            status, reason, headers, body = 0, "", [], b""
            self.log("error", "-", kind, f"unreadable create reply ({e!r})")
        if 0 < status < 200 or status >= 300:
            return status, reason, headers, body            # podman refused it: nothing was created
        try:
            doc = json.loads(body or b"{}")
            oid = doc.get("Id") or doc.get("id") or (doc.get("Name") if what == "volumes" else None) \
                if isinstance(doc, dict) else None
        except ValueError:
            oid = None
        if not oid:
            # Created (or maybe created), but the gate cannot tell what: find it by the name it was given and
            # remove it if it is this session's (never anything else's), then refuse.
            found = self.inspect(what, name) if name else None
            if found is not None and self.is_dev(self.labels_in(what, found)):
                self.remove(what, self.ident(what, found), "create reply without an ID")
            raise Deny(f"the create reply could not be read (status {status}); "
                       f"{'removed what was created' if found else 'nothing found to remove'}")
        try:
            stored = self.inspect(what, oid)
            if stored is None:
                bad = [f"cannot inspect the new {what[:-1]}"]
            elif what == "containers":
                bad = self.container_violations(stored)
            elif what == "pods":
                bad = self.pod_violations(stored)
                infra = stored.get("InfraContainerID")
                if infra and not bad:
                    idoc = self.inspect("containers", infra)
                    bad = self.infra_violations(idoc) if idoc else ["cannot inspect the infra container"]
            elif what == "volumes":
                bad = self.volume_violations(stored)
            else:
                bad = self.net_violations(stored)
        except (OSError, ValueError, http.client.HTTPException) as e:
            stored, bad = None, [f"inspect failed ({e!r})"]
        except Exception as e:                       # a check that crashes on an odd reply fails closed (item 44)
            stored, bad = None, [f"check crashed ({e!r})"]
        if bad:
            # An object found under its new ID was created by this request: removing it is safe. A volume or
            # network reported for an existing name (IgnoreIfExists) is refused before it gets here.
            if stored is None or what in ("containers", "pods") or self.is_dev(self.labels_in(what, stored)):
                self.remove(what, oid, "; ".join(bad))
            raise Deny(f"created {what[:-1]} did not match the rules ({'; '.join(bad)}); removed it")
        with self.lock:
            self.verified.add(oid)
            if what == "networks":
                self.verified.add(stored.get("name") or stored.get("Name") or oid)
            if what == "pods" and stored.get("InfraContainerID"):
                self.verified.add(stored["InfraContainerID"])
        return status, reason, headers, body

    # ---- start: re-check, then confirm what was mounted -------------------------------------------
    def start_targets(self, kind, ref):
        if kind == "start":
            return [self.require_dev("containers", ref)]
        pod_id = self.require_dev("pods", ref)
        pod = self.inspect("pods", pod_id) or {}
        return [c.get("Id") for c in pod.get("Containers") or [] if c.get("Id")]

    def check_start(self, kind, ref, op="start"):
        ids = self.start_targets(kind, ref)
        infra = ""
        if kind == "pod_start":
            infra = (self.inspect("pods", ref) or {}).get("InfraContainerID") or ""
        for cid in ids:
            if not self.passed(cid):
                raise Deny(f"container {cid[:12]} has not passed the gate's post-create check")
            doc = self.inspect("containers", cid)
            if doc is None:
                raise NotFound(f"no such container: {cid[:12]!r}")
            bad = self.infra_violations(doc) if cid == infra else self.container_violations(doc)
            if bad:
                raise Deny(f"container {cid[:12]} no longer matches the rules ({'; '.join(bad)})")
        seen = set()
        for cid in ids:
            self.prepare_start(cid, restart=(op == "restart"), seen=seen)
        return ids

    # ---- create rewrites --------------------------------------------------------------------------
    def stamp(self, labels):
        if self.deploy:
            # the client's own agentbox.* labels are dropped: only the gate says what a live object belongs to
            out = {k: v for k, v in (labels or {}).items() if not (k == "agentbox" or str(k).startswith("agentbox."))}
            self.check_compose_project(out, required=False)
            out.update(self.labels)
            out["agentbox.role"] = ROLE_DEPLOY
            return out
        out = dict(labels or {})
        out.update(self.labels)
        out[DEV_OWNER] = self.session
        out["agentbox.role"] = "dev"
        return out

    def network_names(self):
        """Names and network aliases of the containers already on the session network."""
        flt = quote(json.dumps({"network": [self.network]}), safe="")
        status, docs = self.raw_json(f"/libpod/containers/json?all=true&filters={flt}")
        names = set()
        for c in docs if status == 200 and isinstance(docs, list) else []:
            labels = c.get("Labels") or {}
            if not self.is_dev(labels):
                continue                         # only the dev stack's own names, not the session's infrastructure
            names.update(n.lstrip("/") for n in c.get("Names") or [])
            status2, doc = self.raw_json(f"/libpod/containers/{quote(c.get('Id', ''), safe='')}/json")
            if status2 == 200 and isinstance(doc, dict):
                net = ((doc.get("NetworkSettings") or {}).get("Networks") or {}).get(self.network) or {}
                cid = (doc.get("Id") or "")[:12]
                names.update(a for a in net.get("Aliases") or [] if a != cid)
        return names

    def no_proxy(self, own_names):
        """localhost, 127.0.0.1, the session subnet, and the names and aliases of the containers on the session
        network (plus this one's own), so service-to-service calls by name go direct (decision, 1 Oct)."""
        names = sorted((self.network_names() | set(own_names)) - {""})
        return ",".join(["localhost", "127.0.0.1"] + list(self.cfg.get("subnets", [])) + names)

    def proxy_env(self, env, as_list, own_names=()):
        """The proxy variables, unless the container sets that variable itself in any spelling (podman keeps
        env keys case-sensitive; adding a second spelling would leave two)."""
        add = dict(self.cfg["proxy_env"])
        add["no_proxy"] = self.no_proxy(own_names)
        if as_list:
            have = {e.split("=", 1)[0].lower() for e in env or []}
            return list(env or []) + [f"{k}={v}" for k, v in add.items() if k.lower() not in have]
        out = dict(env or {})
        have = {k.lower() for k in out}
        for k, v in add.items():
            if k.lower() not in have:
                out[k] = v
        return out

    def named_volume(self, name, where):
        """A named volume a create refers to: this session's own, or a new name, created here labelled."""
        if not isinstance(name, str) or not name:
            return
        if "/" in name or name.startswith(".") or "~" in name or not re.match(r"^[A-Za-z0-9][\w.-]*$", name):
            raise Deny(f"{where}: {name!r} is neither an absolute bind source nor a volume name")
        if self.deploy and name in self.external_ok:
            if self.inspect("volumes", name) is None:
                raise Deny(f"{where}: external volume {name!r} does not exist (agentbox never creates one)")
            return
        if self.deploy:
            self.deploy_volume_name(name, where)
        doc = self.inspect("volumes", name)
        if doc is None:
            status = self.raw_request("POST", "/libpod/volumes/create",
                                      {"Name": name, "Label": self.stamp({})})
            if not (isinstance(status, int) and 200 <= status < 300):
                raise Deny(f"could not create volume {name} (status {status})")
            with self.lock:
                self.verified.add(name)
            return
        self.require_dev("volumes", name)

    def list_pods(self, target):
        """GET /libpod/pods/json, answered by the gate: podman's list (from the raw service, the client's query
        kept), filtered to pods carrying the dev labels that passed the post-create check. Sockguard's owner
        policy cannot resolve pods, so the list it forwarded showed every pod on the host (item 13)."""
        q = urlsplit(target).query
        status, docs = self.raw_json("/libpod/pods/json" + (f"?{q}" if q else ""))
        if status != 200 or not isinstance(docs, list):
            raise UpstreamError(f"podman could not list pods (status {status})")
        with self.lock:
            verified = set(self.verified)
        keep = [d for d in docs if isinstance(d, dict) and self.is_dev(d.get("Labels") or {})
                and ((d.get("Id") or "") not in self.refused if self.deploy else (d.get("Id") or "") in verified)]
        return (json.dumps(keep) + "\n").encode()

    def check_annotations(self, ann, where):
        out = {}
        for k, v in (ann or {}).items():
            if k.startswith(ANNOTATION_DROPPED):
                continue
            if k.startswith(ANNOTATION_REFUSED):
                raise Deny(f"{where} annotation {k} is not allowed")
            out[k] = v
        return out

    @staticmethod
    def check_sysctls(sysctls, where):
        for k in (sysctls or {}):
            if not SYSCTL_ALLOWED or not str(k).startswith(SYSCTL_ALLOWED):
                raise Deny(f"{where} sysctl {k} is not allowed")

    @staticmethod
    def check_hosts_file(value):
        if value not in (None, "", "image", "none"):
            raise Deny("a hosts file from the host is not allowed")

    def check_limits(self, memory, swap, pids):
        """Explicit limits: positive and within the session's caps (an unset value gets the default)."""
        if memory not in (None, 0) and not (isinstance(memory, int) and 0 < memory <= self.memory_max):
            raise Deny(f"memory limit {memory} is not allowed (1 byte to {self.memory_max})")
        if swap not in (None, 0) and not (isinstance(swap, int) and swap > 0):
            raise Deny(f"swap limit {swap} is not allowed")
        if pids not in (None, 0) and not (isinstance(pids, int) and 0 < pids <= self.pids_max):
            raise Deny(f"pids limit {pids} is not allowed (1 to {self.pids_max})")

    def check_common_libpod(self, spec, refused, what):
        for key, why in refused.items():
            if spec.get(key):
                raise Deny(f"{why} are not allowed" if why.endswith("s") else f"{why} is not allowed")
        for ns in ("pidns", "ipcns", "utsns", "userns", "cgroupns"):
            mode = cget(spec.get(ns), "nsmode")
            if mode in NS_REFUSED:
                raise Deny(f"{what} {ns} {mode} is not allowed")
        self.check_sysctls(spec.get("sysctl"), what)
        self.check_hosts_file(spec.get("base_hosts_file"))
        for v in spec.get("volumes") or []:
            opts = cget(v, "Options") or []
            self.check_mount_options(opts, cget(v, "Dest"))
            sub = cget(v, "SubPath") or ""
            if ".." in str(sub).split("/") or str(sub).startswith("/"):
                raise Deny(f"volume subpath {sub!r} is not allowed")
            self.named_volume(cget(v, "Name"), "volume")
        kept, grant_mounts = [], []
        for m in spec.get("mounts") or []:
            t = cget(m, "type")
            if t == "bind":
                self.check_mount_options(cget(m, "options"), cget(m, "destination"))
                sub = self.grant_subpath(cget(m, "source")) if what == "container" and not self.deploy else None
                if sub:
                    # A path below a grant root: mounted as the gate's bind-backed volume over that root with a
                    # SubPath, which podman resolves inside the volume (openat2 in-root), so a symlink swapped in
                    # before start cannot lead out (Tim's option 3, 1 Oct; evidence in docs/decisions.md).
                    name, rel = sub
                    opts = [o for o in cget(m, "options") or [] if o in VOLUME_OPTS]
                    if self.is_ro_root(os.path.realpath(cget(m, "source"))):
                        opts = [o for o in opts if o not in ("rw", "ro")] + ["ro"]      # a read-only grant (audit H3)
                    grant_mounts.append({"Name": name, "Dest": cget(m, "destination"), "SubPath": rel,
                                         "Options": opts})
                    continue
                real = self.check_bind(cget(m, "source"))
                cset(m, "source", real)
                if self.is_ro_root(real):
                    cset(m, "options", [o for o in cget(m, "options") or [] if o not in ("rw", "ro")] + ["ro"])
            elif t == "tmpfs":
                self.check_mount_options(cget(m, "options"), cget(m, "destination"))
            else:
                raise Deny(f"{t!r} mounts are not allowed (bind and tmpfs only; named volumes go in volumes)")
            kept.append(m)
        if grant_mounts:
            spec["mounts"] = kept
            spec["volumes"] = list(spec.get("volumes") or []) + grant_mounts
        for iv in spec.get("image_volumes") or []:
            self.check_image_volume(cget(iv, "Source"))
        rl = spec.get("resource_limits") or {}
        mem = cget(rl, "memory") or {}
        self.check_limits(cget(mem, "limit"), cget(mem, "swap"), cget(cget(rl, "pids") or {}, "limit"))
        if cget(mem, "disableOOMKiller"):
            raise Deny("disabling the OOM killer is not allowed")
        if cget(rl, "unified"):
            raise Deny("raw cgroup settings (unified) are not allowed")
        for k in ("network_options", "networks", "cni_networks"):
            spec.pop(k, None)

    def rewrite_libpod_container(self, spec):
        self.check_common_libpod(spec, DEPLOY_LIBPOD_REFUSED if self.deploy else LIBPOD_REFUSED, "container")
        if self.deploy and spec.get("portmappings"):
            spec["portmappings"] = self.deploy_portmappings(spec["portmappings"])
        if spec.get("dependencyContainers"):
            # depends_on (podman-compose's --requires): only containers of this session, passed by ID
            spec["dependencyContainers"] = [self.require_dev("containers", str(d))
                                            for d in spec["dependencyContainers"]]
        if not spec.get("image"):
            raise Deny("a container needs an image (a host directory as root is refused)")
        own = self.own_short_name(spec["image"])
        if own:
            spec["image"] = own                    # this project's own build, by its full name
        self.check_image(spec["image"], "create")
        if spec.get("cgroups_mode") not in (None, "", "enabled"):
            raise Deny(f"cgroups mode {spec['cgroups_mode']} is not allowed")
        if spec.get("seccomp_policy") not in (None, "", "default"):
            raise Deny("a seccomp policy from the image is not allowed")
        if spec.get("oom_score_adj") not in (None, self.oom_score):
            raise Deny(f"OOM score {spec['oom_score_adj']} is not allowed (the gate sets {self.oom_score})")
        if "keep-groups" in (spec.get("groups") or []):
            raise Deny("keep-groups is not allowed")
        if spec.get("health_check_on_failure_action") in ("restart", 3):
            raise Deny("a health check that restarts the container is not allowed")
        if spec.get("healthLogDestination") not in (None, "", "local", "events"):
            raise Deny("a health log directory on the host is not allowed")
        log = spec.get("log_configuration") or {}
        if cget(log, "path") or any(str(k).lower() == "path" for k in (cget(log, "options") or {})):
            raise Deny("a log path on the host is not allowed")
        spec["annotations"] = self.check_annotations(spec.get("annotations"), "container")
        pod = spec.get("pod")
        if pod:
            # by ID from here on: the name could be reused, and sockguard's owner policy resolves IDs
            pod = spec["pod"] = self.require_dev("pods", pod)
            if self.deploy and self.pod_has_infra(pod):
                raise Deny("a pod with an infra container cannot be joined in a deploy")
            if not self.pod_has_infra(pod):
                # A pod without an infra container sharing its network (podman-compose's default has none)
                # gives its containers nothing: membership is dropped and the container pinned like any other.
                spec.pop("pod")
                pod = None
        if pod:
            # The infra container owns the network: the member takes it, nothing of its own.
            spec.pop("netns", None)
            spec.pop("Networks", None)
        elif self.deploy:
            self.deploy_libpod_network(spec)
        else:
            mode = cget(spec.get("netns"), "nsmode")
            if mode in ("host", "path", "container", "ns", "slirp4netns", "pasta"):
                raise Deny(f"network mode {mode} is not allowed")
            aliases = []
            for net in (spec.get("Networks") or {}).values():
                aliases += cget(net, "aliases") or []
            spec["netns"] = {"nsmode": "bridge"}
            spec["Networks"] = {self.network: {"aliases": sorted(set(aliases))} if aliases else {}}
        if self.deploy:
            self.check_compose_project(spec.get("labels") or {}, required=True)
        spec["labels"] = self.stamp(spec.get("labels"))
        spec.pop("restart_policy", None)
        spec.pop("restart_tries", None)
        if self.deploy:
            spec["restart_policy"] = DEPLOY_RESTART
        spec["systemd"] = "false"
        if not self.deploy:
            spec["cgroup_parent"] = self.cfg["cgroup_parent"]
        spec["oom_score_adj"] = self.oom_score
        rl = spec.get("resource_limits") or {}
        mem = cget(rl, "memory") or {}
        limit = cget(mem, "limit") or self.cfg["memory"]
        cset(mem, "limit", limit)
        cset(mem, "swap", limit)
        cset(rl, "memory", mem)
        pids = cget(rl, "pids") or {}
        cset(pids, "limit", cget(pids, "limit") or self.cfg["pids"])
        cset(rl, "pids", pids)
        spec["resource_limits"] = rl
        if not self.deploy:
            own = [spec.get("name", ""), spec.get("hostname", "")]
            own += [a for net in (spec.get("Networks") or {}).values() for a in cget(net, "aliases") or []]
            spec["env"] = self.proxy_env(spec.get("env"), as_list=False, own_names=own)
        self.split_case_variants(spec)
        return spec

    def deploy_libpod_network(self, spec):
        """Deploy mode: the container joins the stack's own networks (bridge), or none. Never podman's default
        network (not the stack's), host, another container's or a path namespace, pasta or slirp4netns."""
        mode = cget(spec.get("netns"), "nsmode")
        nets = spec.get("Networks") or {}
        if not isinstance(nets, dict):
            raise Deny("Networks must be an object")
        if mode == "none":
            if nets:
                raise Deny("network mode none with networks")
            spec.pop("Networks", None)
            return
        if mode not in (None, "", "bridge"):
            raise Deny(f"network mode {mode} is not allowed")
        if not nets:
            raise Deny("a deploy container must join one of the stack's own networks (podman's default network "
                       "is not the stack's)")
        for name, opts in nets.items():
            self.require_own_network(name)
            for k, v in (opts or {}).items() if isinstance(opts, dict) else [("?", opts)]:
                if k.lower() not in DEPLOY_NET_ENTRY_KEYS and v:
                    raise Deny(f"network {name}: option {k} is not allowed (aliases only)")
        spec["netns"] = {"nsmode": "bridge"}

    @staticmethod
    def split_case_variants(spec):
        """NO_PROXY and no_proxy are two variables, but the dev sockguard refuses a JSON object holding keys
        that differ only in case. The second spelling goes in `envmerge` ("KEY=VALUE"), which podman sets the
        same way (trial, 2 Oct). A value with a `$` would be expanded there, so that case is refused."""
        env, seen, moved = dict(spec.get("env") or {}), {}, []
        for k in list(env):
            if k.lower() in seen:
                v = str(env.pop(k))
                if "$" in v:
                    raise Deny(f"env {k} and {seen[k.lower()]} differ only in case and {k}'s value has a '$'; "
                               "rename one")
                moved.append(f"{k}={v}")
            else:
                seen[k.lower()] = k
        if moved:
            spec["env"] = env
            spec["envmerge"] = list(spec.get("envmerge") or []) + moved

    def rewrite_compat_container(self, spec):
        hc = canonical(spec.get("HostConfig") or {}, HOSTCONFIG_KEYS, "HostConfig")
        for key in ("MaskedPaths", "ReadonlyPaths"):
            if hc.get(key) is not None:          # an empty list means "unmask everything"
                raise Deny(f"changed {key} are not allowed")
        if self.deploy and hc.get("PortBindings"):
            hc["PortBindings"] = self.deploy_port_bindings(hc["PortBindings"])
        for key, why in (DEPLOY_HOSTCONFIG_REFUSED if self.deploy else HOSTCONFIG_REFUSED).items():
            if hc.get(key):
                raise Deny(f"{why} are not allowed" if why.endswith("s") else f"{why} is not allowed")
        for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode"):
            if hc.get(key) and hc.get(key) not in ("private", "", "shareable", "none"):
                raise Deny(f"{key} {hc[key]} is not allowed")
        for opt in hc.get("SecurityOpt") or []:
            if not re.match(r"^no-new-privileges([:=]true)?$", str(opt)):
                raise Deny(f"security option {opt} is not allowed")
        if hc.get("OomScoreAdj") not in (None, 0, self.oom_score):
            raise Deny(f"OOM score {hc['OomScoreAdj']} is not allowed (the gate sets {self.oom_score})")
        self.check_sysctls(hc.get("Sysctls"), "container")
        hc["Annotations"] = self.check_annotations(hc.get("Annotations"), "container")
        log = hc.get("LogConfig") or {}
        if any(str(k).lower() == "path" for k in (cget(log, "Config") or {})):
            raise Deny("a log path on the host is not allowed")
        if "keep-groups" in (hc.get("GroupAdd") or []):
            raise Deny("keep-groups is not allowed")
        self.check_limits(hc.get("Memory"), hc.get("MemorySwap") if hc.get("MemorySwap") != -1 else None,
                          hc.get("PidsLimit"))
        if hc.get("MemorySwap") == -1 or (hc.get("PidsLimit") or 0) < 0:
            raise Deny("unlimited swap or pids are not allowed")
        mode = hc.get("NetworkMode") or ""
        if mode == "host" or mode.startswith(("container:", "ns:", "pasta", "slirp4netns")):
            raise Deny(f"network mode {mode} is not allowed")
        aliases = []
        for ep in (cget(spec.get("NetworkingConfig"), "EndpointsConfig") or {}).values():
            aliases += cget(ep, "Aliases") or []
        if self.deploy:
            self.deploy_compat_network(spec, hc)
        else:
            hc["NetworkMode"] = self.network
            spec["NetworkingConfig"] = {"EndpointsConfig": {self.network: {"Aliases": sorted(set(aliases))}}}
        binds = []
        for b in hc.get("Binds") or []:
            src, _, rest = str(b).partition(":")
            if src.startswith("/"):
                src = self.check_bind(src)
            else:
                self.named_volume(src, "bind")
            self.check_mount_options([o for o in rest.split(":", 1)[-1].split(",") if o] if ":" in rest else [],
                                     b)
            if src.startswith("/") and self.is_ro_root(src):         # a read-only grant (audit H3)
                dst, _, opts = rest.partition(":")
                opts = ",".join([o for o in opts.split(",") if o and o not in ("rw", "ro")] + ["ro"])
                rest = f"{dst}:{opts}"
            binds.append(f"{src}:{rest}" if rest else src)
        if hc.get("Binds"):
            hc["Binds"] = binds
        for m in hc.get("Mounts") or []:
            t = cget(m, "Type")
            if t == "bind":
                real = self.check_bind(cget(m, "Source"))
                cset(m, "Source", real)
                if self.is_ro_root(real):
                    cset(m, "ReadOnly", True)
            elif t == "volume":
                if cget(cget(m, "VolumeOptions") or {}, "DriverConfig"):
                    raise Deny("volume driver options are not allowed")
                self.named_volume(cget(m, "Source"), "mount")
            elif t != "tmpfs":
                raise Deny(f"{t!r} mounts are not allowed")
        hc.pop("RestartPolicy", None)
        if self.deploy:
            hc["RestartPolicy"] = {"Name": DEPLOY_RESTART}
        else:
            hc["CgroupParent"] = self.cfg["cgroup_parent"]
        hc["OomScoreAdj"] = self.oom_score
        if not hc.get("Memory"):
            hc["Memory"] = self.cfg["memory"]
        hc["MemorySwap"] = hc["Memory"]
        if not hc.get("PidsLimit"):
            hc["PidsLimit"] = self.cfg["pids"]
        spec["HostConfig"] = hc
        if self.deploy:
            self.check_compose_project(spec.get("Labels") or {}, required=True)
        spec["Labels"] = self.stamp(spec.get("Labels"))
        if not self.deploy:
            own = [spec.get("Hostname", "")] + aliases
            spec["Env"] = self.proxy_env(spec.get("Env"), as_list=True, own_names=own)
        return spec

    def deploy_compat_network(self, spec, hc):
        """Deploy mode, Docker API: NetworkMode one of the stack's networks (or none), every endpoint too."""
        mode = hc.get("NetworkMode") or ""
        eps = cget(spec.get("NetworkingConfig"), "EndpointsConfig") or {}
        if not isinstance(eps, dict):
            raise Deny("EndpointsConfig must be an object")
        if mode == "none":
            if eps:
                raise Deny("network mode none with networks")
            spec.pop("NetworkingConfig", None)
            return
        names = list(eps)
        if mode not in ("", "bridge", "default") and mode not in names:
            names.insert(0, mode)
        if not names:
            raise Deny("a deploy container must join one of the stack's own networks (podman's default network "
                       "is not the stack's)")
        out = {}
        for name in names:
            self.require_own_network(name)
            ep = eps.get(name) or {}
            for k, v in ep.items() if isinstance(ep, dict) else [("?", ep)]:
                if k.lower() != "aliases" and v:
                    raise Deny(f"network {name}: endpoint setting {k} is not allowed (aliases only)")
            out[name] = {"Aliases": sorted(set(cget(ep, "Aliases") or []))}
        hc["NetworkMode"] = names[0]
        spec["NetworkingConfig"] = {"EndpointsConfig": out}

    def pod_has_infra(self, ref):
        """The pod has an infra container whose network namespace its members share."""
        doc = self.inspect("pods", ref)
        return bool(doc and doc.get("InfraContainerID") and "net" in (doc.get("SharedNamespaces") or []))

    def rewrite_pod(self, spec):
        self.check_common_libpod(spec, POD_REFUSED, "pod")
        mode = cget(spec.get("netns"), "nsmode")
        if mode in NS_REFUSED + ("slirp4netns", "pasta"):
            raise Deny(f"pod netns {mode} is not allowed")
        for opt in spec.get("security_opt") or []:
            if not re.match(r"^no-new-privileges([:=]true)?$", str(opt)):
                raise Deny(f"pod security option {opt} is not allowed")
        if self.deploy and not spec.get("no_infra"):
            raise Deny("pods with an infra container are not supported in a deploy (podman-compose's default pods "
                       "have none)")
        if not spec.get("no_infra") and not self.cfg.get("infra_pods"):
            # The dev sockguard's owner policy cannot resolve pods (1 Oct: it refuses every container that
            # names one), so a pod's infra container would only ever be an empty network namespace. Refused;
            # podman-compose's default pods have no infra container.
            raise Deny("pods with an infra container are not supported in the dev tier (use --infra=false; "
                       "podman-compose's default)")
        if not spec.get("no_infra"):
            # A pod with an infra container owns the network its containers share: pin it here, and always
            # share it (a member outside it would get podman's default network). A pod without one
            # (podman-compose's default) has none; each container is pinned instead.
            spec["netns"] = {"nsmode": "bridge"}
            spec["Networks"] = {self.network: {}}
            share = spec.get("shared_namespaces")
            if share is not None and "net" not in share:
                spec["shared_namespaces"] = list(share) + ["net"]
        spec.pop("restart_policy", None)
        spec.pop("restart_tries", None)
        spec["labels"] = self.stamp(spec.get("labels"))
        if not self.deploy:
            spec["cgroup_parent"] = self.cfg["cgroup_parent"]
        return spec

    def rewrite_network(self, spec, *, compat):
        """Bridge, internal, isolate=strict, no IPv6, and nothing that picks addresses, interfaces or routes.
        Nothing attaches to it (containers are pinned to the session network); if anything did, it would have
        no route out."""
        if compat:
            spec = canonical(spec, COMPAT_NETWORK_KEYS, "network")
            if spec.get("Driver") not in (None, "", "bridge"):
                raise Deny(f"network driver {spec['Driver']} is not allowed")
            if spec.get("EnableIPv6") or spec.get("Ingress") or spec.get("ConfigFrom") or spec.get("ConfigOnly"):
                raise Deny("IPv6, ingress and config networks are not allowed")
            ipam = spec.get("IPAM") or {}
            if cget(ipam, "Config") or cget(ipam, "Driver") not in (None, "", "default") or cget(ipam, "Options"):
                raise Deny("choosing subnets or IPAM is not allowed")
            self.network_options(spec.pop("Options", None))
            spec.update({"Internal": bool(spec.get("Internal")) if self.deploy else True, "Driver": "bridge",
                         "EnableIPv6": False, "Labels": self.stamp(spec.get("Labels"))})
            return spec
        spec = canonical(spec, NETWORK_KEYS, "network")
        if spec.get("driver") not in (None, "", "bridge"):
            raise Deny(f"network driver {spec['driver']} is not allowed")
        for k in ("network_interface", "subnets", "routes", "ipam_options", "network_dns_servers", "ipv6_enabled"):
            if spec.get(k):
                raise Deny(f"network {k} is not allowed")
        self.network_options(spec.pop("options", None))
        spec.update({"internal": bool(spec.get("internal")) if self.deploy else True, "driver": "bridge",
                     "ipv6_enabled": False, "labels": self.stamp(spec.get("labels"))})
        return spec

    @staticmethod
    def network_options(opts):
        """Client network options are refused. The gate cannot add isolate=strict either: the dev sockguard
        refuses any network options (1 Oct). Nothing can attach to an agent-made network (containers are pinned
        to the session network and connect is refused), and it is internal."""
        if opts:
            raise Deny(f"network options {sorted(opts)} are not allowed")
        return None

    def rewrite_volume(self, spec, compat=False):
        spec = canonical(spec, VOLUME_KEYS, "volume")
        if spec.get("Driver") not in (None, "", "local") or spec.get("Options") or spec.get("DriverOpts"):
            raise Deny("volume drivers and options (bind-backed volumes) are not allowed")
        if spec.get("IgnoreIfExists"):
            raise Deny("IgnoreIfExists is not allowed (an existing volume would be reported as created)")
        if not re.match(r"^[A-Za-z0-9][\w.-]*$", spec.get("Name") or "x"):
            raise Deny(f"volume name {spec.get('Name')!r} is not allowed")
        if self.deploy:
            self.deploy_volume_name(spec.get("Name"), "volume create")
        # podman's libpod API reads "Label", the Docker API "Labels"; the other spelling is ignored, so a
        # stamp under the wrong one would leave the volume unlabelled.
        key, other = ("Labels", "Label") if compat else ("Label", "Labels")
        spec[key] = self.stamp(spec.get(key) or spec.get(other))
        spec.pop(other, None)
        return spec

    # ---- dispatch ---------------------------------------------------------------------------------
    def decide(self, method, target, headers, body):
        """Returns (target, body, kind) to forward, or raises Deny."""
        path = canonical_path(target)
        tail = TAIL.match(path).group(1) if TAIL.match(path) else path
        for m, rx, why in DENIED:
            if (m is None or m == method) and rx.match(tail):
                raise Deny(f"{why} is not allowed in the dev tier")
        if method in ("GET", "HEAD"):
            m = IMAGE_READ.match(path)
            if m:
                self.check_image_read(m.group(1))
            if PODS_LIST.match(path):
                raise LocalReply(self.list_pods(target))
        if method == "POST":
            if RE["compat_create"].match(path) and not self.cfg.get("compat_creates"):
                raise Deny("Docker-API container creates are not supported in the dev tier: sockguard refuses the "
                           "cgroup parent that keeps a container in agentbox.slice. Use podman or podman-compose")
            for kind, fn, known in (("libpod_create", self.rewrite_libpod_container, LIBPOD_KEYS),
                                    ("compat_create", self.rewrite_compat_container, COMPAT_KEYS),
                                    ("pod_create", self.rewrite_pod, POD_KEYS)):
                if RE[kind].match(path):
                    spec = canonical(strict_loads(body, {"libpod_create": "libpod", "compat_create": "compat",
                                                         "pod_create": "pod"}[kind]), known, kind.split("_")[0])
                    return target, json.dumps(fn(spec)).encode(), kind
            if RE["net_create"].match(path):
                q = parse_qsl(urlsplit(target).query)
                # Fail closed on the spelling: podman decodes the flag with gorilla/schema, which takes "on" and
                # every strconv.ParseBool true ("t", "T", "1", "TRUE", ...); only a plain false passes (review
                # of the gate's error paths, 17 Nov).
                if any(k.lower() == "ignoreifexists" and v.lower() not in ("", "0", "f", "false") for k, v in q):
                    raise Deny("ignoreIfExists is not allowed (an existing network would be reported as created)")
                spec = self.rewrite_network(strict_loads(body, "network"), compat="/libpod/" not in path)
                return target, json.dumps(spec).encode(), "net_create"
            if RE["vol_create"].match(path):
                spec = self.rewrite_volume(strict_loads(body, "volume"), compat="/libpod/" not in path)
                return target, json.dumps(spec).encode(), "vol_create"
            if RE["build"].match(path):
                return self.rewrite_build(target), body, "build"
            if RE["pull"].match(path):
                return self.rewrite_pull(target, path), body, "pull"
            if tail.endswith("/create"):
                raise Deny("an unrecognised create endpoint")
        m = RE["exec_op"].match(path)
        if m:
            with self.lock:
                cid = self.exec_ids.get(m.group(1))
                ok = cid is not None and (cid not in self.refused if self.deploy else cid in self.verified)
                if ok and m.group(2) == "remove":
                    self.exec_ids.pop(m.group(1), None)        # removed (or failed closed: unusable either way)
            if not ok:
                raise Deny(f"exec session {m.group(1)[:12]} was not created through this gate on a checked "
                           "container")
            return target, body, "exec_op"
        m = RE["exec_create"].match(path)
        if m:
            self.require_dev("containers", m.group(1))
            return target, body, "exec_create"
        if method == "POST":
            for kind in ("start", "pod_start"):
                m = RE[kind].match(path)
                if m:
                    self.check_start(kind, m.group(1), m.group(2))
                    if m.group(2) == "restart":
                        # already stopped and re-initialised by the gate, with its mounts checked: start it
                        parts = urlsplit(target)
                        target = re.sub(r"/restart$", "/start", parts.path) + (f"?{parts.query}" if parts.query else "")
                    if kind == "pod_start":          # to the podman service, by the checked ID (M1)
                        target = by_id(target, "pods", self.require_dev("pods", m.group(1)))
                    return target, body, kind
        m = RESOURCE.match(path)
        if m and m.group(2) not in COLLECTION:
            kind, ref = m.group(1), m.group(2)
            if kind == "networks" and method == "GET" and ref == self.network:
                return target, body, "pass"           # compose inspects the network it was told to use
            if kind in ("volumes", "networks") and ref in self.external_ok and method in ("GET", "HEAD"):
                # deploy: podman-compose inspects an external volume or network before using it. Read on the raw
                # service (the deploy sockguard's owner isolation would hide it); nothing else may touch it.
                return target, body, "ext_read"
            oid = self.require_dev(kind, ref)
            if kind == "containers" and (method == "DELETE" or LIFECYCLE.match(path)):
                self.hold(oid)                       # waits for a start in progress (N1)
            if kind == "pods" and (method == "DELETE" or POD_LIFECYCLE.match(path)):
                for c in (self.inspect("pods", oid) or {}).get("Containers") or []:
                    if c.get("Id"):
                        self.hold(c["Id"])
            if kind == "pods" and POD_RAW.match(path) and (method, POD_RAW.match(path).group(1) or "") in POD_RAW_OPS:
                # straight to the podman service: by the ID that was checked, never the client's name, which
                # could name another pod by the time it arrives (review of 785ee15, M1)
                return by_id(target, "pods", oid), body, "pod_raw"
        return target, body, "pass"


def signal_number(value):
    """SIGINT, INT, 2 -> 2; None when unknown."""
    import signal as _signal
    if value is None or value == "":
        return None
    v = str(value).strip().upper()
    if v.isdigit():
        return int(v)
    try:
        return int(_signal.Signals[v if v.startswith("SIG") else "SIG" + v])
    except (KeyError, ValueError):
        return None


def created_name(kind, target, body):
    """The name a create asked for (to find the object if podman's reply cannot be read)."""
    if kind == "compat_create":
        return dict(parse_qsl(urlsplit(target).query)).get("name")
    try:
        doc = json.loads(body or b"{}")
    except ValueError:
        return None
    return cget(doc, "name") if isinstance(doc, dict) else None


def by_id(target, kind, oid):
    """The request target with the object segment after /<kind>/ replaced by its checked ID."""
    parts = urlsplit(target)
    path = re.sub(rf"(/{kind}/)[^/]+", lambda m: m.group(1) + quote(oid, safe=""), parts.path, count=1)
    return path + (f"?{parts.query}" if parts.query else "")


def make_handler(gate):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if not gate.slots.acquire(blocking=False):
                gate.log("deny", "-", "-", f"over {MAX_CONNECTIONS} connections")
                try:
                    deny(self.request, "too many connections to the gate; try again", 503)
                except OSError:
                    pass
                return
            try:
                self.request.settimeout(CLIENT_TIMEOUT)
                self.serve_one()
            finally:
                gate.slots.release()

        def serve_one(self):
            client, upstream = self.request, None
            method = path = "?"
            gate.begin_request()
            try:
                head, rest = read_head(client)
                if head is None:
                    return
                method, target, version, headers = parse_head(head)
                path = canonical_path(target)
                if sum(k.lower() == "content-length" for k, _ in headers) > 1 or \
                        (hget(headers, "Content-Length") and hget(headers, "Transfer-Encoding")):
                    raise Deny("ambiguous request framing (Content-Length with Transfer-Encoding, or twice)")
                te = (hget(headers, "Transfer-Encoding") or "").lower()
                if te and te != "chunked":
                    raise Deny(f"transfer encoding {te} is not supported")
                upgrade = hget(headers, "Upgrade") if UPGRADE.match(path) else None
                headers = hset(headers, "Upgrade", upgrade)
                headers = hset(headers, "Expect", None)
                if not upgrade:
                    headers = hset(headers, "Connection", "close")
                is_build = bool(RE["build"].match(path))
                has_body = hget(headers, "Content-Length") or te == "chunked"
                body = b""
                if has_body and not is_build:
                    body, rest = read_body(client, headers, rest)
                    headers = hset(headers, "Transfer-Encoding", None)
                    headers = hset(headers, "Content-Length", str(len(body)))
                if rest and not upgrade and not is_build:
                    raise Deny("pipelined requests are not accepted (one request per connection)")
                target, new_body, kind = gate.decide(method, target, headers, body)
                if new_body is not body:
                    body = new_body
                    headers = hset(headers, "Content-Length", str(len(body)))
                gate.log("allow", method, path, kind if kind not in ("pull", "build") else f"{kind} {target}")
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.settimeout(UPSTREAM_TIMEOUT)
                # pod_start and pod_raw: a pod the session owns, already checked; sockguard cannot resolve pods
                dest = gate.cfg["raw"] if kind in ("pod_start", "pod_raw", "ext_read") else gate.cfg["upstream"]
                if not is_build:
                    upstream.connect(dest)
                if kind in CREATES:
                    upstream.sendall(build_head(method, target, version, headers) + body)
                    reply = gate.verify_created(kind, recv_all(upstream), created_name(kind, target, body))
                    client.sendall(build_response(*reply))
                    return
                if kind in ("exec_create", "start", "pod_start"):
                    upstream.sendall(build_head(method, target, version, headers) + body)
                    status, reason, hdrs, resp = parse_response(recv_all(upstream))
                    if 200 <= status < 300:
                        ref = (RE["exec_create"].match(path) or RE["start"].match(path)
                               or RE["pod_start"].match(path)).group(1)
                        if kind == "exec_create":
                            exec_id = (json.loads(resp or b"{}") or {}).get("Id")
                            if exec_id:
                                cid = gate.require_dev("containers", ref)
                                gate.remember_exec(exec_id, cid)
                        else:
                            gate.after_start_checked()
                    client.sendall(build_response(status, reason, hdrs, resp))
                    return
                if is_build:
                    # spooled whole (to the session's TMPDIR), so its Containerfile can be read, then forwarded
                    spool_dir = gate.cfg.get("spool") or None
                    if spool_dir:
                        os.makedirs(spool_dir, mode=0o700, exist_ok=True)
                    with tempfile.TemporaryFile(dir=spool_dir) as f:
                        size = spool_body(client, headers, rest, f)
                        f.flush()
                        gate.check_build_images(f"/proc/self/fd/{f.fileno()}", target)
                        headers = hset(hset(headers, "Transfer-Encoding", None), "Content-Length", str(size))
                        target = gate.finalize_build(target)      # holder PID re-checked after the spool (item 11)
                        # Connected only now: spooling and checking a many-file context took 8-11 s, and
                        # sockguard closed the idle connection before its head arrived (16 Nov).
                        upstream.connect(dest)
                        sent = 0
                        try:
                            upstream.sendall(build_head(method, target, version, headers))
                            f.seek(0)
                            for block in iter(lambda: f.read(1 << 20), b""):
                                upstream.sendall(block)
                                sent += len(block)
                        except (BrokenPipeError, ConnectionResetError):
                            # upstream stopped reading: say what it answered, not "BrokenPipeError" (16 Nov)
                            status, msg = build_cut_off_message(early_reply(upstream), size, sent)
                            e = Deny(msg)
                            e.status = status
                            raise e from None
                    upstream.settimeout(None)
                    client.settimeout(None)               # the build itself can run for a long time
                    # only the ID the reply ends with is recorded, before the client sees that end, so a build
                    # started right after it already finds this one in the record
                    started = time.time()
                    pipe_build_reply(upstream, client, lambda image_id: gate.record_build(image_id, started))
                    return
                upstream.sendall(build_head(method, target, version, headers) + body)
                upstream.settimeout(None)
                client.settimeout(None)                   # long-lived by design: attach, exec, wait, logs -f
                if upgrade:
                    if rest:
                        upstream.sendall(rest)        # the upgraded stream (exec start, attach) has begun
                    pipe(client, upstream)
                    return
                dropped = pipe_reply(upstream, client)
                if dropped:
                    gate.log("deny", method, path, f"{dropped} bytes after the request were dropped, not forwarded "
                             "(one request per connection)")
            except LocalImage as e:
                gate.log("allow", method, path, f"pull answered locally: {e.image_id[:12]} is present")
                send_json(client, (json.dumps({"images": [e.image_id], "id": e.image_id}) + "\n").encode())
            except LocalReply as e:
                gate.log("allow", method, path, "answered by the gate, filtered to the session's objects")
                send_json(client, e.body)
            except Deny as e:
                gate.log({403: "deny", 404: "notfound"}.get(e.status, "error"), method, path, str(e))
                try:
                    deny(client, str(e), e.status)
                except OSError:
                    pass
            except (AgentboxError, ValueError, OSError, http.client.HTTPException) as e:
                gate.log("error", method, path, repr(e))
                try:
                    deny(client, f"request refused ({e})")
                except OSError:
                    pass
            except Exception as e:
                # A bug (TypeError on an odd body, say) is a refusal, not a traceback and a silent drop (item 44)
                gate.log("error", method, path, f"internal error {e!r}")
                try:
                    deny(client, f"request refused (internal error: {type(e).__name__})")
                except OSError:
                    pass
            finally:
                gate.release_held()                  # per-container locks of this request (N1)
                for s in (upstream, client):
                    if s is not None:
                        try:
                            s.close()
                        except OSError:
                            pass
    return Handler


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve(cfg):
    gate = Gate(cfg)
    try:
        os.unlink(cfg["listen"])
    except FileNotFoundError:
        pass
    old = os.umask(0o177)
    server = Server(cfg["listen"], make_handler(gate))
    os.umask(old)
    gate.log("start", "-", cfg["listen"], f"upstream {cfg['upstream']}")
    server.serve_forever()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.exit("usage: python3 -m agentbox.gate <config.json>")
    with open(argv[0]) as f:
        cfg = json.load(f)
    serve(cfg)


if __name__ == "__main__":
    main()
