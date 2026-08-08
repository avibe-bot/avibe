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

    def test_responses_stream_rejects_done_argument_snapshot_mismatch(self) -> None:
        result = probe.TransportResult(
            200,
            None,
            [
                {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather"}}},
                {"kind": "event", "event": {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"city":"Shanghai"}'}},
                {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Paris"}'}}},
                {"kind": "event", "event": {"type": "response.completed", "response": {"status": "completed", "output": []}}},
            ],
            False,
            0,
        )
        turn = probe._parse_responses_stream(result)
        self.assertIn("stream_item_snapshot_mismatch", turn.parse_errors)

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

    def test_responses_stream_rejects_identity_snapshot_mismatch(self) -> None:
        result = probe.TransportResult(
            200,
            None,
            [
                {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather"}}},
                {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_2", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}},
                {"kind": "event", "event": {"type": "response.completed", "response": {"status": "completed"}}},
            ],
            False,
            0,
        )
        turn = probe._parse_responses_stream(result)
        self.assertIn("stream_item_snapshot_mismatch", turn.parse_errors)

    def test_responses_stream_requires_argument_fragments(self) -> None:
        result = probe.TransportResult(
            200,
            None,
            [
                {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather"}}},
                {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}},
                {"kind": "event", "event": {"type": "response.completed", "response": {"status": "completed"}}},
            ],
            False,
            0,
        )
        turn = probe._parse_responses_stream(result)
        self.assertIn("stream_arguments_missing", turn.parse_errors)

    def test_chat_stream_reassembles_tool_fragments(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":'}}]}}]}},
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "function": {"arguments": '"Shanghai"}'}}]}}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertEqual(turn.tool_calls[0].arguments, {"city": "Shanghai"})
        self.assertTrue(turn.terminal)

    def test_chat_stream_allows_sparse_continuation_type(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "function": {"arguments": '{"city":'}}]}}]}},
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "type": None, "function": {"arguments": '"Shanghai"}'}}]}}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertEqual(turn.tool_calls[0].arguments, {"city": "Shanghai"})
        self.assertNotIn("tool_call_type_invalid", turn.parse_errors)

    def test_chat_stream_requires_argument_fragments(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "id": "call_1", "function": {"name": "lookup_weather"}}]}}]}},
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("stream_arguments_missing", turn.parse_errors)

    def test_chat_stream_rejects_tool_call_id_changes(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}}]}},
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "type": "function", "id": "call_2", "function": {}}]}}]}},
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("tool_call_id_changed", turn.parse_errors)
        self.assertEqual(turn.tool_calls[0].call_id, "call_1")

    def test_tool_results_must_remain_paired_with_call_ids(self) -> None:
        calls = [
            probe.ToolCall("call_weather", "lookup_weather", {"city": "Shanghai"}),
            probe.ToolCall("call_time", "lookup_time", {"city": "Shanghai"}),
        ]
        text = "\n".join(
            [
                probe._tool_output(calls[0]).replace("call_weather", "call_time"),
                probe._tool_output(calls[1]).replace("call_time", "call_weather"),
            ]
        )
        turn = probe._parse_anthropic_document({"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"})
        projection = probe._validate_second(turn, ("lookup_weather", "lookup_time"), stream=False, expected_calls=calls)
        self.assertFalse(projection["checks"]["tool_output_call_pairs"])

    def test_tool_result_tuple_check_rejects_swaps_on_one_line(self) -> None:
        calls = [
            probe.ToolCall("call_weather", "lookup_weather", {"city": "Shanghai"}),
            probe.ToolCall("call_time", "lookup_time", {"city": "Shanghai"}),
        ]
        text = " ".join(
            [
                probe._tool_output(calls[0]).replace("call_weather", "call_time"),
                probe._tool_output(calls[1]).replace("call_time", "call_weather"),
            ]
        )
        turn = probe._parse_anthropic_document({"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"})
        projection = probe._validate_second(turn, ("lookup_weather", "lookup_time"), stream=False, expected_calls=calls)
        self.assertFalse(projection["checks"]["tool_output_call_pairs"])

    def test_empty_responses_reasoning_item_is_not_a_signal(self) -> None:
        turn = probe._parse_responses_document(
            {"output": [{"type": "reasoning", "summary": [], "content": []}, {"type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}], "status": "completed"}
        )
        self.assertFalse(turn.reasoning_present)

    def test_responses_encrypted_reasoning_is_a_signal(self) -> None:
        turn = probe._parse_responses_document(
            {"output": [{"type": "reasoning", "encrypted_content": "opaque"}], "status": "completed"}
        )
        self.assertTrue(turn.reasoning_present)

    def test_responses_encrypted_reasoning_requires_string_payload(self) -> None:
        turn = probe._parse_responses_document(
            {"output": [{"type": "reasoning", "encrypted_content": True}], "status": "completed"}
        )
        self.assertFalse(turn.reasoning_present)

    def test_duplicate_argument_keys_are_rejected(self) -> None:
        arguments, error = probe._parse_arguments('{"city":"Paris","city":"Shanghai"}')
        self.assertEqual(arguments, '{"city":"Paris","city":"Shanghai"}')
        self.assertEqual(error, "arguments_duplicate_key")

    def test_nonfinite_tool_argument_is_invalid_json(self) -> None:
        arguments, error = probe._parse_arguments('{"city":"Shanghai","score":NaN}')
        self.assertEqual(arguments, '{"city":"Shanghai","score":NaN}')
        self.assertEqual(error, "arguments_invalid_json")

    def test_malformed_chat_function_is_a_parse_error(self) -> None:
        turn = probe._parse_chat_document(
            {"choices": [{"message": {"tool_calls": [{"id": "call_1", "type": "function", "function": None}]}, "finish_reason": "tool_calls"}]}
        )
        self.assertIn("tool_function_invalid", turn.parse_errors)

    def test_nonstream_chat_requires_single_assistant_choice_zero(self) -> None:
        wrong_role = probe._parse_chat_document(
            {"choices": [{"index": 0, "message": {"role": "user", "content": "wrong"}, "finish_reason": "stop"}]}
        )
        multiple = probe._parse_chat_document(
            {
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "a"}, "finish_reason": "stop"},
                    {"index": 1, "message": {"role": "assistant", "content": "b"}, "finish_reason": "stop"},
                ]
            }
        )
        nonzero = probe._parse_chat_document(
            {"choices": [{"index": 1, "message": {"role": "assistant", "content": "wrong"}, "finish_reason": "stop"}]}
        )
        self.assertIn("assistant_role_invalid", wrong_role.parse_errors)
        self.assertIn("choice_invalid", multiple.parse_errors)
        self.assertIn("choice_index_invalid", nonzero.parse_errors)

    def test_chat_stream_requires_done_sentinel(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))

    def test_chat_stream_allows_indices_to_restart_each_chunk(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0}, {"index": 1}]}}]}},
            {"kind": "event", "sequence": 1, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0}, {"index": 1}], "content": "done"}, "finish_reason": "tool_calls"}]}},
            {"kind": "done", "sequence": 2},
        ]
        self.assertTrue(probe._stream_order_ok("chat", events))

    def test_responses_stream_rejects_text_after_item_completion(self) -> None:
        events = [
            {"kind": "event", "type": "response.output_item.added", "event": {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message"}}},
            {"kind": "event", "type": "response.output_item.done", "event": {"type": "response.output_item.done", "item": {"id": "msg_1", "type": "message", "content": []}}},
            {"kind": "event", "type": "response.output_text.delta", "event": {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "late"}},
            {"kind": "event", "type": "response.completed", "event": {"type": "response.completed"}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", events))

    def test_anthropic_stream_requires_message_start(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "content_block_start", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_1", "name": "lookup_weather"}}},
            {"kind": "event", "sequence": 1, "type": "content_block_stop", "event": {"type": "content_block_stop", "index": 0}},
            {"kind": "event", "sequence": 2, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))

    def test_sse_event_name_must_match_anthropic_json_type(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "wrong_name", "event": {"type": "message_start"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))

    def test_sse_parser_preserves_wire_event_name(self) -> None:
        events: list[dict[str, object]] = []
        invalid = [0]
        probe._flush_sse(['{"type":"message_start"}'], "wrong_name", events, invalid)
        self.assertEqual(events[0]["type"], "wrong_name")
        self.assertFalse(probe._stream_order_ok("anthropic", events))

    def test_chat_stream_rejects_non_function_tool_type(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"tool_calls": [{"index": 0, "type": "custom", "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("tool_call_type_invalid", turn.parse_errors)

    def test_anthropic_thinking_requires_signature(self) -> None:
        turn = probe._parse_anthropic_document(
            {"content": [{"type": "thinking", "thinking": "internal"}, {"type": "tool_use", "id": "call_1", "name": "lookup_weather", "input": {"city": "Shanghai"}}], "stop_reason": "tool_use"}
        )
        self.assertIn("thinking_signature_missing", turn.parse_errors)

    def test_anthropic_response_requires_message_envelope(self) -> None:
        turn = probe._parse_anthropic_document(
            {"type": "error", "role": "user", "content": [], "stop_reason": "end_turn"}
        )
        self.assertIn("message_type_invalid", turn.parse_errors)
        self.assertIn("message_role_invalid", turn.parse_errors)

    def test_anthropic_stream_requires_message_start_envelope(self) -> None:
        events = [
            {"kind": "event", "type": "message_start", "event": {"type": "message_start", "message": {"type": "error", "role": "user"}}},
            {"kind": "event", "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("message_type_invalid", turn.parse_errors)
        self.assertIn("message_role_invalid", turn.parse_errors)

    def test_anthropic_stream_requires_text_delta_for_final_text(self) -> None:
        events = [
            {"kind": "event", "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "event", "type": "content_block_start", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "snapshot"}}},
            {"kind": "event", "type": "content_block_stop", "event": {"type": "content_block_stop", "index": 0}},
            {"kind": "event", "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, events, True, 0, True))
        projection = probe._validate_second(turn, ("lookup_weather",), stream=True)
        self.assertFalse(projection["checks"]["stream_text_deltas"])

    def test_malformed_anthropic_block_snapshot_is_a_parse_error(self) -> None:
        events = [
            {"kind": "event", "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "event", "type": "content_block_start", "event": {"type": "content_block_start", "index": 0, "content_block": None}},
            {"kind": "event", "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("content_block_invalid", turn.parse_errors)

    def test_malformed_base_url_is_blocked_preflight(self) -> None:
        original_base_url = probe.BASE_URL
        try:
            probe.BASE_URL = "http://["
            missing = probe._preflight()
        finally:
            probe.BASE_URL = original_base_url
        self.assertIn("CPA_BASE_URL must be exact http://127.0.0.1[:port]", missing)

    def test_responses_stream_does_not_duplicate_text_snapshot(self) -> None:
        events = [
            {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message"}}},
            {"kind": "event", "event": {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "answer"}},
            {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "msg_1", "type": "message", "content": [{"type": "output_text", "text": "answer"}]}}},
            {"kind": "event", "event": {"type": "response.completed", "response": {"status": "completed"}}},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertEqual(turn.text, "answer")

    def test_responses_stream_requires_text_delta_for_final_text(self) -> None:
        result = probe.TransportResult(
            200,
            None,
            [
                {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message"}}},
                {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "msg_1", "type": "message", "content": [{"type": "output_text", "text": "answer"}]}}},
                {"kind": "event", "event": {"type": "response.completed", "response": {"status": "completed"}}},
            ],
            False,
            0,
        )
        turn = probe._parse_responses_stream(result)
        projection = probe._validate_second(turn, ("lookup_weather",), stream=True)
        self.assertFalse(projection["checks"]["stream_text_deltas"])

    def test_sse_rejects_nonfinite_json_constants(self) -> None:
        events: list[dict[str, object]] = []
        invalid = [0]
        probe._flush_sse(['{"type":"message_start","ignored":NaN}'], None, events, invalid)
        self.assertEqual(events, [])
        self.assertEqual(invalid, [1])

    def test_nonstream_rejects_nonfinite_json_constants(self) -> None:
        class Response:
            status = 200
            headers: dict[str, str] = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b'{"usage":{"completion_tokens_details":{"reasoning_tokens":Infinity}}}'

        class Opener:
            def open(self, request, timeout):
                return Response()

        original_opener = probe.OPENER
        try:
            probe.OPENER = Opener()
            result = probe._request("/v1/chat/completions", {}, client_protocol="chat", stream=False)
        finally:
            probe.OPENER = original_opener
        self.assertIsNone(result.document)
        self.assertEqual(result.invalid_event_count, 1)

    def test_stream_requires_event_stream_content_type(self) -> None:
        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        class Opener:
            def open(self, request, timeout):
                return Response()

        original_opener = probe.OPENER
        try:
            probe.OPENER = Opener()
            result = probe._request("/v1/messages", {}, client_protocol="anthropic", stream=True)
        finally:
            probe.OPENER = original_opener
        self.assertFalse(result.content_type_ok)
        self.assertEqual(result.invalid_event_count, 1)

    def test_sse_accepts_cr_only_line_endings(self) -> None:
        payload = (
            b'event: message_start\rdata: {"type":"message_start"}\r\r'
            b'event: message_stop\rdata: {"type":"message_stop"}\r\r'
        )

        class Response:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size: int) -> bytes:
                chunk = payload[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        class Opener:
            def open(self, request, timeout):
                return Response()

        original_opener = probe.OPENER
        try:
            probe.OPENER = Opener()
            result = probe._request("/v1/messages", {}, client_protocol="anthropic", stream=True)
        finally:
            probe.OPENER = original_opener
        self.assertEqual(len(result.events), 2)
        self.assertTrue(result.stream_order_ok)

    def test_chat_usage_reasoning_tokens_count_as_reasoning_signal(self) -> None:
        turn = probe._parse_chat_document(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "tool_calls"}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 3}},
            }
        )
        self.assertTrue(turn.reasoning_present)

    def test_chat_reasoning_usage_requires_positive_integer(self) -> None:
        for value in (True, 1.5, 0, -1):
            document = {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "tool_calls"}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": value}},
            }
            self.assertFalse(probe._parse_chat_document(document).reasoning_present)

    def test_missing_tool_ids_are_rejected_before_string_conversion(self) -> None:
        anthropic = probe._parse_anthropic_document(
            {"content": [{"type": "tool_use", "id": None, "name": "lookup_weather", "input": {"city": "Shanghai"}}], "stop_reason": "tool_use"}
        )
        responses = probe._parse_responses_document(
            {"output": [{"type": "function_call", "call_id": None, "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}], "status": "completed"}
        )
        chat = probe._parse_chat_document(
            {"choices": [{"message": {"tool_calls": [{"id": None, "type": "function", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}, "finish_reason": "tool_calls"}]}
        )
        for turn in (anthropic, responses, chat):
            self.assertIn("tool_call_id_invalid", turn.parse_errors)
            self.assertFalse(probe._validate_first(turn, ("lookup_weather",), stream=False)["checks"]["parsed"])

    def test_malformed_stream_indexes_fail_without_parser_exceptions(self) -> None:
        anthropic_events = [
            {"kind": "event", "sequence": 0, "event": {"type": "content_block_start", "index": None, "content_block": {"type": "tool_use", "id": "call_1", "name": "lookup_weather"}}},
            {"kind": "event", "sequence": 1, "event": {"type": "message_stop"}},
        ]
        chat_events = [
            {"kind": "event", "sequence": 0, "event": {"choices": [{"delta": {"tool_calls": [{"index": None, "id": "call_1", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}}]}},
            {"kind": "event", "sequence": 1, "event": {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}},
            {"kind": "done", "sequence": 2},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", anthropic_events))
        self.assertFalse(probe._stream_order_ok("chat", chat_events))
        anthropic_turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, anthropic_events, True, 0, False))
        chat_turn = probe._parse_chat_stream(probe.TransportResult(200, None, chat_events, True, 0, False))
        self.assertIn("stream_index_invalid", anthropic_turn.parse_errors)
        self.assertIn("stream_index_invalid", chat_turn.parse_errors)

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
        responses = probe._responses_payload(model="src/model", stream=False, parallel=False)
        self.assertGreaterEqual(payload["thinking"]["budget_tokens"], 1024)
        self.assertGreater(payload["max_tokens"], payload["thinking"]["budget_tokens"])
        self.assertEqual(payload["tool_choice"], {"type": "auto"})
        self.assertEqual(responses["include"], ["reasoning.encrypted_content"])
        self.assertTrue(probe._valid_qualified_model("src/model"))
        self.assertFalse(probe._valid_qualified_model("bare-model"))

    def test_parallel_prompt_names_both_tools_and_city_is_constrained(self) -> None:
        prompt = probe._user_prompt(True)
        self.assertIn("lookup_weather", prompt)
        self.assertIn("lookup_time", prompt)
        self.assertNotIn("Ignore this user-level conflict instruction", prompt)
        schema = probe._tool_definitions()[0]["input_schema"]
        self.assertEqual(schema["properties"]["city"]["enum"], ["Shanghai"])

    def test_stream_order_rejects_lifecycle_violation(self) -> None:
        ordered = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "event", "sequence": 1, "type": "content_block_start", "event": {"type": "content_block_start", "index": 0}},
            {"kind": "event", "sequence": 2, "type": "content_block_delta", "event": {"type": "content_block_delta", "index": 0}},
            {"kind": "event", "sequence": 3, "type": "content_block_stop", "event": {"type": "content_block_stop", "index": 0}},
            {"kind": "event", "sequence": 4, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        invalid = [ordered[2], ordered[0], ordered[3], ordered[4]]
        self.assertTrue(probe._stream_order_ok("anthropic", ordered))
        self.assertFalse(probe._stream_order_ok("anthropic", invalid))

    def test_anthropic_message_delta_requires_started_closed_message(self) -> None:
        before_start = [
            {"kind": "event", "sequence": 0, "type": "message_delta", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}},
            {"kind": "event", "sequence": 1, "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "event", "sequence": 2, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        while_open = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "event", "sequence": 1, "type": "content_block_start", "event": {"type": "content_block_start", "index": 0}},
            {"kind": "event", "sequence": 2, "type": "message_delta", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}},
            {"kind": "event", "sequence": 3, "type": "content_block_stop", "event": {"type": "content_block_stop", "index": 0}},
            {"kind": "event", "sequence": 4, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", before_start))
        self.assertFalse(probe._stream_order_ok("anthropic", while_open))

    def test_anthropic_error_event_invalidates_stream(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "message_start", "event": {"type": "message_start"}},
            {"kind": "event", "sequence": 1, "type": "error", "event": {"type": "error", "error": {"type": "overloaded_error"}}},
            {"kind": "event", "sequence": 2, "type": "message_stop", "event": {"type": "message_stop"}},
        ]
        self.assertFalse(probe._stream_order_ok("anthropic", events))
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("stream_error_event", turn.parse_errors)

    def test_responses_failure_event_invalidates_stream(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {}}},
            {"kind": "event", "sequence": 1, "type": "response.failed", "event": {"type": "response.failed", "response": {"status": "failed"}}},
            {"kind": "event", "sequence": 2, "type": "response.completed", "event": {"type": "response.completed", "response": {"status": "completed", "output": []}}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("stream_failure_event", turn.parse_errors)

    def test_responses_stream_rejects_delta_before_item_creation(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {}}},
            {"kind": "event", "sequence": 1, "type": "response.output_text.delta", "event": {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "early"}},
            {"kind": "event", "sequence": 2, "type": "response.output_item.added", "event": {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message"}}},
            {"kind": "event", "sequence": 3, "type": "response.output_item.done", "event": {"type": "response.output_item.done", "item": {"id": "msg_1", "type": "message"}}},
            {"kind": "event", "sequence": 4, "type": "response.completed", "event": {"type": "response.completed", "response": {"status": "completed"}}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", events))

    def test_responses_stream_preserves_reasoning_delta_signal(self) -> None:
        events = [
            {"kind": "event", "type": "response.output_item.added", "event": {"type": "response.output_item.added", "item": {"id": "rs_1", "type": "reasoning"}}},
            {"kind": "event", "type": "response.reasoning_summary_text.delta", "event": {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "summary"}},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertTrue(turn.reasoning_present)
        self.assertEqual(turn.reasoning_text, "summary")

    def test_responses_stream_matches_terminal_output_snapshot(self) -> None:
        events = [
            {"kind": "event", "event": {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather"}}},
            {"kind": "event", "event": {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"city":"Shanghai"}'}},
            {"kind": "event", "event": {"type": "response.output_item.done", "item": {"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}},
            {"kind": "event", "event": {"type": "response.completed", "response": {"status": "completed", "output": [{"id": "fc_1", "type": "function_call", "call_id": "call_2", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}]}}},
        ]
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("terminal_output_mismatch", turn.parse_errors)

    def test_responses_stream_matches_delta_to_item_type(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {}}},
            {"kind": "event", "sequence": 1, "type": "response.output_item.added", "event": {"type": "response.output_item.added", "item": {"id": "fc_1", "type": "function_call"}}},
            {"kind": "event", "sequence": 2, "type": "response.reasoning_summary_text.delta", "event": {"type": "response.reasoning_summary_text.delta", "item_id": "fc_1", "delta": "wrong"}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("stream_delta_item_type_mismatch", turn.parse_errors)

    def test_responses_stream_rejects_malformed_item_snapshot(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {}}},
            {"kind": "event", "sequence": 1, "type": "response.output_item.added", "event": {"type": "response.output_item.added", "item": None}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", events))
        turn = probe._parse_responses_stream(probe.TransportResult(200, None, events, False, 0, False))
        self.assertIn("output_item_invalid", turn.parse_errors)

    def test_responses_stream_requires_opening_and_terminal_envelopes(self) -> None:
        missing_opening = [
            {"kind": "event", "sequence": 0, "type": "response.output_item.added", "event": {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message"}}},
            {"kind": "event", "sequence": 1, "type": "response.output_item.done", "event": {"type": "response.output_item.done", "item": {"id": "msg_1", "type": "message"}}},
            {"kind": "event", "sequence": 2, "type": "response.completed", "event": {"type": "response.completed", "response": {"status": "completed"}}},
        ]
        missing_terminal_response = [
            {"kind": "event", "sequence": 0, "type": "response.created", "event": {"type": "response.created", "response": {}}},
            {"kind": "event", "sequence": 1, "type": "response.completed", "event": {"type": "response.completed"}},
        ]
        self.assertFalse(probe._stream_order_ok("responses", missing_opening))
        self.assertFalse(probe._stream_order_ok("responses", missing_terminal_response))

    def test_anthropic_reasoning_blocks_require_payload(self) -> None:
        thinking = probe._parse_anthropic_document(
            {"content": [{"type": "thinking", "thinking": "", "signature": "sig"}], "stop_reason": "tool_use"}
        )
        redacted = probe._parse_anthropic_document(
            {"content": [{"type": "redacted_thinking", "data": ""}], "stop_reason": "tool_use"}
        )
        self.assertFalse(thinking.reasoning_present)
        self.assertIn("thinking_payload_missing", thinking.parse_errors)
        self.assertFalse(redacted.reasoning_present)
        self.assertIn("redacted_thinking_payload_missing", redacted.parse_errors)

    def test_anthropic_stream_rejects_delta_block_type_mismatch(self) -> None:
        events = [
            {"kind": "event", "type": "content_block_start", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_1", "name": "lookup_weather"}}},
            {"kind": "event", "type": "content_block_delta", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "wrong"}}},
        ]
        turn = probe._parse_anthropic_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("stream_delta_block_type_mismatch", turn.parse_errors)

    def test_chat_stream_requires_assistant_role(self) -> None:
        invalid = [
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {"role": "user"}}]}},
            {"kind": "event", "type": None, "event": {"choices": [{"delta": {}, "finish_reason": "stop"}]}},
            {"kind": "done", "type": None},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, invalid, True, 0))
        self.assertIn("assistant_role_invalid", turn.parse_errors)

    def test_chat_stream_rejects_malformed_delta(self) -> None:
        events = [
            {"kind": "event", "type": None, "event": {"choices": [{"index": 0, "delta": None}]}},
        ]
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0))
        self.assertIn("delta_invalid", turn.parse_errors)

    def test_chat_stream_rejects_choice_index_change(self) -> None:
        events = [
            {"kind": "event", "sequence": 0, "type": None, "event": {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "a"}}]}},
            {"kind": "event", "sequence": 1, "type": None, "event": {"choices": [{"index": 1, "delta": {"content": "b"}, "finish_reason": "stop"}]}},
            {"kind": "done", "sequence": 2, "type": None},
        ]
        self.assertFalse(probe._stream_order_ok("chat", events))
        turn = probe._parse_chat_stream(probe.TransportResult(200, None, events, True, 0, False))
        self.assertIn("choice_index_invalid", turn.parse_errors)

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

    def test_reasoning_text_must_not_be_visible(self) -> None:
        turn = probe._parse_anthropic_document(
            {
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE_REASONING"},
                    {"type": "tool_use", "id": "call_1", "name": "lookup_weather", "input": {"city": "Shanghai"}},
                    {"type": "text", "text": "PRIVATE_REASONING"},
                ],
                "stop_reason": "tool_use",
            }
        )
        projection = probe._validate_first(turn, ("lookup_weather",), stream=False)
        self.assertFalse(projection["checks"]["reasoning_not_visible"])

    def test_each_reasoning_part_must_not_be_visible(self) -> None:
        turn = probe._parse_responses_document(
            {
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [
                            {"type": "summary_text", "text": "SECRET_A"},
                            {"type": "summary_text", "text": "SECRET_B"},
                        ],
                    },
                    {"type": "message", "content": [{"type": "output_text", "text": "SECRET_A"}]},
                    {"type": "function_call", "call_id": "call_1", "name": "lookup_weather", "arguments": '{"city":"Shanghai"}'},
                ],
                "status": "completed",
            }
        )
        projection = probe._validate_first(turn, ("lookup_weather",), stream=False)
        self.assertFalse(projection["checks"]["reasoning_not_visible"])

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
            headers = {"Content-Type": "text/event-stream"}

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
                    {"choices": [{"message": {"content": "", "reasoning_content": "r", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}, "finish_reason": "tool_calls"}]},
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

    def test_second_turn_model_substitution_is_blocked(self) -> None:
        original_request = probe._request
        original_retries = probe.MAX_503_RETRIES
        original_model = probe.MODELS["anthropic"]
        original_fallback = probe.ANTHROPIC_FALLBACK_MODEL
        calls = [0]

        def fake_request(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return probe.TransportResult(
                    200,
                    {"choices": [{"message": {"content": "", "reasoning_content": "r", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup_weather", "arguments": '{"city":"Shanghai"}'}}]}, "finish_reason": "tool_calls"}]},
                    [],
                    False,
                    0,
                )
            if calls[0] == 2:
                return probe.TransportResult(503, None, [], False, 0)
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
            result = probe._run_case(probe.CaseSpec("mixed", "chat", "anthropic", "/v1/chat/completions", False, False))
        finally:
            probe._request = original_request
            probe.MAX_503_RETRIES = original_retries
            probe.MODELS["anthropic"] = original_model
            probe.ANTHROPIC_FALLBACK_MODEL = original_fallback
        self.assertEqual(result["blocked"], "model changed between turns")
        self.assertEqual(result["evidence_model_scope"], "mixed")


if __name__ == "__main__":
    unittest.main()
