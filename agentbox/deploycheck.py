#!/usr/bin/env python3
"""The `ab deploy` contract as a check on a compose file (design.md, "What `ab deploy` deploys").

One answer per rule, with the fix, BEFORE a deploy: the gate still enforces every rule at deploy time; this
only moves the feedback to when the compose file is written. Two callers:

  * inside a dev-tier session, as `/run/agentbox/bin/deploy-check` (a copy of this file; the session writes the
    project's values to /run/agentbox/deploy-contract.json). It parses YAML with PyYAML, which the agent
    images carry for podman-compose;
  * `ab deploy --check` on the host, which has the compose file parsed to JSON in the base image (agentbox
    itself is stdlib-only) and calls `check()` on the result.

Self-contained on purpose: the sandbox copy cannot import the agentbox package. Stdlib only, except the
`yaml` import in `main()`.
"""
import json
import os
import posixpath
import subprocess
import sys

TIMEZONE = ("/etc/localtime", "/etc/timezone", "/usr/share/zoneinfo")
REGISTRIES = ("docker.io", "ghcr.io", "lscr.io", "quay.io")
LOOPBACK = "127.0.0.1"
# Service keys the deploy gate refuses whatever their value (privilege, devices, host namespaces).
REFUSED_KEYS = ("privileged", "cap_add", "devices", "device_cgroup_rules", "security_opt", "sysctls",
                "cgroup_parent", "cgroup")
HOST_NS_KEYS = ("pid", "ipc", "uts", "userns_mode")
REPLACED_KEYS = {"restart": "always", "oom_score_adj": 0, "mem_swappiness": None, "memswap_limit": None}
CONTRACT_PATH = "/run/agentbox/deploy-contract.json"


def _inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _items(value):
    """A compose list-or-mapping (`environment`, `networks` of a service) as names."""
    if isinstance(value, dict):
        return list(value)
    return [v for v in value or [] if isinstance(v, str)]


class Report:
    def __init__(self):
        self.lines = []

    def add(self, level, rule, msg):
        self.lines.append((level, rule, msg))

    @property
    def failed(self):
        return any(level == "FAIL" for level, _r, _m in self.lines)

    def text(self):
        return "\n".join(f"{level:<4}  {rule}: {msg}" for level, rule, msg in self.lines)


def _registry(image):
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"                       # a short name: podman and the gate read it as docker.io


def _port(spec):
    """(host_ip, host_port, container) from a compose port entry; host_port None when podman would pick one,
    "range" for a range, "var" when it holds a ${...} the deploy substitutes."""
    if isinstance(spec, dict):
        published = spec.get("published")
        ip = str(spec.get("host_ip") or "")
        if published in (None, ""):
            return ip, None, spec.get("target")
        published = str(published)
    else:
        text = str(spec).split("/", 1)[0]
        parts = text.rsplit(":", 2) if text.count(":") >= 2 else text.split(":")
        if len(parts) == 1:
            return "", None, parts[0]
        ip, published = (parts[0], parts[1]) if len(parts) == 3 else ("", parts[0])
    if "$" in published or "$" in ip:
        return ip, "var", None
    if "-" in published:
        return ip, "range", None
    return ip, (int(published) if published.isdigit() else published), None


def _volume(spec):
    """(kind, source, target, read_only) from a service volume entry."""
    if isinstance(spec, dict):
        kind = spec.get("type") or ("bind" if str(spec.get("source", "")).startswith(("/", ".", "~"))
                                    else "volume")
        return kind, spec.get("source"), spec.get("target"), bool(spec.get("read_only"))
    parts = str(spec).split(":")
    if len(parts) == 1:
        return "volume", None, parts[0], False            # anonymous
    src, dst = parts[0], parts[1]
    ro = len(parts) > 2 and "ro" in parts[2].split(",")
    kind = "bind" if src.startswith(("/", ".", "~")) else "volume"
    return kind, src, dst, ro


def check(doc, ctx, tracked):
    """The contract on one parsed compose file. `ctx`: compose (root-relative path), lan_addr, ports, binds,
    external, adopted, compose_project. `tracked`: the repo's tracked paths, root-relative. Returns a Report."""
    r = Report()
    compose = ctx.get("compose") or "compose.yaml"
    cdir = posixpath.dirname(compose)
    lan = ctx.get("lan_addr") or ""
    ports = set(ctx.get("ports") or [])
    binds = list(ctx.get("binds") or [])
    external_ok = set(ctx.get("external") or []) | set(ctx.get("adopted") or [])
    tracked = set(tracked)
    tracked_dirs = {posixpath.dirname(t) for t in tracked}
    for t in list(tracked_dirs):
        while t:
            t = posixpath.dirname(t)
            tracked_dirs.add(t)

    def committed(rel):
        return rel in tracked or rel in tracked_dirs

    def repo_path(src):
        """A relative path in the compose file as a root-relative path, or None when it leaves the repo."""
        p = posixpath.normpath(posixpath.join(cdir, src))
        return None if p == ".." or p.startswith("../") or p.startswith("/") else p

    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        r.add("FAIL", "compose", f"{compose} has no services")
        return r
    r.add("PASS", "compose", f"the live compose file is {compose}")
    for key in ("include", "extends"):
        if key in doc:
            r.add("FAIL", "compose", f"top-level `{key}` pulls in files the review does not show; keep the live "
                  "stack in this one file")

    vols = doc.get("volumes") or {}
    for name, v in vols.items() if isinstance(vols, dict) else []:
        v = v or {}
        if v.get("external"):
            real = v.get("name") or name
            if real in external_ok:
                r.add("PASS", "external", f"volume {real} is listed")
            else:
                r.add("FAIL", "external", f"external volume {real} is not in deploy_external: the user adds it "
                      f"with `ab project set <project> --deploy-external {real}`, or the stack uses its own volume")
    nets = doc.get("networks") or {}
    for name, n in nets.items() if isinstance(nets, dict) else []:
        n = n or {}
        if n.get("external"):
            real = n.get("name") or name
            if real in external_ok:
                r.add("PASS", "external", f"network {real} is listed")
            else:
                r.add("FAIL", "external", f"external network {real} is not in deploy_external: the user adds it "
                      f"with `ab project set <project> --deploy-external {real}`")
        if n.get("driver") not in (None, "bridge") or n.get("ipam") or n.get("driver_opts"):
            r.add("FAIL", "network", f"network {name}: only plain bridge networks (no driver, IPAM or options)")

    for svc, s in doc["services"].items():
        s = s or {}
        where = f"service {svc}"
        if "extends" in s:
            r.add("FAIL", "compose", f"{where}: `extends` pulls in another definition; write it out here")
        for key in REFUSED_KEYS:
            if s.get(key):
                r.add("FAIL", "privilege", f"{where}: `{key}` is refused by the deploy gate; remove it (if the "
                      "service truly needs it, it cannot run under `ab deploy`: tell the user)")
        for key in HOST_NS_KEYS:
            if str(s.get(key) or "") == "host":
                r.add("FAIL", "privilege", f"{where}: `{key}: host` is refused")
        mode = str(s.get("network_mode") or "")
        if mode == "host" or mode.startswith(("container:", "service:", "ns:")):
            r.add("FAIL", "network", f"{where}: `network_mode: {mode}` is refused; use the stack's networks")
        for key, same in REPLACED_KEYS.items():
            if key in s and s[key] != same:
                r.add("WARN", "set-by-deploy", f"{where}: `{key}` is replaced by the deploy (restart is always "
                      "`always`, swap off, OOM score 0)")
        image = s.get("image")
        if image and not s.get("build"):
            if "$" in str(image):
                r.add("WARN", "image", f"{where}: image {image} uses a variable; checked at deploy time")
            elif _registry(str(image)) not in REGISTRIES:
                r.add("FAIL", "image", f"{where}: image {image} is not from {', '.join(REGISTRIES)}, and not "
                      "built from the repo")
        build = s.get("build")
        if build:
            context = build if isinstance(build, str) else (build.get("context") or ".")
            ctx_rel = repo_path(str(context))
            if ctx_rel is None:
                r.add("FAIL", "commit", f"{where}: build context {context} is outside the repo")
            else:
                df = None if isinstance(build, str) else build.get("dockerfile")
                if df:
                    df_rel = posixpath.normpath(posixpath.join(ctx_rel, df))
                    if not committed(df_rel):
                        r.add("FAIL", "commit", f"{where}: {df_rel} is not committed, so it is not in the "
                              "deploy copy")
        for spec in s.get("ports") or []:
            ip, hp, _c = _port(spec)
            label = f"{where}: port {spec}"
            if hp is None:
                r.add("FAIL", "ports", f"{label}: give a host port (a random one is refused)")
            elif hp == "range":
                r.add("FAIL", "ports", f"{label}: list each port (ranges are refused)")
            elif hp == "var":
                r.add("WARN", "ports", f"{label}: uses a variable; checked at deploy time")
            else:
                if ip in ("", "0.0.0.0"):
                    ip_ok, note = True, f" (published on {lan or 'the LAN address'})"
                else:
                    ip_ok, note = ip in (lan, LOOPBACK) and bool(ip), ""
                fix = ("the user adds it with `ab deploy <project> --lan ADDR:PORT` or "
                       "`ab project set <project> --deploy-port PORT`")
                if not ip_ok and not lan and ip != LOOPBACK:
                    # No LAN address stored yet (a first deploy gives it with --lan): cannot tell, so say so.
                    r.add("WARN", "ports", f"{label}: {ip} must be the LAN address given at deploy time "
                          f"(none stored yet) or {LOOPBACK}")
                elif not ip_ok:
                    r.add("FAIL", "ports", f"{label}: only {lan} or {LOOPBACK}")
                elif hp not in ports and not ports:
                    r.add("WARN", "ports", f"{label}: no deploy ports set yet; {fix}")
                elif hp not in ports:
                    r.add("FAIL", "ports", f"{label}: host port {hp} is not in deploy_ports "
                          f"({', '.join(map(str, sorted(ports)))}); {fix}")
                else:
                    r.add("PASS", "ports", f"{label}{note}")
        for spec in s.get("volumes") or []:
            kind, src, dst, ro = _volume(spec)
            label = f"{where}: volume {spec}"
            opts = str(spec).split(":")[2].split(",") if isinstance(spec, str) and spec.count(":") >= 2 else []
            if "U" in opts or any(o.startswith("chown") for o in opts):
                r.add("FAIL", "state", f"{label}: `U`/`chown` re-own the host files and are refused; own the "
                      "directory once instead (`podman unshare chown <uid>:<gid> DIR`, the container user's "
                      "ids) and drop the option")
            if kind == "tmpfs" or (kind == "volume" and not src):
                continue
            if kind == "volume":
                v = (vols.get(src) or {}) if isinstance(vols, dict) else {}
                if not v.get("external"):
                    r.add("PASS", "state", f"{label}: named volume (the deploy creates "
                          f"{ctx.get('compose_project') or '<project>'}_{src})")
                continue
            src = str(src)
            if "$" in src:
                r.add("WARN", "state", f"{label}: uses a variable; checked at deploy time")
            elif src.startswith("~"):
                r.add("FAIL", "state", f"{label}: `~` is refused; use an absolute path the user lists in "
                      "deploy_binds")
            elif src.startswith("/"):
                if any(_inside(src, t) for t in TIMEZONE):
                    (r.add("PASS", "state", f"{label}: timezone file") if ro else
                     r.add("FAIL", "state", f"{label}: timezone files only read-only (add `:ro`)"))
                elif any(_inside(posixpath.normpath(src), b) for b in binds):
                    r.add("PASS", "state", f"{label}: in deploy_binds")
                else:
                    r.add("FAIL", "state", f"{label}: {src} is not in deploy_binds"
                          + (f" ({', '.join(binds)})" if binds else " (none set yet)")
                          + ": live data belongs in a named volume, or a host directory outside every project "
                          "that the user lists with `ab project set <project> --deploy-bind DIR`")
            else:
                rel = repo_path(src)
                if rel is None:
                    r.add("FAIL", "state", f"{label}: {src} leaves the repo; use a named volume or an absolute "
                          "path in deploy_binds")
                elif not committed(rel):
                    r.add("FAIL", "state", f"{label}: {rel} is not committed (untracked or ignored), so it is "
                          "empty in the deploy copy. Live data goes in a named volume or a deploy_binds path; "
                          "config the app reads goes in a committed file")
                else:
                    r.add("PASS", "state", f"{label}: committed, read from the deploy copy")
        for env_file in ([s["env_file"]] if isinstance(s.get("env_file"), str) else s.get("env_file") or []):
            path = env_file if isinstance(env_file, str) else (env_file or {}).get("path", "")
            rel = repo_path(str(path))
            if rel is None:
                r.add("FAIL", "secrets", f"{where}: env_file {path} is outside the repo")
            elif posixpath.basename(rel) == ".env" and rel != ".env":
                r.add("FAIL", "secrets", f"{where}: env_file {path} is not the root .env, the one the deploy "
                      "copies")
    return r


def tracked_files(root):
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


def main(argv=None):
    """`deploy-check` in a session: the project's values from CONTRACT_PATH, the compose file from the working
    tree (what would be deployed once committed), tracked files from git."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        import yaml
    except ImportError:
        print("deploy-check: PyYAML is not available here", file=sys.stderr)
        return 2
    try:
        with open(CONTRACT_PATH) as f:
            ctx = json.load(f)
    except (OSError, ValueError) as e:
        print(f"deploy-check: no project values ({e})", file=sys.stderr)
        return 2
    root = ctx.get("root") or os.getcwd()
    rel = argv[0] if argv else ctx.get("compose") or next(
        (n for n in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
         if os.path.isfile(os.path.join(root, n))), "compose.yaml")
    ctx = dict(ctx, compose=rel)
    try:
        with open(os.path.join(root, rel)) as f:
            doc = yaml.safe_load(f)
    except OSError as e:
        print(f"FAIL  compose: {rel}: {e.strerror}")
        return 1
    except yaml.YAMLError as e:
        print(f"FAIL  compose: {rel} is not valid YAML ({e})")
        return 1
    report = check(doc, ctx, tracked_files(root))
    print(report.text())
    print("\nThe deploy gate enforces these at deploy time too; this only reports early. "
          + ("Fix the FAIL lines, or tell the user what the project needs." if report.failed else "No refusals."))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
