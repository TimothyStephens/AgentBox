"""make omp-live: OMP with the real ~/.omp, the real provider keys and the real models, through squid.

Run by Tim, never by the builder: it mounts pieces of the real ~/.omp and reads the real key files in place
(~/.config/agentbox/keys/, never copied). The keyless model seed ~/.config/agentbox/omp/models.yml is copied
into the test config if it exists (it holds no keys; the launcher refuses one that does). Everything else
lives under /tmp/abt-<run>/ and is removed afterwards.

  1. `omp -p` must answer AB_OK with the default model (the local model server must be up, or a keyed
     provider must be the default in ~/.omp/agent/config.yml);
  2. OMP is asked to use its browser tool on https://pypi.org/ and its bash tool to import debugpy;
     reported as INFO from OMP's own output;
  3. no key value may appear in any state file, log, backup or output.
AB_LIVE_EGRESS=locked|packages|open (default packages). AB_LIVE_MODEL=<provider/model> picks the model.
"""
import os
import re
import shutil
import sys

from tests.integration import harness

MODE = os.environ.get("AB_LIVE_EGRESS", "packages")
MODEL = os.environ.get("AB_LIVE_MODEL", "")
REAL_CFG = os.path.expanduser("~/.config/agentbox")
TOOLS_PROMPT = (
    "Do these two steps, each with the named tool, and carry on if one fails. "
    "1) Use the browser tool to open https://pypi.org/ and tell me the page title. "
    "2) Use the bash tool to run: python -c \"import debugpy; print('AB_DEBUGPY_OK')\". "
    "Then reply with two lines: BROWSER <the title, or fail> and DEBUGPY ok or DEBUGPY fail."
)


def real_keys():
    d = os.path.join(REAL_CFG, "keys")
    out = []
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        with open(os.path.join(d, fn)) as f:
            v = f.read().strip()
        if v:
            out.append(v)
    return out


def section(title, text):
    print(f"\n--- {title} ---")
    print(text.strip() if text and text.strip() else "(none)")


def main():
    env = harness.Env()
    with open(os.path.join(env.cfg, "agentbox.toml"), "a") as f:
        f.write(f'keys_dir = "{os.path.join(REAL_CFG, "keys")}"\n')
    seed = os.path.join(REAL_CFG, "omp", "models.yml")
    if os.path.isfile(seed):
        os.makedirs(os.path.join(env.cfg, "omp"))
        shutil.copyfile(seed, os.path.join(env.cfg, "omp", "models.yml"))
    extra = [] if MODE == "packages" else ["--egress", MODE]
    root = env.make_project("omplive", extra=extra, secrets_files=False)
    print(f"[omp-live run {harness.RUN}: egress {MODE}, project {root}, seed {'yes' if os.path.isfile(seed) else 'none'}]",
          flush=True)
    keys = real_keys()

    def redact(s):
        for k in keys:
            s = s.replace(k, "[REDACTED]")
        return s

    model = ["--model", MODEL] if MODEL else []
    p1 = env.ab("run", "--agent", "omp", "--project", "omplive", "--no-tty", "--",
                "-p", "--no-title", *model, "Reply with exactly: AB_OK", timeout=900)
    section("1. omp -p output", redact(p1.stdout + p1.stderr)[-3000:])
    p2 = env.ab("run", "--agent", "omp", "--project", "omplive", "--no-tty", "--",
                "-p", "--no-title", *model, TOOLS_PROMPT, timeout=1200)
    section("2. omp tools output", redact(p2.stdout + p2.stderr)[-4000:])
    logs = os.path.join(env.data, "logs")
    access = ""
    for fn in sorted(os.listdir(logs)) if os.path.isdir(logs) else []:
        if fn.endswith("-proxy.log"):
            with open(os.path.join(logs, fn)) as f:
                access += f.read()
    section("proxy access log", access[-4000:])
    section("denied destinations", "\n".join(sorted({l.split()[3] for l in access.splitlines()
                                                    if len(l.split()) > 4 and l.split()[4] == "403"})))

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
    left = harness.leftovers()
    print("\n--- result ---")
    print(f"{'PASS' if ok1 else 'FAIL'}  1. OMP answered AB_OK through the proxy (egress {MODE})")
    print(f"INFO  2. browser: {'worked' if re.search(r'BROWSER (?!fail)', p2.stdout) else 'failed or unclear'}; "
          f"debugpy: {'worked' if 'DEBUGPY ok' in p2.stdout or 'AB_DEBUGPY_OK' in p2.stdout else 'failed or unclear'}")
    print(f"{'PASS' if not leaks else 'FAIL'}  no key in any log, state file or output ({len(keys)} key(s) checked)"
          + (f": {', '.join(leaks)}" if leaks else ""))
    print(f"{'PASS' if not left else 'FAIL'}  sessions torn down" + (f": {left}" if left else ""))
    harness.cleanup()
    return 0 if ok1 and not leaks and not left else 1


if __name__ == "__main__":
    sys.exit(main())
