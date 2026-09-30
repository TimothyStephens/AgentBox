"""Parsers for make claude-live output (no podman, no credentials; unit-tested)."""
import json
import re


def denied(access):
    return sorted({m.group(1) for m in re.finditer(r" [A-Z]+ (\S+) 403 - TCP_DENIED", access)})


def tool_outcomes(stream):
    """{tool name: (ok, detail)} from Claude's stream-json events."""
    uses, results = {}, {}
    for line in stream.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") or {}
        for item in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
            if item.get("type") == "tool_use":
                uses.setdefault(item.get("name"), []).append(item.get("id"))
            elif item.get("type") == "tool_result":
                content = item.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                results[item.get("tool_use_id")] = (not item.get("is_error"), str(content or ""))
    out = {}
    for name in ("WebSearch", "WebFetch", "Bash"):
        ids = uses.get(name) or []
        if not ids:
            out[name] = (False, "not called")
            continue
        ok, detail = results.get(ids[-1], (False, "no result"))
        if name == "Bash" and "AB_BASH_OK" not in detail:
            ok = False
        out[name] = (ok, " ".join(detail.split())[:240])
    return out


def empty_proxy_logs(runs):
    """Names of runs that answered (so went through squid) but whose captured proxy log is empty.
    `runs` is [(name, answered, access_log_text)]. Any such run means the log capture is broken."""
    return [name for name, answered, access in runs if answered and not (access or "").strip()]
