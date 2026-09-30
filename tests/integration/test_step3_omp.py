"""Step 3 gate: OMP boots, the debugger and the browser work through the proxy; keys stay out of everything.

Uses a fake ~/.omp built from tests/fixtures/omp/tree.txt (every file a canary), fake provider keys, and a
stub OpenAI-compatible model on 127.0.0.1 inside the sandbox. The stub checks OMP sends the session's key,
and can answer with an `eval` tool call that uses OMP's `browser`, driving OMP's own browser through the proxy.
`make omp-live` (Tim) repeats the boot with the real ~/.omp and the real local model.
"""
import os
import re
import secrets
import subprocess
import unittest

from tests import ompfake

from . import harness

ENV = None
IMAGE = "localhost/agentbox-omp:latest"
KEYS = {"stubai": f"sk-proj-FAKESTUB{secrets.token_hex(12)}", "openai": f"sk-proj-FAKEOAI{secrets.token_hex(12)}"}
SEED_EXTRA = """  stubai:
    api: openai-completions
    baseUrl: http://127.0.0.1:8766/v1
    models:
      - id: stub-model
        contextWindow: 32000
        maxOut: 2000
"""

STUB = r"""
const http = require('http'), fs = require('fs');
const log = (s) => fs.appendFileSync('/tmp/stub.log', s + '\n');
function sse(res, chunks) {
  res.writeHead(200, {'content-type': 'text/event-stream'});
  for (const c of chunks) res.write(`data: ${JSON.stringify(c)}\n\n`);
  res.write('data: [DONE]\n\n'); res.end();
}
const base = {id: 'chatcmpl-stub', object: 'chat.completion.chunk', created: 1, model: 'stub-model'};
const usage = {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2};
// OMP 18's browser is the `browser` object inside its eval tool, not a tool of its own.
const BROWSER_JS = "const tab = await browser.open({ name: 'abt', url: 'https://example.com/' });\n" +
  "const t = await tab.title();\nconsole.log('TITLE=' + t);\nawait tab.close();\nt";
http.createServer((req, res) => {
  let body = '';
  req.on('data', d => body += d);
  req.on('end', () => {
    const auth = req.headers['authorization'] || '';
    const key = process.env.AB_KEY_STUBAI || 'unset';
    log(`${req.method} ${req.url} ${auth.includes(key) ? 'KEY' : 'no-key'}`);
    if (req.url.endsWith('/models')) {
      res.writeHead(200, {'content-type': 'application/json'});
      return res.end(JSON.stringify({object: 'list', data: [{id: 'stub-model', object: 'model'}]}));
    }
    let j = {}; try { j = JSON.parse(body); } catch (e) {}
    const msgs = j.messages || [];
    const toolMsg = msgs.find(m => m.role === 'tool');
    const text = JSON.stringify(msgs.filter(m => m.role === 'user'));
    let delta, finish;
    if (toolMsg) {
      fs.appendFileSync('/tmp/stub-tool.txt', JSON.stringify(toolMsg.content) + '\n');
      delta = {role: 'assistant', content: 'STUB_TOOL_DONE'}; finish = 'stop';
    } else if (text.includes('USE_BROWSER')) {
      delta = {role: 'assistant', tool_calls: [{index: 0, id: 'call_1', type: 'function',
               function: {name: 'eval', arguments: JSON.stringify({language: 'js', code: BROWSER_JS, timeout: 150})}}]};
      finish = 'tool_calls';
    } else {
      delta = {role: 'assistant', content: 'STUB_OK'}; finish = 'stop';
    }
    if (j.stream) {
      return sse(res, [{...base, choices: [{index: 0, delta, finish_reason: null}]},
                       {...base, choices: [{index: 0, delta: {}, finish_reason: finish}], usage}]);
    }
    const message = {role: 'assistant', content: delta.content || null};
    if (delta.tool_calls) message.tool_calls = delta.tool_calls.map(({index, ...t}) => t);
    res.writeHead(200, {'content-type': 'application/json'});
    res.end(JSON.stringify({...base, object: 'chat.completion', usage,
                            choices: [{index: 0, message, finish_reason: finish}]}));
  });
}).listen(8766, '127.0.0.1');
"""

DAP_PROBE = r"""
import json, os, socket, subprocess, sys, time
target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dap_target.py")
proc = subprocess.Popen([sys.executable, "-m", "debugpy", "--listen", "127.0.0.1:5678", "--wait-for-client", target])
for _ in range(150):
    try:
        s = socket.create_connection(("127.0.0.1", 5678), timeout=30)
        break
    except OSError:
        time.sleep(0.2)
else:
    sys.exit("DAP_NO_CONNECT")
buf = b""
seq = 0
def send(command, **arguments):
    global seq
    seq += 1
    body = json.dumps({"seq": seq, "type": "request", "command": command, "arguments": arguments}).encode()
    s.sendall(b"Content-Length: %d\r\n\r\n" % len(body) + body)
def recv():
    global buf
    while b"\r\n\r\n" not in buf:
        buf += s.recv(65536)
    head, buf = buf.split(b"\r\n\r\n", 1)
    n = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0].split(b":")[1])
    while len(buf) < n:
        buf += s.recv(65536)
    msg, buf = json.loads(buf[:n]), buf[n:]
    return msg
def wait(pred, what):
    end = time.time() + 60
    while time.time() < end:
        m = recv()
        if pred(m):
            return m
    sys.exit("DAP_TIMEOUT " + what)
send("initialize", adapterID="abt", clientID="abt", linesStartAt1=True, pathFormat="path")
wait(lambda m: m.get("type") == "response" and m.get("command") == "initialize" and m.get("success"), "init")
print("DAP_INIT", flush=True)
send("attach", justMyCode=True)
wait(lambda m: m.get("event") == "initialized", "initialized")
send("setBreakpoints", source={"path": target}, breakpoints=[{"line": 2}])
send("configurationDone")
m = wait(lambda m: m.get("event") == "stopped", "stopped")
print("DAP_STOPPED", m["body"].get("reason"), flush=True)
send("continue", threadId=m["body"].get("threadId", 1))
send("disconnect")
proc.wait(timeout=60)
print("DAP_DONE", proc.returncode, flush=True)
"""

DAP_TARGET = "x = 41\ny = x + 1\nprint('TARGET_RAN', y)\n"


def image_present():
    return subprocess.run(["podman", "image", "exists", IMAGE]).returncode == 0


def setUpModule():
    global ENV
    if not image_present():
        raise unittest.SkipTest(f"{IMAGE} missing: run make omp-image")
    ENV = harness.Env()
    omp_home = ompfake.build(os.path.join(harness.BASE, "omp"))
    with open(os.path.join(ENV.cfg, "agentbox.toml"), "a") as f:
        f.write(f'omp_home = "{omp_home}"\n')
    with open(os.path.join(ompfake.FIXTURE, "models.redacted.yml")) as f:
        seed = "".join(line for line in f if not line.strip().startswith("apiKey"))
    os.makedirs(os.path.join(ENV.cfg, "omp"))
    with open(os.path.join(ENV.cfg, "omp", "models.yml"), "w") as f:
        f.write(seed + SEED_EXTRA)
    kdir = os.path.join(ENV.cfg, "keys")
    os.makedirs(kdir, mode=0o700)
    for provider, value in KEYS.items():
        path = os.path.join(kdir, provider)
        with open(path, "w") as f:
            f.write(value + "\n")
        os.chmod(path, 0o600)
    for name, extra in (("o1", []), ("oopen", ["--egress", "open"]), ("olock", [])):
        root = ENV.make_project(name, preset="lockdown" if name == "olock" else "python", extra=extra)
        for fn, text in (("stub.js", STUB), ("dap_probe.py", DAP_PROBE), ("dap_target.py", DAP_TARGET)):
            with open(os.path.join(root, fn), "w") as f:
                f.write(text)
    print(f"\n[itest run {harness.RUN}: step 3, omp_home {omp_home}]")


def tearDownModule():
    harness.cleanup()


def shell(project, script, timeout=900):
    return ENV.ab("run", "--agent", "omp", "--project", project, "--shell", "--no-tty", "--", "-c", script,
                  timeout=timeout)


def redact(text):
    for v in KEYS.values():
        text = text.replace(v, "[KEY]")
    return text


class Step3(unittest.TestCase):
    maxDiff = None

    def assertNoKeys(self, *texts):
        for t in texts:
            for v in KEYS.values():
                self.assertNotIn(v, t)

    def test_selftest(self):
        for project in ("o1", "olock"):
            results, p = ENV.selftest("omp", project)
            print(f"\nselftest omp/{project}:\n{harness.report(results)}")
            fails = [r for r in results if r["verdict"] == "FAIL"]
            self.assertFalse(fails, harness.report(fails) + "\n" + redact(p.stderr)[-2000:])
            labels = " | ".join(r["label"] for r in results)
            for want in ("AB_KEY_STUBAI is set", "AB_KEY_OPENAI is set", "tool chromium present",
                         "proxied CONNECT api.openai.com:443 allowed", "keys not in podman's container config",
                         "keys not on disk"):
                self.assertIn(want, labels)
            self.assertNoKeys(p.stdout, p.stderr)

    def test_omp_answers_through_its_model_config_with_the_key(self):
        root = os.path.join(ENV.projects, "o1")
        script = (f"node {root}/stub.js & sleep 1; "
                  "omp -p --no-title --model stubai/stub-model 'Reply with exactly: STUB_OK'; "
                  "echo \"OMP_EXIT=$?\"; echo '--- stub log'; cat /tmp/stub.log; "
                  "echo '--- sessions'; ls ~/.omp/agent/sessions")
        p = shell("o1", script)
        out = redact(p.stdout)
        print(f"\n{out[-2500:]}\n{redact(p.stderr)[-1500:]}")
        answer, _, rest = p.stdout.partition("--- stub log")
        stub_log, _, sessions = rest.partition("--- sessions")
        self.assertRegex(answer, r"(?m)^STUB_OK\s*$", "OMP answered through the stub")
        self.assertIn("OMP_EXIT=0", answer)
        self.assertRegex(stub_log, r"POST \S*chat/completions KEY", "OMP sent the session key")
        self.assertNotIn("no-key", "\n".join(l for l in stub_log.splitlines() if "chat/completions" in l))
        # sessions are named after the project's real path
        encoded = root.strip("/").replace("/", "-")
        self.assertTrue(any(encoded in d for d in sessions.split()), f"{encoded!r} not in {sessions.split()}")
        state = os.path.join(ENV.data, "o1", "omp", "agent", "sessions")
        self.assertTrue(any(encoded in d for d in os.listdir(state)), "session history kept on the host")
        self.assertNoKeys(p.stdout, p.stderr)

    def test_keys_not_at_rest(self):
        # after the other tests have run OMP; also scans the project roots and the logs
        self.test_omp_answers_through_its_model_config_with_the_key()
        hits = []
        for where in (ENV.data, ENV.run_dir, ENV.projects):
            for dirpath, _dirs, files in os.walk(where):
                for fn in files:
                    path = os.path.join(dirpath, fn)
                    try:
                        with open(path, "rb") as f:
                            data = f.read()
                    except OSError:
                        continue
                    if any(v.encode() in data for v in KEYS.values()):
                        hits.append(os.path.relpath(path, harness.BASE))
        self.assertEqual(hits, [], "a key was found at rest")

    def test_only_named_omp_files_are_visible(self):
        p = shell("o1", "grep -rRso 'OMPCANARY:[^ ]*' ~/.omp/ /root/.omp/ | sed 's/.*OMPCANARY://' | sort -u; "
                        # agent/ and logs/ are the project's own state (OMP creates its databases there),
                        # so the host's files are checked there by canary only, above.
                        "for f in agent/models.yml.backup agent/.mcp.json agent/mags-codedev.yaml "
                        "agent/extensions/pi-permission-system versions/legacy-20260905172136 logs/omp.2026-07-30.log "
                        "puppeteer stats.db .omp-sandbox.json; do "
                        "test -e ~/.omp/$f && echo VISIBLE:$f; done; "
                        "touch ~/.omp/plugins/x 2>/dev/null && echo WRITABLE:plugins; "
                        "touch ~/.omp/agent/config.yml 2>/dev/null && echo WRITABLE:config; "
                        "touch ~/.omp/agent/models.yml 2>/dev/null && echo WRITABLE:models; "
                        "touch ~/.omp/agent/newfile && echo AGENT_DIR_WRITABLE; "
                        "python -c 'import abt_pylib_probe as m; print(m.VALUE)'; "
                        "grep -q OMPCANARY ~/.omp/plugins/node_modules/pi-fake-ext/index.js && echo PLUGIN_LINK_OK; "
                        "cat ~/.omp/agent/models.yml | head -c 0; grep -c apiKey ~/.omp/agent/models.yml")
        print(f"\n{redact(p.stdout)}\n{redact(p.stderr)[-800:]}")
        self.assertEqual(p.returncode, 0, p.stderr)
        seen = [l.strip() for l in p.stdout.splitlines() if l.strip() and ":" not in l and not l.isdigit()
                and l.strip() not in ("AGENT_DIR_WRITABLE", "pylibs-importable", "PLUGIN_LINK_OK")]
        for rel in seen:
            self.assertTrue(ompfake.is_mounted(rel), f"{rel} is visible but not in the mount list")
        self.assertIn("agent/config.yml", seen)
        self.assertNotIn("VISIBLE:", p.stdout)
        self.assertNotIn("WRITABLE:", p.stdout)
        self.assertIn("AGENT_DIR_WRITABLE", p.stdout)
        self.assertIn("pylibs-importable", p.stdout)
        self.assertIn("PLUGIN_LINK_OK", p.stdout, "a plugin's /root/.omp/versions/... link resolves in the session")

    def test_debugger(self):
        root = os.path.join(ENV.projects, "o1")
        p = shell("o1", f"cd {root} && python dap_probe.py", timeout=600)
        print(f"\n{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
        self.assertIn("DAP_INIT", p.stdout)
        self.assertRegex(p.stdout, r"DAP_STOPPED breakpoint")
        self.assertIn("TARGET_RAN 42", p.stdout)

    def test_chromium_follows_the_egress_mode(self):
        cmd = ("chromium --headless --no-sandbox --disable-gpu --disable-dev-shm-usage "
               "--proxy-server=\"$HTTPS_PROXY\" --dump-dom https://example.com/ 2>/dev/null | "
               "grep -c 'Example Domain' || true")
        opened = shell("oopen", cmd, timeout=600)
        refused = shell("o1", cmd, timeout=600)
        print(f"\nopen: {opened.stdout.strip()} / packages: {refused.stdout.strip()}\n{opened.stderr[-600:]}")
        self.assertNotEqual(opened.stdout.strip(), "0", "open egress: example.com loads through the proxy")
        self.assertEqual(refused.stdout.strip(), "0", "packages egress: example.com is refused")

    def test_omp_browser_tool_through_the_proxy(self):
        root = os.path.join(ENV.projects, "oopen")
        script = (f"node {root}/stub.js & sleep 1; "
                  "omp -p --no-title --model stubai/stub-model 'USE_BROWSER now'; echo \"OMP_EXIT=$?\"; "
                  "echo '--- tool result'; cat /tmp/stub-tool.txt; echo '--- stub log'; cat /tmp/stub.log; "
                  "echo '--- run'; ls -la ~/.omp/run 2>&1 | head; findmnt -no FSTYPE --target ~/.omp/run; "
                  "echo '--- omp logs'; grep -hiE 'browser|chrom|puppeteer|proxy|error' ~/.omp/logs/*.log | tail -40")
        p = shell("oopen", script, timeout=900)
        print(f"\n{redact(p.stdout)[-4000:]}\n{redact(p.stderr)[-2000:]}")
        tool = p.stdout.partition("--- tool result")[2].partition("--- stub log")[0]
        self.assertIn("STUB_TOOL_DONE", p.stdout)
        self.assertIn("Example Domain", tool, "OMP's browser opened example.com through the proxy")
        self.assertRegex(p.stdout.partition("--- run")[2], r"(?m)^tmpfs\s*$", "~/.omp/run is a real tmpfs")
        log = os.path.join(ENV.data, "logs")
        proxied = ""
        for fn in os.listdir(log):
            if fn.endswith("-proxy.log"):
                with open(os.path.join(log, fn)) as f:
                    proxied += f.read()
        self.assertRegex(proxied, r"example\.com:443", "the browser's request went through squid")

    def test_bun_reaches_the_local_model_through_squid(self):
        # OMP runs on bun, whose fetch honours HTTPS_PROXY/NO_PROXY. OMP's own PI_PROXY is deliberately not
        # set: it bypasses private ranges, so the local model at 10.0.0.2 would go direct and find no route.
        js = ("fetch('http://10.0.0.2:8080/v1/models').then(r => console.log('STATUS', r.status))"
              ".catch(e => console.log('ERR', e.message))")
        p = shell("o1", f"test -z \"$PI_PROXY\" && echo PI_PROXY_UNSET; bun -e \"{js}\"", timeout=300)
        print(f"\n{p.stdout}\n{p.stderr[-500:]}")
        self.assertIn("PI_PROXY_UNSET", p.stdout)
        self.assertRegex(p.stdout, r"STATUS (200|502|503|504)", "answered by the model or by squid, not refused")
        logs = os.path.join(ENV.data, "logs")
        text = ""
        for fn in os.listdir(logs):
            if fn.endswith("-proxy.log"):
                with open(os.path.join(logs, fn)) as f:
                    text += f.read()
        self.assertIn("10.0.0.2:8080", text, "the request went through squid")

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
