"""The host-side gate: the only podman socket a dev-tier agent gets.

    agent --/run/agentbox/podman.sock--> gate --> sockguard (dev profile, owner isolation) --> podman service
                                          \\--> podman service (the gate's own inspects only)

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
    resolves inside the volume (a symlink swapped in before start cannot lead out), named volumes only the session's own (a new name is created labelled
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
  * start/restart/init (and pod start/restart): the container is checked again from what podman stored,
    with bind sources re-resolved (a directory swapped for a symlink after create is refused); after the
    start, what the runtime actually mounted is read from /proc/<pid>/mountinfo and a bind from outside
    the grants gets the container removed;
  * build: the query must use known keys only; RUN steps join the session holder's network namespace (the
    holder's PID re-checked every build), the client's namespace options dropped except `user`, never
    pulls, capped by buildcap, proxy passed as build args, labelled; tags only localhost/ names, never
    localhost/agentbox-*, and never a name an image outside this project already has; build secrets only
    from inside the context;
  * image pull: allowlisted registries only; short names become docker.io names;
  * exec sessions can be started, resized, inspected or removed only if they were created through the gate
    on a container that passed its check.

Everything else is forwarded to sockguard, whose rules and owner isolation apply on top.
"""
import fnmatch
import http.client
import io
import json
import os
import re
import socket
import socketserver
import sys
import tarfile
import tempfile
import threading
import time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from . import buildcap
from .errors import AgentboxError

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
UPGRADE = re.compile(rf"^{V}{LIB}/(?:exec/[^/]+/start|containers/[^/]+/attach)$")
TAIL = re.compile(rf"^{V}{LIB}(/.*)$")
# Collection paths whose second segment is an action word, not a resource name.
COLLECTION = {"json", "create", "prune", "stats", "load", "import", "pull", "search", "export", "exists",
              "showmounts", "kube"}
RESOURCE = re.compile(rf"^{V}{LIB}/(containers|pods|volumes|networks)/([^/]+)(?:/.*)?$")

# Refused by the gate itself (sockguard refuses them too; this keeps them refused if its rules ever loosen).
# Matched on the path without its version and /libpod prefix.
DENIED = [(m, re.compile(p), why) for m, p, why in (
    (None, r"^/images/.+/(push|tag|untag)$", "pushing or retagging images"),
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

DEV_OWNER = "agentbox.dev-owner"     # sockguard's ownership label key for the dev tier
UPSTREAM_TIMEOUT = 300               # builds and pulls can be slow on this host
MAX_HEAD = 1 << 20
MAX_BODY = 64 << 20
MAX_BUILD_BODY = 8 << 30             # a build context, spooled to the session's TMPDIR to read its Containerfile

# podman's build API: networkmode is buildah's NetworkConfigurationPolicy as a number
NET_ENABLED = "2"
NS_KEYS = ("networkmode", "nsoptions")
REFUSED_BUILD_KEYS = ("cgroupparent",)
DROPPED_BUILD_KEYS = ("pull", "pullpolicy", "labels", "buildargs", "httpproxy", "seccomp", "apparmor", "isolation",
                      "idmappingoptions", "cgroupns", "ipcns", "pidns", "utsns", "userns", "devices", "capadd",
                      "privileged", "securityopt", "addhost", "dnsservers", "dnsoptions", "dnssearch", "labelopts",
                      "tlsverify")
# Build query keys passed through (after the checks below). Anything not listed here or above is refused:
# addcaps, volume, remote, additionalbuildcontexts, cachefrom/cacheto (registry access), outputs (writes on the
# host), rusagelogfile (a host path), registry, manifest, allplatforms, groupadd, ssh.
BUILD_KEYS = ("dockerfile", "t", "output", "outputformat", "forcerm", "rm", "q", "nocache", "layers", "jobs",
              "target", "platform", "squash", "squashall", "inheritannotations", "inheritlabels", "annotations",
              "unsetannotation", "unsetlabel", "unsetenv", "setenv", "envs", "timestamp", "sourcedateepoch",
              "rewritetimestamp", "identitylabel", "omithistory", "createdannotation", "skipunusedstages",
              "compatvolumes", "excludes", "ignorefile", "dropcaps", "nohosts", "extrahosts", "shmsize", "ulimits",
              "retry", "retry-delay", "secrets", "memory", "memswap", "cpushares", "cpusetcpus", "cpuperiod",
              "cpuquota", "cpusetmems", "osfeature", "osversion", "layerlabel", "osversion", "logsplit")
SECRET_SRC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class Deny(Exception):
    pass


# Podman decodes JSON field names case-insensitively, so "PortMappings" sets portmappings. Found 1 Oct:
# "PortMappings" got a published host port past the gate and sockguard. Bodies are parsed strictly (two keys
# that differ only in case are refused), top-level fields are renamed to their exact spelling (unknown ones
# refused), and nested fields are read case-insensitively. Values of maps the client owns (env, labels,
# annotations) are never renamed.
LIBPOD_KEYS = (
    "name", "pod", "entrypoint", "command", "env", "terminal", "stdin", "labels", "annotations", "stop_signal",
    "stop_timeout", "timeout", "log_configuration", "raw_image_name", "restart_policy", "restart_tries", "systemd",
    "sdnotifyMode", "pidns", "utsns", "hostname", "hostusers", "sysctl", "remove", "removeImage",
    "containerCreateCommand", "personality", "httpproxy", "envmerge", "unsetenv", "unsetenvall", "manage_password",
    "healthconfig", "health_check_on_failure_action", "startupHealthConfig", "healthLogDestination",
    "healthMaxLogCount", "healthMaxLogSize", "image", "image_volume_mode", "image_volumes", "image_arch",
    "image_os", "image_variant", "mounts", "volumes", "init", "ipcns", "shm_size", "shm_size_systemd", "work_dir",
    "create_working_dir", "chroot_directories", "privileged", "user", "groups", "cap_add", "cap_drop",
    "selinux_opts", "apparmor_profile", "seccomp_policy", "seccomp_profile_path", "no_new_privileges", "userns",
    "idmappings", "read_only_filesystem", "read_write_tmpfs", "label_nested", "procfs_opts", "mask", "unmask",
    "umask", "cgroupns", "cgroups_mode", "cgroup_parent", "netns", "portmappings", "publish_image_ports", "expose",
    "Networks", "cni_networks", "dns_server", "dns_search", "dns_option", "use_image_resolve_conf",
    "use_image_hosts", "hostadd", "base_hosts_file", "network_options", "resource_limits", "r_limits",
    "oom_score_adj", "weightDevice", "throttleReadBpsDevice", "throttleWriteBpsDevice", "throttleReadIOPSDevice",
    "throttleWriteIOPSDevice", "intelRdt", "devices", "device_cgroup_rule", "devices_from", "rootfs",
    "rootfs_overlay", "rootfs_propagation", "rootfs_mapping", "overlay_volumes", "artifact_volumes",
    "volumes_from", "oci_runtime", "conmon_pid_file", "init_path", "env_host", "secrets", "secret_env",
    "dependencyContainers", "timezone", "passwd_entry", "group_entry", "init_container_type", "storage_opts",
    "use_image_hostname", "hostname_path", "stop_after", "volatile")
# Refused whenever set to anything but an empty value.
LIBPOD_REFUSED = {
    "privileged": "privileged containers", "cap_add": "added capabilities", "devices": "devices",
    "device_cgroup_rule": "device cgroup rules", "devices_from": "devices", "portmappings": "published ports",
    "publish_image_ports": "published ports", "cgroup_parent": "a client-chosen cgroup parent",
    "unmask": "unmasked kernel paths", "apparmor_profile": "AppArmor profiles", "seccomp_profile_path":
    "seccomp profiles", "selinux_opts": "SELinux options", "label_nested": "nested SELinux labels",
    "rootfs": "a host directory as root filesystem", "rootfs_overlay": "a host directory as root filesystem",
    "rootfs_mapping": "a host directory as root filesystem", "overlay_volumes": "overlay volumes (host paths)",
    "artifact_volumes": "artifact volumes", "volumes_from": "volumes from other containers",
    "oci_runtime": "a client-chosen OCI runtime", "conmon_pid_file": "a conmon PID file on the host",
    "init_path": "an init binary from the host", "env_host": "the podman service's environment",
    "secrets": "podman secrets", "secret_env": "podman secrets", "dependencyContainers": "dependency containers",
    "storage_opts": "storage options"}
POD_KEYS = (
    "name", "hostname", "exit_policy", "labels", "no_infra", "infra_name", "pod_create_command", "pidns", "ipcns",
    "utsns", "userns", "cgroupns", "restart_policy", "restart_tries", "shared_namespaces", "share_parent",
    "sysctl", "netns", "portmappings", "Networks", "network_options", "cni_networks", "dns_server", "dns_search",
    "dns_option", "no_manage_resolv_conf", "no_manage_hosts", "hostadd", "base_hosts_file", "cgroup_parent",
    "resource_limits", "cpu_period", "cpu_quota", "throttleReadBpsDevice", "mounts", "volumes", "image_volumes",
    "overlay_volumes", "volumes_from", "shm_size", "shm_size_systemd", "security_opt", "idmappings",
    "pod_devices", "infra_image", "infra_command", "infra_conmon_pid_file", "serviceContainerID",
    "use_image_hosts", "use_image_resolve_conf", "no_manage_hostname")
POD_REFUSED = {
    "portmappings": "published ports", "cgroup_parent": "a client-chosen cgroup parent",
    "overlay_volumes": "overlay volumes (host paths)", "volumes_from": "volumes from other containers",
    "pod_devices": "devices", "infra_image": "a client-chosen infra image", "infra_command": "an infra command",
    "infra_conmon_pid_file": "a conmon PID file on the host", "serviceContainerID": "service containers"}
COMPAT_KEYS = (
    "Hostname", "Domainname", "User", "AttachStdin", "AttachStdout", "AttachStderr", "ExposedPorts", "Tty",
    "OpenStdin", "StdinOnce", "Env", "Cmd", "Healthcheck", "ArgsEscaped", "Image", "Volumes", "WorkingDir",
    "Entrypoint", "NetworkDisabled", "MacAddress", "OnBuild", "Labels", "StopSignal", "StopTimeout", "Shell",
    "HostConfig", "NetworkingConfig", "EnvMerge", "UnsetEnv", "UnsetEnvAll", "Platform")
HOSTCONFIG_KEYS = (
    "Binds", "ContainerIDFile", "LogConfig", "NetworkMode", "PortBindings", "RestartPolicy", "AutoRemove",
    "VolumeDriver", "VolumesFrom", "ConsoleSize", "Annotations", "CapAdd", "CapDrop", "CgroupnsMode", "Dns",
    "DnsOptions", "DnsSearch", "ExtraHosts", "GroupAdd", "IpcMode", "Cgroup", "Links", "OomScoreAdj", "PidMode",
    "Privileged", "PublishAllPorts", "ReadonlyRootfs", "SecurityOpt", "StorageOpt", "Tmpfs", "UTSMode",
    "UsernsMode", "ShmSize", "Sysctls", "Runtime", "Isolation", "CpuShares", "Memory", "NanoCpus", "CgroupParent",
    "BlkioWeight", "BlkioWeightDevice", "BlkioDeviceReadBps", "BlkioDeviceWriteBps", "BlkioDeviceReadIOps",
    "BlkioDeviceWriteIOps", "CpuPeriod", "CpuQuota", "CpuRealtimePeriod", "CpuRealtimeRuntime", "CpusetCpus",
    "CpusetMems", "Devices", "DeviceCgroupRules", "DeviceRequests", "KernelMemory", "KernelMemoryTCP",
    "MemoryReservation", "MemorySwap", "MemorySwappiness", "OomKillDisable", "PidsLimit", "Ulimits", "CpuCount",
    "CpuPercent", "IOMaximumIOps", "IOMaximumBandwidth", "Mounts", "MaskedPaths", "ReadonlyPaths", "Init")
HOSTCONFIG_REFUSED = {
    "PortBindings": "published ports", "PublishAllPorts": "published ports", "Privileged": "privileged containers",
    "CapAdd": "added capabilities", "Devices": "devices", "DeviceRequests": "devices",
    "DeviceCgroupRules": "device cgroup rules", "CgroupParent": "a client-chosen cgroup parent",
    "VolumeDriver": "volume drivers", "VolumesFrom": "volumes from other containers",
    "Runtime": "a client-chosen OCI runtime", "MaskedPaths": "changed masked paths",
    "ReadonlyPaths": "changed read-only paths", "StorageOpt": "storage options", "Cgroup": "cgroup sharing",
    "OomKillDisable": "disabling the OOM killer"}
VOLUME_KEYS = ("Name", "Driver", "Options", "DriverOpts", "Labels", "Label", "UID", "GID", "IgnoreIfExists")
NETWORK_KEYS = ("name", "driver", "internal", "labels", "options", "dns_enabled", "ipv6_enabled", "id", "created",
                "network_interface", "subnets", "routes", "network_dns_servers", "ipam_options")
COMPAT_NETWORK_KEYS = ("Name", "CheckDuplicate", "Driver", "Scope", "EnableIPv4", "EnableIPv6", "IPAM", "Internal",
                       "Attachable", "Ingress", "ConfigOnly", "ConfigFrom", "Options", "Labels")

# Annotations with an effect on the runtime or OCI hooks (crun reads run.oci.*, e.g. keep_original_groups).
# podman's own informational io.podman.annotations.* are dropped from requests; podman re-adds some.
ANNOTATION_REFUSED = ("run.oci.", "io.kubernetes.", "io.containers.", "org.systemd.", "io.crun.", "module.wasm.",
                      "org.opencontainers.")
ANNOTATION_DROPPED = "io.podman.annotations."
SYSCTL_ALLOWED = ()                  # none: the dev sockguard refuses every sysctl (1 Oct), so the gate does too
INFRA_ROOTFS = "/libpod/tmp/infra-container"
ROLE_GRANT = "grant"                 # the gate's bind-backed volumes over the grant roots (sub-path binds)
VOLUME_OPTS = ("ro", "rw", "z", "Z", "U", "idmap", "noexec", "exec", "nosuid", "suid", "nodev", "dev")
NS_REFUSED = ("host", "path", "container", "ns")
MOUNT_OPTION_OK = re.compile(r"^(r?(shared|slave|private|unbindable)|ro|rw|nosuid|suid|nodev|dev|noexec|exec|"
                             r"r?bind|z|Z|U|idmap(=.*)?|nocopy|copy|notmpcopyup|tmpcopyup|size=\d+[kmgKMG]?|"
                             r"mode=[0-7]{3,4}|uid=\d+|gid=\d+|relatime|norelatime|noatime|strictatime|chown|"
                             r"src-nofollow|dest-nofollow)$")


def strict_loads(body):
    """JSON whose objects never hold two keys that differ only in case (podman would see just one of them)."""
    def pairs(items):
        seen = {}
        for k, _ in items:
            if k.lower() in seen:
                raise Deny(f"ambiguous request: keys {seen[k.lower()]!r} and {k!r} differ only in case")
            seen[k.lower()] = k
        return dict(items)
    try:
        doc = json.loads(body or b"{}", object_pairs_hook=pairs)
    except ValueError as e:
        if isinstance(e.__context__, Deny):
            raise e.__context__
        raise Deny(f"the request body is not valid JSON ({e})")
    if not isinstance(doc, dict):
        raise Deny("the request body must be a JSON object")
    return doc


def canonical(obj, known, what):
    """Rename an object's own keys to their exact spelling; refuse keys the gate does not know. Values are
    left as they are (nested fields are read with cget)."""
    if not isinstance(obj, dict):
        raise Deny(f"{what} must be a JSON object")
    canon = {k.lower(): k for k in known}
    out = {}
    for k, v in obj.items():
        if k.lower() not in canon:
            raise Deny(f"{what} field {k!r} is not one the gate knows; refused")
        out[canon[k.lower()]] = v
    return out


def cget(d, key, default=None):
    """d[key] matched case-insensitively (strict_loads guarantees at most one match)."""
    if isinstance(d, dict):
        for k, v in d.items():
            if k.lower() == key.lower():
                return v
    return default


def cset(d, key, value):
    for k in [k for k in d if k.lower() == key.lower()]:
        del d[k]
    d[key] = value


def cpop(d, key):
    for k in [k for k in d if k.lower() == key.lower()]:
        del d[k]


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


def image_name(ref):
    """A build tag as podman stores it: localhost/<name>:<tag> unless a registry is given."""
    ref = ref.strip()
    first = ref.split("/", 1)[0]
    if "/" not in ref or not ("." in first or ":" in first or first == "localhost"):
        ref = "localhost/" + ref
    last = ref.rsplit("/", 1)[-1]
    if ":" not in last and "@" not in last:
        ref += ":latest"
    return ref


class Gate:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = cfg["session"]
        self.network = cfg["network"]
        self.labels = dict(cfg["labels"])
        self.exec_ids = {}               # exec session id -> container id
        self.verified = set()            # IDs (volume names) whose post-create check passed
        self.refused = set()             # IDs that failed it and could not be confirmed removed
        self.lock = threading.Lock()
        self.log_path = cfg.get("log")

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
        status, doc = self.raw_json(f"/libpod/{kind}/{q}/json")
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
        return labels.get(DEV_OWNER) == self.session and labels.get("agentbox.role") == "dev"

    def labels_of(self, kind, ref):
        doc = self.inspect(kind, ref)
        return None if doc is None else self.labels_in(kind, doc)

    def require_dev(self, kind, ref):
        """The object must carry the dev labels and must have passed this gate's post-create check.
        Returns its ID (a volume's name)."""
        doc = self.inspect(kind, ref)
        if doc is None:
            raise Deny(f"{kind[:-1]} {ref!r} not found")
        if not self.is_dev(self.labels_in(kind, doc)):
            raise Deny(f"{kind[:-1]} {ref!r} is not part of this session's dev stack")
        oid = self.ident(kind, doc)
        with self.lock:
            ok = oid in self.verified
        if not ok:
            raise Deny(f"{kind[:-1]} {ref!r} has not passed the gate's post-create check")
        return oid

    def remove(self, kind, oid, why):
        """Remove an object the gate refused; check the outcome. An object that may still exist stays on the
        refused list (it is never in `verified`, so nothing can use it)."""
        path = {"containers": f"/libpod/containers/{quote(oid, safe='')}?force=true&v=true",
                "pods": f"/libpod/pods/{quote(oid, safe='')}?force=true",
                "volumes": f"/libpod/volumes/{quote(oid, safe='')}?force=true",
                "networks": f"/libpod/networks/{quote(oid, safe='')}?force=true"}[kind]
        with self.lock:
            self.verified.discard(oid)
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

    def _roots(self):
        return [os.path.realpath(r) for r in self.cfg["bind_roots"]]

    def _inside_roots(self, src):
        if not isinstance(src, str) or not src.startswith("/"):
            return False
        real = os.path.realpath(src)
        return any(real == r or real.startswith(r.rstrip("/") + "/") for r in self._roots())

    def _is_root(self, src):
        """Only a grant root itself may be bind-mounted (Tim, 1 Oct): podman resolves a bind source again when
        the container starts, and a path below a root is one the agent can swap for a symlink in between. A
        grant root is a mount point in the agent's container, so the agent cannot replace it."""
        return isinstance(src, str) and src.startswith("/") and os.path.realpath(src) in self._roots()

    # ---- post-create checks (what podman stored) ---------------------------------------------------
    def privilege_violations(self, doc, infra=False):
        """Shared by containers and pod infra containers."""
        hc = doc.get("HostConfig") or {}
        bad = []
        if any(v for v in (hc.get("PortBindings") or {}).values()):
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
        for k in annotations:
            if k.startswith(ANNOTATION_REFUSED):
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
        if doc.get("Dependencies"):
            bad.append("dependency containers")
        if hc.get("VolumesFrom"):
            bad.append("volumes from other containers")
        bad += self.mount_rule_violations(doc.get("Mounts") or [], cid)
        return bad

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
            if t == "bind":
                if not self._is_root(m.get("Source", "")):
                    bad.append(f"bind {m.get('Source')} (only a grant root itself)")
            elif t == "volume":
                name = m.get("Name") or ""
                vdoc = self.inspect("volumes", name) if name else None
                labels = self.labels_in("volumes", vdoc) if vdoc else None
                if labels is None or not (self.is_dev(labels) or self.anonymous_volume(name, labels)
                                          or self.grant_volume_root(vdoc)):
                    bad.append(f"volume {name or m.get('Source')} is not this session's")
            elif t not in ("tmpfs", "image"):
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
        inside = [r for r in self._roots() if real.startswith(r.rstrip("/") + "/")]
        if not inside or self._is_root(real):
            return None
        root = max(inside, key=len)
        return self.grant_volume(root), os.path.relpath(real, root)

    @staticmethod
    def anonymous_volume(name, labels):
        """podman's anonymous volumes have a 64-hex name and no labels."""
        return bool(re.fullmatch(r"[0-9a-f]{64}", name or "")) and not labels

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
        if self.cfg.get("cgroup_parent") and self.cfg["cgroup_parent"] not in (hc.get("CgroupParent") or ""):
            bad.append(f"cgroup parent {hc.get('CgroupParent')}")
        if (hc.get("RestartPolicy") or {}).get("Name") not in (None, "", "no"):
            bad.append("a restart policy")
        if hc.get("OomScoreAdj") != 1000:
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
        hc = doc.get("HostConfig") or {}
        bad = []
        mode = hc.get("NetworkMode") or ""
        if mode not in ("bridge", self.network):
            bad.append(f"NetworkMode {mode}")
        nets = set(((doc.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        if nets != {self.network}:
            bad.append(f"networks {sorted(nets)}")
        return bad

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
        if doc.get("InfraContainerID"):
            if "net" not in (doc.get("SharedNamespaces") or []):
                bad.append("an infra container that does not share its network")
            if set(infra.get("Networks") or []) != {self.network}:
                bad.append(f"networks {infra.get('Networks')}")
        if self.cfg.get("cgroup_parent") and self.cfg["cgroup_parent"] not in (doc.get("CgroupParent") or ""):
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
        if not (doc.get("internal") or doc.get("Internal")):
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

    def check_start(self, kind, ref):
        ids = self.start_targets(kind, ref)
        infra = ""
        if kind == "pod_start":
            infra = (self.inspect("pods", ref) or {}).get("InfraContainerID") or ""
        for cid in ids:
            with self.lock:
                ok = cid in self.verified
            if not ok:
                raise Deny(f"container {cid[:12]} has not passed the gate's post-create check")
            doc = self.inspect("containers", cid)
            if doc is None:
                raise Deny(f"container {cid[:12]} not found")
            bad = self.infra_violations(doc) if cid == infra else self.container_violations(doc)
            if bad:
                raise Deny(f"container {cid[:12]} no longer matches the rules ({'; '.join(bad)})")
        return ids

    def after_start(self, ids):
        """Read what the runtime mounted; a bind from outside the grants (a path swapped between the check
        and the mount) gets the container removed."""
        for cid in ids:
            doc = self.inspect("containers", cid)
            bad = self.mount_violations(doc) if doc else []
            if bad:
                self.remove("containers", cid, "; ".join(bad))
                raise Deny(f"container {cid[:12]} mounted something outside the project ({'; '.join(bad)}); "
                           "removed it")

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
                if real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/":
                    if best is None or len(mp) >= len(best[0]):
                        best = (mp, dev, root)
            if best:
                mp, dev, root = best
                rel = real[len(mp):].lstrip("/") if mp != "/" else real.lstrip("/")
                loc = (dev, os.path.join(root, rel) if rel else root)
                out.append(loc + (real,) if with_root else loc)
        return out

    def mount_violations(self, doc):
        pid = (doc.get("State") or {}).get("Pid")
        binds = [m for m in doc.get("Mounts") or [] if m.get("Type") == "bind"
                 and not self.own_storage(m, doc.get("Id") or "")]
        grants = []
        for m in doc.get("Mounts") or []:
            if m.get("Type") == "volume" and (m.get("Name") or "").startswith(f"{self.session}-grant"):
                grants.append((m, self.grant_volume_root(self.inspect("volumes", m["Name"]))))
        if not pid or not (binds or grants):
            return []
        try:
            mounted = {mp: (dev, root) for mp, dev, root in parse_mountinfo(self.read_mountinfo(pid))}
        except OSError:
            return []                                    # it has exited already; nothing left to read
        allowed = self.allowed_fs()
        bad = []
        for m in binds:
            got = mounted.get(m.get("Destination"))
            if got is None:
                bad.append(f"cannot confirm what is mounted at {m.get('Destination')}")
                continue
            dev, root = got
            if not any(dev == d and root == p for d, p in allowed):
                bad.append(f"{m.get('Destination')} is {root} (device {dev}; stored as {m.get('Type')} "
                           f"{m.get('Source')}), not a grant root")
        located = {r: (d, p) for d, p, r in self.allowed_fs(with_root=True)}
        for m, groot in grants:
            got = mounted.get(m.get("Destination"))
            want = located.get(groot)
            if got is None or want is None:
                bad.append(f"cannot confirm what is mounted at {m.get('Destination')}")
                continue
            dev, root = got
            if not (dev == want[0] and (root == want[1] or root.startswith(want[1].rstrip("/") + "/"))):
                bad.append(f"{m.get('Destination')} is {root} (device {dev}), outside its grant root {groot}")
        return bad

    # ---- create rewrites --------------------------------------------------------------------------
    def stamp(self, labels):
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

    def check_bind(self, src):
        """A bind source: absolute, and by its real path exactly a grant root (see _is_root). Returns the real
        path, which is what gets forwarded."""
        if not isinstance(src, str) or not src.startswith("/"):
            raise Deny(f"bind mount source {src!r} must be an absolute path")
        real = os.path.realpath(src)
        if self._is_root(real):
            return real
        if self._inside_roots(real):
            roots = ", ".join(self._roots())
            raise Deny(f"bind mount {src}: only a grant root itself can be bind-mounted ({roots}), not a path "
                       "inside one (podman resolves the path again at start, and it could be swapped for a symlink "
                       "by then). Bind the root and use the sub-path inside the container, or use a named volume "
                       "created in this session")
        raise Deny(f"bind mount {src} is outside the project's grants")

    def check_binds(self, sources):
        for src in sources:
            self.check_bind(src)

    def check_mount_options(self, opts, where):
        for o in opts or []:
            if not isinstance(o, str) or not MOUNT_OPTION_OK.match(o):
                raise Deny(f"mount option {o!r} on {where} is not allowed")

    def named_volume(self, name, where):
        """A named volume a create refers to: this session's own, or a new name, created here labelled."""
        if not isinstance(name, str) or not name:
            return
        if "/" in name or name.startswith(".") or "~" in name or not re.match(r"^[A-Za-z0-9][\w.-]*$", name):
            raise Deny(f"{where}: {name!r} is neither an absolute bind source nor a volume name")
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

    def check_image_volume(self, src):
        if not isinstance(src, str) or not src or self.image_doc(src) is None:
            raise Deny(f"image volume {src!r}: only images already present are allowed (no pulls)")
        self.check_image(src, "image volume")

    # ---- which images the dev tier may run or build from (Tim, 1 Oct) ------------------------------
    def image_doc(self, ref):
        return self.inspect("images", ref)

    def check_image(self, ref, where):
        """An image the dev tier runs, mounts or builds from must be: named from an allowlisted public
        registry, built in this session, or matched by the project's dev_images patterns. So a localhost/
        image of another project or a live stack (its files, its ENV) is out of reach unless listed."""
        doc = self.image_doc(ref)
        if doc is None:
            raise Deny(f"{where}: image {ref} is not present (pull it first; builds and creates never pull)")
        labels = self.labels_in("images", doc)
        if labels.get("agentbox.session") == self.session and labels.get("agentbox.role") == "dev":
            return
        names = list(doc.get("RepoTags") or []) + list(doc.get("RepoDigests") or [])
        for n in names:
            if "/" in n and n.split("/", 1)[0] in self.cfg["registries"]:
                return
            if any(fnmatch.fnmatchcase(n, pat) for pat in self.cfg.get("images") or []):
                return
        raise Deny(f"{where}: image {ref} ({', '.join(names) or 'untagged'}) is not allowed in the dev tier: "
                   f"only images from {', '.join(self.cfg['registries'])}, this session's own builds, or names "
                   "matching the project's dev_images (`ab project add --dev-image`)")

    def check_build_images(self, context_path, target):
        """Every image the build's Containerfile(s) use (FROM, COPY --from, RUN --mount from=) must pass
        check_image. The context is the spooled request body (a tar)."""
        q = dict((k.lower(), v) for k, v in parse_qsl(urlsplit(target).query, keep_blank_values=True))
        raw = q.get("dockerfile") or ""
        try:
            files = json.loads(raw) if raw.startswith("[") else ([raw] if raw else [])
        except ValueError:
            raise Deny("build dockerfile is not readable")
        try:
            args = json.loads(q.get("buildargs") or "{}")
        except ValueError:
            args = {}
        try:
            with tarfile.open(context_path, "r:*") as tar:
                members = {m.name.lstrip("./") if m.name.startswith("./") else m.name: m for m in tar.getmembers()}
                if not files:
                    files = [n for n in ("Containerfile", "Dockerfile") if n in members][:1]
                if not files:
                    raise Deny("the build context has no Containerfile or Dockerfile")
                for name in files:
                    if not isinstance(name, str) or name.startswith("/") or ".." in name.split("/"):
                        raise Deny(f"build dockerfile {name!r} must be inside the context")
                    m = members.get(name[2:] if name.startswith("./") else name)
                    if m is None or not m.isfile():
                        raise Deny(f"build dockerfile {name!r} is not a file in the build context")
                    text = tar.extractfile(m).read(1 << 20).decode("utf-8", errors="replace")
                    for _instr, ref in dockerfile_images(text, args):
                        self.check_image(ref, f"build {_instr}")
        except tarfile.TarError as e:
            raise Deny(f"the build context is not a readable tar ({e})")

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

    def check_limits(self, memory, swap, pids, *, compat):
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
                sub = self.grant_subpath(cget(m, "source")) if what == "container" else None
                if sub:
                    # A path below a grant root: mounted as the gate's bind-backed volume over that root with a
                    # SubPath, which podman resolves inside the volume (openat2 in-root), so a symlink swapped in
                    # before start cannot lead out (Tim's option 3, 1 Oct; evidence in docs/decisions.md).
                    name, rel = sub
                    grant_mounts.append({"Name": name, "Dest": cget(m, "destination"), "SubPath": rel,
                                         "Options": [o for o in cget(m, "options") or [] if o in VOLUME_OPTS]})
                    continue
                cset(m, "source", self.check_bind(cget(m, "source")))
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
        self.check_limits(cget(mem, "limit"), cget(mem, "swap"), cget(cget(rl, "pids") or {}, "limit"), compat=False)
        if cget(mem, "disableOOMKiller"):
            raise Deny("disabling the OOM killer is not allowed")
        if cget(rl, "unified"):
            raise Deny("raw cgroup settings (unified) are not allowed")
        for k in ("network_options", "networks", "cni_networks"):
            spec.pop(k, None)

    def rewrite_libpod_container(self, spec):
        self.check_common_libpod(spec, LIBPOD_REFUSED, "container")
        if not spec.get("image"):
            raise Deny("a container needs an image (a host directory as root is refused)")
        self.check_image(spec["image"], "create")
        if spec.get("cgroups_mode") not in (None, "", "enabled"):
            raise Deny(f"cgroups mode {spec['cgroups_mode']} is not allowed")
        if spec.get("seccomp_policy") not in (None, "", "default"):
            raise Deny("a seccomp policy from the image is not allowed")
        if spec.get("oom_score_adj") not in (None, 1000):
            raise Deny(f"OOM score {spec['oom_score_adj']} is not allowed (the gate sets 1000)")
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
            if not self.pod_has_infra(pod):
                # A pod without an infra container sharing its network (podman-compose's default has none)
                # gives its containers nothing: membership is dropped and the container pinned like any other.
                spec.pop("pod")
                pod = None
        if pod:
            # The infra container owns the network: the member takes it, nothing of its own.
            spec.pop("netns", None)
            spec.pop("Networks", None)
        else:
            mode = cget(spec.get("netns"), "nsmode")
            if mode in ("host", "path", "container", "ns", "slirp4netns", "pasta"):
                raise Deny(f"network mode {mode} is not allowed")
            aliases = []
            for net in (spec.get("Networks") or {}).values():
                aliases += cget(net, "aliases") or []
            spec["netns"] = {"nsmode": "bridge"}
            spec["Networks"] = {self.network: {"aliases": sorted(set(aliases))} if aliases else {}}
        spec["labels"] = self.stamp(spec.get("labels"))
        spec.pop("restart_policy", None)
        spec.pop("restart_tries", None)
        spec["systemd"] = "false"
        spec["cgroup_parent"] = self.cfg["cgroup_parent"]
        spec["oom_score_adj"] = 1000
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
        own = [spec.get("name", ""), spec.get("hostname", "")]
        own += [a for net in (spec.get("Networks") or {}).values() for a in cget(net, "aliases") or []]
        spec["env"] = self.proxy_env(spec.get("env"), as_list=False, own_names=own)
        return spec

    def rewrite_compat_container(self, spec):
        hc = canonical(spec.get("HostConfig") or {}, HOSTCONFIG_KEYS, "HostConfig")
        for key in ("MaskedPaths", "ReadonlyPaths"):
            if hc.get(key) is not None:          # an empty list means "unmask everything"
                raise Deny(f"changed {key} are not allowed")
        for key, why in HOSTCONFIG_REFUSED.items():
            if hc.get(key):
                raise Deny(f"{why} are not allowed" if why.endswith("s") else f"{why} is not allowed")
        for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode"):
            if hc.get(key) and hc.get(key) not in ("private", "", "shareable", "none"):
                raise Deny(f"{key} {hc[key]} is not allowed")
        for opt in hc.get("SecurityOpt") or []:
            if not re.match(r"^no-new-privileges([:=]true)?$", str(opt)):
                raise Deny(f"security option {opt} is not allowed")
        if hc.get("OomScoreAdj") not in (None, 0, 1000):
            raise Deny(f"OOM score {hc['OomScoreAdj']} is not allowed (the gate sets 1000)")
        self.check_sysctls(hc.get("Sysctls"), "container")
        hc["Annotations"] = self.check_annotations(hc.get("Annotations"), "container")
        log = hc.get("LogConfig") or {}
        if any(str(k).lower() == "path" for k in (cget(log, "Config") or {})):
            raise Deny("a log path on the host is not allowed")
        if "keep-groups" in (hc.get("GroupAdd") or []):
            raise Deny("keep-groups is not allowed")
        self.check_limits(hc.get("Memory"), hc.get("MemorySwap") if hc.get("MemorySwap") != -1 else None,
                          hc.get("PidsLimit"), compat=True)
        if hc.get("MemorySwap") == -1 or (hc.get("PidsLimit") or 0) < 0:
            raise Deny("unlimited swap or pids are not allowed")
        mode = hc.get("NetworkMode") or ""
        if mode == "host" or mode.startswith(("container:", "ns:", "pasta", "slirp4netns")):
            raise Deny(f"network mode {mode} is not allowed")
        aliases = []
        for ep in (cget(spec.get("NetworkingConfig"), "EndpointsConfig") or {}).values():
            aliases += cget(ep, "Aliases") or []
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
            binds.append(f"{src}:{rest}" if rest else src)
        if hc.get("Binds"):
            hc["Binds"] = binds
        for m in hc.get("Mounts") or []:
            t = cget(m, "Type")
            if t == "bind":
                cset(m, "Source", self.check_bind(cget(m, "Source")))
            elif t == "volume":
                if cget(cget(m, "VolumeOptions") or {}, "DriverConfig"):
                    raise Deny("volume driver options are not allowed")
                self.named_volume(cget(m, "Source"), "mount")
            elif t != "tmpfs":
                raise Deny(f"{t!r} mounts are not allowed")
        hc.pop("RestartPolicy", None)
        hc["CgroupParent"] = self.cfg["cgroup_parent"]
        hc["OomScoreAdj"] = 1000
        if not hc.get("Memory"):
            hc["Memory"] = self.cfg["memory"]
        hc["MemorySwap"] = hc["Memory"]
        if not hc.get("PidsLimit"):
            hc["PidsLimit"] = self.cfg["pids"]
        spec["HostConfig"] = hc
        spec["Labels"] = self.stamp(spec.get("Labels"))
        own = [spec.get("Hostname", "")] + aliases
        spec["Env"] = self.proxy_env(spec.get("Env"), as_list=True, own_names=own)
        return spec

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
        spec["cgroup_parent"] = self.cfg["cgroup_parent"]
        return spec

    def rewrite_network(self, spec, compat=None):
        """Bridge, internal, isolate=strict, no IPv6, and nothing that picks addresses, interfaces or routes.
        Nothing attaches to it (containers are pinned to the session network); if anything did, it would have
        no route out."""
        if compat is None:
            compat = any(k in spec for k in ("Name", "Internal", "Driver", "Labels"))
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
            spec.update({"Internal": True, "Driver": "bridge", "EnableIPv6": False,
                         "Labels": self.stamp(spec.get("Labels"))})
            return spec
        spec = canonical(spec, NETWORK_KEYS, "network")
        if spec.get("driver") not in (None, "", "bridge"):
            raise Deny(f"network driver {spec['driver']} is not allowed")
        for k in ("network_interface", "subnets", "routes", "ipam_options", "network_dns_servers", "ipv6_enabled"):
            if spec.get(k):
                raise Deny(f"network {k} is not allowed")
        self.network_options(spec.pop("options", None))
        spec.update({"internal": True, "driver": "bridge", "ipv6_enabled": False,
                     "labels": self.stamp(spec.get("labels"))})
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
        # podman's libpod API reads "Label", the Docker API "Labels"; the other spelling is ignored, so a
        # stamp under the wrong one would leave the volume unlabelled.
        key, other = ("Labels", "Label") if compat else ("Label", "Labels")
        spec[key] = self.stamp(spec.get(key) or spec.get(other))
        spec.pop(other, None)
        return spec

    # ---- build and pull ---------------------------------------------------------------------------
    def holder_ns(self):
        """/proc/<pid>/ns/net of the session's holder, re-read on every build. The container must be running
        with this session's holder labels, and the PID podman reports must sit in that container's own cgroup
        (its ID in the cgroup path), so a reused PID is never taken for the holder."""
        status, doc = self.raw_json(f"/libpod/containers/{quote(self.cfg['holder'], safe='')}/json")
        if status != 200 or not isinstance(doc, dict):
            raise Deny("the session's build holder is not running")
        labels = (doc.get("Config") or {}).get("Labels") or {}
        state = doc.get("State") or {}
        if labels.get("agentbox.session") != self.session or labels.get("agentbox.role") != "holder" \
                or not state.get("Running") or not state.get("Pid"):
            raise Deny("the session's build holder is not running")
        pid, cid = int(state["Pid"]), doc.get("Id") or ""
        try:
            with open(f"/proc/{pid}/cgroup") as f:
                cgroup = f.read()
            ns = os.readlink(f"/proc/{pid}/ns/net")
        except OSError:
            raise Deny("the build holder's process is gone")
        if not cid or cid not in cgroup:
            raise Deny("the build holder's PID does not belong to its container")
        return f"/proc/{pid}/ns/net", pid, ns

    def check_build_tag(self, ref):
        """A build may only name localhost/ images, never localhost/agentbox-*, and never a name an image
        outside this project's dev builds already has (that would move another stack's or another project's
        tag onto the agent's image)."""
        name = image_name(ref)
        if not name.startswith("localhost/"):
            raise Deny(f"build tag {ref}: only local names (localhost/...) are allowed")
        if name[len("localhost/"):].startswith("agentbox"):
            raise Deny(f"build tag {ref}: agentbox's own image names are reserved")
        doc = self.inspect("images", name)
        if doc is not None:
            labels = self.labels_in("images", doc)
            if labels.get("agentbox.project") != self.labels.get("agentbox.project") \
                    or labels.get("agentbox.role") != "dev":
                raise Deny(f"build tag {ref}: an image outside this project's dev builds already has that name")

    @staticmethod
    def check_build_secrets(value):
        try:
            items = json.loads(value or "[]")
        except ValueError:
            raise Deny("build secrets are not a JSON list")
        for item in items if isinstance(items, list) else [items]:
            for tok in str(item).split(","):
                k, _, v = tok.partition("=")
                k = k.strip().lower()
                if k == "env" or (k == "type" and v.strip() == "env"):
                    raise Deny("build secrets from the service's environment are not allowed")
                if k in ("src", "source") and not SECRET_SRC.match(v.strip()):
                    raise Deny("build secrets must come from the build context")

    def rewrite_build(self, target):
        parts = urlsplit(target)
        q = parse_qsl(parts.query, keep_blank_values=True)
        for k, v in q:
            kl = k.lower()
            if kl in REFUSED_BUILD_KEYS:
                raise Deny("a client-chosen cgroup parent is not allowed")
            if kl not in BUILD_KEYS + NS_KEYS + DROPPED_BUILD_KEYS:
                raise Deny(f"build option {k} is not allowed")
            if kl in ("t", "output") and v:
                self.check_build_tag(v)
            if kl == "secrets":
                self.check_build_secrets(v)
        ns_path, pid, ns_id = self.holder_ns()
        try:
            opts = json.loads(dict((k.lower(), v) for k, v in q).get("nsoptions") or "[]")
        except ValueError:
            opts = []
        kept = [o for o in opts if isinstance(o, dict) and o.get("Name") == "user"]
        nsopts = [{"Name": "network", "Host": False, "Path": ns_path}] + kept
        # Dropped, so the service's defaults apply: the client's security and namespace knobs (a seccomp or
        # AppArmor profile path names a file on the host; isolation and id mappings change the sandbox).
        q = [(k, v) for k, v in q if k.lower() not in NS_KEYS + DROPPED_BUILD_KEYS]
        q = buildcap.rewrite_build_query(q, slice_name=self.cfg["slice"], session=self.session,
                                         memory_bytes=self.cfg["build_memory"],
                                         state=self.slice_state) if self.cfg["slice"] else q
        orig = dict(parse_qsl(parts.query, keep_blank_values=True))
        try:
            args = json.loads(orig.get("buildargs") or "{}")
        except ValueError:
            args = {}
        args.update(self.cfg["proxy_env"])
        try:
            labels = json.loads(orig.get("labels") or "{}")
        except ValueError:
            labels = {}
        q += [("networkmode", NET_ENABLED), ("nsoptions", json.dumps(nsopts, separators=(",", ":"))),
              ("pull", "false"), ("pullpolicy", "never"), ("buildargs", json.dumps(args)),
              ("labels", json.dumps(self.stamp(labels)))]
        # Re-check just before forwarding: the PID could have been reused between the two reads.
        ns_again, pid_again, ns_id_again = self.holder_ns()
        if (ns_again, pid_again, ns_id_again) != (ns_path, pid, ns_id):
            raise Deny("the build holder changed during the build request")
        return parts.path + "?" + urlencode(q)

    def slice_state(self, name):
        return buildcap.slice_state(name)

    def rewrite_pull(self, target, path):
        parts = urlsplit(target)
        q = parse_qsl(parts.query, keep_blank_values=True)
        key = "reference" if "/libpod/" in path else "fromImage"
        refs = [v for k, v in q if k.lower() == key.lower()]
        if any(k.lower() in ("fromsrc", "repo") for k, _ in q) or len(refs) != 1:
            raise Deny("image imports and ambiguous pulls are not allowed")
        ref = normalize_ref(refs[0])
        if key == "fromImage":
            tag = [v for k, v in q if k.lower() == "tag"]
            if tag and tag[0] and ":" not in ref.rsplit("/", 1)[-1] and "@" not in ref:
                ref = f"{ref}:{tag[0]}"
            q = [(k, v) for k, v in q if k.lower() != "tag"]
        registry = ref.split("/", 1)[0]
        if registry not in self.cfg["registries"]:
            raise Deny(f"pulls from {registry} are not allowed (allowed: {', '.join(self.cfg['registries'])})")
        q = [(k, v) for k, v in q if k.lower() != key.lower()] + [(key, ref)]
        return parts.path + "?" + urlencode(q)

    # ---- dispatch ---------------------------------------------------------------------------------
    def decide(self, method, target, headers, body):
        """Returns (target, body, kind) to forward, or raises Deny."""
        path = canonical_path(target)
        tail = TAIL.match(path).group(1) if TAIL.match(path) else path
        for m, rx, why in DENIED:
            if (m is None or m == method) and rx.match(tail):
                raise Deny(f"{why} is not allowed in the dev tier")
        if method == "POST":
            if RE["compat_create"].match(path) and not self.cfg.get("compat_creates"):
                raise Deny("Docker-API container creates are not supported in the dev tier: sockguard refuses the "
                           "cgroup parent that keeps a container in agentbox.slice. Use podman or podman-compose")
            for kind, fn, known in (("libpod_create", self.rewrite_libpod_container, LIBPOD_KEYS),
                                    ("compat_create", self.rewrite_compat_container, COMPAT_KEYS),
                                    ("pod_create", self.rewrite_pod, POD_KEYS)):
                if RE[kind].match(path):
                    spec = canonical(strict_loads(body), known, kind.split("_")[0])
                    return target, json.dumps(fn(spec)).encode(), kind
            if RE["net_create"].match(path):
                q = parse_qsl(urlsplit(target).query)
                if any(k.lower() == "ignoreifexists" and v.lower() in ("1", "true") for k, v in q):
                    raise Deny("ignoreIfExists is not allowed (an existing network would be reported as created)")
                spec = self.rewrite_network(strict_loads(body), compat="/libpod/" not in path)
                return target, json.dumps(spec).encode(), "net_create"
            if RE["vol_create"].match(path):
                spec = self.rewrite_volume(strict_loads(body), compat="/libpod/" not in path)
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
                ok = cid is not None and cid in self.verified
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
                    self.check_start(kind, m.group(1))
                    return target, body, kind
        m = RESOURCE.match(path)
        if m and m.group(2) not in COLLECTION:
            kind, ref = m.group(1), m.group(2)
            if kind == "networks" and method == "GET" and ref == self.network:
                return target, body, "pass"           # compose inspects the network it was told to use
            self.require_dev(kind, ref)
        return target, body, "pass"


def _subst(value, args):
    """${VAR}, ${VAR:-default}, $VAR from build args; an unresolvable reference is refused."""
    def one(m):
        name = m.group(1) or m.group(3)
        default = m.group(2)
        if name in args and args[name] not in (None, ""):
            return str(args[name])
        if default is not None:
            return default
        raise Deny(f"build: cannot tell which image ${{{name}}} names (give it a default or a build arg)")
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)", one, value)


def dockerfile_images(text, buildargs=None):
    """[(instruction, image ref)] a Containerfile uses: FROM, COPY/ADD --from, RUN --mount=...,from=. Stage
    names, stage numbers and `scratch` are not images. ARG defaults and build args are substituted."""
    escape = "\\"
    lines = text.splitlines()
    for line in lines[:5]:
        m = re.match(r"^#\s*escape\s*=\s*(\S)\s*$", line, re.I)
        if m:
            escape = m.group(1)
    logical, cur, heredoc = [], "", None
    for line in lines:
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        if not cur and line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith(escape):
            cur += line.rstrip()[:-1] + " "
            continue
        cur += line
        m = re.search(r"<<-?[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", cur)
        if m and cur.split(None, 1)[0].upper() in ("RUN", "COPY", "ADD"):
            heredoc = m.group(1)
        logical.append(cur.strip())
        cur = ""
    if cur.strip():
        logical.append(cur.strip())
    args = {k: v for k, v in (buildargs or {}).items()}
    defaults, stages, out = {}, set(), []
    for line in logical:
        if not line:
            continue
        instr, _, rest = line.partition(" ")
        instr = instr.upper()
        env = dict(defaults, **args)
        if instr == "ARG":
            for tok in rest.split():
                k, eq, v = tok.partition("=")
                if eq and k not in defaults:
                    defaults[k] = v.strip("\"'")
        elif instr == "FROM":
            toks = [t for t in rest.split() if not t.startswith("--")]
            if not toks:
                raise Deny("build: a FROM without an image")
            ref = _subst(toks[0], env)
            if ref.lower() != "scratch" and ref.lower() not in stages:
                out.append(("FROM", ref))
            if len(toks) >= 3 and toks[1].lower() == "as":
                stages.add(toks[2].lower())
        elif instr in ("COPY", "ADD", "RUN"):
            for tok in rest.split():
                if not tok.startswith("--"):
                    break
                if tok.lower().startswith("--from="):
                    refs = [tok.split("=", 1)[1]]
                elif tok.lower().startswith("--mount="):
                    kv = dict(p.split("=", 1) for p in tok[len("--mount="):].split(",") if "=" in p)
                    refs = [kv["from"]] if "from" in kv else []
                else:
                    refs = []
                for ref in refs:
                    ref = _subst(ref.strip("\"'"), env)
                    if ref.lower() not in stages and not ref.isdigit() and ref.lower() != "scratch":
                        out.append((instr, ref))
    return out


def created_name(kind, target, body):
    """The name a create asked for (to find the object if podman's reply cannot be read)."""
    if kind == "compat_create":
        return dict(parse_qsl(urlsplit(target).query)).get("name")
    try:
        doc = json.loads(body or b"{}")
    except ValueError:
        return None
    return cget(doc, "name") if isinstance(doc, dict) else None


def normalize_ref(ref):
    """A short image name becomes a docker.io name (docker.io/library/x for official images)."""
    ref = ref.strip()
    if ref.startswith("docker://"):
        ref = ref[len("docker://"):]
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return ref
    return f"docker.io/{ref}" if "/" in ref else f"docker.io/library/{ref}"


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


# ---- HTTP plumbing ------------------------------------------------------------------------------

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("d", timeout=UPSTREAM_TIMEOUT)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(UPSTREAM_TIMEOUT)
        self.sock.connect(self._path)


class _BytesSocket:
    def __init__(self, data):
        self._f = io.BytesIO(data)

    def makefile(self, *_a, **_k):
        return self._f


def parse_response(raw):
    """(status, reason, headers, body) of a complete HTTP response; interim 1xx responses are skipped and a
    chunked body is decoded."""
    r = http.client.HTTPResponse(_BytesSocket(raw))
    r.begin()
    body = r.read()
    headers = [(k, v) for k, v in r.getheaders()
               if k.lower() not in ("transfer-encoding", "content-length", "connection")]
    return r.status, r.reason, headers, body


def build_response(status, reason, headers, body):
    lines = [f"HTTP/1.1 {status} {reason}"] + [f"{k}: {v}" for k, v in headers]
    lines += [f"Content-Length: {len(body)}", "Connection: close"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def read_head(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            return None, b""
        buf += chunk
        if len(buf) > MAX_HEAD:
            raise ValueError("request head too large")
    head, rest = buf.split(b"\r\n\r\n", 1)
    return head.decode("latin-1"), rest


def parse_head(head):
    lines = head.split("\r\n")
    method, target, version = lines[0].split(" ", 2)
    headers = []
    for line in lines[1:]:
        if line:
            k, v = line.split(":", 1)
            headers.append((k.strip(), v.strip()))
    return method, target, version, headers


def hget(headers, name):
    for k, v in headers:
        if k.lower() == name.lower():
            return v
    return None


def hset(headers, name, value):
    out = [(k, v) for k, v in headers if k.lower() != name.lower()]
    if value is not None:
        out.append((name, value))
    return out


def build_head(method, target, version, headers):
    return ("\r\n".join([f"{method} {target} {version}"] + [f"{k}: {v}" for k, v in headers])
            + "\r\n\r\n").encode("latin-1")


def read_body(sock, headers, rest):
    if "chunked" in (hget(headers, "Transfer-Encoding") or "").lower():
        return read_chunked(sock, rest)
    n = int(hget(headers, "Content-Length") or 0)
    if n > MAX_BODY:
        raise ValueError("request body too large")
    data = rest
    while len(data) < n:
        chunk = sock.recv(min(65536, n - len(data)))
        if not chunk:
            raise ValueError("connection closed mid-body")
        data += chunk
    return data[:n], data[n:]


def read_chunked(sock, data):
    body = b""

    def fill(pred):
        nonlocal data
        while not pred():
            chunk = sock.recv(65536) if sock is not None else b""
            if not chunk:
                raise ValueError("connection closed mid-chunk")
            data += chunk
    while True:
        fill(lambda: b"\r\n" in data)
        line, data = data.split(b"\r\n", 1)
        size = int(line.split(b";")[0].strip(), 16)
        if size == 0:
            while True:
                fill(lambda: b"\r\n" in data)
                trailer, data = data.split(b"\r\n", 1)
                if trailer == b"":
                    return body, data
        fill(lambda: len(data) >= size + 2)
        body += data[:size]
        data = data[size + 2:]
        if len(body) > MAX_BODY:
            raise ValueError("request body too large")


def spool_body(client, headers, rest, out, limit=MAX_BUILD_BODY):
    """Read a (build) body exactly into `out` (decoded: Content-Length bytes, or the chunks' payload up to the
    last chunk and its trailers). Returns its size. Bytes after the body (a pipelined request) are refused."""
    def more():
        chunk = client.recv(1 << 20)
        if not chunk:
            raise ValueError("connection closed mid-body")
        return chunk
    size = 0

    def put(data):
        nonlocal size
        size += len(data)
        if size > limit:
            raise Deny("the build context is too large")
        out.write(data)
    if "chunked" not in (hget(headers, "Transfer-Encoding") or "").lower():
        left = int(hget(headers, "Content-Length") or 0)
        if len(rest) > left:
            raise Deny("pipelined requests are not accepted (one request per connection)")
        put(rest)
        left -= len(rest)
        while left > 0:
            chunk = client.recv(min(1 << 20, left))
            if not chunk:
                raise ValueError("connection closed mid-body")
            put(chunk)
            left -= len(chunk)
        return size
    buf = rest
    while True:
        while b"\r\n" not in buf:
            buf += more()
        line, after = buf.split(b"\r\n", 1)
        n = int(line.split(b";")[0].strip(), 16)
        if n == 0:
            end = len(line) + 2
            while True:
                while b"\r\n" not in buf[end:]:
                    buf += more()
                i = buf.index(b"\r\n", end)
                trailer = buf[end:i]
                end = i + 2
                if trailer == b"":
                    break
            if len(buf) > end:
                raise Deny("pipelined requests are not accepted (one request per connection)")
            return size
        need = len(line) + 2 + n + 2
        while len(buf) < need:
            buf += more()
        if buf[need - 2:need] != b"\r\n":
            raise ValueError("malformed chunk")
        put(buf[len(line) + 2:need - 2])
        buf = buf[need:]


def recv_all(sock):
    raw = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return raw
        raw += chunk


def deny(client, msg):
    body = json.dumps({"cause": "agentbox-gate", "message": f"agentbox gate: {msg}", "response": 403}).encode()
    client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nConnection: close\r\n"
                   + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


def pipe(a, b):
    def fwd(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    t = threading.Thread(target=fwd, args=(a, b), daemon=True)
    t.start()
    fwd(b, a)
    t.join(timeout=10)


def pipe_back(upstream, client):
    try:
        while True:
            data = upstream.recv(65536)
            if not data:
                break
            client.sendall(data)
    except OSError:
        pass


def make_handler(gate):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            client, upstream = self.request, None
            method = path = "?"
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
                upstream.connect(gate.cfg["upstream"])
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
                                with gate.lock:
                                    gate.exec_ids[exec_id] = cid
                        else:
                            gate.after_start(gate.start_targets(kind, ref))
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
                        upstream.sendall(build_head(method, target, version, headers))
                        f.seek(0)
                        for block in iter(lambda: f.read(1 << 20), b""):
                            upstream.sendall(block)
                    upstream.settimeout(None)
                    pipe_back(upstream, client)
                    return
                upstream.sendall(build_head(method, target, version, headers) + body)
                if rest:
                    upstream.sendall(rest)            # an upgraded stream (exec start, attach) only
                upstream.settimeout(None)
                pipe(client, upstream)
            except Deny as e:
                gate.log("deny", method, path, str(e))
                try:
                    deny(client, str(e))
                except OSError:
                    pass
            except (AgentboxError, ValueError, OSError, http.client.HTTPException) as e:
                gate.log("error", method, path, repr(e))
                try:
                    deny(client, f"request refused ({e})")
                except OSError:
                    pass
            finally:
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
