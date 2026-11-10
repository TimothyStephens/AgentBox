"""Review finding #1: does Claude Code persist its token once it has authenticated?

The fake token never authenticates against the real API, so the normal suite only proves the token stays
out of what the launcher writes. Here a stub Anthropic API (node, inside the agent container, on
127.0.0.1) accepts the fake token, so Claude completes a real turn and its post-authentication code runs.
Then the per-project Claude state, backups, logs and run dir are grepped for the token.
`make claude-live` remains the check with the real token and the real API.
"""
import os
import unittest

from . import harness
from .harness import FAKE_TOKEN

ENV = None

STUB = r"""
const http = require('http'), fs = require('fs');
http.createServer((req, res) => {
  let body = '';
  req.on('data', d => body += d);
  req.on('end', () => {
    const auth = req.headers['authorization'] || req.headers['x-api-key'] || '';
    fs.appendFileSync('/tmp/stub.log', `${req.method} ${req.url} ${auth.includes(process.env.CLAUDE_CODE_OAUTH_TOKEN) ? 'TOKEN' : 'no-token'}\n`);
    if (req.method === 'POST' && req.url.startsWith('/v1/messages') && !req.url.includes('count_tokens')) {
      let stream = false; try { stream = JSON.parse(body).stream; } catch (e) {}
      const msg = {id: 'msg_stub', type: 'message', role: 'assistant', model: 'claude-sonnet-4-5', content: [],
                   stop_reason: null, stop_sequence: null, usage: {input_tokens: 1, output_tokens: 1}};
      if (stream) {
        res.writeHead(200, {'content-type': 'text/event-stream'});
        const ev = (t, d) => res.write(`event: ${t}\ndata: ${JSON.stringify(d)}\n\n`);
        ev('message_start', {type: 'message_start', message: msg});
        ev('content_block_start', {type: 'content_block_start', index: 0, content_block: {type: 'text', text: ''}});
        ev('content_block_delta', {type: 'content_block_delta', index: 0, delta: {type: 'text_delta', text: 'STUB_OK'}});
        ev('content_block_stop', {type: 'content_block_stop', index: 0});
        ev('message_delta', {type: 'message_delta', delta: {stop_reason: 'end_turn', stop_sequence: null}, usage: {output_tokens: 1}});
        ev('message_stop', {type: 'message_stop'});
        return res.end();
      }
      msg.content = [{type: 'text', text: 'STUB_OK'}]; msg.stop_reason = 'end_turn';
      res.writeHead(200, {'content-type': 'application/json'}); return res.end(JSON.stringify(msg));
    }
    if (req.url.includes('count_tokens')) { res.writeHead(200, {'content-type': 'application/json'}); return res.end('{"input_tokens":1}'); }
    res.writeHead(200, {'content-type': 'application/json'}); res.end('{}');
  });
}).listen(8765, '127.0.0.1');
"""


def setUpModule():
    global ENV
    ENV = harness.Env()
    ENV.make_project("rest", secrets_files=False)
    with open(os.path.join(ENV.projects, "rest", "stub.js"), "w") as f:
        f.write(STUB)


def tearDownModule():
    harness.cleanup()


class TokenAtRest(unittest.TestCase):
    def test_claude_authenticated_turn_leaves_no_token_on_disk(self):
        root = os.path.join(ENV.projects, "rest")
        script = (f"node {root}/stub.js & sleep 1; "
                  "ANTHROPIC_BASE_URL=http://127.0.0.1:8765 claude -p 'Reply with exactly: STUB_OK' "
                  "--model claude-sonnet-4-5; echo \"CLAUDE_EXIT=$?\"; echo '--- stub log'; cat /tmp/stub.log")
        p = ENV.ab("session", "start", "claude", "rest", "--shell", "--no-tty", "--", "-c", script,
                   timeout=600)
        out = p.stdout.replace(FAKE_TOKEN, "[TOKEN]")
        print(f"\n{out[-2500:]}\n{p.stderr[-800:]}")
        self.assertIn("STUB_OK", p.stdout.split("--- stub log")[0], "Claude completed a turn against the stub")
        self.assertIn("TOKEN", p.stdout.split("--- stub log")[1], "Claude sent the session token to the API")
        hits = []
        for where in (ENV.data, ENV.run_dir, root):
            for dirpath, dirnames, files in os.walk(where):
                for fn in files:
                    path = os.path.join(dirpath, fn)
                    try:
                        with open(path, "rb") as f:
                            if FAKE_TOKEN.encode() in f.read():
                                hits.append(os.path.relpath(path, harness.BASE))
                    except OSError:
                        pass
        state = os.path.join(ENV.data, "projects", "rest", "claude")
        print("claude state files:", sorted(os.path.relpath(os.path.join(d, f), state)
                                             for d, _, fs in os.walk(state) for f in fs)[:40])
        self.assertEqual(hits, [], "token found at rest")
        self.assertNotIn(FAKE_TOKEN, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
