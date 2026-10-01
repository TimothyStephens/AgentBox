"""The host-side gate: the only podman socket a dev-tier agent gets.

    agent --/run/agentbox/podman.sock--> gate --> sockguard (dev profile, owner isolation) --> podman service
                                          \\--> podman service (the gate's own inspects only)

Run per session as `python3 -m agentbox.gate <config.json>` (started by the launcher in the session's slice
scope). Standard library only. Every request is read whole (one request per connection) and then:

  * create (container, libpod or Docker API): pinned to the session network (aliases kept), no published
    ports, restart policy dropped, owner labels stamped, cgroup parent forced to the slice (a client value is
    refused), memory/pids limits defaulted, OOM score 1000, proxy variables added, binds only inside the
    project's grants, privileged/host namespaces/devices/added capabilities refused, systemd mode off;
  * pod create: the same network, port, label and cgroup rules;
  * network create: forced internal and labelled; volume create: labelled;
  * build: RUN steps join the session holder's network namespace (the holder's PID re-checked every
    build), the client's namespace options dropped except `user`, never pulls, capped by buildcap, proxy
    passed as build args, labelled;
  * image pull: allowlisted registries only; short names become docker.io names;
  * every request naming one container, pod, volume or network must name one this session's dev tier
    created (labels agentbox.dev-owner=<session> and agentbox.role=dev; checked by inspecting it), so
    the agent's own container, the proxy, sockguard and the holder are out of reach;
  * exec sessions can be started, resized, inspected or removed only if they were created through the gate.

Everything else is forwarded to sockguard, whose rules and owner isolation apply on top.
"""
import http.client
import json
import os
import re
import socket
import socketserver
import sys
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
}
# Collection paths whose second segment is an action word, not a resource name.
COLLECTION = {"json", "create", "prune", "stats", "load", "import", "pull", "search", "export", "exists",
              "showmounts", "kube"}
RESOURCE = re.compile(rf"^{V}{LIB}/(containers|pods|volumes|networks)/([^/]+)(?:/.*)?$")

DEV_OWNER = "agentbox.dev-owner"     # sockguard's ownership label key for the dev tier
UPSTREAM_TIMEOUT = 300               # builds and pulls can be slow on this host
MAX_HEAD = 1 << 20
MAX_BODY = 64 << 20

# podman's build API: networkmode is buildah's NetworkConfigurationPolicy as a number
NET_ENABLED = "2"
NS_KEYS = ("networkmode", "nsoptions")
REFUSED_BUILD_KEYS = ("cgroupparent",)
DROPPED_BUILD_KEYS = ("pull", "pullpolicy", "labels", "buildargs", "httpproxy", "seccomp", "apparmor", "isolation",
                      "idmappingoptions", "cgroupns", "ipcns", "pidns", "utsns", "userns", "devices", "capadd",
                      "privileged", "securityopt", "volume", "addhost", "dnsservers", "dnsoptions", "dnssearch")


class Deny(Exception):
    pass


# Podman decodes JSON field names case-insensitively, so "PortMappings" sets portmappings. Before checking, the
# gate renames every field it knows to its exact spelling (per body kind), and refuses objects with two keys
# that differ only in case. Found 1 Oct: "PortMappings" got a published host port past the gate and sockguard.
LIBPOD_FIELDS = ("portmappings", "publish_image_ports", "privileged", "cap_add", "devices", "device_cgroup_rule",
                 "pidns", "ipcns", "utsns", "userns", "cgroupns", "cgroup_parent", "pod", "netns", "nsmode", "value",
                 "Networks", "aliases", "cni_networks", "mounts", "type", "source", "destination", "labels",
                 "restart_policy", "restart_tries", "systemd", "oom_score_adj", "resource_limits", "memory", "limit",
                 "swap", "pids", "env", "name", "hostname", "no_infra", "image", "sysctl", "security_opt",
                 "volumes", "image_volumes", "overlay_volumes", "infra_image", "share", "options")
COMPAT_FIELDS = ("HostConfig", "PortBindings", "PublishAllPorts", "Privileged", "CapAdd", "Devices", "DeviceRequests",
                 "DeviceCgroupRules", "PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode", "CgroupParent",
                 "NetworkMode", "Binds", "Mounts", "Type", "Source", "Target", "RestartPolicy", "OomScoreAdj",
                 "Memory", "MemorySwap", "PidsLimit", "Labels", "Env", "NetworkingConfig", "EndpointsConfig",
                 "Aliases", "Hostname", "Image", "Sysctls", "SecurityOpt", "Volumes", "VolumesFrom", "ExposedPorts")
VOLUME_FIELDS = ("Name", "Driver", "Options", "DriverOpts", "Labels", "Label")
NETWORK_FIELDS = ("Name", "Internal", "Labels", "Driver", "Options", "name", "internal", "labels", "driver",
                  "options", "network_interface", "subnets")
CANON = {kind: {f.lower(): f for f in fields} for kind, fields in (
    ("libpod", LIBPOD_FIELDS), ("compat", COMPAT_FIELDS), ("volume", VOLUME_FIELDS))}


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


def canonical(obj, canon):
    """Rename known field names to their exact spelling, at every level."""
    if isinstance(obj, dict):
        return {canon.get(k.lower(), k): canonical(v, canon) for k, v in obj.items()}
    if isinstance(obj, list):
        return [canonical(v, canon) for v in obj]
    return obj


class Gate:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = cfg["session"]
        self.network = cfg["network"]
        self.labels = dict(cfg["labels"])
        self.exec_ids = set()
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

    def labels_of(self, kind, ref):
        path = {"containers": f"/libpod/containers/{quote(ref, safe='')}/json",
                "pods": f"/libpod/pods/{quote(ref, safe='')}/json",
                "volumes": f"/libpod/volumes/{quote(ref, safe='')}/json",
                "networks": f"/libpod/networks/{quote(ref, safe='')}/json"}[kind]
        status, doc = self.raw_json(path)
        if status != 200 or doc is None:
            return None
        if isinstance(doc, list):
            doc = doc[0] if doc else {}
        if kind == "containers":
            return (doc.get("Config") or {}).get("Labels") or {}
        return doc.get("Labels") or doc.get("labels") or {}

    def raw_request(self, method, path):
        conn = UnixHTTP(self.cfg["raw"])
        try:
            conn.request(method, "/v5.0.0" + path)
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def _inside_roots(self, src):
        real = os.path.realpath(src)
        return any(real == r or real.startswith(r.rstrip("/") + "/") for r in self.cfg["bind_roots"])

    def container_violations(self, doc):
        """What is wrong with a created container, judged by what podman stored (not by the request)."""
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
        labels = (doc.get("Config") or {}).get("Labels") or {}
        if labels.get(DEV_OWNER) != self.session or labels.get("agentbox.role") != "dev":
            bad.append("missing the dev labels")
        pod_infra = None
        if doc.get("Pod"):
            pod = self.pod_doc(doc["Pod"])
            plabels = (pod or {}).get("Labels") or {}
            if not pod or plabels.get(DEV_OWNER) != self.session:
                bad.append("in a pod that is not this session's")
            pod_infra = (pod or {}).get("InfraContainerID") or None
        for key in ("NetworkMode", "PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode"):
            mode = hc.get(key) or ""
            if mode == "host" or mode.startswith("ns:") or mode.startswith("/proc"):
                bad.append(f"{key} {mode}")
            if mode.startswith("container:") and (not pod_infra or mode[len("container:"):] != pod_infra):
                bad.append(f"{key} {mode}")
        if not pod_infra:
            nets = set(((doc.get("NetworkSettings") or {}).get("Networks") or {}).keys())
            if nets != {self.network}:
                bad.append(f"networks {sorted(nets)}")
        for m in doc.get("Mounts") or []:
            if m.get("Type") == "bind" and not self._inside_roots(m.get("Source", "")):
                bad.append(f"bind {m.get('Source')}")
        if self.cfg.get("cgroup_parent") and self.cfg["cgroup_parent"] not in (hc.get("CgroupParent") or ""):
            bad.append(f"cgroup parent {hc.get('CgroupParent')}")
        if (hc.get("RestartPolicy") or {}).get("Name") not in (None, "", "no"):
            bad.append("a restart policy")
        if hc.get("OomScoreAdj") != 1000:
            bad.append(f"OOM score {hc.get('OomScoreAdj')}")
        return bad

    def pod_doc(self, ref):
        status, doc = self.raw_json(f"/libpod/pods/{quote(ref, safe='')}/json")
        return doc if status == 200 and isinstance(doc, dict) else None

    def pod_violations(self, doc):
        bad = []
        if (doc.get("Labels") or {}).get(DEV_OWNER) != self.session:
            bad.append("missing the dev labels")
        infra = doc.get("InfraConfig") or {}
        if any(v for v in (infra.get("PortBindings") or {}).values()):
            bad.append("published ports")
        if infra.get("HostNetwork"):
            bad.append("host network")
        if doc.get("InfraContainerID") and set(infra.get("Networks") or []) - {self.network}:
            bad.append(f"networks {infra.get('Networks')}")
        return bad

    def verify_created(self, kind, raw):
        """After a create: inspect what podman stored; on any violation remove it and refuse."""
        try:
            created_id = json.loads(raw.split(b"\r\n\r\n", 1)[1].decode() or "{}").get("Id")
        except (ValueError, IndexError, AttributeError):
            created_id = None
        if not created_id:
            return                                   # the create failed; nothing exists to check
        if kind == "pod_create":
            doc = self.pod_doc(created_id)
            bad = self.pod_violations(doc) if doc else ["cannot inspect the new pod"]
            if bad:
                self.raw_request("DELETE", f"/libpod/pods/{created_id}?force=true")
        else:
            status, doc = self.raw_json(f"/libpod/containers/{created_id}/json")
            bad = self.container_violations(doc) if status == 200 and isinstance(doc, dict) \
                else ["cannot inspect the new container"]
            if bad:
                self.raw_request("DELETE", f"/libpod/containers/{created_id}?force=true")
        if bad:
            raise Deny(f"created {kind.split('_')[0]} did not match the rules ({'; '.join(bad)}); removed it")

    def require_dev(self, kind, ref):
        labels = self.labels_of(kind, ref)
        if labels is None:
            raise Deny(f"{kind[:-1]} {ref!r} not found")
        if labels.get(DEV_OWNER) != self.session or labels.get("agentbox.role") != "dev":
            raise Deny(f"{kind[:-1]} {ref!r} is not part of this session's dev stack")

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
            if labels.get(DEV_OWNER) != self.session or labels.get("agentbox.role") != "dev":
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
        add = dict(self.cfg["proxy_env"])
        add["no_proxy"] = self.no_proxy(own_names)
        if as_list:
            have = {e.split("=", 1)[0] for e in env or []}
            return list(env or []) + [f"{k}={v}" for k, v in add.items() if k not in have]
        out = dict(env or {})
        for k, v in add.items():
            out.setdefault(k, v)
        return out

    def check_binds(self, sources):
        roots = self.cfg["bind_roots"]
        for src in sources:
            real = os.path.realpath(src)
            if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots):
                raise Deny(f"bind mount {src} is outside the project's grants")

    def rewrite_libpod_container(self, spec):
        if spec.get("portmappings") or spec.get("publish_image_ports"):
            raise Deny("publishing ports on the host is not allowed")
        if spec.get("privileged"):
            raise Deny("privileged containers are not allowed")
        if spec.get("cap_add") or spec.get("devices") or spec.get("device_cgroup_rule"):
            raise Deny("added capabilities and devices are not allowed")
        for ns in ("pidns", "ipcns", "utsns", "userns", "cgroupns"):
            mode = (spec.get(ns) or {}).get("nsmode")
            if mode in ("host", "path", "container", "ns"):
                raise Deny(f"{ns} {mode} is not allowed")
        if spec.get("cgroup_parent"):
            raise Deny("a client-chosen cgroup parent is not allowed")
        pod = spec.get("pod")
        if pod:
            self.require_dev("pods", pod)
            if not self.pod_has_infra(pod):
                # A pod without an infra container shares no namespaces (podman-compose uses one only to
                # group containers); membership is dropped and the container pinned like any other.
                spec.pop("pod")
                pod = None
        if pod:
            pass                                   # the pod's network was pinned when it was created
        else:
            mode = (spec.get("netns") or {}).get("nsmode")
            if mode in ("host", "path", "container", "ns", "slirp4netns", "pasta"):
                raise Deny(f"network mode {mode} is not allowed")
            aliases = []
            for net in (spec.get("Networks") or spec.get("networks") or {}).values():
                aliases += (net or {}).get("aliases") or []
            spec.pop("networks", None)
            spec["netns"] = {"nsmode": "bridge"}
            spec["Networks"] = {self.network: {"aliases": sorted(set(aliases))} if aliases else {}}
            spec.pop("cni_networks", None)
        self.check_binds(m.get("source", "") for m in spec.get("mounts") or [] if m.get("type") == "bind")
        spec["labels"] = self.stamp(spec.get("labels"))
        spec.pop("restart_policy", None)
        spec.pop("restart_tries", None)
        spec["systemd"] = "false"
        spec["cgroup_parent"] = self.cfg["cgroup_parent"]
        spec["oom_score_adj"] = 1000
        rl = spec.setdefault("resource_limits", {}) or {}
        spec["resource_limits"] = rl
        mem = rl.setdefault("memory", {}) or {}
        rl["memory"] = mem
        if not mem.get("limit"):
            mem["limit"] = self.cfg["memory"]
            mem["swap"] = self.cfg["memory"]
        pids = rl.setdefault("pids", {}) or {}
        rl["pids"] = pids
        if not pids.get("limit"):
            pids["limit"] = self.cfg["pids"]
        own = [spec.get("name", ""), spec.get("hostname", "")]
        own += [a for net in (spec.get("Networks") or {}).values() for a in (net or {}).get("aliases") or []]
        spec["env"] = self.proxy_env(spec.get("env"), as_list=False, own_names=own)
        return spec

    def rewrite_compat_container(self, spec):
        hc = spec.get("HostConfig") or {}
        if hc.get("PortBindings") or hc.get("PublishAllPorts"):
            raise Deny("publishing ports on the host is not allowed")
        if hc.get("Privileged"):
            raise Deny("privileged containers are not allowed")
        if hc.get("CapAdd") or hc.get("Devices") or hc.get("DeviceRequests") or hc.get("DeviceCgroupRules"):
            raise Deny("added capabilities and devices are not allowed")
        for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode"):
            if hc.get(key) and hc.get(key) not in ("private", ""):
                raise Deny(f"{key} {hc[key]} is not allowed")
        if hc.get("CgroupParent"):
            raise Deny("a client-chosen cgroup parent is not allowed")
        mode = hc.get("NetworkMode") or ""
        if mode == "host" or mode.startswith(("container:", "ns:")):
            raise Deny(f"network mode {mode} is not allowed")
        aliases = []
        for ep in ((spec.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}).values():
            aliases += (ep or {}).get("Aliases") or []
        hc["NetworkMode"] = self.network
        spec["NetworkingConfig"] = {"EndpointsConfig": {self.network: {"Aliases": sorted(set(aliases))}}}
        binds = [b.split(":", 1)[0] for b in hc.get("Binds") or [] if b.startswith("/")]
        binds += [m.get("Source", "") for m in hc.get("Mounts") or [] if m.get("Type") == "bind"]
        self.check_binds(binds)
        hc.pop("RestartPolicy", None)
        hc["CgroupParent"] = self.cfg["cgroup_parent"]
        hc["OomScoreAdj"] = 1000
        if not hc.get("Memory"):
            hc["Memory"] = self.cfg["memory"]
            hc["MemorySwap"] = self.cfg["memory"]
        if not hc.get("PidsLimit"):
            hc["PidsLimit"] = self.cfg["pids"]
        spec["HostConfig"] = hc
        spec["Labels"] = self.stamp(spec.get("Labels"))
        own = [spec.get("Hostname", "")] + aliases
        spec["Env"] = self.proxy_env(spec.get("Env"), as_list=True, own_names=own)
        return spec

    def pod_has_infra(self, ref):
        status, doc = self.raw_json(f"/libpod/pods/{quote(ref, safe='')}/json")
        return status == 200 and isinstance(doc, dict) and bool(doc.get("InfraContainerID"))

    def rewrite_pod(self, spec):
        if spec.get("portmappings"):
            raise Deny("publishing ports on the host is not allowed")
        for ns in ("pidns", "ipcns", "utsns", "userns", "cgroupns", "netns"):
            mode = (spec.get(ns) or {}).get("nsmode")
            if mode in ("host", "path", "container", "ns"):
                raise Deny(f"pod {ns} {mode} is not allowed")
        if spec.get("cgroup_parent"):
            raise Deny("a client-chosen cgroup parent is not allowed")
        if not spec.get("no_infra"):
            # A pod with an infra container owns the network its containers share: pin it here. A pod
            # without one (podman-compose's default) has none; each container is pinned instead.
            spec["netns"] = {"nsmode": "bridge"}
            spec["Networks"] = {self.network: {}}
            spec.pop("networks", None)
            spec.pop("cni_networks", None)
        spec["labels"] = self.stamp(spec.get("labels"))
        spec["cgroup_parent"] = self.cfg["cgroup_parent"]
        return spec

    def rewrite_network(self, spec):
        # Nothing attaches to it (containers are pinned to the session network), but if anything did, it
        # would have no route out.
        if "Internal" in spec or "Name" in spec:
            spec["Internal"] = True
            spec["Labels"] = self.stamp(spec.get("Labels"))
        else:
            spec["internal"] = True
            spec["labels"] = self.stamp(spec.get("labels"))
        return spec

    def rewrite_volume(self, spec):
        key = "Labels" if "Labels" in spec or "Name" in spec else "labels"
        if spec.get("Driver") not in (None, "", "local") or spec.get("Options") or spec.get("DriverOpts") \
                or spec.get("options"):
            raise Deny("volume drivers and options (bind-backed volumes) are not allowed")
        spec[key] = self.stamp(spec.get(key))
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

    def rewrite_build(self, target):
        parts = urlsplit(target)
        q = parse_qsl(parts.query, keep_blank_values=True)
        for k, _ in q:
            if k.lower() in REFUSED_BUILD_KEYS:
                raise Deny("a client-chosen cgroup parent is not allowed")
            if k.lower() in ("remote", "additionalbuildcontexts") or k.lower().startswith("volume"):
                raise Deny(f"build option {k} is not allowed")
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
        """Returns (target, body) to forward, or raises Deny. May record exec ids after forwarding."""
        path = unquote(urlsplit(target).path)
        if method == "POST":
            for kind, fn, canon in (("libpod_create", self.rewrite_libpod_container, "libpod"),
                                    ("compat_create", self.rewrite_compat_container, "compat"),
                                    ("pod_create", self.rewrite_pod, "libpod"),
                                    ("net_create", self.rewrite_network, None),
                                    ("vol_create", self.rewrite_volume, "volume")):
                if RE[kind].match(path):
                    spec = strict_loads(body)
                    if canon:
                        spec = canonical(spec, CANON[canon])
                    return target, json.dumps(fn(spec)).encode(), kind
            if RE["build"].match(path):
                return self.rewrite_build(target), body, "build"
            if RE["pull"].match(path):
                return self.rewrite_pull(target, path), body, "pull"
        m = RE["exec_op"].match(path)
        if m:
            with self.lock:
                if m.group(1) not in self.exec_ids:
                    raise Deny(f"exec session {m.group(1)[:12]} was not created through this gate")
            return target, body, "exec_op"
        m = RE["exec_create"].match(path)
        if m:
            self.require_dev("containers", m.group(1))
            return target, body, "exec_create"
        m = RESOURCE.match(path)
        if m and m.group(2) not in COLLECTION:
            kind, ref = m.group(1), m.group(2)
            if kind == "networks" and method == "GET" and ref == self.network:
                return target, body, "pass"           # compose inspects the network it was told to use
            self.require_dev(kind, ref)
        return target, body, "pass"


def normalize_ref(ref):
    """A short image name becomes a docker.io name (docker.io/library/x for official images)."""
    ref = ref.strip()
    if ref.startswith("docker://"):
        ref = ref[len("docker://"):]
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return ref
    return f"docker.io/{ref}" if "/" in ref else f"docker.io/library/{ref}"


# ---- HTTP plumbing ------------------------------------------------------------------------------

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("d", timeout=UPSTREAM_TIMEOUT)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(UPSTREAM_TIMEOUT)
        self.sock.connect(self._path)


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


def exec_id_from(raw):
    try:
        head, rest = raw.split(b"\r\n\r\n", 1)
        if b"chunked" in head.lower():
            rest, _ = read_chunked(None, rest)
        return json.loads(rest).get("Id")
    except (ValueError, AttributeError):
        return None


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
                path = unquote(urlsplit(target).path)
                upgrade = hget(headers, "Upgrade")
                if not upgrade:
                    headers = hset(headers, "Connection", "close")
                has_body = hget(headers, "Content-Length") or "chunked" in (hget(headers, "Transfer-Encoding") or "")
                body = b""
                if has_body and not RE["build"].match(path):
                    body, rest = read_body(client, headers, rest)
                    headers = hset(headers, "Transfer-Encoding", None)
                    headers = hset(headers, "Content-Length", str(len(body)))
                target, new_body, kind = gate.decide(method, target, headers, body)
                if new_body is not body:
                    body = new_body
                    headers = hset(headers, "Content-Length", str(len(body)))
                gate.log("allow", method, path, kind if kind not in ("pull", "build") else f"{kind} {target}")
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.settimeout(UPSTREAM_TIMEOUT)
                upstream.connect(gate.cfg["upstream"])
                if kind in ("libpod_create", "compat_create", "pod_create"):
                    upstream.sendall(build_head(method, target, version, headers) + body)
                    raw = b""
                    while True:
                        chunk = upstream.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                    if b"chunked" in raw.split(b"\r\n\r\n", 1)[0].lower():
                        head, rest_body = raw.split(b"\r\n\r\n", 1)
                        payload, _ = read_chunked(None, rest_body)
                        raw = head + b"\r\n\r\n" + payload
                        raw = re.sub(rb"(?i)transfer-encoding: chunked\r\n", b"Content-Length: %d\r\n" % len(payload), raw, 1)
                    gate.verify_created(kind, raw)
                    client.sendall(raw)
                    return
                if kind == "exec_create":
                    upstream.sendall(build_head(method, target, version, headers) + body)
                    raw = b""
                    while True:
                        chunk = upstream.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                    exec_id = exec_id_from(raw)
                    if exec_id:
                        with gate.lock:
                            gate.exec_ids.add(exec_id)
                    client.sendall(raw)
                    return
                upstream.sendall(build_head(method, target, version, headers) + body)
                if rest:
                    upstream.sendall(rest)
                upstream.settimeout(None)
                pipe(client, upstream)
            except Deny as e:
                gate.log("deny", method, path, str(e))
                try:
                    deny(client, str(e))
                except OSError:
                    pass
            except (AgentboxError, ValueError, OSError) as e:
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
