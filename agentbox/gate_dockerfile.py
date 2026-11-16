"""Reading a Containerfile the way buildkit does, to find the images a build uses (FROM, COPY/ADD --from,
RUN --mount from=) and the RUN --mount options the gate refuses; plus the image-reference spelling rules.
Split out of gate.py; gate.py re-exports every name here."""
import re
import shlex

from .gate_schema import Deny


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


# buildkit splits an instruction and its arguments on exactly these (tokenWhitespace in its parser); so does
# the gate, so `FROM<TAB>image` names the same image for both.
_WS = re.compile(r"[ \t\v\f\r]+")


def dockerfile_images(text, buildargs=None):
    """[(instruction, image ref)] a Containerfile uses: FROM, COPY/ADD --from, RUN --mount=...,from=. Stage
    names, stage numbers and `scratch` are not images. ARG defaults and build args are substituted."""
    escape = "\\"
    # Lines end at "\n" only (buildkit's scanner; a trailing "\r" is dropped): str.splitlines would also break a
    # line at \v, \f or a lone \r, and see an instruction where buildkit sees one argument.
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    for line in lines[:5]:
        m = re.match(r"^#\s*escape\s*=\s*(\S)\s*$", line, re.I)
        if m:
            escape = m.group(1)
    logical, cur, heredocs = [], "", []
    for line in lines:
        if heredocs:
            name, chomp = heredocs[0]
            if (line.lstrip("\t") if chomp else line) == name:
                heredocs.pop(0)
            continue
        if not cur and line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith(escape):
            cur += line.rstrip()[:-1] + " "
            continue
        cur += line
        heredocs = heredoc_markers(cur, escape)
        logical.append(cur.strip())
        cur = ""
    if heredocs:
        raise Deny(f"build: unterminated heredoc {heredocs[0][0]!r}")
    if cur.strip():
        logical.append(cur.strip())
    args = {k: v for k, v in (buildargs or {}).items()}
    defaults, stages, out = {}, set(), []
    for line in logical:
        if not line:
            continue
        instr, rest = (_WS.split(line, 1) + [""])[:2]
        instr = instr.upper()
        env = dict(defaults, **args)
        if instr == "ARG":
            for tok in _WS.split(rest):
                k, eq, v = tok.partition("=")
                if eq and k not in defaults:
                    defaults[k] = v.strip("\"'")
        elif instr == "FROM":
            toks = [t for t in _WS.split(rest) if t and not t.startswith("--")]
            if not toks:
                raise Deny("build: a FROM without an image")
            ref = _subst(toks[0], env)
            if ref.lower() != "scratch" and ref.lower() not in stages:
                out.append(("FROM", ref))
            if len(toks) >= 3 and toks[1].lower() == "as":
                stages.add(toks[2].lower())
        elif instr in ("COPY", "ADD", "RUN"):
            for tok in _WS.split(rest):
                if not tok.startswith("--"):
                    break
                if tok.lower().startswith("--from="):
                    refs = [tok.split("=", 1)[1]]
                elif tok.lower().startswith("--mount="):
                    kv = {k.lower(): v for k, v in (p.split("=", 1) for p in tok[len("--mount="):].split(",")
                                                     if "=" in p)}
                    check_run_mount(kv)
                    refs = [kv["from"]] if "from" in kv else []
                else:
                    refs = []
                for ref in refs:
                    ref = _subst(ref.strip("\"'"), env)
                    if ref.lower() not in stages and not ref.isdigit() and ref.lower() != "scratch":
                        out.append((instr, ref))
    return out


# buildkit's heredoc word: [fd]<<[-]name, nothing else in the word (reHeredoc in its parser).
_HEREDOC = re.compile(r"^\d*<<(-?)([^<]+)$")


def instruction_words(rest, escape="\\"):
    """An instruction's arguments split into words as buildkit's parseWords does: on whitespace; a quote
    anywhere in a word opens a span that may hold spaces and is kept in the word, quotes included; the escape
    char keeps the next char (a quote too); an unclosed quote runs to the end of the line. Not shlex: shlex
    ends a word at a closing quote, so `echo ""<<EOF` would give the gate a `<<EOF` word buildkit never sees."""
    words, word, quote, blank, i, n = [], "", "", False, 0, len(rest)
    while i < n:
        ch = rest[i]
        if not quote:
            if ch.isspace():
                if blank or word:
                    words.append(word)
                word, blank = "", False
                i += 1
                continue
            if ch in "'\"":
                quote, blank = ch, True
            elif ch == escape:
                if i + 1 == n:
                    break                              # a trailing escape is dropped
                word += ch
                i += 1
                ch = rest[i]
        else:
            if ch == quote:
                quote = ""
            elif ch == escape and quote != "'":
                if i + 1 == n:
                    break
                word += ch
                i += 1
                ch = rest[i]
        word += ch
        i += 1
    if blank or word:
        words.append(word)
    return words


def heredoc_markers(cur, escape="\\"):
    """[(terminator, chomp)] of the heredocs one RUN, COPY or ADD instruction opens, found the way buildkit finds
    them: among the instruction's words (quotes kept), a word is a heredoc marker only when the whole word is
    `[fd]<<[-]name`; the name is then unquoted and must be one word. So `echo "<<EOF"` opens none, `<<EOF.1`
    opens one closed by `EOF.1`, and `<<"EOF"` by `EOF` (review 2026-11-16, item 2: the old search matched
    `<<` anywhere, so a quoted one opened a heredoc buildkit never closed and every later FROM went unchecked).
    A marker the gate cannot read is refused rather than guessed at: the two parsers must agree on which
    lines are instructions."""
    instr, rest = (_WS.split(cur.strip(), 1) + [""])[:2]
    if instr.upper() not in ("RUN", "COPY", "ADD") or not rest:
        return []
    out = []
    for word in instruction_words(rest, escape):
        m = _HEREDOC.match(word)
        if not m:
            continue
        try:
            names = shlex.split(m.group(2))
        except ValueError as e:
            raise Deny(f"build: cannot read heredoc marker {word!r} ({e})")
        if len(names) != 1:
            raise Deny(f"build: heredoc marker {word!r} names more than one word")
        out.append((names[0], m.group(1) == "-"))
    return out


def check_run_mount(kv):
    """RUN --mount options the gate checks itself rather than leaving to buildah (audit 6 Oct, N3): a
    `source=` that is absolute or climbs with `..`, for a bind from the context or for a cache, and
    `bind-propagation=` / `relabel=` are refused. Builds run as the host user, so a buildah path bug there
    would reach the whole home directory, not just the project."""
    kind = (kv.get("type") or "bind").lower()
    src = kv.get("source") or kv.get("src") or ""
    if kind in ("bind", "cache") and src and not (kind == "bind" and kv.get("from")):
        parts = src.replace("\\", "/").split("/")
        if src.startswith("/") or ".." in parts:
            raise Deny(f"build: RUN --mount type={kind} source={src} is not allowed (absolute or '..'); "
                       "use a path inside the build context")
    for k in ("bind-propagation", "relabel"):
        if k in kv:
            raise Deny(f"build: RUN --mount {k}= is not allowed")


def normalize_ref(ref):
    """A short image name becomes a docker.io name (docker.io/library/x for official images)."""
    ref = ref.strip()
    if ref.startswith("docker://"):
        ref = ref[len("docker://"):]
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return ref
    return f"docker.io/{ref}" if "/" in ref else f"docker.io/library/{ref}"
