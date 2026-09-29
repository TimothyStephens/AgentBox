"""make claude-live: Claude Code, with the real setup-token token, whose only way out is squid.

Run by Tim, never by the builder. The real token is read in place from ~/.config/agentbox/ (it is
not copied); everything else lives under /tmp/abt-<run>/ and is removed afterwards. It prints
Claude's answer, the proxy's access log (so the `locked` domain list can be adjusted from what was
denied), and checks that the token is in no log, state file or output.
"""
import os
import re
import sys

from tests.integration import harness

MODE = os.environ.get("AB_LIVE_EGRESS", "locked")
REAL_DIR = os.path.expanduser("~/.config/agentbox")


def main():
    env = harness.Env()
    with open(os.path.join(env.cfg, "agentbox.toml"), "a") as f:
        f.write(f'token_dir = "{REAL_DIR}"\n')
    os.unlink(os.path.join(env.cfg, "claude-oauth-token"))           # the fake one is not used
    extra = [] if MODE == "packages" else ["--egress", MODE]
    root = env.make_project("live", extra=extra, secrets_files=False)
    print(f"[claude-live run {harness.RUN}: egress {MODE}, project {root}]\n", flush=True)

    p = env.ab("run", "--agent", "claude", "--project", "live", "--no-tty", "--",
               "-p", "Reply with exactly: AB_OK", timeout=600)
    with open(os.path.join(REAL_DIR, "claude-oauth-token")) as f:
        token = f.read().strip()
    out = p.stdout + p.stderr
    if token:
        out = out.replace(token, "[REDACTED]")
    print("--- claude output ---")
    print(out.strip()[-3000:] or "(none)")

    logs = os.path.join(env.data, "logs")
    access = ""
    for fn in sorted(os.listdir(logs)) if os.path.isdir(logs) else []:
        if fn.endswith("-proxy.log"):
            with open(os.path.join(logs, fn)) as f:
                access += f.read()
    print("\n--- proxy access log ---")
    print(access.strip() or "(empty)")
    denied = sorted({m.group(1) for m in re.finditer(r" [A-Z]+ (\S+) 403 - TCP_DENIED", access)})
    print("\n--- denied destinations ---")
    print("\n".join(denied) if denied else "(none)")

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
    if token and token in p.stdout + p.stderr:
        leaks.append("ab output")

    ok = re.search(r"^AB_OK\s*$", p.stdout, re.M) is not None
    left = harness.leftovers()
    print("\n--- result ---")
    print(f"{'PASS' if ok else 'FAIL'}  Claude answered AB_OK through the proxy (egress {MODE})")
    print(f"{'PASS' if not leaks else 'FAIL'}  token in no log, state file or output"
          + (f": {', '.join(leaks)}" if leaks else ""))
    print(f"{'PASS' if not left else 'FAIL'}  session torn down" + (f": {left}" if left else ""))
    harness.cleanup()
    return 0 if ok and not leaks and not left else 1


if __name__ == "__main__":
    sys.exit(main())
