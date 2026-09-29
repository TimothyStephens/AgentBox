#!/usr/bin/env python3
"""
spike_gate.py - feasibility prototype of the agentbox host-side gate.

Sits between a podman client and a podman API socket (or sockguard in front of it) and:
  * pins every created container (libpod and docker-compat) and pod to one network,
  * forces build RUN steps onto a chosen network (--build-strategy picks how),
  * refuses host port publishing,
  * strips restart policies and stamps an ownership label,
  * with --exec-scope, only lets exec reach containers carrying an allowed label, and only
    lets exec sessions it saw created be started, resized, inspected or removed.

This is a SPIKE, not a security boundary. It exists to prove these rewrites work
against podman 5.x's API. Do not rely on it to protect anything.

Standard library only.
"""
import argparse
import http.client
import json
import os
import re
import socket
import socketserver
import sys
import threading
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

V = r"(?:/v[0-9][0-9.]*)?"
LIBPOD_CREATE = re.compile(rf"^{V}/libpod/containers/create$")
COMPAT_CREATE = re.compile(rf"^{V}/containers/create$")
POD_CREATE = re.compile(rf"^{V}/libpod/pods/create$")
BUILD = re.compile(rf"^{V}(?:/libpod)?/build$")
EXEC_CREATE = re.compile(rf"^{V}(?:/libpod)?/containers/([^/]+)/exec$")
EXEC_OP = re.compile(rf"^{V}(?:/libpod)?/exec/([^/]+)/(start|resize|json|remove)$")

# Upstream calls the gate makes itself (container inspect, exec create). Generous: a busy
# host plus sockguard's own lookup took over 15 s in one run.
UPSTREAM_TIMEOUT = 60

MAX_HEAD = 1 << 20
MAX_BODY = 16 << 20

# podman's build API: networkmode is buildah's NetworkConfigurationPolicy as a number
NET_DEFAULT, NET_DISABLED, NET_ENABLED = "0", "1", "2"
NET_KEYS = ("networkmode", "nsoptions")

ARGS = None
TEMPLATE = {}
EXEC_IDS = set()          # exec sessions created through this gate
EXEC_LOCK = threading.Lock()


class Deny(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---- HTTP plumbing -----------------------------------------------------------

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
        if not line:
            continue
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
    lines = [f"{method} {target} {version}"] + [f"{k}: {v}" for k, v in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def read_exact(sock, data, n):
    if n > MAX_BODY:
        raise ValueError("request body too large")
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
            while True:  # trailers, ended by an empty line
                fill(lambda: b"\r\n" in data)
                trailer, data = data.split(b"\r\n", 1)
                if trailer == b"":
                    return body, data
        fill(lambda: len(data) >= size + 2)
        body += data[:size]
        data = data[size + 2:]
        if len(body) > MAX_BODY:
            raise ValueError("request body too large")


def read_body(sock, headers, rest):
    if "chunked" in (hget(headers, "Transfer-Encoding") or "").lower():
        return read_chunked(sock, rest)
    return read_exact(sock, rest, int(hget(headers, "Content-Length") or 0))


def deny(client, msg):
    body = json.dumps({"cause": "agentbox-gate", "message": f"agentbox-gate: {msg}", "response": 403}).encode()
    client.sendall(
        b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nConnection: close\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


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


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("d", timeout=UPSTREAM_TIMEOUT)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(UPSTREAM_TIMEOUT)
        self.sock.connect(self._path)


def upstream_json(path):
    """GET a JSON document from upstream. Returns (status, parsed-or-None)."""
    conn = UnixHTTPConnection(ARGS.upstream)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, None
    finally:
        conn.close()


# ---- rewrites ----------------------------------------------------------------

def label_pair():
    k, _, v = ARGS.label.partition("=")
    return k, v


def add_label(labels):
    labels = dict(labels or {})
    k, v = label_pair()
    labels[k] = v
    return labels


def rewrite_libpod_container(spec):
    if spec.get("portmappings") or spec.get("publish_image_ports"):
        raise Deny("host port publishing is not allowed")
    if spec.get("pod"):
        log(f"        note: container joins pod {spec['pod']!r}; the pod decides its network")
    else:
        spec["netns"] = {"nsmode": "bridge"}
        spec["Networks"] = {ARGS.network: {}}
        spec.pop("cni_networks", None)
    spec["labels"] = add_label(spec.get("labels"))
    spec.pop("restart_policy", None)
    spec.pop("restart_tries", None)
    return spec


def rewrite_pod(spec):
    if spec.get("portmappings"):
        raise Deny("host port publishing is not allowed")
    spec["netns"] = {"nsmode": "bridge"}
    spec["Networks"] = {ARGS.network: {}}
    spec.pop("cni_networks", None)
    spec["labels"] = add_label(spec.get("labels"))
    return spec


def rewrite_compat_container(spec):
    hc = spec.get("HostConfig") or {}
    if hc.get("PortBindings") or hc.get("PublishAllPorts"):
        raise Deny("host port publishing is not allowed")
    hc["NetworkMode"] = ARGS.network
    hc.pop("RestartPolicy", None)
    spec["HostConfig"] = hc
    spec["NetworkingConfig"] = {"EndpointsConfig": {ARGS.network: {}}}
    spec["Labels"] = add_label(spec.get("Labels"))
    return spec


def merged_build_query(seen):
    """Keep the client's other namespace options; replace only the network one."""
    try:
        opts = json.loads(seen.get("nsoptions") or "[]")
    except ValueError:
        opts = []
    opts = [o for o in opts if isinstance(o, dict) and o.get("Name") != "network"]
    if ARGS.build_network == "none":
        opts.insert(0, {"Name": "network", "Host": False, "Path": ""})
        mode = NET_DISABLED
    else:
        # "ns:/proc/<pid>/ns/net" joins an existing network namespace; anything else is a network name
        path = ARGS.build_network[3:] if ARGS.build_network.startswith("ns:") else ARGS.build_network
        opts.insert(0, {"Name": "network", "Host": False, "Path": path})
        mode = NET_ENABLED
    return {"networkmode": mode, "nsoptions": json.dumps(opts, separators=(",", ":"))}


def rewrite_build_target(target):
    """Force the network a build's RUN steps get.

    Strategies (--build-strategy):
      observe   change nothing, only log what the client sent
      template  replace networkmode + nsoptions with the values in --build-template
                (captured from the real client asking for the wanted network)
      merge     keep the client's other namespace options, replace the network
                entry, set networkmode to match (what a real gate would do)
    """
    parts = urlsplit(target)
    query = parse_qsl(parts.query, keep_blank_values=True)
    seen = {k: v for k, v in query if k.lower() in NET_KEYS}
    log("BUILDQ " + json.dumps(seen, sort_keys=True))

    strategy = ARGS.build_strategy
    if strategy == "observe":
        return target
    forced = TEMPLATE if strategy == "template" else merged_build_query(seen)
    query = [(k, v) for k, v in query if k.lower() not in NET_KEYS] + sorted(forced.items())
    log("BUILDQ-SENT " + json.dumps(forced, sort_keys=True))
    return urlunsplit(("", "", parts.path, urlencode(query), ""))


# ---- exec scoping ------------------------------------------------------------

def container_allowed(ref):
    """True if the container carries one of the --exec-scope labels (or our owner label)."""
    status, doc = upstream_json(f"/v5.0.0/libpod/containers/{quote(ref, safe='')}/json")
    if status != 200 or not isinstance(doc, dict):
        return False, f"cannot inspect container {ref!r} (status {status})"
    labels = (doc.get("Config") or {}).get("Labels") or {}
    allowed = list(ARGS.exec_scope) + [ARGS.label]
    for pair in allowed:
        k, _, v = pair.partition("=")
        if labels.get(k) == v:
            return True, f"label {pair}"
    return False, f"container {ref!r} has none of the allowed labels"


def forward_capture(method, target, version, headers, body):
    """Send one request upstream and return the full raw response (Connection: close)."""
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    up.settimeout(UPSTREAM_TIMEOUT)
    up.connect(ARGS.upstream)
    try:
        up.sendall(build_head(method, target, version, headers) + body)
        raw = b""
        while True:
            chunk = up.recv(65536)
            if not chunk:
                break
            raw += chunk
        return raw
    finally:
        up.close()


def exec_id_from_response(raw):
    try:
        head, rest = raw.split(b"\r\n\r\n", 1)
        if b"chunked" in head.lower():
            rest, _ = read_chunked(None, rest)
        return json.loads(rest).get("Id")
    except Exception:
        return None


# ---- server ------------------------------------------------------------------

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        upstream = None
        try:
            head, rest = read_head(client)
            if head is None:
                return
            method, target, version, headers = parse_head(head)
            path = unquote(urlsplit(target).path)

            # One request per connection keeps the parsing honest; upgrades (attach/exec) keep theirs.
            if not hget(headers, "Upgrade"):
                headers = hset(headers, "Connection", "close")

            # --- exec scoping ---------------------------------------------------
            if ARGS.exec_scope:
                m = EXEC_CREATE.match(path)
                if method == "POST" and m:
                    ok, why = container_allowed(m.group(1))
                    if not ok:
                        raise Deny(f"exec refused: {why}")
                    raw_body, rest = read_body(client, headers, rest)
                    headers = hset(headers, "Transfer-Encoding", None)
                    headers = hset(headers, "Content-Length", str(len(raw_body)))
                    resp = forward_capture(method, target, version, headers, raw_body)
                    exec_id = exec_id_from_response(resp)
                    if exec_id:
                        with EXEC_LOCK:
                            EXEC_IDS.add(exec_id)
                    log(f"EXEC    create on {m.group(1)} allowed ({why}) -> {exec_id}")
                    client.sendall(resp)
                    return
                m = EXEC_OP.match(path)
                if m:
                    with EXEC_LOCK:
                        known = m.group(1) in EXEC_IDS
                    if not known:
                        raise Deny(f"exec session {m.group(1)[:12]} was not created through this gate")

            body = None
            kind = None
            if method == "POST" and LIBPOD_CREATE.match(path):
                kind = rewrite_libpod_container
            elif method == "POST" and COMPAT_CREATE.match(path):
                kind = rewrite_compat_container
            elif method == "POST" and POD_CREATE.match(path):
                kind = rewrite_pod

            if kind is not None:
                raw, rest = read_body(client, headers, rest)
                spec = json.loads(raw or b"{}")
                body = json.dumps(kind(spec)).encode()
                headers = hset(headers, "Transfer-Encoding", None)
                headers = hset(headers, "Content-Length", str(len(body)))
                log(f"REWRITE {method} {path} -> network {ARGS.network}")
            elif method == "POST" and BUILD.match(path):
                target = rewrite_build_target(target)
                log(f"REWRITE {method} {path} -> strategy={ARGS.build_strategy} network={ARGS.build_network}")
            else:
                log(f"PASS    {method} {path}")

            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(ARGS.upstream)
            upstream.sendall(build_head(method, target, version, headers))
            if body is not None:
                upstream.sendall(body)
            if rest:
                upstream.sendall(rest)
            pipe(client, upstream)
        except Deny as e:
            log(f"DENY    {e}")
            try:
                deny(client, str(e))
            except OSError:
                pass
        except Exception as e:  # spike: report, never crash the server
            log(f"ERROR   {e!r}")
        finally:
            for s in (upstream, client):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    global ARGS, TEMPLATE
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen", required=True, help="unix socket path to listen on")
    p.add_argument("--upstream", required=True, help="podman (or sockguard) API unix socket path")
    p.add_argument("--network", required=True, help="network every container and pod is pinned to")
    p.add_argument("--build-network", default=None,
                   help="forced on builds: 'none', 'ns:/proc/<pid>/ns/net', or a network name (default: --network)")
    p.add_argument("--build-strategy", default="merge", choices=["observe", "template", "merge"],
                   help="how builds are forced onto --build-network (see rewrite_build_target)")
    p.add_argument("--build-template", default=None,
                   help="JSON file of build query values (networkmode, nsoptions) for --build-strategy template")
    p.add_argument("--exec-scope", action="append", default=[], metavar="KEY=VALUE",
                   help="only allow exec into containers with this label (repeatable); off when not given")
    p.add_argument("--label", default="agentbox-spike=1", help="ownership label key=value")
    ARGS = p.parse_args()
    if not ARGS.build_network:
        ARGS.build_network = ARGS.network
    if ARGS.build_strategy == "template":
        if not ARGS.build_template:
            p.error("--build-strategy template needs --build-template")
        with open(ARGS.build_template) as f:
            TEMPLATE = {k: v for k, v in json.load(f).items() if k.lower() in NET_KEYS}

    try:
        os.unlink(ARGS.listen)
    except FileNotFoundError:
        pass
    old = os.umask(0o177)  # socket is 0600
    server = Server(ARGS.listen, Handler)
    os.umask(old)
    log(f"gate listening on {ARGS.listen} -> {ARGS.upstream} (network={ARGS.network}, "
        f"build={ARGS.build_strategy}:{ARGS.build_network}, exec-scope={ARGS.exec_scope or 'off'})")
    server.serve_forever()


if __name__ == "__main__":
    main()
