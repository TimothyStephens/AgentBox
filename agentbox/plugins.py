"""`plugins.toml`: what `ab profile install <agent>` installs, and the rules every entry has to satisfy.

**The schema is the profile repo's, not agentbox's.** `agentbox-profile-omp/plugins.toml` already declares
plugins as OMP *marketplace* entries pinned by commit sha, with the naming rules OMP itself enforces, and
that file is deployed to `~/.config/agentbox/omp/profile/plugins.toml` -- where `profile.BUILD_ITEMS`
already hashes and snapshots it. agentbox reads what the profile declares; it does not get to invent a
format. (The first version of this module did invent one, modelled on omp-env's `manifest.json`, and was
wrong: see decisions.md, 5 Nov.)

**Three ways an OMP plugin can arrive** -- the profile's own comment says so, from
`reference/omp/marketplace.md` and `reference/omp/extensions.md` -- and all three are expressible here now.
The profile noted that the second and third "need a second entry kind first", and that this is an `ab`
change rather than a profile change:

  * `[[plugin]]` -- a **marketplace** plugin: `id`, `marketplace`, `source`, `ref`. The source repo carries
    `.omp-plugin/marketplace.json`, or `.claude-plugin/marketplace.json` as the Claude-compatible fallback.
  * `[[npm]]` -- an **npm package** that declares `omp.extensions` in its `package.json`.
  * `[[repo]]` -- a **git checkout** linked with `omp plugin link`, for an extension that is neither
    published to npm nor carrying a marketplace catalog. `extension_dir` is the subdirectory that holds it,
    `.` when the repo *is* it.

**Everything is pinned to an exact thing, and `ref` is a 40-character sha.** That rule is the profile's,
with the profile's reason: *"Never a branch or tag: marketplace catalogs are mutable and an unpinned
install is not reproducible."* It is applied to `[[repo]]` too -- the reason does not stop at marketplaces,
and a branch would make the tree a session mounts depend on when it happened to be installed. An earlier
draft of this module allowed a branch for git entries, on the grounds that the sources are tracked on
`main`; the profile's rule is the better one, and "a sha is a nuisance to look up" is a job for
`ab profile install --resolve`, not a reason to loosen the file. npm entries name an exact version for the
same reason.

**Why this is a validator rather than a loop over a TOML file.** Every string here becomes argv in a
container that has network, and several become directory names in a tree every OMP session mounts
read-only. A `ref` that starts with `-` is a git option; a `..` in a path leaves the tree; a credential in
a URL is a credential in a sandbox; and an unknown key that is silently ignored turns a typo'd `verison`
into an entry with no version at all.

What is deliberately NOT here: a `pylibs` section. omp-env produced that directory, but nothing declares a
python dependency, so the installer does not invent a source for one. If a plugin turns out to need python
libraries, that becomes a `[[pylib]]` section with pinned, hashed requirements -- the rule
`images/*/Containerfile` already enforces for pip.
"""
import os
import re
import tomllib

from .errors import AgentboxError

# OMP's own naming rule for a plugin name and a marketplace name: ASCII letters, digits, hyphens and dots,
# starting and ending with a letter or digit (reference/omp/marketplace.md, "Naming rules").
OMP_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
OMP_NAME_MAX = 64
OMP_ID_MAX = 128
# A pinned commit. Nothing else: not a branch, not a tag, not an abbreviated sha.
SHA40 = re.compile(r"^[0-9a-f]{40}$")
# GitHub shorthand, as `/marketplace add` takes it.
SHORTHAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
# An npm package name: `pkg` or `@scope/pkg`.
NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
# An exact version: 1.2.3 with an optional prerelease/build. No ranges, no tags.
NPM_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
# A directory name in the installed tree.
PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

KEYS = {"plugin": {"id", "marketplace", "source", "ref"},
        "npm": {"name", "version"},
        "repo": {"name", "url", "ref", "extension_dir"}}
REQUIRED = {"plugin": KEYS["plugin"], "npm": KEYS["npm"], "repo": {"name", "url", "ref"}}
SECTIONS = ("plugin", "npm", "repo")
# The npm entries install into `node_modules/` of the installed tree; each repo entry is a directory beside
# it, named by its `name`. So that one name is reserved.
NPM_DIR = "node_modules"


class Entry:
    """One thing to install. `kind` is "plugin", "npm" or "repo"; `label` is what names it in a message."""

    def __init__(self, kind, label, **fields):
        self.kind = kind
        self.label = label
        self.__dict__.update(fields)

    def __repr__(self):
        return f"Entry({self.kind}, {self.label})"

    def __eq__(self, other):
        return isinstance(other, Entry) and self.__dict__ == other.__dict__


def _refuse(what, why):
    raise AgentboxError(f"plugins.toml: {what}: {why}")


def _str(table, key, what):
    v = table.get(key)
    if not isinstance(v, str) or not v.strip():
        _refuse(what, f"{key} must be a non-empty string")
    return v


def _check_keys(table, kind, what):
    """Unknown keys are refused rather than ignored: a typo'd key that is quietly dropped leaves an entry
    missing the very thing this file exists to state."""
    extra = sorted(set(table) - KEYS[kind])
    if extra:
        _refuse(what, f"unknown key(s) {', '.join(extra)} (allowed: {', '.join(sorted(KEYS[kind]))})")
    missing = sorted(REQUIRED[kind] - set(table))
    if missing:
        _refuse(what, f"missing {', '.join(missing)}")


def _check_sha(ref, what):
    if not SHA40.match(ref):
        _refuse(what, f"ref {ref!r} is not a 40-character commit sha. A branch or tag is not a pin: "
                      "marketplace catalogs are mutable, and an unpinned install is not reproducible")


def _check_https(url, what, key="url"):
    if not url.startswith("https://"):
        _refuse(what, f"{key} {url!r} must be https:// (git://, http:// and ssh remotes are refused: no "
                      "unauthenticated transport, and an ssh remote would need a key in the container)")
    host = url[len("https://"):].split("/", 1)[0]
    if "@" in host:
        _refuse(what, f"{key} {url!r} carries credentials; a credential never enters a sandbox")
    if not host or host.startswith(".") or ".." in url:
        _refuse(what, f"{key} {url!r} is not a plain https URL")


def _check_omp_name(name, what, field):
    if not OMP_NAME.match(name) or len(name) > OMP_NAME_MAX:
        _refuse(what, f"{field} {name!r} is not an OMP name: ASCII letters, digits, hyphens and dots, "
                      f"starting and ending with a letter or digit, at most {OMP_NAME_MAX} characters")


def parse(text):
    """[Entry] in file order, every rule checked. Raises AgentboxError on anything doubtful; returns [] for
    an empty file, because an agent with no plugins declared is a normal state and not an error."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise AgentboxError(f"plugins.toml: not valid TOML ({e})")
    unknown = sorted(set(doc) - set(SECTIONS))
    if unknown:
        _refuse("file", f"unknown section(s) {', '.join(unknown)} "
                        f"(expected {', '.join('[[%s]]' % s for s in SECTIONS)})")
    out = []
    for kind in SECTIONS:
        tables = doc.get(kind, [])
        if not isinstance(tables, list):
            _refuse(f"[{kind}]", f"must be written as [[{kind}]], one table per entry")
        for i, table in enumerate(tables):
            if not isinstance(table, dict):
                _refuse(f"[[{kind}]] #{i + 1}", "is not a table")
            out.append(_entry(kind, table, f"[[{kind}]] #{i + 1}"))
    _check_unique(out)
    return out


def _plugin(table, what):
    pid = _str(table, "id", what)
    marketplace = _str(table, "marketplace", what)
    source = _str(table, "source", what)
    ref = _str(table, "ref", what)
    if len(pid) > OMP_ID_MAX:
        _refuse(what, f"id {pid!r} is longer than {OMP_ID_MAX} characters")
    name, sep, after = pid.partition("@")
    if not sep:
        _refuse(what, f"id {pid!r} must be written as <name>@<marketplace>")
    if after != marketplace:
        _refuse(what, f"id {pid!r} ends in @{after} but marketplace is {marketplace!r}: that is one name "
                      "said twice, and a mismatch installs something other than what is declared")
    _check_omp_name(name, what, "the plugin name in id")
    _check_omp_name(marketplace, what, "marketplace")
    # A marketplace source is GitHub shorthand or a URL, and `/marketplace add` takes either. A local path
    # is refused: the install runs in a container with none of the host's filesystem in it.
    if not SHORTHAND.match(source):
        _check_https(source, what, key="source")
    _check_sha(ref, what)
    return Entry("plugin", pid, id=pid, marketplace=marketplace, source=source, ref=ref)


def _npm(table, what):
    name = _str(table, "name", what)
    version = _str(table, "version", what)
    if not NPM_NAME.match(name):
        _refuse(what, f"name {name!r} is not an npm package name")
    if not NPM_VERSION.match(version):
        _refuse(what, f"version {version!r} is not an exact version. A range or a tag resolves to "
                      "different code on different days; name the version you tested")
    return Entry("npm", name, name=name, version=version)


def _repo(table, what):
    name = _str(table, "name", what)
    url = _str(table, "url", what)
    ref = _str(table, "ref", what)
    ext = table.get("extension_dir", ".")
    if not isinstance(ext, str) or not ext.strip():
        _refuse(what, "extension_dir must be a non-empty string")
    if not PLAIN_NAME.match(name) or name == NPM_DIR:
        _refuse(what, f"name {name!r} becomes a directory in the installed tree: letters, digits, '.', '_' "
                      f"and '-', and not {NPM_DIR!r}, where the npm entries land")
    _check_https(url, what)
    _check_sha(ref, what)
    # Joined onto the clone, so it must stay inside it. `normpath(rel) == rel` rejects `a/../..`, `a//b`
    # and `a/..` in one rule; the two explicit cases are an absolute path and a leading `..`.
    if ext.startswith("/") or ext.split("/")[0] == ".." or os.path.normpath(ext) != ext:
        _refuse(what, f"extension_dir {ext!r} must be a plain relative path inside the repo")
    return Entry("repo", name, name=name, url=url, ref=ref, extension_dir=ext)


def _entry(kind, table, what):
    _check_keys(table, kind, what)
    return {"plugin": _plugin, "npm": _npm, "repo": _repo}[kind](table, what)


def _check_unique(entries):
    """A label listed twice is a file that cannot say what it means. Two entries that would write the same
    directory are worse: the second silently wins."""
    seen = set()
    for e in entries:
        if (e.kind, e.label) in seen:
            _refuse(f"{e.kind} {e.label}", "is listed twice")
        seen.add((e.kind, e.label))


def load(path):
    """[Entry] for a profile's plugins.toml; [] when there is none. A symlink is refused, as everywhere
    else in the profile: it would be followed out of the profile and into the installed tree."""
    if os.path.islink(path):
        raise AgentboxError(f"{path} is a symlink; refusing to read it")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise AgentboxError(f"cannot read {path}: {e}")
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        raise AgentboxError(f"{path} is not UTF-8 text")
    return parse(text)


def npm_args(entries):
    """`name@version` for each npm entry, in file order."""
    return [f"{e.name}@{e.version}" for e in entries if e.kind == "npm"]


def summary(entries):
    """What `ab profile install` and the session log report: counts by kind, in a fixed order."""
    counts = [(kind, len([e for e in entries if e.kind == kind])) for kind in SECTIONS]
    said = ", ".join(f"{n} {kind}" + ("s" if n != 1 else "") for kind, n in counts if n)
    return said or "nothing declared"
