import json
import unittest

import probe


class ProbeParserTests(unittest.TestCase):
    def test_anthropic_followup_preserves_observed_id_and_thinking(self) -> None:
        turn = probe._parse_anthropic_document(
            {
                "content": [
                    {"type": "thinking", "thinking": "internal", "signature": "sig"},
                    {"type": "tool_use", "id": "toolu_actual", "name": "lookup_weather", "input": {"city": "Shanghai"}},
                ],
                "stop_reason": "tool_use",
            }
        )
        payload = probe._anthropic_payload(model="src/model", stream=False, parallel=False, followup=turn)
        self.assertEqual(payload["messages"][1]["content"][0]["type"], "thinking")
        self.assertEqual(payload["messages"][1]["content"][0]["thinking"], "internal")
        self.assertEqual(payload["messages"][2]["content"][0]["tool_use_id"], "toolu_actual")
        projection = probe._validate_first(turn, ("lookup_weather",), stream=False)
        self.assertNotIn("toolu_actual", json.dumps(projection))

    def test_responses_stream_reassembles_function_arguments(self) -> None:
        result = probe.TransportResult(
            200,
            None,
            [
                {"kind": "event", "type": "response.output_item.added", "event": {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather"}}},
                {"kind": "event", "type": "response.function_call_arguments.delta", "event": {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"city":'}},
                {"kind": "event", "type": "response.function_call_arguments.delta", "event": {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '"Shanghai"}'}},
                {"kind": "event", "type": "response.completed", "event": {"type": "response.completed"}},
            ],
            True,
            0,
        )
        turn = probe._parse_responses_stream(result)
        self.assertEqual(turn.tool_calls[0].arguments, {"city": "Shanghai"})
        self.assertTrue(turn.terminal)

    def test_chat_stream_reassembles_tool_fragments(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":'}}]}}]}},
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Shanghai"}'}}]}}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertEqual(turn.tool_calls[0].arguments, {"city": "Shanghai"})
        self.assertTrue(turn.terminal)

    def test_semantic_checks_reject_missing_reasoning_and_markers(self) -> None:
        first = probe._parse_anthropic_document(
            {"content": [{"type": "tool_use", "id": "id", "name": "lookup_weather", "input": {"city": "Shanghai"}}], "stop_reason": "tool_use"}
        )
        first_checks = probe._validate_first(first, ("lookup_weather",), stream=False)
        self.assertFalse(first_checks["checks"]["reasoning_present"])
        second = probe._parse_anthropic_document({"content": [{"type": "text", "text": "WEATHER_OK"}], "stop_reason": "end_turn"})
        second_checks = probe._validate_second(second, ("lookup_weather",), stream=False)
        self.assertFalse(second_checks["checks"]["system_marker"])

    def test_case_round_trip_is_driven_by_first_response(self) -> None:
        requests: list[dict[str, object]] = []

        def fake_request(path: str, payload: dict[str, object], *, stream: bool) -> probe.TransportResult:
            requests.append(payload)
            if len(requests) == 1:
                return probe.TransportResult(
                    200,
                    {
                        "content": [
                            {"type": "thinking", "thinking": "internal", "signature": "sig"},
                            {"type": "tool_use", "id": "toolu_from_first_response", "name": "lookup_weather", "input": {"city": "Shanghai"}},
                        ],
                        "stop_reason": "tool_use",
                    },
                    [],
                    False,
                    0,
                )
            return probe.TransportResult(
                200,
                {"content": [{"type": "text", "text": f"{probe.SYSTEM_MARKER} WEATHER_OK"}], "stop_reason": "end_turn"},
                [],
                False,
                0,
            )

        original_request = probe._request
        try:
            probe._request = fake_request
            result = probe._run_case(probe.CaseSpec("fake", "anthropic", "responses", "/v1/messages", False, False))
        finally:
            probe._request = original_request
        self.assertEqual(len(requests), 2)
        followup = requests[1]
        self.assertEqual(followup["messages"][2]["content"][0]["tool_use_id"], "toolu_from_first_response")
        self.assertTrue(result["checks"]["second_system_marker"])
        self.assertTrue(result["checks"]["second_tool_outputs"])

    def test_redirect_handler_blocks_redirect(self) -> None:
        request = probe.urllib.request.Request("http://127.0.0.1:15220/v1/messages")
        self.assertIsNone(probe._NoRedirectHandler().redirect_request(request, None, 302, "Found", {}, "http://example.test"))

    def test_payload_uses_valid_thinking_and_qualified_models(self) -> None:
        payload = probe._anthropic_payload(model="src/model", stream=False, parallel=False)
        self.assertGreaterEqual(payload["thinking"]["budget_tokens"], 1024)
        self.assertGreater(payload["max_tokens"], payload["thinking"]["budget_tokens"])
        self.assertEqual(payload["tool_choice"], {"type": "auto"})
        self.assertTrue(probe._valid_qualified_model("src/model"))
        self.assertFalse(probe._valid_qualified_model("bare-model"))


if __name__ == "__main__":
    unittest.main()
