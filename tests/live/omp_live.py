"""make omp-live: OMP with the real ~/.omp, the real provider keys and the real models, through squid.

Run by Tim, never by the builder: it mounts pieces of the real ~/.omp and reads the real key files in place
(~/.config/agentbox/keys/, never copied). The keyless model seed ~/.config/agentbox/omp/models.yml is copied
into the test config if it exists (it holds no keys; the launcher refuses one that does). Everything else
lives under /tmp/abt-<run>/ and is removed afterwards.

  1. `omp -p` must answer AB_OK with the default model (the local model server must be up, or a keyed
     provider must be the default in ~/.config/agentbox/omp/config.yml);
  2. OMP is asked to use its browser tool on https://pypi.org/ and its bash tool to import debugpy;
     reported as INFO from OMP's own output;
  3. no key value may appear in any state file, log, backup or output.
AB_LIVE_EGRESS=locked|packages|open (default packages). AB_LIVE_MODEL=<provider/model> picks the model.
"""
import os
import re
import shutil
import subprocess
import sys
import time

from tests.integration import harness
from tests.live.parse import empty_proxy_logs

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


class Run:
    """One `omp -p`: what came back, how long it took, and whether it came back at all.

    A timeout used to raise straight out of `main`, so the first host run died on step 1 and never reached
    the leak scan or the teardown check -- a slow model cost the whole report instead of one line. It is
    still a FAIL naming the command (the harness guarantee), just one the rest of the run survives."""

    def __init__(self, proc, seconds, timed_out=""):
        self.proc, self.seconds, self.timed_out = proc, seconds, timed_out

    @property
    def stdout(self):
        return self.proc.stdout if self.proc else ""

    @property
    def stderr(self):
        return self.proc.stderr if self.proc else ""

    @property
    def returncode(self):
        return self.proc.returncode if self.proc else None

    def why(self, pattern):
        """Why this run did not produce what was asked for -- empty, wrong, or never finished.

        "FAIL 1. OMP answered AB_OK" said nothing about which of those happened, and the three have
        completely different causes (a slow model, a changed model, a broken session)."""
        if self.timed_out:
            return f"did not finish in time ({self.seconds:.0f}s)"
        if not self.stdout.strip():
            return f"stdout was empty (exit {self.returncode}, {self.seconds:.0f}s)"
        return (f"stdout had no {pattern} line (exit {self.returncode}, {self.seconds:.0f}s, "
                f"{len(self.stdout.splitlines())} line(s))")


def run_omp(env, prompt_args, timeout):
    started = time.time()
    try:
        p = env.ab("session", "start", "omp", "omplive", "--no-tty", "--", *prompt_args,
                   timeout=timeout)
        return Run(p, time.time() - started)
    except AssertionError as e:
        return Run(None, time.time() - started, str(e))


def model_calls(access):
    """(number of model requests, their total bytes) from a squid access log -- so "the model is slow" or
    "the model was never reached" is visible in the report rather than inferred from a wall-clock gap."""
    n, size = 0, 0
    for line in access.splitlines():
        f = line.split()
        if len(f) > 7 and f[2] in ("POST", "GET") and f[4] == "200":
            n += 1
            size += int(f[7]) if f[7].isdigit() else 0
    return n, size


def main():
    env = harness.Env()
    with open(os.path.join(env.cfg, "agentbox.toml"), "a") as f:
        f.write(f'keys_dir = "{os.path.join(REAL_CFG, "keys")}"\n')
    seed = os.path.join(REAL_CFG, "omp", "models.yml")
    if os.path.isfile(seed):
        os.makedirs(os.path.join(env.cfg, "omp"), exist_ok=True)
        shutil.copyfile(seed, os.path.join(env.cfg, "omp", "models.yml"))
    # The REAL installed plugin tree, so this check keeps the one property only it has: that a plugin
    # actually loads. It used to come for free, when `omp_home` defaulted to the host's ~/.omp; the tree
    # lives in the config dir now and omp-live runs with a temporary one, so it is brought in explicitly.
    # `cp -al` makes hard links rather than copies -- the tree is node_modules-sized -- and the session
    # mounts it read-only, so nothing in the session can reach the real files through them.
    real_tree = os.path.join(REAL_CFG, "omp", "plugins")
    tree = os.path.join(env.cfg, "omp", "plugins")
    if os.path.isdir(real_tree):
        os.makedirs(os.path.dirname(tree), exist_ok=True)
        cp = subprocess.run(["cp", "-al", real_tree, tree], capture_output=True, text=True)
        if cp.returncode != 0:
            print(f"[omp-live: could not link the plugin tree: {cp.stderr.strip()[:200]}]", flush=True)
        else:
            print(f"[omp-live: plugin tree linked from {real_tree}]", flush=True)
    else:
        print(f"[omp-live: no installed plugin tree at {real_tree}; OMP runs without plugins "
              "(`ab profile install omp`)]", flush=True)
    extra = [] if MODE == "packages" else ["--egress", MODE]
    root = env.make_project("omplive", extra=extra, secrets_files=False)
    print(f"[omp-live run {harness.RUN}: egress {MODE}, project {root}, seed {'yes' if os.path.isfile(seed) else 'none'}]",
          flush=True)
    keys = real_keys()

    def redact(s):
        for k in keys:
            s = s.replace(k, "[REDACTED]")
        return s

    logs = os.path.join(env.data, "logs")

    def new_proxy_logs(seen):
        """Access-log text of sessions that ended since `seen`; each session has its own file."""
        text = ""
        for fn in sorted(os.listdir(logs)) if os.path.isdir(logs) else []:
            if fn.endswith("-proxy.log") and fn not in seen:
                seen.add(fn)
                with open(os.path.join(logs, fn)) as f:
                    text += f.read()
        return text

    seen = set()
    model = ["--model", MODEL] if MODEL else []
    p1 = run_omp(env, ["-p", "--no-title", *model, "Reply with exactly: AB_OK"], 900)
    section("1. omp -p output", redact(p1.stdout + p1.stderr)[-3000:])
    if p1.timed_out:
        section("1. timed out", redact(p1.timed_out)[-2000:])
    access1 = new_proxy_logs(seen)
    section("1. proxy access log", access1[-3000:])
    p2 = run_omp(env, ["-p", "--no-title", *model, TOOLS_PROMPT], 1200)
    section("2. omp tools output", redact(p2.stdout + p2.stderr)[-4000:])
    if p2.timed_out:
        section("2. timed out", redact(p2.timed_out)[-2000:])
    access2 = new_proxy_logs(seen)
    section("2. proxy access log", access2[-4000:])
    access = access1 + access2
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
    # OMP only reaches a model through squid (the local one included), so an answer with an empty log means
    # the capture is broken.
    empty = empty_proxy_logs([("1", ok1, access1), ("2", p2.returncode == 0 and bool(p2.stdout.strip()), access2)])
    left = harness.leftovers()
    calls1, bytes1 = model_calls(access1)
    print("\n--- result ---")
    print(f"{'PASS' if ok1 else 'FAIL'}  1. OMP answered AB_OK through the proxy (egress {MODE})"
          + (f": {p1.why('AB_OK')}" if not ok1 else f" ({p1.seconds:.0f}s)"))
    print(f"INFO  1. {calls1} model request(s) through the proxy, {bytes1} byte(s) in "
          f"{p1.seconds:.0f}s"
          + ("  -- the model was never reached: check it is up and that its host is allowed"
             if not calls1 else "")
          + ("  -- slow; `AB_LIVE_MODEL=<provider/model>` tries another"
             if calls1 and p1.seconds > 120 else ""))
    print(f"INFO  2. browser: {'worked' if re.search(r'BROWSER (?!fail)', p2.stdout) else 'failed or unclear'}; "
          f"debugpy: {'worked' if 'DEBUGPY ok' in p2.stdout or 'AB_DEBUGPY_OK' in p2.stdout else 'failed or unclear'}")
    print(f"{'PASS' if not empty else 'FAIL'}  every session that answered has a proxy access log"
          + (f" (empty for session {', '.join(empty)})" if empty else ""))
    print(f"{'PASS' if not leaks else 'FAIL'}  no key in any log, state file or output ({len(keys)} key(s) checked)"
          + (f": {', '.join(leaks)}" if leaks else ""))
    print(f"{'PASS' if not left else 'FAIL'}  sessions torn down" + (f": {left}" if left else ""))
    harness.cleanup()
    return 0 if ok1 and not leaks and not left and not empty else 1


if __name__ == "__main__":
    sys.exit(main())
