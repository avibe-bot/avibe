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

    def test_responses_stream_does_not_trust_terminal_snapshot_arguments(self) -> None:
        result = probe.TransportResult(
            200,
            None,
            [
                {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather"}}},
                {"kind": "event", "event": {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "not-json"}},
                {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}},
                {"kind": "event", "event": {"type": "response.completed", "response": {"status": "completed", "output": [{"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}' }]}}},
            ],
            False,
            0,
        )
        turn = probe._parse_responses_stream(result)
        self.assertIn("arguments_invalid_json", turn.parse_errors)
        self.assertNotEqual(turn.tool_calls[0].arguments, {"city": "Shanghai"})

    def test_chat_stream_reassembles_tool_fragments(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":'}}]}}]}},
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Shanghai"}'}}]}}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertEqual(turn.tool_calls[0].arguments, {"city": "Shanghai"})
        self.assertTrue(turn.terminal)

    def test_chat_usage_reasoning_tokens_count_as_reasoning_signal(self) -> None:
        turn = probe._parse_chat_document(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "tool_calls"}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 3}},
            }
        )
        self.assertTrue(turn.reasoning_present)

    def test_loopback_opener_has_no_environment_proxy(self) -> None:
        proxy_handlers = [handler for handler in probe.OPENER.handlers if isinstance(handler, probe.urllib.request.ProxyHandler)]
        self.assertEqual(proxy_handlers, [])

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

        def fake_request(path: str, payload: dict[str, object], *, client_protocol: str, stream: bool) -> probe.TransportResult:
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
                {"content": [{"type": "text", "text": f"{probe.SYSTEM_MARKER} {probe.SYSTEM_SCOPE_OK} WEATHER_OK"}], "stop_reason": "end_turn"},
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

    def test_parallel_prompt_names_both_tools_and_city_is_constrained(self) -> None:
        prompt = probe._user_prompt(True)
        self.assertIn("lookup_weather", prompt)
        self.assertIn("lookup_time", prompt)
        schema = probe._tool_definitions()[0]["input_schema"]
        self.assertEqual(schema["properties"]["city"]["enum"], ["Shanghai"])

    def test_stream_order_rejects_lifecycle_violation(self) -> None:
        ordered = [
            {"kind": "event", "sequence": 0, "event": {"type": "content_block_start", "index": 0}},
            {"kind": "event", "sequence": 1, "event": {"type": "content_block_delta", "index": 0}},
            {"kind": "event", "sequence": 2, "event": {"type": "content_block_stop", "index": 0}},
            {"kind": "event", "sequence": 3, "event": {"type": "message_stop"}},
        ]
        invalid = [ordered[1], ordered[0], ordered[2], ordered[3]]
        self.assertTrue(probe._stream_order_ok("anthropic", ordered))
        self.assertFalse(probe._stream_order_ok("anthropic", invalid))

    def test_chat_stream_order_rejects_content_after_finish(self) -> None:
        allowed = [
            {"kind": "event", "sequence": 0, "event": {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}},
            {"kind": "event", "sequence": 1, "event": {"choices": [], "usage": {"completion_tokens": 1}}},
            {"kind": "done", "sequence": 2},
        ]
        invalid = [
            allowed[0],
            {"kind": "event", "sequence": 1, "event": {"choices": [{"delta": {"content": "late"}}]}},
            {"kind": "done", "sequence": 2},
        ]
        self.assertTrue(probe._stream_order_ok("chat", allowed))
        self.assertFalse(probe._stream_order_ok("chat", invalid))

    def test_protocol_stop_reasons_are_required(self) -> None:
        first = probe._parse_chat_document({"choices": [{"message": {"tool_calls": []}, "finish_reason": "stop"}]})
        projection = probe._validate_first(first, ("lookup_weather",), stream=False)
        self.assertFalse(projection["checks"]["stop_reason"])

    def test_system_scope_rejects_user_conflict_marker(self) -> None:
        second = probe._parse_anthropic_document(
            {"content": [{"type": "text", "text": f"{probe.SYSTEM_MARKER} {probe.SYSTEM_SCOPE_OK} {probe.USER_SCOPE_LEAK} WEATHER_OK"}], "stop_reason": "end_turn"}
        )
        projection = probe._validate_second(second, ("lookup_weather",), stream=False)
        self.assertFalse(projection["checks"]["system_scope"])

    def test_stream_gate_rejects_invalid_utf8_and_deadline(self) -> None:
        result = probe.TransportResult(200, None, [], False, 1, False, True)
        turn = probe._parse_turn("anthropic", result, stream=True)
        projection = probe._validate_first(turn, ("lookup_weather",), stream=True)
        self.assertFalse(projection["checks"]["parsed"])
        self.assertFalse(projection["checks"]["stream_order"])
        self.assertFalse(projection["checks"]["stream_deadline"])

    def test_stream_deadline_expires_while_partial_line_keeps_arriving(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size: int) -> bytes:
                self.assert_size = size
                return b"x"

        class Opener:
            def open(self, request, timeout):
                return Response()

        original_opener = probe.OPENER
        original_timeout = probe.STREAM_TOTAL_TIMEOUT
        original_monotonic = probe.time.monotonic
        clock = [0.0]

        def monotonic() -> float:
            clock[0] += 0.02
            return clock[0]

        try:
            probe.OPENER = Opener()
            probe.STREAM_TOTAL_TIMEOUT = 0.03
            probe.time.monotonic = monotonic
            result = probe._request("/v1/messages", {}, client_protocol="anthropic", stream=True)
        finally:
            probe.OPENER = original_opener
            probe.STREAM_TOTAL_TIMEOUT = original_timeout
            probe.time.monotonic = original_monotonic
        self.assertTrue(result.deadline_expired)

    def test_exhausted_503_is_blocked_for_openai_targets(self) -> None:
        original_request = probe._request
        original_retries = probe.MAX_503_RETRIES
        try:
            probe.MAX_503_RETRIES = 1
            probe._request = lambda *args, **kwargs: probe.TransportResult(503, None, [], False, 0)
            result, _, blocked = probe._request_with_retries(
                probe.CaseSpec("capacity", "anthropic", "responses", "/v1/messages", False, False),
                {"model": "src/model"},
                "src/model",
            )
        finally:
            probe._request = original_request
            probe.MAX_503_RETRIES = original_retries
        self.assertEqual(result.status, 503)
        self.assertTrue(blocked)

    def test_fallback_use_is_reported_without_model_identity(self) -> None:
        original_request = probe._request
        original_retries = probe.MAX_503_RETRIES
        original_model = probe.MODELS["anthropic"]
        original_fallback = probe.ANTHROPIC_FALLBACK_MODEL
        calls = [0]

        def fake_request(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return probe.TransportResult(503, None, [], False, 0)
            if calls[0] == 2:
                return probe.TransportResult(
                    200,
                    {"choices": [{"message": {"content": "", "reasoning_content": "r", "tool_calls": [{"id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}, "finish_reason": "tool_calls"}]},
                    [],
                    False,
                    0,
                )
            return probe.TransportResult(
                200,
                {"choices": [{"message": {"content": f"{probe.SYSTEM_MARKER} {probe.SYSTEM_SCOPE_OK} WEATHER_OK"}, "finish_reason": "stop"}]},
                [],
                False,
                0,
            )

        try:
            probe._request = fake_request
            probe.MAX_503_RETRIES = 1
            probe.MODELS["anthropic"] = "primary/model"
            probe.ANTHROPIC_FALLBACK_MODEL = "fallback/model"
            result = probe._run_case(probe.CaseSpec("fallback", "chat", "anthropic", "/v1/chat/completions", False, False))
        finally:
            probe._request = original_request
            probe.MAX_503_RETRIES = original_retries
            probe.MODELS["anthropic"] = original_model
            probe.ANTHROPIC_FALLBACK_MODEL = original_fallback
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["evidence_model_scope"], "fallback")
        projection = json.dumps(result)
        self.assertNotIn("primary/model", projection)
        self.assertNotIn("fallback/model", projection)


if __name__ == "__main__":
    unittest.main()
