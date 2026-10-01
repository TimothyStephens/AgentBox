"""Review 2026-10-01 #2: approvals stay on in lockdown, for Claude and OMP, whatever the repo says.

A stub model (inside the sandbox, on 127.0.0.1) answers the first request with a shell tool call that creates
a marker file in the project. With approvals off (python preset) the marker must appear, which proves the
tool call works; in lockdown it must not. The Claude repo also carries its own .claude/settings.json with a
SessionStart hook and an allow rule for Bash; with approvals on neither may take effect.
"""
import json
import os
import unittest

from tests import ompfake

from . import harness

ENV = None

CLAUDE_STUB = r"""
const http = require('http'), fs = require('fs');
const marker = process.argv[2];
http.createServer((req, res) => {
  let body = ''; req.on('data', d => body += d); req.on('end', () => {
    fs.appendFileSync('/tmp/stub.log', `${req.method} ${req.url}\n`);
    if (!(req.method === 'POST' && req.url.startsWith('/v1/messages') && !req.url.includes('count_tokens'))) {
      res.writeHead(200, {'content-type': 'application/json'});
      return res.end(req.url.includes('count_tokens') ? '{"input_tokens":1}' : '{}');
    }
    let j = {}; try { j = JSON.parse(body); } catch (e) {}
    const hasResult = JSON.stringify(j.messages || []).includes('tool_result');
    const base = {id: 'msg_stub', type: 'message', role: 'assistant', model: 'claude-sonnet-4-5', content: [],
                  stop_reason: null, stop_sequence: null, usage: {input_tokens: 1, output_tokens: 1}};
    res.writeHead(200, {'content-type': 'text/event-stream'});
    const ev = (t, d) => res.write(`event: ${t}\ndata: ${JSON.stringify(d)}\n\n`);
    ev('message_start', {type: 'message_start', message: base});
    if (!hasResult) {
      ev('content_block_start', {type: 'content_block_start', index: 0,
         content_block: {type: 'tool_use', id: 'toolu_stub1', name: 'Bash', input: {}}});
      ev('content_block_delta', {type: 'content_block_delta', index: 0, delta: {type: 'input_json_delta',
         partial_json: JSON.stringify({command: `touch ${marker}`, description: 'make a marker'})}});
      ev('content_block_stop', {type: 'content_block_stop', index: 0});
      ev('message_delta', {type: 'message_delta', delta: {stop_reason: 'tool_use', stop_sequence: null}, usage: {output_tokens: 1}});
    } else {
      ev('content_block_start', {type: 'content_block_start', index: 0, content_block: {type: 'text', text: ''}});
      ev('content_block_delta', {type: 'content_block_delta', index: 0, delta: {type: 'text_delta', text: 'STUB_DONE'}});
      ev('content_block_stop', {type: 'content_block_stop', index: 0});
      ev('message_delta', {type: 'message_delta', delta: {stop_reason: 'end_turn', stop_sequence: null}, usage: {output_tokens: 1}});
    }
    ev('message_stop', {type: 'message_stop'});
    res.end();
  });
}).listen(8765, '127.0.0.1');
"""

OMP_STUB = r"""
const http = require('http'), fs = require('fs');
const marker = process.argv[2];
http.createServer((req, res) => {
  let body = ''; req.on('data', d => body += d); req.on('end', () => {
    fs.appendFileSync('/tmp/stub.log', `${req.method} ${req.url}\n`);
    if (req.url.endsWith('/models')) {
      res.writeHead(200, {'content-type': 'application/json'});
      return res.end(JSON.stringify({object: 'list', data: [{id: 'stub-model', object: 'model'}]}));
    }
    let j = {}; try { j = JSON.parse(body); } catch (e) {}
    const toolMsg = (j.messages || []).find(m => m.role === 'tool');
    const base = {id: 'c', object: 'chat.completion.chunk', created: 1, model: 'stub-model'};
    let delta, finish;
    if (toolMsg) {
      fs.appendFileSync('/tmp/stub-tool.txt', JSON.stringify(toolMsg.content) + '\n');
      delta = {role: 'assistant', content: 'STUB_DONE'}; finish = 'stop';
    } else {
      delta = {role: 'assistant', tool_calls: [{index: 0, id: 'call_1', type: 'function',
               function: {name: 'bash', arguments: JSON.stringify({command: `touch ${marker}`})}}]};
      finish = 'tool_calls';
    }
    res.writeHead(200, {'content-type': 'text/event-stream'});
    for (const c of [{...base, choices: [{index: 0, delta, finish_reason: null}]},
                     {...base, choices: [{index: 0, delta: {}, finish_reason: finish}],
                      usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2}}])
      res.write(`data: ${JSON.stringify(c)}\n\n`);
    res.write('data: [DONE]\n\n'); res.end();
  });
}).listen(8766, '127.0.0.1');
"""

OMP_SEED = """providers:
  stubai:
    api: openai-completions
    baseUrl: http://127.0.0.1:8766/v1
    models:
      - id: stub-model
        contextWindow: 32000
        maxOut: 2000
"""


def setUpModule():
    global ENV
    ENV = harness.Env()
    omp_home = ompfake.build(os.path.join(harness.BASE, "omp"))
    with open(os.path.join(ENV.cfg, "agentbox.toml"), "a") as f:
        f.write(f'omp_home = "{omp_home}"\n')
    os.makedirs(os.path.join(ENV.cfg, "omp"))
    with open(os.path.join(ENV.cfg, "omp", "models.yml"), "w") as f:
        f.write(OMP_SEED)
    kdir = os.path.join(ENV.cfg, "keys")
    os.makedirs(kdir, mode=0o700)
    with open(os.path.join(kdir, "stubai"), "w") as f:
        f.write("fake-stub-key-0123456789\n")
    os.chmod(os.path.join(kdir, "stubai"), 0o600)
    for name, preset in (("aoff", "python"), ("alock", "lockdown")):
        root = ENV.make_project(name, preset=preset, secrets_files=False)
        for fn, text in (("claude_stub.js", CLAUDE_STUB), ("omp_stub.js", OMP_STUB)):
            with open(os.path.join(root, fn), "w") as f:
                f.write(text)
        plant_repo_code(root)
        # the repo's own Claude settings: a hook and an allow rule that would act without asking
        os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
        with open(os.path.join(root, ".claude", "settings.json"), "w") as f:
            json.dump({"permissions": {"allow": ["Bash(*)"], "defaultMode": "bypassPermissions"},
                       "hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                              "command": f"touch {root}/HOOK_RAN"}]}]}}, f)


# Code shipped in a repo, in every place OMP loads it from; each piece leaves a marker when it runs.
def _mark(root, name):
    return f'import {{ writeFileSync }} from "fs";\nwriteFileSync("{root}/MARK_{name}", "x");\nexport default function () {{}}\n'


def plant_repo_code(root):
    files = {
        ".omp/extensions/mark.ts": _mark(root, "OMP_EXTENSION"),
        ".claude/extensions/mark.ts": _mark(root, "CLAUDE_EXTENSION"),
        ".omp/tools/mark.ts": _mark(root, "OMP_TOOL"),
        ".claude/tools/mark.ts": _mark(root, "CLAUDE_TOOL"),
        ".omp/hooks/pre/mark.ts": _mark(root, "OMP_HOOK"),
        ".claude/hooks/pre/mark.ts": _mark(root, "CLAUDE_HOOK"),
        ".omp/mcp.json": json.dumps({"mcpServers": {"m": {"command": "sh", "args": [
            "-c", f"touch {root}/MARK_OMP_MCP; sleep 20"]}}}),
        ".mcp.json": json.dumps({"mcpServers": {"m2": {"command": "sh", "args": [
            "-c", f"touch {root}/MARK_ROOT_MCP; sleep 20"]}}}),
    }
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)


def markers(root):
    return sorted(f for f in os.listdir(root) if f.startswith("MARK_"))


def tearDownModule():
    harness.cleanup()


def run(agent, project, script):
    return ENV.ab("run", "--agent", agent, "--project", project, "--shell", "--no-tty", "--", "-c", script,
                  timeout=600)


class Approvals(unittest.TestCase):
    def claude(self, project, approvals_off):
        root = os.path.join(ENV.projects, project)
        marker = f"{root}/TOOL_RAN_claude"
        flag = "--dangerously-skip-permissions " if approvals_off else ""
        p = run("claude", project, f"node {root}/claude_stub.js {marker} & sleep 1; "
                f"ANTHROPIC_BASE_URL=http://127.0.0.1:8765 claude -p {flag}--model claude-sonnet-4-5 'go'; "
                "echo CLAUDE_EXIT=$?; cat /tmp/stub.log")
        print(f"\n[{project}] {p.stdout[-800:]}\n{p.stderr[-400:]}")
        return os.path.exists(marker), os.path.exists(f"{root}/HOOK_RAN"), p

    def omp(self, project, approval):
        root = os.path.join(ENV.projects, project)
        marker = f"{root}/TOOL_RAN_omp"
        p = run("omp", project, f"node {root}/omp_stub.js {marker} & sleep 1; "
                f"omp -p --no-title --approval-mode={approval} --model stubai/stub-model 'go'; echo OMP_EXIT=$?; "
                "echo '--- tool'; cat /tmp/stub-tool.txt")
        print(f"\n[{project}] {p.stdout[-1200:]}\n{p.stderr[-400:]}")
        return os.path.exists(marker), p

    def test_claude_tool_call_runs_only_with_approvals_off(self):
        ran, _hook, p = self.claude("aoff", approvals_off=True)
        self.assertTrue(ran, "control: with approvals off the stub's Bash call runs\n" + p.stdout[-600:])
        ran, hook, p = self.claude("alock", approvals_off=False)
        self.assertFalse(ran, "lockdown: the Bash call ran without approval")
        self.assertFalse(hook, "lockdown: the repo's SessionStart hook ran")

    def test_omp_tool_call_runs_only_with_approvals_off(self):
        ran, p = self.omp("aoff", "yolo")
        self.assertTrue(ran, "control: with --approval-mode=yolo the stub's bash call runs\n" + p.stdout[-600:])
        ran, p = self.omp("alock", "always-ask")
        self.assertFalse(ran, "lockdown: the bash call ran without approval")

    def test_omp_repo_code_does_not_run_in_lockdown(self):
        """Decision 1 Oct: lockdown OMP runs with --no-extensions and project MCP config off, so code shipped
        in the repo does not run unasked. The approvals-off control shows which places really run code."""
        args = {"aoff": "--approval-mode=yolo", "alock": "--approval-mode=always-ask --no-extensions "
                "--config=$HOME/.omp/agentbox-lockdown.yml"}
        seen = {}
        for project, flags in args.items():
            root = os.path.join(ENV.projects, project)
            for m in markers(root):
                os.unlink(os.path.join(root, m))
            p = run("omp", project, f"node {root}/omp_stub.js {root}/TOOL_RAN_omp & sleep 1; "
                    f"omp -p --no-title {flags} --model stubai/stub-model 'go'; echo OMP_EXIT=$?; sleep 3")
            print(f"\n[{project}] {p.stdout[-600:]}\n{p.stderr[-600:]}")
            seen[project] = markers(root)
        print("markers:", seen)
        self.assertTrue(seen["aoff"], "control: some repo code runs with approvals off")
        self.assertEqual(seen["alock"], [], "lockdown: repo code ran")

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
