"""make pi-live: Pi with the real provider keys and the real model seed, through squid (docs/pi-proposal.md).

Run by Tim, never by the builder: it reads the real key files in place (~/.config/agentbox/keys/, never copied).
The keyless model seed ~/.config/agentbox/omp/models.yml (shared with OMP, P4) and Pi's settings seed
~/.config/agentbox/pi/settings.json are copied into the test config when they exist. Everything else lives under
/tmp/abt-<run>/ and is removed afterwards.

  1. `pi -p` must answer AB_OK (AB_LIVE_MODEL=<provider/model>, or a default model in the settings seed);
  2. `pi -p --mode json` is asked to `cat` a file holding a random token with its bash tool: the JSON events must
     show a bash tool call whose result holds the token (the prompt never contains it, so a model cannot answer
     without running the tool, as one did on 19 Nov with `echo AB_BASH_OK`);
  3. the project ships a `.pi/settings.json` naming a package: nothing may be installed (no npm registry or git
     host in the proxy log) and nothing may reach pi.dev (PI_OFFLINE=1, PI_TELEMETRY=0, --no-approve);
  4. no key value may appear in any state file, log, backup or output.
AB_LIVE_EGRESS=locked|packages|open (default packages).
"""
import json
import os
import re
import secrets
import shutil
import sys
import time

from tests.integration import harness
from tests.live.omp_live import Run, model_calls, real_keys, section, tls_tunnels
from tests.live.parse import empty_proxy_logs

MODE = os.environ.get("AB_LIVE_EGRESS", "packages")
MODEL = os.environ.get("AB_LIVE_MODEL", "")
REAL_CFG = os.path.expanduser("~/.config/agentbox")
SEEDS = (("omp", "models.yml"), ("pi", "settings.json"))
TOKEN_FILE = "ab-bash-token.txt"


def bash_prompt(token_file):
    return f"Use the bash tool to run exactly this command: cat {token_file}. Then reply with its output."
FORBIDDEN_HOSTS = ("registry.npmjs.org", "pi.dev", "github.com", "codeload.github.com")


def no_model_reason(model, real_cfg):
    path = os.path.join(real_cfg, "pi", "settings.json")
    if model:
        return None
    try:
        with open(path) as f:
            if json.load(f).get("defaultModel"):
                return None
    except (OSError, ValueError):
        pass
    return ("no model chosen: set AB_LIVE_MODEL=<provider/model> (e.g. `AB_LIVE_MODEL=openrouter/z-ai/glm-5.3-flash "
            f"make pi-live`) or put a defaultModel in {path}.")


def copy_seeds(real_cfg, cfg):
    copied = []
    for agent, fn in SEEDS:
        src = os.path.join(real_cfg, agent, fn)
        if os.path.isfile(src) and not os.path.islink(src):
            os.makedirs(os.path.join(cfg, agent), exist_ok=True)
            shutil.copyfile(src, os.path.join(cfg, agent, fn))
            copied.append(f"{agent}/{fn}")
    return copied


def run_pi(env, prompt_args, timeout):
    started = time.time()
    try:
        p = env.ab("session", "start", "pi", "pilive", "--no-tty", "--", *prompt_args, timeout=timeout)
        return Run(p, time.time() - started)
    except AssertionError as e:
        try:
            env.ab("session", "kill", "pilive", timeout=180)
        except AssertionError:
            pass
        return Run(None, time.time() - started, str(e))


def bash_events(stdout, token):
    """(saw a bash tool call, saw `token` in a bash tool result) from `--mode json` JSON lines. Only a bash
    tool_execution_end counts as output: the prompt and the model's own text appear in other events."""
    called = output = False
    bash_ids = set()
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or str(ev.get("toolName", "")) != "bash":
            continue
        if ev.get("type") == "tool_execution_start":
            called = True
            bash_ids.add(ev.get("toolCallId"))
        elif ev.get("type") == "tool_execution_end" and ev.get("toolCallId") in bash_ids:
            if token in json.dumps(ev.get("result")):
                output = True
    return called, output


def main():
    reason = no_model_reason(MODEL, REAL_CFG)
    if reason:
        print(f"pi-live: {reason}", file=sys.stderr)
        return 2
    env = harness.Env()
    with open(os.path.join(env.cfg, "agentbox.toml"), "a") as f:
        f.write(f'keys_dir = "{os.path.join(REAL_CFG, "keys")}"\n')
    seeds = copy_seeds(REAL_CFG, env.cfg)
    extra = [] if MODE == "packages" else ["--egress", MODE]
    root = env.make_project("pilive", extra=extra, secrets_files=False)
    os.makedirs(os.path.join(root, ".pi"), exist_ok=True)
    with open(os.path.join(root, ".pi", "settings.json"), "w") as f:
        json.dump({"packages": ["npm:left-pad@1.3.0", "git:github.com/example/never@main"]}, f)
    token = f"AB_BASH_{secrets.token_hex(8)}"
    with open(os.path.join(root, TOKEN_FILE), "w") as f:
        f.write(token + "\n")
    print(f"[pi-live run {harness.RUN}: egress {MODE}, project {root}, seeds {', '.join(seeds) or 'none'}]",
          flush=True)
    keys = real_keys()

    def redact(s):
        for k in keys:
            s = s.replace(k, "[REDACTED]")
        return s

    logs = os.path.join(env.data, "logs")

    def new_proxy_logs(seen):
        text = ""
        for fn in sorted(os.listdir(logs)) if os.path.isdir(logs) else []:
            if fn.endswith("-proxy.log") and fn not in seen:
                seen.add(fn)
                with open(os.path.join(logs, fn)) as f:
                    text += f.read()
        return text

    seen = set()
    model = ["--model", MODEL] if MODEL else []
    p1 = run_pi(env, ["-p", *model, "Reply with exactly: AB_OK"], 900)
    section("1. pi -p output", redact(p1.stdout + p1.stderr)[-3000:])
    if p1.timed_out:
        section("1. timed out", redact(p1.timed_out)[-2000:])
    access1 = new_proxy_logs(seen)
    section("1. proxy access log", access1[-3000:])
    p2 = run_pi(env, ["-p", "--mode", "json", *model, bash_prompt(TOKEN_FILE)], 900)
    section("2. pi --mode json output (tail)", redact(p2.stdout + p2.stderr)[-4000:])
    if p2.timed_out:
        section("2. timed out", redact(p2.timed_out)[-2000:])
    access2 = new_proxy_logs(seen)
    section("2. proxy access log", access2[-3000:])
    access = access1 + access2
    denied = sorted({l.split()[3] for l in access.splitlines() if len(l.split()) > 4 and l.split()[4] == "403"})
    section("denied destinations", "\n".join(denied))
    reached = sorted({h for h in FORBIDDEN_HOSTS for l in access.splitlines() if h in l})

    leaks = []
    for where in (env.data, env.run_dir, env.cfg, env.projects):
        for dirpath, _d, files in os.walk(where):
            for fn in files:
                try:
                    with open(os.path.join(dirpath, fn), "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                if any(k.encode() in data for k in keys):
                    leaks.append(os.path.join(dirpath, fn))
    for label, proc in (("run 1 output", p1), ("run 2 output", p2)):
        if any(k in proc.stdout + proc.stderr for k in keys):
            leaks.append(label)

    ok1 = re.search(r"^AB_OK\s*$", p1.stdout, re.M) is not None
    called, output = bash_events(p2.stdout, token)
    ok2 = called and output
    empty = empty_proxy_logs([("1", ok1, access1), ("2", ok2, access2)])
    left = harness.leftovers()
    calls1, bytes1 = model_calls(access1)
    tun1, tbytes1 = tls_tunnels(access1)
    print("\n--- result ---")
    print(f"{'PASS' if ok1 else 'FAIL'}  1. Pi answered AB_OK through the proxy (egress {MODE})"
          + (f": {p1.why('AB_OK')}" if not ok1 else f" ({p1.seconds:.0f}s)"))
    print(f"INFO  1. {calls1} plain model request(s), {bytes1} byte(s); {tun1} TLS tunnel(s), {tbytes1} byte(s)")
    print(f"{'PASS' if ok2 else 'FAIL'}  2. the bash tool ran (event: {'yes' if called else 'no'}, "
          f"output: {'yes' if output else 'no'})")
    print(f"{'PASS' if not reached else 'FAIL'}  3. nothing installed or reported (no {', '.join(FORBIDDEN_HOSTS)})"
          + (f": reached {', '.join(reached)}" if reached else ""))
    print(f"{'PASS' if not empty else 'FAIL'}  every session that answered has a proxy access log"
          + (f" (empty for session {', '.join(empty)})" if empty else ""))
    print(f"{'PASS' if not leaks else 'FAIL'}  no key in any log, state file or output ({len(keys)} key(s) checked)"
          + (f": {', '.join(leaks)}" if leaks else ""))
    print(f"{'PASS' if not left else 'FAIL'}  sessions torn down" + (f": {left}" if left else ""))
    harness.cleanup()
    return 0 if ok1 and ok2 and not reached and not leaks and not left and not empty else 1


if __name__ == "__main__":
    sys.exit(main())
