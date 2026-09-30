"""A strict reader for the small YAML subset model configs use (the runtime is standard library only).

Supported: block mappings and sequences by indentation, `- key: value` items, plain / single- / double-quoted
scalars, true/false/null, integers and floats, flow lists of scalars (`[a, "b"]`) and `{}`/`[]`.
Refused, with the line number: tabs in indentation, anchors, aliases, tags, block scalars (`|`, `>`), flow
mappings, complex keys, several documents, duplicate keys. Output is written as JSON, which is valid YAML.
"""
import json
import re

from .errors import AgentboxError

INT = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
FLOAT = re.compile(r"^[-+]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?$")


def _err(n, msg):
    raise AgentboxError(f"line {n}: {msg}")


def _strip_comment(line):
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'" and (i == 0 or line[i - 1] in " -:[,"):
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] == " "):
            return line[:i]
    return line


def _lines(text):
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        body = _strip_comment(raw).rstrip()
        if not body.strip():
            continue
        lead = body[:len(body) - len(body.lstrip())]
        if "\t" in lead:
            _err(n, "tab in indentation")
        if body.strip() in ("---", "...") or body.startswith("%"):
            _err(n, "document markers and directives are not supported")
        out.append([len(lead), body.strip(), n])
    return out


def _is_item(text):
    return text == "-" or text.startswith("- ")


def _split_key(text, n):
    """'key: rest' -> (key, rest), or None if the text is not a mapping entry."""
    if text.startswith("? "):
        _err(n, "complex keys are not supported")
    if text[0] in "\"'":
        end = text.find(text[0], 1)
        while end != -1 and text[0] == "'" and text[end + 1:end + 2] == "'":
            end = text.find("'", end + 2)
        if end == -1 or text[end + 1:end + 2] != ":" or text[end + 2:end + 3] not in ("", " "):
            return None
        return _scalar(text[:end + 1], n), text[end + 2:].strip()
    m = re.search(r":( |$)", text)
    if not m:
        return None
    key = text[:m.start()].strip()
    if not key or key[0] in "[{&*!|>":
        _err(n, f"unsupported key {key!r}")
    return key, text[m.end():].strip()


def _scalar(s, n):
    if s[0] in "&*!|>%@`":
        _err(n, f"unsupported value {s!r} (anchors, aliases, tags and block scalars are not supported; "
                "quote a string that starts with one of &*!|>%@`)")
    if s[0] == '"':
        if len(s) < 2 or s[-1] != '"':
            _err(n, "unterminated double-quoted string")
        try:
            v = json.loads(s)
        except ValueError:
            _err(n, f"cannot read double-quoted string {s!r}")
        if not isinstance(v, str):
            _err(n, f"cannot read double-quoted string {s!r}")
        return v
    if s[0] == "'":
        if len(s) < 2 or s[-1] != "'" or "'" in s[1:-1].replace("''", ""):
            _err(n, "bad single-quoted string")
        return s[1:-1].replace("''", "'")
    if s[0] == "[":
        return _flow_list(s, n)
    if s[0] == "{":
        if s.replace(" ", "") != "{}":
            _err(n, "flow mappings are not supported (only {})")
        return {}
    if ": " in s or s.endswith(":"):
        _err(n, f"ambiguous plain value {s!r}; quote it")
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if s in ("null", "Null", "NULL", "~"):
        return None
    if INT.match(s):
        return int(s)
    if FLOAT.match(s):
        return float(s)
    return s


def _flow_list(s, n):
    if s[-1] != "]":
        _err(n, "unterminated flow list")
    inner = s[1:-1].strip()
    if not inner:
        return []
    items, cur, quote = [], "", None
    for c in inner:
        if quote:
            cur += c
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            cur += c
        elif c in "[]{}":
            _err(n, "nested flow collections are not supported")
        elif c == ",":
            items.append(cur.strip())
            cur = ""
        else:
            cur += c
    if quote:
        _err(n, "unterminated string in flow list")
    items.append(cur.strip())
    if any(not x for x in items):
        _err(n, "empty item in flow list")
    return [_scalar(x, n) for x in items]


def _block(lines, i, indent):
    return (_seq if _is_item(lines[i][1]) else _map)(lines, i, indent)


def _nested(lines, i, indent):
    """The value of a `key:` or `-` with nothing after it: a nested block, or null."""
    if i + 1 < len(lines):
        nxt = lines[i + 1]
        if nxt[0] > indent or (nxt[0] == indent and _is_item(nxt[1])):
            return _block(lines, i + 1, nxt[0])
    return None, i + 1


def _seq(lines, i, indent):
    items = []
    while i < len(lines) and lines[i][0] == indent and _is_item(lines[i][1]):
        _ind, text, n = lines[i]
        rest = text[1:].lstrip()
        if not rest:
            val, i = _nested(lines, i, indent)
        elif _split_key(rest, n) is not None:
            col = indent + len(text) - len(rest)
            lines[i] = [col, rest, n]
            val, i = _map(lines, i, col)
        else:
            val, i = _scalar(rest, n), i + 1
        items.append(val)
    if i < len(lines) and lines[i][0] > indent:
        _err(lines[i][2], "unexpected indentation")
    return items, i


def _map(lines, i, indent):
    d = {}
    while i < len(lines) and lines[i][0] == indent:
        _ind, text, n = lines[i]
        if _is_item(text):
            _err(n, "a list item where a mapping key was expected")
        kv = _split_key(text, n)
        if kv is None:
            _err(n, f"expected `key: value`, got {text!r}")
        key, rest = kv
        if key in d:
            _err(n, f"duplicate key {key!r}")
        if rest:
            val, i = _scalar(rest, n), i + 1
        else:
            val, i = _nested(lines, i, indent)
        d[key] = val
    if i < len(lines) and lines[i][0] > indent:
        _err(lines[i][2], "unexpected indentation")
    return d, i


def loads(text):
    lines = _lines(text)
    if not lines:
        return None
    if lines[0][0] != 0:
        _err(lines[0][2], "the document must start at column 0")
    val, i = _block(lines, 0, 0)
    if i != len(lines):
        _err(lines[i][2], "unexpected content")
    return val


def dumps(doc):
    return json.dumps(doc, indent=2) + "\n"
