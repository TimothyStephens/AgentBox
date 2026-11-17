"""The gate's image handling: which images the dev tier may run, mount or build from, the build context's
image and mask checks, the build and pull rewrites, and the record of images built through the gate. A mixin
of `gate.Gate` (every method keeps its name and runs with the Gate as `self`); split out of gate.py, which
re-exports every module-level name here."""
import fnmatch
import json
import os
import posixpath
import re
import tarfile
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from . import buildcap, builtrec
from .gate_dockerfile import dockerfile_images, image_name, normalize_ref
from .gate_schema import Deny, NotFound

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


class LocalImage(Exception):
    """A pull of an image that is already present and allowed: answered by the gate, never forwarded."""
    def __init__(self, image_id):
        super().__init__(image_id)
        self.image_id = image_id


def _member_key(name):
    """A build-context tar member's name as the path it unpacks to, relative to the context: "." for the root."""
    n = posixpath.normpath("/" + name).lstrip("/")
    return n or "."


def image_created(doc):
    """An image's creation time as a Unix time, or None."""
    from datetime import datetime
    value = (doc or {}).get("Created")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        v = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
        try:
            return datetime.fromisoformat(v).timestamp()
        except ValueError:
            return None
    return None


class ImagesMixin:
    # ---- images on a create ----------------------------------------------------------------------
    def check_image_volume(self, src):
        if not isinstance(src, str) or not src or self.image_doc(src) is None:
            raise Deny(f"image volume {src!r}: only images already present are allowed (no pulls)")
        self.check_image(src, "image volume")

    # ---- which images the dev tier may run or build from (Tim, 1 Oct) ------------------------------
    def image_doc(self, ref):
        return self.inspect("images", ref)

    def check_image(self, ref, where):
        """An image the dev tier runs, mounts or builds from must be: named from an allowlisted public
        registry, built through the gate for this project (in any session; by the gate's record, not labels),
        or matched by the project's dev_images patterns. So a localhost/ image of another project or a live
        stack (its files, its ENV) is out of reach unless listed."""
        doc = self.image_doc(ref)
        if doc is None:
            raise Deny(f"{where}: image {ref} is not present (pull it first; builds and creates never pull)")
        if self.built_here(doc):
            return
        names = list(doc.get("RepoTags") or []) + list(doc.get("RepoDigests") or [])
        for n in names:
            if "/" in n and n.split("/", 1)[0] in self.cfg["registries"]:
                return
            if any(fnmatch.fnmatchcase(n, pat) for pat in self.cfg.get("images") or []):
                return
        raise Deny(f"{where}: image {ref} ({', '.join(names) or 'untagged'}) is not allowed in the dev tier: "
                   f"only images from {', '.join(self.cfg['registries'])}, this project's own builds, or names "
                   "matching the project's dev_images (`ab project add --dev-image`)")

    def check_image_read(self, ref):
        """An image inspect, history, exists or tree (review 2026-11-16, item 13): the same rule as running it.
        A present image the tier may not use is answered as missing, as a foreign container is (L1): the
        status must not tell whether another project's or a live stack's image exists. A missing one is left
        to podman, which answers 404 itself."""
        if self.image_doc(ref) is None:
            return
        try:
            self.check_image(ref, "image inspect")
        except Deny:
            raise NotFound(f"no such image: {ref!r}")

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
                # Keyed by the path the member unpacks to, as extraction cleans it: `./x`, `././x`, `a/../x`
                # and `/x` are all `x`, and the last of them wins, as it does on disk. Keying on the raw name
                # let a second `././Containerfile` replace the one checked here (review 16 Nov, R-3). Not
                # lstrip("./"): that is a set of characters, and turned "./.env" into "env" (item 42).
                members = {_member_key(m.name): m for m in tar.getmembers()}
                if not files:
                    files = [n for n in ("Containerfile", "Dockerfile") if n in members][:1]
                if not files:
                    raise Deny("the build context has no Containerfile or Dockerfile")
                for name in files:
                    if not isinstance(name, str) or name.startswith("/") or ".." in name.split("/"):
                        raise Deny(f"build dockerfile {name!r} must be inside the context")
                    m = members.get(_member_key(name))
                    if m is None or not m.isfile():
                        raise Deny(f"build dockerfile {name!r} is not a file in the build context")
                    text = tar.extractfile(m).read(1 << 20).decode("utf-8", errors="replace")
                    for _instr, ref in dockerfile_images(text, args):
                        self.check_image(self.own_short_name(ref) or ref, f"build {_instr}")
                self.check_context_masks(members)
        except tarfile.TarError as e:
            raise Deny(f"the build context is not a readable tar ({e})")

    def check_context_masks(self, members):
        """A build context is packed by the client in the agent's own mount namespace, where every mask reads
        as empty, so a COPY of a masked file or directory can only ever carry nothing. If a context arrives
        carrying content at a masked path, it was not packed through the masks: refuse it (Tim, 5 Oct). The
        context's host directory is not sent, so it is inferred: a directory D for which a member sits at a
        masked path and every top-level member exists on disk under D."""
        masked = list(self.cfg.get("masked") or [])
        if not masked:
            return
        tops = {n.split("/", 1)[0] for n in members if n and n not in (".", "./")}
        valid = {}

        def context_ok(d):
            if d not in valid:
                valid[d] = self._inside_roots(d) and all(os.path.lexists(os.path.join(d, t)) for t in tops)
            return valid[d]
        for name, m in members.items():
            n = name.strip("/")
            if not n or not m.isfile() or m.size == 0:
                continue
            parts = n.split("/")
            for i in range(len(parts), 0, -1):
                prefix = "/".join(parts[:i])
                for c in masked:
                    if c.endswith("/" + prefix) and context_ok(c[:-len(prefix) - 1]):
                        raise Deny(f"the build context carries content at {n}, which is masked in the session "
                                   f"({c}); a context packed in the session has it empty")
        # N8 (audit 6 Oct) is left as a heuristic, not a name-based backstop: a dev-swap `.env` is non-empty by
        # design, so refusing every non-empty secret-named member would break ordinary `COPY . /app` builds.
        # The agent cannot get a cover's real content into a context anyway (it reads empty in the session).

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
        if doc is not None and not self.built_here(doc):
            raise Deny(f"build tag {ref}: an image this project did not build through the gate already has that name")

    def own_short_name(self, ref):
        """`localhost/<ref>` when `ref` is a short name (no registry) and that image is in this project's build
        record. podman-compose builds localhost/<project>_<service> and then runs the short name, which would
        otherwise resolve to docker.io (trial 2, 2 Oct)."""
        if not isinstance(ref, str) or not ref or ref.startswith("sha256:") or re.fullmatch(r"[0-9a-f]{12,64}", ref):
            return None
        first = ref.split("/", 1)[0]
        if "/" in ref and ("." in first or ":" in first or first == "localhost"):
            return None
        cand = image_name(ref)
        doc = self.image_doc(cand)
        return cand if doc is not None and self.built_here(doc) else None

    def built_here(self, doc):
        """The image is one the gate built for this project (its record, never labels: anyone can label)."""
        rec = self.cfg.get("built_record")
        image_id = (doc.get("Id") or "").split(":", 1)[-1]
        return bool(rec and image_id and image_id in builtrec.ids(rec))

    def record_build(self, image_id, since=0.0):
        """Record the image a build ended with. Also required (a second check, review of 785ee15 H1): it
        carries this session's label, as the gate stamped it, and was created after the build started."""
        rec = self.cfg.get("built_record")
        doc = self.image_doc(image_id) if rec and image_id else None
        if doc is None:
            return False
        labels = self.labels_in("images", doc)
        created = image_created(doc)
        if labels.get("agentbox.session") != self.session or created is None or created < since - 5:
            self.log("not-recorded", "-", "build", f"image {image_id[:12]} is not this build's "
                     f"(session label {labels.get('agentbox.session')!r}, created {created})")
            return False
        builtrec.add(rec, image_id, self.session)
        self.log("built", "-", "build", f"recorded image {image_id[:12]} for this project")
        return True

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
        # By the proxy's IP, in both spellings: a build's RUN steps run in the holder's network namespace with
        # their own resolv.conf, where the proxy's container name does not resolve (trial, 2 Oct).
        args.update(self.cfg.get("build_proxy_env") or self.cfg["proxy_env"])
        try:
            labels = json.loads(orig.get("labels") or "{}")
        except ValueError:
            labels = {}
        q += [("networkmode", NET_ENABLED), ("nsoptions", json.dumps(nsopts, separators=(",", ":"))),
              ("pull", "false"), ("pullpolicy", "never"), ("buildargs", json.dumps(args)),
              ("labels", json.dumps(self.stamp(labels)))]
        if self.cfg.get("build_dns"):
            q.append(("dnsservers", json.dumps(self.cfg["build_dns"])))   # podman's DNS on the session network
        # The holder is read again in finalize_build, which the handler calls after the context has been spooled
        # and read, just before the build head goes upstream: the PID could be reused in between.
        self._tls.build_holder = (ns_path, pid, ns_id)
        return parts.path + "?" + urlencode(q)

    def finalize_build(self, target):
        """Just before a build is forwarded (after the body spool, which can take a while): the holder must still
        be the process rewrite_build pinned the build's network namespace to (review 2026-11-16, item 11: the
        re-check used to run inside rewrite_build, before the spool, leaving the spool time unchecked)."""
        first = getattr(self._tls, "build_holder", None)
        self._tls.build_holder = None
        if first is None:
            raise Deny("build was not prepared")
        if self.holder_ns() != first:
            raise Deny("the build holder changed during the build request")
        return target

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
        own = self.own_short_name(refs[0]) if "/libpod/" in path else None
        if own:
            raise LocalImage((self.image_doc(own).get("Id") or "").split(":", 1)[-1])
        registry = ref.split("/", 1)[0]
        if registry not in self.cfg["registries"]:
            # podman run/compose up pull with policy=missing before every create. An image that is already
            # present and that the dev tier may use (its own build, or listed) is answered here, never pulled.
            # One answer whether the image is absent or present but not the dev tier's (review of 785ee15, L2).
            doc = self.image_doc(ref) if "/libpod/" in path else None
            if doc is not None:
                try:
                    self.check_image(ref, "pull")
                except Deny:
                    doc = None
            if doc is not None:
                raise LocalImage((doc.get("Id") or "").split(":", 1)[-1])
            raise Deny(f"pulls from {registry} are not allowed (allowed: {', '.join(self.cfg['registries'])})")
        q = [(k, v) for k, v in q if k.lower() != key.lower()] + [(key, ref)]
        return parts.path + "?" + urlencode(q)
