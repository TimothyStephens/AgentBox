import json
import unittest

from tests.live.parse import denied, tool_outcomes


def ev(kind, *items):
    return json.dumps({"type": kind, "message": {"content": list(items)}})


class LiveParseTest(unittest.TestCase):
    def test_tool_outcomes(self):
        stream = "\n".join([
            json.dumps({"type": "system"}),
            ev("assistant", {"type": "tool_use", "id": "s1", "name": "WebSearch"}),
            ev("user", {"type": "tool_result", "tool_use_id": "s1", "content": [{"type": "text", "text": "results"}]}),
            ev("assistant", {"type": "tool_use", "id": "f1", "name": "WebFetch"}),
            ev("user", {"type": "tool_result", "tool_use_id": "f1", "is_error": True, "content": "403 Forbidden"}),
            ev("assistant", {"type": "tool_use", "id": "b1", "name": "Bash"}),
            ev("user", {"type": "tool_result", "tool_use_id": "b1", "content": "AB_BASH_OK"}),
            "not json",
        ])
        out = tool_outcomes(stream)
        self.assertEqual({k: v[0] for k, v in out.items()}, {"WebSearch": True, "WebFetch": False, "Bash": True})
        self.assertIn("403", out["WebFetch"][1])

    def test_missing_and_bash_without_marker(self):
        stream = "\n".join([
            ev("assistant", {"type": "tool_use", "id": "b1", "name": "Bash"}),
            ev("user", {"type": "tool_result", "tool_use_id": "b1", "content": "something else"}),
        ])
        out = tool_outcomes(stream)
        self.assertEqual(out["WebSearch"], (False, "not called"))
        self.assertFalse(out["Bash"][0])

    def test_denied(self):
        log = ("1.0 10.89.3.3 CONNECT api.anthropic.com:443 200 1.2.3.4 TCP_TUNNEL/HIER_DIRECT\n"
               "1.1 10.89.3.3 CONNECT claude.ai:443 403 - TCP_DENIED/HIER_NONE\n"
               "1.2 10.89.3.3 GET http://example.com/ 403 - TCP_DENIED/HIER_NONE\n"
               "1.3 10.89.3.3 CONNECT claude.ai:443 403 - TCP_DENIED/HIER_NONE\n")
        self.assertEqual(denied(log), ["claude.ai:443", "http://example.com/"])


class EmptyProxyLogs(unittest.TestCase):
    def test_answered_with_empty_log_is_flagged(self):
        from tests.live.parse import empty_proxy_logs
        runs = [("1", True, "1.0 10.89.0.2 CONNECT api.anthropic.com:443 200\n"), ("2", True, ""),
                ("3", False, ""), ("4", True, "  \n")]
        self.assertEqual(empty_proxy_logs(runs), ["2", "4"])
