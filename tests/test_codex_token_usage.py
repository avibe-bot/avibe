"""Codex backend → current context-window occupancy for the status-bubble footer.

Covers ``CodexEventHandler._extract_context_tokens``: it reads the SNAPSHOT
``tokenUsage.last.totalTokens`` from a ``thread/tokenUsage/updated`` notification
(the "latest active context size" the Codex CLI's own context bar uses), NOT the
cumulative ``total``. It must support the legacy v1 snake_case shape and degrade
to 0 when the field is missing/malformed.
"""

import unittest

from modules.agents.codex.event_handler import CodexEventHandler


class ExtractContextTokensTests(unittest.TestCase):
    def test_v2_camelcase_reads_last_total(self):
        params = {
            "threadId": "t1",
            "turnId": "u1",
            "tokenUsage": {
                "last": {"totalTokens": 38000, "inputTokens": 37000, "outputTokens": 1000},
                "total": {"totalTokens": 250000},  # cumulative — must NOT be used
                "modelContextWindow": 200000,
            },
        }
        self.assertEqual(CodexEventHandler._extract_context_tokens(params), 38000)

    def test_v1_snakecase_info_shape(self):
        params = {"info": {"last_token_usage": {"total_tokens": 12345}}}
        self.assertEqual(CodexEventHandler._extract_context_tokens(params), 12345)

    def test_missing_or_malformed_is_zero(self):
        self.assertEqual(CodexEventHandler._extract_context_tokens({}), 0)
        self.assertEqual(CodexEventHandler._extract_context_tokens({"tokenUsage": {"last": None}}), 0)
        self.assertEqual(CodexEventHandler._extract_context_tokens("nope"), 0)
        self.assertEqual(
            CodexEventHandler._extract_context_tokens({"tokenUsage": {"last": {"totalTokens": "x"}}}), 0
        )

    def test_total_field_is_ignored_when_last_absent(self):
        # Only `last` is occupancy; a payload with just `total` yields 0.
        params = {"tokenUsage": {"total": {"totalTokens": 999999}}}
        self.assertEqual(CodexEventHandler._extract_context_tokens(params), 0)


if __name__ == "__main__":
    unittest.main()
