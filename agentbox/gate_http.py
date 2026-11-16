"""The gate's HTTP plumbing: the upstream connection, reading and framing a request (head, body, chunks, a
spooled build context), writing replies, and relaying sockets. Split out of gate.py; gate.py re-exports every
name here."""
import http.client
import io
import json
import re
import socket
import threading

from . import builtrec
from .gate_schema import Deny


UPSTREAM_TIMEOUT = 300               # builds and pulls can be slow on this host
MAX_HEAD = 1 << 20
MAX_BODY = 64 << 20
MAX_BUILD_BODY = 8 << 30


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


TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")       # RFC 9110 token: header and method names


def parse_head(head):
    """The request line and headers, checked before anything is re-emitted upstream (review 2026-11-16,
    item 10): the head is split on CRLF only, so a bare LF, CR or NUL inside a value went to podman verbatim
    as a header the gate never saw. Refused: a name that is not a token (obs-fold lines too), a value holding
    CR, LF or NUL, more than one Transfer-Encoding, a request line not of the form METHOD SP target SP
    HTTP/1.x."""
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not TOKEN.match(parts[0]) or not re.fullmatch(r"HTTP/1\.[01]", parts[2]) \
            or not parts[1] or any(c.isspace() or ord(c) < 32 or c == "\x7f" for c in parts[1]):
        raise Deny("malformed request line")
    method, target, version = parts
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        k, sep, v = line.partition(":")
        if not sep or not TOKEN.match(k):
            raise Deny(f"malformed header line {line[:40]!r}")
        v = v.strip(" \t")
        if any(c in "\r\n\0" for c in v):
            raise Deny(f"header {k} holds a control character")
        headers.append((k, v))
    if sum(k.lower() == "transfer-encoding" for k, _ in headers) > 1:
        raise Deny("ambiguous request framing (Transfer-Encoding twice)")
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
            chunk = sock.recv(65536)
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
        if len(body) + size > MAX_BODY:              # before reading the chunk, not after (item 43)
            raise ValueError("request body too large")
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


def early_reply(sock, timeout=5.0, limit=1 << 20):
    """What an upstream that stopped reading a request body said before it closed: (status, reason, headers,
    body), or None if it said nothing parseable. A server that refuses a body mid-upload (sockguard's 413 on
    an oversized build context) writes its answer and closes; the sender sees EPIPE, but the answer is
    still in the socket's receive buffer."""
    raw = b""
    try:
        sock.settimeout(timeout)
        while len(raw) < limit:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
    except OSError:
        pass
    if not raw:
        return None
    try:
        return parse_response(raw)
    except (http.client.HTTPException, ValueError, OSError):
        return None


def build_cut_off_message(reply, size, sent):
    """(status, message) for the client when the build context could not be forwarded whole."""
    mib = lambda n: f"{n / (1 << 20):.0f} MiB"   # noqa: E731
    advice = ("Keep large files and directories (data, build outputs, .git) out of the context with a "
              ".containerignore, or build from a smaller directory")
    if reply is None:
        return 502, (f"the build context ({mib(size)}) was cut off after {mib(sent)}: podman's side closed the "
                     f"connection without a reply. {advice}")
    status, reason, _h, body = reply
    said = ""
    try:
        said = str((json.loads(body or b"{}") or {}).get("message") or "")
    except (ValueError, AttributeError):
        said = (body or b"").decode("utf-8", "replace")
    said = " ".join(said.split())[:300]
    return status, (f"the build context ({mib(size)}) was refused after {mib(sent)} by the dev tier's socket "
                    f"proxy: {status} {reason}{': ' + said if said else ''}. {advice}")


def send_json(client, body):
    try:
        client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                       + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    except OSError:
        pass


def deny(client, msg, status=403):
    cause = {404: "no such object", 502: "podman error"}.get(status, "agentbox-gate")
    body = json.dumps({"cause": cause, "message": f"agentbox gate: {msg}", "response": status}).encode()
    reason = {404: "Not Found", 413: "Payload Too Large", 502: "Bad Gateway",
              503: "Service Unavailable"}.get(status, "Forbidden")
    client.sendall(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nConnection: close\r\n".encode()
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


def pipe_reply(upstream, client):
    """Relay a plain (not upgraded) reply. Bytes the client sends after its request are read and dropped, never
    forwarded (review 2026-11-16, item 9: `pipe` relayed them, so a second request could follow a checked one
    unchecked). The client's own EOF still half-closes upstream, as before, so a long handler (wait, logs -f,
    events) ends when the client goes; a half-close right after the request would make podman's Go server
    cancel that handler at once. Returns the number of bytes dropped."""
    dropped = 0

    def drain():
        nonlocal dropped
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                dropped += len(data)
        except OSError:
            pass
        finally:
            try:
                upstream.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    pipe_back(upstream, client)
    try:
        client.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    t.join(timeout=10)
    return dropped


def pipe_build_reply(upstream, client, record):
    """Pass a build reply on, holding back any bytes that may carry the final ID line until more output
    follows (then it was not the final line) or the reply ends (then it is recorded first, and sent after)."""
    watch, held = builtrec.BuildWatcher(), b""
    try:
        while True:
            data = upstream.recv(65536)
            if data:
                watch.feed(data)
                held += data
            if not data or watch.done:
                record(watch.close())
                if held:
                    client.sendall(held)
                if not data:
                    return
                held = b""
                pipe_back(upstream, client)
                return
            if watch.candidate is None:
                client.sendall(held)
                held = b""
    except OSError:
        pass


def pipe_back(upstream, client):
    """Relay the rest of a reply. A build's reply is watched by `pipe_build_reply`, which feeds its own
    watcher and then hands the remainder here -- so this never needed a watcher of its own."""
    try:
        while True:
            data = upstream.recv(65536)
            if not data:
                break
            client.sendall(data)
    except OSError:
        pass
