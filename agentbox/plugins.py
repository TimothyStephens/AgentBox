"""`plugins.toml`: what `ab profile install` installs, and the rules every entry has to satisfy.

The file lives in the agent's profile (`~/.config/agentbox/<agent>/profile/plugins.toml`), so it is already
hashed and snapshotted -- `profile.BUILD_ITEMS` has held its name since before there was an installer.

**Why this is a validator and not a loop over a TOML file.** Every string here becomes argv inside a
container that has network: `npm install <name>@<version>`, `git clone <url>`, `git checkout <ref>`. The
names also become directories in a tree that every OMP session mounts read-only. So a name with a `..` in
it, a `ref` that starts with `-`, or a URL with credentials in it are not typos to tolerate -- and an
unpinned version is the difference between "install" and "whatever npm published this morning".

**Pinning, revised.** The first design refused anything unpinned outright. That is wrong for git: four of
the five repos Tim actually uses are his own, tracked on `main`, and refusing a branch would make the tool
useless for them. So:

  * **npm entries must name an exact version.** A range (`^1.2.3`), `latest` or `*` is refused: npm can
    resolve those differently on two days, and there is no reason to accept it when the manifest has the
    exact number.
  * **git entries may name a branch**, because that is how the sources are actually tracked -- and the
    **lockfile records the commit it resolved to**, so `ab profile install --locked` reproduces the tree
    exactly. A branch in the input plus a commit in the lockfile is the npm/cargo bargain, and it is the
    one that survives contact with how these repos are maintained.

What is deliberately NOT here: a `pylibs` section. omp-env produced a `pylibs` directory, but the manifest
names no python dependency, so the installer does not invent one. If a plugin turns out to need python
libraries, that becomes a `[[pylib]]` section with pinned, hashed requirements -- the same rule
`images/*/Containerfile` already enforces for pip.
"""
import os
import re
import tomllib

from .errors import AgentboxError

# An npm package name: `pkg` or `@scope/pkg`, npm's own character rule, and nothing that could be read as a
# path, a flag or a second argument.
NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
# An exact version: 1.2.3, with an optional prerelease/build. No ranges, no tags.
NPM_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
# A directory name in the installed tree (also a git entry's name).
PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A branch, tag or commit. `/` is allowed (release/1.2); a leading `-` is not (it would be a git flag).
GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

NPM_KEYS = {"name", "version"}
GIT_KEYS = {"name", "url", "ref", "extension_dir"}
GIT_REQUIRED = {"name", "url", "ref"}


class Entry:
    """One thing to install. `kind` is "npm" or "git"."""

    def __init__(self, kind, **fields):
        self.kind = kind
        self.__dict__.update(fields)

    def __repr__(self):
        return f"Entry({self.kind}, {self.name})"

    def __eq__(self, other):
        return isinstance(other, Entry) and self.__dict__ == other.__dict__


def _refuse(what, why):
    raise AgentboxError(f"plugins.toml: {what}: {why}")


def _str(table, key, what):
    v = table.get(key)
    if not isinstance(v, str) or not v.strip():
        _refuse(what, f"{key} must be a non-empty string")
    return v


def _check_keys(table, allowed, what):
    """Unknown keys are refused rather than ignored. A typo'd `verison` that is quietly dropped is an entry
    with no version at all, which is exactly the state this file exists to prevent."""
    extra = sorted(set(table) - allowed)
    if extra:
        _refuse(what, f"unknown key(s) {', '.join(extra)} (allowed: {', '.join(sorted(allowed))})")


def _check_url(url, what):
    if not url.startswith("https://"):
        _refuse(what, f"url {url!r} must be https:// (git://, http:// and ssh remotes are refused: no "
                      "unauthenticated transport, and an ssh remote would need a key in the container)")
    rest = url[len("https://"):]
    host = rest.split("/", 1)[0]
    if "@" in host:
        _refuse(what, f"url {url!r} carries credentials; a credential never enters a sandbox")
    if not host or host.startswith(".") or "/" in host or ".." in url:
        _refuse(what, f"url {url!r} is not a plain https URL")


def _check_extension_dir(rel, what):
    """The subdirectory of the repo that holds the extension (`.` for a repo that IS one). It is joined onto
    the clone, so it must stay inside it: `normpath(rel) == rel` rejects `a/../..`, `a//b` and `a/..` in one
    rule, and the two explicit cases are an absolute path and a leading `..`."""
    if rel.startswith("/") or rel.split("/")[0] == ".." or os.path.normpath(rel) != rel:
        _refuse(what, f"extension_dir {rel!r} must be a plain relative path inside the repo")


def parse(text):
    """[Entry] from the file's text, in file order, every rule checked. Raises AgentboxError on anything
    doubtful; returns [] for an empty file (an agent with no plugins is a normal state, not an error)."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise AgentboxError(f"plugins.toml: not valid TOML ({e})")
    unknown = sorted(set(doc) - {"npm", "git"})
    if unknown:
        _refuse("file", f"unknown section(s) {', '.join(unknown)} (expected [[npm]] and [[git]])")
    out = []
    for kind, tables in (("npm", doc.get("npm", [])), ("git", doc.get("git", []))):
        if not isinstance(tables, list):
            _refuse(f"[{kind}]", f"must be written as [[{kind}]] (one table per entry)")
        for i, table in enumerate(tables):
            if not isinstance(table, dict):
                _refuse(f"[[{kind}]] #{i + 1}", "is not a table")
            out.append(_entry(kind, table, f"[[{kind}]] #{i + 1}"))
    _check_unique(out)
    return out


def _entry(kind, table, what):
    if kind == "npm":
        _check_keys(table, NPM_KEYS, what)
        name = _str(table, "name", what)
        version = _str(table, "version", what)
        if not NPM_NAME.match(name):
            _refuse(what, f"name {name!r} is not an npm package name")
        if not NPM_VERSION.match(version):
            _refuse(what, f"version {version!r} is not an exact version. A range or a tag resolves to "
                          "different code on different days; name the version you tested")
        return Entry("npm", name=name, version=version)
    _check_keys(table, GIT_KEYS, what)
    missing = sorted(GIT_REQUIRED - set(table))
    if missing:
        _refuse(what, f"missing {', '.join(missing)}")
    name = _str(table, "name", what)
    url = _str(table, "url", what)
    ref = _str(table, "ref", what)
    ext = table.get("extension_dir", ".")
    if not isinstance(ext, str) or not ext.strip():
        _refuse(what, "extension_dir must be a non-empty string")
    if not PLAIN_NAME.match(name) or name in (".", ".."):
        _refuse(what, f"name {name!r} becomes a directory in the installed tree; use letters, digits, "
                      "'.', '_' and '-'")
    _check_url(url, what)
    # The character rule, then git's own: `git check-ref-format` refuses `..`, `@{` and a `.lock` suffix,
    # so refusing them here means the error names the entry instead of arriving from a clone 20 seconds in.
    if not GIT_REF.match(ref) or ".." in ref or "@{" in ref or ref.endswith((".lock", "/")):
        _refuse(what, f"ref {ref!r} is not a branch, tag or commit (a leading '-' would be read as a git "
                      "option; '..', '@{' and a '.lock' suffix are refused by git itself)")
    _check_extension_dir(ext, what)
    return Entry("git", name=name, url=url, ref=ref, extension_dir=ext)


# The npm entries install into `node_modules/` of the installed tree; each git entry is a directory beside
# it, named by its `name`. So that one name is reserved.
NPM_DIR = "node_modules"


def _check_unique(entries):
    """A name listed twice, or a git entry that would be written over the npm tree, is a file that cannot
    say what it means."""
    seen = set()
    for e in entries:
        key = (e.kind, e.name)
        if key in seen:
            _refuse(f"{e.kind} {e.name}", "is listed twice")
        seen.add(key)
        if e.kind == "git" and e.name == NPM_DIR:
            _refuse(f"git {e.name}", f"{NPM_DIR}/ is where the npm entries are installed")


def load(path):
    """[Entry] for a profile's plugins.toml; [] when there is no such file (and a symlink is refused, as
    everywhere else in the profile: it would be followed into the installed tree)."""
    if os.path.islink(path):
        raise AgentboxError(f"{path} is a symlink; refusing to read it")
    try:
        with open(path, "rb") as f:
            text = f.read().decode()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise AgentboxError(f"cannot read {path}: {e}")
    except UnicodeDecodeError:
        raise AgentboxError(f"{path} is not UTF-8 text")
    return parse(text)


def npm_args(entries):
    """The `npm install -g` arguments for the npm entries, each as name@version."""
    return [f"{e.name}@{e.version}" for e in entries if e.kind == "npm"]
