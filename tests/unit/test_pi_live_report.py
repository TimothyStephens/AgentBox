"""make pi-live step 2: the bash tool must really have run (19 Nov host run: a model answered the echoed text
without calling the tool, and the old check passed `output` because the prompt itself was in the events)."""
import json
import unittest

from tests.live.pi_live import bash_events, bash_prompt

TOKEN = "ab-7f3c9e"


def lines(*evs):
    return "\n".join(json.dumps(e) for e in evs)


PROMPT_ECHO = {"type": "agent_end", "messages": [{"role": "user", "content": [{"type": "text",
                                                  "text": bash_prompt("token.txt")}]}]}


class BashEvents(unittest.TestCase):
    def test_model_answering_without_the_tool_fails(self):
        out = lines(PROMPT_ECHO, {"type": "message_end", "message": {"role": "assistant",
                                                                     "content": [{"type": "text", "text": TOKEN}]}})
        self.assertEqual(bash_events(out, TOKEN), (False, False))

    def test_prompt_never_contains_the_token(self):
        self.assertNotIn(TOKEN, bash_prompt("token.txt"))

    def test_bash_call_with_the_token_in_its_result_passes(self):
        out = lines(PROMPT_ECHO,
                    {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
                     "args": {"command": "cat token.txt"}},
                    {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
                     "result": {"content": [{"type": "text", "text": TOKEN + "\n"}]}, "isError": False})
        self.assertEqual(bash_events(out, TOKEN), (True, True))

    def test_token_from_another_tool_does_not_count(self):
        out = lines({"type": "tool_execution_start", "toolCallId": "r1", "toolName": "read"},
                    {"type": "tool_execution_end", "toolCallId": "r1", "toolName": "read",
                     "result": {"content": [{"type": "text", "text": TOKEN}]}, "isError": False})
        self.assertEqual(bash_events(out, TOKEN), (False, False))


if __name__ == "__main__":
    unittest.main()
