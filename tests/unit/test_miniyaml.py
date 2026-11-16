"""miniyaml edge cases (item 50). The general reader tests live with the OMP adapter (test_omp.MiniYaml)."""
import unittest

from agentbox import miniyaml
from agentbox.errors import AgentboxError


class NestedSequences(unittest.TestCase):
    def test_an_inline_nested_sequence_is_refused_not_read_as_a_string(self):
        """`- - a` is a list inside a list in YAML; it used to come back as the string "- a"."""
        for text in ("a:\n  - - x\n", "- - x\n", "- -\n", "a:\n  - k: v\n  - - y\n"):
            with self.subTest(text=text), self.assertRaises(AgentboxError):
                miniyaml.loads(text)

    def test_the_block_form_still_reads(self):
        self.assertEqual(miniyaml.loads("f:\n  -\n    - nested\n"), {"f": [["nested"]]})

    def test_a_plain_scalar_that_starts_with_a_dash_is_still_a_string(self):
        self.assertEqual(miniyaml.loads("a:\n  - -x\n  - '- quoted'\n"), {"a": ["-x", "- quoted"]})


class SingleQuoteEscape(unittest.TestCase):
    def test_a_doubled_quote_does_not_end_the_string_for_comment_stripping(self):
        self.assertEqual(miniyaml.loads("a: 'it''s # not a comment'\n"), {"a": "it's # not a comment"})
        self.assertEqual(miniyaml.loads("a: 'x '' # y' # real comment\n"), {"a": "x ' # y"})
        self.assertEqual(miniyaml.loads("- 'a''b # c'\n"), ["a'b # c"])

    def test_an_escaped_double_quote_does_not_end_the_string_either(self):
        self.assertEqual(miniyaml.loads('a: "say \\"hi # there\\"" # c\n'), {"a": 'say "hi # there"'})

    def test_a_comment_after_a_quoted_string_is_still_stripped(self):
        self.assertEqual(miniyaml.loads("a: 'it''s' # gone\n"), {"a": "it's"})


if __name__ == "__main__":
    unittest.main()
