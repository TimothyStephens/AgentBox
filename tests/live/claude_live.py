"""make claude-live: Claude Code, with the real setup-token token, whose only way out is squid.

Run by Tim, never by the builder. The real token is read in place from ~/.config/agentbox/ (it is
not copied); everything else lives under /tmp/abt-<run>/ and is removed afterwards.

Two sessions, both in the chosen egress mode (AB_LIVE_EGRESS, default locked):
  1. a one-shot `claude -p` that must answer AB_OK;
  2. a prompt that makes Claude call WebSearch, then WebFetch on one URL, then Bash. Which tools
     worked is read from Claude's own event stream (stream-json tool results), not from its reply.
For each, it prints the proxy's access log and the destinations squid denied, so the `locked` list
can be adjusted from evidence. It checks the token is in no log, state file or output.
On a pass it records the commit in tests/live/claude-live-pass.txt; `make itest` warns once token-handling
files change after it (tests/live/freshness.py).
"""
import json
import os
import re
import sys

from tests.integration import harness
from tests.live import freshness
from tests.live.parse import denied, tool_outcomes

MODE = os.environ.get("AB_LIVE_EGRESS", "locked")
REAL_DIR = os.path.expanduser("~/.config/agentbox")
FETCH_URL = "https://example.com/"
TOOLS_PROMPT = (
    "Do these three steps in order, each with the named tool, and carry on even if a step fails. "
    "1) Use the WebSearch tool to search for: IANA reserved example domains. "
    f"2) Use the WebFetch tool to fetch {FETCH_URL} and report the page title. "
    "3) Use the Bash tool to run: echo AB_BASH_OK. "
    "Then reply with exactly three lines: SEARCH ok or SEARCH fail, FETCH ok or FETCH fail, BASH ok or BASH fail."
)


def read_token():
    with open(os.path.join(REAL_DIR, "claude-oauth-token")) as f:
        return f.read().strip()


def proxy_log(env, before):
    """Access-log text of the sessions that ended since `before` (a set of file names)."""
    logs = os.path.join(env.data, "logs")
    text, names = "", set()
    for fn in sorted(os.listdir(logs)) if os.path.isdir(logs) else []:
        if fn.endswith("-proxy.log"):
            names.add(fn)
            if fn not in before:
                with open(os.path.join(logs, fn)) as f:
                    text += f.read()
    return text, names


def section(title, text):
    print(f"\n--- {title} ---")
    print(text.strip() if text and text.strip() else "(none)")


def main():
    env = harness.Env()
    with open(os.path.join(env.cfg, "agentbox.toml"), "a") as f:
        f.write(f'token_dir = "{REAL_DIR}"\n')
    os.unlink(os.path.join(env.cfg, "claude-oauth-token"))           # the fake one is not used
    extra = [] if MODE == "packages" else ["--egress", MODE]
    root = env.make_project("live", extra=extra, secrets_files=False)
    print(f"[claude-live run {harness.RUN}: egress {MODE}, project {root}]", flush=True)
    token = read_token()

    def redact(s):
        return s.replace(token, "[REDACTED]") if token else s

    # --- 1: one-shot answer ---------------------------------------------------------------
    seen = set()
    p1 = env.ab("run", "--agent", "claude", "--project", "live", "--no-tty", "--",
                "-p", "Reply with exactly: AB_OK", timeout=600)
    access1, seen = proxy_log(env, seen)
    section("1. claude -p output", redact(p1.stdout + p1.stderr)[-3000:])
    section("1. proxy access log", access1)
    section("1. denied destinations", "\n".join(denied(access1)))

    # --- 2: WebSearch, WebFetch, Bash ------------------------------------------------------
    p2 = env.ab("run", "--agent", "claude", "--project", "live", "--no-tty", "--",
                "-p", TOOLS_PROMPT, "--output-format", "stream-json", "--verbose", timeout=900)
    access2, seen = proxy_log(env, seen)
    outcomes = tool_outcomes(p2.stdout)
    final = ""
    for line in p2.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            final = str(ev.get("result", ""))
    section("2. Claude's own summary", redact(final))
    section("2. tool results (from the event stream)", "\n".join(
        f"{'WORKED' if ok else 'FAILED'}  {name}: {redact(detail)}" for name, (ok, detail) in outcomes.items()))
    if p2.returncode != 0 or not outcomes:
        section("2. stderr", redact(p2.stderr)[-2000:])
    section("2. proxy access log", access2)
    section("2. denied destinations", "\n".join(denied(access2)))

    # --- token and cleanup checks ------------------------------------------------------------
    leaks = []
    for where in (env.data, env.run_dir, env.cfg):
        for dirpath, _d, files in os.walk(where):
            for fn in files:
                try:
                    with open(os.path.join(dirpath, fn), "rb") as f:
                        if token and token.encode() in f.read():
                            leaks.append(os.path.join(dirpath, fn))
                except OSError:
                    pass
    for label, proc in (("run 1 output", p1), ("run 2 output", p2)):
        if token and token in proc.stdout + proc.stderr:
            leaks.append(label)

    ok1 = re.search(r"^AB_OK\s*$", p1.stdout, re.M) is not None
    left = harness.leftovers()
    print("\n--- result ---")
    print(f"{'PASS' if ok1 else 'FAIL'}  1. Claude answered AB_OK through the proxy (egress {MODE})")
    for name, (ok, _d) in outcomes.items():
        print(f"INFO  2. {name}: {'worked' if ok else 'failed'}")
    if MODE == "locked":
        exp = {"WebSearch": True, "WebFetch": False, "Bash": True}
        match = all(outcomes.get(k, (None,))[0] == v for k, v in exp.items())
        print(f"INFO  2. matches the expectation for locked (search works, fetch does not, bash works): "
              f"{'yes' if match else 'no'}")
    print(f"{'PASS' if not leaks else 'FAIL'}  token in no log, state file or output"
          + (f": {', '.join(leaks)}" if leaks else ""))
    print(f"{'PASS' if not left else 'FAIL'}  sessions torn down" + (f": {left}" if left else ""))
    harness.cleanup()
    passed = ok1 and outcomes.get("Bash", (False,))[0] and not leaks and not left
    if passed:
        freshness.record(MODE)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
