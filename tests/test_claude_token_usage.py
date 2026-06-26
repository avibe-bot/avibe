"""Claude backend → current context-window occupancy for the status-bubble footer.

Covers ``ClaudeAgent._extract_context_tokens``: it reads the RAW per-request usage
from an AssistantMessage (the latest one reflects current context size) and sums
input + cache_read + cache_creation + output. It must degrade to 0 when ``usage``
is missing/malformed so a missing field never breaks the turn.
"""

import unittest

from modules.agents.claude_agent import ClaudeAgent


class _Msg:
    def __init__(self, usage):
        self.usage = usage


class _UsageObj:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class ExtractContextTokensTests(unittest.TestCase):
    def test_sums_prompt_and_output_including_cache(self):
        # Full prompt sent = input + cache_read + cache_creation; + output joins
        # the context for the next request → all four count toward occupancy.
        msg = _Msg(
            {
                "input_tokens": 1200,
                "cache_read_input_tokens": 30000,
                "cache_creation_input_tokens": 800,
                "output_tokens": 345,
            }
        )
        self.assertEqual(ClaudeAgent._extract_context_tokens(msg), 1200 + 30000 + 800 + 345)

    def test_object_usage_is_supported(self):
        msg = _Msg(_UsageObj(input_tokens=100, output_tokens=50))
        # cache fields absent → counted as 0.
        self.assertEqual(ClaudeAgent._extract_context_tokens(msg), 150)

    def test_missing_usage_is_zero(self):
        self.assertEqual(ClaudeAgent._extract_context_tokens(_Msg(None)), 0)
        self.assertEqual(ClaudeAgent._extract_context_tokens(object()), 0)

    def test_malformed_fields_are_ignored(self):
        msg = _Msg({"input_tokens": "nope", "output_tokens": None, "cache_read_input_tokens": -5})
        self.assertEqual(ClaudeAgent._extract_context_tokens(msg), 0)
        msg2 = _Msg({"input_tokens": 20, "output_tokens": "x"})
        self.assertEqual(ClaudeAgent._extract_context_tokens(msg2), 20)


if __name__ == "__main__":
    unittest.main()
