"""The gate's record of the images it built, per project (Tim, 2 Oct).

The dev tier may run or build from an image this project built through the gate in any session. Which images
those are is decided by this record, never by labels: anyone can put agentbox's labels on an image built
elsewhere. The gate adds an image ID when a build it forwarded ends with that ID; `ab gc` removes an ID when it
removes the image.

<data>/built-images/<project>.json: {"images": {"<64-hex id>": {"session": ..., "built": <unix time>}}}
Written under an exclusive lock (two sessions of one project may build at once), replaced atomically.
"""
import fcntl
import json
import os
import re
import time

ID = re.compile(r"^[0-9a-f]{64}$")


def path_for(data_dir, project):
    return os.path.join(data_dir, "built-images", f"{project}.json")


def load(path):
    try:
        with open(path) as f:
            doc = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    images = doc.get("images") if isinstance(doc, dict) else None
    return {k: v for k, v in (images or {}).items() if ID.match(k)}


def ids(path):
    return set(load(path))


def _update(path, change):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        images = load(path)
        change(images)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"images": images}, f, indent=1, sort_keys=True)
        os.replace(tmp, path)


def add(path, image_id, session):
    image_id = image_id.split(":", 1)[-1]
    if not ID.match(image_id):
        raise ValueError(f"not an image ID: {image_id!r}")
    _update(path, lambda images: images.__setitem__(image_id, {"session": session, "built": int(time.time())}))


def remove(path, image_ids):
    gone = {i.split(":", 1)[-1] for i in image_ids}
    _update(path, lambda images: [images.pop(i, None) for i in gone])


def remove_everywhere(data_dir, image_ids):
    """Drop image IDs from every project's record (ab gc, after it removed the images)."""
    d = os.path.join(data_dir, "built-images")
    if not os.path.isdir(d):
        return
    for fn in os.listdir(d):
        if fn.endswith(".json"):
            p = os.path.join(d, fn)
            if set(load(p)) & {i.split(":", 1)[-1] for i in image_ids}:
                remove(p, image_ids)


class BuildWatcher:
    """Reads a libpod build reply (raw HTTP, as it streams past) and remembers the image ID it ends with. A
    successful build's last stream line is the full 64-hex ID (podman 5.7, checked 2 Oct); an error line, any
    later stream line, or a status other than 200 clears it. Only the ID the reply *ends* with counts: a RUN
    step can print any 64-hex string (review of 785ee15, H1)."""

    def __init__(self):
        self.raw = b""                    # bytes not yet parsed as HTTP framing
        self.head = None                  # None until the response head is read
        self.chunked = False
        self.chunk_left = 0               # payload bytes left in the current chunk
        self.done = False
        self.ok = False
        self.buf = b""                    # decoded body bytes not yet split into lines
        self.image_id = None

    def feed(self, data):
        self.raw += data
        if self.head is None:
            if b"\r\n\r\n" not in self.raw:
                if len(self.raw) > 1 << 20:
                    self.raw, self.done = b"", True
                return
            self.head, self.raw = self.raw.split(b"\r\n\r\n", 1)
            lines = self.head.split(b"\r\n")
            self.ok = lines[0].split(b" ")[1:2] == [b"200"]
            self.chunked = any(l.lower().startswith(b"transfer-encoding:") and b"chunked" in l.lower()
                               for l in lines[1:])
        if self.done:
            return
        if not self.chunked:
            self._body(self.raw)
            self.raw = b""
            return
        while self.raw:
            if self.chunk_left == 0:
                if b"\r\n" not in self.raw:
                    return
                size_line, rest = self.raw.split(b"\r\n", 1)
                if not size_line.strip():           # the CRLF that ends the previous chunk
                    self.raw = rest
                    continue
                try:
                    size = int(size_line.split(b";")[0].strip(), 16)
                except ValueError:
                    self.done = True
                    return
                if size == 0:
                    self.done = True
                    return
                self.chunk_left, self.raw = size, rest
            take = self.raw[:self.chunk_left]
            self._body(take)
            self.chunk_left -= len(take)
            self.raw = self.raw[len(take):]

    def _body(self, data):
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self._line(line)
        if len(self.buf) > 1 << 20:              # a line this long is no ID line; do not hold it
            self.buf = b""

    def close(self):
        if self.buf:
            self._line(self.buf)
            self.buf = b""
        return self.image_id if self.ok else None

    def _line(self, line):
        line = line.strip()
        if not line.startswith(b"{"):
            return
        try:
            doc = json.loads(line)
        except ValueError:
            return
        if not isinstance(doc, dict):
            return
        if doc.get("error") or doc.get("errorDetail"):
            self.image_id = None
            return
        text = str(doc.get("stream") or "").strip()
        if text:
            self.image_id = text if ID.match(text) else None   # the ID is the last stream line

    @property
    def candidate(self):
        """An ID that would be the build's if the reply ended now (pipe_build_reply holds the bytes back meanwhile)."""
        return self.image_id if self.ok else None
